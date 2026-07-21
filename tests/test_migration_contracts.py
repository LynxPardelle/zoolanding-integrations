import hashlib
import json
import unittest

from src.contracts.internal import (
    ContractError,
    derive_command_idempotency_key,
    validate_command,
    validate_service_result,
)
from src.domain.integrations import IntegrationScope


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()


def offer(offer_id, amount):
    snapshot = {
        "schemaVersion": 1,
        "amountMinor": amount,
        "billingScheme": "per_unit",
        "currency": "MXN",
        "saleType": "recurring",
        "recurrence": {"interval": "month", "intervalCount": 1, "usageType": "licensed"},
        "taxBehavior": "exclusive",
    }
    return {
        "offerVersionId": offer_id,
        "revision": 2,
        "schemaVersion": 1,
        "snapshot": snapshot,
        "contentHash": digest({"schemaVersion": 1, "snapshot": snapshot}),
    }


def envelope(kind, input_value):
    scope = IntegrationScope("test", "tenant-example", "draft-example", "example.com")
    if kind == "migration-preview":
        identity = (
            "migration-preview",
            input_value["commercialRequestId"],
            input_value["targetOffer"]["revision"],
        )
    elif kind == "migration-execute":
        identity = (
            "migration-execute",
            input_value["jobId"],
            input_value["dryRunRevision"],
        )
    elif kind == "migration-control":
        identity = (
            input_value["action"],
            input_value["jobId"],
            input_value["expectedRevision"],
        )
    else:
        identity = None
    idempotency_key = "migration-status-read"
    if identity is not None:
        idempotency_key = derive_command_idempotency_key(
            scope,
            "stripe-primary",
            *identity,
            digest(input_value),
        )
    return {
        "version": 1,
        "scope": {
            "environment": "test",
            "tenantId": "tenant-example",
            "draftId": "draft-example",
            "domain": "example.com",
        },
        "connectionId": "stripe-primary",
        "commandId": "command-1",
        "idempotencyKey": idempotency_key,
        "input": input_value,
    }


class MigrationContractTests(unittest.TestCase):
    def test_mutating_migration_idempotency_is_scope_and_content_derived(self):
        value = {
            "commercialRequestId": "request-1",
            "sourceOffer": offer("offer-old", 90000),
            "targetOffer": offer("offer-new", 100000),
            "requestedPolicy": {"mode": "next_renewal"},
            "candidateScope": {"kind": "all_matching_source_price"},
            "canarySize": 5,
            "accountConcurrency": 2,
        }
        request = envelope("migration-preview", value)
        validate_command("migration-preview", request)

        request["idempotencyKey"] = "caller-selected-retry-key"
        with self.assertRaises(ContractError):
            validate_command("migration-preview", request)

    def test_preview_contract_is_closed_and_contains_exact_offer_snapshots(self):
        value = {
            "commercialRequestId": "request-1",
            "sourceOffer": offer("offer-old", 90000),
            "targetOffer": offer("offer-new", 100000),
            "requestedPolicy": {"mode": "next_renewal"},
            "candidateScope": {"kind": "all_matching_source_price"},
            "canarySize": 5,
            "accountConcurrency": 2,
        }
        command = validate_command("migration-preview", envelope("migration-preview", value))
        self.assertEqual(command.input, value)

        for mutation in (
            lambda item: item.update({"providerPriceId": "price_forbidden"}),
            lambda item: item["sourceOffer"].update({"contentHash": "0" * 64}),
            lambda item: item.update({"canarySize": 26}),
            lambda item: item.update({"accountConcurrency": 6}),
        ):
            changed = json.loads(json.dumps(value))
            mutation(changed)
            with self.assertRaises(ContractError):
                validate_command("migration-preview", envelope("migration-preview", changed))

    def test_execute_control_and_status_contracts_are_revision_bound(self):
        execute = validate_command(
            "migration-execute",
            envelope("migration-execute", {
                "commercialRequestId": "request-1",
                "jobId": "migration-job-1",
                "dryRunRevision": 3,
                "dryRunHash": "a" * 64,
                "confirmation": True,
            }),
        )
        self.assertTrue(execute.input["confirmation"])
        validate_command(
            "migration-control",
            envelope("migration-control", {
                "commercialRequestId": "request-1",
                "jobId": "migration-job-1",
                "expectedRevision": 4,
                "action": "pause",
            }),
        )
        validate_command(
            "migration-status",
            envelope("migration-status", {
                "commercialRequestId": "request-1",
                "jobId": "migration-job-1",
                "limit": 25,
            }),
        )
        with self.assertRaises(ContractError):
            validate_command(
                "migration-control",
                envelope("migration-control", {
                    "commercialRequestId": "request-1",
                    "jobId": "migration-job-1",
                    "expectedRevision": 4,
                    "action": "cancel",
                    "itemId": "migration-item-1",
                }),
            )

    def test_results_return_only_closed_job_and_protected_status_shapes(self):
        command = validate_command(
            "migration-execute",
            envelope("migration-execute", {
                "commercialRequestId": "request-1",
                "jobId": "migration-job-1",
                "dryRunRevision": 3,
                "dryRunHash": "a" * 64,
                "confirmation": True,
            }),
        )
        self.assertEqual(
            validate_service_result(
                {"commandId": "command-1", "status": "accepted", "jobId": "migration-job-1", "revision": 4},
                command,
            )["revision"],
            4,
        )

        status_command = validate_command(
            "migration-status",
            envelope("migration-status", {
                "commercialRequestId": "request-1",
                "jobId": "migration-job-1",
                "limit": 25,
            }),
        )
        status = {
            "commercialRequestId": "request-1",
            "jobId": "migration-job-1",
            "connectionId": "stripe-primary",
            "revision": 4,
            "state": "awaiting_approval",
            "dryRunRevision": 3,
            "dryRunHash": "a" * 64,
            "expiresAt": 1_900_000_000,
            "counts": {"total": 2, "pending": 2, "applied": 0, "needsReview": 0, "failed": 0},
            "items": [],
            "nextCursor": None,
        }
        self.assertEqual(validate_service_result(status, status_command), status)
        with self.assertRaises(ContractError):
            validate_service_result({**status, "providerPayload": {}}, status_command)
        with self.assertRaises(ContractError):
            validate_service_result(
                {**status, "counts": {**status["counts"], "total": 3}},
                status_command,
            )
        applied_event_item = {
            "itemId": "migration-item-" + "1" * 40,
            "state": "pending_update_applied",
            "reasonCode": None,
            "attempts": 1,
        }
        self.assertEqual(
            validate_service_result(
                {**status, "items": [applied_event_item]}, status_command
            )["items"][0],
            applied_event_item,
        )
        expired_event_item = {
            **applied_event_item,
            "state": "pending_update_expired",
            "reasonCode": "payment-failed",
        }
        self.assertEqual(
            validate_service_result(
                {**status, "items": [expired_event_item]}, status_command
            )["items"][0],
            expired_event_item,
        )
        with self.assertRaises(ContractError):
            validate_service_result(
                {
                    **status,
                    "items": [
                        {
                            **applied_event_item,
                            "state": "needs_review",
                        }
                    ],
                },
                status_command,
            )
        with self.assertRaises(ContractError):
            validate_service_result(
                {
                    **status,
                    "items": [{**applied_event_item, "itemId": "migration-item-1"}],
                },
                status_command,
            )
        with self.assertRaises(ContractError):
            validate_service_result(
                {
                    **status,
                    "items": [{**expired_event_item, "reasonCode": "arbitrary"}],
                },
                status_command,
            )


if __name__ == "__main__":
    unittest.main()
