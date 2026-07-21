import importlib
import importlib.util
import unittest

from tests.test_registry import binding, connection, scope


def admin_module(testcase):
    testcase.assertIsNotNone(
        importlib.util.find_spec("src.connection_admin"),
        "connection admin is not implemented",
    )
    return importlib.import_module("src.connection_admin")


class Secrets:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def describe_secret(self, **kwargs):
        self.calls.append(("describe_secret", kwargs))
        return self.response

    def get_secret_value(self, **kwargs):
        self.calls.append(("get_secret_value", kwargs))
        raise AssertionError("secret values must never be read")


class Registry:
    def __init__(self):
        self.calls = []

    def register(self, candidate, candidate_binding, idempotency_key):
        self.calls.append((candidate, candidate_binding, idempotency_key))


def secret_metadata(candidate):
    if candidate.provider == "stripe":
        return {
            "Name": candidate.credential_reference,
            "Tags": [
                {"Key": "zoolanding:environment", "Value": candidate.scope.environment},
                {
                    "Key": "zoolanding:secret-purpose",
                    "Value": "stripe-connect-platform",
                },
                {"Key": "zoolanding:enabled", "Value": "true"},
            ],
        }
    return {
        "Name": candidate.credential_reference,
        "Tags": [
            {"Key": "zoolanding:environment", "Value": candidate.scope.environment},
            {"Key": "zoolanding:tenant-id", "Value": candidate.scope.tenant_id},
            {"Key": "zoolanding:draft-id", "Value": candidate.scope.draft_id},
            {"Key": "zoolanding:secret-purpose", "Value": "stripe"},
            {"Key": "zoolanding:connection-id", "Value": candidate.connection_id},
            {"Key": "zoolanding:enabled", "Value": "true"},
        ],
    }


class ConnectionAdminTests(unittest.TestCase):
    def test_registers_only_after_describe_secret_metadata_and_returns_sanitized_state(
        self,
    ):
        admin_api = admin_module(self)
        candidate = connection()
        secrets = Secrets(secret_metadata(candidate))
        registry = Registry()

        result = admin_api.ConnectionAdmin(registry, secrets).register(
            candidate,
            binding(),
            credential_reference=candidate.credential_reference,
            idempotency_key="request-1",
        )

        self.assertEqual(
            result,
            {
                "connectionId": "stripe-primary",
                "status": "active",
                "mode": "test",
                "revision": 1,
            },
        )
        self.assertEqual(
            secrets.calls,
            [("describe_secret", {"SecretId": candidate.credential_reference})],
        )
        self.assertEqual(len(registry.calls), 1)
        self.assertNotIn("acct_synthetic", str(result))
        self.assertNotIn("credential", str(result).lower())

    def test_wrong_reference_deleted_secret_or_wrong_scope_tags_fail_closed(self):
        admin_api = admin_module(self)
        candidate = connection()
        cases = []
        wrong_tags = secret_metadata(candidate)
        wrong_tags["Tags"] = [
            (
                {**tag, "Value": "production"}
                if tag["Key"] == "zoolanding:environment"
                else tag
            )
            for tag in wrong_tags["Tags"]
        ]
        deleted = {**secret_metadata(candidate), "DeletedDate": "synthetic-date"}
        cases.extend(
            (
                (
                    "/zoolanding/test/tenant-example/draft-other/stripe",
                    secret_metadata(candidate),
                ),
                (candidate.credential_reference, wrong_tags),
                (candidate.credential_reference, deleted),
            )
        )
        for reference, metadata in cases:
            with (
                self.subTest(reference=reference),
                self.assertRaises(admin_api.ConnectionAdminError),
            ):
                admin_api.ConnectionAdmin(Registry(), Secrets(metadata)).register(
                    candidate,
                    binding(),
                    credential_reference=reference,
                    idempotency_key="request-1",
                )

    def test_smtp_registration_keeps_endpoint_and_domain_code_owned(self):
        admin_api = admin_module(self)
        smtp_scope = scope("draft-email", domain="zoolandingpage.com.mx")
        from src.domain.integrations import IntegrationBinding, IntegrationConnection

        candidate = IntegrationConnection(
            scope=smtp_scope,
            connection_id="billing-mailbox",
            provider="email.smtp",
            adapter_version="v1",
            status="pending",
            mode="test",
            capabilities=frozenset({"send"}),
            provider_metadata={
                "adapterId": "smtp2go-smtp-v1",
                "host": "mail.smtp2go.com",
                "port": 465,
                "tlsMode": "implicit",
                "canonicalSendingDomain": "zoolandingpage.com.mx",
            },
        )
        smtp_binding = IntegrationBinding(
            scope=smtp_scope,
            binding_id="billing-mailbox",
            provider="email.smtp",
            adapter_version="v1",
            connection_id="billing-mailbox",
            status="active",
            mode="test",
            capabilities=frozenset({"send"}),
            provider_metadata={},
        )
        metadata = secret_metadata(candidate)
        for tag in metadata["Tags"]:
            if tag["Key"] == "zoolanding:secret-purpose":
                tag["Value"] = "smtp"
        result = admin_api.ConnectionAdmin(Registry(), Secrets(metadata)).register(
            candidate,
            smtp_binding,
            credential_reference=candidate.credential_reference,
            idempotency_key="request-2",
        )
        self.assertEqual(result["connectionId"], "billing-mailbox")


if __name__ == "__main__":
    unittest.main()
