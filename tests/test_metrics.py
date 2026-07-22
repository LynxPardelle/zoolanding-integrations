from contextlib import redirect_stdout
from io import StringIO
import json
import unittest


class MetricsTests(unittest.TestCase):
    def test_emf_metric_contains_only_closed_operational_fields(self):
        from src.common.metrics import emit_metric

        output = StringIO()
        with redirect_stdout(output):
            emit_metric("WebhookAgeSeconds", 15, environment="test")
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["Environment"], "test")
        self.assertEqual(payload["WebhookAgeSeconds"], 15)
        self.assertEqual(
            payload["_aws"]["CloudWatchMetrics"][0]["Namespace"],
            "Zoolanding/Integrations",
        )
        rendered = output.getvalue().lower()
        for forbidden in ("email", "account", "payload", "secret", "token"):
            self.assertNotIn(forbidden, rendered)

    def test_emf_metric_rejects_unknown_names_values_and_environments(self):
        from src.common.metrics import emit_metric

        for args in (
            ("UnknownMetric", 1, "test"),
            ("WebhookAgeSeconds", -1, "test"),
            ("WebhookAgeSeconds", 1.5, "test"),
            ("WebhookAgeSeconds", 1, "dev"),
        ):
            with self.subTest(args=args), self.assertRaises(ValueError):
                emit_metric(args[0], args[1], environment=args[2])


if __name__ == "__main__":
    unittest.main()
