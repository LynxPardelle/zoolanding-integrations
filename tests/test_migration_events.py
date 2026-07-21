import unittest

from src.domain.integrations import IntegrationScope
from src.domain.operations import IntegrationEventEnvelope


SCOPE = IntegrationScope("test", "tenant-example", "draft-example", "example.com")
COUNTS = {"total": 3, "pending": 2, "applied": 1, "needsReview": 0, "failed": 0}
COMMON = {
    "commercialRequestId": "request-1",
    "jobId": "migration-job-1",
    "connectionId": "stripe-primary",
    "revision": 3,
    "dedupeKey": "migration-event-1",
}


class MigrationEventTests(unittest.TestCase):
    def test_exact_four_migration_event_shapes_are_accepted(self):
        payloads = {
            "migration.preview_ready.v1": {
                **COMMON,
                "dryRunRevision": 2,
                "dryRunHash": "a" * 64,
                "expiresAt": 1_900_000_000,
                "counts": COUNTS,
            },
            "migration.progressed.v1": {**COMMON, "state": "running", "counts": COUNTS},
            "migration.item_needs_review.v1": {
                **COMMON,
                "itemId": "migration-item-" + "1" * 40,
                "reasonCode": "unsupported-schedule",
            },
            "migration.completed.v1": {
                **COMMON,
                "state": "completed_with_errors",
                "counts": COUNTS,
            },
        }
        for event_type, data in payloads.items():
            with self.subTest(event_type=event_type):
                result = IntegrationEventEnvelope(
                    SCOPE, "event-1", event_type, 1_800_000_000, data
                ).to_dict()
                self.assertEqual(result["data"], data)

    def test_migration_events_reject_extra_sensitive_or_invalid_aggregate_data(self):
        valid = {
            **COMMON,
            "state": "running",
            "counts": COUNTS,
        }
        for changed in (
            {**valid, "customerEmail": "forbidden@example.com"},
            {**valid, "counts": {**COUNTS, "total": -1}},
            {**valid, "counts": {**COUNTS, "total": 4}},
            {**valid, "state": "arbitrary"},
        ):
            with self.assertRaises(ValueError):
                IntegrationEventEnvelope(
                    SCOPE, "event-1", "migration.progressed.v1", 1_800_000_000, changed
                )

    def test_progressed_and_completed_states_are_closed_by_event_type(self):
        with self.assertRaises(ValueError):
            IntegrationEventEnvelope(
                SCOPE,
                "event-1",
                "migration.completed.v1",
                1_800_000_000,
                {**COMMON, "state": "running", "counts": COUNTS},
            )
        with self.assertRaises(ValueError):
            IntegrationEventEnvelope(
                SCOPE,
                "event-1",
                "migration.progressed.v1",
                1_800_000_000,
                {**COMMON, "state": "completed", "counts": COUNTS},
            )

    def test_item_review_event_rejects_generic_item_id_and_noncanonical_reason(self):
        valid = {
            **COMMON,
            "itemId": "migration-item-" + "1" * 40,
            "reasonCode": "source-drift",
        }
        for changed in (
            {**valid, "itemId": "migration-item-1"},
            {**valid, "reasonCode": "conflict"},
        ):
            with self.assertRaises(ValueError):
                IntegrationEventEnvelope(
                    SCOPE,
                    "event-1",
                    "migration.item_needs_review.v1",
                    1_800_000_000,
                    changed,
                )


if __name__ == "__main__":
    unittest.main()
