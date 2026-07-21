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
            receipt_id="evt-1",
            connection_id="stripe-primary",
            provider="stripe",
            mode="test",
            event_type="checkout.session.completed",
            account_hash="c" * 64,
            payload_hash="a" * 64,
            status="received",
            revision=1,
            decision_code="queued",
            event_created_at=900,
            received_at=1_000,
            expires_at=1_000 + 90 * 24 * 60 * 60,
        ).to_record()
        replay = operations.GlobalWebhookReplaySentinel(
            environment="test",
            event_id="evt-1",
            account_hash="c" * 64,
            mode="test",
            payload_hash="a" * 64,
            receipt_pk=scope().partition_key,
            receipt_sk="WEBHOOK_RECEIPT#evt-1",
            created_at=1_000,
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
        self.assertEqual(replay["itemType"], "GlobalWebhookReplaySentinel")
        self.assertEqual(idem["itemType"], "IdempotencyReceipt")
        self.assertEqual(receipt["pk"], scope().partition_key)
        self.assertNotIn("acct_", repr(receipt))
        self.assertIn("expiresAt", receipt)
        self.assertIn("expiresAt", idem)

    def test_ingress_and_outgoing_outboxes_use_only_closed_status_and_event_contracts(
        self,
    ):
        operations = operations_module(self)
        ingress = operations.WebhookIngressOutbox(
            scope=scope(),
            outbox_id="ingress-1",
            receipt_id="evt-1",
            processing_status="pending",
            revision=1,
            attempt_count=0,
            created_at=1_000,
            expires_at=1_000 + 90 * 24 * 60 * 60,
        ).to_record()
        envelope = operations.IntegrationEventEnvelope(
            scope=scope(),
            event_id="integration-event-1",
            event_type="commerce.payment.succeeded.v1",
            occurred_at=1_000,
            data={
                "reservationId": "reservation-1",
                "orderId": "order-1",
                "paymentAttemptId": "attempt-1",
            },
        ).to_dict()
        outgoing = operations.IntegrationEventOutbox(
            scope=scope(),
            outbox_id="event-1",
            envelope=envelope,
            payload_hash=operations.canonical_hash(envelope),
            delivery_status="pending",
            revision=1,
            created_at=1_000,
            expires_at=1_000 + 90 * 24 * 60 * 60,
        ).to_record()
        self.assertEqual(ingress["itemType"], "WebhookIngressOutbox")
        self.assertEqual(outgoing["itemType"], "IntegrationEventOutbox")
        with self.assertRaises(ValueError):
            operations.IntegrationEventOutbox(
                scope=scope(),
                outbox_id="event-2",
                envelope={**envelope, "eventType": "payment.succeeded"},
                payload_hash="d" * 64,
                delivery_status="pending",
                revision=1,
                created_at=1_000,
                expires_at=2_000,
            )

    def test_commerce_envelopes_are_exact_and_reject_pii_or_paused_status(self):
        operations = operations_module(self)
        subscription = {
            "subscriptionId": "subscription-1",
            "offerVersionId": "offer-1",
            "status": "active",
            "currentPeriodEnd": 2_000,
            "sourceRevision": 1,
        }
        envelope = operations.IntegrationEventEnvelope(
            scope=scope(),
            event_id="integration-event-1",
            event_type="commerce.subscription.updated.v1",
            occurred_at=1_000,
            data=subscription,
        ).to_dict()
        self.assertEqual(
            set(envelope),
            {
                "schemaVersion",
                "eventId",
                "eventType",
                "occurredAt",
                "environment",
                "tenantId",
                "draftId",
                "domain",
                "data",
            },
        )
        for changed in (
            {**subscription, "status": "paused"},
            {**subscription, "email": "private@example.com"},
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                operations.IntegrationEventEnvelope(
                    scope=scope(),
                    event_id="integration-event-2",
                    event_type="commerce.subscription.updated.v1",
                    occurred_at=1_000,
                    data=changed,
                )


if __name__ == "__main__":
    unittest.main()
