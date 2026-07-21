import copy
import unittest

from src.contracts.internal import InternalCommand
from src.domain.integrations import IntegrationScope
from src.subscription_migrations import (
    MigrationConflict,
    SubscriptionMigrationService,
    SubscriptionMigrationStatusService,
)
from tests.test_migration_contracts import offer


SCOPE = IntegrationScope("test", "tenant-example", "draft-example", "example.com")


def command(kind, value, *, key="integrations-command-v1:" + "a" * 64):
    return InternalCommand(
        kind,
        SCOPE,
        "stripe-primary",
        "command-1",
        key,
        copy.deepcopy(value),
        "b" * 64,
    )


class Resolver:
    def __init__(self):
        self.calls = []
        self.resolved = object()

    def resolve(self, scope, connection_id, **kwargs):
        self.calls.append((scope, connection_id, kwargs))
        return self.resolved


class Mappings:
    def __init__(self):
        self.values = {
            "offer-old": {
                "resourceType": "offer",
                "resourceId": "offer-old",
                "revision": 2,
                "contentHash": offer("offer-old", 90000)["contentHash"],
                "priceId": "price_old",
                "status": "existing_only",
            },
            "offer-new": {
                "resourceType": "offer",
                "resourceId": "offer-new",
                "revision": 2,
                "contentHash": offer("offer-new", 100000)["contentHash"],
                "priceId": "price_new",
                "status": "active",
            },
        }

    def get_mapping(self, scope, connection_id, resource_type, resource_id):
        if scope != SCOPE or connection_id != "stripe-primary" or resource_type != "offer":
            raise AssertionError("wrong mapping scope")
        return copy.deepcopy(self.values.get(resource_id))


class Store:
    def __init__(self):
        self.jobs = {}
        self.claims = {}

    def create_preview(self, **kwargs):
        key = kwargs["idempotencyKeyHash"]
        request_hash = kwargs["requestHash"]
        if key in self.claims:
            if self.claims[key] != request_hash:
                raise MigrationConflict("migration command conflicted")
            return copy.deepcopy(self.jobs[kwargs["jobId"]]), False
        self.claims[key] = request_hash
        job = {
            **kwargs,
            "state": "previewing",
            "revision": 1,
            "dryRunRevision": None,
            "dryRunHash": None,
            "expiresAt": None,
            "counts": {"total": 0, "pending": 0, "applied": 0, "needsReview": 0, "failed": 0},
        }
        self.jobs[job["jobId"]] = job
        return copy.deepcopy(job), True

    def get_job(self, **kwargs):
        job = self.jobs.get(kwargs["jobId"])
        if (
            job is None
            or job["connectionId"] != kwargs["connectionId"]
            or job["commercialRequestId"] != kwargs["commercialRequestId"]
        ):
            return None
        return copy.deepcopy(job)

    def schedule_execution(self, **kwargs):
        job = self.jobs[kwargs["jobId"]]
        if (
            job["state"] != "awaiting_approval"
            or job["dryRunRevision"] != kwargs["dryRunRevision"]
            or job["dryRunHash"] != kwargs["dryRunHash"]
            or job["expiresAt"] <= kwargs["nowEpoch"]
        ):
            return None
        job["state"] = "scheduled"
        job["revision"] += 1
        job["taxAuthorization"] = kwargs["taxAuthorization"]
        return copy.deepcopy(job)

    def control(self, **kwargs):
        job = self.jobs[kwargs["jobId"]]
        if job["revision"] != kwargs["expectedRevision"]:
            raise MigrationConflict("migration command conflicted")
        transitions = {
            ("running", "pause"): "paused",
            ("paused", "resume"): "running",
            ("running", "cancel"): "cancel_requested",
            ("paused", "cancel"): "cancel_requested",
            ("scheduled", "cancel"): "cancel_requested",
        }
        state = transitions.get((job["state"], kwargs["action"]))
        if state is None:
            raise MigrationConflict("migration command conflicted")
        job["state"] = state
        job["revision"] += 1
        return copy.deepcopy(job)

    def status(self, **kwargs):
        job = self.jobs[kwargs["jobId"]]
        return {
            "commercialRequestId": job["commercialRequestId"],
            "jobId": job["jobId"],
            "connectionId": job["connectionId"],
            "revision": job["revision"],
            "state": job["state"],
            "dryRunRevision": job["dryRunRevision"],
            "dryRunHash": job["dryRunHash"],
            "expiresAt": job["expiresAt"],
            "counts": job["counts"],
            "items": [],
            "nextCursor": None,
        }


class Queue:
    def __init__(self):
        self.messages = []

    def send(self, value):
        self.messages.append(copy.deepcopy(value))


class Tax:
    def __init__(self, authorization=("manual-rate", None)):
        self.authorization = authorization
        self.calls = []

    def authorize(self, resolved, revision):
        self.calls.append((resolved, revision))
        return self.authorization


class SubscriptionMigrationServiceTests(unittest.TestCase):
    def setUp(self):
        self.resolver = Resolver()
        self.mappings = Mappings()
        self.store = Store()
        self.queue = Queue()
        self.tax = Tax()
        self.service = SubscriptionMigrationService(
            self.resolver,
            self.mappings,
            self.store,
            self.queue,
            tax_verifier=self.tax,
            now_epoch=lambda: 1_800_000_000,
        )
        self.preview_input = {
            "commercialRequestId": "request-1",
            "sourceOffer": offer("offer-old", 90000),
            "targetOffer": offer("offer-new", 100000),
            "requestedPolicy": {"mode": "next_renewal"},
            "candidateScope": {"kind": "all_matching_source_price"},
            "canarySize": 5,
            "accountConcurrency": 2,
        }

    def test_preview_only_persists_and_queues_a_redacted_async_job(self):
        result = self.service.execute(
            "migration-preview", command("migration-preview", self.preview_input)
        )

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["revision"], 1)
        self.assertEqual(len(self.queue.messages), 1)
        self.assertEqual(
            set(self.queue.messages[0]),
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
            },
        )
        self.assertNotIn("price_", repr(self.queue.messages))
        self.assertNotIn("offer-", repr(self.queue.messages))
        self.assertEqual(self.resolver.calls[0][2], {"provider": "stripe", "capability": "subscriptions"})

    def test_preview_replay_is_safe_and_same_key_different_request_conflicts(self):
        selected = command("migration-preview", self.preview_input)
        first = self.service.execute("migration-preview", selected)
        second = self.service.execute("migration-preview", selected)
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.jobs), 1)

        changed = command("migration-preview", self.preview_input)
        changed.input["canarySize"] = 6
        with self.assertRaises(MigrationConflict):
            self.service.execute("migration-preview", changed)

    def test_pending_queue_admission_is_retried_by_the_exact_command_replay(self):
        class FlakyQueue:
            def __init__(self):
                self.attempts = 0
                self.messages = []

            def send(self, value):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError("temporary")
                self.messages.append(copy.deepcopy(value))

        queue = FlakyQueue()
        service = SubscriptionMigrationService(
            self.resolver,
            self.mappings,
            self.store,
            queue,
            tax_verifier=self.tax,
            now_epoch=lambda: 1_800_000_000,
        )
        selected = command("migration-preview", self.preview_input)

        first = service.execute("migration-preview", selected)
        replay = service.execute("migration-preview", selected)

        self.assertEqual(first["status"], "pending")
        self.assertEqual(replay["status"], "accepted")
        self.assertEqual(first["jobId"], replay["jobId"])
        self.assertEqual(first["revision"], replay["revision"])
        self.assertEqual(queue.attempts, 2)
        self.assertEqual(len(queue.messages), 1)

    def test_reverse_migration_accepts_existing_only_price_that_remains_provider_active(self):
        self.mappings.values["offer-old"]["status"] = "active"
        self.mappings.values["offer-new"]["status"] = "existing_only"
        reverse = {
            **self.preview_input,
            "sourceOffer": offer("offer-old", 90000),
            "targetOffer": offer("offer-new", 100000),
        }

        result = self.service.execute(
            "migration-preview", command("migration-preview", reverse)
        )

        self.assertEqual(result["status"], "accepted")

    def test_execute_requires_exact_unexpired_preview_and_tax_approval(self):
        preview = self.service.execute(
            "migration-preview", command("migration-preview", self.preview_input)
        )
        job = self.store.jobs[preview["jobId"]]
        job.update(
            {
                "state": "awaiting_approval",
                "revision": 2,
                "dryRunRevision": 1,
                "dryRunHash": "c" * 64,
                "expiresAt": 1_800_000_000 + 24 * 60 * 60,
            }
        )
        execute_input = {
            "commercialRequestId": "request-1",
            "jobId": preview["jobId"],
            "dryRunRevision": 1,
            "dryRunHash": "c" * 64,
            "confirmation": True,
        }
        result = self.service.execute(
            "migration-execute", command("migration-execute", execute_input)
        )
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(self.tax.calls[-1][1], 2)
        self.assertEqual(self.store.jobs[preview["jobId"]]["state"], "scheduled")

        denied = SubscriptionMigrationService(
            self.resolver,
            self.mappings,
            self.store,
            self.queue,
            tax_verifier=Tax(None),
            now_epoch=lambda: 1_800_000_000,
        )
        job["state"] = "awaiting_approval"
        job["revision"] = 2
        self.assertEqual(
            denied.execute("migration-execute", command("migration-execute", execute_input))["status"],
            "needs_review",
        )

    def test_control_and_status_are_revision_and_scope_bound(self):
        preview = self.service.execute(
            "migration-preview", command("migration-preview", self.preview_input)
        )
        job = self.store.jobs[preview["jobId"]]
        job.update({"state": "running", "revision": 4})
        controlled = self.service.execute(
            "migration-control",
            command(
                "migration-control",
                {
                    "commercialRequestId": "request-1",
                    "jobId": preview["jobId"],
                    "expectedRevision": 4,
                    "action": "pause",
                },
            ),
        )
        self.assertEqual(controlled["revision"], 5)
        status = self.service.execute(
            "migration-status",
            command(
                "migration-status",
                {"commercialRequestId": "request-1", "jobId": preview["jobId"], "limit": 25},
                key="read-only",
            ),
        )
        self.assertEqual(status["state"], "paused")
        self.assertEqual(status["items"], [])

    def test_read_only_status_resolves_the_exact_draft_connection(self):
        preview = self.service.execute(
            "migration-preview", command("migration-preview", self.preview_input)
        )
        status_service = SubscriptionMigrationStatusService(self.resolver, self.store)

        result = status_service.execute(
            "migration-status",
            command(
                "migration-status",
                {
                    "commercialRequestId": "request-1",
                    "jobId": preview["jobId"],
                    "limit": 25,
                },
                key="read-only",
            ),
        )

        self.assertEqual(result["jobId"], preview["jobId"])
        self.assertEqual(
            self.resolver.calls[-1],
            (
                SCOPE,
                "stripe-primary",
                {"provider": "stripe", "capability": "subscriptions"},
            ),
        )
        with self.assertRaises(MigrationConflict):
            status_service.execute(
                "migration-preview", command("migration-preview", self.preview_input)
            )


if __name__ == "__main__":
    unittest.main()
