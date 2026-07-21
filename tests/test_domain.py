import importlib
import unittest


def domain_module():
    try:
        return importlib.import_module("src.domain.integrations")
    except ModuleNotFoundError as exc:
        raise AssertionError("integration domain contract is not implemented") from exc


class IntegrationDomainTests(unittest.TestCase):
    def setUp(self):
        self.domain = domain_module()
        self.scope = self.domain.IntegrationScope(
            "test", "tenant-example", "draft-example", "example.com"
        )

    def stripe_binding(self, **changes):
        value = {
            "id": "stripe-primary",
            "provider": "stripe",
            "adapterVersion": "v1",
            "connectionId": "stripe-primary",
            "status": "active",
            "mode": "test",
            "capabilities": [
                "connect-onboarding",
                "checkout",
                "one-time-payments",
                "subscriptions",
                "prices",
                "coupons",
                "customer-portal",
            ],
            "stripe": {
                "accountModel": "merchant",
                "chargeType": "direct",
                "feePayer": "connected-account",
                "taxMode": "unconfigured",
                "platformFeeMode": "disabled",
                "webhookIngress": "direct-integrations-api",
            },
        }
        value.update(changes)
        return value

    def test_scope_prefixes_every_partition_and_rejects_cross_environment_mode(self):
        self.assertEqual(
            self.scope.partition_key,
            "ENV#test#TENANT#tenant-example#DRAFT#draft-example",
        )
        with self.assertRaises(ValueError):
            self.domain.IntegrationBinding.from_mapping(
                self.scope, self.stripe_binding(mode="live")
            )

    def test_binding_is_immutable_and_closed_to_provider_contract(self):
        binding = self.domain.IntegrationBinding.from_mapping(
            self.scope, self.stripe_binding()
        )
        self.assertEqual(binding.provider, "stripe")
        self.assertEqual(
            binding.capabilities, frozenset(self.stripe_binding()["capabilities"])
        )
        self.assertEqual(binding.provider_metadata["chargeType"], "direct")
        with self.assertRaises(TypeError):
            binding.provider_metadata["chargeType"] = "destination"
        with self.assertRaises(ValueError):
            self.domain.IntegrationBinding.from_mapping(
                self.scope,
                self.stripe_binding(capabilities=["checkout", "arbitrary-power"]),
            )
        with self.assertRaises(ValueError):
            self.domain.IntegrationBinding.from_mapping(
                self.scope,
                self.stripe_binding(capabilities=["checkout", "checkout"]),
            )

    def test_unknown_provider_fails_with_a_sanitized_validation_error(self):
        unknown = self.stripe_binding(provider="unknown")
        unknown.pop("stripe")
        with self.assertRaisesRegex(ValueError, "provider is invalid"):
            self.domain.IntegrationBinding.from_mapping(self.scope, unknown)

    def test_secret_looking_keys_and_values_are_rejected_recursively(self):
        unsafe_key = self.stripe_binding()
        unsafe_key["stripe"] = {**unsafe_key["stripe"], "apiKey": "synthetic"}
        with self.assertRaises(ValueError):
            self.domain.IntegrationBinding.from_mapping(self.scope, unsafe_key)

        unsafe_value = self.stripe_binding()
        unsafe_value["stripe"] = {
            **unsafe_value["stripe"],
            "taxApprovalId": "_".join(("sk", "live", "synthetic")),
        }
        with self.assertRaises(ValueError):
            self.domain.IntegrationBinding.from_mapping(self.scope, unsafe_value)

        embedded = self.stripe_binding()
        embedded["stripe"] = {
            **embedded["stripe"],
            "taxApprovalId": "approval." + "_".join(("sk", "live", "synthetic")),
        }
        with self.assertRaises(ValueError):
            self.domain.IntegrationBinding.from_mapping(self.scope, embedded)

    def test_connection_derives_stripe_secret_reference_and_safe_record(self):
        connection = self.domain.IntegrationConnection(
            scope=self.scope,
            connection_id="stripe-primary",
            provider="stripe",
            adapter_version="v1",
            status="pending",
            mode="test",
            capabilities=frozenset({"connect-onboarding", "checkout"}),
            provider_metadata={"accountReference": "acct_synthetic"},
        )
        self.assertEqual(
            connection.credential_reference,
            "/zoolanding/test/integrations/tenant-example/draft-example/stripe/stripe-primary",
        )
        record = connection.to_record()
        self.assertEqual(record["pk"], self.scope.partition_key)
        self.assertEqual(record["sk"], "CONNECTION#stripe-primary")
        self.assertNotIn("secret", str(record).lower())

    def test_active_production_stripe_connection_requires_an_account_mapping(self):
        scope = self.domain.IntegrationScope(
            "production", "tenant-example", "draft-example", "example.com"
        )
        with self.assertRaises(ValueError):
            self.domain.IntegrationConnection(
                scope=scope,
                connection_id="stripe-primary",
                provider="stripe",
                adapter_version="v1",
                status="active",
                mode="live",
                capabilities=frozenset({"checkout"}),
                provider_metadata={},
            )

    def test_production_assignment_rejects_cross_draft_account_reuse(self):
        first = self.domain.IntegrationConnection(
            scope=self.domain.IntegrationScope(
                "production", "tenant-example", "draft-one", "one.example.com"
            ),
            connection_id="stripe-primary",
            provider="stripe",
            adapter_version="v1",
            status="active",
            mode="live",
            capabilities=frozenset({"checkout"}),
            provider_metadata={"accountReference": "acct_synthetic"},
        )
        reused = self.domain.IntegrationConnection(
            scope=self.domain.IntegrationScope(
                "production", "tenant-example", "draft-two", "two.example.com"
            ),
            connection_id="stripe-primary",
            provider="stripe",
            adapter_version="v1",
            status="active",
            mode="live",
            capabilities=frozenset({"checkout"}),
            provider_metadata={"accountReference": "acct_synthetic"},
        )
        with self.assertRaises(ValueError):
            self.domain.assert_isolated_connection_assignment(reused, (first,))

    def test_test_assignments_may_share_the_code_owned_sender_domain(self):
        def smtp_connection(draft_id):
            return self.domain.IntegrationConnection(
                scope=self.domain.IntegrationScope(
                    "test", "tenant-example", draft_id, "zoolandingpage.com.mx"
                ),
                connection_id="billing-mailbox",
                provider="email.smtp",
                adapter_version="v1",
                status="active",
                mode="test",
                capabilities=frozenset({"send"}),
                provider_metadata={
                    "adapterId": "smtp2go-smtp-v1",
                    "host": "mail.smtp2go.com",
                    "port": 465,
                    "canonicalSendingDomain": "zoolandingpage.com.mx",
                    "accountOwnershipState": "audited",
                },
            )

        first = smtp_connection("draft-one")
        second = smtp_connection("draft-two")
        self.domain.assert_isolated_connection_assignment(second, (first,))

    def test_email_connection_uses_code_owned_smtp2go_metadata(self):
        scope = self.domain.IntegrationScope(
            "test",
            "tenant-example",
            "draft-email",
            "zoolandingpage.com.mx",
        )
        connection = self.domain.IntegrationConnection(
            scope=scope,
            connection_id="billing-mailbox",
            provider="email.smtp",
            adapter_version="v1",
            status="active",
            mode="test",
            capabilities=frozenset({"send"}),
            provider_metadata={
                "adapterId": "smtp2go-smtp-v1",
                "host": "mail.smtp2go.com",
                "port": 465,
                "canonicalSendingDomain": "zoolandingpage.com.mx",
                "accountOwnershipState": "audited",
            },
        )
        self.assertEqual(
            connection.credential_reference,
            "/zoolanding/test/tenant-example/draft-email/notifications/smtp/billing-mailbox",
        )
        self.assertEqual(connection.provider_metadata["adapterId"], "smtp2go-smtp-v1")

    def test_email_metadata_cannot_override_host_domain_or_ownership_policy(self):
        scope = self.domain.IntegrationScope(
            "production", "tenant-example", "draft-email", "example.com"
        )
        base = {
            "adapterId": "smtp2go-smtp-v1",
            "host": "mail.smtp2go.com",
            "port": 465,
            "canonicalSendingDomain": "example.com",
            "accountOwnershipState": "audited",
        }
        for change in (
            {"host": "smtp.example.net"},
            {"canonicalSendingDomain": "other.example.com"},
            {"accountOwnershipState": "unverified"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.domain.IntegrationConnection(
                    scope=scope,
                    connection_id="billing-mailbox",
                    provider="email.smtp",
                    adapter_version="v1",
                    status="active",
                    mode="live",
                    capabilities=frozenset({"send"}),
                    provider_metadata={**base, **change},
                )

    def test_technical_receipts_expire_after_exactly_ninety_days(self):
        self.assertEqual(
            self.domain.technical_expiry(1_700_000_000),
            1_700_000_000 + 90 * 24 * 60 * 60,
        )


if __name__ == "__main__":
    unittest.main()
