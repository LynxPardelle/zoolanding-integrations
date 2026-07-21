import unittest

from src.registry import _deserialize, _serialize
from src.domain.operations import IntegrationEventEnvelope, canonical_hash
from src.stripe_store import (
    DynamoStripeWebhookStore,
    WebhookReplayConflict,
)
from tests.test_registry import scope


NOW = 1_800_000_000
TTL = NOW + 90 * 24 * 60 * 60


class MemoryDynamoClient:
    def __init__(self):
        self.items = {}
        self.transactions = []

    def transact_write_items(self, **kwargs):
        self.transactions.append(kwargs)
        if any("Update" in operation for operation in kwargs["TransactItems"]):
            return
        puts = [operation["Put"] for operation in kwargs["TransactItems"]]
        keys = []
        for put in puts:
            item = _deserialize(put["Item"])
            key = (item["pk"], item["sk"])
            if key in self.items:
                raise RuntimeError("conditional conflict")
            keys.append((key, item))
        for key, item in keys:
            self.items[key] = item

    def get_item(self, **kwargs):
        key = _deserialize(kwargs["Key"])
        item = self.items.get((key["pk"], key["sk"]))
        return {} if item is None else {"Item": _serialize(item)}


class StripeWebhookStoreTests(unittest.TestCase):
    def accept(self, store, **changed):
        values = {
            "scope": scope(),
            "connection_id": "stripe-primary",
            "event_id": "evt-1",
            "event_type": "checkout.session.completed",
            "account_hash": "a" * 64,
            "mode": "test",
            "payload_hash": "b" * 64,
            "event_created_at": NOW - 5,
            "received_at": NOW,
            "expires_at": TTL,
        }
        values.update(changed)
        return store.accept_supported(**values)

    def test_first_event_atomically_writes_only_closed_metadata(self):
        client = MemoryDynamoClient()
        result = self.accept(DynamoStripeWebhookStore("receipts", client=client))

        self.assertEqual(result, {"status": "queued", "duplicate": False})
        transaction = client.transactions[0]["TransactItems"]
        self.assertEqual(len(transaction), 3)
        records = [_deserialize(operation["Put"]["Item"]) for operation in transaction]
        self.assertEqual(
            {record["itemType"] for record in records},
            {
                "GlobalWebhookReplaySentinel",
                "WebhookReceipt",
                "WebhookIngressOutbox",
            },
        )
        persisted = repr(records)
        self.assertNotIn("acct_", persisted)
        self.assertNotIn("data.object", persisted)
        self.assertTrue(
            all(
                "attribute_not_exists(pk)" in operation["Put"]["ConditionExpression"]
                for operation in transaction
            )
        )

    def test_exact_duplicate_is_acknowledged_but_changed_replay_is_rejected(self):
        client = MemoryDynamoClient()
        store = DynamoStripeWebhookStore("receipts", client=client)
        self.accept(store)

        self.assertEqual(self.accept(store), {"status": "queued", "duplicate": True})
        with self.assertRaises(WebhookReplayConflict):
            self.accept(store, payload_hash="c" * 64)

    def test_exact_duplicate_reactivates_incomplete_work_without_new_payload_storage(
        self,
    ):
        client = MemoryDynamoClient()
        store = DynamoStripeWebhookStore("receipts", client=client)
        self.accept(store)
        receipt_key = (scope().partition_key, "WEBHOOK_RECEIPT#evt-1")
        ingress_key = (scope().partition_key, "WEBHOOK_INGRESS_OUTBOX#evt-1")
        client.items[receipt_key] = {
            **client.items[receipt_key],
            "status": "failed",
            "decisionCode": "retryable",
            "revision": 2,
        }
        client.items[ingress_key] = {
            **client.items[ingress_key],
            "processingStatus": "failed",
            "processingRevision": 2,
            "attemptCount": 1,
        }

        result = self.accept(store)

        self.assertEqual(result, {"status": "queued", "duplicate": True})
        reactivation = client.transactions[-1]["TransactItems"]
        self.assertEqual(len(reactivation), 2)
        self.assertTrue(all("Update" in operation for operation in reactivation))
        self.assertNotIn("acct_", repr(reactivation))

    def test_worker_claim_binds_stream_sequence_and_completion_writes_closed_outbox(
        self,
    ):
        class Client:
            def __init__(self):
                self.transactions = []
                self.items = {}

            def transact_write_items(self, **kwargs):
                self.transactions.append(kwargs)

            def get_item(self, **kwargs):
                key = _deserialize(kwargs["Key"])
                value = self.items.get((key["pk"], key["sk"]))
                return {} if value is None else {"Item": _serialize(value)}

        client = Client()
        store = DynamoStripeWebhookStore("receipts", client=client)
        claimed = store.claim_ingress(
            scope=scope(),
            outbox_id="evt-1",
            receipt_id="evt-1",
            expected_revision=1,
            sequence="101",
        )
        self.assertEqual(claimed, {"processingRevision": 2})
        claim_text = repr(client.transactions[0]["TransactItems"])
        self.assertIn("processingSequence", claim_text)
        self.assertIn("101", claim_text)
        self.assertNotIn("eventID", claim_text)

        receipt = {
            "pk": scope().partition_key,
            "sk": "WEBHOOK_RECEIPT#evt-1",
            "itemType": "WebhookReceipt",
            **scope().fields(),
            "receiptId": "evt-1",
            "connectionId": "stripe-primary",
            "provider": "stripe",
            "mode": "test",
            "eventType": "checkout.session.completed",
            "accountHash": "a" * 64,
            "payloadHash": "b" * 64,
            "status": "processing",
            "revision": 2,
            "decisionCode": "processing",
            "eventCreatedAt": NOW - 5,
            "receivedAt": NOW,
            "expiresAt": TTL,
        }
        client.items[(receipt["pk"], receipt["sk"])] = receipt
        envelope = IntegrationEventEnvelope(
            scope=scope(),
            event_id="integration-event-1",
            event_type="commerce.payment.succeeded.v1",
            occurred_at=NOW - 5,
            data={
                "reservationId": "reservation-1",
                "orderId": "order-1",
                "paymentAttemptId": "attempt-1",
            },
        ).to_dict()

        store.complete_ingress(
            scope=scope(),
            outbox_id="evt-1",
            receipt_id="evt-1",
            claimed_revision=2,
            sequence="101",
            decision_code="processed",
            envelopes=[envelope],
            projection=None,
        )

        completion = client.transactions[1]["TransactItems"]
        self.assertEqual(len(completion), 3)
        outgoing = _deserialize(completion[-1]["Put"]["Item"])
        self.assertEqual(outgoing["eventEnvelope"], envelope)
        self.assertEqual(outgoing["dedupeKey"], envelope["eventId"])
        self.assertNotIn("acct_", repr(completion))

    def test_mapping_lag_retry_is_revision_and_sequence_bound(self):
        client = MemoryDynamoClient()
        store = DynamoStripeWebhookStore("receipts", client=client)

        store.retry_ingress(
            scope=scope(),
            outbox_id="evt-1",
            receipt_id="evt-1",
            claimed_revision=2,
            sequence="101",
        )

        transaction = client.transactions[-1]["TransactItems"]
        self.assertEqual(len(transaction), 2)
        rendered = repr(transaction)
        self.assertIn("processingSequence = :sequence", rendered)
        self.assertIn("processingStatus = :processing", rendered)
        self.assertIn("decisionCode = :retryable", rendered)
        self.assertNotIn("eventEnvelope", rendered)

    def test_subscription_projection_uses_local_monotonic_revision_and_event_order(
        self,
    ):
        client = MemoryDynamoClient()
        store = DynamoStripeWebhookStore("receipts", client=client)
        first = store.plan_subscription_projection(
            scope=scope(),
            subscription_id="attempt-1",
            offer_version_id="offer-v1",
            status="active",
            current_period_end=1_900_000_000,
            event_id="evt-1",
            event_created_at=NOW,
            state_hash="d" * 64,
        )
        self.assertEqual(first["sourceRevision"], 1)
        self.assertEqual(first["expectedRevision"], 0)
        self.assertFalse(first["stale"])

        projection = {
            "pk": scope().partition_key,
            "sk": "STRIPE_SUBSCRIPTION_PROJECTION#attempt-1",
            "itemType": "StripeSubscriptionProjection",
            **scope().fields(),
            "subscriptionId": "attempt-1",
            "offerVersionId": "offer-v1",
            "status": "past_due",
            "currentPeriodEnd": 1_900_000_000,
            "sourceRevision": 2,
            "lastEventId": "evt-2",
            "lastEventCreatedAt": NOW + 10,
            "stateHash": "e" * 64,
        }
        client.items[(projection["pk"], projection["sk"])] = projection
        stale = store.plan_subscription_projection(
            scope=scope(),
            subscription_id="attempt-1",
            offer_version_id="offer-v1",
            status="active",
            current_period_end=1_900_000_000,
            event_id="evt-1",
            event_created_at=NOW,
            state_hash="d" * 64,
        )
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["sourceRevision"], 2)
        self.assertNotIn("expiresAt", projection)

    def test_subscription_projection_is_written_to_the_registry_table(self):
        class Client:
            def __init__(self):
                self.transactions = []
                self.receipt = {
                    "pk": scope().partition_key,
                    "sk": "WEBHOOK_RECEIPT#evt-1",
                    "itemType": "WebhookReceipt",
                    **scope().fields(),
                    "receiptId": "evt-1",
                    "status": "processing",
                    "revision": 2,
                    "receivedAt": NOW,
                    "expiresAt": TTL,
                }

            def get_item(self, **kwargs):
                return {"Item": _serialize(self.receipt)}

            def transact_write_items(self, **kwargs):
                self.transactions.append(kwargs)

        client = Client()
        store = DynamoStripeWebhookStore(
            "receipts", projection_table_name="registry", client=client
        )
        store.complete_ingress(
            scope=scope(),
            outbox_id="evt-1",
            receipt_id="evt-1",
            claimed_revision=2,
            sequence="101",
            decision_code="processed",
            envelopes=[
                IntegrationEventEnvelope(
                    scope=scope(),
                    event_id="integration-event-1",
                    event_type="commerce.subscription.updated.v1",
                    occurred_at=NOW,
                    data={
                        "subscriptionId": "attempt-1",
                        "offerVersionId": "offer-v1",
                        "status": "active",
                        "currentPeriodEnd": NOW + 100,
                        "sourceRevision": 1,
                    },
                ).to_dict()
            ],
            projection={
                "subscriptionId": "attempt-1",
                "offerVersionId": "offer-v1",
                "status": "active",
                "currentPeriodEnd": NOW + 100,
                "sourceRevision": 1,
                "expectedRevision": 0,
                "lastEventId": "evt-1",
                "lastEventCreatedAt": NOW,
                "stateHash": "d" * 64,
                "stale": False,
            },
        )

        projection_put = next(
            operation["Put"]
            for operation in client.transactions[0]["TransactItems"]
            if "Put" in operation
            and _deserialize(operation["Put"]["Item"])["itemType"]
            == "StripeSubscriptionProjection"
        )
        self.assertEqual(projection_put["TableName"], "registry")

    def test_subscription_completion_atomically_commits_projection_without_technical_ttl(
        self,
    ):
        class Client:
            def __init__(self):
                self.calls = []
                self.receipt = {
                    "pk": scope().partition_key,
                    "sk": "WEBHOOK_RECEIPT#evt-1",
                    "itemType": "WebhookReceipt",
                    **scope().fields(),
                    "receiptId": "evt-1",
                    "connectionId": "stripe-primary",
                    "provider": "stripe",
                    "mode": "test",
                    "eventType": "customer.subscription.updated",
                    "accountHash": "a" * 64,
                    "payloadHash": "b" * 64,
                    "status": "processing",
                    "revision": 2,
                    "decisionCode": "processing",
                    "eventCreatedAt": NOW,
                    "receivedAt": NOW,
                    "expiresAt": TTL,
                }

            def get_item(self, **kwargs):
                key = _deserialize(kwargs["Key"])
                return (
                    {"Item": _serialize(self.receipt)}
                    if key["sk"] == self.receipt["sk"]
                    else {}
                )

            def transact_write_items(self, **kwargs):
                self.calls.append(kwargs)

        client = Client()
        store = DynamoStripeWebhookStore("receipts", client=client)
        envelope = IntegrationEventEnvelope(
            scope=scope(),
            event_id="integration-event-1",
            event_type="commerce.subscription.updated.v1",
            occurred_at=NOW,
            data={
                "subscriptionId": "attempt-1",
                "offerVersionId": "offer-v1",
                "status": "active",
                "currentPeriodEnd": 1_900_000_000,
                "sourceRevision": 1,
            },
        ).to_dict()
        projection = {
            "subscriptionId": "attempt-1",
            "offerVersionId": "offer-v1",
            "status": "active",
            "currentPeriodEnd": 1_900_000_000,
            "sourceRevision": 1,
            "expectedRevision": 0,
            "lastEventId": "evt-1",
            "lastEventCreatedAt": NOW,
            "stateHash": "c" * 64,
            "stale": False,
        }
        store.complete_ingress(
            scope=scope(),
            outbox_id="evt-1",
            receipt_id="evt-1",
            claimed_revision=2,
            sequence="101",
            decision_code="processed",
            envelopes=[envelope],
            projection=projection,
        )
        operations = client.calls[0]["TransactItems"]
        self.assertEqual(len(operations), 4)
        projection_item = _deserialize(operations[2]["Put"]["Item"])
        self.assertEqual(projection_item["itemType"], "StripeSubscriptionProjection")
        self.assertNotIn("expiresAt", projection_item)

    def test_projection_condition_uses_event_id_as_same_timestamp_tiebreaker(self):
        class Client:
            def __init__(self):
                self.calls = []
                self.receipt = {
                    "pk": scope().partition_key,
                    "sk": "WEBHOOK_RECEIPT#evt-2",
                    "itemType": "WebhookReceipt",
                    **scope().fields(),
                    "receiptId": "evt-2",
                    "connectionId": "stripe-primary",
                    "provider": "stripe",
                    "mode": "test",
                    "eventType": "customer.subscription.updated",
                    "accountHash": "a" * 64,
                    "payloadHash": "b" * 64,
                    "status": "processing",
                    "revision": 2,
                    "decisionCode": "processing",
                    "eventCreatedAt": NOW,
                    "receivedAt": NOW,
                    "expiresAt": TTL,
                }

            def get_item(self, **kwargs):
                key = _deserialize(kwargs["Key"])
                return (
                    {"Item": _serialize(self.receipt)}
                    if key["sk"] == self.receipt["sk"]
                    else {}
                )

            def transact_write_items(self, **kwargs):
                self.calls.append(kwargs)

        client = Client()
        store = DynamoStripeWebhookStore("receipts", client=client)
        envelope = IntegrationEventEnvelope(
            scope=scope(),
            event_id="integration-event-2",
            event_type="commerce.subscription.updated.v1",
            occurred_at=NOW,
            data={
                "subscriptionId": "attempt-1",
                "offerVersionId": "offer-v1",
                "status": "active",
                "currentPeriodEnd": 1_900_000_000,
                "sourceRevision": 2,
            },
        ).to_dict()
        store.complete_ingress(
            scope=scope(),
            outbox_id="evt-2",
            receipt_id="evt-2",
            claimed_revision=2,
            sequence="102",
            decision_code="processed",
            envelopes=[envelope],
            projection={
                "subscriptionId": "attempt-1",
                "offerVersionId": "offer-v1",
                "status": "active",
                "currentPeriodEnd": 1_900_000_000,
                "sourceRevision": 2,
                "expectedRevision": 1,
                "lastEventId": "evt-2",
                "lastEventCreatedAt": NOW,
                "stateHash": "c" * 64,
                "stale": False,
            },
        )
        projection_put = client.calls[0]["TransactItems"][2]["Put"]
        self.assertIn(
            "lastEventCreatedAt = :created", projection_put["ConditionExpression"]
        )
        self.assertIn("lastEventId < :eventId", projection_put["ConditionExpression"])
        self.assertIn(":eventId", projection_put["ExpressionAttributeValues"])

    def test_delivery_claim_and_mark_are_sequence_bound_and_hash_provider_receipt(self):
        envelope = IntegrationEventEnvelope(
            scope=scope(),
            event_id="integration-event-1",
            event_type="commerce.payment.succeeded.v1",
            occurred_at=NOW,
            data={
                "reservationId": "reservation-1",
                "orderId": "order-1",
                "paymentAttemptId": "attempt-1",
            },
        ).to_dict()
        record = {
            "pk": scope().partition_key,
            "sk": "INTEGRATION_EVENT_OUTBOX#integration-event-1",
            "itemType": "IntegrationEventOutbox",
            **scope().fields(),
            "outboxId": "integration-event-1",
            "eventId": "integration-event-1",
            "eventType": "commerce.payment.succeeded.v1",
            "dedupeKey": "integration-event-1",
            "eventEnvelope": envelope,
            "payloadHash": canonical_hash(envelope),
            "deliveryStatus": "pending",
            "deliveryRevision": 1,
            "createdAt": NOW,
            "expiresAt": TTL,
        }

        class Client:
            def __init__(self):
                self.calls = []

            def update_item(self, **kwargs):
                self.calls.append(kwargs)
                changed = {
                    **record,
                    "deliveryStatus": "delivering",
                    "deliveryRevision": 2,
                    "deliverySequence": "201",
                }
                return {"Attributes": _serialize(changed)}

        client = Client()
        store = DynamoStripeWebhookStore("receipts", client=client)
        claimed = store.claim_delivery(
            scope=scope(),
            outbox_id="integration-event-1",
            expected_revision=1,
            sequence="201",
            record=record,
        )
        self.assertEqual(claimed["deliveryRevision"], 2)
        store.mark_delivered(
            scope=scope(),
            outbox_id="integration-event-1",
            claimed_revision=2,
            sequence="201",
            message_id="provider-message-1",
        )
        mark_call = client.calls[1]
        self.assertNotIn("provider-message-1", repr(mark_call))
        self.assertIn("deliveryReceiptHash", mark_call["UpdateExpression"])


if __name__ == "__main__":
    unittest.main()
