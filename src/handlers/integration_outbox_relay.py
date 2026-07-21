"""Idempotent relay from the Integrations outbox Stream to SNS."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

try:
    from domain.integrations import IntegrationScope
    from domain.operations import (
        IntegrationEventEnvelope,
        IntegrationEventOutbox,
        canonical_hash,
    )
    from registry import _deserialize
except ModuleNotFoundError:
    from src.domain.integrations import IntegrationScope
    from src.domain.operations import (
        IntegrationEventEnvelope,
        IntegrationEventOutbox,
        canonical_hash,
    )
    from src.registry import _deserialize


class SnsIntegrationEventPublisher:
    _TOPIC = re.compile(
        r"arn:(?:aws|aws-us-gov|aws-cn):sns:[a-z0-9-]+:\d{12}:[A-Za-z0-9_-]{1,256}",
        re.ASCII,
    )

    def __init__(self, topic_arn: object, *, client: Any):
        if (
            type(topic_arn) is not str
            or self._TOPIC.fullmatch(topic_arn) is None
            or client is None
        ):
            raise ValueError("Integration event publisher is unavailable")
        self._topic_arn = topic_arn
        self._client = client

    def publish(self, envelope: object) -> str:
        if not isinstance(envelope, Mapping):
            raise ValueError("Integration event is invalid")
        try:
            scope = IntegrationScope(
                envelope["environment"],
                envelope["tenantId"],
                envelope["draftId"],
                envelope["domain"],
            )
            selected = IntegrationEventEnvelope(
                scope=scope,
                event_id=envelope["eventId"],
                event_type=envelope["eventType"],
                occurred_at=envelope["occurredAt"],
                data=envelope["data"],
            ).to_dict()
            if selected != envelope or envelope.get("schemaVersion") != 1:
                raise ValueError
            response = self._client.publish(
                TopicArn=self._topic_arn,
                Message=json.dumps(
                    selected,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ),
                MessageAttributes={
                    "environment": {
                        "DataType": "String",
                        "StringValue": scope.environment,
                    },
                    "eventType": {
                        "DataType": "String",
                        "StringValue": selected["eventType"],
                    },
                },
            )
        except Exception:
            raise RuntimeError("Integration event publish failed") from None
        message_id = (
            response.get("MessageId") if isinstance(response, Mapping) else None
        )
        if (
            type(message_id) is not str
            or not 1 <= len(message_id) <= 256
            or any(
                ord(character) < 33 or ord(character) > 126 for character in message_id
            )
        ):
            raise RuntimeError("Integration event publish failed")
        return message_id


class IntegrationOutboxRelay:
    def __init__(self, store: Any, publisher: Any):
        if store is None or publisher is None:
            raise ValueError("Integration outbox relay is unavailable")
        self._store = store
        self._publisher = publisher

    def process(self, record: dict[str, Any], sequence: str) -> None:
        claimed = self._store.claim_delivery(
            scope=record["scope"],
            outbox_id=record["outboxId"],
            expected_revision=record["deliveryRevision"],
            sequence=sequence,
            record={key: value for key, value in record.items() if key != "scope"},
        )
        if claimed is None:
            return
        envelope = claimed.get("eventEnvelope")
        if (
            not isinstance(envelope, Mapping)
            or claimed.get("payloadHash") != canonical_hash(envelope)
            or envelope != record["eventEnvelope"]
            or claimed.get("deliveryRevision") != record["deliveryRevision"] + 1
        ):
            raise RuntimeError("Integration outbox claim is invalid")
        message_id = self._publisher.publish(dict(envelope))
        self._store.mark_delivered(
            scope=record["scope"],
            outbox_id=record["outboxId"],
            claimed_revision=claimed["deliveryRevision"],
            sequence=sequence,
            message_id=message_id,
        )


def handle_records(event: Any, *, relay: Any) -> dict[str, list[dict[str, str]]]:
    records = event.get("Records") if isinstance(event, Mapping) else None
    if not isinstance(records, list) or not 1 <= len(records) <= 100:
        raise RuntimeError("Integration outbox batch is invalid")
    sequences = [_sequence(record) for record in records]
    for index, record in enumerate(records):
        try:
            relay.process(_outgoing_record(record), sequences[index])
        except Exception:
            return {
                "batchItemFailures": [
                    {"itemIdentifier": sequence} for sequence in sequences[index:]
                ]
            }
    return {"batchItemFailures": []}


def lambda_handler(event: Any, context: Any) -> dict[str, list[dict[str, str]]]:
    del context
    return handle_records(event, relay=_runtime_relay())


def _runtime_relay() -> Any:
    try:
        from runtime import integration_outbox_relay_runtime
    except ModuleNotFoundError:
        from src.runtime import integration_outbox_relay_runtime
    return integration_outbox_relay_runtime()


def _sequence(record: object) -> str:
    dynamodb = record.get("dynamodb") if isinstance(record, Mapping) else None
    value = dynamodb.get("SequenceNumber") if isinstance(dynamodb, Mapping) else None
    if type(value) is not str or not value.isdecimal() or len(value) > 128:
        raise RuntimeError("Integration outbox batch is invalid")
    return value


def _outgoing_record(record: object) -> dict[str, Any]:
    if not isinstance(record, Mapping) or record.get("eventName") not in {
        "INSERT",
        "MODIFY",
    }:
        raise ValueError("Integration outbox record is invalid")
    dynamodb = record.get("dynamodb")
    image = dynamodb.get("NewImage") if isinstance(dynamodb, Mapping) else None
    value = _deserialize(image)
    scope = IntegrationScope(
        value.get("environment"),
        value.get("tenantId"),
        value.get("draftId"),
        value.get("domain"),
    )
    try:
        model = IntegrationEventOutbox(
            scope=scope,
            outbox_id=value.get("outboxId"),
            envelope=value.get("eventEnvelope"),
            payload_hash=value.get("payloadHash"),
            delivery_status=value.get("deliveryStatus"),
            revision=value.get("deliveryRevision"),
            created_at=value.get("createdAt"),
            expires_at=value.get("expiresAt"),
        )
        if model.to_record() != value or value.get("deliveryStatus") != "pending":
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Integration outbox record is invalid") from None
    return {**value, "scope": scope}
