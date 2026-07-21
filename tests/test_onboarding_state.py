import hashlib
import importlib
import importlib.util
import unittest

from tests.test_registry import scope


def state_module(testcase):
    testcase.assertIsNotNone(
        importlib.util.find_spec("src.onboarding_state"),
        "one-use onboarding state is not implemented",
    )
    return importlib.import_module("src.onboarding_state")


class MemoryStateStore:
    def __init__(self):
        self.records = {}

    def put_once(self, record):
        if record["stateHash"] in self.records:
            raise RuntimeError("duplicate")
        self.records[record["stateHash"]] = dict(record)

    def consume_once(self, state_hash, expected, now_epoch):
        record = self.records.get(state_hash)
        if (
            record is None
            or record.get("consumedAt") is not None
            or record["expiresAt"] <= now_epoch
            or any(record.get(key) != value for key, value in expected.items())
        ):
            raise RuntimeError("invalid")
        record["consumedAt"] = now_epoch
        return dict(record)


class OnboardingStateTests(unittest.TestCase):
    def test_state_is_opaque_hashed_scope_bound_and_consumed_once(self):
        state = state_module(self)
        store = MemoryStateStore()
        manager = state.OnboardingStateManager(
            store,
            token_bytes=lambda size: b"x" * size,
        )
        session_hash = hashlib.sha256(b"session").hexdigest()

        token = manager.issue(
            scope(),
            "stripe-primary",
            session_hash=session_hash,
            now_epoch=1_000,
        )

        self.assertEqual(len(token), 43)
        self.assertNotIn(token, str(store.records))
        record = next(iter(store.records.values()))
        self.assertEqual(record["expiresAt"], 1_600)
        self.assertEqual(record["draftId"], "draft-example")
        self.assertEqual(record["sessionHash"], session_hash)

        manager.consume(
            token,
            scope(),
            "stripe-primary",
            session_hash=session_hash,
            now_epoch=1_001,
        )
        with self.assertRaises(state.OnboardingStateError):
            manager.consume(
                token,
                scope(),
                "stripe-primary",
                session_hash=session_hash,
                now_epoch=1_002,
            )

    def test_wrong_session_scope_or_expired_state_fails_closed(self):
        state = state_module(self)
        for changed_session, changed_scope, consume_at in (
            ("b" * 64, scope(), 1_001),
            ("a" * 64, scope("draft-other"), 1_001),
            ("a" * 64, scope(), 1_600),
        ):
            store = MemoryStateStore()
            manager = state.OnboardingStateManager(
                store,
                token_bytes=lambda size: b"y" * size,
            )
            token = manager.issue(
                scope(),
                "stripe-primary",
                session_hash="a" * 64,
                now_epoch=1_000,
            )
            with self.assertRaises(state.OnboardingStateError):
                manager.consume(
                    token,
                    changed_scope,
                    "stripe-primary",
                    session_hash=changed_session,
                    now_epoch=consume_at,
                )

    def test_connection_identifier_must_be_ascii_safe_before_state_is_written(self):
        state = state_module(self)
        store = MemoryStateStore()
        manager = state.OnboardingStateManager(store)
        with self.assertRaises(state.OnboardingStateError):
            manager.issue(
                scope(),
                "strípe-primary",
                session_hash="a" * 64,
                now_epoch=1_000,
            )
        self.assertEqual(store.records, {})


if __name__ == "__main__":
    unittest.main()
