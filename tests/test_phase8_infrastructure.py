from pathlib import Path
import re
import tomllib
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


class Phase8InfrastructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.template_text = (ROOT / "template.yaml").read_text(encoding="utf-8")
        cls.template = yaml.safe_load(cls.template_text)
        cls.resources = cls.template["Resources"]

    def test_environment_and_cross_service_inputs_are_required_and_fail_closed(self):
        parameters = self.template["Parameters"]
        self.assertEqual(
            parameters["EnvironmentName"],
            {"Type": "String", "AllowedValues": ["test", "production"]},
        )
        for name in (
            "ConfigRegistryTableName",
            "ConfigPayloadsBucketName",
            "AuthSessionTableName",
            "AuthUserStateTableName",
        ):
            self.assertEqual(
                parameters[name]["Type"], "AWS::SSM::Parameter::Value<String>"
            )
            self.assertNotIn("Default", parameters[name])
            self.assertNotIn("NoEcho", parameters[name])

        role_list_pattern = (
            r"^arn:(aws|aws-us-gov|aws-cn):iam::[0-9]{12}:role/"
            r"[A-Za-z0-9+=,.@_/-]+(?:,arn:(aws|aws-us-gov|aws-cn):iam::"
            r"[0-9]{12}:role/[A-Za-z0-9+=,.@_/-]+)*$"
        )
        self.assertEqual(parameters["InternalCallerArns"]["AllowedPattern"], role_list_pattern)
        self.assertEqual(parameters["SmtpActivationCallerArns"]["AllowedPattern"], role_list_pattern)
        self.assertNotIn("Default", parameters["InternalCallerArns"])
        self.assertNotIn("Default", parameters["SmtpActivationCallerArns"])
        self.assertEqual(
            parameters["AlarmTopicArn"]["AllowedPattern"],
            r"^arn:(aws|aws-us-gov|aws-cn):sns:[a-z0-9-]+:[0-9]{12}:[A-Za-z0-9_.-]+$",
        )

    def test_stack_publishes_only_safe_service_identifiers_to_ssm(self):
        api_parameter = self.resources["IntegrationApiIdParameter"]["Properties"]
        self.assertEqual(
            api_parameter["Name"],
            {"Fn::Sub": "/zoolanding/${EnvironmentName}/services/integrations/api-id"},
        )
        self.assertEqual(api_parameter["Type"], "String")
        self.assertEqual(api_parameter["Value"], {"Ref": "IntegrationApi"})

        topic_parameter = self.resources["IntegrationEventsTopicArnParameter"]["Properties"]
        self.assertEqual(
            topic_parameter["Name"],
            {"Fn::Sub": "/zoolanding/${EnvironmentName}/topics/integration-events-arn"},
        )
        self.assertEqual(topic_parameter["Type"], "String")
        self.assertEqual(topic_parameter["Value"], {"Ref": "IntegrationEventsTopic"})

        outputs = self.template["Outputs"]
        self.assertEqual(outputs["IntegrationApiId"]["Value"], {"Ref": "IntegrationApi"})
        rendered_outputs = yaml.safe_dump(outputs, sort_keys=True).lower()
        for forbidden in ("secret", "credential", "token", "claimhash"):
            self.assertNotIn(forbidden, rendered_outputs)

    def test_published_policy_and_secret_iam_are_exact(self):
        self.assertNotIn(
            "arn:${AWS::Partition}:s3:::${ConfigPayloadsBucketName}/sites/*\n",
            self.template_text,
        )
        for descriptor in (
            "integration-bindings.json",
            "auth-profile-registry.json",
            "commerce.json",
        ):
            self.assertIn(
                f"sites/*/versions/*/*/server/{descriptor}", self.template_text
            )

        self.assertNotIn("connect-platform*", self.template_text)
        self.assertNotIn("connect-webhook*", self.template_text)
        self.assertIn("connect-platform-??????", self.template_text)
        self.assertIn("connect-webhook-??????", self.template_text)
        self.assertIn("notifications/smtp/*-??????", self.template_text)
        self.assertNotIn("/index/*", self.template_text)
        self.assertIn("/index/MigrationWorkIndex", self.template_text)
        for forbidden in (
            "secretsmanager:PutSecretValue",
            "secretsmanager:UpdateSecret",
            "secretsmanager:CreateSecret",
            "secretsmanager:TagResource",
        ):
            self.assertNotIn(forbidden, self.template_text)

        for generic_role in (
            "BrowserReadRole",
            "BrowserActionRole",
            "ConnectionAdminRole",
            "InternalConnectionResolveRole",
            "InternalMigrationBoundaryRole",
            "InternalMigrationStatusRole",
        ):
            self.assertNotIn(
                "secretsmanager:GetSecretValue", self._resource_text(generic_role)
            )

    def test_required_operational_alarms_target_only_the_operator_topic(self):
        required = {
            "Api5xxAlarm",
            "StripeWebhookErrorsAlarm",
            "StripeWebhookThrottlesAlarm",
            "MigrationWorkerErrorsAlarm",
            "MigrationWorkerThrottlesAlarm",
            "WebhookAgeAlarm",
            "WebhookSignatureFailuresAlarm",
            "MigrationQueueAgeAlarm",
            "MigrationBacklogAlarm",
            "MigrationDlqAgeAlarm",
            "MigrationDlqDepthAlarm",
            "TestLiveMismatchAlarm",
        }
        self.assertTrue(required.issubset(self.resources))
        for logical_id in required:
            alarm = self.resources[logical_id]
            self.assertEqual(alarm["Type"], "AWS::CloudWatch::Alarm")
            self.assertEqual(
                alarm["Properties"]["AlarmActions"], [{"Ref": "AlarmTopicArn"}]
            )
            self.assertEqual(alarm["Properties"]["TreatMissingData"], "notBreaching")

        custom_metrics = {
            self.resources[name]["Properties"]["MetricName"]
            for name in (
                "WebhookAgeAlarm",
                "WebhookSignatureFailuresAlarm",
                "TestLiveMismatchAlarm",
            )
        }
        self.assertEqual(
            custom_metrics,
            {"WebhookAgeSeconds", "WebhookSignatureFailures", "TestLiveMismatch"},
        )
        for name in (
            "WebhookAgeAlarm",
            "WebhookSignatureFailuresAlarm",
            "TestLiveMismatchAlarm",
        ):
            self.assertEqual(
                self.resources[name]["Properties"]["Namespace"],
                "Zoolanding/Integrations",
            )

    def test_ci_runs_on_every_branch_and_deploys_use_protected_artifacts(self):
        ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
        self.assertRegex(ci, r"(?m)^on:\n  push:\s*$\n  pull_request:\s*$")
        self.assertNotIn("id-token: write", ci)
        self.assertIn("Enforce protected promotion graph", ci)
        self.assertIn("python -m unittest discover", ci)
        self.assertIn("sam build --no-cached", ci)
        self.assertIn("python tools/verify_sam_build.py", ci)

        for filename, branch, source, environment in (
            ("deploy-test.yml", "test", "dev", "test"),
            ("deploy-production.yml", "main", "test", "production"),
        ):
            text = (WORKFLOWS / filename).read_text(encoding="utf-8")
            self.assertIn(f"branches: [{branch}]", text)
            self.assertIn(f"SOURCE_BRANCH: {source}", text)
            self.assertIn(f"TARGET_BRANCH: {branch}", text)
            self.assertIn(f"environment: {environment}", text)
            self.assertEqual(text.count("id-token: write"), 1)
            self.assertLess(text.index("environment: "), text.index("id-token: write"))
            self.assertIn("actions/upload-artifact@", text)
            self.assertIn("actions/download-artifact@", text)
            self.assertIn("build-manifest.sha256", text)
            self.assertIn("sha256sum --check --strict", text)
            self.assertGreaterEqual(
                text.count("find .aws-sam/build -type l -print -quit"), 2
            )
            self.assertIn("Reverify exact", text)
            self.assertLess(text.index("Reverify exact"), text.index("configure-aws-credentials@"))
            self.assertIn("Validate exact cross-service SSM values", text)
            self.assertIn("aws ssm get-parameter", text)
            self.assertLess(
                text.index("configure-aws-credentials@"),
                text.index("Validate exact cross-service SSM values"),
            )
            self.assertLess(
                text.index("Validate exact cross-service SSM values"),
                text.index("sam deploy"),
            )
            self.assertIn("sam deploy", text)
            self.assertIn("python tools/verify_sam_build.py", text)
            self.assertIn(f'"EnvironmentName={environment}"', text)
            self.assertIn(
                f'"ConfigRegistryTableName=/zoolanding/{environment}/config/registry-table-name"',
                text,
            )
            self.assertIn(
                f'"ConfigPayloadsBucketName=/zoolanding/{environment}/config/payload-bucket-name"',
                text,
            )
            self.assertIn(
                f'"AuthSessionTableName=/zoolanding/{environment}/auth/session-table-name"',
                text,
            )
            self.assertIn(
                f'"AuthUserStateTableName=/zoolanding/{environment}/auth/user-state-table-name"',
                text,
            )
            self._assert_actions_are_commit_pinned(text)

    def test_samconfig_has_only_test_and_production_deploy_profiles(self):
        with (ROOT / "samconfig.toml").open("rb") as handle:
            config = tomllib.load(handle)
        self.assertEqual(set(config), {"version", "test", "production"})
        self.assertEqual(
            config["test"]["deploy"]["parameters"]["parameter_overrides"],
            ["EnvironmentName=test"],
        )
        self.assertEqual(
            config["production"]["deploy"]["parameters"]["parameter_overrides"],
            ["EnvironmentName=production"],
        )
        self.assertNotIn("dev", (ROOT / "samconfig.toml").read_text(encoding="utf-8").lower())

    def test_readiness_smoke_is_present_and_no_cli_secret_surface_exists(self):
        path = ROOT / "tools" / "integration_platform_readiness_smoke.py"
        self.assertTrue(path.exists())
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("--token", text)
        self.assertNotIn("--secret", text)
        self.assertNotIn("--password", text)
        self.assertNotIn("print(request", text)
        self.assertNotIn("print(response", text)

    def test_readme_documents_fail_closed_bootstrap_and_no_deployment_claim(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for required in (
            "/zoolanding/{environment}/config/registry-table-name",
            "/zoolanding/{environment}/config/payload-bucket-name",
            "/zoolanding/{environment}/auth/session-table-name",
            "/zoolanding/{environment}/auth/user-state-table-name",
            "/zoolanding/{environment}/services/integrations/api-id",
            "/zoolanding/{environment}/topics/integration-events-arn",
            "INTERNAL_CALLER_ARNS",
            "SMTP_ACTIVATION_CALLER_ARNS",
            "ALARM_TOPIC_ARN",
            "SMTP_TEST_SHARED_ACCOUNT_CLAIM_HASH",
            "ZLP_INTEGRATIONS_SMOKE_API_URL",
            "No AWS deployment was performed",
        ):
            self.assertIn(required, readme)
        self.assertIn("consumer roles first", readme)
        self.assertIn("dev -> test -> main", readme)

    def _resource_text(self, logical_id):
        return yaml.safe_dump(self.resources[logical_id], sort_keys=True)

    def _assert_actions_are_commit_pinned(self, text):
        uses = re.findall(r"(?m)^\s*-?\s*uses:\s*([^\s#]+)", text)
        self.assertTrue(uses)
        for action in uses:
            if action.startswith("./"):
                continue
            self.assertRegex(action, r"^[^@]+@[a-f0-9]{40}$")


if __name__ == "__main__":
    unittest.main()
