"""Closed technical receipt, outbox, and idempotency row contracts."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from types import MappingProxyType
from typing import Any, Mapping

from .integrations import IntegrationScope, TECHNICAL_TTL_SECONDS


COMMERCE_EVENT_TYPES = frozenset(
    {
        "commerce.payment.succeeded.v1",
        "commerce.payment.terminal_unpaid.v1",
        "commerce.refund.confirmed.v1",
        "commerce.subscription.updated.v1",
    }
)
MIGRATION_EVENT_TYPES = frozenset(
    {
        "migration.preview_ready.v1",
        "migration.progressed.v1",
        "migration.item_needs_review.v1",
        "migration.completed.v1",
    }
)
INTEGRATION_EVENT_TYPES = COMMERCE_EVENT_TYPES | MIGRATION_EVENT_TYPES
STRIPE_WEBHOOK_EVENT_TYPES = frozenset(
    {
        "checkout.session.completed",
        "checkout.session.expired",
        "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed",
        "refund.created",
        "refund.updated",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
        "customer.subscription.pending_update_applied",
        "customer.subscription.pending_update_expired",
        "invoice.paid",
        "invoice.payment_failed",
        "account.application.deauthorized",
    }
)
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}", re.ASCII)
_COMMERCE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}", re.ASCII)
_EVENT_TYPE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}", re.ASCII)
_HASH = re.compile(r"[a-f0-9]{64}", re.ASCII)
_MIGRATION_ITEM_ID = re.compile(r"migration-item-[a-f0-9]{40}", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}", re.ASCII)
_PAYMENT_KEYS = frozenset({"reservationId", "orderId", "paymentAttemptId"})
_REFUND_KEYS = frozenset({"orderId", "refundId", "amountMinor", "currency"})
_SUBSCRIPTION_KEYS = frozenset(
    {"subscriptionId", "offerVersionId", "status", "currentPeriodEnd", "sourceRevision"}
)
_MIGRATION_COMMON_KEYS = frozenset(
    {"commercialRequestId", "jobId", "connectionId", "revision", "dedupeKey"}
)
_MIGRATION_COUNTS_KEYS = frozenset(
    {"total", "pending", "applied", "needsReview", "failed"}
)
_MIGRATION_JOB_STATES = frozenset(
    {
        "previewing",
        "awaiting_approval",
        "scheduled",
        "running",
        "paused",
        "cancel_requested",
        "canceling",
        "completed",
        "completed_with_errors",
        "canceled",
    }
)
_MIGRATION_TERMINAL_STATES = frozenset(
    {"completed", "completed_with_errors", "canceled"}
)
_MIGRATION_REASON_CODES = frozenset(
    {
        "ambiguous-price",
        "near-term-schedule",
        "nonpositive-proration",
        "payment-failed",
        "pending-invoice-items",
        "pending-update",
        "phase-limit",
        "provider-unknown",
        "retry-exhausted",
        "scope-mismatch",
        "snapshot-too-large",
        "source-drift",
        "tax-approval",
        "unmapped-price",
        "unpaid-invoice",
        "unsupported-collection-mode",
        "unsupported-payment-method",
        "unsupported-schedule",
    }
)
_ENVELOPE_KEYS = frozenset(
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
    }
)


@dataclass(frozen=True, slots=True)
class WebhookReceipt:
    scope: IntegrationScope
    receipt_id: str
    connection_id: str
    provider: str
    mode: str
    event_type: str
    account_hash: str
    payload_hash: str
    status: str
    revision: int
    decision_code: str
    event_created_at: int
    received_at: int
    expires_at: int

    def __post_init__(self) -> None:
        _scope(self.scope)
        _id(self.receipt_id)
        _id(self.connection_id)
        if self.provider not in {"stripe"}:
            raise ValueError("webhook provider is invalid")
        _mode(self.scope, self.mode)
        if self.event_type not in STRIPE_WEBHOOK_EVENT_TYPES:
            raise ValueError("webhook event type is invalid")
        _hash(self.account_hash)
        _hash(self.payload_hash)
        if self.status not in {
            "received",
            "processing",
            "processed",
            "ignored",
            "failed",
            "needs_review",
        }:
            raise ValueError("webhook status is invalid")
        _positive_int(self.revision, "webhook revision")
        if self.decision_code not in {
            "queued",
            "processing",
            "processed",
            "ignored_unmapped",
            "ignored_nonterminal",
            "ignored_no_change",
            "retryable",
            "needs_review",
        }:
            raise ValueError("webhook decision code is invalid")
        _epoch(self.event_created_at, "webhook event timestamp")
        _technical_times(self.received_at, self.expires_at)

    def to_record(self) -> dict[str, Any]:
        return {
            "pk": self.scope.partition_key,
            "sk": f"WEBHOOK_RECEIPT#{self.receipt_id}",
            "itemType": "WebhookReceipt",
            **self.scope.fields(),
            "receiptId": self.receipt_id,
            "connectionId": self.connection_id,
            "provider": self.provider,
            "mode": self.mode,
            "eventType": self.event_type,
            "accountHash": self.account_hash,
            "payloadHash": self.payload_hash,
            "status": self.status,
            "revision": self.revision,
            "decisionCode": self.decision_code,
            "eventCreatedAt": self.event_created_at,
            "receivedAt": self.received_at,
            "expiresAt": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class GlobalWebhookReplaySentinel:
    environment: str
    event_id: str
    account_hash: str
    mode: str
    payload_hash: str
    receipt_pk: str
    receipt_sk: str
    created_at: int
    expires_at: int

    def __post_init__(self) -> None:
        if self.environment not in {"test", "production"}:
            raise ValueError("webhook environment is invalid")
        _id(self.event_id)
        _hash(self.account_hash)
        expected_mode = "test" if self.environment == "test" else "live"
        if self.mode != expected_mode:
            raise ValueError("webhook mode is invalid")
        _hash(self.payload_hash)
        if (
            type(self.receipt_pk) is not str
            or not self.receipt_pk.startswith(f"ENV#{self.environment}#TENANT#")
            or type(self.receipt_sk) is not str
            or self.receipt_sk != f"WEBHOOK_RECEIPT#{self.event_id}"
        ):
            raise ValueError("webhook receipt reference is invalid")
        _technical_times(self.created_at, self.expires_at)

    def to_record(self) -> dict[str, Any]:
        return {
            "pk": f"GLOBAL_WEBHOOK#{self.environment}#stripe#{self.event_id}",
            "sk": "CLAIM",
            "itemType": "GlobalWebhookReplaySentinel",
            "environment": self.environment,
            "provider": "stripe",
            "eventId": self.event_id,
            "accountHash": self.account_hash,
            "mode": self.mode,
            "payloadHash": self.payload_hash,
            "receiptPk": self.receipt_pk,
            "receiptSk": self.receipt_sk,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class WebhookIngressOutbox:
    scope: IntegrationScope
    outbox_id: str
    receipt_id: str
    processing_status: str
    revision: int
    attempt_count: int
    created_at: int
    expires_at: int
    processing_sequence: str | None = None

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
        _positive_int(self.revision, "ingress revision")
        if type(self.attempt_count) is not int or not 0 <= self.attempt_count <= 100:
            raise ValueError("ingress attempt count is invalid")
        if self.processing_sequence is not None and (
            type(self.processing_sequence) is not str
            or not self.processing_sequence.isdecimal()
        ):
            raise ValueError("ingress processing sequence is invalid")
        _technical_times(self.created_at, self.expires_at)

    def to_record(self) -> dict[str, Any]:
        record = {
            "pk": self.scope.partition_key,
            "sk": f"WEBHOOK_INGRESS_OUTBOX#{self.outbox_id}",
            "itemType": "WebhookIngressOutbox",
            **self.scope.fields(),
            "outboxId": self.outbox_id,
            "receiptId": self.receipt_id,
            "processingStatus": self.processing_status,
            "processingRevision": self.revision,
            "attemptCount": self.attempt_count,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
        }
        if self.processing_sequence is not None:
            record["processingSequence"] = self.processing_sequence
        return record


@dataclass(frozen=True, slots=True)
class IntegrationEventEnvelope:
    scope: IntegrationScope
    event_id: str
    event_type: str
    occurred_at: int
    data: Mapping[str, Any]

    def __post_init__(self) -> None:
        _scope(self.scope)
        _commerce_id(self.event_id)
        if self.event_type not in INTEGRATION_EVENT_TYPES:
            raise ValueError("integration event type is invalid")
        _epoch(self.occurred_at, "integration event timestamp")
        object.__setattr__(
            self,
            "data",
            MappingProxyType(
                _migration_data(self.event_type, self.data)
                if self.event_type in MIGRATION_EVENT_TYPES
                else _commerce_data(self.event_type, self.data)
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "eventId": self.event_id,
            "eventType": self.event_type,
            "occurredAt": self.occurred_at,
            **self.scope.fields(),
            "data": dict(self.data),
        }


@dataclass(frozen=True, slots=True)
class IntegrationEventOutbox:
    scope: IntegrationScope
    outbox_id: str
    envelope: Mapping[str, Any]
    payload_hash: str
    delivery_status: str
    revision: int
    created_at: int
    expires_at: int

    def __post_init__(self) -> None:
        _scope(self.scope)
        _id(self.outbox_id)
        normalized = _parse_envelope(self.envelope)
        if any(
            normalized[field] != expected
            for field, expected in self.scope.fields().items()
        ):
            raise ValueError("integration event scope is invalid")
        object.__setattr__(self, "envelope", MappingProxyType(normalized))
        _hash(self.payload_hash)
        if canonical_hash(normalized) != self.payload_hash:
            raise ValueError("integration event payload hash is invalid")
        if self.delivery_status not in {"pending", "delivering", "delivered", "failed"}:
            raise ValueError("delivery status is invalid")
        _positive_int(self.revision, "delivery revision")
        _technical_times(self.created_at, self.expires_at)

    def to_record(self) -> dict[str, Any]:
        envelope = dict(self.envelope)
        envelope["data"] = dict(envelope["data"])
        return {
            "pk": self.scope.partition_key,
            "sk": f"INTEGRATION_EVENT_OUTBOX#{self.outbox_id}",
            "itemType": "IntegrationEventOutbox",
            **self.scope.fields(),
            "outboxId": self.outbox_id,
            "eventId": envelope["eventId"],
            "eventType": envelope["eventType"],
            "dedupeKey": envelope["eventId"],
            "eventEnvelope": envelope,
            "payloadHash": self.payload_hash,
            "deliveryStatus": self.delivery_status,
            "deliveryRevision": self.revision,
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


def canonical_hash(value: object) -> str:
    """Return the stable digest used to bind closed technical payloads."""
    try:
        payload = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise ValueError("canonical payload is invalid") from None
    return hashlib.sha256(payload).hexdigest()


def _commerce_id(value: object) -> str:
    if type(value) is not str or _COMMERCE_ID.fullmatch(value) is None:
        raise ValueError("integration event identifier is invalid")
    return value


def _epoch(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= 9_999_999_999:
        raise ValueError(f"{name} is invalid")
    return value


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or not 1 <= value <= 9_999_999_999:
        raise ValueError(f"{name} is invalid")
    return value


def _commerce_data(event_type: str, value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("integration event data is invalid")
    if event_type.startswith("commerce.payment."):
        if set(value) != _PAYMENT_KEYS:
            raise ValueError("integration event data is invalid")
        return {key: _commerce_id(value[key]) for key in sorted(_PAYMENT_KEYS)}
    if event_type == "commerce.refund.confirmed.v1":
        if set(value) != _REFUND_KEYS:
            raise ValueError("integration event data is invalid")
        amount_minor = value["amountMinor"]
        currency = value["currency"]
        if type(amount_minor) is not int or amount_minor <= 0:
            raise ValueError("integration event data is invalid")
        if type(currency) is not str or _CURRENCY.fullmatch(currency) is None:
            raise ValueError("integration event data is invalid")
        return {
            "orderId": _commerce_id(value["orderId"]),
            "refundId": _commerce_id(value["refundId"]),
            "amountMinor": amount_minor,
            "currency": currency,
        }
    if set(value) != _SUBSCRIPTION_KEYS:
        raise ValueError("integration event data is invalid")
    if value["status"] not in {"active", "past_due", "canceled"}:
        raise ValueError("integration event data is invalid")
    return {
        "subscriptionId": _commerce_id(value["subscriptionId"]),
        "offerVersionId": _commerce_id(value["offerVersionId"]),
        "status": value["status"],
        "currentPeriodEnd": _epoch(
            value["currentPeriodEnd"], "subscription period end"
        ),
        "sourceRevision": _positive_int(
            value["sourceRevision"], "subscription source revision"
        ),
    }


def _migration_data(event_type: str, value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("integration event data is invalid")
    required = set(_MIGRATION_COMMON_KEYS)
    if event_type == "migration.preview_ready.v1":
        required.update({"dryRunRevision", "dryRunHash", "expiresAt", "counts"})
    elif event_type in {"migration.progressed.v1", "migration.completed.v1"}:
        required.update({"state", "counts"})
    else:
        required.update({"itemId", "reasonCode"})
    if set(value) != required:
        raise ValueError("integration event data is invalid")
    output = {
        field: _commerce_id(value[field])
        for field in sorted(_MIGRATION_COMMON_KEYS - {"revision"})
    }
    output["revision"] = _positive_int(value["revision"], "migration revision")
    if event_type == "migration.preview_ready.v1":
        output.update(
            {
                "dryRunRevision": _positive_int(
                    value["dryRunRevision"], "migration dry-run revision"
                ),
                "dryRunHash": _migration_hash(value["dryRunHash"]),
                "expiresAt": _positive_int(value["expiresAt"], "migration expiry"),
                "counts": _migration_counts(value["counts"]),
            }
        )
    elif event_type in {"migration.progressed.v1", "migration.completed.v1"}:
        terminal = value["state"] in _MIGRATION_TERMINAL_STATES
        if (
            value["state"] not in _MIGRATION_JOB_STATES
            or (event_type == "migration.completed.v1") != terminal
        ):
            raise ValueError("integration event data is invalid")
        output.update(
            {"state": value["state"], "counts": _migration_counts(value["counts"])}
        )
    else:
        reason = value["reasonCode"]
        if reason not in _MIGRATION_REASON_CODES:
            raise ValueError("integration event data is invalid")
        item_id = value["itemId"]
        if type(item_id) is not str or _MIGRATION_ITEM_ID.fullmatch(item_id) is None:
            raise ValueError("integration event data is invalid")
        output.update({"itemId": item_id, "reasonCode": reason})
    return {key: output[key] for key in value}


def _migration_counts(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != _MIGRATION_COUNTS_KEYS:
        raise ValueError("integration event data is invalid")
    counts = dict(value)
    if any(type(count) is not int or count < 0 for count in counts.values()):
        raise ValueError("integration event data is invalid")
    if (
        counts["pending"]
        + counts["applied"]
        + counts["needsReview"]
        + counts["failed"]
        != counts["total"]
    ):
        raise ValueError("integration event data is invalid")
    return counts


def _migration_hash(value: object) -> str:
    _hash(value)
    return value


def _parse_envelope(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _ENVELOPE_KEYS:
        raise ValueError("integration event envelope is invalid")
    if value.get("schemaVersion") != 1:
        raise ValueError("integration event envelope is invalid")
    try:
        scope = IntegrationScope(
            environment=value["environment"],
            tenant_id=value["tenantId"],
            draft_id=value["draftId"],
            domain=value["domain"],
        )
        normalized = IntegrationEventEnvelope(
            scope=scope,
            event_id=value["eventId"],
            event_type=value["eventType"],
            occurred_at=value["occurredAt"],
            data=value["data"],
        ).to_dict()
    except (KeyError, TypeError, ValueError):
        raise ValueError("integration event envelope is invalid") from None
    return normalized


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
