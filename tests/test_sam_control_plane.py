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
            "InternalSmtpConnectionActivateFunction": "/internal/v1/integrations/smtp-connection-activate",
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
            "InternalSmtpConnectionActivateFunction",
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
            "/zoolanding/${EnvironmentName}/integrations/stripe/connect-platform-??????",
            connection_admin_role,
        )
        self.assertIn(
            "/zoolanding/${EnvironmentName}/*/*/notifications/smtp/*-??????",
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
                "/zoolanding/${EnvironmentName}/integrations/stripe/connect-platform-??????",
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

    def test_smtp_activation_has_a_separate_metadata_only_role(self):
        function = self.resources["InternalSmtpConnectionActivateFunction"]["Properties"]
        self.assertEqual(
            function["Handler"],
            "handlers.internal_smtp_connection_activate.lambda_handler",
        )
        self.assertEqual(
            function["Role"], {"Fn::GetAtt": ["SmtpConnectionActivationRole", "Arn"]}
        )
        self.assertEqual(
            function["Environment"]["Variables"],
            {
                "SMTP_TEST_SHARED_ACCOUNT_CLAIM_HASH": {
                    "Ref": "SmtpTestSharedAccountClaimHash"
                },
                "SMTP_ACTIVATION_CALLER_ARNS": {
                    "Ref": "SmtpActivationCallerArns"
                },
            },
        )
        self.assertIn("SmtpActivationCallerArns", self.template["Parameters"])
        self.assertEqual(self.text.count("SMTP_ACTIVATION_CALLER_ARNS"), 1)
        role = self._role("SmtpConnectionActivationRole")
        self.assertIn("secretsmanager:DescribeSecret", role)
        self.assertIn("dynamodb:TransactWriteItems", role)
        self.assertIn("dynamodb:GetItem", role)
        for forbidden in (
            "secretsmanager:GetSecretValue",
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "sns:",
            "sqs:",
        ):
            self.assertNotIn(forbidden, role)
        self.assertNotIn("Resource: '*'", role)
        self.assertEqual(
            self.resources["InternalConnectionResolveFunction"]["Properties"]["Environment"]["Variables"],
            {"SMTP_TEST_SHARED_ACCOUNT_CLAIM_HASH": {"Ref": "SmtpTestSharedAccountClaimHash"}},
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
            worker["Environment"]["Variables"]["MIGRATION_WORK_QUEUE_URL"],
            {"Ref": "SubscriptionMigrationWorkQueue"},
        )
        self.assertEqual(
            relay["Environment"]["Variables"]["INTEGRATION_EVENTS_TOPIC_ARN"],
            {"Ref": "IntegrationEventsTopic"},
        )
        ingress_role = self._role("StripeWebhookRole")
        worker_role = self._role("WebhookIngressStreamRole")
        self.assertIn("SubscriptionMigrationWorkQueue", worker_role)
        relay_role = self._role("IntegrationOutgoingStreamRole")
        self.assertIn(
            "/zoolanding/${EnvironmentName}/integrations/stripe/connect-webhook",
            ingress_role,
        )
        self.assertNotIn("sns:Publish", ingress_role)
        self.assertIn("dynamodb:TransactWriteItems", worker_role)
        self.assertIn("dynamodb:PutItem", worker_role)
        self.assertIn("dynamodb:UpdateItem", worker_role)
        self.assertIn("IntegrationRegistryTable", worker_role)
        self.assertIn("WebhookReceiptTable", worker_role)
        self.assertIn("secretsmanager:GetSecretValue", worker_role)
        self.assertNotIn("sns:Publish", worker_role)
        self.assertIn("sns:Publish", relay_role)
        self.assertNotIn("secretsmanager:GetSecretValue", relay_role)
        self.assertNotIn("handlers.pending_stream", self.text)

    def test_subscription_migration_queue_is_standard_encrypted_and_redrives_after_five_receives(self):
        self.assertIn("SubscriptionMigrationDeadLetterQueue", self.resources)
        self.assertIn("SubscriptionMigrationWorkQueue", self.resources)
        dead_letter_queue = self.resources["SubscriptionMigrationDeadLetterQueue"]
        work_queue = self.resources["SubscriptionMigrationWorkQueue"]

        self.assertEqual(dead_letter_queue["Type"], "AWS::SQS::Queue")
        self.assertEqual(work_queue["Type"], "AWS::SQS::Queue")
        for queue in (dead_letter_queue, work_queue):
            properties = queue["Properties"]
            self.assertTrue(properties["SqsManagedSseEnabled"])
            self.assertEqual(properties["MessageRetentionPeriod"], 1_209_600)
            self.assertNotIn("FifoQueue", properties)
        self.assertEqual(work_queue["Properties"]["VisibilityTimeout"], 180)
        self.assertEqual(
            work_queue["Properties"]["RedrivePolicy"],
            {
                "deadLetterTargetArn": {
                    "Fn::GetAtt": ["SubscriptionMigrationDeadLetterQueue", "Arn"]
                },
                "maxReceiveCount": 5,
            },
        )

    def test_subscription_migration_worker_has_bounded_partial_batch_processing(self):
        self.assertIn("SubscriptionMigrationWorkerFunction", self.resources)
        worker = self.resources["SubscriptionMigrationWorkerFunction"]["Properties"]
        self.assertEqual(
            worker["Handler"], "handlers.subscription_migration_worker.lambda_handler"
        )
        self.assertEqual(worker["Timeout"], 30)
        self.assertEqual(worker["ReservedConcurrentExecutions"], 5)
        self.assertEqual(
            worker["Environment"]["Variables"],
            {
                "MIGRATION_WORK_QUEUE_URL": {
                    "Ref": "SubscriptionMigrationWorkQueue"
                },
                "WEBHOOK_RECEIPT_TABLE_NAME": {"Ref": "WebhookReceiptTable"},
            },
        )
        event = worker["Events"]["MigrationWork"]
        self.assertEqual(event["Type"], "SQS")
        self.assertEqual(
            event["Properties"],
            {
                "Queue": {
                    "Fn::GetAtt": ["SubscriptionMigrationWorkQueue", "Arn"]
                },
                "BatchSize": 1,
                "FunctionResponseTypes": ["ReportBatchItemFailures"],
                "ScalingConfig": {"MaximumConcurrency": 5},
            },
        )

        role = self._role("SubscriptionMigrationWorkerRole")
        for action in (
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:Query",
            "dynamodb:UpdateItem",
            "dynamodb:TransactWriteItems",
            "sqs:ReceiveMessage",
            "sqs:DeleteMessage",
            "sqs:GetQueueAttributes",
            "sqs:SendMessage",
            "secretsmanager:GetSecretValue",
        ):
            self.assertIn(action, role)
        self.assertIn("IntegrationRegistryTable", role)
        self.assertIn("WebhookReceiptTable", role)
        self.assertIn("SubscriptionMigrationWorkQueue", role)
        self.assertIn(
            "/zoolanding/${EnvironmentName}/integrations/stripe/connect-platform-??????",
            role,
        )
        self.assertNotIn("sns:Publish", role)

    def test_migration_work_uses_a_sparse_bounded_dynamodb_index(self):
        table = self.resources["IntegrationRegistryTable"]["Properties"]
        self.assertEqual(
            table["GlobalSecondaryIndexes"],
            [{
                "IndexName": "MigrationWorkIndex",
                "KeySchema": [
                    {"AttributeName": "migrationWorkPk", "KeyType": "HASH"},
                    {"AttributeName": "migrationWorkSk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }],
        )
        attributes = {
            item["AttributeName"] for item in table["AttributeDefinitions"]
        }
        self.assertTrue({"migrationWorkPk", "migrationWorkSk"}.issubset(attributes))
        self.assertIn(
            "/index/MigrationWorkIndex",
            self._role("SubscriptionMigrationWorkerRole"),
        )
        self.assertNotIn("/index/*", self.text)

    def test_migration_apis_have_command_and_read_only_status_roles(self):
        for logical_id in (
            "InternalStripeMigrationsPreviewFunction",
            "InternalStripeMigrationsExecuteFunction",
            "InternalStripeMigrationsControlFunction",
        ):
            function = self.resources[logical_id]["Properties"]
            self.assertIn("Environment", function)
            self.assertEqual(
                function["Role"], {"Fn::GetAtt": ["InternalMigrationBoundaryRole", "Arn"]}
            )
            self.assertEqual(
                function["Environment"]["Variables"],
                {
                    "MIGRATION_WORK_QUEUE_URL": {
                        "Ref": "SubscriptionMigrationWorkQueue"
                    },
                    "WEBHOOK_RECEIPT_TABLE_NAME": {"Ref": "WebhookReceiptTable"},
                },
            )

        self.assertEqual(
            self.resources["InternalStripeMigrationsStatusFunction"]["Properties"][
                "Role"
            ],
            {"Fn::GetAtt": ["InternalMigrationStatusRole", "Arn"]},
        )
        self.assertNotIn(
            "Environment",
            self.resources["InternalStripeMigrationsStatusFunction"]["Properties"],
        )
        command_role = self._role("InternalMigrationBoundaryRole")
        for action in (
            "dynamodb:GetItem",
            "dynamodb:PutItem",
            "dynamodb:Query",
            "dynamodb:UpdateItem",
            "dynamodb:TransactWriteItems",
            "sqs:SendMessage",
        ):
            self.assertIn(action, command_role)
        self.assertIn("IntegrationRegistryTable", command_role)
        self.assertIn("WebhookReceiptTable", command_role)
        self.assertIn("SubscriptionMigrationWorkQueue", command_role)
        self.assertNotIn("secretsmanager:", command_role)
        self.assertNotIn("sns:Publish", command_role)

        status_role = self._role("InternalMigrationStatusRole")
        self.assertIn("IntegrationRegistryReadPolicy", status_role)
        for forbidden in (
            "dynamodb:PutItem",
            "dynamodb:UpdateItem",
            "dynamodb:TransactWriteItems",
            "sqs:",
            "secretsmanager:",
            "sns:",
        ):
            self.assertNotIn(forbidden, status_role)

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
