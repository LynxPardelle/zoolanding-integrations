from pathlib import Path
import importlib
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

    def test_sam_foundation_has_exact_storage_stream_and_topic_boundaries(self):
        template = (ROOT / "template.yaml").read_text(encoding="utf-8")
        for resource in (
            "IntegrationRegistryTable",
            "WebhookReceiptTable",
            "WebhookIngressStreamFunction",
            "IntegrationOutgoingStreamFunction",
            "WebhookIngressFailureQueue",
            "IntegrationOutgoingFailureQueue",
            "IntegrationEventsTopic",
        ):
            self.assertIn(f"  {resource}:", template)
        self.assertIn("Runtime: python3.12", template)
        self.assertEqual(template.count("BillingMode: PAY_PER_REQUEST"), 2)
        self.assertEqual(template.count("PointInTimeRecoveryEnabled: true"), 2)
        self.assertEqual(template.count("SSEEnabled: true"), 2)
        self.assertIn("AttributeName: expiresAt", template)
        self.assertIn("StreamViewType: NEW_IMAGE", template)
        self.assertEqual(template.count("ReportBatchItemFailures"), 2)
        self.assertIn("dynamodb:ListStreams", template)
        self.assertIn('itemType":{"S":["WebhookIngressOutbox"]}', template)
        self.assertIn('itemType":{"S":["IntegrationEventOutbox"]}', template)
        self.assertNotIn("AWS::Serverless::Api", template)
        self.assertNotIn("Type: Api", template)
        self.assertNotIn("Tracing: Active", template)

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


if __name__ == "__main__":
    unittest.main()
