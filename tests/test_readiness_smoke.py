from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


def environment():
    return {
        "ZLP_INTEGRATIONS_SMOKE_API_URL": (
            "https://abcdefghij.execute-api.us-east-1.amazonaws.com/test"
        ),
        "ZLP_INTEGRATIONS_SMOKE_TENANT_ID": "tenant-example",
        "ZLP_INTEGRATIONS_SMOKE_DRAFT_ID": "draft-example",
        "ZLP_INTEGRATIONS_SMOKE_DOMAIN": "example.com",
        "ZLP_INTEGRATIONS_SMOKE_CONNECTION_ID": "billing-mailbox",
        "AWS_REGION": "us-east-1",
    }


class ReadinessSmokeTests(unittest.TestCase):
    def smoke(self):
        from tools import integration_platform_readiness_smoke

        return integration_platform_readiness_smoke

    def test_classifies_success_auth_configuration_provider_and_propagation(self):
        smoke = self.smoke()
        cases = (
            (200, {}, "ready", True),
            (403, {}, "auth_failure", False),
            (400, {}, "configuration_failure", False),
            (502, {}, "provider_failure", False),
            (
                404,
                {"ZLP_INTEGRATIONS_SMOKE_PROPAGATION_UNTIL_EPOCH": "1800000030"},
                "propagation_delay",
                False,
            ),
            (404, {}, "configuration_failure", False),
        )
        for status, extra, classification, ok in cases:
            with self.subTest(status=status, classification=classification):
                values = {**environment(), **extra}
                result = smoke.run(
                    values,
                    sender=lambda request, value=status: smoke.SmokeResponse(value),
                    now_epoch=lambda: 1_800_000_000,
                )
                self.assertEqual(result["classification"], classification)
                self.assertEqual(result["ok"], ok)
                self.assertEqual(result.get("httpStatus"), status)
                self.assertIn("environment", result)
                self.assertIn("observedAtEpoch", result)
                self.assertEqual(result["environment"], "test")
                self.assertEqual(result["observedAtEpoch"], 1_800_000_000)
                self.assertEqual(
                    set(result),
                    {
                        "ok",
                        "classification",
                        "httpStatus",
                        "attempts",
                        "environment",
                        "observedAtEpoch",
                    },
                )

    def test_missing_input_fails_before_transport(self):
        smoke = self.smoke()
        called = []
        values = environment()
        del values["ZLP_INTEGRATIONS_SMOKE_DOMAIN"]
        result = smoke.run(
            values,
            sender=lambda request: called.append(request),
            now_epoch=lambda: 1_800_000_000,
        )
        self.assertEqual(
            result,
            {
                "ok": False,
                "classification": "missing_input",
                "attempts": 0,
                "environment": None,
                "observedAtEpoch": 1_800_000_000,
            },
        )
        self.assertEqual(called, [])

    def test_malformed_url_transport_and_deadline_fail_closed(self):
        smoke = self.smoke()
        called = []
        values = {
            **environment(),
            "ZLP_INTEGRATIONS_SMOKE_API_URL": (
                "https://abcdefghij.execute-api.us-east-1.amazonaws.com:invalid/test"
            ),
        }
        self.assertEqual(
            smoke.run(
                values,
                sender=lambda request: called.append(request),
                now_epoch=lambda: 1_800_000_000,
            ),
            {
                "ok": False,
                "classification": "missing_input",
                "attempts": 0,
                "environment": None,
                "observedAtEpoch": 1_800_000_000,
            },
        )
        self.assertEqual(called, [])

        self.assertEqual(
            smoke.run(
                environment(),
                sender=lambda request: object(),
                now_epoch=lambda: 1_800_000_000,
            ),
            {
                "ok": False,
                "classification": "provider_failure",
                "attempts": 1,
                "environment": "test",
                "observedAtEpoch": 1_800_000_000,
            },
        )

        self.assertEqual(
            smoke.run(
                environment(),
                sender=lambda request: (_ for _ in ()).throw(smoke.SmokeAuthError()),
                now_epoch=lambda: 1_800_000_000,
            ),
            {
                "ok": False,
                "classification": "auth_failure",
                "attempts": 1,
                "environment": "test",
                "observedAtEpoch": 1_800_000_000,
            },
        )

        huge_deadline = {
            **environment(),
            "ZLP_INTEGRATIONS_SMOKE_PROPAGATION_UNTIL_EPOCH": "9" * 10_000,
        }
        self.assertEqual(
            smoke.run(
                huge_deadline,
                sender=lambda request: smoke.SmokeResponse(404),
                now_epoch=lambda: 1_800_000_000,
            )["classification"],
            "configuration_failure",
        )

    def test_request_is_exact_safe_and_never_contains_credentials(self):
        smoke = self.smoke()
        captured = []
        values = {
            **environment(),
            "AWS_ACCESS_KEY_ID": "DO-NOT-PRINT-ACCESS",
            "AWS_SECRET_ACCESS_KEY": "DO-NOT-PRINT-SECRET",
            "AWS_SESSION_TOKEN": "DO-NOT-PRINT-TOKEN",
        }
        result = smoke.run(
            values,
            sender=lambda request: captured.append(request) or smoke.SmokeResponse(200),
            now_epoch=lambda: 1_800_000_000,
        )
        self.assertTrue(result["ok"])
        self.assertIn("environment", result)
        self.assertIn("observedAtEpoch", result)
        self.assertEqual(result["environment"], "test")
        self.assertEqual(result["observedAtEpoch"], 1_800_000_000)
        self.assertEqual(len(captured), 1)
        request = captured[0]
        self.assertEqual(
            request.url,
            "https://abcdefghij.execute-api.us-east-1.amazonaws.com/test/"
            "internal/v1/integrations/connection-resolve",
        )
        self.assertEqual(request.region, "us-east-1")
        rendered = json.dumps({"result": result, "request": request.payload})
        for secret in (
            values["AWS_ACCESS_KEY_ID"],
            values["AWS_SECRET_ACCESS_KEY"],
            values["AWS_SESSION_TOKEN"],
        ):
            self.assertNotIn(secret, rendered)

    def test_environment_comes_only_from_validated_stage_and_clock_runs_once(self):
        smoke = self.smoke()
        clock_calls = []
        values = {
            **environment(),
            "ZLP_INTEGRATIONS_SMOKE_API_URL": (
                "https://abcdefghij.execute-api.us-east-1.amazonaws.com/production"
            ),
            "ENVIRONMENT_NAME": "test",
        }

        def clock():
            clock_calls.append(True)
            return 1_800_000_123

        result = smoke.run(
            values,
            sender=lambda request: smoke.SmokeResponse(200),
            now_epoch=clock,
        )

        self.assertIn("environment", result)
        self.assertIn("observedAtEpoch", result)
        self.assertEqual(result["environment"], "production")
        self.assertEqual(result["observedAtEpoch"], 1_800_000_123)
        self.assertEqual(clock_calls, [True])

        invalid = {
            **values,
            "ZLP_INTEGRATIONS_SMOKE_API_URL": (
                "https://abcdefghij.execute-api.us-east-1.amazonaws.com/preview"
            ),
        }
        invalid_result = smoke.run(
            invalid,
            sender=lambda request: self.fail("transport must not run"),
            now_epoch=lambda: 1_800_000_124,
        )
        self.assertIn("environment", invalid_result)
        self.assertIn("observedAtEpoch", invalid_result)
        self.assertIsNone(invalid_result["environment"])
        self.assertEqual(invalid_result["observedAtEpoch"], 1_800_000_124)

    def test_cli_emits_only_redacted_missing_input_result(self):
        environment_values = dict(os.environ)
        environment_values.pop("ZLP_INTEGRATIONS_SMOKE_API_URL", None)
        environment_values["AWS_SECRET_ACCESS_KEY"] = "DO-NOT-PRINT-SECRET"
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "integration_platform_readiness_smoke.py")],
            cwd=ROOT,
            env=environment_values,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        output = json.loads(result.stdout)
        self.assertEqual(output["attempts"], 0)
        self.assertEqual(output["classification"], "missing_input")
        self.assertFalse(output["ok"])
        self.assertIn("environment", output)
        self.assertIn("observedAtEpoch", output)
        self.assertIsNone(output["environment"])
        self.assertIs(type(output["observedAtEpoch"]), int)
        self.assertEqual(
            set(output),
            {
                "attempts",
                "classification",
                "environment",
                "observedAtEpoch",
                "ok",
            },
        )
        self.assertNotIn("DO-NOT-PRINT-SECRET", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
