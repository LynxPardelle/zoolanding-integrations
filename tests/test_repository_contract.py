from pathlib import Path
import importlib
import os
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RepositoryContractTests(unittest.TestCase):
    def test_dependencies_are_exact_and_documented(self):
        requirements = (
            (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        )
        self.assertEqual(requirements, ["boto3==1.39.13", "stripe==15.3.1"])
        self.assertEqual(
            (ROOT / "src" / "requirements.txt")
            .read_text(encoding="utf-8")
            .splitlines(),
            requirements,
        )
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("official Stripe Python SDK", readme)
        self.assertIn("sole new runtime dependency", readme)

        development_requirements = (
            (ROOT / "requirements-dev.txt")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        self.assertEqual(
            development_requirements,
            ["-r requirements.txt", "PyYAML==6.0.2"],
        )
        self.assertIn("requirements-dev.txt", readme)

    def test_sam_foundation_has_exact_storage_stream_and_topic_boundaries(self):
        template = (ROOT / "template.yaml").read_text(encoding="utf-8")
        for resource in (
            "IntegrationRegistryTable",
            "WebhookReceiptTable",
            "WebhookIngressStreamFunction",
            "IntegrationOutgoingStreamFunction",
            "WebhookIngressFailureQueue",
            "IntegrationOutgoingFailureQueue",
            "SubscriptionMigrationDeadLetterQueue",
            "SubscriptionMigrationWorkQueue",
            "SubscriptionMigrationWorkerFunction",
            "IntegrationEventsTopic",
        ):
            self.assertIn(f"  {resource}:", template)
        self.assertIn("Runtime: python3.13", template)
        self.assertEqual(template.count("BillingMode: PAY_PER_REQUEST"), 2)
        self.assertEqual(template.count("PointInTimeRecoveryEnabled: true"), 2)
        self.assertEqual(template.count("SSEEnabled: true"), 2)
        self.assertIn("AttributeName: expiresAt", template)
        self.assertIn("StreamViewType: NEW_IMAGE", template)
        self.assertEqual(template.count("ReportBatchItemFailures"), 3)
        self.assertIn("dynamodb:ListStreams", template)
        self.assertIn('itemType":{"S":["WebhookIngressOutbox"]}', template)
        self.assertIn('itemType":{"S":["IntegrationEventOutbox"]}', template)
        self.assertIn("AWS::Serverless::Api", template)
        self.assertIn("Type: Api", template)
        self.assertNotIn("Tracing: Active", template)
        self.assertIn("WebhookIngressStreamRole:", template)
        self.assertIn("IntegrationOutgoingStreamRole:", template)
        self.assertNotIn("StreamBoundaryRole:", template)
        ingress_role = template.split("  WebhookIngressStreamRole:", 1)[1].split(
            "  IntegrationOutgoingStreamRole:", 1
        )[0]
        outgoing_role = template.split("  IntegrationOutgoingStreamRole:", 1)[1].split(
            "  WebhookIngressStreamFunction:", 1
        )[0]
        self.assertIn("WebhookIngressFailureQueue", ingress_role)
        self.assertNotIn("IntegrationOutgoingFailureQueue", ingress_role)
        self.assertIn("IntegrationOutgoingFailureQueue", outgoing_role)
        self.assertNotIn("WebhookIngressFailureQueue", outgoing_role)

    def test_pending_stream_boundary_fails_closed_without_payload_logging(self):
        try:
            handler = importlib.import_module("src.handlers.pending_stream")
        except ModuleNotFoundError as exc:
            self.fail(f"pending stream boundary is not implemented: {exc}")
        result = handler.lambda_handler(
            {"Records": [{"eventID": "record-1"}, {"eventID": "record-2"}]}, None
        )
        self.assertEqual(
            result,
            {
                "batchItemFailures": [
                    {"itemIdentifier": "record-1"},
                    {"itemIdentifier": "record-2"},
                ]
            },
        )

    def test_every_sam_handler_imports_from_the_lambda_code_root(self):
        handlers = (
            "handlers.connection_read",
            "handlers.connection_action",
            "handlers.stripe_onboarding",
            "handlers.internal_connection_register",
            "handlers.internal_connection_resolve",
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        for name in handlers:
            with self.subTest(handler=name):
                result = subprocess.run(
                    [sys.executable, "-c", f"import {name}"],
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_repository_has_no_dev_or_deployment_surface(self):
        self.assertFalse((ROOT / "samconfig.toml").exists())
        self.assertFalse((ROOT / ".github" / "workflows" / "deploy-dev.yml").exists())
        self.assertFalse((ROOT / ".github" / "workflows" / "deploy-test.yml").exists())
        self.assertFalse(
            (ROOT / ".github" / "workflows" / "deploy-production.yml").exists()
        )
        for path in ROOT.rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                self.assertNotIn("deploy-dev", path.name.lower())

    def test_task_041_042_runtime_composition_is_available_to_small_handlers(self):
        runtime = importlib.import_module("src.runtime")
        self.assertTrue(callable(getattr(runtime, "stripe_command_runtime", None)))
        for name in (
            "internal_stripe_offer",
            "internal_stripe_product_presentation",
            "internal_stripe_discount",
            "internal_stripe_discount_lifecycle",
            "internal_stripe_checkout",
            "internal_stripe_checkout_status",
        ):
            module = importlib.import_module(f"src.handlers.{name}")
            self.assertIn(
                "stripe_command_runtime", module._runtime_dependencies.__code__.co_names
            )

    def test_stripe_webhook_runtime_is_available_to_the_public_ingress_handler(self):
        runtime = importlib.import_module("src.runtime")
        handler = importlib.import_module("src.handlers.stripe_webhook")
        self.assertTrue(callable(getattr(runtime, "stripe_webhook_runtime", None)))
        self.assertIn(
            "stripe_webhook_runtime", handler._runtime_dependencies.__code__.co_names
        )

    def test_worker_and_outbox_relay_have_separate_runtime_composition(self):
        runtime = importlib.import_module("src.runtime")
        worker = importlib.import_module("src.handlers.stripe_event_worker")
        relay = importlib.import_module("src.handlers.integration_outbox_relay")
        self.assertTrue(callable(getattr(runtime, "stripe_event_worker_runtime", None)))
        self.assertTrue(
            callable(getattr(runtime, "integration_outbox_relay_runtime", None))
        )
        self.assertIn(
            "stripe_event_worker_runtime", worker._runtime_worker.__code__.co_names
        )
        self.assertIn(
            "integration_outbox_relay_runtime", relay._runtime_relay.__code__.co_names
        )


if __name__ == "__main__":
    unittest.main()
