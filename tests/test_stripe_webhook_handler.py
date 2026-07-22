import base64
import hashlib
import json
import unittest

from src.stripe_store import WebhookReplayConflict
from tests.test_registry import connection, scope


RAW = b'{"id":"evt-1","type":"checkout.session.completed","account":"acct_synthetic","livemode":false,"created":1799999995,"data":{"object":{"email":"private@example.com"}}}'


def api_event(raw=RAW, *, encoded=False):
    body = base64.b64encode(raw).decode("ascii") if encoded else raw.decode("utf-8")
    return {
        "httpMethod": "POST",
        "path": "/webhooks/stripe/connect",
        "headers": {"Stripe-Signature": "t=1800000000,v1=synthetic"},
        "body": body,
        "isBase64Encoded": encoded,
        "requestContext": {"requestId": "request-1"},
    }


class Verifier:
    def __init__(self, result=None, error=None):
        self.result = result or json.loads(RAW)
        self.error = error
        self.calls = []

    def verify(self, raw, signature):
        self.calls.append((raw, signature))
        if self.error:
            raise self.error
        return self.result


class Registry:
    def __init__(self):
        self.calls = []

    def stripe_webhook_connection(self, **kwargs):
        self.calls.append(kwargs)
        return connection()


class Store:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def accept_supported(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"status": "queued", "duplicate": False}


class Metrics:
    def __init__(self):
        self.values = []

    def __call__(self, name, value):
        self.values.append((name, value))


class StripeWebhookHandlerTests(unittest.TestCase):
    def handler(self):
        from src.handlers import stripe_webhook

        return stripe_webhook

    def call(self, event=None, *, verifier=None, registry=None, store=None, metrics=None):
        return self.handler().handle_request(
            event or api_event(),
            verifier=verifier or Verifier(),
            registry=registry or Registry(),
            store=store or Store(),
            environment="test",
            now_epoch=1_800_000_000,
            metric_sink=metrics or Metrics(),
        )

    def test_supported_event_verifies_exact_raw_bytes_and_persists_only_metadata(self):
        verifier, registry, store = Verifier(), Registry(), Store()
        response = self.call(
            api_event(encoded=True), verifier=verifier, registry=registry, store=store
        )

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(verifier.calls, [(RAW, "t=1800000000,v1=synthetic")])
        self.assertEqual(
            registry.calls,
            [
                {
                    "environment": "test",
                    "mode": "test",
                    "account_reference": "acct_synthetic",
                    "event_type": "checkout.session.completed",
                }
            ],
        )
        persisted = store.calls[0]
        self.assertEqual(persisted["scope"], scope())
        self.assertEqual(persisted["payload_hash"], hashlib.sha256(RAW).hexdigest())
        self.assertEqual(
            persisted["account_hash"], hashlib.sha256(b"acct_synthetic").hexdigest()
        )
        self.assertNotIn("email", repr(persisted))
        self.assertNotIn("data", persisted)

    def test_valid_unsupported_event_is_acknowledged_without_routing_or_storage(self):
        verifier = Verifier(result={**json.loads(RAW), "type": "account.updated"})
        registry, store = Registry(), Store()
        response = self.call(verifier=verifier, registry=registry, store=store)

        self.assertEqual(response["statusCode"], 200)
        self.assertIn('"status":"ignored"', response["body"])
        self.assertEqual(registry.calls, [])
        self.assertEqual(store.calls, [])

    def test_invalid_signature_wrong_mode_and_changed_replay_fail_closed(self):
        cases = (
            (Verifier(error=ValueError("signature")), Store(), 400),
            (Verifier(result={**json.loads(RAW), "livemode": True}), Store(), 409),
            (Verifier(), Store(WebhookReplayConflict("conflict")), 409),
        )
        for verifier, store, expected in cases:
            with self.subTest(expected=expected):
                response = self.call(verifier=verifier, store=store)
                self.assertEqual(response["statusCode"], expected)
                self.assertNotIn("signature", response["body"])
                self.assertNotIn("acct_synthetic", response["body"])

    def test_emits_only_redacted_webhook_age_signature_and_mode_metrics(self):
        accepted_metrics = Metrics()
        response = self.call(metrics=accepted_metrics)
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(accepted_metrics.values, [("WebhookAgeSeconds", 5)])

        signature_metrics = Metrics()
        response = self.call(
            verifier=Verifier(error=ValueError("private signature detail")),
            metrics=signature_metrics,
        )
        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(signature_metrics.values, [("WebhookSignatureFailures", 1)])

        missing_signature = api_event()
        missing_signature["headers"] = {}
        missing_metrics = Metrics()
        response = self.call(event=missing_signature, metrics=missing_metrics)
        self.assertEqual(response["statusCode"], 400)
        self.assertEqual(missing_metrics.values, [("WebhookSignatureFailures", 1)])

        mismatch_metrics = Metrics()
        response = self.call(
            verifier=Verifier(result={**json.loads(RAW), "livemode": True}),
            metrics=mismatch_metrics,
        )
        self.assertEqual(response["statusCode"], 409)
        self.assertEqual(
            mismatch_metrics.values,
            [("WebhookAgeSeconds", 5), ("TestLiveMismatch", 1)],
        )

    def test_metric_transport_failure_never_changes_ingress_result(self):
        def unavailable_metric_sink(name, value):
            del name, value
            raise RuntimeError("metric transport unavailable")

        response = self.call(metrics=unavailable_metric_sink)
        self.assertEqual(response["statusCode"], 200)


if __name__ == "__main__":
    unittest.main()
