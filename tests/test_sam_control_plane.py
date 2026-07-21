from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class SamControlPlaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = (ROOT / "template.yaml").read_text(encoding="utf-8")
        cls.template = yaml.safe_load(cls.text)
        cls.resources = cls.template["Resources"]

    def test_browser_and_internal_routes_are_literal_and_separately_authorized(self):
        self.assertEqual(
            self.resources["IntegrationApi"]["Type"], "AWS::Serverless::Api"
        )
        expected_browser = {
            "ConnectionReadFunction": "/features/integrations/read",
            "ConnectionActionFunction": "/features/integrations/action",
            "StripeOnboardingFunction": "/features/integrations/stripe/onboarding",
        }
        expected_internal = {
            "InternalStripeOfferFunction": "/internal/v1/stripe/offer",
            "InternalStripeProductPresentationFunction": "/internal/v1/stripe/product-presentation",
            "InternalStripeDiscountFunction": "/internal/v1/stripe/discount",
            "InternalStripeDiscountLifecycleFunction": "/internal/v1/stripe/discount-lifecycle",
            "InternalStripeCheckoutFunction": "/internal/v1/stripe/checkout",
            "InternalStripeCheckoutStatusFunction": "/internal/v1/stripe/checkout-status",
            "InternalStripeSubscriptionChangeFunction": "/internal/v1/stripe/subscription/change",
            "InternalStripeSubscriptionDiscountFunction": "/internal/v1/stripe/subscription/discount",
            "InternalStripeSubscriptionPauseFunction": "/internal/v1/stripe/subscription/pause",
            "InternalStripeCustomerPortalFunction": "/internal/v1/stripe/customer-portal",
            "InternalStripeMigrationsPreviewFunction": "/internal/v1/stripe/migrations/preview",
            "InternalStripeMigrationsExecuteFunction": "/internal/v1/stripe/migrations/execute",
            "InternalStripeMigrationsControlFunction": "/internal/v1/stripe/migrations/control",
            "InternalStripeMigrationsStatusFunction": "/internal/v1/stripe/migrations/status",
            "InternalConnectionRegisterFunction": "/internal/v1/integrations/connection-register",
            "InternalConnectionResolveFunction": "/internal/v1/integrations/connection-resolve",
        }
        for logical_id, path in expected_browser.items():
            event = self._api_event(logical_id)
            self.assertEqual(event["Properties"]["Path"], path)
            self.assertNotIn("Auth", event["Properties"])
        for logical_id, path in expected_internal.items():
            event = self._api_event(logical_id)
            self.assertEqual(event["Properties"]["Path"], path)
            self.assertEqual(event["Properties"]["Auth"]["Authorizer"], "AWS_IAM")

    def test_handlers_are_small_separate_entrypoints_with_bounded_roles(self):
        function_ids = {
            logical_id
            for logical_id, resource in self.resources.items()
            if resource.get("Type") == "AWS::Serverless::Function"
        }
        required = {
            "ConnectionReadFunction",
            "ConnectionActionFunction",
            "StripeOnboardingFunction",
            "InternalConnectionRegisterFunction",
            "InternalConnectionResolveFunction",
            "InternalStripeMigrationsPreviewFunction",
            "InternalStripeMigrationsExecuteFunction",
            "InternalStripeMigrationsControlFunction",
            "InternalStripeMigrationsStatusFunction",
        }
        self.assertTrue(required.issubset(function_ids))
        self.assertNotIn("Resource: '*'", self.text)
        self.assertNotIn('Resource: "*"', self.text)
        self.assertNotIn(
            "secretsmanager:GetSecretValue", self._role("ConnectionAdminRole")
        )
        self.assertIn(
            "secretsmanager:DescribeSecret", self._role("ConnectionAdminRole")
        )
        connection_admin_role = self._role("ConnectionAdminRole")
        self.assertIn(
            "/zoolanding/${EnvironmentName}/integrations/stripe/connect-platform*",
            connection_admin_role,
        )
        self.assertIn(
            "/zoolanding/${EnvironmentName}/*/*/notifications/smtp/*",
            connection_admin_role,
        )
        self.assertIn(
            "secretsmanager:GetSecretValue", self._role("StripeOnboardingRole")
        )
        for name in (
            "InternalMigrationBoundaryRole",
            "InternalConnectionResolveRole",
        ):
            self.assertNotIn("secretsmanager:", self._role(name))

    def test_browser_action_transaction_is_limited_to_the_registry_table(self):
        statements = self.resources["BrowserActionRole"]["Properties"]["Policies"][
            0
        ]["PolicyDocument"]["Statement"]
        transaction = [
            statement
            for statement in statements
            if "dynamodb:TransactWriteItems"
            in (
                statement["Action"]
                if isinstance(statement["Action"], list)
                else [statement["Action"]]
            )
        ]
        self.assertEqual(
            transaction,
            [
                {
                    "Effect": "Allow",
                    "Action": [
                        "dynamodb:UpdateItem",
                        "dynamodb:TransactWriteItems",
                    ],
                    "Resource": {
                        "Fn::GetAtt": ["IntegrationRegistryTable", "Arn"]
                    },
                }
            ],
        )

    def test_runtime_inputs_are_explicit_and_no_dev_or_cache_surface_exists(self):
        parameters = self.template["Parameters"]
        for name in (
            "ConfigRegistryTableName",
            "ConfigPayloadsBucketName",
            "AuthSessionTableName",
            "AuthUserStateTableName",
            "InternalCallerArns",
        ):
            self.assertIn(name, parameters)
        self.assertNotIn("StripeSecretsPrefixArn", parameters)
        self.assertNotIn("IntegrationSecretsPrefixArn", parameters)
        for role in ("StripeOnboardingRole", "InternalProviderCommandRole"):
            rendered = self._role(role)
            self.assertIn("${AWS::Partition}", rendered)
            self.assertIn(
                "/zoolanding/${EnvironmentName}/integrations/stripe/connect-platform*",
                rendered,
            )
            self.assertNotIn("integrations/*/*/stripe", rendered)
            self.assertNotIn("PrefixArn", rendered)
        self.assertIn("Runtime: python3.13", self.text)
        self.assertNotIn("Cache", self.text)
        self.assertNotIn("dev", self.text.lower())
        self.assertNotIn("DeploymentPreference", self.text)
        self.assertEqual(
            self.resources["IntegrationRegistryTable"]["Properties"][
                "TimeToLiveSpecification"
            ],
            {"AttributeName": "expiresAt", "Enabled": True},
        )

    def test_webhook_worker_and_relay_are_separate_least_privilege_functions(self):
        webhook = self.resources["StripeWebhookFunction"]["Properties"]
        worker = self.resources["WebhookIngressStreamFunction"]["Properties"]
        relay = self.resources["IntegrationOutgoingStreamFunction"]["Properties"]
        self.assertEqual(webhook["Handler"], "handlers.stripe_webhook.lambda_handler")
        public_event = self._api_event("StripeWebhookFunction")
        self.assertEqual(public_event["Properties"]["Path"], "/webhooks/stripe/connect")
        self.assertEqual(public_event["Properties"]["Method"], "post")
        self.assertNotIn("Auth", public_event["Properties"])
        self.assertEqual(
            worker["Handler"], "handlers.stripe_event_worker.lambda_handler"
        )
        self.assertEqual(
            relay["Handler"], "handlers.integration_outbox_relay.lambda_handler"
        )
        self.assertEqual(
            worker["Environment"]["Variables"]["WEBHOOK_RECEIPT_TABLE_NAME"],
            {"Ref": "WebhookReceiptTable"},
        )
        self.assertEqual(
            relay["Environment"]["Variables"]["INTEGRATION_EVENTS_TOPIC_ARN"],
            {"Ref": "IntegrationEventsTopic"},
        )
        ingress_role = self._role("StripeWebhookRole")
        worker_role = self._role("WebhookIngressStreamRole")
        relay_role = self._role("IntegrationOutgoingStreamRole")
        self.assertIn(
            "/zoolanding/${EnvironmentName}/integrations/stripe/connect-webhook",
            ingress_role,
        )
        self.assertNotIn("sns:Publish", ingress_role)
        self.assertIn("dynamodb:TransactWriteItems", worker_role)
        self.assertIn("secretsmanager:GetSecretValue", worker_role)
        self.assertNotIn("sns:Publish", worker_role)
        self.assertIn("sns:Publish", relay_role)
        self.assertNotIn("secretsmanager:GetSecretValue", relay_role)
        self.assertNotIn("handlers.pending_stream", self.text)

    def _api_event(self, logical_id):
        events = self.resources[logical_id]["Properties"]["Events"]
        matches = [event for event in events.values() if event["Type"] == "Api"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(
            matches[0]["Properties"]["RestApiId"], {"Ref": "IntegrationApi"}
        )
        return matches[0]

    def _role(self, logical_id):
        resource = self.resources[logical_id]
        return yaml.safe_dump(resource, sort_keys=True)


if __name__ == "__main__":
    unittest.main()
