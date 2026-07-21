import copy
import hashlib
import unittest

from src.domain.integrations import IntegrationScope
from src.migration_store import (
    DynamoMigrationStore,
    DynamoOfferReferenceGuard,
    MigrationStoreConflict,
    MigrationStoreError,
    SqsMigrationQueue,
    _with_migration_work_index,
)
from src.registry import _deserialize, _serialize
from tests.test_migration_contracts import offer
from tests.test_subscription_migration_domain import snapshot
from src.subscription_migrations import migration_snapshot_hash


SCOPE = IntegrationScope("test", "tenant-example", "draft-example", "example.com")
NOW = 1_800_000_000


def rollback_guard():
    return {
        "hash": "d" * 64,
        "defaultsHash": "e" * 64,
        "phaseIndex": 0,
        "phaseStart": NOW,
    }


class Dynamo:
    def __init__(self):
        self.items = {}
        self.transactions = []
        self.query_response = {"Items": []}
        self.dynamic_query = False
        self.queries = []

    def get_item(self, **kwargs):
        key = _deserialize(kwargs["Key"])
        item = self.items.get((kwargs["TableName"], key["pk"], key["sk"]))
        return {} if item is None else {"Item": item}

    def transact_write_items(self, **kwargs):
        self.transactions.append(kwargs)
        for operation in kwargs["TransactItems"]:
            put = operation.get("Put")
            delete = operation.get("Delete")
            if delete is not None:
                key = _deserialize(delete["Key"])
                self.items.pop((delete["TableName"], key["pk"], key["sk"]), None)
                continue
            if put is None:
                continue
            plain = _deserialize(put["Item"])
            identity = (put["TableName"], plain["pk"], plain["sk"])
            if identity in self.items and "attribute_not_exists" in put.get("ConditionExpression", ""):
                raise RuntimeError("conditional")
            self.items[identity] = put["Item"]

    def query(self, **kwargs):
        self.last_query = kwargs
        self.queries.append(copy.deepcopy(kwargs))
        if self.dynamic_query:
            values = _deserialize(kwargs["ExpressionAttributeValues"])
            if kwargs.get("IndexName") == "MigrationWorkIndex":
                selected = []
                for (table, _pk, _sk), stored in self.items.items():
                    if table != kwargs["TableName"]:
                        continue
                    plain = _deserialize(stored)
                    if plain.get("migrationWorkPk") != values[":workPk"]:
                        continue
                    if (
                        ":due" in values
                        and plain.get("migrationWorkSk", "") > values[":due"]
                    ):
                        continue
                    selected.append(plain)
                selected.sort(key=lambda item: item["migrationWorkSk"])
                selected = selected[: kwargs["Limit"]]
                return {"Items": [_serialize(item) for item in selected]}
            items = [
                value
                for (table, pk, sk), value in self.items.items()
                if table == kwargs["TableName"]
                and pk == values[":pk"]
                and sk.startswith(values[":prefix"])
            ]
            return {"Items": copy.deepcopy(items)}
        return copy.deepcopy(self.query_response)


def preview_kwargs():
    return {
        "scope": SCOPE,
        "connectionId": "stripe-primary",
        "jobId": "migration-job-1",
        "commercialRequestId": "request-1",
        "sourceOffer": offer("offer-old", 90000),
        "targetOffer": offer("offer-new", 100000),
        "sourcePriceId": "price_old",
        "targetPriceId": "price_new",
        "requestedPolicy": {"mode": "next_renewal"},
        "candidateScope": {"kind": "all_matching_source_price"},
        "canarySize": 5,
        "accountConcurrency": 2,
        "idempotencyKeyHash": "a" * 64,
        "requestHash": "b" * 64,
        "commandId": "command-1",
        "createdAt": NOW,
    }


class MigrationStoreTests(unittest.TestCase):
    def setUp(self):
        self.client = Dynamo()
        self.store = DynamoMigrationStore(
            "registry", "technical", client=self.client
        )

    def _pending_migration(self, item_state="pending_customer_action"):
        client = Dynamo()
        client.dynamic_query = True
        store = DynamoMigrationStore(
            "registry", "technical", client=client, now_epoch=lambda: NOW + 20
        )
        store.create_preview(**preview_kwargs())
        provider_id = "sub_synthetic"
        item_id = "migration-item-" + hashlib.sha256(
            provider_id.encode("ascii")
        ).hexdigest()[:40]
        selected_snapshot = snapshot()
        store.put_preview_item(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-1",
            itemId=item_id,
            state="pending",
            reasonCode=None,
            attempts=0,
            providerSubscriptionId=provider_id,
            snapshot=selected_snapshot,
            snapshotHash=migration_snapshot_hash(selected_snapshot),
            prorationTimestamp=NOW + 1,
            previewAmountMinor=1_000,
        )
        store.bind_migration_subscription(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-1",
            itemId=item_id,
            providerSubscriptionId=provider_id,
            offerVersionIds=["offer-old", "offer-addon"],
            primaryOfferVersionId="offer-old",
        )
        job_key = (
            "registry",
            SCOPE.partition_key,
            "MIGRATION_JOB#stripe-primary#migration-job-1",
        )
        running = _deserialize(client.items[job_key])
        running.update(
            {
                "state": "running",
                "revision": 3,
                "dryRunRevision": 1,
                "dryRunHash": "c" * 64,
                "previewExpiresAt": NOW + 86_400,
                "taxAuthorization": {"taxMode": "manual-rate", "approvalHash": None},
                "mutationStarted": True,
                "canaryClaims": 1,
                "canaryCompleted": 0,
                "canaryApproved": True,
                "awaitingProviderCount": (
                    1
                    if item_state in {"pending_payment", "pending_customer_action"}
                    else 0
                ),
                "counts": {"total": 1, "pending": 1, "applied": 0, "needsReview": 0, "failed": 0},
            }
        )
        client.items[job_key] = _serialize(running)
        item_key = (
            "registry",
            SCOPE.partition_key,
            f"MIGRATION_ITEM#stripe-primary#migration-job-1#{item_id}",
        )
        pending = _deserialize(client.items[item_key])
        pending.update({"state": item_state, "attempts": 1, "canary": True})
        if item_state != "applying":
            pending = _with_migration_work_index(pending)
        client.items[item_key] = _serialize(pending)
        return store, client, item_key, job_key

    def test_pending_update_webhooks_apply_or_expire_idempotently_and_finish_job(self):
        store, client, item_key, job_key = self._pending_migration()
        applied = {
            "scope": SCOPE,
            "connectionId": "stripe-primary",
            "providerSubscriptionId": "sub_synthetic",
            "eventId": "evt-applied",
            "eventType": "customer.subscription.pending_update_applied",
            "eventCreatedAt": NOW + 10,
            "priceIds": ["price_new", "price_addon"],
            "pendingUpdate": False,
        }
        result = store.reconcile_migration_webhook(**applied)
        replay = store.reconcile_migration_webhook(**applied)

        self.assertEqual(result["state"], "pending_update_applied")
        self.assertFalse(replay["enqueue"])
        persisted = _deserialize(client.items[item_key])
        self.assertEqual(persisted["state"], "pending_update_applied")
        self.assertEqual(persisted["lastProviderEventId"], "evt-applied")
        completed_job = _deserialize(client.items[job_key])
        self.assertEqual(completed_job["state"], "completed")
        self.assertEqual(completed_job["appliedItemCount"], 1)

        expired_store, expired_client, expired_item_key, expired_job_key = (
            self._pending_migration("pending_payment")
        )
        expired = expired_store.reconcile_migration_webhook(
            **{
                **applied,
                "eventId": "evt-expired",
                "eventType": "customer.subscription.pending_update_expired",
                "priceIds": ["price_old", "price_addon"],
            }
        )
        self.assertEqual(expired["state"], "pending_update_expired")
        self.assertEqual(
            _deserialize(expired_client.items[expired_item_key])["reasonCode"],
            "payment-failed",
        )
        self.assertEqual(
            _deserialize(expired_client.items[expired_job_key])["state"],
            "completed_with_errors",
        )

    def test_completed_next_renewal_can_enter_exact_schedule_rollback_only(self):
        store, client, item_key, job_key = self._pending_migration()
        applied = _deserialize(client.items[item_key])
        applied.update(
            {
                "state": "applied",
                "scheduleId": "sub_sched_synthetic",
                "rollbackGuard": rollback_guard(),
                "lastProviderEventCreatedAt": None,
                "lastProviderEventId": None,
            }
        )
        client.items[item_key] = _serialize(_with_migration_work_index(applied))
        completed = _deserialize(client.items[job_key])
        completed.update(
            {
                "state": "completed",
                "counts": {
                    "total": 1,
                    "pending": 0,
                    "applied": 1,
                    "needsReview": 0,
                    "failed": 0,
                },
                "canaryCompleted": 1,
                "awaitingProviderCount": 0,
                "appliedItemCount": 1,
            }
        )
        client.items[job_key] = _serialize(completed)
        client.queries.clear()
        store._query_items = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("terminal rollback must not enumerate the base table")
        )

        result = store.control(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-1",
            commercialRequestId="request-1",
            expectedRevision=3,
            action="cancel",
            idempotencyKeyHash="7" * 64,
            requestHash="8" * 64,
            commandId="rollback-completed",
            nowEpoch=NOW + 30,
        )

        self.assertEqual(result["revision"], 4)
        self.assertEqual(_deserialize(client.items[job_key])["state"], "cancel_requested")
        self.assertEqual(len(client.queries), 1)
        self.assertEqual(client.queries[0].get("IndexName"), "MigrationWorkIndex")
        self.assertEqual(client.queries[0].get("Limit"), 1)
        job_write = next(
            operation["Put"]
            for operation in client.transactions[-1]["TransactItems"]
            if operation.get("Put")
            and _deserialize(operation["Put"]["Item"]).get("itemType")
            == "SubscriptionMigrationJob"
        )
        self.assertIn("previewAggregate", job_write["ConditionExpression"])
        self.assertIn("appliedItemCount", job_write["ConditionExpression"])
        self.assertIn("#counts", job_write["ConditionExpression"])

        immediate_store, immediate_client, immediate_item_key, immediate_job_key = (
            self._pending_migration()
        )
        immediate_item = _deserialize(immediate_client.items[immediate_item_key])
        immediate_item.update({"state": "applied", "scheduleId": None})
        immediate_client.items[immediate_item_key] = _serialize(
            _with_migration_work_index(immediate_item)
        )
        immediate_job = _deserialize(immediate_client.items[immediate_job_key])
        immediate_job.update(
            {
                "state": "completed",
                "requestedPolicy": {"mode": "immediate_prorated"},
                "counts": {
                    "total": 1,
                    "pending": 0,
                    "applied": 1,
                    "needsReview": 0,
                    "failed": 0,
                },
                "canaryCompleted": 1,
                "awaitingProviderCount": 0,
                "appliedItemCount": 1,
            }
        )
        immediate_client.items[immediate_job_key] = _serialize(immediate_job)
        with self.assertRaises(MigrationStoreConflict):
            immediate_store.control(
                scope=SCOPE,
                connectionId="stripe-primary",
                jobId="migration-job-1",
                commercialRequestId="request-1",
                expectedRevision=3,
                action="cancel",
                idempotencyKeyHash="9" * 64,
                requestHash="0" * 64,
                commandId="rollback-immediate",
                nowEpoch=NOW + 30,
            )

    def test_cancellation_batches_only_indexed_work_and_finalizes_from_counters(self):
        store, client, _item_key, job_key = self._pending_migration()
        cancel_requested = _deserialize(client.items[job_key])
        cancel_requested["state"] = "cancel_requested"
        client.items[job_key] = _serialize(cancel_requested)

        canceling = store.begin_cancel(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-1",
            expectedRevision=3,
        )
        self.assertEqual(canceling["cancellationRemaining"], 1)
        client.queries.clear()

        items = store.cancellation_items(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-1",
            limit=1,
        )

        self.assertEqual(len(items), 1)
        self.assertTrue(client.queries)
        self.assertTrue(
            all(query.get("IndexName") == "MigrationWorkIndex" for query in client.queries)
        )
        store.complete_cancellation_item(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-1",
            itemId=items[0]["itemId"],
            state="needs_review",
            reasonCode="provider-unknown",
        )
        client.queries.clear()
        self.assertTrue(
            store.finalize_cancellation(
                scope=SCOPE,
                connectionId="stripe-primary",
                jobId="migration-job-1",
            )
        )
        self.assertTrue(client.queries)
        self.assertTrue(
            all(
                query.get("IndexName") == "MigrationWorkIndex"
                and query.get("Limit") == 1
                for query in client.queries
            )
        )
        self.assertEqual(_deserialize(client.items[job_key])["state"], "canceled")

    def test_cancellation_retry_is_conditional_idempotent_and_exhausts_at_five(self):
        store, client, item_key, job_key = self._pending_migration("applied")
        canceling = _deserialize(client.items[job_key])
        canceling.update(
            {
                "state": "canceling",
                "revision": 4,
                "cancellationRemaining": 1,
                "appliedItemCount": 1,
                "counts": {
                    "total": 1,
                    "pending": 0,
                    "applied": 1,
                    "needsReview": 0,
                    "failed": 0,
                },
                "canaryCompleted": 1,
            }
        )
        client.items[job_key] = _serialize(canceling)
        retry = getattr(store, "retry_cancellation_item", None)
        self.assertTrue(callable(retry), "durable cancellation retry is missing")

        self.assertTrue(
            retry(
                scope=SCOPE,
                connectionId="stripe-primary",
                jobId="migration-job-1",
                itemId=_deserialize(client.items[item_key])["itemId"],
                cancellationAttempts=1,
                nextAttemptAt=NOW + 23,
            )
        )
        persisted = _deserialize(client.items[item_key])
        self.assertEqual(persisted["cancellationAttempts"], 1)
        self.assertEqual(persisted["cancellationNextAttemptAt"], NOW + 23)
        item_write = next(
            operation["Put"]
            for operation in client.transactions[-1]["TransactItems"]
            if operation.get("Put")
            and _deserialize(operation["Put"]["Item"]).get("itemType")
            == "SubscriptionMigrationItem"
        )
        self.assertIn("cancellationAttempts", item_write["ConditionExpression"])
        transaction_count = len(client.transactions)
        self.assertTrue(
            retry(
                scope=SCOPE,
                connectionId="stripe-primary",
                jobId="migration-job-1",
                itemId=persisted["itemId"],
                cancellationAttempts=1,
                nextAttemptAt=NOW + 23,
            )
        )
        self.assertEqual(len(client.transactions), transaction_count)
        with self.assertRaises(MigrationStoreConflict):
            retry(
                scope=SCOPE,
                connectionId="stripe-primary",
                jobId="migration-job-1",
                itemId=persisted["itemId"],
                cancellationAttempts=1,
                nextAttemptAt=NOW + 24,
            )

        for attempt in range(2, 5):
            self.assertTrue(
                retry(
                    scope=SCOPE,
                    connectionId="stripe-primary",
                    jobId="migration-job-1",
                    itemId=persisted["itemId"],
                    cancellationAttempts=attempt,
                    nextAttemptAt=NOW + 20 + attempt,
                )
            )
        self.assertFalse(
            retry(
                scope=SCOPE,
                connectionId="stripe-primary",
                jobId="migration-job-1",
                itemId=persisted["itemId"],
                cancellationAttempts=5,
                nextAttemptAt=None,
            )
        )
        exhausted = _deserialize(client.items[item_key])
        self.assertEqual(exhausted["state"], "needs_review")
        self.assertEqual(exhausted["reasonCode"], "retry-exhausted")
        self.assertEqual(exhausted["cancellationAttempts"], 5)
        self.assertIsNone(exhausted["cancellationNextAttemptAt"])

    def test_preview_rejects_unknown_reason_code(self):
        self.store.create_preview(**preview_kwargs())
        try:
            self.store.put_preview_item(
                scope=SCOPE,
                connectionId="stripe-primary",
                jobId="migration-job-1",
                itemId="migration-item-" + "1" * 40,
                state="needs_review",
                reasonCode="arbitrary",
                attempts=0,
                providerSubscriptionId="sub_synthetic",
                snapshot=None,
                snapshotHash=None,
                prorationTimestamp=None,
                previewAmountMinor=None,
            )
        except MigrationStoreConflict:
            return
        except MigrationStoreError as error:
            self.fail(f"unknown reason was not rejected at the command boundary: {error}")
        self.fail("unknown migration reason was accepted")

    def test_cancellation_counter_excludes_already_skipped_and_reverted_items(self):
        store, client, item_key, job_key = self._pending_migration()
        source = _deserialize(client.items[item_key])

        def terminal_item(provider_id, state, schedule_id=None):
            selected = copy.deepcopy(source)
            item_id = "migration-item-" + hashlib.sha256(
                provider_id.encode("ascii")
            ).hexdigest()[:40]
            selected.update(
                {
                    "sk": f"MIGRATION_ITEM#stripe-primary#migration-job-1#{item_id}",
                    "itemId": item_id,
                    "providerSubscriptionId": provider_id,
                    "state": state,
                    "scheduleId": schedule_id,
                    "rollbackGuard": (
                        rollback_guard()
                        if state == "applied" and schedule_id is not None
                        else None
                    ),
                    "leaseExpiresAt": None,
                    "nextAttemptAt": None,
                    "accountKeyHash": None,
                    "accountSlot": None,
                    "canary": False,
                }
            )
            selected["snapshot"]["subscriptionId"] = provider_id
            selected["snapshotHash"] = migration_snapshot_hash(selected["snapshot"])
            return _with_migration_work_index(selected)

        applied = terminal_item("sub_applied", "applied", "sub_sched_applied")
        skipped = terminal_item("sub_skipped", "skipped")
        reverted = terminal_item("sub_reverted", "reverted")
        client.items.pop(item_key)
        for selected in (applied, skipped, reverted):
            client.items[("registry", selected["pk"], selected["sk"])] = _serialize(
                selected
            )
        cancel_requested = _deserialize(client.items[job_key])
        cancel_requested.update(
            {
                "state": "cancel_requested",
                "counts": {
                    "total": 3,
                    "pending": 0,
                    "applied": 3,
                    "needsReview": 0,
                    "failed": 0,
                },
                "canaryClaims": 0,
                "canaryCompleted": 0,
                "mutationStarted": False,
                "appliedItemCount": 1,
                "awaitingProviderCount": 0,
            }
        )
        client.items[job_key] = _serialize(cancel_requested)

        canceling = store.begin_cancel(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-1",
            expectedRevision=3,
        )
        self.assertEqual(canceling["cancellationRemaining"], 1)
        items = store.cancellation_items(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-1",
            limit=25,
        )
        self.assertEqual([item["itemId"] for item in items], [applied["itemId"]])
        store.complete_cancellation_item(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-1",
            itemId=applied["itemId"],
            state="reverted",
            reasonCode=None,
        )
        store.complete_cancellation_item(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-1",
            itemId=applied["itemId"],
            state="needs_review",
            reasonCode="provider-unknown",
        )
        self.assertTrue(
            store.finalize_cancellation(
                scope=SCOPE,
                connectionId="stripe-primary",
                jobId="migration-job-1",
            )
        )
        final = _deserialize(client.items[job_key])
        self.assertEqual(final["cancellationRemaining"], 0)
        self.assertEqual(final["appliedItemCount"], 0)

    def test_stale_expired_webhook_cannot_poison_a_new_or_applying_item(self):
        store, client, item_key, _ = self._pending_migration("applying")
        applying = _deserialize(client.items[item_key])
        applying.update(
            {
                "leaseExpiresAt": NOW + 120,
                "accountKeyHash": "d" * 64,
                "accountSlot": 0,
            }
        )
        client.items[item_key] = _serialize(_with_migration_work_index(applying))
        result = store.reconcile_migration_webhook(
            scope=SCOPE,
            connectionId="stripe-primary",
            providerSubscriptionId="sub_synthetic",
            eventId="evt-old-expired",
            eventType="customer.subscription.pending_update_expired",
            eventCreatedAt=NOW + 10,
            priceIds=["price_old", "price_addon"],
            pendingUpdate=False,
        )
        self.assertEqual(result["state"], "applying")
        self.assertEqual(_deserialize(client.items[item_key])["state"], "applying")

    def test_fifth_retryable_attempt_becomes_terminal_and_releases_its_lease(self):
        store, client, item_key, _ = self._pending_migration("applying")
        applying = _deserialize(client.items[item_key])
        applying.update(
            {
                "attempts": 5,
                "leaseExpiresAt": NOW + 120,
                "accountKeyHash": "d" * 64,
                "accountSlot": 0,
            }
        )
        client.items[item_key] = _serialize(_with_migration_work_index(applying))

        retry = store.retry_item(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-1",
            itemId=applying["itemId"],
            attempts=5,
            reasonCode="retry-exhausted",
            nextAttemptAt=None,
            scheduleId=None,
        )

        self.assertFalse(retry)
        terminal = _deserialize(client.items[item_key])
        self.assertEqual(terminal["state"], "needs_review")
        self.assertEqual(terminal["reasonCode"], "retry-exhausted")
        self.assertIsNone(terminal["leaseExpiresAt"])

    def test_retryable_item_keeps_a_closed_reason_and_bounded_due_index(self):
        store, client, item_key, _ = self._pending_migration("applying")
        applying = _deserialize(client.items[item_key])
        applying.update(
            {
                "leaseExpiresAt": NOW + 120,
                "accountKeyHash": "d" * 64,
                "accountSlot": 0,
            }
        )
        client.items[item_key] = _serialize(_with_migration_work_index(applying))

        self.assertTrue(
            store.retry_item(
                scope=SCOPE,
                connectionId="stripe-primary",
                jobId="migration-job-1",
                itemId=applying["itemId"],
                attempts=1,
                reasonCode="provider-unknown",
                nextAttemptAt=NOW + 5,
                scheduleId=None,
            )
        )
        retrying = _deserialize(client.items[item_key])
        self.assertEqual(retrying["state"], "retryable_failure")
        self.assertEqual(retrying["reasonCode"], "provider-unknown")
        self.assertEqual(retrying["nextAttemptAt"], NOW + 5)
        self.assertIn("migrationWorkPk", retrying)

    def test_preview_atomically_writes_business_job_without_ttl_and_technical_receipt_with_ttl(self):
        job, created = self.store.create_preview(**preview_kwargs())

        self.assertTrue(created)
        self.assertEqual(job["state"], "previewing")
        writes = self.client.transactions[0]["TransactItems"]
        records = {
            operation["Put"]["TableName"]: _deserialize(operation["Put"]["Item"])
            for operation in writes
        }
        self.assertEqual(records["registry"]["itemType"], "SubscriptionMigrationJob")
        self.assertNotIn("expiresAt", records["registry"])
        self.assertEqual(records["technical"]["itemType"], "MigrationCommandReceipt")
        self.assertEqual(records["technical"]["expiresAt"], NOW + 90 * 24 * 60 * 60)
        self.assertNotIn("price_old", repr(records["technical"]))
        self.assertNotIn("idempotencyKey", repr(records))

    def test_exact_replay_is_idempotent_and_same_claim_different_hash_conflicts(self):
        self.store.create_preview(**preview_kwargs())
        replay, created = self.store.create_preview(**preview_kwargs())
        self.assertFalse(created)
        self.assertEqual(replay["jobId"], "migration-job-1")

        changed = preview_kwargs()
        changed["requestHash"] = "c" * 64
        with self.assertRaises(MigrationStoreConflict):
            self.store.create_preview(**changed)

    def test_preview_replay_returns_the_receipted_revision_after_job_advanced(self):
        self.store.create_preview(**preview_kwargs())
        key = (
            "registry",
            SCOPE.partition_key,
            "MIGRATION_JOB#stripe-primary#migration-job-1",
        )
        advanced = _deserialize(self.client.items[key])
        advanced.update(
            {
                "state": "awaiting_approval",
                "revision": 2,
                "dryRunRevision": 1,
                "dryRunHash": "c" * 64,
                "previewExpiresAt": NOW + 86_400,
            }
        )
        self.client.items[key] = _serialize(advanced)

        replay, created = self.store.create_preview(**preview_kwargs())

        self.assertFalse(created)
        self.assertEqual(replay["revision"], 1)

    def test_preview_summary_is_incremental_idempotent_and_never_queries_all_items(self):
        self.store.create_preview(**preview_kwargs())
        selected_snapshot = snapshot()
        arguments = {
            "scope": SCOPE,
            "connectionId": "stripe-primary",
            "jobId": "migration-job-1",
            "itemId": "migration-item-"
            + hashlib.sha256(b"sub_synthetic").hexdigest()[:40],
            "state": "pending",
            "reasonCode": None,
            "attempts": 0,
            "providerSubscriptionId": "sub_synthetic",
            "snapshot": selected_snapshot,
            "snapshotHash": migration_snapshot_hash(selected_snapshot),
            "prorationTimestamp": None,
            "previewAmountMinor": None,
        }

        self.store.put_preview_item(**arguments)
        first = self.store.preview_summary(
            SCOPE, "stripe-primary", "migration-job-1"
        )
        self.store.put_preview_item(**arguments)
        replay = self.store.preview_summary(
            SCOPE, "stripe-primary", "migration-job-1"
        )

        self.assertEqual(first, replay)
        self.assertEqual(
            first["counts"],
            {
                "total": 1,
                "pending": 1,
                "applied": 0,
                "needsReview": 0,
                "failed": 0,
            },
        )
        self.assertEqual(self.client.queries, [])
        with self.assertRaises(MigrationStoreConflict):
            self.store.put_preview_item(
                **{
                    **arguments,
                    "state": "needs_review",
                    "reasonCode": "source-drift",
                }
            )
        with self.assertRaises(MigrationStoreConflict):
            self.store.complete_preview(
                scope=SCOPE,
                connectionId="stripe-primary",
                jobId="migration-job-1",
                expectedRevision=1,
                dryRunHash="0" * 64,
                counts=first["counts"],
                expiresAt=NOW + 86_400,
            )
        self.store.complete_preview(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-1",
            expectedRevision=1,
            dryRunHash=first["dryRunHash"],
            counts=first["counts"],
            expiresAt=NOW + 86_400,
        )

    def test_corrupt_command_receipt_scope_or_ttl_is_rejected(self):
        self.store.create_preview(**preview_kwargs())
        key = next(
            identity
            for identity in self.client.items
            if identity[0] == "technical"
            and identity[2].startswith("MIGRATION_COMMAND#")
        )
        receipt = _deserialize(self.client.items[key])
        receipt["expiresAt"] += 1
        self.client.items[key] = _serialize(receipt)
        with self.assertRaises(MigrationStoreConflict):
            self.store.create_preview(**preview_kwargs())

    def test_status_returns_only_protected_safe_items_with_opaque_cursor(self):
        self.store.create_preview(**preview_kwargs())
        item_id = "migration-item-" + hashlib.sha256(b"sub_private").hexdigest()[:40]
        item = {
            "pk": SCOPE.partition_key,
            "sk": f"MIGRATION_ITEM#stripe-primary#migration-job-1#{item_id}",
            "itemType": "SubscriptionMigrationItem",
            **SCOPE.fields(),
            "connectionId": "stripe-primary",
            "jobId": "migration-job-1",
            "itemId": item_id,
            "state": "needs_review",
            "reasonCode": "unmapped-price",
            "attempts": 0,
            "providerSubscriptionId": "sub_private",
            "snapshot": None,
            "snapshotHash": None,
            "prorationTimestamp": None,
            "previewAmountMinor": None,
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
        self.client.query_response = {
            "Items": [_serialize(item)],
            "LastEvaluatedKey": _serialize({"pk": item["pk"], "sk": item["sk"]}),
        }

        result = self.store.status(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-1",
            commercialRequestId="request-1",
            limit=25,
            cursor=None,
        )

        self.assertEqual(
            result["items"],
            [{"itemId": item_id, "state": "needs_review", "reasonCode": "unmapped-price", "attempts": 0}],
        )
        self.assertEqual(result["nextCursor"], item_id)
        self.assertNotIn("sub_private", repr(result))

    def test_job_rejects_same_partition_key_with_a_different_domain(self):
        self.store.create_preview(**preview_kwargs())
        other_scope = IntegrationScope(
            SCOPE.environment,
            SCOPE.tenant_id,
            SCOPE.draft_id,
            "other.example.com",
        )

        with self.assertRaises(MigrationStoreError):
            self.store.get_job(
                scope=other_scope,
                connectionId="stripe-primary",
                jobId="migration-job-1",
                commercialRequestId="request-1",
            )

    def test_cancel_before_mutation_is_terminal_without_provider_reconciliation(self):
        self.store.create_preview(**preview_kwargs())
        result = self.store.control(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-1",
            commercialRequestId="request-1",
            expectedRevision=1,
            action="cancel",
            idempotencyKeyHash="c" * 64,
            requestHash="d" * 64,
            commandId="command-2",
            nowEpoch=NOW,
        )
        self.assertEqual(result["revision"], 2)
        status = self.store.get_job(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-1",
            commercialRequestId="request-1",
        )
        self.assertEqual(status["state"], "canceled")
        outbox = next(
            _deserialize(operation["Put"]["Item"])
            for operation in self.client.transactions[-1]["TransactItems"]
            if operation.get("Put")
            and _deserialize(operation["Put"]["Item"]).get("itemType")
            == "IntegrationEventOutbox"
        )
        self.assertEqual(outbox["eventType"], "migration.completed.v1")
        self.assertEqual(outbox["eventEnvelope"]["data"]["state"], "canceled")

    def test_event_outbox_uses_transition_time_not_an_old_job_creation_time(self):
        current = NOW + 200 * 24 * 60 * 60
        client = Dynamo()
        store = DynamoMigrationStore(
            "registry", "technical", client=client, now_epoch=lambda: current
        )
        old = preview_kwargs()
        old["createdAt"] = NOW
        store.create_preview(**old)
        summary = store.preview_summary(SCOPE, "stripe-primary", "migration-job-1")
        store.complete_preview(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-1",
            expectedRevision=1,
            dryRunHash=summary["dryRunHash"],
            counts=summary["counts"],
            expiresAt=current + 86_400,
        )
        outbox = next(
            _deserialize(operation["Put"]["Item"])
            for operation in client.transactions[-1]["TransactItems"]
            if operation.get("Put")
            and _deserialize(operation["Put"]["Item"]).get("itemType")
            == "IntegrationEventOutbox"
        )
        self.assertEqual(outbox["createdAt"], current)
        self.assertEqual(outbox["eventEnvelope"]["occurredAt"], current)
        self.assertEqual(outbox["expiresAt"], current + 90 * 24 * 60 * 60)

    def test_offer_reference_guard_stays_closed_even_with_stale_zero_reference_coverage(self):
        guard = DynamoOfferReferenceGuard("registry", client=self.client)
        self.assertFalse(
            guard.can_deactivate(SCOPE, "stripe-primary", "offer-old", "price_old")
        )
        record = {
            "pk": SCOPE.partition_key,
            "sk": "MIGRATION_OFFER_COVERAGE#stripe-primary#offer-old",
            "itemType": "MigrationOfferCoverage",
            **SCOPE.fields(),
            "connectionId": "stripe-primary",
            "offerVersionId": "offer-old",
            "priceHash": hashlib.sha256(b"price_old").hexdigest(),
            "complete": True,
            "activeReferenceCount": 0,
            "scheduleReferenceCount": 0,
            "pendingUpdateReferenceCount": 0,
            "revision": 1,
        }
        self.client.items[("registry", record["pk"], record["sk"])] = _serialize(record)
        self.assertFalse(
            guard.can_deactivate(SCOPE, "stripe-primary", "offer-old", "price_old")
        )
        record["activeReferenceCount"] = 1
        self.client.items[("registry", record["pk"], record["sk"])] = _serialize(record)
        self.assertFalse(
            guard.can_deactivate(SCOPE, "stripe-primary", "offer-old", "price_old")
        )

    def test_migration_membership_preserves_an_existing_checkout_owner(self):
        provider_id = "sub_synthetic"
        provider_hash = hashlib.sha256(provider_id.encode("ascii")).hexdigest()
        owner = {
            "pk": SCOPE.partition_key,
            "sk": f"STRIPEOBJECT#stripe-primary#subscription#{provider_hash}",
            "itemType": "StripeObjectIndex",
            "connectionId": "stripe-primary",
            "objectType": "subscription",
            "providerIdHash": provider_hash,
            "resourceType": "checkout",
            "resourceId": "payment-attempt-1",
        }
        mapping = {
            "pk": SCOPE.partition_key,
            "sk": "STRIPEMAP#stripe-primary#checkout#payment-attempt-1",
            "itemType": "StripeResourceMapping",
            "connectionId": "stripe-primary",
            "resourceType": "checkout",
            "resourceId": "payment-attempt-1",
            "providerSubscriptionId": provider_id,
            "offerVersionIds": ["offer-old"],
            "primaryOfferVersionId": "offer-old",
        }
        for record in (owner, mapping):
            self.client.items[("registry", record["pk"], record["sk"])] = _serialize(
                record
            )

        self.store.bind_migration_subscription(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-1",
            itemId="migration-item-1",
            providerSubscriptionId=provider_id,
            offerVersionIds=["offer-old"],
            primaryOfferVersionId="offer-old",
        )

        self.assertEqual(
            _deserialize(self.client.items[("registry", owner["pk"], owner["sk"])]),
            owner,
        )
        self.assertFalse(
            any(
                identity[0] == "registry"
                and identity[2].startswith(
                    "STRIPEMAP#stripe-primary#migration-subscription#"
                )
                for identity in self.client.items
            )
        )
        self.assertIn(
            (
                "registry",
                SCOPE.partition_key,
                f"MIGRATION_SUBSCRIPTION#stripe-primary#{provider_hash}#migration-job-1",
            ),
            self.client.items,
        )
    def test_active_overlay_is_lifecycle_scoped_and_fails_closed_on_review(self):
        store, client, item_key, job_key = self._pending_migration("applied")
        completed = _deserialize(client.items[job_key])
        completed.update(
            {
                "state": "completed",
                "requestedPolicy": {"mode": "immediate_prorated"},
                "counts": {
                    "total": 1,
                    "pending": 0,
                    "applied": 1,
                    "needsReview": 0,
                    "failed": 0,
                },
                "canaryCompleted": 1,
                "appliedItemCount": 1,
            }
        )
        client.items[job_key] = _serialize(completed)

        active = store.active_migration(SCOPE, "stripe-primary", "sub_synthetic")
        self.assertEqual(active["offerVersionIds"], ["offer-addon", "offer-new"])

        reverted = _deserialize(client.items[item_key])
        reverted.update({"state": "reverted", "reasonCode": None})
        client.items[item_key] = _serialize(_with_migration_work_index(reverted))
        canceled = _deserialize(client.items[job_key])
        canceled.update(
            {
                "state": "canceled",
                "counts": {
                    "total": 1,
                    "pending": 0,
                    "applied": 1,
                    "needsReview": 0,
                    "failed": 0,
                },
                "appliedItemCount": 0,
            }
        )
        client.items[job_key] = _serialize(canceled)
        active = store.active_migration(SCOPE, "stripe-primary", "sub_synthetic")
        self.assertEqual(
            set(active["offerVersionIds"]), {"offer-old", "offer-addon"}
        )

        review = _deserialize(client.items[item_key])
        review.update({"state": "needs_review", "reasonCode": "source-drift"})
        client.items[item_key] = _serialize(_with_migration_work_index(review))
        completed_with_errors = _deserialize(client.items[job_key])
        completed_with_errors.update(
            {
                "state": "completed_with_errors",
                "counts": {
                    "total": 1,
                    "pending": 0,
                    "applied": 0,
                    "needsReview": 1,
                    "failed": 0,
                },
                "appliedItemCount": 0,
            }
        )
        client.items[job_key] = _serialize(completed_with_errors)
        self.assertIsNone(
            store.active_migration(SCOPE, "stripe-primary", "sub_synthetic")
        )

    def test_migration_import_creates_an_owner_but_rejects_a_conflicting_owner(self):
        provider_id = "sub_synthetic"
        provider_hash = hashlib.sha256(provider_id.encode("ascii")).hexdigest()
        arguments = {
            "scope": SCOPE,
            "connectionId": "stripe-primary",
            "jobId": "migration-job-1",
            "itemId": "migration-item-1",
            "providerSubscriptionId": provider_id,
            "offerVersionIds": ["offer-old"],
            "primaryOfferVersionId": "offer-old",
        }
        self.store.bind_migration_subscription(**arguments)
        self.assertIn(
            ("registry", SCOPE.partition_key, f"STRIPEOBJECT#stripe-primary#subscription#{provider_hash}"),
            self.client.items,
        )

        other_client = Dynamo()
        other_store = DynamoMigrationStore("registry", "technical", client=other_client)
        owner = {
            "pk": SCOPE.partition_key,
            "sk": f"STRIPEOBJECT#stripe-primary#subscription#{provider_hash}",
            "itemType": "StripeObjectIndex",
            "connectionId": "stripe-primary",
            "objectType": "subscription",
            "providerIdHash": provider_hash,
            "resourceType": "checkout",
            "resourceId": "other-payment",
        }
        other_client.items[("registry", owner["pk"], owner["sk"])] = _serialize(owner)
        with self.assertRaises(MigrationStoreConflict):
            other_store.bind_migration_subscription(**arguments)

    def test_terminal_job_allows_a_reverse_membership_but_active_jobs_conflict(self):
        self.store.create_preview(**preview_kwargs())
        provider_id = "sub_synthetic"
        provider_hash = hashlib.sha256(provider_id.encode("ascii")).hexdigest()
        first = {
            "scope": SCOPE,
            "connectionId": "stripe-primary",
            "jobId": "migration-job-1",
            "itemId": "migration-item-1",
            "providerSubscriptionId": provider_id,
            "offerVersionIds": ["offer-old"],
            "primaryOfferVersionId": "offer-old",
        }
        self.store.bind_migration_subscription(**first)
        second_preview = {
            **preview_kwargs(),
            "jobId": "migration-job-2",
            "commercialRequestId": "request-2",
            "idempotencyKeyHash": "2" * 64,
            "requestHash": "3" * 64,
            "commandId": "command-2",
        }
        self.store.create_preview(**second_preview)
        second = {
            **first,
            "jobId": "migration-job-2",
            "offerVersionIds": ["offer-new"],
            "primaryOfferVersionId": "offer-new",
        }
        with self.assertRaises(MigrationStoreConflict):
            self.store.bind_migration_subscription(**second)

        first_job_key = (
            "registry",
            SCOPE.partition_key,
            "MIGRATION_JOB#stripe-primary#migration-job-1",
        )
        terminal = _deserialize(self.client.items[first_job_key])
        terminal.update(
            {
                "state": "completed",
                "revision": 4,
                "dryRunRevision": 1,
                "dryRunHash": "c" * 64,
                "previewExpiresAt": NOW + 86_400,
                "taxAuthorization": {
                    "taxMode": "manual-rate",
                    "approvalHash": None,
                },
            }
        )
        self.client.items[first_job_key] = _serialize(terminal)

        self.store.bind_migration_subscription(**second)

        active_key = (
            "registry",
            SCOPE.partition_key,
            f"MIGRATION_ACTIVE_SUBSCRIPTION#stripe-primary#{provider_hash}",
        )
        self.assertEqual(
            _deserialize(self.client.items[active_key])["jobId"], "migration-job-2"
        )
        self.assertIn(
            (
                "registry",
                SCOPE.partition_key,
                f"MIGRATION_SUBSCRIPTION#stripe-primary#{provider_hash}#migration-job-1",
            ),
            self.client.items,
        )
        self.assertIn(
            (
                "registry",
                SCOPE.partition_key,
                f"MIGRATION_SUBSCRIPTION#stripe-primary#{provider_hash}#migration-job-2",
            ),
            self.client.items,
        )

    def test_two_preview_jobs_make_the_second_active_membership_conflict_durable(self):
        self.client.dynamic_query = True
        self.store.create_preview(**preview_kwargs())
        second_preview = {
            **preview_kwargs(),
            "jobId": "migration-job-2",
            "commercialRequestId": "request-2",
            "idempotencyKeyHash": "2" * 64,
            "requestHash": "3" * 64,
            "commandId": "command-2",
        }
        self.store.create_preview(**second_preview)
        provider_id = "sub_synthetic"
        item_id = "migration-item-" + hashlib.sha256(
            provider_id.encode("ascii")
        ).hexdigest()[:40]
        selected_snapshot = snapshot()
        for job_id in ("migration-job-1", "migration-job-2"):
            self.store.put_preview_item(
                scope=SCOPE,
                connectionId="stripe-primary",
                jobId=job_id,
                itemId=item_id,
                state="pending",
                reasonCode=None,
                attempts=0,
                providerSubscriptionId=provider_id,
                snapshot=selected_snapshot,
                snapshotHash=migration_snapshot_hash(selected_snapshot),
                prorationTimestamp=None,
                previewAmountMinor=None,
            )
        membership = {
            "scope": SCOPE,
            "connectionId": "stripe-primary",
            "itemId": item_id,
            "providerSubscriptionId": provider_id,
            "offerVersionIds": ["offer-old", "offer-addon"],
            "primaryOfferVersionId": "offer-old",
        }
        self.store.bind_migration_subscription(
            **membership, jobId="migration-job-1"
        )
        with self.assertRaises(MigrationStoreConflict):
            self.store.bind_migration_subscription(
                **membership, jobId="migration-job-2"
            )

        rejected = self.store.reject_preview_item(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-2",
            itemId=item_id,
            reasonCode="scope-mismatch",
        )

        self.assertEqual(rejected["state"], "needs_review")
        summary = self.store.preview_summary(
            SCOPE, "stripe-primary", "migration-job-2"
        )
        self.assertEqual(
            summary["counts"],
            {
                "total": 1,
                "pending": 0,
                "applied": 0,
                "needsReview": 1,
                "failed": 0,
            },
        )
    def test_canary_admission_is_durable_pauses_at_n_and_resume_opens_bulk(self):
        self.store.create_preview(**{**preview_kwargs(), "canarySize": 3})
        job_key = (
            "registry",
            SCOPE.partition_key,
            "MIGRATION_JOB#stripe-primary#migration-job-1",
        )
        item_keys = []
        for index in range(4):
            provider_id = f"sub_synthetic_{index}"
            item_id = "migration-item-" + hashlib.sha256(
                provider_id.encode("ascii")
            ).hexdigest()[:40]
            selected_snapshot = snapshot()
            selected_snapshot["subscriptionId"] = provider_id
            self.store.put_preview_item(
                scope=SCOPE,
                connectionId="stripe-primary",
                jobId="migration-job-1",
                itemId=item_id,
                state="pending",
                reasonCode=None,
                attempts=0,
                providerSubscriptionId=provider_id,
                snapshot=selected_snapshot,
                snapshotHash=migration_snapshot_hash(selected_snapshot),
                prorationTimestamp=None,
                previewAmountMinor=None,
            )
            item_keys.append(
                (
                    "registry",
                    SCOPE.partition_key,
                    f"MIGRATION_ITEM#stripe-primary#migration-job-1#{item_id}",
                )
            )
        running = _deserialize(self.client.items[job_key])
        running.update(
            {
                "state": "running",
                "revision": 3,
                "dryRunRevision": 1,
                "dryRunHash": "c" * 64,
                "previewExpiresAt": NOW + 86_400,
                "taxAuthorization": {
                    "taxMode": "manual-rate",
                    "approvalHash": None,
                },
                "counts": {
                    "total": 4,
                    "pending": 4,
                    "applied": 0,
                    "needsReview": 0,
                    "failed": 0,
                },
            }
        )
        self.client.items[job_key] = _serialize(running)
        self.client.dynamic_query = True
        arguments = {
            "scope": SCOPE,
            "connectionId": "stripe-primary",
            "jobId": "migration-job-1",
            "limit": 3,
            "maxAttempts": 5,
            "accountConcurrency": 5,
            "accountKeyHash": "d" * 64,
            "nowEpoch": NOW,
            "leaseSeconds": 120,
            "expectedJobRevision": 3,
            "canaryLimit": 3,
        }

        claimed = self.store.claim_items(**arguments)
        duplicate = self.store.claim_items(**arguments)

        self.assertEqual(len(claimed), 3)
        self.assertEqual(duplicate, [])
        self.assertTrue(self.client.queries)
        self.assertTrue(
            all(
                query.get("IndexName") == "MigrationWorkIndex"
                and query.get("Limit", 0) <= arguments["limit"]
                for query in self.client.queries
            )
        )
        current_job = self.store.load_job(SCOPE, "stripe-primary", "migration-job-1")
        self.assertEqual(current_job["canaryClaims"], 3)
        self.assertTrue(current_job["mutationStarted"])

        for item in claimed:
            self.store.complete_item(
                scope=SCOPE,
                connectionId="stripe-primary",
                jobId="migration-job-1",
                itemId=item["itemId"],
                attempts=item["attempts"],
                state="applied",
                reasonCode=None,
                scheduleId="sub_sched_synthetic",
                rollbackGuard=rollback_guard(),
            )
        self.client.queries.clear()
        continuation = self.store.continue_execution(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-1",
            nowEpoch=NOW + 1,
        )
        self.assertIsNone(continuation)
        self.assertTrue(self.client.queries)
        self.assertTrue(
            all(
                query.get("IndexName") == "MigrationWorkIndex"
                and query.get("Limit") == 1
                for query in self.client.queries
            )
        )
        paused = self.store.load_job(SCOPE, "stripe-primary", "migration-job-1")
        self.assertEqual(paused["state"], "paused")
        self.assertTrue(paused["canaryApprovalRequired"])

        control = self.store.control(
            scope=SCOPE,
            connectionId="stripe-primary",
            jobId="migration-job-1",
            commercialRequestId="request-1",
            expectedRevision=paused["revision"],
            action="resume",
            idempotencyKeyHash="e" * 64,
            requestHash="f" * 64,
            commandId="command-resume",
            nowEpoch=NOW + 2,
        )
        resumed = self.store.load_job(SCOPE, "stripe-primary", "migration-job-1")
        self.assertEqual(control["revision"], resumed["revision"])
        self.assertTrue(resumed["canaryApproved"])
        self.assertFalse(resumed["canaryApprovalRequired"])

        bulk = self.store.claim_items(
            **{
                **arguments,
                "limit": 5,
                "expectedJobRevision": resumed["revision"],
                "canaryLimit": 0,
                "nowEpoch": NOW + 3,
            }
        )
        self.assertEqual(len(bulk), 1)


class QueueTests(unittest.TestCase):
    def test_sqs_queue_sends_only_closed_redacted_messages_and_bounded_delay(self):
        class Sqs:
            def __init__(self):
                self.calls = []

            def send_message(self, **kwargs):
                self.calls.append(kwargs)

        sqs = Sqs()
        queue = SqsMigrationQueue("https://sqs.example/queue", client=sqs)
        message = {
            "version": 1,
            **SCOPE.fields(),
            "connectionId": "stripe-primary",
            "jobId": "migration-job-1",
            "action": "execute",
            "revision": 2,
        }
        queue.send(message, delay_seconds=60)
        self.assertEqual(sqs.calls[0]["DelaySeconds"], 60)
        self.assertNotIn("provider", sqs.calls[0]["MessageBody"])
        with self.assertRaises(Exception):
            queue.send({**message, "subscriptionId": "sub_forbidden"})


if __name__ == "__main__":
    unittest.main()
