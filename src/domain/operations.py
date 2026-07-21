"""Closed technical receipt, outbox, and idempotency row contracts."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .integrations import IntegrationScope, TECHNICAL_TTL_SECONDS


COMMERCE_EVENT_TYPES = frozenset(
    {
        "commerce.payment.succeeded.v1",
        "commerce.payment.terminal_unpaid.v1",
        "commerce.refund.confirmed.v1",
        "commerce.subscription.updated.v1",
    }
)
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}", re.ASCII)
_EVENT_TYPE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}", re.ASCII)
_HASH = re.compile(r"[a-f0-9]{64}", re.ASCII)


@dataclass(frozen=True, slots=True)
class WebhookReceipt:
    scope: IntegrationScope
    receipt_id: str
    provider: str
    mode: str
    event_type: str
    payload_hash: str
    status: str
    received_at: int
    expires_at: int

    def __post_init__(self) -> None:
        _scope(self.scope)
        _id(self.receipt_id)
        if self.provider not in {"stripe"}:
            raise ValueError("webhook provider is invalid")
        _mode(self.scope, self.mode)
        if _EVENT_TYPE.fullmatch(self.event_type) is None:
            raise ValueError("webhook event type is invalid")
        _hash(self.payload_hash)
        if self.status not in {
            "received",
            "processing",
            "processed",
            "ignored",
            "failed",
        }:
            raise ValueError("webhook status is invalid")
        _technical_times(self.received_at, self.expires_at)

    def to_record(self) -> dict[str, Any]:
        return {
            "pk": self.scope.partition_key,
            "sk": f"WEBHOOK_RECEIPT#{self.receipt_id}",
            "itemType": "WebhookReceipt",
            **self.scope.fields(),
            "receiptId": self.receipt_id,
            "provider": self.provider,
            "mode": self.mode,
            "eventType": self.event_type,
            "payloadHash": self.payload_hash,
            "status": self.status,
            "receivedAt": self.received_at,
            "expiresAt": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class WebhookIngressOutbox:
    scope: IntegrationScope
    outbox_id: str
    receipt_id: str
    processing_status: str
    created_at: int
    expires_at: int

    def __post_init__(self) -> None:
        _scope(self.scope)
        _id(self.outbox_id)
        _id(self.receipt_id)
        if self.processing_status not in {
            "pending",
            "processing",
            "processed",
            "failed",
        }:
            raise ValueError("ingress status is invalid")
        _technical_times(self.created_at, self.expires_at)

    def to_record(self) -> dict[str, Any]:
        return {
            "pk": self.scope.partition_key,
            "sk": f"WEBHOOK_INGRESS_OUTBOX#{self.outbox_id}",
            "itemType": "WebhookIngressOutbox",
            **self.scope.fields(),
            "outboxId": self.outbox_id,
            "receiptId": self.receipt_id,
            "processingStatus": self.processing_status,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class IntegrationEventOutbox:
    scope: IntegrationScope
    outbox_id: str
    event_id: str
    event_type: str
    dedupe_key: str
    delivery_status: str
    created_at: int
    expires_at: int

    def __post_init__(self) -> None:
        _scope(self.scope)
        _id(self.outbox_id)
        _id(self.event_id)
        _id(self.dedupe_key)
        if self.event_type not in COMMERCE_EVENT_TYPES:
            raise ValueError("integration event type is invalid")
        if self.delivery_status not in {"pending", "delivering", "delivered", "failed"}:
            raise ValueError("delivery status is invalid")
        _technical_times(self.created_at, self.expires_at)

    def to_record(self) -> dict[str, Any]:
        return {
            "pk": self.scope.partition_key,
            "sk": f"INTEGRATION_EVENT_OUTBOX#{self.outbox_id}",
            "itemType": "IntegrationEventOutbox",
            **self.scope.fields(),
            "outboxId": self.outbox_id,
            "eventId": self.event_id,
            "eventType": self.event_type,
            "dedupeKey": self.dedupe_key,
            "deliveryStatus": self.delivery_status,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class IdempotencyReceipt:
    scope: IntegrationScope
    receipt_id: str
    operation: str
    request_hash: str
    status: str
    created_at: int
    expires_at: int

    def __post_init__(self) -> None:
        _scope(self.scope)
        _id(self.receipt_id)
        _id(self.operation)
        _hash(self.request_hash)
        if self.status not in {"pending", "succeeded", "failed", "unknown"}:
            raise ValueError("idempotency status is invalid")
        _technical_times(self.created_at, self.expires_at)

    def to_record(self) -> dict[str, Any]:
        return {
            "pk": self.scope.partition_key,
            "sk": f"IDEMPOTENCY#{self.receipt_id}",
            "itemType": "IdempotencyReceipt",
            **self.scope.fields(),
            "receiptId": self.receipt_id,
            "operation": self.operation,
            "requestHash": self.request_hash,
            "status": self.status,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
        }


def _scope(value: object) -> None:
    if type(value) is not IntegrationScope:
        raise ValueError("operation scope is invalid")


def _id(value: object) -> None:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise ValueError("operation identifier is invalid")


def _hash(value: object) -> None:
    if type(value) is not str or _HASH.fullmatch(value) is None:
        raise ValueError("operation hash is invalid")


def _mode(scope: IntegrationScope, mode: object) -> None:
    expected = "test" if scope.environment == "test" else "live"
    if mode != expected:
        raise ValueError("operation mode is invalid")


def _technical_times(created_at: object, expires_at: object) -> None:
    if (
        type(created_at) is not int
        or created_at < 0
        or type(expires_at) is not int
        or expires_at != created_at + TECHNICAL_TTL_SECONDS
    ):
        raise ValueError("technical expiry is invalid")
