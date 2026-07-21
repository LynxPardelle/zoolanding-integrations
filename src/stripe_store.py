"""Conditional DynamoDB receipts and server-only Stripe resource mappings."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

try:
    from domain.integrations import IntegrationScope
    from domain.operations import (
        IntegrationEventOutbox,
        GlobalWebhookReplaySentinel,
        WebhookIngressOutbox,
        WebhookReceipt,
        canonical_hash,
    )
    from registry import _deserialize, _serialize
    from stripe_commands import StripeCommandConflict
except ModuleNotFoundError:
    from src.domain.integrations import IntegrationScope
    from src.domain.operations import (
        IntegrationEventOutbox,
        GlobalWebhookReplaySentinel,
        WebhookIngressOutbox,
        WebhookReceipt,
        canonical_hash,
    )
    from src.registry import _deserialize, _serialize
    from src.stripe_commands import StripeCommandConflict


_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}", re.ASCII)
_COMMAND_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}", re.ASCII)
_HASH = re.compile(r"[a-f0-9]{64}", re.ASCII)
_FORBIDDEN_KEYS = {
    "address",
    "customer",
    "customeremail",
    "email",
    "name",
    "phone",
    "redirecturl",
    "secret",
    "token",
}
_PROVIDER_OBJECT_FIELDS = {
    "product": "productId",
    "price": "priceId",
    "coupon": "couponId",
    "promotion-code": "promotionCodeId",
    "checkout-session": "sessionId",
    "payment-intent": "paymentIntentId",
    "subscription": "providerSubscriptionId",
    "charge": "chargeId",
}


class StripeStoreError(RuntimeError):
    pass


class WebhookReplayConflict(StripeStoreError):
    pass


class DynamoStripeWebhookStore:
    """Atomic receipt/outbox persistence without retaining provider payloads."""

    def __init__(self, table_name: str, *, client=None):
        if type(table_name) is not str or not table_name.strip():
            raise StripeStoreError("Stripe webhook store is unavailable")
        if client is None:
            try:
                import boto3  # type: ignore

                client = boto3.client("dynamodb")
            except Exception:
                raise StripeStoreError("Stripe webhook store is unavailable") from None
        self._table_name = table_name
        self._client = client

    def accept_supported(
        self,
        *,
        scope: IntegrationScope,
        connection_id: str,
        event_id: str,
        event_type: str,
        account_hash: str,
        mode: str,
        payload_hash: str,
        event_created_at: int,
        received_at: int,
        expires_at: int,
    ) -> dict[str, Any]:
        receipt = WebhookReceipt(
            scope=scope,
            receipt_id=event_id,
            connection_id=connection_id,
            provider="stripe",
            mode=mode,
            event_type=event_type,
            account_hash=account_hash,
            payload_hash=payload_hash,
            status="received",
            revision=1,
            decision_code="queued",
            event_created_at=event_created_at,
            received_at=received_at,
            expires_at=expires_at,
        ).to_record()
        sentinel = GlobalWebhookReplaySentinel(
            environment=scope.environment,
            event_id=event_id,
            account_hash=account_hash,
            mode=mode,
            payload_hash=payload_hash,
            receipt_pk=receipt["pk"],
            receipt_sk=receipt["sk"],
            created_at=received_at,
            expires_at=expires_at,
        ).to_record()
        ingress = WebhookIngressOutbox(
            scope=scope,
            outbox_id=event_id,
            receipt_id=event_id,
            processing_status="pending",
            revision=1,
            attempt_count=0,
            created_at=received_at,
            expires_at=expires_at,
        ).to_record()
        records = (sentinel, receipt, ingress)
        operations = [
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _serialize(record),
                    "ConditionExpression": (
                        "attribute_not_exists(pk) AND attribute_not_exists(sk)"
                    ),
                }
            }
            for record in records
        ]
        try:
            self._client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=hashlib.sha256(
                    (
                        "stripe-webhook-v1\0"
                        + scope.environment
                        + "\0"
                        + event_id
                        + "\0"
                        + payload_hash
                    ).encode("ascii")
                ).hexdigest()[:36],
            )
            return {"status": "queued", "duplicate": False}
        except Exception:
            existing = tuple(
                self._get(record["pk"], record["sk"]) for record in records
            )
            if existing == records:
                return {"status": "queued", "duplicate": True}
            existing_sentinel, existing_receipt, existing_ingress = existing
            if not (
                existing_sentinel == sentinel
                and _same_webhook_receipt(existing_receipt, receipt)
                and _same_ingress_outbox(existing_ingress, ingress)
            ):
                raise WebhookReplayConflict(
                    "Stripe webhook replay conflicted"
                ) from None
            if existing_ingress.get(
                "processingStatus"
            ) == "processed" and existing_receipt.get("status") in {
                "processed",
                "ignored",
                "needs_review",
            }:
                return {"status": "processed", "duplicate": True}
            if (
                existing_ingress.get("processingStatus") == "pending"
                and existing_receipt.get("status") == "received"
            ):
                return {"status": "queued", "duplicate": True}
            current_revision = existing_ingress.get("processingRevision")
            if (
                type(current_revision) is not int
                or current_revision < 1
                or existing_receipt.get("revision") != current_revision
                or existing_ingress.get("processingStatus")
                not in {"processing", "failed"}
                or existing_receipt.get("status") not in {"processing", "failed"}
            ):
                raise WebhookReplayConflict(
                    "Stripe webhook replay conflicted"
                ) from None
            self._reactivate(
                scope,
                event_id,
                current_revision=current_revision,
                ingress_status=existing_ingress["processingStatus"],
                receipt_status=existing_receipt["status"],
                payload_hash=payload_hash,
            )
            return {"status": "queued", "duplicate": True}

    def _reactivate(
        self,
        scope: IntegrationScope,
        event_id: str,
        *,
        current_revision: int,
        ingress_status: str,
        receipt_status: str,
        payload_hash: str,
    ) -> None:
        values = _serialize_values(
            {
                ":outbox_type": "WebhookIngressOutbox",
                ":receipt_type": "WebhookReceipt",
                ":event_id": event_id,
                ":ingress_status": ingress_status,
                ":receipt_status": receipt_status,
                ":pending": "pending",
                ":received": "received",
                ":queued": "queued",
                ":expected": current_revision,
                ":next": current_revision + 1,
            }
        )
        operations = [
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _serialize(
                        {
                            "pk": scope.partition_key,
                            "sk": f"WEBHOOK_INGRESS_OUTBOX#{event_id}",
                        }
                    ),
                    "UpdateExpression": (
                        "SET processingStatus = :pending, "
                        "processingRevision = :next REMOVE processingSequence"
                    ),
                    "ConditionExpression": (
                        "itemType = :outbox_type AND receiptId = :event_id AND "
                        "processingStatus = :ingress_status AND "
                        "processingRevision = :expected"
                    ),
                    "ExpressionAttributeValues": values,
                }
            },
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _serialize(
                        {
                            "pk": scope.partition_key,
                            "sk": f"WEBHOOK_RECEIPT#{event_id}",
                        }
                    ),
                    "UpdateExpression": (
                        "SET #status = :received, revision = :next, "
                        "decisionCode = :queued"
                    ),
                    "ConditionExpression": (
                        "itemType = :receipt_type AND #status = :receipt_status AND "
                        "revision = :expected"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": values,
                }
            },
        ]
        try:
            self._client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=hashlib.sha256(
                    (
                        "stripe-webhook-reactivate-v1\0"
                        + scope.partition_key
                        + "\0"
                        + event_id
                        + "\0"
                        + str(current_revision)
                        + "\0"
                        + payload_hash
                    ).encode("ascii")
                ).hexdigest()[:36],
            )
        except Exception:
            ingress = self._get(
                scope.partition_key, f"WEBHOOK_INGRESS_OUTBOX#{event_id}"
            )
            receipt = self._get(scope.partition_key, f"WEBHOOK_RECEIPT#{event_id}")
            if (
                ingress is not None
                and receipt is not None
                and ingress.get("processingStatus")
                in {"pending", "processing", "processed"}
                and receipt.get("status")
                in {"received", "processing", "processed", "ignored", "needs_review"}
            ):
                return
            raise WebhookReplayConflict("Stripe webhook replay conflicted") from None

    def claim_ingress(
        self,
        *,
        scope: IntegrationScope,
        outbox_id: str,
        receipt_id: str,
        expected_revision: int,
        sequence: str,
    ) -> dict[str, int] | None:
        _identity(scope, "stripe-webhook")
        _identifier(outbox_id)
        _identifier(receipt_id)
        if (
            outbox_id != receipt_id
            or type(expected_revision) is not int
            or expected_revision < 1
            or type(sequence) is not str
            or not sequence.isdecimal()
            or len(sequence) > 128
        ):
            raise StripeStoreError("Stripe webhook claim is unavailable")
        values = _serialize_values(
            {
                ":outbox_type": "WebhookIngressOutbox",
                ":receipt_type": "WebhookReceipt",
                ":receipt_id": receipt_id,
                ":pending": "pending",
                ":received": "received",
                ":failed": "failed",
                ":processing": "processing",
                ":expected": expected_revision,
                ":next": expected_revision + 1,
                ":one": 1,
                ":sequence": sequence,
            }
        )
        operations = [
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _serialize(
                        {
                            "pk": scope.partition_key,
                            "sk": f"WEBHOOK_INGRESS_OUTBOX#{outbox_id}",
                        }
                    ),
                    "UpdateExpression": (
                        "SET processingStatus = :processing, "
                        "processingRevision = :next, "
                        "attemptCount = attemptCount + :one, "
                        "processingSequence = :sequence"
                    ),
                    "ConditionExpression": (
                        "itemType = :outbox_type AND receiptId = :receipt_id AND "
                        "processingStatus = :pending AND processingRevision = :expected"
                    ),
                    "ExpressionAttributeValues": values,
                }
            },
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _serialize(
                        {
                            "pk": scope.partition_key,
                            "sk": f"WEBHOOK_RECEIPT#{receipt_id}",
                        }
                    ),
                    "UpdateExpression": (
                        "SET #status = :processing, revision = :next, "
                        "decisionCode = :processing"
                    ),
                    "ConditionExpression": (
                        "itemType = :receipt_type AND revision = :expected AND "
                        "#status IN (:received, :failed)"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": values,
                }
            },
        ]
        try:
            self._client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=hashlib.sha256(
                    (
                        "stripe-worker-claim-v1\0"
                        + scope.partition_key
                        + "\0"
                        + receipt_id
                        + "\0"
                        + str(expected_revision)
                        + "\0"
                        + sequence
                    ).encode("ascii")
                ).hexdigest()[:36],
            )
            return {"processingRevision": expected_revision + 1}
        except Exception:
            outbox = self._get(
                scope.partition_key, f"WEBHOOK_INGRESS_OUTBOX#{outbox_id}"
            )
            receipt = self._get(scope.partition_key, f"WEBHOOK_RECEIPT#{receipt_id}")
            if (
                outbox is not None
                and receipt is not None
                and outbox.get("processingStatus") == "processing"
                and outbox.get("processingSequence") == sequence
                and outbox.get("processingRevision") == expected_revision + 1
                and receipt.get("status") == "processing"
                and receipt.get("revision") == expected_revision + 1
            ):
                return {"processingRevision": expected_revision + 1}
            if outbox is not None and outbox.get("processingStatus") == "processed":
                return None
            raise StripeStoreError("Stripe webhook claim is unavailable") from None

    def receipt(self, scope: IntegrationScope, receipt_id: str) -> dict[str, Any]:
        _identity(scope, "stripe-webhook")
        _identifier(receipt_id)
        record = self._get(scope.partition_key, f"WEBHOOK_RECEIPT#{receipt_id}")
        if not isinstance(record, Mapping):
            raise StripeStoreError("Stripe webhook receipt is unavailable")
        try:
            model = WebhookReceipt(
                scope=scope,
                receipt_id=record.get("receiptId"),
                connection_id=record.get("connectionId"),
                provider=record.get("provider"),
                mode=record.get("mode"),
                event_type=record.get("eventType"),
                account_hash=record.get("accountHash"),
                payload_hash=record.get("payloadHash"),
                status=record.get("status"),
                revision=record.get("revision"),
                decision_code=record.get("decisionCode"),
                event_created_at=record.get("eventCreatedAt"),
                received_at=record.get("receivedAt"),
                expires_at=record.get("expiresAt"),
            )
            if model.to_record() != record:
                raise ValueError
        except (TypeError, ValueError):
            raise StripeStoreError("Stripe webhook receipt is unavailable") from None
        return {
            "scope": scope,
            **{
                key: value
                for key, value in record.items()
                if key not in {"pk", "sk", "itemType", *scope.fields().keys()}
            },
        }

    def retry_ingress(
        self,
        *,
        scope: IntegrationScope,
        outbox_id: str,
        receipt_id: str,
        claimed_revision: int,
        sequence: str,
    ) -> None:
        _identity(scope, "stripe-webhook")
        _identifier(outbox_id)
        _identifier(receipt_id)
        if (
            outbox_id != receipt_id
            or type(claimed_revision) is not int
            or claimed_revision < 2
            or type(sequence) is not str
            or not sequence.isdecimal()
            or len(sequence) > 128
        ):
            raise StripeStoreError("Stripe webhook retry is unavailable")
        values = _serialize_values(
            {
                ":outbox_type": "WebhookIngressOutbox",
                ":receipt_type": "WebhookReceipt",
                ":receipt_id": receipt_id,
                ":processing": "processing",
                ":pending": "pending",
                ":failed": "failed",
                ":retryable": "retryable",
                ":expected": claimed_revision,
                ":next": claimed_revision + 1,
                ":sequence": sequence,
            }
        )
        operations = [
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _serialize(
                        {
                            "pk": scope.partition_key,
                            "sk": f"WEBHOOK_INGRESS_OUTBOX#{outbox_id}",
                        }
                    ),
                    "UpdateExpression": (
                        "SET processingStatus = :pending, processingRevision = :next "
                        "REMOVE processingSequence"
                    ),
                    "ConditionExpression": (
                        "itemType = :outbox_type AND receiptId = :receipt_id AND "
                        "processingStatus = :processing AND "
                        "processingRevision = :expected AND "
                        "processingSequence = :sequence"
                    ),
                    "ExpressionAttributeValues": values,
                }
            },
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _serialize(
                        {
                            "pk": scope.partition_key,
                            "sk": f"WEBHOOK_RECEIPT#{receipt_id}",
                        }
                    ),
                    "UpdateExpression": (
                        "SET #status = :failed, revision = :next, "
                        "decisionCode = :retryable"
                    ),
                    "ConditionExpression": (
                        "itemType = :receipt_type AND #status = :processing AND "
                        "revision = :expected"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": values,
                }
            },
        ]
        try:
            self._client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=hashlib.sha256(
                    (
                        "stripe-worker-retry-v1\0"
                        + scope.partition_key
                        + "\0"
                        + receipt_id
                        + "\0"
                        + str(claimed_revision)
                        + "\0"
                        + sequence
                    ).encode("ascii")
                ).hexdigest()[:36],
            )
        except Exception:
            outbox = self._get(
                scope.partition_key, f"WEBHOOK_INGRESS_OUTBOX#{outbox_id}"
            )
            receipt = self._get(scope.partition_key, f"WEBHOOK_RECEIPT#{receipt_id}")
            if (
                outbox is not None
                and receipt is not None
                and outbox.get("processingStatus") == "pending"
                and outbox.get("processingRevision") == claimed_revision + 1
                and "processingSequence" not in outbox
                and receipt.get("status") == "failed"
                and receipt.get("decisionCode") == "retryable"
                and receipt.get("revision") == claimed_revision + 1
            ):
                return
            raise StripeStoreError("Stripe webhook retry is unavailable") from None

    def plan_subscription_projection(
        self,
        *,
        scope: IntegrationScope,
        subscription_id: str,
        offer_version_id: str,
        status: str,
        current_period_end: int,
        event_id: str,
        event_created_at: int,
        state_hash: str,
    ) -> dict[str, Any]:
        _identity(scope, "stripe-webhook")
        _identifier(subscription_id)
        _identifier(offer_version_id)
        _identifier(event_id)
        _digest(state_hash)
        if (
            status not in {"active", "past_due", "canceled"}
            or type(current_period_end) is not int
            or not 0 <= current_period_end <= 9_999_999_999
            or type(event_created_at) is not int
            or not 0 <= event_created_at <= 9_999_999_999
        ):
            raise StripeStoreError("Stripe subscription projection is unavailable")
        current = self._get(
            scope.partition_key,
            f"STRIPE_SUBSCRIPTION_PROJECTION#{subscription_id}",
        )
        if current is None:
            return {
                "subscriptionId": subscription_id,
                "offerVersionId": offer_version_id,
                "status": status,
                "currentPeriodEnd": current_period_end,
                "sourceRevision": 1,
                "expectedRevision": 0,
                "lastEventId": event_id,
                "lastEventCreatedAt": event_created_at,
                "stateHash": state_hash,
                "stale": False,
            }
        expected_keys = {
            "pk",
            "sk",
            "itemType",
            *scope.fields().keys(),
            "subscriptionId",
            "offerVersionId",
            "status",
            "currentPeriodEnd",
            "sourceRevision",
            "lastEventId",
            "lastEventCreatedAt",
            "stateHash",
        }
        if (
            set(current) != expected_keys
            or current.get("pk") != scope.partition_key
            or current.get("sk") != f"STRIPE_SUBSCRIPTION_PROJECTION#{subscription_id}"
            or current.get("itemType") != "StripeSubscriptionProjection"
            or any(
                current.get(key) != expected for key, expected in scope.fields().items()
            )
            or current.get("subscriptionId") != subscription_id
            or current.get("status") not in {"active", "past_due", "canceled"}
            or type(current.get("sourceRevision")) is not int
            or current["sourceRevision"] < 1
            or type(current.get("lastEventCreatedAt")) is not int
            or type(current.get("lastEventId")) is not str
            or type(current.get("stateHash")) is not str
            or _HASH.fullmatch(current["stateHash"]) is None
        ):
            raise StripeStoreError("Stripe subscription projection is unavailable")
        is_stale = (event_created_at, event_id) <= (
            current["lastEventCreatedAt"],
            current["lastEventId"],
        ) or state_hash == current["stateHash"]
        if is_stale:
            return {
                "subscriptionId": subscription_id,
                "offerVersionId": current["offerVersionId"],
                "status": current["status"],
                "currentPeriodEnd": current["currentPeriodEnd"],
                "sourceRevision": current["sourceRevision"],
                "expectedRevision": current["sourceRevision"],
                "lastEventId": current["lastEventId"],
                "lastEventCreatedAt": current["lastEventCreatedAt"],
                "stateHash": current["stateHash"],
                "stale": True,
            }
        return {
            "subscriptionId": subscription_id,
            "offerVersionId": offer_version_id,
            "status": status,
            "currentPeriodEnd": current_period_end,
            "sourceRevision": current["sourceRevision"] + 1,
            "expectedRevision": current["sourceRevision"],
            "lastEventId": event_id,
            "lastEventCreatedAt": event_created_at,
            "stateHash": state_hash,
            "stale": False,
        }

    def complete_ingress(
        self,
        *,
        scope: IntegrationScope,
        outbox_id: str,
        receipt_id: str,
        claimed_revision: int,
        sequence: str,
        decision_code: str,
        envelopes: list[Mapping[str, Any]],
        projection: Mapping[str, Any] | None,
    ) -> None:
        _identity(scope, "stripe-webhook")
        _identifier(outbox_id)
        _identifier(receipt_id)
        if (
            outbox_id != receipt_id
            or type(claimed_revision) is not int
            or claimed_revision < 2
            or type(sequence) is not str
            or not sequence.isdecimal()
            or decision_code
            not in {
                "processed",
                "ignored_unmapped",
                "ignored_nonterminal",
                "ignored_no_change",
                "needs_review",
            }
            or not isinstance(envelopes, list)
            or len(envelopes) > 2
        ):
            raise StripeStoreError("Stripe webhook completion is unavailable")
        receipt = self._get(scope.partition_key, f"WEBHOOK_RECEIPT#{receipt_id}")
        if (
            not isinstance(receipt, Mapping)
            or receipt.get("status") != "processing"
            or receipt.get("revision") != claimed_revision
        ):
            raise StripeStoreError("Stripe webhook completion is unavailable")
        outgoing = []
        for envelope in envelopes:
            event_id = (
                envelope.get("eventId") if isinstance(envelope, Mapping) else None
            )
            _identifier(event_id)
            outgoing.append(
                IntegrationEventOutbox(
                    scope=scope,
                    outbox_id=event_id,
                    envelope=envelope,
                    payload_hash=canonical_hash(envelope),
                    delivery_status="pending",
                    revision=1,
                    created_at=receipt["receivedAt"],
                    expires_at=receipt["expiresAt"],
                ).to_record()
            )
        projection_operations = []
        if projection is not None:
            projection_keys = {
                "subscriptionId",
                "offerVersionId",
                "status",
                "currentPeriodEnd",
                "sourceRevision",
                "expectedRevision",
                "lastEventId",
                "lastEventCreatedAt",
                "stateHash",
                "stale",
            }
            if (
                not isinstance(projection, Mapping)
                or set(projection) != projection_keys
                or projection.get("stale") is not False
                or projection.get("status") not in {"active", "past_due", "canceled"}
                or type(projection.get("sourceRevision")) is not int
                or type(projection.get("expectedRevision")) is not int
                or projection["sourceRevision"] != projection["expectedRevision"] + 1
                or projection["expectedRevision"] < 0
                or type(projection.get("currentPeriodEnd")) is not int
                or type(projection.get("lastEventCreatedAt")) is not int
            ):
                raise StripeStoreError("Stripe webhook completion is unavailable")
            for field in ("subscriptionId", "offerVersionId", "lastEventId"):
                _identifier(projection.get(field))
            _digest(projection.get("stateHash"))
            matching = [
                envelope
                for envelope in envelopes
                if envelope.get("eventType") == "commerce.subscription.updated.v1"
            ]
            if (
                len(matching) != 1
                or matching[0].get("data", {}).get("subscriptionId")
                != projection["subscriptionId"]
                or matching[0].get("data", {}).get("sourceRevision")
                != projection["sourceRevision"]
            ):
                raise StripeStoreError("Stripe webhook completion is unavailable")
            projection_record = {
                "pk": scope.partition_key,
                "sk": (
                    "STRIPE_SUBSCRIPTION_PROJECTION#" + projection["subscriptionId"]
                ),
                "itemType": "StripeSubscriptionProjection",
                **scope.fields(),
                **{
                    key: value
                    for key, value in projection.items()
                    if key not in {"expectedRevision", "stale"}
                },
            }
            put = {
                "TableName": self._table_name,
                "Item": _serialize(projection_record),
            }
            if projection["expectedRevision"] == 0:
                put["ConditionExpression"] = (
                    "attribute_not_exists(pk) AND attribute_not_exists(sk)"
                )
            else:
                put.update(
                    {
                        "ConditionExpression": (
                            "itemType = :itemType AND sourceRevision = :expected AND "
                            "(lastEventCreatedAt < :created OR "
                            "(lastEventCreatedAt = :created AND lastEventId < :eventId))"
                        ),
                        "ExpressionAttributeValues": _serialize_values(
                            {
                                ":itemType": "StripeSubscriptionProjection",
                                ":expected": projection["expectedRevision"],
                                ":created": projection["lastEventCreatedAt"],
                                ":eventId": projection["lastEventId"],
                            }
                        ),
                    }
                )
            projection_operations.append({"Put": put})
        terminal_status = (
            "processed"
            if decision_code == "processed"
            else "needs_review" if decision_code == "needs_review" else "ignored"
        )
        values = _serialize_values(
            {
                ":outbox_type": "WebhookIngressOutbox",
                ":receipt_type": "WebhookReceipt",
                ":receipt_id": receipt_id,
                ":processing": "processing",
                ":processed": "processed",
                ":terminal": terminal_status,
                ":decision": decision_code,
                ":expected": claimed_revision,
                ":next": claimed_revision + 1,
                ":sequence": sequence,
            }
        )
        operations = [
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _serialize(
                        {
                            "pk": scope.partition_key,
                            "sk": f"WEBHOOK_INGRESS_OUTBOX#{outbox_id}",
                        }
                    ),
                    "UpdateExpression": (
                        "SET processingStatus = :processed, processingRevision = :next"
                    ),
                    "ConditionExpression": (
                        "itemType = :outbox_type AND receiptId = :receipt_id AND "
                        "processingStatus = :processing AND "
                        "processingRevision = :expected AND processingSequence = :sequence"
                    ),
                    "ExpressionAttributeValues": values,
                }
            },
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _serialize(
                        {
                            "pk": scope.partition_key,
                            "sk": f"WEBHOOK_RECEIPT#{receipt_id}",
                        }
                    ),
                    "UpdateExpression": (
                        "SET #status = :terminal, revision = :next, "
                        "decisionCode = :decision"
                    ),
                    "ConditionExpression": (
                        "itemType = :receipt_type AND #status = :processing AND "
                        "revision = :expected"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": values,
                }
            },
            *projection_operations,
            *[
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": _serialize(record),
                        "ConditionExpression": (
                            "attribute_not_exists(pk) AND attribute_not_exists(sk)"
                        ),
                    }
                }
                for record in outgoing
            ],
        ]
        try:
            self._client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=hashlib.sha256(
                    (
                        "stripe-worker-complete-v1\0"
                        + scope.partition_key
                        + "\0"
                        + receipt_id
                        + "\0"
                        + str(claimed_revision)
                        + "\0"
                        + decision_code
                    ).encode("ascii")
                ).hexdigest()[:36],
            )
        except Exception:
            latest = self._get(
                scope.partition_key, f"WEBHOOK_INGRESS_OUTBOX#{outbox_id}"
            )
            if (
                latest is not None
                and latest.get("processingStatus") == "processed"
                and latest.get("processingRevision") == claimed_revision + 1
            ):
                return
            raise StripeStoreError("Stripe webhook completion is unavailable") from None

    def claim_delivery(
        self,
        *,
        scope: IntegrationScope,
        outbox_id: str,
        expected_revision: int,
        sequence: str,
        record: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        _identity(scope, "stripe-webhook")
        _identifier(outbox_id)
        if (
            type(expected_revision) is not int
            or expected_revision < 1
            or type(sequence) is not str
            or not sequence.isdecimal()
            or len(sequence) > 128
        ):
            raise StripeStoreError("Integration event delivery is unavailable")
        try:
            model = IntegrationEventOutbox(
                scope=scope,
                outbox_id=record.get("outboxId"),
                envelope=record.get("eventEnvelope"),
                payload_hash=record.get("payloadHash"),
                delivery_status=record.get("deliveryStatus"),
                revision=record.get("deliveryRevision"),
                created_at=record.get("createdAt"),
                expires_at=record.get("expiresAt"),
            )
            if (
                model.to_record() != record
                or record.get("outboxId") != outbox_id
                or record.get("deliveryStatus") != "pending"
                or record.get("deliveryRevision") != expected_revision
            ):
                raise ValueError
        except (TypeError, ValueError):
            raise StripeStoreError(
                "Integration event delivery is unavailable"
            ) from None
        try:
            response = self._client.update_item(
                TableName=self._table_name,
                Key=_serialize(
                    {
                        "pk": scope.partition_key,
                        "sk": f"INTEGRATION_EVENT_OUTBOX#{outbox_id}",
                    }
                ),
                UpdateExpression=(
                    "SET deliveryStatus = :delivering, deliveryRevision = :next, "
                    "deliverySequence = :sequence"
                ),
                ConditionExpression=(
                    "itemType = :itemType AND deliveryStatus = :pending AND "
                    "deliveryRevision = :expected AND payloadHash = :payloadHash"
                ),
                ExpressionAttributeValues=_serialize_values(
                    {
                        ":itemType": "IntegrationEventOutbox",
                        ":pending": "pending",
                        ":delivering": "delivering",
                        ":expected": expected_revision,
                        ":next": expected_revision + 1,
                        ":sequence": sequence,
                        ":payloadHash": record["payloadHash"],
                    }
                ),
                ReturnValues="ALL_NEW",
            )
            changed = _deserialize(response.get("Attributes"))
        except Exception:
            changed = self._get(
                scope.partition_key, f"INTEGRATION_EVENT_OUTBOX#{outbox_id}"
            )
            if changed is not None and changed.get("deliveryStatus") == "delivered":
                return None
        if (
            not isinstance(changed, Mapping)
            or changed.get("deliveryStatus") != "delivering"
            or changed.get("deliveryRevision") != expected_revision + 1
            or changed.get("deliverySequence") != sequence
            or changed.get("eventEnvelope") != record["eventEnvelope"]
            or changed.get("payloadHash") != record["payloadHash"]
        ):
            raise StripeStoreError("Integration event delivery is unavailable")
        return {
            "deliveryRevision": changed["deliveryRevision"],
            "eventEnvelope": changed["eventEnvelope"],
            "payloadHash": changed["payloadHash"],
        }

    def mark_delivered(
        self,
        *,
        scope: IntegrationScope,
        outbox_id: str,
        claimed_revision: int,
        sequence: str,
        message_id: str,
    ) -> None:
        _identity(scope, "stripe-webhook")
        _identifier(outbox_id)
        if (
            type(claimed_revision) is not int
            or claimed_revision < 2
            or type(sequence) is not str
            or not sequence.isdecimal()
            or type(message_id) is not str
            or not 1 <= len(message_id) <= 256
            or any(
                ord(character) < 33 or ord(character) > 126 for character in message_id
            )
        ):
            raise StripeStoreError("Integration event delivery is unavailable")
        receipt_hash = hashlib.sha256(message_id.encode("ascii")).hexdigest()
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key=_serialize(
                    {
                        "pk": scope.partition_key,
                        "sk": f"INTEGRATION_EVENT_OUTBOX#{outbox_id}",
                    }
                ),
                UpdateExpression=(
                    "SET deliveryStatus = :delivered, deliveryRevision = :next, "
                    "deliveryReceiptHash = :receiptHash"
                ),
                ConditionExpression=(
                    "itemType = :itemType AND deliveryStatus = :delivering AND "
                    "deliveryRevision = :expected AND deliverySequence = :sequence"
                ),
                ExpressionAttributeValues=_serialize_values(
                    {
                        ":itemType": "IntegrationEventOutbox",
                        ":delivering": "delivering",
                        ":delivered": "delivered",
                        ":expected": claimed_revision,
                        ":next": claimed_revision + 1,
                        ":sequence": sequence,
                        ":receiptHash": receipt_hash,
                    }
                ),
            )
        except Exception:
            current = self._get(
                scope.partition_key, f"INTEGRATION_EVENT_OUTBOX#{outbox_id}"
            )
            if (
                current is not None
                and current.get("deliveryStatus") == "delivered"
                and current.get("deliveryReceiptHash") == receipt_hash
            ):
                return
            raise StripeStoreError(
                "Integration event delivery is unavailable"
            ) from None

    def _get(self, pk: str, sk: str) -> dict[str, Any] | None:
        try:
            response = self._client.get_item(
                TableName=self._table_name,
                Key=_serialize({"pk": pk, "sk": sk}),
                ConsistentRead=True,
            )
            item = response.get("Item")
            return None if item is None else _deserialize(item)
        except Exception:
            raise StripeStoreError("Stripe webhook store is unavailable") from None


class DynamoStripeCommandStore:
    def __init__(self, table_name: str, *, client=None):
        if type(table_name) is not str or not table_name.strip():
            raise StripeStoreError("Stripe command store is unavailable")
        if client is None:
            try:
                import boto3  # type: ignore

                client = boto3.client("dynamodb")
            except Exception:
                raise StripeStoreError("Stripe command store is unavailable") from None
        self._table_name = table_name
        self._client = client

    def claim(
        self,
        scope: IntegrationScope,
        connection_id: str,
        key: str,
        request_hash: str,
        command_id: str,
        expires_at: int,
        attempted_at: int,
        operation_claim: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        _identity(scope, connection_id)
        _digest(request_hash)
        _command_identifier(command_id)
        if (
            type(key) is not str
            or not 1 <= len(key) <= 256
            or type(expires_at) is not int
            or type(attempted_at) is not int
            or not 0 <= attempted_at <= 9_999_999_999
        ):
            raise StripeCommandConflict("Stripe command conflicted")
        claim = _operation_claim(operation_claim)
        sk = _receipt_key(connection_id, key)
        existing = self._get(scope.partition_key, sk)
        if existing is not None:
            return _same_receipt(existing, request_hash)
        operation_sk = _operation_key(connection_id, claim)
        current_operation = self._get(scope.partition_key, operation_sk)
        record = {
            "pk": scope.partition_key,
            "sk": sk,
            "itemType": "StripeCommandReceipt",
            "connectionId": connection_id,
            "requestHash": request_hash,
            "commandId": command_id,
            "status": "pending",
            "attemptedAt": attempted_at,
            "operationSk": operation_sk,
            "expiresAt": expires_at,
        }
        operation_record = {
            "pk": scope.partition_key,
            "sk": operation_sk,
            "itemType": "StripeOperationClaim",
            "connectionId": connection_id,
            **claim,
            "requestHash": request_hash,
            "commandId": command_id,
            "status": "pending",
            "attemptedAt": attempted_at,
        }
        if current_operation is None:
            operations = [
                _conditional_put(self._table_name, record),
                _conditional_put(self._table_name, operation_record),
            ]
            replay = None
        else:
            current = _validated_operation_record(
                current_operation, scope.partition_key, connection_id, claim
            )
            exact = (
                current["revision"] == claim["revision"]
                and current["contentHash"] == claim["contentHash"]
            )
            if exact:
                if (
                    current.get("requestHash") != request_hash
                    or current.get("commandId") != command_id
                ):
                    raise StripeCommandConflict("Stripe command conflicted")
                record["status"] = current["status"]
                record["attemptedAt"] = current["attemptedAt"]
                operations = [
                    _conditional_put(self._table_name, record),
                    {
                        "ConditionCheck": {
                            "TableName": self._table_name,
                            "Key": _serialize(
                                {"pk": scope.partition_key, "sk": operation_sk}
                            ),
                            "ConditionExpression": (
                                "#itemType = :itemType AND requestHash = :requestHash "
                                "AND commandId = :commandId AND #status = :status "
                                "AND revision = :revision AND contentHash = :contentHash"
                            ),
                            "ExpressionAttributeNames": {
                                "#itemType": "itemType",
                                "#status": "status",
                            },
                            "ExpressionAttributeValues": _serialize_values(
                                {
                                    ":itemType": "StripeOperationClaim",
                                    ":requestHash": request_hash,
                                    ":commandId": command_id,
                                    ":status": current["status"],
                                    ":revision": claim["revision"],
                                    ":contentHash": claim["contentHash"],
                                }
                            ),
                        }
                    },
                ]
                replay = dict(record)
            elif (
                current["status"] == "accepted"
                and current["revision"] < claim["revision"]
            ):
                operations = [
                    _conditional_put(self._table_name, record),
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": _serialize(
                                {"pk": scope.partition_key, "sk": operation_sk}
                            ),
                            "UpdateExpression": (
                                "SET revision = :revision, contentHash = :contentHash, "
                                "requestHash = :requestHash, commandId = :commandId, "
                                "#status = :pending, attemptedAt = :attemptedAt"
                            ),
                            "ConditionExpression": (
                                "#itemType = :itemType AND #status = :accepted "
                                "AND revision = :expectedRevision "
                                "AND contentHash = :expectedContentHash"
                            ),
                            "ExpressionAttributeNames": {
                                "#itemType": "itemType",
                                "#status": "status",
                            },
                            "ExpressionAttributeValues": _serialize_values(
                                {
                                    ":itemType": "StripeOperationClaim",
                                    ":accepted": "accepted",
                                    ":pending": "pending",
                                    ":expectedRevision": current["revision"],
                                    ":expectedContentHash": current["contentHash"],
                                    ":revision": claim["revision"],
                                    ":contentHash": claim["contentHash"],
                                    ":requestHash": request_hash,
                                    ":commandId": command_id,
                                    ":attemptedAt": attempted_at,
                                }
                            ),
                        }
                    },
                ]
                replay = None
            else:
                raise StripeCommandConflict("Stripe command conflicted")
        try:
            self._client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=hashlib.sha256(
                    ("stripe-command-claim-v1\0" + sk + "\0" + request_hash).encode(
                        "ascii"
                    )
                ).hexdigest()[:36],
            )
            return replay
        except Exception:
            existing = self._get(scope.partition_key, sk)
            if existing is None:
                raise StripeCommandConflict("Stripe command conflicted") from None
            return _same_receipt(existing, request_hash)

    def get_mapping(
        self,
        scope: IntegrationScope,
        connection_id: str,
        resource_type: str,
        resource_id: str,
    ) -> dict[str, Any] | None:
        _identity(scope, connection_id)
        _identifier(resource_type)
        _identifier(resource_id)
        record = self._get(
            scope.partition_key,
            _mapping_key(connection_id, resource_type, resource_id),
        )
        if record is None:
            return None
        expected = {
            "pk",
            "sk",
            "itemType",
            "connectionId",
        }
        if (
            not expected.issubset(record)
            or record["itemType"] != "StripeResourceMapping"
            or record["connectionId"] != connection_id
            or record.get("resourceType") != resource_type
            or record.get("resourceId") != resource_id
        ):
            raise StripeStoreError("Stripe command store is unavailable")
        return {key: value for key, value in record.items() if key not in expected}

    def code_owner(
        self, scope: IntegrationScope, connection_id: str, code_hash: str
    ) -> str | None:
        _identity(scope, connection_id)
        _digest(code_hash)
        record = self._get(
            scope.partition_key, f"STRIPECODE#{connection_id}#{code_hash}"
        )
        if record is None:
            return None
        owner = record.get("resourceId")
        if (
            record.get("itemType") != "StripeDiscountCodeClaim"
            or record.get("connectionId") != connection_id
            or type(owner) is not str
        ):
            raise StripeStoreError("Stripe command store is unavailable")
        return owner

    def object_owner(
        self,
        scope: IntegrationScope,
        connection_id: str,
        object_type: str,
        provider_id: str,
    ) -> dict[str, Any] | None:
        _identity(scope, connection_id)
        field = _PROVIDER_OBJECT_FIELDS.get(object_type)
        if field is None:
            raise StripeCommandConflict("Stripe command conflicted")
        provider_hash = _provider_hash(provider_id)
        record = self._get(
            scope.partition_key,
            _object_key(connection_id, object_type, provider_hash),
        )
        if record is None:
            return None
        resource_type = record.get("resourceType")
        resource_id = record.get("resourceId")
        if (
            record.get("itemType") != "StripeObjectIndex"
            or record.get("connectionId") != connection_id
            or record.get("objectType") != object_type
            or record.get("providerIdHash") != provider_hash
            or type(resource_type) is not str
            or type(resource_id) is not str
        ):
            raise StripeStoreError("Stripe command store is unavailable")
        mapping = self.get_mapping(scope, connection_id, resource_type, resource_id)
        if mapping is None or mapping.get(field) != provider_id:
            raise StripeStoreError("Stripe command store is unavailable")
        return mapping

    def bind_checkout_objects(
        self,
        scope: IntegrationScope,
        connection_id: str,
        mapping: Mapping[str, Any],
        *,
        payment_intent_id: str | None,
        subscription_id: str | None,
    ) -> None:
        _identity(scope, connection_id)
        _safe_mapping(mapping)
        if (
            mapping.get("resourceType") != "checkout"
            or type(mapping.get("sessionId")) is not str
            or (payment_intent_id is None and subscription_id is None)
        ):
            raise ValueError("Stripe command persistence is invalid")
        selected = []
        if payment_intent_id is not None:
            _provider_hash(payment_intent_id)
            selected.append(("payment-intent", "paymentIntentId", payment_intent_id))
        if subscription_id is not None:
            _provider_hash(subscription_id)
            selected.append(("subscription", "providerSubscriptionId", subscription_id))
        names = {
            "#itemType": "itemType",
            "#resourceType": "resourceType",
            "#resourceId": "resourceId",
            "#revision": "revision",
            "#sessionId": "sessionId",
            **{f"#{field}": field for _, field, _ in selected},
        }
        values = {
            ":itemType": "StripeResourceMapping",
            ":resourceType": "checkout",
            ":resourceId": mapping["resourceId"],
            ":revision": mapping["revision"],
            ":sessionId": mapping["sessionId"],
            **{f":{field}": value for _, field, value in selected},
        }
        conditions = [
            "#itemType = :itemType",
            "#resourceType = :resourceType",
            "#resourceId = :resourceId",
            "#revision = :revision",
            "#sessionId = :sessionId",
            *[
                f"(attribute_not_exists(#{field}) OR #{field} = :{field})"
                for _, field, _ in selected
            ],
        ]
        operations = [
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _serialize(
                        {
                            "pk": scope.partition_key,
                            "sk": _mapping_key(
                                connection_id, "checkout", mapping["resourceId"]
                            ),
                        }
                    ),
                    "UpdateExpression": "SET "
                    + ", ".join(f"#{field} = :{field}" for _, field, _ in selected),
                    "ConditionExpression": " AND ".join(conditions),
                    "ExpressionAttributeNames": names,
                    "ExpressionAttributeValues": _serialize_values(values),
                }
            },
            *[
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": _serialize(
                            {
                                "pk": scope.partition_key,
                                "sk": _object_key(
                                    connection_id,
                                    object_type,
                                    _provider_hash(provider_id),
                                ),
                                "itemType": "StripeObjectIndex",
                                "connectionId": connection_id,
                                "objectType": object_type,
                                "providerIdHash": _provider_hash(provider_id),
                                "resourceType": "checkout",
                                "resourceId": mapping["resourceId"],
                            }
                        ),
                        "ConditionExpression": (
                            "attribute_not_exists(pk) OR "
                            "(#resourceType = :resourceType AND #resourceId = :resourceId)"
                        ),
                        "ExpressionAttributeNames": {
                            "#resourceType": "resourceType",
                            "#resourceId": "resourceId",
                        },
                        "ExpressionAttributeValues": _serialize_values(
                            {
                                ":resourceType": "checkout",
                                ":resourceId": mapping["resourceId"],
                            }
                        ),
                    }
                }
                for object_type, _, provider_id in selected
            ],
        ]
        try:
            self._client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=hashlib.sha256(
                    (
                        "stripe-checkout-links-v1\0"
                        + scope.partition_key
                        + "\0"
                        + connection_id
                        + "\0"
                        + mapping["resourceId"]
                        + "\0"
                        + "\0".join(value for _, _, value in selected)
                    ).encode("ascii")
                ).hexdigest()[:36],
            )
        except Exception:
            try:
                owners = [
                    self.object_owner(scope, connection_id, object_type, provider_id)
                    for object_type, _, provider_id in selected
                ]
            except Exception:
                raise StripeStoreError("Stripe command store is unavailable") from None
            if all(
                owner is not None
                and owner.get("resourceType") == "checkout"
                and owner.get("resourceId") == mapping["resourceId"]
                for owner in owners
            ):
                return
            raise StripeStoreError("Stripe command store is unavailable") from None

    def complete(
        self,
        scope: IntegrationScope,
        connection_id: str,
        key: str,
        request_hash: str,
        result: Mapping[str, Any],
        mappings: list[Mapping[str, Any]],
        code_claim: str | None = None,
    ) -> None:
        _identity(scope, connection_id)
        _digest(request_hash)
        if result != {"status": "accepted"} or not 0 <= len(mappings) <= 20:
            raise ValueError("Stripe command persistence is invalid")
        if code_claim is not None and not mappings:
            raise ValueError("Stripe command persistence is invalid")
        receipt = self._get(scope.partition_key, _receipt_key(connection_id, key))
        if receipt is None or receipt.get("requestHash") != request_hash:
            raise StripeCommandConflict("Stripe command conflicted")
        operation_sk = receipt.get("operationSk")
        if type(operation_sk) is not str:
            raise StripeStoreError("Stripe command store is unavailable")
        operations = []
        for mapping in mappings:
            operations.append(self._mapping_write(scope, connection_id, mapping))
            operations.extend(self._object_index_writes(scope, connection_id, mapping))
        if code_claim is not None:
            _digest(code_claim)
            resource_id = mappings[0].get("resourceId")
            _identifier(resource_id)
            operations.append(
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": _serialize(
                            {
                                "pk": scope.partition_key,
                                "sk": f"STRIPECODE#{connection_id}#{code_claim}",
                                "itemType": "StripeDiscountCodeClaim",
                                "connectionId": connection_id,
                                "resourceId": resource_id,
                            }
                        ),
                        "ConditionExpression": (
                            "attribute_not_exists(pk) OR resourceId = :resourceId"
                        ),
                        "ExpressionAttributeValues": {
                            ":resourceId": {"S": resource_id}
                        },
                    }
                }
            )
        else:
            for mapping in mappings:
                code_hash = mapping.get("codeHash")
                if (
                    mapping.get("resourceType") != "discount"
                    or mapping.get("status") == "active"
                    or code_hash is None
                ):
                    continue
                _digest(code_hash)
                resource_id = _identifier(mapping.get("resourceId"))
                operations.append(
                    {
                        "Delete": {
                            "TableName": self._table_name,
                            "Key": _serialize(
                                {
                                    "pk": scope.partition_key,
                                    "sk": f"STRIPECODE#{connection_id}#{code_hash}",
                                }
                            ),
                            "ConditionExpression": "resourceId = :resourceId",
                            "ExpressionAttributeValues": {
                                ":resourceId": {"S": resource_id}
                            },
                        }
                    }
                )
        operations.append(
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _serialize(
                        {
                            "pk": scope.partition_key,
                            "sk": _receipt_key(connection_id, key),
                        }
                    ),
                    "UpdateExpression": "SET #status = :accepted",
                    "ConditionExpression": (
                        "requestHash = :requestHash AND "
                        "#status IN (:pending, :unknown)"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": _serialize_values(
                        {
                            ":accepted": "accepted",
                            ":pending": "pending",
                            ":unknown": "unknown",
                            ":requestHash": request_hash,
                        }
                    ),
                }
            }
        )
        operations.append(
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _serialize({"pk": scope.partition_key, "sk": operation_sk}),
                    "UpdateExpression": "SET #status = :accepted",
                    "ConditionExpression": (
                        "#itemType = :itemType AND requestHash = :requestHash AND "
                        "#status IN (:pending, :unknown)"
                    ),
                    "ExpressionAttributeNames": {
                        "#itemType": "itemType",
                        "#status": "status",
                    },
                    "ExpressionAttributeValues": _serialize_values(
                        {
                            ":itemType": "StripeOperationClaim",
                            ":accepted": "accepted",
                            ":pending": "pending",
                            ":unknown": "unknown",
                            ":requestHash": request_hash,
                        }
                    ),
                }
            }
        )
        if len(operations) > 25:
            raise ValueError("Stripe command persistence is invalid")
        try:
            self._client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=hashlib.sha256(
                    (key + "\0" + request_hash).encode("utf-8")
                ).hexdigest()[:36],
            )
        except Exception:
            receipt = self._get(scope.partition_key, _receipt_key(connection_id, key))
            if (
                receipt is not None
                and receipt.get("requestHash") == request_hash
                and receipt.get("status") == "accepted"
            ):
                return
            raise StripeCommandConflict("Stripe command conflicted") from None

    def mark_unknown(
        self,
        scope: IntegrationScope,
        connection_id: str,
        key: str,
        request_hash: str,
    ) -> None:
        self._transition_command(scope, connection_id, key, request_hash, "unknown")

    def mark_needs_review(
        self,
        scope: IntegrationScope,
        connection_id: str,
        key: str,
        request_hash: str,
    ) -> None:
        self._transition_command(
            scope, connection_id, key, request_hash, "needs_review"
        )

    def mark_rejected(
        self,
        scope: IntegrationScope,
        connection_id: str,
        key: str,
        request_hash: str,
    ) -> None:
        self._transition_command(scope, connection_id, key, request_hash, "rejected")

    def _transition_command(
        self,
        scope: IntegrationScope,
        connection_id: str,
        key: str,
        request_hash: str,
        target: str,
    ) -> None:
        _identity(scope, connection_id)
        _digest(request_hash)
        if target not in {"unknown", "needs_review", "rejected"}:
            raise ValueError("Stripe command persistence is invalid")
        receipt_sk = _receipt_key(connection_id, key)
        receipt = self._get(scope.partition_key, receipt_sk)
        if receipt is None or receipt.get("requestHash") != request_hash:
            raise StripeCommandConflict("Stripe command conflicted")
        operation_sk = receipt.get("operationSk")
        if type(operation_sk) is not str:
            raise StripeStoreError("Stripe command store is unavailable")
        transition_values = {
            ":itemType": "StripeOperationClaim",
            ":target": target,
            ":pending": "pending",
            ":unknown": "unknown",
            ":requestHash": request_hash,
        }
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": _serialize(
                                {"pk": scope.partition_key, "sk": receipt_sk}
                            ),
                            "UpdateExpression": "SET #status = :target",
                            "ConditionExpression": (
                                "requestHash = :requestHash AND "
                                "#status IN (:pending, :unknown)"
                            ),
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": _serialize_values(
                                {
                                    key: value
                                    for key, value in transition_values.items()
                                    if key != ":itemType"
                                }
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": _serialize(
                                {"pk": scope.partition_key, "sk": operation_sk}
                            ),
                            "UpdateExpression": "SET #status = :target",
                            "ConditionExpression": (
                                "#itemType = :itemType AND requestHash = :requestHash "
                                "AND #status IN (:pending, :unknown)"
                            ),
                            "ExpressionAttributeNames": {
                                "#itemType": "itemType",
                                "#status": "status",
                            },
                            "ExpressionAttributeValues": _serialize_values(
                                transition_values
                            ),
                        }
                    },
                ],
                ClientRequestToken=hashlib.sha256(
                    (target + "\0" + receipt_sk + "\0" + request_hash).encode("ascii")
                ).hexdigest()[:36],
            )
        except Exception:
            receipt = self._get(scope.partition_key, receipt_sk)
            operation = self._get(scope.partition_key, operation_sk)
            if (
                receipt is not None
                and operation is not None
                and receipt.get("requestHash") == request_hash
                and operation.get("requestHash") == request_hash
                and receipt.get("status") == target
                and operation.get("status") == target
            ):
                return
            raise StripeStoreError("Stripe command store is unavailable") from None

    def _mapping_write(self, scope, connection_id, mapping):
        _safe_mapping(mapping)
        resource_type = mapping["resourceType"]
        resource_id = mapping["resourceId"]
        sk = _mapping_key(connection_id, resource_type, resource_id)
        existing = self._get(scope.partition_key, sk)
        if existing is None:
            return {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _serialize(
                        {
                            "pk": scope.partition_key,
                            "sk": sk,
                            "itemType": "StripeResourceMapping",
                            "connectionId": connection_id,
                            **dict(mapping),
                        }
                    ),
                    "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)",
                }
            }

        plain_existing = {
            key: value
            for key, value in existing.items()
            if key not in {"pk", "sk", "itemType", "connectionId"}
        }
        dimension, fields = _advanced_dimension(plain_existing, mapping)
        names = {f"#f{index}": field for index, field in enumerate(fields)}
        values = {f":v{index}": mapping[field] for index, field in enumerate(fields)}
        expected = plain_existing.get(dimension)
        hash_field = {
            "revision": "contentHash",
            "presentationRevision": "presentationHash",
            "lifecycleRevision": "lifecycleHash",
        }[dimension]
        expected_hash = plain_existing.get(hash_field)
        values[":expected"] = expected if expected is not None else 0
        if expected is None:
            if expected_hash is not None:
                raise StripeCommandConflict("Stripe command conflicted")
            condition = (
                f"attribute_not_exists({dimension}) AND "
                f"attribute_not_exists({hash_field})"
            )
        else:
            _digest(expected_hash)
            values[":expectedHash"] = expected_hash
            condition = f"{dimension} = :expected AND {hash_field} = :expectedHash"
        return {
            "Update": {
                "TableName": self._table_name,
                "Key": _serialize({"pk": scope.partition_key, "sk": sk}),
                "UpdateExpression": "SET "
                + ", ".join(
                    f"{alias} = :v{index}" for index, alias in enumerate(names)
                ),
                "ConditionExpression": condition,
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": _serialize_values(values),
            }
        }

    def get_subscription_projection(
        self,
        scope: IntegrationScope,
        connection_id: str,
        subscription_id: str,
    ) -> dict[str, Any] | None:
        _identity(scope, connection_id)
        _identifier(subscription_id)
        record = self._get(
            scope.partition_key,
            f"STRIPE_SUBSCRIPTION_PROJECTION#{subscription_id}",
        )
        if record is None:
            return None
        expected_keys = {
            "pk",
            "sk",
            "itemType",
            *scope.fields().keys(),
            "subscriptionId",
            "offerVersionId",
            "status",
            "currentPeriodEnd",
            "sourceRevision",
            "lastEventId",
            "lastEventCreatedAt",
            "stateHash",
        }
        if (
            not isinstance(record, Mapping)
            or set(record) != expected_keys
            or record.get("pk") != scope.partition_key
            or record.get("sk") != f"STRIPE_SUBSCRIPTION_PROJECTION#{subscription_id}"
            or record.get("itemType") != "StripeSubscriptionProjection"
            or any(
                record.get(key) != expected for key, expected in scope.fields().items()
            )
            or record.get("subscriptionId") != subscription_id
            or record.get("status") not in {"active", "past_due", "canceled"}
            or type(record.get("sourceRevision")) is not int
            or record["sourceRevision"] < 1
            or type(record.get("currentPeriodEnd")) is not int
            or record["currentPeriodEnd"] < 0
            or type(record.get("lastEventCreatedAt")) is not int
            or record["lastEventCreatedAt"] < 0
        ):
            raise StripeStoreError("Stripe subscription projection is unavailable")
        _identifier(record.get("offerVersionId"))
        _identifier(record.get("lastEventId"))
        _digest(record.get("stateHash"))
        return {
            "subscriptionId": record["subscriptionId"],
            "offerVersionId": record["offerVersionId"],
            "status": record["status"],
            "sourceRevision": record["sourceRevision"],
        }

    def _object_index_writes(self, scope, connection_id, mapping):
        operations = []
        for object_type, field in _PROVIDER_OBJECT_FIELDS.items():
            if field not in mapping:
                continue
            provider_hash = _provider_hash(mapping[field])
            resource_type = mapping["resourceType"]
            resource_id = mapping["resourceId"]
            operations.append(
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": _serialize(
                            {
                                "pk": scope.partition_key,
                                "sk": _object_key(
                                    connection_id, object_type, provider_hash
                                ),
                                "itemType": "StripeObjectIndex",
                                "connectionId": connection_id,
                                "objectType": object_type,
                                "providerIdHash": provider_hash,
                                "resourceType": resource_type,
                                "resourceId": resource_id,
                            }
                        ),
                        "ConditionExpression": (
                            "attribute_not_exists(pk) OR "
                            "(#resourceType = :resourceType AND #resourceId = :resourceId)"
                        ),
                        "ExpressionAttributeNames": {
                            "#resourceType": "resourceType",
                            "#resourceId": "resourceId",
                        },
                        "ExpressionAttributeValues": _serialize_values(
                            {
                                ":resourceType": resource_type,
                                ":resourceId": resource_id,
                            }
                        ),
                    }
                }
            )
        return operations

    def _get(self, pk: str, sk: str) -> dict[str, Any] | None:
        try:
            response = self._client.get_item(
                TableName=self._table_name,
                Key=_serialize({"pk": pk, "sk": sk}),
                ConsistentRead=True,
            )
            item = response.get("Item")
            return None if item is None else _deserialize(item)
        except StripeStoreError:
            raise
        except Exception:
            raise StripeStoreError("Stripe command store is unavailable") from None


def _mapping_key(connection_id: str, resource_type: str, resource_id: str) -> str:
    return f"STRIPEMAP#{connection_id}#{resource_type}#{resource_id}"


def _same_webhook_receipt(value: object, expected: Mapping[str, Any]) -> bool:
    mutable = {"status", "revision", "decisionCode"}
    return isinstance(value, Mapping) and {
        key: item for key, item in value.items() if key not in mutable
    } == {key: item for key, item in expected.items() if key not in mutable}


def _same_ingress_outbox(value: object, expected: Mapping[str, Any]) -> bool:
    mutable = {
        "processingStatus",
        "processingRevision",
        "attemptCount",
        "processingSequence",
    }
    return isinstance(value, Mapping) and {
        key: item for key, item in value.items() if key not in mutable
    } == {key: item for key, item in expected.items() if key not in mutable}


def _object_key(connection_id: str, object_type: str, provider_hash: str) -> str:
    return f"STRIPEOBJECT#{connection_id}#{object_type}#{provider_hash}"


def _receipt_key(connection_id: str, key: str) -> str:
    return (
        f"STRIPECMD#{connection_id}#{hashlib.sha256(key.encode('utf-8')).hexdigest()}"
    )


def _operation_key(connection_id: str, claim: Mapping[str, Any]) -> str:
    return (
        f"STRIPEOP#{connection_id}#{claim['resourceType']}#"
        f"{claim['resourceId']}#{claim['dimension']}"
    )


def _operation_claim(value: object) -> dict[str, Any]:
    expected = {"resourceType", "resourceId", "dimension", "revision", "contentHash"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise StripeCommandConflict("Stripe command conflicted")
    claim = dict(value)
    _identifier(claim["resourceType"])
    _identifier(claim["resourceId"])
    allowed = {
        "offer": {"immutable", "presentation", "lifecycle"},
        "discount": {"immutable", "presentation", "lifecycle"},
        "checkout": {"immutable"},
        "subscription": {"change", "discount", "pause"},
        "customer-portal": {"immutable"},
    }
    if claim["dimension"] not in allowed.get(claim["resourceType"], set()):
        raise StripeCommandConflict("Stripe command conflicted")
    if (
        type(claim["revision"]) is not int
        or not 1 <= claim["revision"] <= 9_999_999_999
    ):
        raise StripeCommandConflict("Stripe command conflicted")
    _digest(claim["contentHash"])
    return claim


def _validated_operation_record(record, expected_pk, connection_id, expected):
    expected_keys = {
        "pk",
        "sk",
        "itemType",
        "connectionId",
        "resourceType",
        "resourceId",
        "dimension",
        "revision",
        "contentHash",
        "requestHash",
        "commandId",
        "status",
        "attemptedAt",
    }
    if (
        not isinstance(record, Mapping)
        or set(record) != expected_keys
        or record.get("pk") != expected_pk
        or record.get("sk") != _operation_key(connection_id, expected)
        or record.get("itemType") != "StripeOperationClaim"
        or record.get("connectionId") != connection_id
        or any(
            record.get(key) != expected[key]
            for key in ("resourceType", "resourceId", "dimension")
        )
        or type(record.get("revision")) is not int
        or not 1 <= record["revision"] <= 9_999_999_999
        or type(record.get("contentHash")) is not str
        or _HASH.fullmatch(record["contentHash"]) is None
        or type(record.get("requestHash")) is not str
        or _HASH.fullmatch(record["requestHash"]) is None
        or record.get("status")
        not in {"pending", "unknown", "accepted", "needs_review", "rejected"}
        or type(record.get("attemptedAt")) is not int
        or not 0 <= record["attemptedAt"] <= 9_999_999_999
        or type(record.get("commandId")) is not str
        or _COMMAND_ID.fullmatch(record["commandId"]) is None
    ):
        raise StripeStoreError("Stripe command store is unavailable")
    return record


def _conditional_put(table_name: str, record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "Put": {
            "TableName": table_name,
            "Item": _serialize(record),
            "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)",
        }
    }


def _same_receipt(record, request_hash):
    if (
        record.get("itemType") != "StripeCommandReceipt"
        or record.get("requestHash") != request_hash
        or record.get("status")
        not in {"pending", "unknown", "accepted", "needs_review", "rejected"}
        or type(record.get("attemptedAt")) is not int
        or type(record.get("operationSk")) is not str
        or type(record.get("commandId")) is not str
        or _COMMAND_ID.fullmatch(record["commandId"]) is None
    ):
        raise StripeCommandConflict("Stripe command conflicted")
    return record


def _identity(scope, connection_id):
    if type(scope) is not IntegrationScope:
        raise StripeCommandConflict("Stripe command conflicted")
    _identifier(connection_id)


def _identifier(value):
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise StripeCommandConflict("Stripe command conflicted")
    return value


def _command_identifier(value):
    if type(value) is not str or _COMMAND_ID.fullmatch(value) is None:
        raise StripeCommandConflict("Stripe command conflicted")
    return value


def _digest(value):
    if type(value) is not str or _HASH.fullmatch(value) is None:
        raise StripeCommandConflict("Stripe command conflicted")


def _provider_hash(value):
    if (
        type(value) is not str
        or not 1 <= len(value) <= 255
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ValueError("Stripe command persistence is invalid")
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _safe_mapping(value):
    if not isinstance(value, Mapping) or not {
        "resourceType",
        "resourceId",
        "revision",
    }.issubset(value):
        raise ValueError("Stripe command persistence is invalid")
    _identifier(value["resourceType"])
    _identifier(value["resourceId"])
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise ValueError("Stripe command persistence is invalid")
    _reject_sensitive(value)


def _reject_sensitive(value):
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if (
                type(key) is not str
                or key.casefold().replace("_", "") in _FORBIDDEN_KEYS
            ):
                raise ValueError("Stripe command persistence is invalid")
            _reject_sensitive(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_sensitive(nested)


def _advanced_dimension(existing, mapping):
    dimensions = []
    for field in ("revision", "presentationRevision", "lifecycleRevision"):
        if field in mapping and mapping[field] > existing.get(field, 0):
            dimensions.append(field)
    if len(dimensions) != 1:
        raise StripeCommandConflict("Stripe command conflicted")
    dimension = dimensions[0]
    fields_by_dimension = {
        "revision": ("revision",),
        "presentationRevision": (
            "presentationRevision",
            "presentationHash",
            *(
                ("displayName", "displayDescription")
                if mapping.get("resourceType") == "discount"
                else ()
            ),
        ),
        "lifecycleRevision": ("lifecycleRevision", "lifecycleHash", "status"),
    }
    fields = fields_by_dimension[dimension]
    if any(field not in mapping for field in fields):
        raise StripeCommandConflict("Stripe command conflicted")
    return dimension, fields


def _serialize_values(values):
    return {
        key: next(iter(_serialize({"value": value}).values()))
        for key, value in values.items()
    }
