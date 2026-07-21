import unittest
from types import SimpleNamespace

from src.domain.integrations import IntegrationScope
from src.registry import _deserialize, _serialize
from src.runtime import PublishedTaxPolicyVerifier


class Client:
    def __init__(self, item=None):
        self.item = item
        self.calls = []

    def get_item(self, **kwargs):
        self.calls.append(kwargs)
        return {} if self.item is None else {"Item": _serialize(self.item)}


def resolved(scope, *, tax_mode="stripe-tax", approval="approval-1"):
    return SimpleNamespace(
        connection=SimpleNamespace(scope=scope, connection_id="stripe-primary"),
        binding=SimpleNamespace(
            provider_metadata={"taxMode": tax_mode, "taxApprovalId": approval}
        ),
    )


class PublishedTaxPolicyVerifierTests(unittest.TestCase):
    def setUp(self):
        self.scope = IntegrationScope(
            "production", "tenant-example", "draft-example", "example.com"
        )
        self.record = {
            "pk": self.scope.partition_key,
            "sk": "TAX_APPROVAL#stripe-primary#approval-1",
            "itemType": "StripeTaxApproval",
            **self.scope.fields(),
            "connectionId": "stripe-primary",
            "approvalId": "approval-1",
            "provider": "stripe",
            "taxMode": "stripe-tax",
            "status": "approved",
            "revision": 1,
        }
        self.state = {
            "automaticTax": {"enabled": True},
            "defaultTaxRateIds": [],
            "items": [{"taxRateIds": []}],
        }

    def test_production_requires_exact_server_owned_scoped_approval(self):
        client = Client(self.record)
        verifier = PublishedTaxPolicyVerifier("registry-table", client=client)

        self.assertTrue(verifier(resolved(self.scope), self.state, {}))
        call = client.calls[0]
        self.assertEqual(call["TableName"], "registry-table")
        self.assertTrue(call["ConsistentRead"])
        self.assertEqual(
            _deserialize(call["Key"]),
            {
                "pk": self.scope.partition_key,
                "sk": "TAX_APPROVAL#stripe-primary#approval-1",
            },
        )

    def test_draft_hint_alone_or_foreign_corrupt_approval_never_authorizes(self):
        corruptions = (
            None,
            {**self.record, "tenantId": "tenant-other"},
            {**self.record, "connectionId": "stripe-other"},
            {**self.record, "approvalId": "approval-other"},
            {**self.record, "taxMode": "manual-rate"},
            {**self.record, "status": "pending"},
            {**self.record, "revision": True},
            {**self.record, "unexpected": True},
        )
        for record in corruptions:
            with self.subTest(record=record):
                verifier = PublishedTaxPolicyVerifier(
                    "registry-table", client=Client(record)
                )
                self.assertFalse(verifier(resolved(self.scope), self.state, {}))

    def test_tax_mode_must_match_current_server_observed_tax_settings(self):
        stripe_tax = PublishedTaxPolicyVerifier(
            "registry-table", client=Client(self.record)
        )
        self.assertFalse(
            stripe_tax(
                resolved(self.scope),
                {**self.state, "automaticTax": {"enabled": False}},
                {},
            )
        )

        manual = {**self.record, "taxMode": "manual-rate"}
        manual_state = {
            **self.state,
            "automaticTax": {"enabled": False},
            "defaultTaxRateIds": ["txr_synthetic01"],
        }
        self.assertTrue(
            PublishedTaxPolicyVerifier("registry-table", client=Client(manual))(
                resolved(self.scope, tax_mode="manual-rate"), manual_state, {}
            )
        )

    def test_test_environment_does_not_require_a_production_approval_record(self):
        test_scope = IntegrationScope(
            "test", "tenant-example", "draft-example", "example.com"
        )
        client = Client()
        verifier = PublishedTaxPolicyVerifier("registry-table", client=client)
        self.assertTrue(verifier(resolved(test_scope), self.state, {}))
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
