"""SQS worker for draft-scoped subscription migration jobs."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from typing import Any

try:
    from domain.integrations import IntegrationScope
    from migration_store import MigrationStoreConflict, _queue_message
    from subscription_migrations import (
        MigrationNeedsReview,
        build_immediate_plan,
        build_next_renewal_plan,
        canonical_migration_snapshot,
        migration_snapshot_hash,
        validate_migration_offer_compatibility,
    )
except ModuleNotFoundError:
    from src.domain.integrations import IntegrationScope
    from src.migration_store import MigrationStoreConflict, _queue_message
    from src.subscription_migrations import (
        MigrationNeedsReview,
        build_immediate_plan,
        build_next_renewal_plan,
        canonical_migration_snapshot,
        migration_snapshot_hash,
        validate_migration_offer_compatibility,
    )


_MAX_ATTEMPTS = 5
_MAX_BATCH = 25
_MAX_DISCOVERY_PAGES = 1000
_PREVIEW_SECONDS = 24 * 60 * 60
_MUTATION_LEASE_SECONDS = 120


class MigrationRetryable(RuntimeError):
    pass


class SubscriptionMigrationWorker:
    def __init__(
        self,
        resolver: Any,
        mappings: Any,
        store: Any,
        provider: Any,
        queue: Any,
        tax_verifier: Any,
        *,
        now_epoch: Any,
        jitter: Any,
    ):
        if any(
            value is None
            for value in (
                resolver,
                mappings,
                store,
                provider,
                queue,
                tax_verifier,
                now_epoch,
                jitter,
            )
        ):
            raise RuntimeError("migration worker is unavailable")
        self._resolver = resolver
        self._mappings = mappings
        self._store = store
        self._provider = provider
        self._queue = queue
        self._tax_verifier = tax_verifier
        self._now_epoch = now_epoch
        self._jitter = jitter

    def process(self, value: object) -> None:
        message = _queue_message(value)
        scope = IntegrationScope(
            message["environment"],
            message["tenantId"],
            message["draftId"],
            message["domain"],
        )
        job = self._store.load_job(
            scope, message["connectionId"], message["jobId"]
        )
        if not isinstance(job, Mapping):
            return
        if (
            job.get("connectionId") != message["connectionId"]
            or job.get("jobId") != message["jobId"]
            or any(job.get(key) != expected for key, expected in scope.fields().items())
            or type(job.get("revision")) is not int
            or message["revision"] > job["revision"]
        ):
            raise RuntimeError("migration job is unavailable")
        if job.get("state") in {"completed", "completed_with_errors", "canceled"}:
            return
        resolved = self._resolver.resolve(
            scope,
            message["connectionId"],
            provider="stripe",
            capability="subscriptions",
        )
        if job["state"] == "previewing" and message["action"] == "preview":
            self._preview(scope, dict(job), resolved, message)
            return
        if job["state"] in {"cancel_requested", "canceling"}:
            self._cancel(scope, dict(job), resolved, message)
            return
        if message["action"] in {"execute", "control", "reconcile"}:
            self._execute(scope, dict(job), resolved, message)

    def _preview(self, scope, job, resolved, message):
        response = self._provider.list_migration_candidates(
            resolved, job["sourcePriceId"], job.get("discoveryCursor")
        )
        if (
            not isinstance(response, Mapping)
            or set(response) != {"subscriptionIds", "nextCursor"}
            or not isinstance(response["subscriptionIds"], list)
            or len(response["subscriptionIds"]) > 100
            or len(set(response["subscriptionIds"])) != len(response["subscriptionIds"])
            or any(type(value) is not str for value in response["subscriptionIds"])
            or (
                response["nextCursor"] is not None
                and type(response["nextCursor"]) is not str
            )
            or (
                response["nextCursor"] is not None
                and (
                    not response["subscriptionIds"]
                    or response["nextCursor"] == job.get("discoveryCursor")
                    or job["revision"] >= _MAX_DISCOVERY_PAGES
                )
            )
        ):
            raise RuntimeError("migration discovery is unavailable")
        page = []
        for provider_subscription_id in response["subscriptionIds"]:
            item_id = _item_id(provider_subscription_id)
            membership = None
            try:
                raw = self._provider.retrieve_migration_snapshot(
                    resolved, provider_subscription_id
                )
                snapshot = canonical_migration_snapshot(raw)
                if snapshot["subscriptionId"] != provider_subscription_id:
                    raise MigrationNeedsReview("scope-mismatch")
                offer_ids = self._owned_offer_ids(scope, job, snapshot)
                validate_migration_offer_compatibility(
                    snapshot,
                    job["sourcePriceId"],
                    job["sourceOffer"],
                    job["targetOffer"],
                )
                membership = {
                    "offerVersionIds": offer_ids,
                    "primaryOfferVersionId": job["sourceOffer"]["offerVersionId"],
                }
                proration_timestamp = None
                preview_amount = None
                if job["requestedPolicy"]["mode"] == "immediate_prorated":
                    proration_timestamp = self._server_time()
                    source_line = _source_line(snapshot, job["sourcePriceId"])
                    preview = self._provider.preview_migration_proration(
                        resolved,
                        subscription_id=provider_subscription_id,
                        item_id=source_line["itemId"],
                        quantity=source_line["quantity"],
                        source_price_id=job["sourcePriceId"],
                        target_price_id=job["targetPriceId"],
                        proration_timestamp=proration_timestamp,
                    )
                    if (
                        not isinstance(preview, Mapping)
                        or set(preview) != {"prorationTimestamp", "amountMinor"}
                        or preview["prorationTimestamp"] != proration_timestamp
                    ):
                        raise MigrationNeedsReview("provider-unknown")
                    preview_amount = preview["amountMinor"]
                    build_immediate_plan(
                        snapshot,
                        job["sourcePriceId"],
                        job["targetPriceId"],
                        proration_timestamp=proration_timestamp,
                        preview_amount_minor=preview_amount,
                    )
                else:
                    build_next_renewal_plan(
                        snapshot, job["sourcePriceId"], job["targetPriceId"]
                    )
                selected = {
                    "itemId": item_id,
                    "state": "pending",
                    "reasonCode": None,
                    "attempts": 0,
                    "providerSubscriptionId": provider_subscription_id,
                    "snapshot": snapshot,
                    "snapshotHash": migration_snapshot_hash(snapshot),
                    "prorationTimestamp": proration_timestamp,
                    "previewAmountMinor": preview_amount,
                }
            except MigrationNeedsReview as error:
                selected = {
                    "itemId": item_id,
                    "state": "needs_review",
                    "reasonCode": _reason(error),
                    "attempts": 0,
                    "providerSubscriptionId": provider_subscription_id,
                    "snapshot": None,
                    "snapshotHash": None,
                    "prorationTimestamp": None,
                    "previewAmountMinor": None,
                }
            except Exception as error:
                if _is_retryable(error):
                    raise
                selected = {
                    "itemId": item_id,
                    "state": "needs_review",
                    "reasonCode": "provider-unknown",
                    "attempts": 0,
                    "providerSubscriptionId": provider_subscription_id,
                    "snapshot": None,
                    "snapshotHash": None,
                    "prorationTimestamp": None,
                    "previewAmountMinor": None,
                }
            persisted = self._store.put_preview_item(
                scope=scope,
                connectionId=job["connectionId"],
                jobId=job["jobId"],
                **selected,
            )
            if selected["state"] == "pending" and membership is not None:
                try:
                    self._store.bind_migration_subscription(
                        scope=scope,
                        connectionId=job["connectionId"],
                        jobId=job["jobId"],
                        itemId=item_id,
                        providerSubscriptionId=provider_subscription_id,
                        **membership,
                    )
                except MigrationStoreConflict:
                    persisted = self._store.reject_preview_item(
                        scope=scope,
                        connectionId=job["connectionId"],
                        jobId=job["jobId"],
                        itemId=item_id,
                        reasonCode="scope-mismatch",
                    )
            page.append(dict(persisted) if isinstance(persisted, Mapping) else selected)
        if response["nextCursor"] is not None:
            updated = self._store.advance_preview(
                scope=scope,
                connectionId=job["connectionId"],
                jobId=job["jobId"],
                expectedRevision=job["revision"],
                cursor=response["nextCursor"],
            )
            self._queue.send(
                {**message, "action": "preview", "revision": updated["revision"]}
            )
            return
        preview_summary = getattr(self._store, "preview_summary", None)
        if callable(preview_summary):
            summary = preview_summary(scope, job["connectionId"], job["jobId"])
            if (
                not isinstance(summary, Mapping)
                or set(summary) != {"dryRunHash", "counts"}
                or type(summary["dryRunHash"]) is not str
                or len(summary["dryRunHash"]) != 64
                or not isinstance(summary["counts"], Mapping)
            ):
                raise RuntimeError("migration preview is unavailable")
            dry_run_hash = summary["dryRunHash"]
            counts = dict(summary["counts"])
        else:
            dry_run_hash = _dry_run_hash(job, page)
            counts = _counts(page)
        self._store.complete_preview(
            scope=scope,
            connectionId=job["connectionId"],
            jobId=job["jobId"],
            expectedRevision=job["revision"],
            dryRunHash=dry_run_hash,
            counts=counts,
            expiresAt=self._server_time() + _PREVIEW_SECONDS,
        )

    def _execute(self, scope, job, resolved, message):
        if job["state"] == "paused":
            return
        if job["state"] == "scheduled":
            job = self._store.start_execution(
                scope=scope,
                connectionId=job["connectionId"],
                jobId=job["jobId"],
                expectedRevision=job["revision"],
            )
        if job.get("state") != "running":
            return
        first_batch = job.get("mutationStarted") is not True
        limit = job["canarySize"] if first_batch else job["accountConcurrency"]
        items = self._store.claim_items(
            scope=scope,
            connectionId=job["connectionId"],
            jobId=job["jobId"],
            limit=limit,
            maxAttempts=_MAX_ATTEMPTS,
            accountConcurrency=job["accountConcurrency"],
            accountKeyHash=_account_key_hash(resolved),
            nowEpoch=self._server_time(),
            leaseSeconds=_MUTATION_LEASE_SECONDS,
            expectedJobRevision=job["revision"],
            canaryLimit=job["canarySize"] if first_batch else 0,
        )
        for item in items:
            self._apply_item(scope, job, item, resolved, message)
        continuation = self._store.continue_execution(
            scope=scope,
            connectionId=job["connectionId"],
            jobId=job["jobId"],
            nowEpoch=self._server_time(),
        )
        if isinstance(continuation, Mapping):
            self._queue.send(
                {**message, "action": "execute", "revision": continuation["revision"]},
                delay_seconds=continuation.get("workDelaySeconds", 0),
            )

    def _apply_item(self, scope, job, item, resolved, message):
        attempts = item.get("attempts")
        if type(attempts) is not int or not 1 <= attempts <= _MAX_ATTEMPTS:
            raise RuntimeError("migration item is unavailable")
        schedule_id = item.get("scheduleId")
        rollback_guard = item.get("rollbackGuard")
        try:
            current = canonical_migration_snapshot(
                self._provider.retrieve_migration_snapshot(
                    resolved, item["providerSubscriptionId"]
                )
            )
            if current["subscriptionId"] != item["providerSubscriptionId"]:
                raise MigrationNeedsReview("scope-mismatch")
            self._owned_offer_ids(scope, job, current)
            validate_migration_offer_compatibility(
                current,
                job["sourcePriceId"],
                job["sourceOffer"],
                job["targetOffer"],
            )
            partial_schedule = _partial_schedule_retry(current, item, job)
            reconciled_guard = None
            if (
                job["requestedPolicy"]["mode"] == "next_renewal"
                and type(schedule_id) is str
            ):
                try:
                    reconciled_guard = _migration_rollback_guard(
                        current, item, job, schedule_id
                    )
                except MigrationNeedsReview:
                    reconciled_guard = None
            if (
                migration_snapshot_hash(current) != item["snapshotHash"]
                and not partial_schedule
                and reconciled_guard is None
            ):
                raise MigrationNeedsReview("source-drift")
            authorization = _tax_authorization(job.get("taxAuthorization"))
            if self._tax_verifier.validate_state(authorization, current) is not True:
                raise MigrationNeedsReview("tax-approval")
            key = _provider_key(
                job["jobId"], item["providerSubscriptionId"], job["targetOffer"]["revision"]
            )
            if job["requestedPolicy"]["mode"] == "next_renewal":
                if reconciled_guard is not None:
                    rollback_guard = reconciled_guard
                    result = {"status": "applied", "scheduleId": schedule_id}
                else:
                    plan = build_next_renewal_plan(
                        item["snapshot"] if partial_schedule else current,
                        job["sourcePriceId"],
                        job["targetPriceId"],
                    )
                    result = self._provider.apply_next_renewal_migration(
                        resolved,
                        subscription_id=item["providerSubscriptionId"],
                        plan=plan,
                        existing_schedule_id=item.get("scheduleId"),
                        idempotency_key=key,
                    )
            else:
                source_line = _source_line(current, job["sourcePriceId"])
                preview = self._provider.preview_migration_proration(
                    resolved,
                    subscription_id=item["providerSubscriptionId"],
                    item_id=source_line["itemId"],
                    quantity=source_line["quantity"],
                    source_price_id=job["sourcePriceId"],
                    target_price_id=job["targetPriceId"],
                    proration_timestamp=item["prorationTimestamp"],
                )
                if (
                    not isinstance(preview, Mapping)
                    or preview.get("prorationTimestamp") != item["prorationTimestamp"]
                    or preview.get("amountMinor") != item["previewAmountMinor"]
                ):
                    raise MigrationNeedsReview("source-drift")
                plan = build_immediate_plan(
                    current,
                    job["sourcePriceId"],
                    job["targetPriceId"],
                    proration_timestamp=item["prorationTimestamp"],
                    preview_amount_minor=item["previewAmountMinor"],
                )
                result = self._provider.apply_immediate_migration(
                    resolved,
                    plan=plan,
                    idempotency_key=key,
                )
            if (
                not isinstance(result, Mapping)
                or set(result) - {"status", "scheduleId"}
                or result.get("status")
                not in {"applied", "pending_payment", "pending_customer_action"}
            ):
                raise MigrationNeedsReview("provider-unknown")
            schedule_id = result.get("scheduleId")
            if (
                job["requestedPolicy"]["mode"] == "next_renewal"
                and reconciled_guard is None
            ):
                applied = canonical_migration_snapshot(
                    self._provider.retrieve_migration_snapshot(
                        resolved, item["providerSubscriptionId"]
                    )
                )
                rollback_guard = _migration_rollback_guard(
                    applied,
                    item,
                    job,
                    schedule_id,
                )
            self._store.complete_item(
                scope=scope,
                connectionId=job["connectionId"],
                jobId=job["jobId"],
                itemId=item["itemId"],
                attempts=attempts,
                state=result["status"],
                reasonCode=None,
                scheduleId=schedule_id,
                rollbackGuard=rollback_guard,
            )
        except MigrationRetryable as error:
            self._retry_item(
                scope, job, item, attempts, message, error, schedule_id=schedule_id
            )
        except MigrationNeedsReview as error:
            self._store.complete_item(
                scope=scope,
                connectionId=job["connectionId"],
                jobId=job["jobId"],
                itemId=item["itemId"],
                attempts=attempts,
                state="needs_review",
                reasonCode=_reason(error),
                scheduleId=schedule_id,
                rollbackGuard=rollback_guard,
            )
        except Exception as error:
            if _is_retryable(error):
                self._retry_item(
                    scope,
                    job,
                    item,
                    attempts,
                    message,
                    error,
                    schedule_id=schedule_id,
                )
                return
            self._store.complete_item(
                scope=scope,
                connectionId=job["connectionId"],
                jobId=job["jobId"],
                itemId=item["itemId"],
                attempts=attempts,
                state="needs_review",
                reasonCode="provider-unknown",
                scheduleId=getattr(error, "schedule_id", schedule_id),
                rollbackGuard=rollback_guard,
            )

    def _retry_item(
        self, scope, job, item, attempts, message, error=None, *, schedule_id=None
    ):
        delay = min(900, 2**attempts + _bounded_jitter(self._jitter, attempts))
        next_attempt_at = self._server_time() + delay if attempts < _MAX_ATTEMPTS else None
        retry = self._store.retry_item(
            scope=scope,
            connectionId=job["connectionId"],
            jobId=job["jobId"],
            itemId=item["itemId"],
            attempts=attempts,
            reasonCode=(
                "retry-exhausted" if attempts >= _MAX_ATTEMPTS else "provider-unknown"
            ),
            nextAttemptAt=next_attempt_at,
            scheduleId=getattr(error, "schedule_id", schedule_id),
        )
        if retry:
            self._queue.send(
                {**message, "action": "execute", "revision": job["revision"]},
                delay_seconds=delay,
            )

    def _cancel(self, scope, job, resolved, message):
        if job["state"] == "cancel_requested":
            job = self._store.begin_cancel(
                scope=scope,
                connectionId=job["connectionId"],
                jobId=job["jobId"],
                expectedRevision=job["revision"],
            )
        items = self._store.cancellation_items(
            scope=scope,
            connectionId=job["connectionId"],
            jobId=job["jobId"],
            limit=_MAX_BATCH,
        )
        pending = False
        retry_delay = 60
        now_epoch = self._server_time()
        for item in items:
            cancellation_due = item.get("cancellationNextAttemptAt")
            if type(cancellation_due) is int and cancellation_due > now_epoch:
                pending = True
                retry_delay = min(
                    retry_delay, max(1, cancellation_due - now_epoch)
                )
                continue
            if item.get("state") == "pending":
                self._store.complete_cancellation_item(
                    scope=scope,
                    connectionId=job["connectionId"],
                    jobId=job["jobId"],
                    itemId=item["itemId"],
                    state="skipped",
                    reasonCode=None,
                )
                continue
            lease_expires = item.get("leaseExpiresAt")
            if (
                item.get("state") == "applying"
                and type(lease_expires) is int
                and lease_expires > now_epoch
            ):
                pending = True
                retry_delay = min(retry_delay, max(1, lease_expires - now_epoch))
                continue
            try:
                if self._cancel_item(scope, job, resolved, item):
                    pending = True
            except MigrationNeedsReview as error:
                self._store.complete_cancellation_item(
                    scope=scope,
                    connectionId=job["connectionId"],
                    jobId=job["jobId"],
                    itemId=item["itemId"],
                    state="needs_review",
                    reasonCode=_reason(error),
                )
            except Exception as error:
                if _is_retryable(error):
                    cancellation_attempts = item.get("cancellationAttempts", 0) + 1
                    delay = min(
                        900,
                        2**cancellation_attempts
                        + _bounded_jitter(self._jitter, cancellation_attempts),
                    )
                    retry = self._store.retry_cancellation_item(
                        scope=scope,
                        connectionId=job["connectionId"],
                        jobId=job["jobId"],
                        itemId=item["itemId"],
                        cancellationAttempts=cancellation_attempts,
                        nextAttemptAt=(
                            now_epoch + delay
                            if cancellation_attempts < _MAX_ATTEMPTS
                            else None
                        ),
                    )
                    if retry:
                        pending = True
                        retry_delay = min(retry_delay, delay)
                    continue
                self._store.complete_cancellation_item(
                    scope=scope,
                    connectionId=job["connectionId"],
                    jobId=job["jobId"],
                    itemId=item["itemId"],
                    state="needs_review",
                    reasonCode="provider-unknown",
                )
        if pending:
            self._queue.send(
                {**message, "action": "reconcile", "revision": job["revision"]},
                delay_seconds=retry_delay,
            )
        else:
            completed = self._store.finalize_cancellation(
                scope=scope,
                connectionId=job["connectionId"],
                jobId=job["jobId"],
            )
            if completed is not True:
                self._queue.send(
                    {**message, "action": "reconcile", "revision": job["revision"]},
                    delay_seconds=2,
                )

    def _cancel_item(self, scope, job, resolved, item):
        current = canonical_migration_snapshot(
            self._provider.retrieve_migration_snapshot(
                resolved, item["providerSubscriptionId"]
            )
        )
        if current["subscriptionId"] != item["providerSubscriptionId"]:
            raise MigrationNeedsReview("scope-mismatch")
        if current["pendingUpdate"] is not None:
            return True
        current_schedule = current.get("schedule")
        schedule_id = (
            current_schedule.get("scheduleId")
            if isinstance(current_schedule, Mapping)
            else None
        )
        original_schedule = (
            item.get("snapshot", {}).get("schedule")
            if isinstance(item.get("snapshot"), Mapping)
            else None
        )
        if type(schedule_id) is str:
            expected_schedule_id = item.get("scheduleId")
            owns_schedule = (
                isinstance(original_schedule, Mapping)
                and original_schedule.get("scheduleId") == schedule_id
            ) or (
                original_schedule is None
                and type(expected_schedule_id) is str
                and expected_schedule_id == schedule_id
            )
            if not owns_schedule:
                raise MigrationNeedsReview("source-drift")
            if not _rollback_guard_allows(current, item, job):
                raise MigrationNeedsReview("source-drift")
            result = self._provider.release_migration_schedule(
                resolved,
                schedule_id=schedule_id,
                subscription_id=item["providerSubscriptionId"],
                original_schedule=original_schedule,
                current_phase_anchor=_current_schedule_anchor(current_schedule),
                idempotency_key=_provider_key(
                    job["jobId"],
                    item["providerSubscriptionId"],
                    job["targetOffer"]["revision"],
                ),
            )
            state = result.get("status") if isinstance(result, Mapping) else None
            if state != "reverted":
                raise MigrationNeedsReview("provider-unknown")
            reason = None
        else:
            prices = [line["priceId"] for line in current["items"]]
            if prices.count(job["sourcePriceId"]) == 1:
                state, reason = "reverted", None
            else:
                state, reason = "needs_review", "provider-unknown"
        self._store.complete_cancellation_item(
            scope=scope,
            connectionId=job["connectionId"],
            jobId=job["jobId"],
            itemId=item["itemId"],
            state=state,
            reasonCode=reason,
        )
        return False

    def _owned_offer_ids(self, scope, job, snapshot):
        offer_ids = []
        source_matches = 0
        for item in snapshot["items"]:
            mapping = self._mappings.object_owner(
                scope, job["connectionId"], "price", item["priceId"]
            )
            if (
                not isinstance(mapping, Mapping)
                or mapping.get("resourceType") != "offer"
                or mapping.get("priceId") != item["priceId"]
                or type(mapping.get("resourceId")) is not str
                or mapping.get("status") not in {"active", "existing_only"}
            ):
                raise MigrationNeedsReview("unmapped-price")
            offer_ids.append(mapping["resourceId"])
            if (
                item["priceId"] == job["sourcePriceId"]
                and mapping["resourceId"] == job["sourceOffer"]["offerVersionId"]
                and mapping.get("revision") == job["sourceOffer"]["revision"]
                and mapping.get("contentHash") == job["sourceOffer"]["contentHash"]
            ):
                source_matches += 1
        if source_matches != 1:
            raise MigrationNeedsReview(
                "ambiguous-price" if source_matches > 1 else "scope-mismatch"
            )
        target_mapping = self._mappings.object_owner(
            scope, job["connectionId"], "price", job["targetPriceId"]
        )
        if (
            not isinstance(target_mapping, Mapping)
            or target_mapping.get("resourceType") != "offer"
            or target_mapping.get("resourceId")
            != job["targetOffer"]["offerVersionId"]
            or target_mapping.get("priceId") != job["targetPriceId"]
            or target_mapping.get("revision") != job["targetOffer"]["revision"]
            or target_mapping.get("contentHash") != job["targetOffer"]["contentHash"]
            or target_mapping.get("status") not in {"active", "existing_only"}
        ):
            raise MigrationNeedsReview("source-drift")
        return sorted(set(offer_ids))

    def _server_time(self):
        value = self._now_epoch()
        if type(value) is not int or not 1 <= value <= 9_999_999_999:
            raise RuntimeError("migration worker is unavailable")
        return value


def handle_records(event: object, *, worker: Any) -> dict[str, list[dict[str, str]]]:
    records = event.get("Records") if isinstance(event, Mapping) else None
    if not isinstance(records, list) or not 1 <= len(records) <= 10:
        raise RuntimeError("migration batch is unavailable")
    failures = []
    for record in records:
        message_id = record.get("messageId") if isinstance(record, Mapping) else None
        body = record.get("body") if isinstance(record, Mapping) else None
        if type(message_id) is not str or not message_id or type(body) is not str:
            raise RuntimeError("migration batch is unavailable")
        try:
            value = json.loads(body)
            worker.process(value)
        except Exception:
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


def lambda_handler(event: object, context: object):
    del context
    return handle_records(event, worker=_runtime_worker())


def _runtime_worker():
    try:
        from runtime import subscription_migration_worker_runtime
    except ModuleNotFoundError:
        from src.runtime import subscription_migration_worker_runtime
    return subscription_migration_worker_runtime()


def _item_id(provider_subscription_id):
    try:
        digest = hashlib.sha256(provider_subscription_id.encode("ascii")).hexdigest()
    except Exception:
        raise MigrationNeedsReview("scope-mismatch") from None
    return "migration-item-" + digest[:40]


def _provider_key(job_id, provider_subscription_id, target_revision):
    digest = hashlib.sha256(
        (job_id + "\0" + provider_subscription_id + "\0" + str(target_revision)).encode("ascii")
    ).hexdigest()
    return "migration-item-v1:" + digest


def _source_line(snapshot, source_price_id):
    matches = [
        item for item in snapshot["items"] if item["priceId"] == source_price_id
    ]
    if len(matches) != 1:
        raise MigrationNeedsReview(
            "ambiguous-price" if len(matches) > 1 else "source-drift"
        )
    return matches[0]


def _is_retryable(error):
    return isinstance(error, MigrationRetryable) or getattr(error, "retryable", False) is True


def _partial_schedule_retry(current, item, job):
    stored_schedule_id = item.get("scheduleId")
    original = item.get("snapshot")
    current_schedule = current.get("schedule")
    if (
        job["requestedPolicy"]["mode"] != "next_renewal"
        or type(stored_schedule_id) is not str
        or not isinstance(original, Mapping)
        or original.get("schedule") is not None
        or not isinstance(current_schedule, Mapping)
        or current_schedule.get("scheduleId") != stored_schedule_id
    ):
        return False
    selected = copy.deepcopy(current)
    selected["schedule"] = None
    selected["providerRevision"] = original["providerRevision"]
    return migration_snapshot_hash(selected) == migration_snapshot_hash(original)


def _migration_rollback_guard(current, item, job, schedule_id):
    if current["subscriptionId"] != item["providerSubscriptionId"]:
        raise MigrationNeedsReview("scope-mismatch")
    schedule = current.get("schedule")
    if (
        type(schedule_id) is not str
        or not isinstance(schedule, Mapping)
        or schedule.get("scheduleId") != schedule_id
        or schedule.get("status") not in {"active", "not_started"}
    ):
        raise MigrationNeedsReview("source-drift")
    plan = build_next_renewal_plan(
        item["snapshot"], job["sourcePriceId"], job["targetPriceId"]
    )
    index = schedule["currentPhaseIndex"]
    phases = schedule["phases"]
    if (
        schedule["endBehavior"] != plan["endBehavior"]
        or not 0 <= index < len(phases)
        or phases[index:] != plan["phases"]
        or (
            plan["defaultSettings"] is not None
            and schedule["defaultSettings"] != plan["defaultSettings"]
        )
    ):
        raise MigrationNeedsReview("source-drift")
    projection = _schedule_projection(schedule)
    return {
        "hash": _canonical_digest(projection),
        "defaultsHash": _canonical_digest(schedule["defaultSettings"]),
        "phaseIndex": index,
        "phaseStart": phases[index]["startDate"],
    }


def _rollback_guard_allows(current, item, job):
    guard = item.get("rollbackGuard")
    schedule = current.get("schedule")
    if (
        not isinstance(guard, Mapping)
        or set(guard) != {"hash", "defaultsHash", "phaseIndex", "phaseStart"}
        or not isinstance(schedule, Mapping)
        or schedule.get("scheduleId") != item.get("scheduleId")
        or schedule.get("status") not in {"active", "not_started"}
        or type(guard.get("phaseIndex")) is not int
        or type(guard.get("phaseStart")) is not int
    ):
        return False
    index = schedule.get("currentPhaseIndex")
    phases = schedule.get("phases")
    if (
        type(index) is not int
        or not isinstance(phases, list)
        or not 0 <= index < len(phases)
    ):
        return False
    anchor = phases[index].get("startDate")
    if (
        type(anchor) is not int
        or index < guard["phaseIndex"]
        or anchor < guard["phaseStart"]
    ):
        return False
    if _canonical_digest(_schedule_projection(schedule)) == guard.get("hash"):
        return True
    if _canonical_digest(schedule.get("defaultSettings")) != guard.get(
        "defaultsHash"
    ):
        return False
    try:
        plan = build_next_renewal_plan(
            item["snapshot"], job["sourcePriceId"], job["targetPriceId"]
        )
    except Exception:
        return False
    matching = [
        offset
        for offset, phase in enumerate(plan["phases"])
        if phase.get("startDate") == anchor
    ]
    return (
        len(matching) == 1
        and schedule.get("endBehavior") == plan["endBehavior"]
        and phases[index:] == plan["phases"][matching[0] :]
    )


def _schedule_projection(schedule):
    return {
        "scheduleId": schedule["scheduleId"],
        "endBehavior": schedule["endBehavior"],
        "defaultSettings": copy.deepcopy(schedule["defaultSettings"]),
        "phases": copy.deepcopy(schedule["phases"]),
    }


def _current_schedule_anchor(schedule):
    try:
        index = schedule["currentPhaseIndex"]
        anchor = schedule["phases"][index]["startDate"]
    except Exception:
        raise MigrationNeedsReview("source-drift") from None
    if type(index) is not int or type(anchor) is not int:
        raise MigrationNeedsReview("source-drift")
    return anchor


def _canonical_digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()


def _account_key_hash(resolved):
    connection = getattr(resolved, "connection", None)
    metadata = getattr(connection, "provider_metadata", None)
    account_reference = (
        metadata.get("accountReference") if isinstance(metadata, Mapping) else None
    )
    mode = getattr(connection, "mode", None)
    if (
        type(account_reference) is not str
        or not account_reference.startswith("acct_")
        or type(mode) is not str
        or mode not in {"test", "live"}
    ):
        raise RuntimeError("migration worker is unavailable")
    try:
        return hashlib.sha256(
            (mode + "\0" + account_reference).encode("ascii")
        ).hexdigest()
    except Exception:
        raise RuntimeError("migration worker is unavailable") from None


def _dry_run_hash(job, items):
    protected = [
        {
            "itemId": item["itemId"],
            "state": item["state"],
            "reasonCode": item["reasonCode"],
            "snapshotHash": item["snapshotHash"],
            "prorationTimestamp": item["prorationTimestamp"],
            "previewAmountMinor": item["previewAmountMinor"],
        }
        for item in sorted(items, key=lambda selected: selected["itemId"])
    ]
    encoded = json.dumps(
        {
            "jobId": job["jobId"],
            "sourceOffer": job["sourceOffer"],
            "targetOffer": job["targetOffer"],
            "requestedPolicy": job["requestedPolicy"],
            "items": protected,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _counts(items):
    result = {"total": len(items), "pending": 0, "applied": 0, "needsReview": 0, "failed": 0}
    for item in items:
        if item["state"] == "needs_review":
            result["needsReview"] += 1
        elif item["state"] == "applied":
            result["applied"] += 1
        elif item["state"] == "permanent_failure":
            result["failed"] += 1
        else:
            result["pending"] += 1
    return result


def _tax_authorization(value):
    if (
        not isinstance(value, Mapping)
        or set(value) != {"taxMode", "approvalHash"}
        or value["taxMode"] not in {"manual-rate", "stripe-tax"}
        or (value["approvalHash"] is not None and type(value["approvalHash"]) is not str)
    ):
        raise MigrationNeedsReview("tax-approval")
    return value["taxMode"], value["approvalHash"]


def _reason(error):
    value = str(error)
    allowed = {
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
    return value if value in allowed else "provider-unknown"


def _bounded_jitter(jitter, attempt):
    try:
        value = jitter(attempt)
    except Exception:
        return 0
    return value if type(value) is int and 0 <= value <= 30 else 0
