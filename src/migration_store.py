"""Durable migration jobs/items plus redacted SQS work admission."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from collections.abc import Mapping
from typing import Any

try:
    from domain.integrations import IntegrationScope, technical_expiry
    from domain.operations import (
        IntegrationEventEnvelope,
        IntegrationEventOutbox,
        canonical_hash,
    )
    from registry import _deserialize, _serialize
    from subscription_migrations import (
        MigrationConflict,
        canonical_migration_snapshot,
        migration_snapshot_hash,
    )
except ModuleNotFoundError:
    from src.domain.integrations import IntegrationScope, technical_expiry
    from src.domain.operations import (
        IntegrationEventEnvelope,
        IntegrationEventOutbox,
        canonical_hash,
    )
    from src.registry import _deserialize, _serialize
    from src.subscription_migrations import (
        MigrationConflict,
        canonical_migration_snapshot,
        migration_snapshot_hash,
    )


_HASH = re.compile(r"[a-f0-9]{64}", re.ASCII)
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}", re.ASCII)
_MIGRATION_ITEM_ID = re.compile(r"migration-item-[a-f0-9]{40}", re.ASCII)
_MAX_PREVIEW_ITEMS = 100_000
_PREVIEW_DIGEST_BITS = 80
_MAX_PREVIEW_DIGEST_SUM = _MAX_PREVIEW_ITEMS * ((1 << _PREVIEW_DIGEST_BITS) - 1)
_QUEUE_KEYS = frozenset(
    {
        "version",
        "environment",
        "tenantId",
        "draftId",
        "domain",
        "connectionId",
        "jobId",
        "action",
        "revision",
    }
)
_JOB_STATES = frozenset(
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
_ITEM_STATES = frozenset(
    {
        "pending",
        "applying",
        "pending_payment",
        "pending_customer_action",
        "pending_update_applied",
        "pending_update_expired",
        "applied",
        "reverted",
        "skipped",
        "retryable_failure",
        "needs_review",
        "permanent_failure",
    }
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
_CANCELLATION_WORK_STATES = (
    "applying",
    "retryable_failure",
    "pending_payment",
    "pending_customer_action",
    "applied",
    "pending",
)


class MigrationStoreError(RuntimeError):
    pass


class MigrationStoreConflict(MigrationConflict, MigrationStoreError):
    pass


class SqsMigrationQueue:
    def __init__(self, queue_url: str, *, client: Any = None):
        if type(queue_url) is not str or not queue_url.startswith("https://"):
            raise MigrationStoreError("migration queue is unavailable")
        if client is None:
            try:
                import boto3  # type: ignore

                client = boto3.client("sqs")
            except Exception:
                raise MigrationStoreError("migration queue is unavailable") from None
        self._queue_url = queue_url
        self._client = client

    def send(self, value: object, *, delay_seconds: int = 0) -> None:
        message = _queue_message(value)
        if type(delay_seconds) is not int or not 0 <= delay_seconds <= 900:
            raise MigrationStoreError("migration queue is unavailable")
        try:
            body = json.dumps(
                message,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            self._client.send_message(
                QueueUrl=self._queue_url,
                MessageBody=body,
                DelaySeconds=delay_seconds,
            )
        except MigrationStoreError:
            raise
        except Exception:
            raise MigrationStoreError("migration queue is unavailable") from None


class DynamoMigrationStore:
    """Business jobs/items in Registry; command/event receipts in technical table."""

    def __init__(
        self,
        registry_table_name: str,
        technical_table_name: str,
        *,
        client: Any = None,
        now_epoch: Any = None,
    ):
        if any(
            type(value) is not str or not value.strip()
            for value in (registry_table_name, technical_table_name)
        ):
            raise MigrationStoreError("migration store is unavailable")
        if client is None:
            try:
                import boto3  # type: ignore

                client = boto3.client("dynamodb")
            except Exception:
                raise MigrationStoreError("migration store is unavailable") from None
        self._registry_table = registry_table_name
        self._technical_table = technical_table_name
        self._client = client
        self._now_epoch = now_epoch or (lambda: int(time.time()))

    def create_preview(self, **value: Any) -> tuple[dict[str, Any], bool]:
        required = {
            "scope",
            "connectionId",
            "jobId",
            "commercialRequestId",
            "sourceOffer",
            "targetOffer",
            "sourcePriceId",
            "targetPriceId",
            "requestedPolicy",
            "candidateScope",
            "canarySize",
            "accountConcurrency",
            "idempotencyKeyHash",
            "requestHash",
            "commandId",
            "createdAt",
        }
        if set(value) != required:
            raise MigrationStoreConflict("migration command conflicted")
        scope = _scope(value["scope"])
        for field in ("connectionId", "jobId", "commercialRequestId", "commandId"):
            _safe_id(value[field])
        for field in ("idempotencyKeyHash", "requestHash"):
            _digest(value[field])
        now_epoch = _epoch(value["createdAt"])
        receipt_sk = _command_key(value["connectionId"], value["idempotencyKeyHash"])
        existing = self._get(self._technical_table, scope.partition_key, receipt_sk)
        if existing is not None:
            return self._preview_replay(existing, value), False
        job = {
            "pk": scope.partition_key,
            "sk": _job_key(value["connectionId"], value["jobId"]),
            "itemType": "SubscriptionMigrationJob",
            **scope.fields(),
            "connectionId": value["connectionId"],
            "jobId": value["jobId"],
            "commercialRequestId": value["commercialRequestId"],
            "sourceOffer": copy.deepcopy(value["sourceOffer"]),
            "targetOffer": copy.deepcopy(value["targetOffer"]),
            "sourcePriceId": value["sourcePriceId"],
            "targetPriceId": value["targetPriceId"],
            "requestedPolicy": copy.deepcopy(value["requestedPolicy"]),
            "candidateScope": copy.deepcopy(value["candidateScope"]),
            "canarySize": value["canarySize"],
            "accountConcurrency": value["accountConcurrency"],
            "state": "previewing",
            "revision": 1,
            "dryRunRevision": None,
            "dryRunHash": None,
            "previewExpiresAt": None,
            "counts": _zero_counts(),
            "previewAggregate": _zero_preview_aggregate(),
            "discoveryCursor": None,
            "taxAuthorization": None,
            "mutationStarted": False,
            "canaryClaims": 0,
            "canaryCompleted": 0,
            "awaitingProviderCount": 0,
            "appliedItemCount": 0,
            "cancellationRemaining": 0,
            "canaryApproved": False,
            "canaryApprovalRequired": False,
            "createdAt": now_epoch,
        }
        receipt = _command_receipt(
            scope,
            value["connectionId"],
            value["idempotencyKeyHash"],
            value["requestHash"],
            value["commandId"],
            value["jobId"],
            result_revision=1,
            created_at=now_epoch,
        )
        try:
            self._client.transact_write_items(
                TransactItems=[
                    _conditional_put(self._registry_table, job),
                    _conditional_put(self._technical_table, receipt),
                ],
                ClientRequestToken=_transaction_token(
                    "migration-preview", receipt_sk, value["requestHash"]
                ),
            )
        except Exception:
            existing = self._get(
                self._technical_table, scope.partition_key, receipt_sk
            )
            if existing is None:
                raise MigrationStoreConflict("migration command conflicted") from None
            return self._preview_replay(existing, value), False
        return _job_value(job), True

    def get_job(self, **value: Any) -> dict[str, Any] | None:
        _closed_keys(
            value, {"scope", "connectionId", "jobId", "commercialRequestId"}
        )
        scope = _scope(value["scope"])
        for field in ("connectionId", "jobId", "commercialRequestId"):
            _safe_id(value[field])
        record = self._get(
            self._registry_table,
            scope.partition_key,
            _job_key(value["connectionId"], value["jobId"]),
        )
        if record is None:
            return None
        return _validated_job(
            record,
            scope,
            value["connectionId"],
            value["jobId"],
            value["commercialRequestId"],
        )

    def schedule_execution(self, **value: Any) -> dict[str, Any] | None:
        required = {
            "scope",
            "connectionId",
            "jobId",
            "commercialRequestId",
            "dryRunRevision",
            "dryRunHash",
            "taxAuthorization",
            "idempotencyKeyHash",
            "requestHash",
            "commandId",
            "nowEpoch",
        }
        _closed_keys(value, required)
        scope = _scope(value["scope"])
        receipt = self._command_replay(scope, value)
        if receipt is not None:
            return {"jobId": value["jobId"], "revision": receipt["resultRevision"]}
        job = self.get_job(
            scope=scope,
            connectionId=value["connectionId"],
            jobId=value["jobId"],
            commercialRequestId=value["commercialRequestId"],
        )
        now_epoch = _epoch(value["nowEpoch"])
        if (
            job is None
            or job["state"] != "awaiting_approval"
            or job["dryRunRevision"] != value["dryRunRevision"]
            or job["dryRunHash"] != value["dryRunHash"]
            or type(job["previewExpiresAt"]) is not int
            or job["previewExpiresAt"] <= now_epoch
        ):
            return None
        authorization = _tax_authorization(value["taxAuthorization"])
        updated = {**job, "state": "scheduled", "revision": job["revision"] + 1, "taxAuthorization": authorization}
        self._write_command_transition(scope, value, job, updated, now_epoch)
        return {"jobId": value["jobId"], "revision": updated["revision"]}

    def control(self, **value: Any) -> dict[str, Any]:
        required = {
            "scope",
            "connectionId",
            "jobId",
            "commercialRequestId",
            "expectedRevision",
            "action",
            "idempotencyKeyHash",
            "requestHash",
            "commandId",
            "nowEpoch",
        }
        _closed_keys(value, required)
        scope = _scope(value["scope"])
        receipt = self._command_replay(scope, value)
        if receipt is not None:
            return {"jobId": value["jobId"], "revision": receipt["resultRevision"]}
        job = self.get_job(
            scope=scope,
            connectionId=value["connectionId"],
            jobId=value["jobId"],
            commercialRequestId=value["commercialRequestId"],
        )
        transitions = {
            ("previewing", "cancel"): "canceled",
            ("awaiting_approval", "cancel"): "canceled",
            ("scheduled", "pause"): "paused",
            ("running", "pause"): "paused",
            ("paused", "resume"): "running",
            ("scheduled", "cancel"): "cancel_requested",
            ("running", "cancel"): "cancel_requested",
            ("paused", "cancel"): "cancel_requested",
        }
        if job is None or job["revision"] != value["expectedRevision"]:
            raise MigrationStoreConflict("migration command conflicted")
        target = transitions.get((job["state"], value["action"]))
        if (
            target is None
            and job["state"] in {"completed", "completed_with_errors"}
            and value["action"] == "cancel"
            and job["requestedPolicy"] == {"mode": "next_renewal"}
        ):
            applied = self._query_work_items(
                scope,
                value["connectionId"],
                value["jobId"],
                "applied",
                limit=1,
            )
            if applied and not _valid_provider_value(applied[0].get("scheduleId")):
                raise MigrationStoreError("migration store is unavailable")
            if applied:
                target = "cancel_requested"
        if target is None:
            raise MigrationStoreConflict("migration command conflicted")
        updated = {**job, "state": target, "revision": job["revision"] + 1}
        if job["state"] == "paused" and value["action"] == "resume":
            if job.get("canaryApprovalRequired") is True:
                updated["canaryApproved"] = True
                updated["canaryApprovalRequired"] = False
        now_epoch = _epoch(value["nowEpoch"])
        self._write_command_transition(
            scope,
            value,
            job,
            updated,
            now_epoch,
            event=(
                (
                    "migration.completed.v1",
                    {"state": "canceled", "counts": updated["counts"]},
                )
                if target == "canceled"
                else None
            ),
        )
        return {"jobId": value["jobId"], "revision": updated["revision"]}

    def status(self, **value: Any) -> dict[str, Any]:
        required = {
            "scope",
            "connectionId",
            "jobId",
            "commercialRequestId",
            "limit",
            "cursor",
        }
        _closed_keys(value, required)
        scope = _scope(value["scope"])
        job = self.get_job(
            scope=scope,
            connectionId=value["connectionId"],
            jobId=value["jobId"],
            commercialRequestId=value["commercialRequestId"],
        )
        if job is None:
            raise MigrationStoreConflict("migration command conflicted")
        limit = value["limit"]
        if type(limit) is not int or not 1 <= limit <= 100:
            raise MigrationStoreConflict("migration command conflicted")
        cursor = value["cursor"]
        if cursor is not None:
            _safe_id(cursor)
        prefix = _item_prefix(value["connectionId"], value["jobId"])
        request = {
            "TableName": self._registry_table,
            "KeyConditionExpression": "pk = :pk AND begins_with(sk, :prefix)",
            "ExpressionAttributeValues": _serialize(
                {":pk": scope.partition_key, ":prefix": prefix}
            ),
            "ConsistentRead": True,
            "Limit": limit,
        }
        if cursor is not None:
            request["ExclusiveStartKey"] = _serialize(
                {
                    "pk": scope.partition_key,
                    "sk": prefix + cursor,
                }
            )
        try:
            response = self._client.query(**request)
            records = [_deserialize(item) for item in response.get("Items", [])]
        except Exception:
            raise MigrationStoreError("migration store is unavailable") from None
        items = [_safe_status_item(record, scope, value) for record in records]
        next_cursor = _cursor(response.get("LastEvaluatedKey"), prefix)
        return {
            "commercialRequestId": job["commercialRequestId"],
            "jobId": job["jobId"],
            "connectionId": job["connectionId"],
            "revision": job["revision"],
            "state": job["state"],
            "dryRunRevision": job["dryRunRevision"],
            "dryRunHash": job["dryRunHash"],
            "expiresAt": job["previewExpiresAt"],
            "counts": copy.deepcopy(job["counts"]),
            "items": items,
            "nextCursor": next_cursor,
        }

    def load_job(
        self, scope: IntegrationScope, connection_id: str, job_id: str
    ) -> dict[str, Any] | None:
        _scope(scope)
        _safe_id(connection_id)
        _safe_id(job_id)
        record = self._get(
            self._registry_table,
            scope.partition_key,
            _job_key(connection_id, job_id),
        )
        if record is None:
            return None
        commercial_request_id = record.get("commercialRequestId")
        _safe_id(commercial_request_id)
        return _validated_job(
            record, scope, connection_id, job_id, commercial_request_id
        )

    def active_migration(
        self,
        scope: IntegrationScope,
        connection_id: str,
        provider_subscription_id: str,
    ) -> dict[str, Any] | None:
        """Resolve the exact draft-scoped migration overlay for one subscription."""

        _scope(scope)
        _safe_id(connection_id)
        _provider_value(provider_subscription_id)
        provider_hash = hashlib.sha256(
            provider_subscription_id.encode("ascii")
        ).hexdigest()
        active = self._get(
            self._registry_table,
            scope.partition_key,
            f"MIGRATION_ACTIVE_SUBSCRIPTION#{connection_id}#{provider_hash}",
        )
        if active is None:
            return None
        _validated_active_membership(active, scope, connection_id, provider_hash)
        membership = self._get(
            self._registry_table,
            scope.partition_key,
            f"MIGRATION_SUBSCRIPTION#{connection_id}#{provider_hash}#{active['jobId']}",
        )
        if (
            not isinstance(membership, Mapping)
            or set(membership)
            != {
                "pk",
                "sk",
                "itemType",
                *scope.fields().keys(),
                "connectionId",
                "jobId",
                "itemId",
                "providerSubscriptionHash",
                "offerVersionIds",
                "primaryOfferVersionId",
            }
            or membership.get("itemType") != "MigrationSubscriptionIndex"
            or membership.get("pk") != scope.partition_key
            or membership.get("connectionId") != connection_id
            or membership.get("jobId") != active["jobId"]
            or membership.get("itemId") != active["itemId"]
            or membership.get("providerSubscriptionHash") != provider_hash
            or membership.get("sk")
            != f"MIGRATION_SUBSCRIPTION#{connection_id}#{provider_hash}#{active['jobId']}"
            or any(
                membership.get(key) != expected
                for key, expected in scope.fields().items()
            )
            or not isinstance(membership.get("offerVersionIds"), list)
            or not 1 <= len(membership["offerVersionIds"]) <= 20
            or len(set(membership["offerVersionIds"]))
            != len(membership["offerVersionIds"])
            or any(
                type(value) is not str or _SAFE_ID.fullmatch(value) is None
                for value in membership["offerVersionIds"]
            )
            or membership.get("primaryOfferVersionId")
            not in membership["offerVersionIds"]
        ):
            raise MigrationStoreError("migration store is unavailable")
        job = self.load_job(scope, connection_id, active["jobId"])
        item = self._load_item(scope, connection_id, active["jobId"], active["itemId"])
        if (
            job is None
            or item is None
            or item.get("providerSubscriptionId") != provider_subscription_id
        ):
            raise MigrationStoreError("migration store is unavailable")
        source_offer = job["sourceOffer"]["offerVersionId"]
        target_offer = job["targetOffer"]["offerVersionId"]
        member_offers = list(membership["offerVersionIds"])
        if source_offer not in member_offers:
            raise MigrationStoreError("migration store is unavailable")
        if job["state"] in {"completed", "completed_with_errors"} and not _is_terminal_item(
            item["state"]
        ):
            raise MigrationStoreError("migration store is unavailable")
        if job["state"] == "canceled" or item["state"] in {"reverted", "skipped"}:
            authorized_offers = member_offers
        elif item["state"] in {
            "pending_update_expired",
            "needs_review",
            "permanent_failure",
        }:
            return None
        elif (
            item["state"] in {"applied", "pending_update_applied"}
            and job["requestedPolicy"] == {"mode": "immediate_prorated"}
        ):
            authorized_offers = [
                value for value in member_offers if value != source_offer
            ] + [target_offer]
        else:
            authorized_offers = list(dict.fromkeys([*member_offers, target_offer]))
        return {
            "jobId": job["jobId"],
            "itemId": item["itemId"],
            "jobState": job["state"],
            "itemState": item["state"],
            "attempts": item["attempts"],
            "sourcePriceId": job["sourcePriceId"],
            "targetPriceId": job["targetPriceId"],
            "sourceOfferVersionId": source_offer,
            "targetOfferVersionId": target_offer,
            "offerVersionIds": authorized_offers,
        }

    def reconcile_migration_webhook(self, **value: Any) -> dict[str, Any] | None:
        _closed_keys(
            value,
            {
                "scope",
                "connectionId",
                "providerSubscriptionId",
                "eventId",
                "eventType",
                "eventCreatedAt",
                "priceIds",
                "pendingUpdate",
            },
        )
        scope = _scope(value["scope"])
        _safe_id(value["connectionId"])
        _provider_value(value["providerSubscriptionId"])
        _safe_id(value["eventId"])
        _epoch(value["eventCreatedAt"])
        if value["eventType"] not in {
            "customer.subscription.pending_update_applied",
            "customer.subscription.pending_update_expired",
        }:
            raise MigrationStoreConflict("migration command conflicted")
        price_ids = value["priceIds"]
        if (
            not isinstance(price_ids, list)
            or not 1 <= len(price_ids) <= 20
            or len(set(price_ids)) != len(price_ids)
            or any(not _valid_provider_value(price) for price in price_ids)
            or type(value["pendingUpdate"]) is not bool
        ):
            raise MigrationStoreConflict("migration command conflicted")
        active = self.active_migration(
            scope, value["connectionId"], value["providerSubscriptionId"]
        )
        if active is None:
            return None
        job = self.load_job(scope, value["connectionId"], active["jobId"])
        item = self._load_item(
            scope, value["connectionId"], active["jobId"], active["itemId"]
        )
        if job is None or item is None:
            raise MigrationStoreError("migration store is unavailable")
        last_event_at = item.get("lastProviderEventCreatedAt")
        if (
            value["eventCreatedAt"] < job["createdAt"]
            or (
                type(last_event_at) is int
                and value["eventCreatedAt"] < last_event_at
            )
        ):
            return {
                "jobId": job["jobId"],
                "revision": job["revision"],
                "state": item["state"],
                "enqueue": False,
            }
        if value["pendingUpdate"]:
            return {
                "jobId": job["jobId"],
                "revision": job["revision"],
                "state": item["state"],
                "enqueue": False,
            }
        target_count = price_ids.count(job["targetPriceId"])
        source_count = price_ids.count(job["sourcePriceId"])
        if job["state"] == "canceling":
            if source_count == 1 and target_count == 0:
                target_state, reason = "reverted", None
            else:
                target_state, reason = "needs_review", "source-drift"
        elif (
            value["eventType"]
            == "customer.subscription.pending_update_applied"
            and target_count == 1
            and source_count == 0
        ):
            target_state, reason = "pending_update_applied", None
        elif (
            value["eventType"]
            == "customer.subscription.pending_update_expired"
            and source_count == 1
            and target_count == 0
            and item["state"] in {"pending_payment", "pending_customer_action"}
        ):
            target_state, reason = "pending_update_expired", "payment-failed"
        elif (
            value["eventType"]
            == "customer.subscription.pending_update_expired"
            and source_count == 1
            and target_count == 0
        ):
            return {
                "jobId": job["jobId"],
                "revision": job["revision"],
                "state": item["state"],
                "enqueue": False,
            }
        else:
            target_state, reason = "needs_review", "source-drift"
        if item["state"] == target_state and item.get("reasonCode") == reason:
            if job["state"] == "canceling":
                self.finalize_cancellation(
                    scope=scope,
                    connectionId=value["connectionId"],
                    jobId=job["jobId"],
                )
                continuation = None
            else:
                continuation = self.continue_execution(
                    scope=scope,
                    connectionId=value["connectionId"],
                    jobId=job["jobId"],
                    nowEpoch=self._now_epoch(),
                )
        elif item["state"] in {
            "applying",
            "retryable_failure",
            "pending_payment",
            "pending_customer_action",
            "pending_update_applied",
            "pending_update_expired",
            "applied",
        }:
            self._finish_item(
                {
                    "scope": scope,
                    "connectionId": value["connectionId"],
                    "jobId": job["jobId"],
                    "itemId": item["itemId"],
                    "attempts": item["attempts"],
                    "state": target_state,
                    "reasonCode": reason,
                    "scheduleId": item.get("scheduleId"),
                    "lastProviderEventCreatedAt": value["eventCreatedAt"],
                    "lastProviderEventId": value["eventId"],
                }
            )
            if job["state"] == "canceling":
                self.finalize_cancellation(
                    scope=scope,
                    connectionId=value["connectionId"],
                    jobId=job["jobId"],
                )
                continuation = None
            else:
                continuation = self.continue_execution(
                    scope=scope,
                    connectionId=value["connectionId"],
                    jobId=job["jobId"],
                    nowEpoch=self._now_epoch(),
                )
        else:
            return {
                "jobId": job["jobId"],
                "revision": job["revision"],
                "state": item["state"],
                "enqueue": False,
            }
        current = self.load_job(scope, value["connectionId"], job["jobId"])
        if current is None:
            raise MigrationStoreError("migration store is unavailable")
        return {
            "jobId": current["jobId"],
            "revision": current["revision"],
            "state": target_state,
            "enqueue": isinstance(continuation, Mapping),
            "workDelaySeconds": (
                continuation.get("workDelaySeconds", 0)
                if isinstance(continuation, Mapping)
                else 0
            ),
        }

    def bind_migration_subscription(self, **value: Any) -> None:
        required = {
            "scope",
            "connectionId",
            "jobId",
            "itemId",
            "providerSubscriptionId",
            "offerVersionIds",
            "primaryOfferVersionId",
        }
        _closed_keys(value, required)
        scope = _scope(value["scope"])
        for field in ("connectionId", "jobId", "itemId", "primaryOfferVersionId"):
            _safe_id(value[field])
        _provider_value(value["providerSubscriptionId"])
        offers = value["offerVersionIds"]
        if (
            not isinstance(offers, list)
            or not 1 <= len(offers) <= 20
            or len(set(offers)) != len(offers)
            or any(_SAFE_ID.fullmatch(offer) is None for offer in offers)
            or value["primaryOfferVersionId"] not in offers
        ):
            raise MigrationStoreConflict("migration command conflicted")
        provider_hash = hashlib.sha256(
            value["providerSubscriptionId"].encode("ascii")
        ).hexdigest()
        stable_resource_id = "migration-subscription-" + provider_hash[:40]
        content_hash = hashlib.sha256(
            json.dumps(
                {"providerSubscriptionHash": provider_hash},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("ascii")
        ).hexdigest()
        mapping = {
            "pk": scope.partition_key,
            "sk": f"STRIPEMAP#{value['connectionId']}#migration-subscription#{stable_resource_id}",
            "itemType": "StripeResourceMapping",
            "connectionId": value["connectionId"],
            "resourceType": "migration-subscription",
            "resourceId": stable_resource_id,
            "revision": 1,
            "contentHash": content_hash,
            "providerSubscriptionId": value["providerSubscriptionId"],
            "status": "active",
        }
        object_index = {
            "pk": scope.partition_key,
            "sk": f"STRIPEOBJECT#{value['connectionId']}#subscription#{provider_hash}",
            "itemType": "StripeObjectIndex",
            "connectionId": value["connectionId"],
            "objectType": "subscription",
            "providerIdHash": provider_hash,
            "resourceType": "migration-subscription",
            "resourceId": stable_resource_id,
        }
        migration_index = {
            "pk": scope.partition_key,
            "sk": (
                f"MIGRATION_SUBSCRIPTION#{value['connectionId']}#"
                f"{provider_hash}#{value['jobId']}"
            ),
            "itemType": "MigrationSubscriptionIndex",
            **scope.fields(),
            "connectionId": value["connectionId"],
            "jobId": value["jobId"],
            "itemId": value["itemId"],
            "providerSubscriptionHash": provider_hash,
            "offerVersionIds": sorted(offers),
            "primaryOfferVersionId": value["primaryOfferVersionId"],
        }
        active_index = {
            "pk": scope.partition_key,
            "sk": f"MIGRATION_ACTIVE_SUBSCRIPTION#{value['connectionId']}#{provider_hash}",
            "itemType": "ActiveMigrationSubscriptionIndex",
            **scope.fields(),
            "connectionId": value["connectionId"],
            "jobId": value["jobId"],
            "itemId": value["itemId"],
            "providerSubscriptionHash": provider_hash,
        }
        current_owner = self._get(
            self._registry_table, scope.partition_key, object_index["sk"]
        )
        operations = []
        if current_owner is None:
            operations.extend(
                [
                    _conditional_or_exact_mapping_put(self._registry_table, mapping),
                    _conditional_or_exact_owner_put(
                        self._registry_table, object_index
                    ),
                ]
            )
        else:
            _validated_subscription_owner(
                current_owner,
                scope,
                value["connectionId"],
                provider_hash,
                value["providerSubscriptionId"],
                value["jobId"],
                value["itemId"],
                sorted(offers),
                value["primaryOfferVersionId"],
                self,
            )
        operations.append(_conditional_put(self._registry_table, migration_index))
        current_active = self._get(
            self._registry_table, scope.partition_key, active_index["sk"]
        )
        if current_active is None:
            operations.append(_conditional_put(self._registry_table, active_index))
        elif (
            current_active.get("jobId") == value["jobId"]
            and current_active.get("itemId") == value["itemId"]
        ):
            operations.append(
                _conditional_or_exact_owner_put(self._registry_table, active_index)
            )
        else:
            _validated_active_membership(
                current_active, scope, value["connectionId"], provider_hash
            )
            previous_job = self.load_job(
                scope, value["connectionId"], current_active["jobId"]
            )
            if previous_job is None or previous_job["state"] not in {
                "completed",
                "completed_with_errors",
                "canceled",
            }:
                raise MigrationStoreConflict("migration command conflicted")
            operations.append(
                _conditional_active_replacement(
                    self._registry_table, active_index, current_active
                )
            )
        try:
            self._client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=_transaction_token(
                    "migration-import", value["jobId"] + value["itemId"], content_hash
                ),
            )
        except Exception:
            current_membership = self._get(
                self._registry_table, scope.partition_key, migration_index["sk"]
            )
            if (
                current_membership is not None
                and current_membership == migration_index
                and (
                    current_active := self._get(
                        self._registry_table,
                        scope.partition_key,
                        active_index["sk"],
                    )
                )
                == active_index
            ):
                return
            raise MigrationStoreConflict("migration command conflicted") from None

    def put_preview_item(self, **value: Any) -> None:
        required = {
            "scope",
            "connectionId",
            "jobId",
            "itemId",
            "state",
            "reasonCode",
            "attempts",
            "providerSubscriptionId",
            "snapshot",
            "snapshotHash",
            "prorationTimestamp",
            "previewAmountMinor",
        }
        _closed_keys(value, required)
        scope = _scope(value["scope"])
        for field in ("connectionId", "jobId", "itemId"):
            _safe_id(value[field])
        _provider_value(value["providerSubscriptionId"])
        if value["state"] not in {"pending", "needs_review"} or value["attempts"] != 0:
            raise MigrationStoreConflict("migration command conflicted")
        if (
            value["reasonCode"] is not None
            and value["reasonCode"] not in _MIGRATION_REASON_CODES
        ):
            raise MigrationStoreConflict("migration command conflicted")
        if value["snapshotHash"] is not None:
            _digest(value["snapshotHash"])
        record = {
            "pk": scope.partition_key,
            "sk": _item_prefix(value["connectionId"], value["jobId"])
            + value["itemId"],
            "itemType": "SubscriptionMigrationItem",
            **scope.fields(),
            **{
                key: copy.deepcopy(nested)
                for key, nested in value.items()
                if key != "scope"
            },
            "scheduleId": None,
            "rollbackGuard": None,
            "leaseExpiresAt": None,
            "nextAttemptAt": None,
            "cancellationAttempts": 0,
            "cancellationNextAttemptAt": None,
            "accountKeyHash": None,
            "accountSlot": None,
            "canary": False,
            "lastProviderEventCreatedAt": None,
            "lastProviderEventId": None,
        }
        record = _with_migration_work_index(record)
        _validated_item(record, scope, value["connectionId"], value["jobId"])
        job = self.load_job(scope, value["connectionId"], value["jobId"])
        if job is None or job["state"] != "previewing":
            raise MigrationStoreConflict("migration command conflicted")
        current = self._get(self._registry_table, scope.partition_key, record["sk"])
        if current is not None:
            try:
                current = _validated_item(
                    current, scope, value["connectionId"], value["jobId"]
                )
            except Exception:
                raise MigrationStoreConflict("migration command conflicted") from None
            if current != record:
                raise MigrationStoreConflict("migration command conflicted")
            return _job_value(current)
        updated_job = {
            **job,
            "previewAggregate": _add_preview_item(
                job["previewAggregate"], record
            ),
        }
        try:
            self._client.transact_write_items(
                TransactItems=[
                    _conditional_put(self._registry_table, record),
                    _conditional_preview_aggregate_transition(
                        self._registry_table, scope, job, updated_job
                    ),
                ],
                ClientRequestToken=_transaction_token(
                    "migration-preview-item",
                    value["jobId"] + value["itemId"],
                    hashlib.sha256(
                        repr((record, job["previewAggregate"])).encode("utf-8")
                    ).hexdigest(),
                ),
            )
        except Exception:
            current = self._get(
                self._registry_table, scope.partition_key, record["sk"]
            )
            try:
                current = _validated_item(
                    current, scope, value["connectionId"], value["jobId"]
                )
            except Exception:
                raise MigrationStoreConflict("migration command conflicted") from None
            if current != record:
                raise MigrationStoreConflict("migration command conflicted")
            return _job_value(current)
        return _job_value(record)

    def reject_preview_item(self, **value: Any) -> dict[str, Any]:
        _closed_keys(
            value,
            {"scope", "connectionId", "jobId", "itemId", "reasonCode"},
        )
        scope = _scope(value["scope"])
        _safe_id(value["connectionId"])
        _safe_id(value["jobId"])
        if not _valid_migration_item_id(value["itemId"]):
            raise MigrationStoreConflict("migration command conflicted")
        if value["reasonCode"] not in _MIGRATION_REASON_CODES:
            raise MigrationStoreConflict("migration command conflicted")
        item = self._load_item(
            scope, value["connectionId"], value["jobId"], value["itemId"]
        )
        job = self.load_job(scope, value["connectionId"], value["jobId"])
        if item is None or job is None or job["state"] != "previewing":
            raise MigrationStoreConflict("migration command conflicted")
        if (
            item["state"] == "needs_review"
            and item["reasonCode"] == value["reasonCode"]
        ):
            return _job_value(item)
        if item["state"] != "pending" or item["attempts"] != 0:
            raise MigrationStoreConflict("migration command conflicted")
        updated = _with_migration_work_index(
            {**item, "state": "needs_review", "reasonCode": value["reasonCode"]}
        )
        updated_job = {
            **job,
            "previewAggregate": _replace_preview_item(
                job["previewAggregate"], item, updated
            ),
        }
        try:
            self._client.transact_write_items(
                TransactItems=[
                    _conditional_item_transition(
                        self._registry_table, item, updated
                    ),
                    _conditional_preview_aggregate_transition(
                        self._registry_table, scope, job, updated_job
                    ),
                ],
                ClientRequestToken=_transaction_token(
                    "migration-preview-reject",
                    value["jobId"] + value["itemId"],
                    hashlib.sha256(
                        repr((item, updated, job["previewAggregate"])).encode(
                            "utf-8"
                        )
                    ).hexdigest(),
                ),
            )
        except Exception:
            current = self._load_item(
                scope, value["connectionId"], value["jobId"], value["itemId"]
            )
            if (
                current is None
                or current["state"] != "needs_review"
                or current["reasonCode"] != value["reasonCode"]
            ):
                raise MigrationStoreConflict("migration command conflicted") from None
            return _job_value(current)
        return _job_value(updated)

    def preview_summary(self, scope, connection_id, job_id):
        job = self.load_job(scope, connection_id, job_id)
        if job is None or job["state"] != "previewing":
            raise MigrationStoreConflict("migration command conflicted")
        return _preview_summary(job)

    def advance_preview(self, **value):
        _closed_keys(
            value,
            {"scope", "connectionId", "jobId", "expectedRevision", "cursor"},
        )
        job = self.load_job(value["scope"], value["connectionId"], value["jobId"])
        if (
            job is None
            or job["state"] != "previewing"
            or job["revision"] != value["expectedRevision"]
            or type(value["cursor"]) is not str
            or not value["cursor"]
            or len(value["cursor"]) > 512
        ):
            raise MigrationStoreConflict("migration command conflicted")
        updated = {
            **job,
            "revision": job["revision"] + 1,
            "discoveryCursor": value["cursor"],
        }
        self._put_job_transition(value["scope"], job, updated)
        return updated

    def complete_preview(self, **value):
        _closed_keys(
            value,
            {
                "scope",
                "connectionId",
                "jobId",
                "expectedRevision",
                "dryRunHash",
                "counts",
                "expiresAt",
            },
        )
        scope = _scope(value["scope"])
        _digest(value["dryRunHash"])
        counts = _migration_counts(value["counts"])
        if counts["applied"] != 0 or counts["failed"] != 0:
            raise MigrationStoreConflict("migration command conflicted")
        expires_at = _epoch(value["expiresAt"])
        job = self.load_job(scope, value["connectionId"], value["jobId"])
        if (
            job is None
            or job["state"] != "previewing"
            or job["revision"] != value["expectedRevision"]
        ):
            raise MigrationStoreConflict("migration command conflicted")
        summary = _preview_summary(job)
        if (
            counts != summary["counts"]
            or value["dryRunHash"] != summary["dryRunHash"]
        ):
            raise MigrationStoreConflict("migration command conflicted")
        revision = job["revision"] + 1
        dry_revision = (job.get("dryRunRevision") or 0) + 1
        updated = {
            **job,
            "state": "awaiting_approval",
            "revision": revision,
            "dryRunRevision": dry_revision,
            "dryRunHash": value["dryRunHash"],
            "previewExpiresAt": expires_at,
            "counts": counts,
            "discoveryCursor": None,
        }
        self._put_job_with_event(
            scope,
            job,
            updated,
            "migration.preview_ready.v1",
            {
                "dryRunRevision": dry_revision,
                "dryRunHash": value["dryRunHash"],
                "expiresAt": expires_at,
                "counts": counts,
            },
        )

    def start_execution(self, **value):
        _closed_keys(
            value, {"scope", "connectionId", "jobId", "expectedRevision"}
        )
        job = self.load_job(value["scope"], value["connectionId"], value["jobId"])
        if (
            job is None
            or job["state"] != "scheduled"
            or job["revision"] != value["expectedRevision"]
        ):
            raise MigrationStoreConflict("migration command conflicted")
        updated = {**job, "state": "running", "revision": job["revision"] + 1}
        self._put_job_with_event(
            value["scope"],
            job,
            updated,
            "migration.progressed.v1",
            {"state": "running", "counts": job["counts"]},
        )
        return updated

    def claim_items(self, **value):
        _closed_keys(
            value,
            {
                "scope",
                "connectionId",
                "jobId",
                "limit",
                "maxAttempts",
                "accountConcurrency",
                "accountKeyHash",
                "nowEpoch",
                "leaseSeconds",
                "expectedJobRevision",
                "canaryLimit",
            },
        )
        scope = _scope(value["scope"])
        now_epoch = _epoch(value["nowEpoch"])
        account_key_hash = _digest(value["accountKeyHash"])
        if (
            type(value["limit"]) is not int
            or not 1 <= value["limit"] <= 25
            or value["maxAttempts"] != 5
            or type(value["accountConcurrency"]) is not int
            or not 1 <= value["accountConcurrency"] <= 5
            or type(value["leaseSeconds"]) is not int
            or not 30 <= value["leaseSeconds"] <= 900
            or type(value["expectedJobRevision"]) is not int
            or value["expectedJobRevision"] < 1
            or type(value["canaryLimit"]) is not int
            or not 0 <= value["canaryLimit"] <= 25
        ):
            raise MigrationStoreConflict("migration command conflicted")
        selected = []
        for item in self._query_claimable_items(
            scope,
            value["connectionId"],
            value["jobId"],
            now_epoch,
            value["limit"],
        ):
            if not _item_is_claimable(item, now_epoch):
                continue
            attempts = item.get("attempts")
            reconcile_only = (
                item.get("state") == "applying"
                and attempts == value["maxAttempts"]
            )
            if (
                type(attempts) is not int
                or attempts > value["maxAttempts"]
                or (attempts == value["maxAttempts"] and not reconcile_only)
            ):
                continue
            claimed_attempt = attempts if reconcile_only else attempts + 1
            lease_expires_at = now_epoch + value["leaseSeconds"]
            updated_base = {
                **item,
                "state": "applying",
                "attempts": claimed_attempt,
                "leaseExpiresAt": lease_expires_at,
                "nextAttemptAt": None,
                "accountKeyHash": account_key_hash,
            }
            claim_sk = _subscription_claim_key(
                item["providerSubscriptionId"]
            )
            claimed = None
            for slot in range(value["accountConcurrency"]):
                current_job = self.load_job(
                    scope, value["connectionId"], value["jobId"]
                )
                if (
                    current_job is None
                    or current_job["state"] != "running"
                    or current_job["revision"] != value["expectedJobRevision"]
                ):
                    break
                if reconcile_only:
                    job_guard = _running_job_check(
                        self._registry_table, scope, current_job
                    )
                elif value["canaryLimit"]:
                    if (
                        current_job["mutationStarted"] is True
                        or current_job["canaryClaims"] >= value["canaryLimit"]
                    ):
                        break
                    next_claims = current_job["canaryClaims"] + 1
                    job_guard = _conditional_canary_transition(
                        self._registry_table,
                        scope,
                        current_job,
                        {
                            **current_job,
                            "canaryClaims": next_claims,
                            "mutationStarted": next_claims >= value["canaryLimit"],
                        },
                    )
                else:
                    if (
                        current_job["mutationStarted"] is not True
                        or current_job["canaryApproved"] is not True
                    ):
                        break
                    job_guard = _running_job_check(
                        self._registry_table, scope, current_job
                    )
                updated = {
                    **updated_base,
                    "accountSlot": slot,
                    "canary": (
                        item["canary"]
                        if reconcile_only
                        else value["canaryLimit"] > 0
                    ),
                }
                updated = _with_migration_work_index(updated)
                claim = {
                    "pk": _account_partition(account_key_hash),
                    "sk": claim_sk,
                    "itemType": "MigrationSubscriptionMutationClaim",
                    "ownerPk": scope.partition_key,
                    "connectionId": value["connectionId"],
                    "jobId": value["jobId"],
                    "itemId": item["itemId"],
                    "attempt": claimed_attempt,
                    "leaseExpiresAt": lease_expires_at,
                }
                slot_record = {
                    "pk": _account_partition(account_key_hash),
                    "sk": _account_slot_key(slot),
                    "itemType": "MigrationAccountMutationSlot",
                    "ownerPk": scope.partition_key,
                    "connectionId": value["connectionId"],
                    "jobId": value["jobId"],
                    "itemId": item["itemId"],
                    "attempt": claimed_attempt,
                    "leaseExpiresAt": lease_expires_at,
                }
                try:
                    self._client.transact_write_items(
                        TransactItems=[
                            _lease_put(self._registry_table, slot_record, now_epoch),
                            _lease_put(
                                self._registry_table,
                                claim,
                                now_epoch,
                                exact_owner=(
                                    scope.partition_key,
                                    value["jobId"],
                                    item["itemId"],
                                ),
                            ),
                            job_guard,
                            _conditional_item_transition(
                                self._registry_table, item, updated
                            ),
                        ],
                        ClientRequestToken=_transaction_token(
                            "migration-item-claim",
                            claim_sk + str(slot),
                            hashlib.sha256(
                                (
                                    value["jobId"]
                                    + item["itemId"]
                                    + str(claimed_attempt)
                                    + (str(lease_expires_at) if reconcile_only else "")
                                ).encode("ascii")
                            ).hexdigest(),
                        ),
                    )
                except Exception:
                    continue
                claimed = {**updated, "reconcileOnly": reconcile_only}
                break
            if claimed is None:
                continue
            selected.append(_job_value(claimed))
            if len(selected) == value["limit"]:
                break
        return selected

    def complete_item(self, **value):
        required = {
            "scope",
            "connectionId",
            "jobId",
            "itemId",
            "attempts",
            "state",
            "reasonCode",
            "scheduleId",
            "rollbackGuard",
        }
        _closed_keys(value, required)
        if value["state"] not in {
            "applied",
            "pending_payment",
            "pending_customer_action",
            "needs_review",
        }:
            raise MigrationStoreConflict("migration command conflicted")
        self._finish_item(value)

    def retry_item(self, **value):
        _closed_keys(
            value,
            {
                "scope",
                "connectionId",
                "jobId",
                "itemId",
                "attempts",
                "reasonCode",
                "nextAttemptAt",
                "scheduleId",
            },
        )
        retry = value["attempts"] < 5
        if value["scheduleId"] is not None:
            _provider_value(value["scheduleId"])
        if retry:
            _epoch(value["nextAttemptAt"])
        elif value["nextAttemptAt"] is not None:
            raise MigrationStoreConflict("migration command conflicted")
        self._finish_item(
            {
                **value,
                "state": "retryable_failure" if retry else "needs_review",
                "scheduleId": value["scheduleId"],
                "nextAttemptAt": value["nextAttemptAt"] if retry else None,
            }
        )
        return retry

    def continue_execution(self, **value):
        _closed_keys(value, {"scope", "connectionId", "jobId", "nowEpoch"})
        scope = _scope(value["scope"])
        now_epoch = _epoch(value["nowEpoch"])
        job = self.load_job(scope, value["connectionId"], value["jobId"])
        if job is None or job["state"] != "running":
            return None
        counts = dict(job["counts"])
        actionable = bool(
            self._query_work_items(
                scope,
                value["connectionId"],
                value["jobId"],
                "pending",
                limit=1,
            )
        )
        wakeups = []
        for state, field in (
            ("retryable_failure", "nextAttemptAt"),
            ("applying", "leaseExpiresAt"),
        ):
            due = self._query_work_items(
                scope,
                value["connectionId"],
                value["jobId"],
                state,
                limit=1,
                due_before=now_epoch,
            )
            if due:
                actionable = True
            future = self._query_work_items(
                scope,
                value["connectionId"],
                value["jobId"],
                state,
                limit=1,
            )
            if future and type(future[0].get(field)) is int:
                wakeups.append(future[0][field])
        unresolved = counts["pending"] > 0
        canary_unfinished = job["canaryCompleted"] < job["canaryClaims"]
        should_pause_for_canary = (
            unresolved
            and job["mutationStarted"] is True
            and job["canaryApproved"] is False
            and not canary_unfinished
        )
        if should_pause_for_canary:
            target_state = "paused"
            event_type = "migration.progressed.v1"
        elif unresolved:
            if actionable:
                return {**job, "workDelaySeconds": 0}
            if not actionable and wakeups:
                delay = max(1, min(900, min(wakeups) - now_epoch))
                return {**job, "workDelaySeconds": delay}
            if job["awaitingProviderCount"] == counts["pending"]:
                return None
            return {**job, "workDelaySeconds": 2}
        else:
            target_state = (
                "completed_with_errors"
                if counts["needsReview"] or counts["failed"]
                else "completed"
            )
            event_type = "migration.completed.v1"
        updated = {
            **job,
            "state": target_state,
            "revision": job["revision"] + 1,
            "counts": counts,
            "canaryApprovalRequired": should_pause_for_canary,
        }
        self._put_job_with_event(
            scope,
            job,
            updated,
            event_type,
            {"state": target_state, "counts": counts},
            now_epoch=now_epoch,
        )
        return None

    def begin_cancel(self, **value):
        _closed_keys(
            value, {"scope", "connectionId", "jobId", "expectedRevision"}
        )
        job = self.load_job(value["scope"], value["connectionId"], value["jobId"])
        if (
            job is None
            or job["state"] != "cancel_requested"
            or job["revision"] != value["expectedRevision"]
        ):
            raise MigrationStoreConflict("migration command conflicted")
        updated = {
            **job,
            "state": "canceling",
            "revision": job["revision"] + 1,
            "cancellationRemaining": (
                job["counts"]["pending"] + job["appliedItemCount"]
            ),
        }
        self._put_job_with_event(
            value["scope"],
            job,
            updated,
            "migration.progressed.v1",
            {"state": "canceling", "counts": job["counts"]},
        )
        return updated

    def cancellation_items(self, **value):
        _closed_keys(value, {"scope", "connectionId", "jobId", "limit"})
        limit = value["limit"]
        if type(limit) is not int or not 1 <= limit <= 25:
            raise MigrationStoreConflict("migration command conflicted")
        selected = []
        for state in _CANCELLATION_WORK_STATES:
            remaining = limit - len(selected)
            if remaining <= 0:
                break
            selected.extend(
                self._query_work_items(
                    value["scope"],
                    value["connectionId"],
                    value["jobId"],
                    state,
                    limit=remaining,
                )
            )
        return [_job_value(record) for record in selected]

    def complete_cancellation_item(self, **value):
        _closed_keys(
            value,
            {
                "scope",
                "connectionId",
                "jobId",
                "itemId",
                "state",
                "reasonCode",
            },
        )
        if value["state"] not in {"reverted", "skipped", "needs_review"}:
            raise MigrationStoreConflict("migration command conflicted")
        if value["state"] == "needs_review":
            if value["reasonCode"] not in _MIGRATION_REASON_CODES:
                raise MigrationStoreConflict("migration command conflicted")
        elif value["reasonCode"] is not None:
            raise MigrationStoreConflict("migration command conflicted")
        item = self._load_item(
            value["scope"], value["connectionId"], value["jobId"], value["itemId"]
        )
        if item is None:
            raise MigrationStoreConflict("migration command conflicted")
        if _is_cancellation_terminal(item["state"]):
            return
        self._finish_item(
            {
                **value,
                "attempts": item["attempts"],
                "scheduleId": item.get("scheduleId"),
                "rollbackGuard": item.get("rollbackGuard"),
                "cancellationAttempts": item["cancellationAttempts"],
                "cancellationNextAttemptAt": None,
            }
        )

    def retry_cancellation_item(self, **value):
        _closed_keys(
            value,
            {
                "scope",
                "connectionId",
                "jobId",
                "itemId",
                "cancellationAttempts",
                "nextAttemptAt",
            },
        )
        scope = _scope(value["scope"])
        attempts = value["cancellationAttempts"]
        if type(attempts) is not int or not 1 <= attempts <= 5:
            raise MigrationStoreConflict("migration command conflicted")
        if attempts < 5:
            _epoch(value["nextAttemptAt"])
        elif value["nextAttemptAt"] is not None:
            raise MigrationStoreConflict("migration command conflicted")
        job = self.load_job(scope, value["connectionId"], value["jobId"])
        item = self._load_item(
            scope, value["connectionId"], value["jobId"], value["itemId"]
        )
        if job is None or job["state"] != "canceling" or item is None:
            raise MigrationStoreConflict("migration command conflicted")
        if _is_cancellation_terminal(item["state"]):
            return False
        current_attempts = item["cancellationAttempts"]
        if current_attempts == attempts:
            if item["cancellationNextAttemptAt"] != value["nextAttemptAt"]:
                raise MigrationStoreConflict("migration command conflicted")
            return attempts < 5
        if current_attempts != attempts - 1:
            raise MigrationStoreConflict("migration command conflicted")
        exhausted = attempts == 5
        self._finish_item(
            {
                "scope": scope,
                "connectionId": value["connectionId"],
                "jobId": value["jobId"],
                "itemId": value["itemId"],
                "attempts": item["attempts"],
                "state": "needs_review" if exhausted else item["state"],
                "reasonCode": "retry-exhausted" if exhausted else item["reasonCode"],
                "scheduleId": item.get("scheduleId"),
                "rollbackGuard": item.get("rollbackGuard"),
                "cancellationAttempts": attempts,
                "cancellationNextAttemptAt": (
                    None if exhausted else value["nextAttemptAt"]
                ),
            }
        )
        return not exhausted

    def finalize_cancellation(self, **value):
        _closed_keys(value, {"scope", "connectionId", "jobId"})
        scope = _scope(value["scope"])
        job = self.load_job(scope, value["connectionId"], value["jobId"])
        if job is None or job["state"] != "canceling":
            return False
        if job["cancellationRemaining"] != 0:
            return False
        if any(
            self._query_work_items(
                scope,
                value["connectionId"],
                value["jobId"],
                state,
                limit=1,
            )
            for state in _CANCELLATION_WORK_STATES
        ):
            return False
        counts = dict(job["counts"])
        updated = {
            **job,
            "state": "canceled",
            "revision": job["revision"] + 1,
            "counts": counts,
        }
        self._put_job_with_event(
            scope,
            job,
            updated,
            "migration.completed.v1",
            {"state": "canceled", "counts": counts},
        )
        return True

    def _finish_item(self, value):
        scope = _scope(value["scope"])
        job = self.load_job(scope, value["connectionId"], value["jobId"])
        item = self._load_item(
            scope, value["connectionId"], value["jobId"], value["itemId"]
        )
        if (
            job is None
            or item is None
            or item.get("state")
            not in {
                "pending",
                "applying",
                "retryable_failure",
                "pending_payment",
                "pending_customer_action",
                "pending_update_applied",
                "pending_update_expired",
                "applied",
            }
            or item.get("attempts") != value["attempts"]
        ):
            raise MigrationStoreConflict("migration command conflicted")
        updated = {
            **item,
            "state": value["state"],
            "reasonCode": value["reasonCode"],
            "scheduleId": value.get("scheduleId"),
            "rollbackGuard": value.get(
                "rollbackGuard", item.get("rollbackGuard")
            ),
            "leaseExpiresAt": None,
            "nextAttemptAt": value.get("nextAttemptAt"),
            "cancellationAttempts": value.get(
                "cancellationAttempts", item["cancellationAttempts"]
            ),
            "cancellationNextAttemptAt": value.get(
                "cancellationNextAttemptAt",
                item["cancellationNextAttemptAt"],
            ),
            "accountKeyHash": None,
            "accountSlot": None,
            "lastProviderEventCreatedAt": value.get(
                "lastProviderEventCreatedAt", item.get("lastProviderEventCreatedAt")
            ),
            "lastProviderEventId": value.get(
                "lastProviderEventId", item.get("lastProviderEventId")
            ),
        }
        updated = _with_migration_work_index(updated)
        _validated_item(
            updated, scope, value["connectionId"], value["jobId"]
        )
        transition = _conditional_item_transition(
            self._registry_table, item, updated
        )
        operations = [transition]
        old_bucket = _count_bucket(item["state"])
        new_bucket = _count_bucket(updated["state"])
        old_waiting = _is_awaiting_provider(item["state"])
        new_waiting = _is_awaiting_provider(updated["state"])
        old_applied = item["state"] in {"applied", "pending_update_applied"}
        new_applied = updated["state"] in {"applied", "pending_update_applied"}
        completed_canary = (
            item["canary"]
            and not _is_terminal_item(item["state"])
            and _is_terminal_item(updated["state"])
        )
        completed_cancellation = (
            job["state"] == "canceling"
            and not _is_cancellation_terminal(item["state"])
            and _is_cancellation_terminal(updated["state"])
        )
        if (
            old_bucket != new_bucket
            or old_waiting != new_waiting
            or old_applied != new_applied
            or completed_canary
            or completed_cancellation
        ):
            counts = dict(job["counts"])
            if old_bucket != new_bucket:
                counts[old_bucket] -= 1
                counts[new_bucket] += 1
            updated_job = {
                **job,
                "counts": counts,
                "awaitingProviderCount": (
                    job["awaitingProviderCount"]
                    + int(new_waiting)
                    - int(old_waiting)
                ),
                "appliedItemCount": (
                    job["appliedItemCount"]
                    + int(new_applied)
                    - int(old_applied)
                ),
                "canaryCompleted": job["canaryCompleted"] + int(completed_canary),
                "cancellationRemaining": (
                    job["cancellationRemaining"] - int(completed_cancellation)
                ),
            }
            operations.append(
                _conditional_job_metrics_transition(
                    self._registry_table, scope, job, updated_job
                )
            )
        account_key_hash = item.get("accountKeyHash")
        account_slot = item.get("accountSlot")
        if (
            type(account_key_hash) is str
            and _HASH.fullmatch(account_key_hash) is not None
            and type(account_slot) is int
            and 0 <= account_slot <= 4
        ):
            owner = {
                "ownerPk": scope.partition_key,
                "jobId": value["jobId"],
                "itemId": value["itemId"],
                "attempt": value["attempts"],
            }
            operations.extend(
                [
                    _lease_delete(
                        self._registry_table,
                        _account_partition(account_key_hash),
                        _subscription_claim_key(item["providerSubscriptionId"]),
                        owner,
                    ),
                    _lease_delete(
                        self._registry_table,
                        _account_partition(account_key_hash),
                        _account_slot_key(account_slot),
                        owner,
                    ),
                ]
            )
        if value["state"] == "needs_review":
            operations.append(
                self._event_put(
                    scope,
                    job,
                    "migration.item_needs_review.v1",
                    {
                        "itemId": value["itemId"],
                        "reasonCode": value["reasonCode"],
                    },
                    suffix=(
                        value["itemId"]
                        + str(value["attempts"])
                        + "-"
                        + str(updated["cancellationAttempts"])
                    ),
                )
            )
        try:
            self._client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=_transaction_token(
                    "migration-item-finish",
                    value["jobId"] + value["itemId"],
                    hashlib.sha256(
                        json.dumps(
                            {
                                "attempt": value["attempts"],
                                "from": item["state"],
                                "to": value["state"],
                                "reason": value.get("reasonCode"),
                                "schedule": value.get("scheduleId"),
                                "rollbackGuard": value.get("rollbackGuard"),
                                "cancellationAttempt": updated[
                                    "cancellationAttempts"
                                ],
                                "cancellationNextAttemptAt": updated[
                                    "cancellationNextAttemptAt"
                                ],
                                "event": value.get("lastProviderEventId"),
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=True,
                        ).encode("ascii")
                    ).hexdigest(),
                ),
            )
        except Exception:
            current = self._load_item(
                scope, value["connectionId"], value["jobId"], value["itemId"]
            )
            if current is None or current.get("state") != value["state"]:
                raise MigrationStoreConflict("migration command conflicted") from None

    def _load_item(self, scope, connection_id, job_id, item_id):
        record = self._get(
            self._registry_table,
            scope.partition_key,
            _item_prefix(connection_id, job_id) + item_id,
        )
        if record is None:
            return None
        return _validated_item(record, scope, connection_id, job_id)

    def _query_claimable_items(
        self, scope, connection_id, job_id, now_epoch, limit
    ):
        selected = []
        seen = set()
        for state in ("applying", "retryable_failure", "pending"):
            remaining = limit - len(selected)
            if remaining <= 0:
                break
            due_before = None if state == "pending" else now_epoch
            for item in self._query_work_items(
                scope,
                connection_id,
                job_id,
                state,
                limit=remaining,
                due_before=due_before,
            ):
                if item["itemId"] in seen or not _item_is_claimable(item, now_epoch):
                    continue
                seen.add(item["itemId"])
                selected.append(item)
        return selected

    def _query_work_items(
        self,
        scope,
        connection_id,
        job_id,
        state,
        *,
        limit,
        due_before=None,
    ):
        scope = _scope(scope)
        if state not in {
            "pending",
            "retryable_failure",
            "applying",
            "pending_payment",
            "pending_customer_action",
            "applied",
        }:
            raise MigrationStoreConflict("migration command conflicted")
        if type(limit) is not int or not 1 <= limit <= 25:
            raise MigrationStoreConflict("migration command conflicted")
        work_pk = (
            f"{scope.partition_key}#MIGRATION_WORK#{connection_id}#{job_id}#{state}"
        )
        values = {":workPk": work_pk}
        request = {
            "TableName": self._registry_table,
            "IndexName": "MigrationWorkIndex",
            "KeyConditionExpression": "migrationWorkPk = :workPk",
            "ExpressionAttributeValues": None,
            "Limit": limit,
            "ScanIndexForward": True,
        }
        if due_before is not None:
            _epoch(due_before)
            request["KeyConditionExpression"] += " AND migrationWorkSk <= :due"
            values[":due"] = f"{due_before:010d}#~"
        request["ExpressionAttributeValues"] = _serialize(values)
        try:
            response = self._client.query(**request)
            records = [_deserialize(item) for item in response.get("Items", [])]
        except Exception:
            raise MigrationStoreError("migration store is unavailable") from None
        if len(records) > limit:
            raise MigrationStoreError("migration store is unavailable")
        return [
            _validated_item(record, scope, connection_id, job_id)
            for record in records
        ]

    def _put_job_transition(self, scope, previous, updated):
        record = {
            "pk": scope.partition_key,
            "sk": _job_key(previous["connectionId"], previous["jobId"]),
            "itemType": "SubscriptionMigrationJob",
            **copy.deepcopy(dict(updated)),
        }
        put = _conditional_job_transition(self._registry_table, previous, record)
        try:
            self._client.transact_write_items(
                TransactItems=[put],
                ClientRequestToken=_transaction_token(
                    "migration-job-transition",
                    previous["jobId"],
                    str(updated["revision"]).zfill(64),
                ),
            )
        except Exception:
            raise MigrationStoreConflict("migration command conflicted") from None

    def _put_job_with_event(
        self, scope, previous, updated, event_type, data, *, now_epoch=None
    ):
        record = {
            "pk": scope.partition_key,
            "sk": _job_key(previous["connectionId"], previous["jobId"]),
            "itemType": "SubscriptionMigrationJob",
            **copy.deepcopy(dict(updated)),
        }
        event_put = self._event_put(
            scope,
            updated,
            event_type,
            data,
            suffix=str(updated["revision"]),
            now_epoch=now_epoch,
        )
        try:
            self._client.transact_write_items(
                TransactItems=[
                    _conditional_job_transition(self._registry_table, previous, record),
                    event_put,
                ],
                ClientRequestToken=_transaction_token(
                    "migration-job-event",
                    updated["jobId"] + event_type,
                    str(updated["revision"]).zfill(64),
                ),
            )
        except Exception:
            raise MigrationStoreConflict("migration command conflicted") from None

    def _event_put(
        self, scope, job, event_type, extra, *, suffix, now_epoch=None
    ):
        event_id = _event_id(job["jobId"], event_type, suffix)
        data = {
            "commercialRequestId": job["commercialRequestId"],
            "jobId": job["jobId"],
            "connectionId": job["connectionId"],
            "revision": job["revision"],
            "dedupeKey": event_id,
            **copy.deepcopy(extra),
        }
        if now_epoch is None:
            try:
                now_epoch = self._now_epoch()
            except Exception:
                raise MigrationStoreError("migration store is unavailable") from None
        now_epoch = _epoch(now_epoch)
        envelope = IntegrationEventEnvelope(
            scope, event_id, event_type, now_epoch, data
        ).to_dict()
        outbox = IntegrationEventOutbox(
            scope=scope,
            outbox_id=event_id,
            envelope=envelope,
            payload_hash=canonical_hash(envelope),
            delivery_status="pending",
            revision=1,
            created_at=now_epoch,
            expires_at=technical_expiry(now_epoch),
        ).to_record()
        return _conditional_put(self._technical_table, outbox)

    def _preview_replay(
        self, receipt: Mapping[str, Any], value: Mapping[str, Any]
    ) -> dict[str, Any]:
        _validate_command_receipt(receipt, value)
        job = self.get_job(
            scope=value["scope"],
            connectionId=value["connectionId"],
            jobId=value["jobId"],
            commercialRequestId=value["commercialRequestId"],
        )
        if job is None:
            raise MigrationStoreConflict("migration command conflicted")
        return {**job, "revision": receipt["resultRevision"]}

    def _command_replay(
        self, scope: IntegrationScope, value: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        for field in (
            "connectionId",
            "jobId",
            "commercialRequestId",
            "commandId",
        ):
            _safe_id(value[field])
        for field in ("idempotencyKeyHash", "requestHash"):
            _digest(value[field])
        receipt = self._get(
            self._technical_table,
            scope.partition_key,
            _command_key(value["connectionId"], value["idempotencyKeyHash"]),
        )
        if receipt is not None:
            _validate_command_receipt(receipt, value)
        return receipt

    def _write_command_transition(
        self,
        scope: IntegrationScope,
        value: Mapping[str, Any],
        previous: Mapping[str, Any],
        updated: Mapping[str, Any],
        now_epoch: int,
        event: tuple[str, dict[str, Any]] | None = None,
    ) -> None:
        record = {
            "pk": scope.partition_key,
            "sk": _job_key(value["connectionId"], value["jobId"]),
            "itemType": "SubscriptionMigrationJob",
            **copy.deepcopy(dict(updated)),
        }
        receipt = _command_receipt(
            scope,
            value["connectionId"],
            value["idempotencyKeyHash"],
            value["requestHash"],
            value["commandId"],
            value["jobId"],
            result_revision=updated["revision"],
            created_at=now_epoch,
        )
        put = _conditional_job_transition(self._registry_table, previous, record)
        operations = [put, _conditional_put(self._technical_table, receipt)]
        if event is not None:
            event_type, data = event
            operations.append(
                self._event_put(
                    scope,
                    updated,
                    event_type,
                    data,
                    suffix=str(updated["revision"]),
                    now_epoch=now_epoch,
                )
            )
        try:
            self._client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=_transaction_token(
                    "migration-command", receipt["sk"], value["requestHash"]
                ),
            )
        except Exception:
            replay = self._command_replay(scope, value)
            if replay is None:
                raise MigrationStoreConflict("migration command conflicted") from None

    def _get(self, table: str, pk: str, sk: str) -> dict[str, Any] | None:
        try:
            response = self._client.get_item(
                TableName=table,
                Key=_serialize({"pk": pk, "sk": sk}),
                ConsistentRead=True,
            )
            raw = response.get("Item") if isinstance(response, Mapping) else None
            return None if raw is None else _deserialize(raw)
        except Exception:
            raise MigrationStoreError("migration store is unavailable") from None


class DynamoMigrationStatusStore:
    """Read-only DynamoDB facade for the status Lambda."""

    def __init__(self, registry_table_name: str, *, client: Any = None):
        if type(registry_table_name) is not str or not registry_table_name.strip():
            raise MigrationStoreError("migration status store is unavailable")
        if client is None:
            try:
                import boto3  # type: ignore

                client = boto3.client("dynamodb")
            except Exception:
                raise MigrationStoreError(
                    "migration status store is unavailable"
                ) from None
        self._registry_table = registry_table_name
        self._client = client

    def status(self, **value: Any) -> dict[str, Any]:
        required = {
            "scope",
            "connectionId",
            "jobId",
            "commercialRequestId",
            "limit",
            "cursor",
        }
        _closed_keys(value, required)
        scope = _scope(value["scope"])
        for field in ("connectionId", "jobId", "commercialRequestId"):
            _safe_id(value[field])
        job_record = self._get(
            scope.partition_key,
            _job_key(value["connectionId"], value["jobId"]),
        )
        if job_record is None:
            raise MigrationStoreConflict("migration command conflicted")
        job = _validated_job(
            job_record,
            scope,
            value["connectionId"],
            value["jobId"],
            value["commercialRequestId"],
        )
        limit = value["limit"]
        if type(limit) is not int or not 1 <= limit <= 100:
            raise MigrationStoreConflict("migration command conflicted")
        cursor = value["cursor"]
        if cursor is not None:
            _safe_id(cursor)
        prefix = _item_prefix(value["connectionId"], value["jobId"])
        request = {
            "TableName": self._registry_table,
            "KeyConditionExpression": "pk = :pk AND begins_with(sk, :prefix)",
            "ExpressionAttributeValues": _serialize(
                {":pk": scope.partition_key, ":prefix": prefix}
            ),
            "ConsistentRead": True,
            "Limit": limit,
        }
        if cursor is not None:
            request["ExclusiveStartKey"] = _serialize(
                {"pk": scope.partition_key, "sk": prefix + cursor}
            )
        try:
            response = self._client.query(**request)
            records = [_deserialize(item) for item in response.get("Items", [])]
        except Exception:
            raise MigrationStoreError("migration status store is unavailable") from None
        return {
            "commercialRequestId": job["commercialRequestId"],
            "jobId": job["jobId"],
            "connectionId": job["connectionId"],
            "revision": job["revision"],
            "state": job["state"],
            "dryRunRevision": job["dryRunRevision"],
            "dryRunHash": job["dryRunHash"],
            "expiresAt": job["previewExpiresAt"],
            "counts": copy.deepcopy(job["counts"]),
            "items": [_safe_status_item(record, scope, value) for record in records],
            "nextCursor": _cursor(response.get("LastEvaluatedKey"), prefix),
        }

    def _get(self, pk: str, sk: str) -> dict[str, Any] | None:
        try:
            response = self._client.get_item(
                TableName=self._registry_table,
                Key=_serialize({"pk": pk, "sk": sk}),
                ConsistentRead=True,
            )
            raw = response.get("Item") if isinstance(response, Mapping) else None
            return None if raw is None else _deserialize(raw)
        except Exception:
            raise MigrationStoreError("migration status store is unavailable") from None


class DynamoOfferReferenceGuard:
    """Allow provider Price archival only after complete zero-reference coverage."""

    def __init__(self, table_name: str, *, client: Any = None):
        if type(table_name) is not str or not table_name.strip():
            raise MigrationStoreError("migration reference guard is unavailable")
        if client is None:
            try:
                import boto3  # type: ignore

                client = boto3.client("dynamodb")
            except Exception:
                raise MigrationStoreError(
                    "migration reference guard is unavailable"
                ) from None
        self._table_name = table_name
        self._client = client

    def can_deactivate(
        self,
        scope: IntegrationScope,
        connection_id: str,
        offer_version_id: str,
        price_id: str,
    ) -> bool:
        del scope, connection_id, offer_version_id, price_id
        # A local enumeration cannot exclude Dashboard/external references racing
        # the check. Keep the provider Price active and gate new sales in Registry.
        return False


def _job_value(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(nested)
        for key, nested in value.items()
        if key not in {"pk", "sk", "itemType"}
    }


def _validated_job(record, scope, connection_id, job_id, commercial_request_id):
    exact_keys = {
        "pk",
        "sk",
        "itemType",
        *scope.fields().keys(),
        "connectionId",
        "jobId",
        "commercialRequestId",
        "sourceOffer",
        "targetOffer",
        "sourcePriceId",
        "targetPriceId",
        "requestedPolicy",
        "candidateScope",
        "canarySize",
        "accountConcurrency",
        "state",
        "revision",
        "dryRunRevision",
        "dryRunHash",
        "previewExpiresAt",
        "counts",
        "previewAggregate",
        "discoveryCursor",
        "taxAuthorization",
        "mutationStarted",
        "canaryClaims",
        "canaryCompleted",
        "awaitingProviderCount",
        "appliedItemCount",
        "cancellationRemaining",
        "canaryApproved",
        "canaryApprovalRequired",
        "createdAt",
    }
    if (
        not isinstance(record, Mapping)
        or set(record) != exact_keys
        or record.get("itemType") != "SubscriptionMigrationJob"
        or record.get("pk") != scope.partition_key
        or record.get("sk") != _job_key(connection_id, job_id)
        or record.get("connectionId") != connection_id
        or record.get("jobId") != job_id
        or record.get("commercialRequestId") != commercial_request_id
        or any(record.get(key) != expected for key, expected in scope.fields().items())
        or record.get("state") not in _JOB_STATES
        or type(record.get("revision")) is not int
        or record["revision"] < 1
        or _valid_offer_snapshot(record.get("sourceOffer")) is not True
        or _valid_offer_snapshot(record.get("targetOffer")) is not True
        or record["sourceOffer"]["offerVersionId"]
        == record["targetOffer"]["offerVersionId"]
        or record["sourceOffer"]["snapshot"]["currency"]
        != record["targetOffer"]["snapshot"]["currency"]
        or record["sourceOffer"]["snapshot"]["recurrence"]
        != record["targetOffer"]["snapshot"]["recurrence"]
        or _valid_provider_value(record.get("sourcePriceId")) is not True
        or _valid_provider_value(record.get("targetPriceId")) is not True
        or record["sourcePriceId"] == record["targetPriceId"]
        or record.get("requestedPolicy")
        not in ({"mode": "next_renewal"}, {"mode": "immediate_prorated"})
        or record.get("candidateScope")
        != {"kind": "all_matching_source_price"}
        or type(record.get("canarySize")) is not int
        or not 1 <= record["canarySize"] <= 25
        or type(record.get("accountConcurrency")) is not int
        or not 1 <= record["accountConcurrency"] <= 5
        or _valid_job_dry_run(record) is not True
        or _valid_job_tax(record) is not True
        or _valid_migration_counts(record.get("counts")) is not True
        or _valid_preview_aggregate(record.get("previewAggregate")) is not True
        or (
            record["state"] in {"awaiting_approval", "scheduled"}
            and record["counts"] != record["previewAggregate"]["counts"]
        )
        or (
            record.get("discoveryCursor") is not None
            and (
                type(record["discoveryCursor"]) is not str
                or not 1 <= len(record["discoveryCursor"]) <= 512
            )
        )
        or (record["state"] != "previewing" and record["discoveryCursor"] is not None)
        or type(record.get("mutationStarted")) is not bool
        or type(record.get("canaryClaims")) is not int
        or not 0 <= record["canaryClaims"] <= record["canarySize"]
        or type(record.get("canaryCompleted")) is not int
        or not 0 <= record["canaryCompleted"] <= record["canaryClaims"]
        or type(record.get("awaitingProviderCount")) is not int
        or not 0 <= record["awaitingProviderCount"] <= record["counts"]["pending"]
        or type(record.get("appliedItemCount")) is not int
        or not 0 <= record["appliedItemCount"] <= record["counts"]["applied"]
        or type(record.get("cancellationRemaining")) is not int
        or not 0 <= record["cancellationRemaining"] <= record["counts"]["total"]
        or (
            record["state"] == "canceling"
            and record["cancellationRemaining"]
            != record["counts"]["pending"] + record["appliedItemCount"]
        )
        or (
            record["state"] != "canceling"
            and record["cancellationRemaining"] != 0
        )
        or (record["state"] == "canceled" and record["appliedItemCount"] != 0)
        or (
            record["state"] in {"completed", "completed_with_errors", "canceled"}
            and record["counts"]["pending"] != 0
        )
        or (
            record["state"] == "completed"
            and (record["counts"]["needsReview"] or record["counts"]["failed"])
        )
        or (
            record["state"] == "completed_with_errors"
            and not (record["counts"]["needsReview"] or record["counts"]["failed"])
        )
        or (record["mutationStarted"] and record["canaryClaims"] == 0)
        or type(record.get("canaryApproved")) is not bool
        or type(record.get("canaryApprovalRequired")) is not bool
        or (record["canaryApproved"] and record["canaryApprovalRequired"])
        or type(record.get("createdAt")) is not int
        or not 1 <= record["createdAt"] <= 9_999_999_999
    ):
        raise MigrationStoreError("migration store is unavailable")
    return _job_value(record)


def _valid_offer_snapshot(value):
    if not isinstance(value, Mapping) or set(value) != {
        "offerVersionId",
        "revision",
        "schemaVersion",
        "snapshot",
        "contentHash",
    }:
        return False
    snapshot = value.get("snapshot")
    if not isinstance(snapshot, Mapping) or set(snapshot) != {
        "schemaVersion",
        "amountMinor",
        "billingScheme",
        "currency",
        "saleType",
        "recurrence",
        "taxBehavior",
    }:
        return False
    recurrence = snapshot.get("recurrence")
    if (
        type(value.get("offerVersionId")) is not str
        or _SAFE_ID.fullmatch(value["offerVersionId"]) is None
        or type(value.get("revision")) is not int
        or value["revision"] < 1
        or value.get("schemaVersion") != 1
        or snapshot.get("schemaVersion") != 1
        or type(snapshot.get("amountMinor")) is not int
        or not 0 <= snapshot["amountMinor"] <= 99_999_999
        or snapshot.get("billingScheme") != "per_unit"
        or type(snapshot.get("currency")) is not str
        or re.fullmatch(r"[A-Z]{3}", snapshot["currency"], re.ASCII) is None
        or snapshot.get("saleType") != "recurring"
        or not isinstance(recurrence, Mapping)
        or set(recurrence) != {"interval", "intervalCount", "usageType"}
        or recurrence.get("interval") not in {"month", "year"}
        or recurrence.get("intervalCount") != 1
        or recurrence.get("usageType") != "licensed"
        or snapshot.get("taxBehavior")
        not in {"exclusive", "inclusive", "unspecified"}
        or type(value.get("contentHash")) is not str
        or _HASH.fullmatch(value["contentHash"]) is None
    ):
        return False
    return canonical_hash(
        {"schemaVersion": 1, "snapshot": copy.deepcopy(dict(snapshot))}
    ) == value["contentHash"]


def _valid_provider_value(value):
    return (
        type(value) is str
        and 1 <= len(value) <= 255
        and all(33 <= ord(character) <= 126 for character in value)
    )


def _valid_migration_counts(value):
    try:
        _migration_counts(value)
        return True
    except Exception:
        return False


def _valid_preview_aggregate(value):
    if not isinstance(value, Mapping) or set(value) != {
        "itemCount",
        "digestA",
        "digestB",
        "counts",
    }:
        return False
    counts = value.get("counts")
    return (
        type(value.get("itemCount")) is int
        and 0 <= value["itemCount"] <= _MAX_PREVIEW_ITEMS
        and type(value.get("digestA")) is int
        and 0 <= value["digestA"] <= _MAX_PREVIEW_DIGEST_SUM
        and type(value.get("digestB")) is int
        and 0 <= value["digestB"] <= _MAX_PREVIEW_DIGEST_SUM
        and _valid_migration_counts(counts)
        and counts["total"] == value["itemCount"]
        and counts["applied"] == 0
        and counts["failed"] == 0
    )


def _valid_job_dry_run(record):
    revision = record.get("dryRunRevision")
    digest = record.get("dryRunHash")
    expires_at = record.get("previewExpiresAt")
    empty = revision is None and digest is None and expires_at is None
    populated = (
        type(revision) is int
        and revision >= 1
        and type(digest) is str
        and _HASH.fullmatch(digest) is not None
        and type(expires_at) is int
        and 1 <= expires_at <= 9_999_999_999
    )
    if not (empty or populated):
        return False
    state = record.get("state")
    if state == "previewing":
        return empty
    if state == "canceled":
        return empty or populated
    return populated


def _valid_job_tax(record):
    value = record.get("taxAuthorization")
    state = record.get("state")
    requires = state in {
        "scheduled",
        "running",
        "paused",
        "cancel_requested",
        "canceling",
        "completed",
        "completed_with_errors",
    }
    if value is None:
        return not requires
    if not isinstance(value, Mapping) or set(value) != {"taxMode", "approvalHash"}:
        return False
    return (
        value.get("taxMode") in {"manual-rate", "stripe-tax"}
        and (
            value.get("approvalHash") is None
            or (
                type(value["approvalHash"]) is str
                and _HASH.fullmatch(value["approvalHash"]) is not None
            )
        )
        and record.get("dryRunRevision") is not None
    )


def _validated_item(record, scope, connection_id, job_id):
    state = record.get("state") if isinstance(record, Mapping) else None
    work_keys = (
        {"migrationWorkPk", "migrationWorkSk"}
        if state
        in {
            "pending",
            "retryable_failure",
            "applying",
            "pending_payment",
            "pending_customer_action",
            "applied",
        }
        else set()
    )
    exact_keys = {
        "pk",
        "sk",
        "itemType",
        *scope.fields().keys(),
        "connectionId",
        "jobId",
        "itemId",
        "state",
        "reasonCode",
        "attempts",
        "providerSubscriptionId",
        "snapshot",
        "snapshotHash",
        "prorationTimestamp",
        "previewAmountMinor",
        "scheduleId",
        "rollbackGuard",
        "leaseExpiresAt",
        "nextAttemptAt",
        "cancellationAttempts",
        "cancellationNextAttemptAt",
        "accountKeyHash",
        "accountSlot",
        "canary",
        "lastProviderEventCreatedAt",
        "lastProviderEventId",
        *work_keys,
    }
    if not isinstance(record, Mapping) or set(record) != exact_keys:
        raise MigrationStoreError("migration store is unavailable")
    item_id = record.get("itemId")
    provider_subscription_id = record.get("providerSubscriptionId")
    try:
        expected_item_id = "migration-item-" + hashlib.sha256(
            provider_subscription_id.encode("ascii")
        ).hexdigest()[:40]
    except Exception:
        expected_item_id = None
    reason = record.get("reasonCode")
    attempts = record.get("attempts")
    cancellation_attempts = record.get("cancellationAttempts")
    cancellation_next_attempt_at = record.get("cancellationNextAttemptAt")
    snapshot = record.get("snapshot")
    snapshot_hash = record.get("snapshotHash")
    try:
        selected_snapshot = (
            None if snapshot is None else canonical_migration_snapshot(snapshot)
        )
    except Exception:
        selected_snapshot = False
    snapshot_valid = (
        isinstance(selected_snapshot, Mapping)
        and type(snapshot_hash) is str
        and _HASH.fullmatch(snapshot_hash) is not None
        and migration_snapshot_hash(selected_snapshot) == snapshot_hash
        and selected_snapshot.get("subscriptionId") == provider_subscription_id
    )
    proration_timestamp = record.get("prorationTimestamp")
    preview_amount = record.get("previewAmountMinor")
    proration_valid = (proration_timestamp is None and preview_amount is None) or (
        type(proration_timestamp) is int
        and 1 <= proration_timestamp <= 9_999_999_999
        and type(preview_amount) is int
        and preview_amount > 0
    )
    applying = state == "applying"
    retrying = state == "retryable_failure"
    last_event_at = record.get("lastProviderEventCreatedAt")
    last_event_id = record.get("lastProviderEventId")
    if (
        record.get("itemType") != "SubscriptionMigrationItem"
        or dict(record) != _with_migration_work_index(record)
        or record.get("pk") != scope.partition_key
        or record.get("sk") != _item_prefix(connection_id, job_id) + str(item_id)
        or any(record.get(key) != expected for key, expected in scope.fields().items())
        or record.get("connectionId") != connection_id
        or record.get("jobId") != job_id
        or item_id != expected_item_id
        or state not in _ITEM_STATES
        or type(record.get("canary")) is not bool
        or type(attempts) is not int
        or not 0 <= attempts <= 5
        or (
            reason is not None and reason not in _MIGRATION_REASON_CODES
        )
        or (
            state
            in {
                "retryable_failure",
                "pending_update_expired",
                "needs_review",
                "permanent_failure",
            }
            and reason is None
        )
        or (
            state
            not in {
                "retryable_failure",
                "pending_update_expired",
                "needs_review",
                "permanent_failure",
            }
            and reason is not None
        )
        or not proration_valid
        or (
            snapshot is None
            and not (state == "needs_review" and attempts == 0 and snapshot_hash is None)
        )
        or (snapshot is not None and not snapshot_valid)
        or (
            record.get("scheduleId") is not None
            and not _valid_provider_value(record["scheduleId"])
        )
        or not _valid_rollback_guard(record.get("rollbackGuard"))
        or (
            record.get("rollbackGuard") is not None
            and record.get("scheduleId") is None
        )
        or (
            state == "applied"
            and record.get("scheduleId") is not None
            and record.get("rollbackGuard") is None
        )
        or type(cancellation_attempts) is not int
        or not 0 <= cancellation_attempts <= 5
        or (
            cancellation_next_attempt_at is not None
            and (
                type(cancellation_next_attempt_at) is not int
                or not 1 <= cancellation_next_attempt_at <= 9_999_999_999
                or not 1 <= cancellation_attempts < 5
                or state not in _CANCELLATION_WORK_STATES
            )
        )
        or (
            cancellation_attempts == 0
            and cancellation_next_attempt_at is not None
        )
        or (
            _is_cancellation_terminal(state)
            and cancellation_next_attempt_at is not None
        )
        or (state == "pending" and attempts != 0)
        or (applying and not 1 <= attempts <= 5)
        or (retrying and not 1 <= attempts < 5)
        or (
            applying
            != (
                type(record.get("leaseExpiresAt")) is int
                and 1 <= record["leaseExpiresAt"] <= 9_999_999_999
                and type(record.get("accountKeyHash")) is str
                and _HASH.fullmatch(record["accountKeyHash"]) is not None
                and type(record.get("accountSlot")) is int
                and 0 <= record["accountSlot"] <= 4
            )
        )
        or (
            not applying
            and any(
                record.get(key) is not None
                for key in ("leaseExpiresAt", "accountKeyHash", "accountSlot")
            )
        )
        or (
            retrying
            != (
                type(record.get("nextAttemptAt")) is int
                and 1 <= record["nextAttemptAt"] <= 9_999_999_999
            )
        )
        or (not retrying and record.get("nextAttemptAt") is not None)
        or ((last_event_at is None) != (last_event_id is None))
        or (
            last_event_at is not None
            and (
                type(last_event_at) is not int
                or not 1 <= last_event_at <= 9_999_999_999
                or type(last_event_id) is not str
                or _SAFE_ID.fullmatch(last_event_id) is None
            )
        )
    ):
        raise MigrationStoreError("migration store is unavailable")
    return dict(record)


def _command_receipt(
    scope,
    connection_id,
    key_hash,
    request_hash,
    command_id,
    job_id,
    *,
    result_revision,
    created_at,
):
    return {
        "pk": scope.partition_key,
        "sk": _command_key(connection_id, key_hash),
        "itemType": "MigrationCommandReceipt",
        **scope.fields(),
        "connectionId": connection_id,
        "keyHash": key_hash,
        "requestHash": request_hash,
        "commandId": command_id,
        "jobId": job_id,
        "resultRevision": result_revision,
        "createdAt": created_at,
        "expiresAt": technical_expiry(created_at),
    }


def _validate_command_receipt(record: Mapping[str, Any], value: Mapping[str, Any]):
    scope = _scope(value.get("scope"))
    expected_keys = {
        "pk",
        "sk",
        "itemType",
        *scope.fields().keys(),
        "connectionId",
        "keyHash",
        "requestHash",
        "commandId",
        "jobId",
        "resultRevision",
        "createdAt",
        "expiresAt",
    }
    if (
        not isinstance(record, Mapping)
        or set(record) != expected_keys
        or record.get("pk") != scope.partition_key
        or record.get("sk")
        != _command_key(value["connectionId"], value["idempotencyKeyHash"])
        or record.get("itemType") != "MigrationCommandReceipt"
        or any(record.get(key) != expected for key, expected in scope.fields().items())
        or record.get("connectionId") != value["connectionId"]
        or record.get("keyHash") != value["idempotencyKeyHash"]
        or record.get("requestHash") != value["requestHash"]
        or record.get("commandId") != value["commandId"]
        or record.get("jobId") != value["jobId"]
        or type(record.get("resultRevision")) is not int
        or not 1 <= record["resultRevision"] <= 9_999_999_999
        or type(record.get("createdAt")) is not int
        or not 1 <= record["createdAt"] <= 9_999_999_999
        or record.get("expiresAt") != technical_expiry(record["createdAt"])
    ):
        raise MigrationStoreConflict("migration command conflicted")
    return record


def _safe_status_item(record, scope, value):
    record = _validated_item(
        record, scope, value["connectionId"], value["jobId"]
    )
    return {
        "itemId": record["itemId"],
        "state": record["state"],
        "reasonCode": record["reasonCode"],
        "attempts": record["attempts"],
    }


def _cursor(raw, prefix):
    if raw is None:
        return None
    try:
        selected = _deserialize(raw)
        sk = selected["sk"]
        cursor = sk[len(prefix) :] if sk.startswith(prefix) else None
        _safe_id(cursor)
        return cursor
    except Exception:
        raise MigrationStoreError("migration store is unavailable") from None


def _tax_authorization(value):
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or value[0] not in {"manual-rate", "stripe-tax"}
        or (value[1] is not None and (type(value[1]) is not str or _HASH.fullmatch(value[1]) is None))
    ):
        raise MigrationStoreConflict("migration command conflicted")
    return {"taxMode": value[0], "approvalHash": value[1]}


def _queue_message(value):
    if not isinstance(value, Mapping) or set(value) != _QUEUE_KEYS or value.get("version") != 1:
        raise MigrationStoreError("migration queue is unavailable")
    try:
        scope = IntegrationScope(
            value["environment"], value["tenantId"], value["draftId"], value["domain"]
        )
    except Exception:
        raise MigrationStoreError("migration queue is unavailable") from None
    for field in ("connectionId", "jobId"):
        _safe_id(value[field])
    if value["action"] not in {"preview", "execute", "control", "reconcile"}:
        raise MigrationStoreError("migration queue is unavailable")
    if type(value["revision"]) is not int or not 1 <= value["revision"] <= 9_999_999_999:
        raise MigrationStoreError("migration queue is unavailable")
    return {"version": 1, **scope.fields(), "connectionId": value["connectionId"], "jobId": value["jobId"], "action": value["action"], "revision": value["revision"]}


def _zero_counts():
    return {"total": 0, "pending": 0, "applied": 0, "needsReview": 0, "failed": 0}


def _zero_preview_aggregate():
    return {"itemCount": 0, "digestA": 0, "digestB": 0, "counts": _zero_counts()}


def _preview_item_commitment(record):
    protected = {
        key: copy.deepcopy(record.get(key))
        for key in (
            "itemId",
            "state",
            "reasonCode",
            "snapshotHash",
            "prorationTimestamp",
            "previewAmountMinor",
        )
    }
    encoded = json.dumps(
        protected,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    digest = hashlib.sha256(encoded).hexdigest()
    width = _PREVIEW_DIGEST_BITS // 4
    return int(digest[:width], 16), int(digest[width : width * 2], 16)


def _add_preview_item(aggregate, record):
    if not _valid_preview_aggregate(aggregate):
        raise MigrationStoreError("migration store is unavailable")
    if aggregate["itemCount"] >= _MAX_PREVIEW_ITEMS:
        raise MigrationStoreConflict("migration command conflicted")
    digest_a, digest_b = _preview_item_commitment(record)
    counts = dict(aggregate["counts"])
    counts["total"] += 1
    counts[_count_bucket(record["state"])] += 1
    selected = {
        "itemCount": aggregate["itemCount"] + 1,
        "digestA": aggregate["digestA"] + digest_a,
        "digestB": aggregate["digestB"] + digest_b,
        "counts": counts,
    }
    if not _valid_preview_aggregate(selected):
        raise MigrationStoreConflict("migration command conflicted")
    return selected


def _replace_preview_item(aggregate, previous, updated):
    if not _valid_preview_aggregate(aggregate):
        raise MigrationStoreError("migration store is unavailable")
    old_a, old_b = _preview_item_commitment(previous)
    new_a, new_b = _preview_item_commitment(updated)
    counts = dict(aggregate["counts"])
    old_bucket = _count_bucket(previous["state"])
    new_bucket = _count_bucket(updated["state"])
    counts[old_bucket] -= 1
    counts[new_bucket] += 1
    selected = {
        "itemCount": aggregate["itemCount"],
        "digestA": aggregate["digestA"] - old_a + new_a,
        "digestB": aggregate["digestB"] - old_b + new_b,
        "counts": counts,
    }
    if not _valid_preview_aggregate(selected):
        raise MigrationStoreConflict("migration command conflicted")
    return selected


def _preview_summary(job):
    aggregate = job.get("previewAggregate")
    if not _valid_preview_aggregate(aggregate):
        raise MigrationStoreError("migration store is unavailable")
    encoded = json.dumps(
        {
            "jobId": job["jobId"],
            "sourceOffer": job["sourceOffer"],
            "targetOffer": job["targetOffer"],
            "requestedPolicy": job["requestedPolicy"],
            "previewAggregate": {
                **aggregate,
                "digestA": str(aggregate["digestA"]),
                "digestB": str(aggregate["digestB"]),
            },
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return {
        "dryRunHash": hashlib.sha256(encoded).hexdigest(),
        "counts": copy.deepcopy(aggregate["counts"]),
    }


def _conditional_put(table, record):
    return {"Put": {"TableName": table, "Item": _serialize(record), "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)"}}


def _conditional_or_exact_mapping_put(table, record):
    return {
        "Put": {
            "TableName": table,
            "Item": _serialize(record),
            "ConditionExpression": (
                "attribute_not_exists(pk) OR "
                "(#resourceType = :resourceType AND #resourceId = :resourceId "
                "AND contentHash = :contentHash)"
            ),
            "ExpressionAttributeNames": {
                "#resourceType": "resourceType",
                "#resourceId": "resourceId",
            },
            "ExpressionAttributeValues": _serialize(
                {
                    ":resourceType": record["resourceType"],
                    ":resourceId": record["resourceId"],
                    ":contentHash": record["contentHash"],
                }
            ),
        }
    }


def _conditional_or_exact_owner_put(table, record):
    identities = {
        key: record[key]
        for key in ("resourceType", "resourceId")
        if key in record
    }
    if identities:
        condition = (
            "attribute_not_exists(pk) OR "
            "(#resourceType = :resourceType AND #resourceId = :resourceId)"
        )
        names = {"#resourceType": "resourceType", "#resourceId": "resourceId"}
        values = _serialize(
            {
                ":resourceType": identities["resourceType"],
                ":resourceId": identities["resourceId"],
            }
        )
    else:
        condition = "attribute_not_exists(pk) OR (jobId = :jobId AND itemId = :itemId)"
        names = None
        values = _serialize({":jobId": record["jobId"], ":itemId": record["itemId"]})
    put = {
        "TableName": table,
        "Item": _serialize(record),
        "ConditionExpression": condition,
        "ExpressionAttributeValues": values,
    }
    if names is not None:
        put["ExpressionAttributeNames"] = names
    return {"Put": put}


def _validated_subscription_owner(
    owner,
    scope,
    connection_id,
    provider_hash,
    provider_subscription_id,
    job_id,
    item_id,
    offer_ids,
    primary_offer_id,
    store,
):
    if (
        not isinstance(owner, Mapping)
        or set(owner)
        != {
            "pk",
            "sk",
            "itemType",
            "connectionId",
            "objectType",
            "providerIdHash",
            "resourceType",
            "resourceId",
        }
        or owner.get("pk") != scope.partition_key
        or owner.get("itemType") != "StripeObjectIndex"
        or owner.get("connectionId") != connection_id
        or owner.get("objectType") != "subscription"
        or owner.get("providerIdHash") != provider_hash
        or owner.get("resourceType") not in {"checkout", "migration-subscription"}
        or type(owner.get("resourceId")) is not str
    ):
        raise MigrationStoreConflict("migration command conflicted")
    stable_resource_id = "migration-subscription-" + provider_hash[:40]
    if (
        owner["resourceType"] == "migration-subscription"
        and owner["resourceId"] != stable_resource_id
    ):
        raise MigrationStoreConflict("migration command conflicted")
    mapping = store._get(
        store._registry_table,
        scope.partition_key,
        f"STRIPEMAP#{connection_id}#{owner['resourceType']}#{owner['resourceId']}",
    )
    if (
        not isinstance(mapping, Mapping)
        or mapping.get("pk") != scope.partition_key
        or mapping.get("itemType") != "StripeResourceMapping"
        or mapping.get("connectionId") != connection_id
        or mapping.get("resourceType") != owner["resourceType"]
        or mapping.get("resourceId") != owner["resourceId"]
        or mapping.get("providerSubscriptionId") != provider_subscription_id
        or (
            owner["resourceType"] == "checkout"
            and (
                sorted(mapping.get("offerVersionIds") or []) != offer_ids
                or mapping.get("primaryOfferVersionId") != primary_offer_id
            )
        )
    ):
        raise MigrationStoreConflict("migration command conflicted")


def _validated_active_membership(record, scope, connection_id, provider_hash):
    expected_keys = {
        "pk",
        "sk",
        "itemType",
        *scope.fields().keys(),
        "connectionId",
        "jobId",
        "itemId",
        "providerSubscriptionHash",
    }
    if (
        not isinstance(record, Mapping)
        or set(record) != expected_keys
        or record.get("pk") != scope.partition_key
        or record.get("sk")
        != f"MIGRATION_ACTIVE_SUBSCRIPTION#{connection_id}#{provider_hash}"
        or record.get("itemType") != "ActiveMigrationSubscriptionIndex"
        or any(record.get(key) != expected for key, expected in scope.fields().items())
        or record.get("connectionId") != connection_id
        or record.get("providerSubscriptionHash") != provider_hash
    ):
        raise MigrationStoreConflict("migration command conflicted")
    _safe_id(record.get("jobId"))
    _safe_id(record.get("itemId"))
    return dict(record)


def _conditional_active_replacement(table, record, previous):
    return {
        "Put": {
            "TableName": table,
            "Item": _serialize(record),
            "ConditionExpression": "jobId = :jobId AND itemId = :itemId",
            "ExpressionAttributeValues": _serialize(
                {":jobId": previous["jobId"], ":itemId": previous["itemId"]}
            ),
        }
    }


def _conditional_job_transition(table, previous, record):
    return {
        "Put": {
            "TableName": table,
            "Item": _serialize(record),
            "ConditionExpression": (
                "revision = :revision AND #state = :state AND #counts = :counts "
                "AND previewAggregate = :previewAggregate "
                "AND mutationStarted = :mutationStarted "
                "AND canaryClaims = :canaryClaims "
                "AND canaryCompleted = :canaryCompleted "
                "AND awaitingProviderCount = :awaitingProviderCount "
                "AND appliedItemCount = :appliedItemCount "
                "AND cancellationRemaining = :cancellationRemaining "
                "AND canaryApproved = :canaryApproved "
                "AND canaryApprovalRequired = :canaryApprovalRequired"
            ),
            "ExpressionAttributeNames": {"#state": "state", "#counts": "counts"},
            "ExpressionAttributeValues": _serialize(
                {
                    ":revision": previous["revision"],
                    ":state": previous["state"],
                    ":counts": previous["counts"],
                    ":previewAggregate": previous["previewAggregate"],
                    ":mutationStarted": previous["mutationStarted"],
                    ":canaryClaims": previous["canaryClaims"],
                    ":canaryCompleted": previous["canaryCompleted"],
                    ":awaitingProviderCount": previous["awaitingProviderCount"],
                    ":appliedItemCount": previous["appliedItemCount"],
                    ":cancellationRemaining": previous["cancellationRemaining"],
                    ":canaryApproved": previous["canaryApproved"],
                    ":canaryApprovalRequired": previous["canaryApprovalRequired"],
                }
            ),
        }
    }


def _conditional_preview_aggregate_transition(table, scope, previous, updated):
    record = {
        "pk": scope.partition_key,
        "sk": _job_key(previous["connectionId"], previous["jobId"]),
        "itemType": "SubscriptionMigrationJob",
        **copy.deepcopy(dict(updated)),
    }
    return {
        "Put": {
            "TableName": table,
            "Item": _serialize(record),
            "ConditionExpression": (
                "#state = :previewing AND revision = :revision "
                "AND previewAggregate = :previewAggregate"
            ),
            "ExpressionAttributeNames": {"#state": "state"},
            "ExpressionAttributeValues": _serialize(
                {
                    ":previewing": "previewing",
                    ":revision": previous["revision"],
                    ":previewAggregate": previous["previewAggregate"],
                }
            ),
        }
    }


def _conditional_job_metrics_transition(table, scope, previous, updated):
    record = {
        "pk": scope.partition_key,
        "sk": _job_key(previous["connectionId"], previous["jobId"]),
        "itemType": "SubscriptionMigrationJob",
        **copy.deepcopy(dict(updated)),
    }
    return {
        "Put": {
            "TableName": table,
            "Item": _serialize(record),
            "ConditionExpression": (
                "#state = :state AND revision = :revision AND #counts = :counts "
                "AND canaryCompleted = :canaryCompleted "
                "AND awaitingProviderCount = :awaitingProviderCount "
                "AND appliedItemCount = :appliedItemCount "
                "AND cancellationRemaining = :cancellationRemaining "
                "AND previewAggregate = :previewAggregate "
                "AND mutationStarted = :mutationStarted "
                "AND canaryClaims = :canaryClaims "
                "AND canaryApproved = :canaryApproved "
                "AND canaryApprovalRequired = :canaryApprovalRequired"
            ),
            "ExpressionAttributeNames": {"#state": "state", "#counts": "counts"},
            "ExpressionAttributeValues": _serialize(
                {
                    ":state": previous["state"],
                    ":revision": previous["revision"],
                    ":counts": previous["counts"],
                    ":canaryCompleted": previous["canaryCompleted"],
                    ":awaitingProviderCount": previous["awaitingProviderCount"],
                    ":appliedItemCount": previous["appliedItemCount"],
                    ":cancellationRemaining": previous["cancellationRemaining"],
                    ":previewAggregate": previous["previewAggregate"],
                    ":mutationStarted": previous["mutationStarted"],
                    ":canaryClaims": previous["canaryClaims"],
                    ":canaryApproved": previous["canaryApproved"],
                    ":canaryApprovalRequired": previous[
                        "canaryApprovalRequired"
                    ],
                }
            ),
        }
    }


def _conditional_item_transition(table, previous, updated):
    return {
        "Put": {
            "TableName": table,
            "Item": _serialize(updated),
            "ConditionExpression": (
                "#state = :state AND attempts = :attempts "
                "AND cancellationAttempts = :cancellationAttempts "
                "AND cancellationNextAttemptAt = :cancellationNextAttemptAt "
                "AND rollbackGuard = :rollbackGuard"
            ),
            "ExpressionAttributeNames": {"#state": "state"},
            "ExpressionAttributeValues": _serialize(
                {
                    ":state": previous["state"],
                    ":attempts": previous["attempts"],
                    ":cancellationAttempts": previous["cancellationAttempts"],
                    ":cancellationNextAttemptAt": previous[
                        "cancellationNextAttemptAt"
                    ],
                    ":rollbackGuard": previous["rollbackGuard"],
                }
            ),
        }
    }


def _running_job_check(table, scope, job):
    return {
        "ConditionCheck": {
            "TableName": table,
            "Key": _serialize(
                {
                    "pk": scope.partition_key,
                    "sk": _job_key(job["connectionId"], job["jobId"]),
                }
            ),
            "ConditionExpression": (
                "#state = :running AND revision = :revision "
                "AND mutationStarted = :started AND canaryApproved = :approved"
            ),
            "ExpressionAttributeNames": {"#state": "state"},
            "ExpressionAttributeValues": _serialize(
                {
                    ":running": "running",
                    ":revision": job["revision"],
                    ":started": True,
                    ":approved": True,
                }
            ),
        }
    }


def _conditional_canary_transition(table, scope, previous, updated):
    record = {
        "pk": scope.partition_key,
        "sk": _job_key(previous["connectionId"], previous["jobId"]),
        "itemType": "SubscriptionMigrationJob",
        **copy.deepcopy(updated),
    }
    return {
        "Put": {
            "TableName": table,
            "Item": _serialize(record),
            "ConditionExpression": (
                "#state = :running AND revision = :revision "
                "AND canaryClaims = :claims AND mutationStarted = :started "
                "AND #counts = :counts AND previewAggregate = :previewAggregate "
                "AND canaryCompleted = :canaryCompleted "
                "AND awaitingProviderCount = :awaitingProviderCount "
                "AND appliedItemCount = :appliedItemCount "
                "AND cancellationRemaining = :cancellationRemaining "
                "AND canaryApproved = :canaryApproved "
                "AND canaryApprovalRequired = :canaryApprovalRequired"
            ),
            "ExpressionAttributeNames": {"#state": "state", "#counts": "counts"},
            "ExpressionAttributeValues": _serialize(
                {
                    ":running": "running",
                    ":revision": previous["revision"],
                    ":claims": previous["canaryClaims"],
                    ":started": False,
                    ":counts": previous["counts"],
                    ":previewAggregate": previous["previewAggregate"],
                    ":canaryCompleted": previous["canaryCompleted"],
                    ":awaitingProviderCount": previous["awaitingProviderCount"],
                    ":appliedItemCount": previous["appliedItemCount"],
                    ":cancellationRemaining": previous["cancellationRemaining"],
                    ":canaryApproved": previous["canaryApproved"],
                    ":canaryApprovalRequired": previous[
                        "canaryApprovalRequired"
                    ],
                }
            ),
        }
    }


def _lease_put(table, record, now_epoch, *, exact_owner=None):
    condition = "attribute_not_exists(pk) OR leaseExpiresAt <= :now"
    names = None
    values = {":now": now_epoch}
    if exact_owner is not None:
        owner_pk, job_id, item_id = exact_owner
        condition = (
            "attribute_not_exists(pk) OR "
            "(leaseExpiresAt <= :now AND ownerPk = :ownerPk "
            "AND jobId = :jobId AND itemId = :itemId)"
        )
        values.update(
            {":ownerPk": owner_pk, ":jobId": job_id, ":itemId": item_id}
        )
    put = {
        "TableName": table,
        "Item": _serialize(record),
        "ConditionExpression": condition,
        "ExpressionAttributeValues": _serialize(values),
    }
    if names is not None:
        put["ExpressionAttributeNames"] = names
    return {"Put": put}


def _lease_delete(table, pk, sk, owner):
    return {
        "Delete": {
            "TableName": table,
            "Key": _serialize({"pk": pk, "sk": sk}),
            "ConditionExpression": (
                "ownerPk = :ownerPk AND jobId = :jobId "
                "AND itemId = :itemId AND attempt = :attempt"
            ),
            "ExpressionAttributeValues": _serialize(
                {
                    ":ownerPk": owner["ownerPk"],
                    ":jobId": owner["jobId"],
                    ":itemId": owner["itemId"],
                    ":attempt": owner["attempt"],
                }
            ),
        }
    }


def _transaction_token(namespace, identity, digest):
    return hashlib.sha256((namespace + "\0" + identity + "\0" + digest).encode("ascii")).hexdigest()[:36]


def _job_key(connection_id, job_id):
    return f"MIGRATION_JOB#{connection_id}#{job_id}"


def _item_prefix(connection_id, job_id):
    return f"MIGRATION_ITEM#{connection_id}#{job_id}#"


def _with_migration_work_index(record):
    selected = dict(record)
    selected.pop("migrationWorkPk", None)
    selected.pop("migrationWorkSk", None)
    state = selected.get("state")
    cancellation_due = selected.get("cancellationNextAttemptAt")
    if (
        state in _CANCELLATION_WORK_STATES
        and type(cancellation_due) is int
    ):
        due_at = cancellation_due
    elif state in {"pending", "pending_payment", "pending_customer_action", "applied"}:
        due_at = 0
    elif state == "retryable_failure":
        due_at = selected.get("nextAttemptAt")
    elif state == "applying":
        due_at = selected.get("leaseExpiresAt")
    else:
        return selected
    if type(due_at) is not int or not 0 <= due_at <= 9_999_999_999:
        raise MigrationStoreConflict("migration command conflicted")
    selected["migrationWorkPk"] = (
        f"{selected['pk']}#MIGRATION_WORK#{selected['connectionId']}#"
        f"{selected['jobId']}#{state}"
    )
    selected["migrationWorkSk"] = f"{due_at:010d}#{selected['itemId']}"
    return selected


def _command_key(connection_id, key_hash):
    return f"MIGRATION_COMMAND#{connection_id}#{key_hash}"


def _coverage_key(connection_id, offer_version_id):
    return f"MIGRATION_OFFER_COVERAGE#{connection_id}#{offer_version_id}"


def _subscription_claim_key(provider_subscription_id):
    _provider_value(provider_subscription_id)
    digest = hashlib.sha256(provider_subscription_id.encode("ascii")).hexdigest()
    return f"SUBSCRIPTION#{digest}"


def _account_partition(account_key_hash):
    _digest(account_key_hash)
    return f"MIGRATION_ACCOUNT#{account_key_hash}"


def _account_slot_key(slot):
    if type(slot) is not int or not 0 <= slot <= 4:
        raise MigrationStoreConflict("migration command conflicted")
    return f"SLOT#{slot}"


def _item_is_claimable(item, now_epoch):
    state = item.get("state")
    if state == "pending":
        return True
    if state == "retryable_failure":
        return type(item.get("nextAttemptAt")) is int and item["nextAttemptAt"] <= now_epoch
    if state == "applying":
        return type(item.get("leaseExpiresAt")) is int and item["leaseExpiresAt"] <= now_epoch
    return False


def _provider_value(value):
    if (
        type(value) is not str
        or not 1 <= len(value) <= 255
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise MigrationStoreConflict("migration command conflicted")
    return value


def _migration_counts(value):
    keys = {"total", "pending", "applied", "needsReview", "failed"}
    if (
        not isinstance(value, Mapping)
        or set(value) != keys
        or any(type(value[key]) is not int or value[key] < 0 for key in keys)
        or value["total"]
        != value["pending"] + value["applied"] + value["needsReview"] + value["failed"]
    ):
        raise MigrationStoreConflict("migration command conflicted")
    return dict(value)


def _count_bucket(state):
    if state in {"applied", "pending_update_applied", "reverted", "skipped"}:
        return "applied"
    if state in {"pending_update_expired", "needs_review"}:
        return "needsReview"
    if state == "permanent_failure":
        return "failed"
    return "pending"


def _is_terminal_item(state):
    return state in {
        "applied",
        "pending_update_applied",
        "pending_update_expired",
        "reverted",
        "skipped",
        "needs_review",
        "permanent_failure",
    }


def _is_awaiting_provider(state):
    return state in {"pending_payment", "pending_customer_action"}


def _is_cancellation_terminal(state):
    return state in {
        "reverted",
        "skipped",
        "pending_update_expired",
        "needs_review",
        "permanent_failure",
    }


def _event_id(job_id, event_type, suffix):
    digest = hashlib.sha256(
        (job_id + "\0" + event_type + "\0" + suffix).encode("ascii")
    ).hexdigest()
    return "migration-" + digest[:40]


def _scope(value):
    if type(value) is not IntegrationScope:
        raise MigrationStoreConflict("migration command conflicted")
    return value


def _safe_id(value):
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise MigrationStoreConflict("migration command conflicted")
    return value


def _valid_migration_item_id(value):
    return type(value) is str and _MIGRATION_ITEM_ID.fullmatch(value) is not None


def _valid_rollback_guard(value):
    if value is None:
        return True
    return (
        isinstance(value, Mapping)
        and set(value) == {"hash", "defaultsHash", "phaseIndex", "phaseStart"}
        and type(value.get("hash")) is str
        and _HASH.fullmatch(value["hash"]) is not None
        and type(value.get("defaultsHash")) is str
        and _HASH.fullmatch(value["defaultsHash"]) is not None
        and type(value.get("phaseIndex")) is int
        and 0 <= value["phaseIndex"] <= 19
        and type(value.get("phaseStart")) is int
        and 1 <= value["phaseStart"] <= 9_999_999_999
    )


def _digest(value):
    if type(value) is not str or _HASH.fullmatch(value) is None:
        raise MigrationStoreConflict("migration command conflicted")
    return value


def _epoch(value):
    if type(value) is not int or not 1 <= value <= 9_999_999_999:
        raise MigrationStoreConflict("migration command conflicted")
    return value


def _closed_keys(value, expected):
    if not isinstance(value, Mapping) or set(value) != expected:
        raise MigrationStoreConflict("migration command conflicted")
