import copy
import hashlib
import json
import unittest
from types import SimpleNamespace

from src.domain.integrations import IntegrationScope
from src.handlers.subscription_migration_worker import (
    MigrationRetryable,
    SubscriptionMigrationWorker,
    _migration_rollback_guard,
    handle_records,
)
from src.subscription_migrations import migration_snapshot_hash
from src.subscription_migrations import build_next_renewal_plan
from src.migration_store import MigrationStoreConflict
from tests.test_subscription_migration_domain import snapshot
from tests.test_migration_contracts import offer


SCOPE = IntegrationScope("test", "tenant-example", "draft-example", "example.com")


def message(action="preview", revision=1):
    return {
        "version": 1,
        **SCOPE.fields(),
        "connectionId": "stripe-primary",
        "jobId": "migration-job-1",
        "action": action,
        "revision": revision,
    }


def job(mode="next_renewal", state="previewing"):
    return {
        **SCOPE.fields(),
        "connectionId": "stripe-primary",
        "jobId": "migration-job-1",
        "commercialRequestId": "request-1",
        "state": state,
        "revision": 1 if state == "previewing" else 3,
        "sourceOffer": offer("offer-old", 90_000),
        "targetOffer": offer("offer-new", 100_000),
        "sourcePriceId": "price_old",
        "targetPriceId": "price_new",
        "requestedPolicy": {"mode": mode},
        "candidateScope": {"kind": "all_matching_source_price"},
        "canarySize": 5,
        "accountConcurrency": 2,
        "discoveryCursor": None,
        "taxAuthorization": {"taxMode": "manual-rate", "approvalHash": None},
        "mutationStarted": False,
        "counts": {"total": 0, "pending": 0, "applied": 0, "needsReview": 0, "failed": 0},
    }


class Resolver:
    def resolve(self, scope, connection_id, **kwargs):
        self.call = (scope, connection_id, kwargs)
        return SimpleNamespace(
            connection=SimpleNamespace(
                mode="test",
                provider_metadata={"accountReference": "acct_synthetic"},
            )
        )


class Mappings:
    def __init__(self):
        self.prices = {
            "price_old": {
                "resourceType": "offer",
                "resourceId": "offer-old",
                "priceId": "price_old",
                "revision": 2,
                "contentHash": offer("offer-old", 90_000)["contentHash"],
                "status": "existing_only",
            },
            "price_new": {
                "resourceType": "offer",
                "resourceId": "offer-new",
                "priceId": "price_new",
                "revision": 2,
                "contentHash": offer("offer-new", 100_000)["contentHash"],
                "status": "active",
            },
            "price_addon": {
                "resourceType": "offer",
                "resourceId": "offer-addon",
                "priceId": "price_addon",
                "revision": 1,
                "contentHash": "f" * 64,
                "status": "active",
            },
        }

    def object_owner(self, scope, connection_id, object_type, provider_id):
        if scope != SCOPE or connection_id != "stripe-primary" or object_type != "price":
            raise AssertionError("wrong ownership scope")
        return copy.deepcopy(self.prices.get(provider_id))


class Store:
    def __init__(self, selected_job):
        self.job = copy.deepcopy(selected_job)
        self.items = []
        self.bindings = []
        self.completed_preview = []
        self.claimed = set()
        self.outcomes = []
        self.retry_calls = []
        self.cancellation_retry_calls = []
        self.cancel_calls = []
        self.preview_rejections = []

    def load_job(self, scope, connection_id, job_id):
        if scope != SCOPE or connection_id != "stripe-primary" or job_id != self.job["jobId"]:
            return None
        return copy.deepcopy(self.job)

    def bind_migration_subscription(self, **kwargs):
        self.bindings.append(copy.deepcopy(kwargs))

    def put_preview_item(self, **kwargs):
        persisted = copy.deepcopy(kwargs)
        self.items = [
            item for item in self.items if item["itemId"] != persisted["itemId"]
        ]
        self.items.append(persisted)
        return copy.deepcopy(persisted)

    def advance_preview(self, **kwargs):
        self.job["discoveryCursor"] = kwargs["cursor"]
        self.job["revision"] += 1
        return copy.deepcopy(self.job)

    def complete_preview(self, **kwargs):
        self.completed_preview.append(copy.deepcopy(kwargs))
        self.job["state"] = "awaiting_approval"
        self.job["revision"] += 1
        self.job["dryRunRevision"] = 1
        self.job["dryRunHash"] = kwargs["dryRunHash"]
        self.job["previewExpiresAt"] = kwargs["expiresAt"]

    def reject_preview_item(self, **kwargs):
        self.preview_rejections.append(copy.deepcopy(kwargs))
        item = next(item for item in self.items if item["itemId"] == kwargs["itemId"])
        item["state"] = "needs_review"
        item["reasonCode"] = kwargs["reasonCode"]
        return copy.deepcopy(item)

    def start_execution(self, **kwargs):
        self.job["state"] = "running"
        self.job["revision"] += 1
        return copy.deepcopy(self.job)

    def claim_items(self, **kwargs):
        selected = []
        for item in self.items:
            if item["itemId"] not in self.claimed and item["state"] in {"pending", "retryable_failure"}:
                self.claimed.add(item["itemId"])
                claimed = copy.deepcopy(item)
                claimed["state"] = "applying"
                claimed["attempts"] += 1
                selected.append(claimed)
                if len(selected) == kwargs["limit"]:
                    break
        return selected

    def complete_item(self, **kwargs):
        self.outcomes.append(copy.deepcopy(kwargs))

    def retry_item(self, **kwargs):
        self.retry_calls.append(copy.deepcopy(kwargs))
        return kwargs["attempts"] < 5

    def continue_execution(self, **kwargs):
        return None

    def begin_cancel(self, **kwargs):
        self.cancel_calls.append(copy.deepcopy(kwargs))
        self.job["state"] = "canceling"
        self.job["revision"] += 1
        return copy.deepcopy(self.job)

    def cancellation_items(self, **kwargs):
        return [copy.deepcopy(item) for item in self.items if item["state"] in {"applying", "pending_payment", "pending_customer_action", "applied"}]

    def complete_cancellation_item(self, **kwargs):
        self.outcomes.append(copy.deepcopy(kwargs))

    def retry_cancellation_item(self, **kwargs):
        self.cancellation_retry_calls.append(copy.deepcopy(kwargs))
        item = next(item for item in self.items if item["itemId"] == kwargs["itemId"])
        item["cancellationAttempts"] = kwargs["cancellationAttempts"]
        item["cancellationNextAttemptAt"] = kwargs["nextAttemptAt"]
        if kwargs["cancellationAttempts"] >= 5:
            item["state"] = "needs_review"
            item["reasonCode"] = "retry-exhausted"
            self.outcomes.append({
                **copy.deepcopy(kwargs),
                "state": "needs_review",
                "reasonCode": "retry-exhausted",
            })
            return False
        return True

    def finalize_cancellation(self, **kwargs):
        self.job["state"] = "canceled"
        return True


class Queue:
    def __init__(self):
        self.calls = []

    def send(self, value, *, delay_seconds=0):
        self.calls.append((copy.deepcopy(value), delay_seconds))


class Provider:
    def __init__(self):
        self.calls = []
        self.snapshots = {"sub_synthetic": snapshot()}
        self.snapshots["sub_synthetic"]["schedule"] = None

    def list_migration_candidates(self, resolved, source_price_id, cursor):
        self.calls.append(("list", source_price_id, cursor))
        return {"subscriptionIds": ["sub_synthetic"], "nextCursor": None}

    def retrieve_migration_snapshot(self, resolved, subscription_id):
        self.calls.append(("snapshot", subscription_id))
        return copy.deepcopy(self.snapshots[subscription_id])

    def preview_migration_proration(self, resolved, **kwargs):
        self.calls.append(("preview", copy.deepcopy(kwargs)))
        return {"prorationTimestamp": kwargs["proration_timestamp"], "amountMinor": 1_000}

    def apply_next_renewal_migration(self, resolved, **kwargs):
        self.calls.append(("schedule", copy.deepcopy(kwargs)))
        schedule_id = kwargs["plan"]["scheduleId"] or "sub_sched_synthetic"
        current = copy.deepcopy(self.snapshots[kwargs["subscription_id"]])
        plan = kwargs["plan"]
        default_settings = plan["defaultSettings"]
        if default_settings is None:
            default_settings = {
                "automaticTax": copy.deepcopy(current["automaticTax"]),
                "billingCycleAnchor": "automatic",
                "billingThresholds": copy.deepcopy(current["billingThresholds"]),
                "collectionMethod": current["collectionMethod"],
                "defaultPaymentMethodId": current["defaultPaymentMethodId"],
                "invoiceSettings": copy.deepcopy(current["invoiceSettings"]),
            }
        current["providerRevision"] = "b" * 64
        current["schedule"] = {
            "scheduleId": schedule_id,
            "status": "active",
            "endBehavior": plan["endBehavior"],
            "currentPhaseIndex": 0,
            "defaultSettings": copy.deepcopy(default_settings),
            "phases": copy.deepcopy(plan["phases"]),
        }
        self.snapshots[kwargs["subscription_id"]] = current
        return {"status": "applied", "scheduleId": schedule_id}

    def apply_immediate_migration(self, resolved, **kwargs):
        self.calls.append(("immediate", copy.deepcopy(kwargs)))
        return {"status": "pending_customer_action"}

    def release_migration_schedule(self, resolved, **kwargs):
        self.calls.append(("release", copy.deepcopy(kwargs)))
        return {"status": "reverted"}


class Tax:
    def validate_state(self, authorization, state):
        self.call = (authorization, copy.deepcopy(state))
        return True


def worker(selected_job, *, mappings=None, provider=None, store=None):
    selected_store = store or Store(selected_job)
    return (
        SubscriptionMigrationWorker(
            Resolver(),
            mappings or Mappings(),
            selected_store,
            provider or Provider(),
            Queue(),
            Tax(),
            now_epoch=lambda: 1_800_000_100,
            jitter=lambda attempt: attempt,
        ),
        selected_store,
    )


class SubscriptionMigrationWorkerTests(unittest.TestCase):
    def test_existing_schedule_post_apply_reread_retry_reconciles_without_second_mutation(self):
        selected_job = job(state="running")
        original = snapshot()
        item = {
            "itemId": "migration-item-1",
            "state": "applying",
            "reasonCode": None,
            "attempts": 1,
            "providerSubscriptionId": "sub_synthetic",
            "snapshot": original,
            "snapshotHash": migration_snapshot_hash(original),
            "prorationTimestamp": None,
            "previewAmountMinor": None,
            "scheduleId": None,
            "rollbackGuard": None,
            "cancellationAttempts": 0,
            "cancellationNextAttemptAt": None,
        }

        class PostApplyReadFailure(Provider):
            def __init__(self):
                super().__init__()
                self.applied = False
                self.failed_once = False

            def apply_next_renewal_migration(self, resolved, **kwargs):
                result = super().apply_next_renewal_migration(resolved, **kwargs)
                self.applied = True
                return result

            def retrieve_migration_snapshot(self, resolved, subscription_id):
                if self.applied and not self.failed_once:
                    self.failed_once = True
                    raise MigrationRetryable("synthetic post-apply read timeout")
                return super().retrieve_migration_snapshot(resolved, subscription_id)

        provider = PostApplyReadFailure()
        provider.snapshots["sub_synthetic"] = copy.deepcopy(original)
        selected_store = Store(selected_job)
        selected_worker, _ = worker(
            selected_job, provider=provider, store=selected_store
        )
        resolved = Resolver().resolve(SCOPE, "stripe-primary")

        selected_worker._apply_item(
            SCOPE, selected_job, item, resolved, message("execute", 3)
        )
        self.assertEqual(selected_store.outcomes, [])
        self.assertEqual(
            selected_store.retry_calls[0]["scheduleId"], "sub_sched_synthetic"
        )

        retry_item = {
            **item,
            "attempts": 2,
            "scheduleId": "sub_sched_synthetic",
        }
        selected_worker._apply_item(
            SCOPE, selected_job, retry_item, resolved, message("execute", 3)
        )

        schedule_calls = [call for call in provider.calls if call[0] == "schedule"]
        self.assertEqual(len(schedule_calls), 1)
        self.assertEqual(selected_store.outcomes[-1]["state"], "applied")
        self.assertIsInstance(selected_store.outcomes[-1]["rollbackGuard"], dict)

    def test_concurrent_preview_membership_conflict_becomes_durable_review(self):
        class ConflictingStore(Store):
            def bind_migration_subscription(self, **kwargs):
                del kwargs
                raise MigrationStoreConflict("migration command conflicted")

        selected_store = ConflictingStore(job())
        selected_worker, _ = worker(job(), store=selected_store)

        try:
            selected_worker.process(message())
        except MigrationStoreConflict as error:
            self.fail(f"membership conflict was not made durable: {error}")

        self.assertEqual(len(selected_store.items), 1)
        self.assertEqual(selected_store.items[0]["state"], "needs_review")
        self.assertEqual(selected_store.items[0]["reasonCode"], "scope-mismatch")
        self.assertEqual(len(selected_store.preview_rejections), 1)
        self.assertEqual(len(selected_store.completed_preview), 1)

    def test_next_renewal_persists_a_schedule_projection_guard_after_apply(self):
        selected_job = job(state="scheduled")
        original = snapshot()
        item = {
            "itemId": "migration-item-1",
            "state": "pending",
            "reasonCode": None,
            "attempts": 0,
            "providerSubscriptionId": "sub_synthetic",
            "snapshot": original,
            "snapshotHash": migration_snapshot_hash(original),
            "prorationTimestamp": None,
            "previewAmountMinor": None,
            "scheduleId": None,
            "rollbackGuard": None,
            "cancellationAttempts": 0,
            "cancellationNextAttemptAt": None,
        }
        selected_store = Store(selected_job)
        selected_store.items = [item]

        class ApplyingProvider(Provider):
            def apply_next_renewal_migration(self, resolved, **kwargs):
                result = super().apply_next_renewal_migration(resolved, **kwargs)
                current = copy.deepcopy(self.snapshots[kwargs["subscription_id"]])
                plan = kwargs["plan"]
                current["providerRevision"] = "b" * 64
                current["schedule"] = {
                    "scheduleId": result["scheduleId"],
                    "status": "active",
                    "endBehavior": plan["endBehavior"],
                    "currentPhaseIndex": 0,
                    "defaultSettings": plan["defaultSettings"],
                    "phases": copy.deepcopy(plan["phases"]),
                }
                self.snapshots[kwargs["subscription_id"]] = current
                return result

        provider = ApplyingProvider()
        provider.snapshots["sub_synthetic"] = copy.deepcopy(original)
        selected_worker, _ = worker(
            selected_job, provider=provider, store=selected_store
        )

        selected_worker.process(message("execute", 3))

        self.assertEqual(selected_store.outcomes[0]["state"], "applied")
        guard = selected_store.outcomes[0].get("rollbackGuard")
        self.assertIsInstance(guard, dict)
        self.assertEqual(set(guard), {"hash", "defaultsHash", "phaseIndex", "phaseStart"})
        self.assertEqual(guard["phaseIndex"], 0)
        self.assertEqual(guard["phaseStart"], 1_800_000_000)

    def test_preview_recovers_if_process_stops_after_item_before_active_membership(self):
        class InterruptedStore(Store):
            def __init__(self, selected_job):
                super().__init__(selected_job)
                self.interrupted = False

            def bind_migration_subscription(self, **kwargs):
                if not self.interrupted:
                    self.interrupted = True
                    raise RuntimeError("synthetic interruption")
                super().bind_migration_subscription(**kwargs)

        selected_store = InterruptedStore(job())
        selected_worker, _ = worker(job(), store=selected_store)

        with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
            selected_worker.process(message())
        self.assertEqual(len(selected_store.items), 1)
        self.assertEqual(selected_store.bindings, [])

        selected_worker.process(message())

        self.assertEqual(len(selected_store.items), 1)
        self.assertEqual(len(selected_store.bindings), 1)
        self.assertEqual(len(selected_store.completed_preview), 1)

    def test_preview_discovers_all_matching_price_and_imports_only_exact_owned_subscription(self):
        selected_worker, store = worker(job())
        selected_worker.process(message())

        self.assertEqual(len(store.items), 1)
        item = store.items[0]
        self.assertEqual(item["state"], "pending")
        self.assertNotEqual(item["itemId"], "sub_synthetic")
        self.assertFalse(item["itemId"].startswith("sub_"))
        self.assertEqual(store.bindings[0]["primaryOfferVersionId"], "offer-old")
        self.assertEqual(
            set(store.bindings[0]["offerVersionIds"]), {"offer-old", "offer-addon"}
        )
        self.assertEqual(store.completed_preview[0]["expiresAt"], 1_800_000_100 + 24 * 60 * 60)
        self.assertEqual(len(store.completed_preview[0]["dryRunHash"]), 64)

    def test_preview_marks_any_unmapped_relevant_price_for_review_without_import(self):
        mappings = Mappings()
        mappings.prices.pop("price_addon")
        selected_worker, store = worker(job(), mappings=mappings)
        selected_worker.process(message())

        self.assertEqual(store.items[0]["state"], "needs_review")
        self.assertEqual(store.items[0]["reasonCode"], "unmapped-price")
        self.assertEqual(store.bindings, [])

    def test_preview_rejects_repeated_or_unbounded_provider_cursors(self):
        selected_job = job()
        selected_job["discoveryCursor"] = "sub_same"
        selected_job["revision"] = 2
        selected_store = Store(selected_job)
        provider = Provider()
        provider.list_migration_candidates = lambda *args: {
            "subscriptionIds": ["sub_same"],
            "nextCursor": "sub_same",
        }
        selected_worker, _ = worker(
            selected_job, provider=provider, store=selected_store
        )
        with self.assertRaisesRegex(RuntimeError, "discovery"):
            selected_worker.process(message("preview", 2))

        selected_store.job["discoveryCursor"] = "sub_previous"
        selected_store.job["revision"] = 1000
        provider.list_migration_candidates = lambda *args: {
            "subscriptionIds": ["sub_next"],
            "nextCursor": "sub_next",
        }
        with self.assertRaisesRegex(RuntimeError, "discovery"):
            selected_worker.process(message("preview", 1000))

    def test_execute_rechecks_ownership_and_snapshot_then_rebuilds_schedule_once(self):
        selected_job = job(state="scheduled")
        selected_store = Store(selected_job)
        selected_snapshot = snapshot()
        selected_store.items = [{
            "itemId": "migration-item-1",
            "state": "pending",
            "reasonCode": None,
            "attempts": 0,
            "providerSubscriptionId": "sub_synthetic",
            "snapshot": selected_snapshot,
            "snapshotHash": migration_snapshot_hash(selected_snapshot),
            "prorationTimestamp": None,
            "previewAmountMinor": None,
        }]
        provider = Provider()
        provider.snapshots["sub_synthetic"] = copy.deepcopy(selected_snapshot)
        selected_worker, _ = worker(selected_job, provider=provider, store=selected_store)

        selected_worker.process(message("execute", 3))
        selected_worker.process(message("execute", 3))

        schedule_calls = [call for call in provider.calls if call[0] == "schedule"]
        self.assertEqual(len(schedule_calls), 1)
        plan = schedule_calls[0][1]["plan"]
        self.assertEqual(plan["endBehavior"], "release")
        self.assertEqual(plan["phases"][1]["items"][0]["priceId"], "price_new")
        self.assertEqual(selected_store.outcomes[0]["state"], "applied")

    def test_immediate_uses_dry_run_timestamp_and_pending_customer_action(self):
        selected_job = job("immediate_prorated", state="scheduled")
        selected_store = Store(selected_job)
        selected_snapshot = snapshot()
        selected_snapshot["schedule"] = None
        selected_store.items = [{
            "itemId": "migration-item-1",
            "state": "pending",
            "reasonCode": None,
            "attempts": 0,
            "providerSubscriptionId": "sub_synthetic",
            "snapshot": selected_snapshot,
            "snapshotHash": migration_snapshot_hash(selected_snapshot),
            "prorationTimestamp": 1_800_000_100,
            "previewAmountMinor": 1_000,
        }]
        provider = Provider()
        provider.snapshots["sub_synthetic"] = copy.deepcopy(selected_snapshot)
        selected_worker, _ = worker(selected_job, provider=provider, store=selected_store)

        selected_worker.process(message("execute", 3))

        immediate = next(call[1] for call in provider.calls if call[0] == "immediate")
        previews = [call[1] for call in provider.calls if call[0] == "preview"]
        self.assertEqual(previews[-1]["item_id"], "si_primary")
        self.assertEqual(previews[-1]["quantity"], 3)
        self.assertEqual(immediate["plan"]["itemId"], "si_primary")
        self.assertEqual(immediate["plan"]["prorationTimestamp"], 1_800_000_100)
        self.assertEqual(selected_store.outcomes[0]["state"], "pending_customer_action")

    def test_retry_is_bounded_and_cancel_uses_schedule_release_never_cancel(self):
        selected_job = job(state="scheduled")
        selected_store = Store(selected_job)
        selected_snapshot = snapshot()
        selected_store.items = [{
            "itemId": "migration-item-1",
            "state": "pending",
            "reasonCode": None,
            "attempts": 4,
            "providerSubscriptionId": "sub_synthetic",
            "snapshot": selected_snapshot,
            "snapshotHash": migration_snapshot_hash(selected_snapshot),
            "prorationTimestamp": None,
            "previewAmountMinor": None,
            "scheduleId": "sub_sched_synthetic",
        }]
        provider = Provider()
        provider.snapshots["sub_synthetic"] = copy.deepcopy(selected_snapshot)
        provider.apply_next_renewal_migration = lambda *args, **kwargs: (_ for _ in ()).throw(MigrationRetryable("429"))
        selected_worker, _ = worker(selected_job, provider=provider, store=selected_store)
        selected_worker.process(message("execute", 3))
        self.assertEqual(selected_store.retry_calls[0]["attempts"], 5)
        self.assertIsNone(selected_store.retry_calls[0]["nextAttemptAt"])
        self.assertEqual(selected_store.retry_calls[0]["reasonCode"], "retry-exhausted")

        selected_store.job["state"] = "cancel_requested"
        selected_store.items[0]["state"] = "applied"
        plan = build_next_renewal_plan(
            selected_store.items[0]["snapshot"], "price_old", "price_new"
        )
        migrated = copy.deepcopy(selected_store.items[0]["snapshot"])
        migrated["providerRevision"] = "b" * 64
        migrated["schedule"] = {
            "scheduleId": "sub_sched_synthetic",
            "status": "active",
            "endBehavior": plan["endBehavior"],
            "currentPhaseIndex": 0,
            "defaultSettings": copy.deepcopy(plan["defaultSettings"]),
            "phases": copy.deepcopy(plan["phases"]),
        }
        provider.snapshots["sub_synthetic"] = migrated
        selected_store.items[0]["rollbackGuard"] = _migration_rollback_guard(
            migrated,
            selected_store.items[0],
            selected_store.job,
            "sub_sched_synthetic",
        )
        selected_store.items[0]["cancellationAttempts"] = 0
        selected_store.items[0]["cancellationNextAttemptAt"] = None
        selected_worker.process(message("control", 4))
        release = next(call[1] for call in provider.calls if call[0] == "release")
        self.assertEqual(release["subscription_id"], "sub_synthetic")
        self.assertEqual(
            release["original_schedule"]["scheduleId"], "sub_sched_synthetic"
        )
        self.assertNotIn("cancel", repr(provider.calls).lower())

    def test_cancel_waits_for_an_active_mutation_lease_before_provider_reconciliation(self):
        selected_job = job(state="cancel_requested")
        selected_job["revision"] = 4
        selected_store = Store(selected_job)
        selected_snapshot = snapshot()
        selected_store.items = [{
            "itemId": "migration-item-1",
            "state": "applying",
            "reasonCode": None,
            "attempts": 1,
            "providerSubscriptionId": "sub_synthetic",
            "snapshot": selected_snapshot,
            "snapshotHash": migration_snapshot_hash(selected_snapshot),
            "prorationTimestamp": None,
            "previewAmountMinor": None,
            "scheduleId": None,
            "leaseExpiresAt": 1_800_000_111,
        }]
        provider = Provider()
        provider.snapshots["sub_synthetic"] = copy.deepcopy(selected_snapshot)
        selected_worker, _ = worker(selected_job, provider=provider, store=selected_store)

        selected_worker.process(message("control", 4))

        self.assertFalse(any(call[0] in {"snapshot", "release"} for call in provider.calls))
        self.assertEqual(selected_worker._queue.calls[-1][1], 11)
        self.assertEqual(selected_store.job["state"], "canceling")

    def test_cancel_never_releases_a_dashboard_schedule_not_owned_by_the_migration(self):
        selected_job = job(state="cancel_requested")
        selected_job["revision"] = 4
        selected_store = Store(selected_job)
        original = snapshot()
        original["schedule"] = None
        selected_store.items = [{
            "itemId": "migration-item-1",
            "state": "applied",
            "reasonCode": None,
            "attempts": 1,
            "providerSubscriptionId": "sub_synthetic",
            "snapshot": original,
            "snapshotHash": migration_snapshot_hash(original),
            "prorationTimestamp": None,
            "previewAmountMinor": None,
            "scheduleId": "sub_sched_migration",
            "leaseExpiresAt": None,
        }]
        provider = Provider()
        dashboard = snapshot()
        dashboard["schedule"]["scheduleId"] = "sub_sched_dashboard"
        provider.snapshots["sub_synthetic"] = dashboard
        selected_worker, _ = worker(selected_job, provider=provider, store=selected_store)

        selected_worker.process(message("control", 4))

        self.assertFalse(any(call[0] == "release" for call in provider.calls))
        self.assertEqual(selected_store.outcomes[0]["state"], "needs_review")
        self.assertEqual(selected_store.outcomes[0]["reasonCode"], "source-drift")

    def test_cancel_does_not_restore_same_id_schedule_changed_after_migration(self):
        selected_job = job(state="cancel_requested")
        selected_job["revision"] = 4
        original = snapshot()
        plan = build_next_renewal_plan(original, "price_old", "price_new")
        applied = copy.deepcopy(original)
        applied["providerRevision"] = "b" * 64
        applied["schedule"] = {
            "scheduleId": "sub_sched_synthetic",
            "status": "active",
            "endBehavior": plan["endBehavior"],
            "currentPhaseIndex": 0,
            "defaultSettings": copy.deepcopy(plan["defaultSettings"]),
            "phases": copy.deepcopy(plan["phases"]),
        }
        projection = {
            "scheduleId": applied["schedule"]["scheduleId"],
            "endBehavior": applied["schedule"]["endBehavior"],
            "defaultSettings": applied["schedule"]["defaultSettings"],
            "phases": applied["schedule"]["phases"],
        }
        guard = {
            "hash": hashlib.sha256(json.dumps(projection, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest(),
            "defaultsHash": hashlib.sha256(json.dumps(projection["defaultSettings"], sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest(),
            "phaseIndex": 0,
            "phaseStart": applied["schedule"]["phases"][0]["startDate"],
        }
        selected_store = Store(selected_job)
        selected_store.items = [{
            "itemId": "migration-item-1",
            "state": "applied",
            "reasonCode": None,
            "attempts": 1,
            "providerSubscriptionId": "sub_synthetic",
            "snapshot": original,
            "snapshotHash": migration_snapshot_hash(original),
            "prorationTimestamp": None,
            "previewAmountMinor": None,
            "scheduleId": "sub_sched_synthetic",
            "rollbackGuard": guard,
            "cancellationAttempts": 0,
            "cancellationNextAttemptAt": None,
            "leaseExpiresAt": None,
        }]
        provider = Provider()
        changed = copy.deepcopy(applied)
        changed["schedule"]["phases"][1]["items"][0]["quantity"] += 1
        changed["providerRevision"] = "c" * 64
        provider.snapshots["sub_synthetic"] = changed
        selected_worker, _ = worker(
            selected_job, provider=provider, store=selected_store
        )

        selected_worker.process(message("control", 4))

        self.assertFalse(any(call[0] == "release" for call in provider.calls))
        self.assertEqual(selected_store.outcomes[0]["state"], "needs_review")
        self.assertEqual(selected_store.outcomes[0]["reasonCode"], "source-drift")

    def test_cancel_accepts_only_an_exact_natural_schedule_phase_advance(self):
        selected_job = job(state="cancel_requested")
        selected_job["revision"] = 4
        original = snapshot()
        plan = build_next_renewal_plan(original, "price_old", "price_new")
        applied = copy.deepcopy(original)
        applied["providerRevision"] = "b" * 64
        applied["schedule"] = {
            "scheduleId": "sub_sched_synthetic",
            "status": "active",
            "endBehavior": plan["endBehavior"],
            "currentPhaseIndex": 0,
            "defaultSettings": copy.deepcopy(plan["defaultSettings"]),
            "phases": copy.deepcopy(plan["phases"]),
        }
        item = {
            "itemId": "migration-item-1",
            "state": "applied",
            "reasonCode": None,
            "attempts": 1,
            "providerSubscriptionId": "sub_synthetic",
            "snapshot": original,
            "snapshotHash": migration_snapshot_hash(original),
            "prorationTimestamp": None,
            "previewAmountMinor": None,
            "scheduleId": "sub_sched_synthetic",
            "cancellationAttempts": 0,
            "cancellationNextAttemptAt": None,
            "leaseExpiresAt": None,
        }
        item["rollbackGuard"] = _migration_rollback_guard(
            applied, item, selected_job, "sub_sched_synthetic"
        )
        advanced = copy.deepcopy(applied)
        advanced["providerRevision"] = "c" * 64
        advanced["schedule"]["phases"] = advanced["schedule"]["phases"][1:]
        advanced["schedule"]["currentPhaseIndex"] = 0
        selected_store = Store(selected_job)
        selected_store.items = [item]
        provider = Provider()
        provider.snapshots["sub_synthetic"] = advanced
        selected_worker, _ = worker(
            selected_job, provider=provider, store=selected_store
        )

        selected_worker.process(message("control", 4))

        self.assertTrue(any(call[0] == "release" for call in provider.calls))
        self.assertEqual(selected_store.outcomes[0]["state"], "reverted")

    def test_cancel_isolates_provider_failures_per_item_and_delays_retryable_work(self):
        selected_job = job(state="cancel_requested")
        selected_job["revision"] = 4
        original = snapshot()
        item = {
            "itemId": "migration-item-1",
            "state": "applied",
            "reasonCode": None,
            "attempts": 1,
            "providerSubscriptionId": "sub_synthetic",
            "snapshot": original,
            "snapshotHash": migration_snapshot_hash(original),
            "prorationTimestamp": None,
            "previewAmountMinor": None,
            "scheduleId": "sub_sched_synthetic",
            "leaseExpiresAt": None,
        }

        retry_store = Store(selected_job)
        retry_store.items = [copy.deepcopy(item)]
        retry_provider = Provider()
        retry_provider.retrieve_migration_snapshot = lambda *args, **kwargs: (
            (_ for _ in ()).throw(MigrationRetryable("synthetic 503"))
        )
        retry_worker, _ = worker(
            selected_job, provider=retry_provider, store=retry_store
        )
        retry_worker.process(message("control", 4))
        self.assertEqual(retry_store.outcomes, [])
        self.assertEqual(retry_worker._queue.calls[-1][1], 3)
        self.assertEqual(retry_store.job["state"], "canceling")

        permanent_store = Store(selected_job)
        permanent_store.items = [copy.deepcopy(item)]
        permanent_provider = Provider()
        permanent_provider.retrieve_migration_snapshot = lambda *args, **kwargs: (
            (_ for _ in ()).throw(RuntimeError("synthetic 400"))
        )
        permanent_worker, _ = worker(
            selected_job, provider=permanent_provider, store=permanent_store
        )
        permanent_worker.process(message("control", 4))
        self.assertEqual(permanent_store.outcomes[0]["state"], "needs_review")
        self.assertEqual(
            permanent_store.outcomes[0]["reasonCode"], "provider-unknown"
        )
        self.assertEqual(permanent_store.job["state"], "canceled")

    def test_cancel_retry_is_durable_bounded_and_uses_deterministic_jitter(self):
        selected_job = job(state="cancel_requested")
        selected_job["revision"] = 4
        original = snapshot()
        item = {
            "itemId": "migration-item-1",
            "state": "applied",
            "reasonCode": None,
            "attempts": 1,
            "providerSubscriptionId": "sub_synthetic",
            "snapshot": original,
            "snapshotHash": migration_snapshot_hash(original),
            "prorationTimestamp": None,
            "previewAmountMinor": None,
            "scheduleId": "sub_sched_synthetic",
            "rollbackGuard": None,
            "cancellationAttempts": 0,
            "cancellationNextAttemptAt": None,
            "leaseExpiresAt": None,
        }
        selected_store = Store(selected_job)
        selected_store.items = [item]
        provider = Provider()
        provider.retrieve_migration_snapshot = lambda *args, **kwargs: (
            (_ for _ in ()).throw(MigrationRetryable("synthetic 503"))
        )
        selected_worker, _ = worker(
            selected_job, provider=provider, store=selected_store
        )

        selected_worker.process(message("control", 4))

        self.assertTrue(selected_store.cancellation_retry_calls)
        retry = selected_store.cancellation_retry_calls[0]
        self.assertEqual(retry["cancellationAttempts"], 1)
        self.assertEqual(retry["nextAttemptAt"], 1_800_000_103)
        self.assertEqual(selected_worker._queue.calls[-1][1], 3)

        provider_calls = len(provider.calls)
        selected_worker.process(message("reconcile", 5))
        self.assertEqual(len(provider.calls), provider_calls)
        self.assertEqual(selected_store.cancellation_retry_calls, [retry])
        self.assertEqual(selected_worker._queue.calls[-1][1], 3)

    def test_failed_delayed_enqueue_is_retried_without_an_immediate_hot_loop(self):
        selected_job = job(state="scheduled")
        selected_snapshot = snapshot()
        selected_store = Store(selected_job)
        selected_store.items = [
            {
                "itemId": "migration-item-1",
                "state": "pending",
                "reasonCode": None,
                "attempts": 0,
                "providerSubscriptionId": "sub_synthetic",
                "snapshot": selected_snapshot,
                "snapshotHash": migration_snapshot_hash(selected_snapshot),
                "prorationTimestamp": None,
                "previewAmountMinor": None,
                "scheduleId": None,
            }
        ]

        def continuation(**kwargs):
            del kwargs
            return {
                **selected_store.job,
                "revision": selected_store.job["revision"],
                "workDelaySeconds": 7,
            }

        selected_store.continue_execution = continuation
        provider = Provider()
        provider.snapshots["sub_synthetic"] = copy.deepcopy(selected_snapshot)
        provider.apply_next_renewal_migration = lambda *args, **kwargs: (
            (_ for _ in ()).throw(MigrationRetryable("429"))
        )

        class FlakyQueue(Queue):
            def __init__(self):
                super().__init__()
                self.attempts = []

            def send(self, value, *, delay_seconds=0):
                self.attempts.append(delay_seconds)
                if len(self.attempts) == 1:
                    raise RuntimeError("temporary queue failure")
                super().send(value, delay_seconds=delay_seconds)

        queue = FlakyQueue()
        selected_worker = SubscriptionMigrationWorker(
            Resolver(),
            Mappings(),
            selected_store,
            provider,
            queue,
            Tax(),
            now_epoch=lambda: 1_800_000_100,
            jitter=lambda attempt: attempt,
        )

        with self.assertRaises(RuntimeError):
            selected_worker.process(message("execute", 3))
        selected_worker.process(message("execute", 3))

        self.assertEqual(queue.attempts, [3, 7])
        self.assertTrue(all(delay > 0 for delay in queue.attempts))
        self.assertEqual(queue.calls[0][1], 7)

    def test_sqs_batch_reports_only_failed_messages(self):
        class Processor:
            def process(self, value):
                if value["jobId"] == "migration-job-2":
                    raise RuntimeError("retry")

        records = [
            {"messageId": "one", "body": __import__("json").dumps(message())},
            {"messageId": "two", "body": __import__("json").dumps({**message(), "jobId": "migration-job-2"})},
        ]
        self.assertEqual(
            handle_records({"Records": records}, worker=Processor()),
            {"batchItemFailures": [{"itemIdentifier": "two"}]},
        )


if __name__ == "__main__":
    unittest.main()
