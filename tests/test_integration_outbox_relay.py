import unittest

from src.domain.operations import IntegrationEventEnvelope, canonical_hash
from src.registry import _serialize
from tests.test_registry import scope


def outgoing_record(sequence="201"):
    envelope = IntegrationEventEnvelope(
        scope=scope(),
        event_id="integration-event-1",
        event_type="commerce.payment.succeeded.v1",
        occurred_at=1_800_000_000,
        data={
            "reservationId": "reservation-1",
            "orderId": "order-1",
            "paymentAttemptId": "attempt-1",
        },
    ).to_dict()
    item = {
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
        "createdAt": 1_800_000_000,
        "expiresAt": 1_800_000_000 + 90 * 24 * 60 * 60,
    }
    return {
        "eventID": "opaque-stream-event",
        "eventName": "INSERT",
        "dynamodb": {"SequenceNumber": sequence, "NewImage": _serialize(item)},
    }


class IntegrationOutboxRelayTests(unittest.TestCase):
    def module(self):
        from src.handlers import integration_outbox_relay

        return integration_outbox_relay

    def test_relay_publishes_only_closed_envelope_and_marks_delivery(self):
        class Store:
            def __init__(self):
                self.marked = []

            def claim_delivery(self, **kwargs):
                self.claim = kwargs
                return {
                    "deliveryRevision": 2,
                    "eventEnvelope": kwargs["record"]["eventEnvelope"],
                    "payloadHash": kwargs["record"]["payloadHash"],
                }

            def mark_delivered(self, **kwargs):
                self.marked.append(kwargs)

        class Publisher:
            def publish(self, envelope):
                self.envelope = envelope
                return "message-1"

        store, publisher = Store(), Publisher()
        relay = self.module().IntegrationOutboxRelay(store, publisher)
        response = self.module().handle_records(
            {"Records": [outgoing_record()]}, relay=relay
        )

        self.assertEqual(response, {"batchItemFailures": []})
        self.assertEqual(
            publisher.envelope["eventType"], "commerce.payment.succeeded.v1"
        )
        self.assertEqual(store.marked[0]["message_id"], "message-1")
        self.assertNotIn("acct_", repr(publisher.envelope))
        self.assertNotIn("email", repr(publisher.envelope).lower())

    def test_relay_partial_failure_uses_sequence_numbers_from_first_failure(self):
        class Relay:
            def process(self, record, sequence):
                self.calls = getattr(self, "calls", []) + [sequence]
                if sequence == "202":
                    raise RuntimeError("retry")

        relay = Relay()
        response = self.module().handle_records(
            {
                "Records": [
                    outgoing_record("201"),
                    outgoing_record("202"),
                    outgoing_record("203"),
                ]
            },
            relay=relay,
        )
        self.assertEqual(relay.calls, ["201", "202"])
        self.assertEqual(
            response,
            {
                "batchItemFailures": [
                    {"itemIdentifier": "202"},
                    {"itemIdentifier": "203"},
                ]
            },
        )

    def test_sns_publisher_uses_exact_topic_and_closed_message(self):
        class Client:
            def publish(self, **kwargs):
                self.call = kwargs
                return {"MessageId": "message-1"}

        client = Client()
        envelope = outgoing_record()["dynamodb"]["NewImage"]["eventEnvelope"]["M"]
        envelope = __import__("src.registry", fromlist=["_deserialize"])._deserialize(
            {"eventEnvelope": {"M": envelope}}
        )["eventEnvelope"]
        publisher = self.module().SnsIntegrationEventPublisher(
            "arn:aws:sns:us-east-1:123456789012:events", client=client
        )
        self.assertEqual(publisher.publish(envelope), "message-1")
        self.assertEqual(
            client.call["TopicArn"],
            "arn:aws:sns:us-east-1:123456789012:events",
        )
        self.assertIn(
            '"eventType":"commerce.payment.succeeded.v1"', client.call["Message"]
        )
        self.assertNotIn("acct_", client.call["Message"])


if __name__ == "__main__":
    unittest.main()
