import hashlib
import json
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


def resolved(
    scope,
    *,
    tax_mode="stripe-tax",
    approval="approval-1",
    account="acct_synthetic",
    mode=None,
):
    return SimpleNamespace(
        connection=SimpleNamespace(
            scope=scope,
            connection_id="stripe-primary",
            mode=mode or ("test" if scope.environment == "test" else "live"),
            provider_metadata={"accountReference": account},
        ),
        binding=SimpleNamespace(
            mode=mode or ("test" if scope.environment == "test" else "live"),
            provider_metadata={"taxMode": tax_mode, "taxApprovalId": approval}
        ),
    )


def approval_hash(record):
    fields = (
        "environment",
        "tenantId",
        "draftId",
        "domain",
        "connectionId",
        "approvalId",
        "provider",
        "taxMode",
        "accountHash",
        "mode",
        "expectedRevision",
        "status",
        "revision",
    )
    return hashlib.sha256(
        json.dumps(
            {field: record[field] for field in fields},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()


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
            "accountHash": hashlib.sha256(b"acct_synthetic").hexdigest(),
            "mode": "live",
            "expectedRevision": 2,
            "status": "approved",
            "revision": 1,
        }
        self.record["approvalHash"] = approval_hash(self.record)
        self.state = {
            "automaticTax": {"enabled": True},
            "defaultTaxRateIds": [],
            "items": [{"taxRateIds": []}],
        }

    def test_production_requires_exact_server_owned_scoped_approval(self):
        client = Client(self.record)
        verifier = PublishedTaxPolicyVerifier("registry-table", client=client)

        authorization = verifier.authorize(resolved(self.scope), 2)
        self.assertEqual(
            authorization, ("stripe-tax", self.record["approvalHash"])
        )
        self.assertTrue(verifier.validate_state(authorization, self.state))
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
            {**self.record, "draftId": "draft-other"},
            {**self.record, "domain": "other.example.com"},
            {**self.record, "connectionId": "stripe-other"},
            {**self.record, "approvalId": "approval-other"},
            {**self.record, "taxMode": "manual-rate"},
            {**self.record, "accountHash": hashlib.sha256(b"acct_other999").hexdigest()},
            {**self.record, "mode": "test"},
            {**self.record, "expectedRevision": 3},
            {**self.record, "status": "pending"},
            {**self.record, "revision": True},
            {**self.record, "approvalHash": "0" * 64},
            {**self.record, "unexpected": True},
        )
        for record in corruptions:
            with self.subTest(record=record):
                verifier = PublishedTaxPolicyVerifier(
                    "registry-table", client=Client(record)
                )
                self.assertIsNone(verifier.authorize(resolved(self.scope), 2))

        verifier = PublishedTaxPolicyVerifier(
            "registry-table", client=Client(self.record)
        )
        self.assertIsNone(
            verifier.authorize(resolved(self.scope, account="acct_other999"), 2)
        )
        self.assertIsNone(verifier.authorize(resolved(self.scope), 3))

    def test_tax_mode_must_match_current_server_observed_tax_settings(self):
        stripe_tax = PublishedTaxPolicyVerifier(
            "registry-table", client=Client(self.record)
        )
        stripe_authorization = stripe_tax.authorize(resolved(self.scope), 2)
        self.assertFalse(
            stripe_tax.validate_state(
                stripe_authorization,
                {**self.state, "automaticTax": {"enabled": False}},
            )
        )

        manual = {**self.record, "taxMode": "manual-rate"}
        manual["approvalHash"] = approval_hash(manual)
        manual_state = {
            **self.state,
            "automaticTax": {"enabled": False},
            "defaultTaxRateIds": ["txr_synthetic01"],
        }
        verifier = PublishedTaxPolicyVerifier("registry-table", client=Client(manual))
        authorization = verifier.authorize(
            resolved(self.scope, tax_mode="manual-rate"), 2
        )
        self.assertTrue(verifier.validate_state(authorization, manual_state))

    def test_test_environment_does_not_require_a_production_approval_record(self):
        test_scope = IntegrationScope(
            "test", "tenant-example", "draft-example", "example.com"
        )
        client = Client()
        verifier = PublishedTaxPolicyVerifier("registry-table", client=client)
        authorization = verifier.authorize(resolved(test_scope), 2)
        self.assertEqual(authorization, ("stripe-tax", None))
        self.assertTrue(verifier.validate_state(authorization, self.state))
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
