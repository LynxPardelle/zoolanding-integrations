import importlib
import importlib.util
import unittest

from tests.test_registry import scope


def operations_module(testcase):
    testcase.assertIsNotNone(
        importlib.util.find_spec("src.domain.operations"),
        "closed operational row models are not implemented",
    )
    return importlib.import_module("src.domain.operations")


class OperationsDomainTests(unittest.TestCase):
    def test_receipt_and_idempotency_rows_require_technical_expiry(self):
        operations = operations_module(self)
        receipt = operations.WebhookReceipt(
            scope=scope(),
            receipt_id="receipt-1",
            provider="stripe",
            mode="test",
            event_type="checkout.session.completed",
            payload_hash="a" * 64,
            status="received",
            received_at=1_000,
            expires_at=1_000 + 90 * 24 * 60 * 60,
        ).to_record()
        idem = operations.IdempotencyReceipt(
            scope=scope(),
            receipt_id="idem-1",
            operation="stripe.checkout",
            request_hash="b" * 64,
            status="pending",
            created_at=1_000,
            expires_at=1_000 + 90 * 24 * 60 * 60,
        ).to_record()
        self.assertEqual(receipt["itemType"], "WebhookReceipt")
        self.assertEqual(idem["itemType"], "IdempotencyReceipt")
        self.assertEqual(receipt["pk"], scope().partition_key)
        self.assertIn("expiresAt", receipt)
        self.assertIn("expiresAt", idem)

    def test_ingress_and_outgoing_outboxes_use_only_closed_status_and_event_contracts(
        self,
    ):
        operations = operations_module(self)
        ingress = operations.WebhookIngressOutbox(
            scope=scope(),
            outbox_id="ingress-1",
            receipt_id="receipt-1",
            processing_status="pending",
            created_at=1_000,
            expires_at=1_000 + 90 * 24 * 60 * 60,
        ).to_record()
        outgoing = operations.IntegrationEventOutbox(
            scope=scope(),
            outbox_id="event-1",
            event_id="event-1",
            event_type="commerce.payment.succeeded.v1",
            dedupe_key="dedupe-1",
            delivery_status="pending",
            created_at=1_000,
            expires_at=1_000 + 90 * 24 * 60 * 60,
        ).to_record()
        self.assertEqual(ingress["itemType"], "WebhookIngressOutbox")
        self.assertEqual(outgoing["itemType"], "IntegrationEventOutbox")
        with self.assertRaises(ValueError):
            operations.IntegrationEventOutbox(
                scope=scope(),
                outbox_id="event-2",
                event_id="event-2",
                event_type="payment.succeeded",
                dedupe_key="dedupe-2",
                delivery_status="pending",
                created_at=1_000,
                expires_at=2_000,
            )


if __name__ == "__main__":
    unittest.main()
