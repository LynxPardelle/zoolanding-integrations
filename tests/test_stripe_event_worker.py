import unittest
import hashlib

from src.registry import _serialize
from tests.test_registry import connection, scope


def stream_record(sequence, *, event_id=None):
    item = {
        "pk": "ENV#test#TENANT#tenant-example#DRAFT#draft-example",
        "sk": f"WEBHOOK_INGRESS_OUTBOX#{event_id or 'evt-' + sequence}",
        "itemType": "WebhookIngressOutbox",
        "environment": "test",
        "tenantId": "tenant-example",
        "draftId": "draft-example",
        "domain": "example.com",
        "outboxId": event_id or "evt-" + sequence,
        "receiptId": event_id or "evt-" + sequence,
        "processingStatus": "pending",
        "processingRevision": 1,
        "attemptCount": 0,
        "createdAt": 1_800_000_000,
        "expiresAt": 1_800_000_000 + 90 * 24 * 60 * 60,
    }
    return {
        "eventID": "opaque-event-id-" + sequence,
        "eventName": "INSERT",
        "dynamodb": {"SequenceNumber": sequence, "NewImage": _serialize(item)},
    }


class Worker:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.calls = []

    def process(self, record, sequence):
        self.calls.append((record["outboxId"], sequence))
        if sequence == self.fail_on:
            raise RuntimeError("retry")


class StripeEventWorkerTests(unittest.TestCase):
    def module(self):
        from src.handlers import stripe_event_worker

        return stripe_event_worker

    def test_partial_batch_uses_sequence_number_and_stops_after_first_failure(self):
        worker = Worker(fail_on="102")
        response = self.module().handle_records(
            {
                "Records": [
                    stream_record("101"),
                    stream_record("102"),
                    stream_record("103"),
                ]
            },
            worker=worker,
        )

        self.assertEqual(worker.calls, [("evt-101", "101"), ("evt-102", "102")])
        self.assertEqual(
            response,
            {
                "batchItemFailures": [
                    {"itemIdentifier": "102"},
                    {"itemIdentifier": "103"},
                ]
            },
        )

    def test_successful_batch_returns_no_failures(self):
        worker = Worker()
        self.assertEqual(
            self.module().handle_records(
                {"Records": [stream_record("101"), stream_record("102")]},
                worker=worker,
            ),
            {"batchItemFailures": []},
        )

    def test_deauthorization_event_disables_exact_account_without_commerce_event(self):
        module = self.module()

        class Store:
            def __init__(self):
                self.completed = []

            def claim_ingress(self, **kwargs):
                return {"processingRevision": 2}

            def receipt(self, selected_scope, receipt_id):
                return {
                    "scope": selected_scope,
                    "receiptId": receipt_id,
                    "connectionId": "stripe-primary",
                    "provider": "stripe",
                    "mode": "test",
                    "eventType": "account.application.deauthorized",
                    "accountHash": hashlib.sha256(b"acct_synthetic").hexdigest(),
                    "payloadHash": "b" * 64,
                    "status": "processing",
                    "revision": 2,
                    "decisionCode": "processing",
                    "eventCreatedAt": 1_799_999_995,
                    "receivedAt": 1_800_000_000,
                    "expiresAt": 1_800_000_000 + 90 * 24 * 60 * 60,
                }

            def complete_ingress(self, **kwargs):
                self.completed.append(kwargs)

        class Registry:
            def __init__(self):
                self.disabled = []

            def connection(self, selected_scope, connection_id):
                return connection()

            def disable_stripe_account(self, *args):
                self.disabled.append(args)

        class Provider:
            def retrieve_webhook_state(self, *args):
                digest = hashlib.sha256(b"acct_synthetic").hexdigest()
                return {
                    "eventId": "evt-deauthorized",
                    "eventType": "account.application.deauthorized",
                    "eventCreatedAt": 1_799_999_995,
                    "mode": "test",
                    "accountHash": digest,
                    "objectType": "account",
                    "objectId": digest,
                    "mappingHint": None,
                    "canonical": {"accountHash": digest},
                }

        class Mappings:
            def __getattr__(self, name):
                raise AssertionError(f"mapping access is forbidden: {name}")

        store = Store()
        registry = Registry()
        worker = module.StripeEventWorker(registry, store, Mappings(), Provider())
        record = module._ingress_record(
            stream_record("101", event_id="evt-deauthorized")
        )

        worker.process(record, "101")

        self.assertEqual(registry.disabled[0][2:], ("acct_synthetic", 1))
        self.assertEqual(store.completed[0]["decision_code"], "processed")
        self.assertEqual(store.completed[0]["envelopes"], [])
        self.assertIsNone(store.completed[0]["projection"])

    def test_worker_refetches_scoped_state_and_emits_closed_payment_event(self):
        class Store:
            def __init__(self):
                self.completed = []

            def claim_ingress(self, **kwargs):
                return {"processingRevision": 2}

            def receipt(self, selected_scope, receipt_id):
                return {
                    "scope": selected_scope,
                    "receiptId": receipt_id,
                    "connectionId": "stripe-primary",
                    "provider": "stripe",
                    "mode": "test",
                    "eventType": "checkout.session.completed",
                    "accountHash": hashlib.sha256(b"acct_synthetic").hexdigest(),
                    "payloadHash": "b" * 64,
                    "status": "processing",
                    "revision": 2,
                    "decisionCode": "processing",
                    "eventCreatedAt": 1_799_999_995,
                    "receivedAt": 1_800_000_000,
                    "expiresAt": 1_800_000_000 + 90 * 24 * 60 * 60,
                }

            def complete_ingress(self, **kwargs):
                self.completed.append(kwargs)

        class Registry:
            def connection(self, selected_scope, connection_id):
                self.call = (selected_scope, connection_id)
                return connection()

        class Provider:
            def retrieve_webhook_state(self, selected_connection, event_id, event_type):
                self.call = (selected_connection, event_id, event_type)
                return {
                    "eventId": "evt-101",
                    "eventType": "checkout.session.completed",
                    "eventCreatedAt": 1_799_999_995,
                    "mode": "test",
                    "accountHash": hashlib.sha256(b"acct_synthetic").hexdigest(),
                    "objectType": "checkout-session",
                    "objectId": "cs_test_synthetic01",
                    "mappingHint": "attempt-1",
                    "canonical": {
                        "sessionId": "cs_test_synthetic01",
                        "status": "complete",
                        "paymentStatus": "paid",
                        "mode": "payment",
                        "paymentIntentId": "pi_synthetic01",
                        "subscriptionId": None,
                        "latestInvoiceId": None,
                    },
                }

        class Mappings:
            def __init__(self):
                self.bind_calls = []

            def object_owner(
                self, selected_scope, connection_id, object_type, provider_id
            ):
                self.call = (selected_scope, connection_id, object_type, provider_id)
                return {
                    "resourceType": "checkout",
                    "resourceId": "attempt-1",
                    "reservationId": "reservation-1",
                    "orderId": "order-1",
                    "paymentAttemptId": "attempt-1",
                    "offerVersionIds": ["offer-v1"],
                    "primaryOfferVersionId": "offer-v1",
                    "sessionId": "cs_test_synthetic01",
                    "revision": 1,
                    "status": "created",
                }

            def bind_checkout_objects(self, *args, **kwargs):
                self.bind_calls.append((args, kwargs))

        store, registry, provider, mappings = (
            Store(),
            Registry(),
            Provider(),
            Mappings(),
        )
        worker = self.module().StripeEventWorker(registry, store, mappings, provider)
        record = self.module()._ingress_record(stream_record("101"))

        worker.process(record, "101")

        completed = store.completed[0]
        self.assertEqual(completed["decision_code"], "processed")
        self.assertEqual(len(completed["envelopes"]), 1)
        envelope = completed["envelopes"][0]
        self.assertEqual(envelope["eventType"], "commerce.payment.succeeded.v1")
        self.assertEqual(
            envelope["data"],
            {
                "reservationId": "reservation-1",
                "orderId": "order-1",
                "paymentAttemptId": "attempt-1",
            },
        )
        self.assertNotIn("acct_", repr(completed))
        self.assertNotIn("pi_synthetic", repr(completed))
        self.assertEqual(len(mappings.bind_calls), 1)
        self.assertEqual(
            mappings.bind_calls[0][1],
            {
                "payment_intent_id": "pi_synthetic01",
                "subscription_id": None,
            },
        )

    def test_hinted_mapping_lag_retries_then_moves_to_needs_review_without_event(self):
        module = self.module()

        class Store:
            def __init__(self):
                self.retries = []
                self.completed = []

            def claim_ingress(self, **kwargs):
                return {"processingRevision": 2}

            def receipt(self, selected_scope, receipt_id):
                return {
                    "scope": selected_scope,
                    "receiptId": receipt_id,
                    "connectionId": "stripe-primary",
                    "provider": "stripe",
                    "mode": "test",
                    "eventType": "customer.subscription.updated",
                    "accountHash": hashlib.sha256(b"acct_synthetic").hexdigest(),
                    "payloadHash": "b" * 64,
                    "status": "processing",
                    "revision": 2,
                    "decisionCode": "processing",
                    "eventCreatedAt": 1_799_999_995,
                    "receivedAt": 1_800_000_000,
                    "expiresAt": 1_800_000_000 + 90 * 24 * 60 * 60,
                }

            def retry_ingress(self, **kwargs):
                self.retries.append(kwargs)

            def complete_ingress(self, **kwargs):
                self.completed.append(kwargs)

        class Registry:
            def connection(self, selected_scope, connection_id):
                return connection()

        class Provider:
            def __init__(self, hint="attempt-1"):
                self.hint = hint

            def retrieve_webhook_state(self, *args):
                return {
                    "eventId": "evt-lag",
                    "eventType": "customer.subscription.updated",
                    "eventCreatedAt": 1_799_999_995,
                    "mode": "test",
                    "accountHash": hashlib.sha256(b"acct_synthetic").hexdigest(),
                    "objectType": "subscription",
                    "objectId": "sub_synthetic01",
                    "mappingHint": self.hint,
                    "canonical": {
                        "subscriptionId": "sub_synthetic01",
                        "status": "active",
                        "currentPeriodEnd": 1_900_000_000,
                        "latestInvoiceId": "in_synthetic01",
                        "priceId": "price_synthetic01",
                        "pauseCollection": None,
                    },
                }

        class Mappings:
            def __init__(self):
                self.checkout = None
                self.offer_id = "offer-v2"
                self.bound = []

            def object_owner(self, *args):
                if len(args) >= 4 and args[2] == "price" and self.checkout is not None:
                    return {
                        "resourceType": "offer",
                        "resourceId": self.offer_id,
                        "priceId": "price_synthetic01",
                    }
                return None

            def get_mapping(self, *args):
                return self.checkout

            def bind_checkout_objects(self, *args, **kwargs):
                self.bound.append((args, kwargs))

        store = Store()
        mappings = Mappings()
        worker = module.StripeEventWorker(Registry(), store, mappings, Provider())
        first = module._ingress_record(stream_record("101", event_id="evt-lag"))
        worker.process(first, "101")
        self.assertEqual(len(store.retries), 1)
        self.assertEqual(store.completed, [])

        final = {**first, "attemptCount": 2}
        worker.process(final, "102")
        self.assertEqual(len(store.retries), 1)
        self.assertEqual(store.completed[-1]["decision_code"], "needs_review")
        self.assertEqual(store.completed[-1]["envelopes"], [])

        external_store = Store()
        external_worker = module.StripeEventWorker(
            Registry(), external_store, mappings, Provider(None)
        )
        external_worker.process(first, "104")
        self.assertEqual(external_store.retries, [])
        self.assertEqual(
            external_store.completed[-1]["decision_code"], "ignored_unmapped"
        )

        mappings.checkout = {
            "resourceType": "checkout",
            "resourceId": "attempt-1",
            "reservationId": "reservation-1",
            "orderId": "order-1",
            "paymentAttemptId": "attempt-1",
            "offerVersionIds": ["offer-v1"],
            "primaryOfferVersionId": "offer-v1",
            "sessionId": "cs_test_synthetic01",
            "revision": 1,
            "status": "pending",
        }
        mismatch_store = Store()
        mismatch_worker = module.StripeEventWorker(
            Registry(), mismatch_store, mappings, Provider()
        )
        mismatch_worker.process(first, "103")
        self.assertEqual(mismatch_store.completed[-1]["decision_code"], "needs_review")
        self.assertEqual(mismatch_store.completed[-1]["envelopes"], [])
        self.assertEqual(mappings.bound, [])
        mappings.offer_id = "offer-v1"
        self.assertTrue(
            mismatch_worker._subscription_offer_is_authorized(
                scope(),
                "stripe-primary",
                Provider().retrieve_webhook_state(None, None, None),
                mappings.checkout,
            )
        )

    def test_hint_resolves_checkout_mapping_before_provider_index_exists(self):
        module = self.module()

        class Mappings:
            def __init__(self):
                self.bound = []

            def object_owner(self, *args):
                return None

            def get_mapping(self, selected_scope, connection_id, kind, resource_id):
                self.lookup = (selected_scope, connection_id, kind, resource_id)
                return {
                    "resourceType": "checkout",
                    "resourceId": "attempt-1",
                    "reservationId": "reservation-1",
                    "orderId": "order-1",
                    "paymentAttemptId": "attempt-1",
                    "offerVersionIds": ["offer-v1"],
                    "primaryOfferVersionId": "offer-v1",
                    "revision": 1,
                    "status": "pending",
                }

            def bind_checkout_objects(self, *args, **kwargs):
                self.bound.append((args, kwargs))

        mappings = Mappings()
        worker = module.StripeEventWorker(object(), object(), mappings, object())
        mapping = worker._mapping(
            scope(),
            "stripe-primary",
            {
                "objectType": "subscription",
                "objectId": "sub_synthetic01",
                "mappingHint": "attempt-1",
                "canonical": {"subscriptionId": "sub_synthetic01"},
            },
        )
        self.assertEqual(mapping["paymentAttemptId"], "attempt-1")
        self.assertEqual(mappings.lookup[2:], ("checkout", "attempt-1"))

    def test_pause_collection_does_not_invent_a_paused_commerce_status(self):
        normalized = self.module()._subscription_status(
            {"status": "active", "pauseCollection": {"behavior": "void"}}
        )
        self.assertEqual(normalized, "active")
        with self.assertRaises(ValueError):
            self.module()._subscription_status(
                {"status": "paused", "pauseCollection": None}
            )

    def test_refund_mapping_falls_back_to_the_canonical_charge(self):
        class Mappings:
            def object_owner(self, *args):
                self.call = args
                return {"resourceId": "attempt-1"}

        mappings = Mappings()
        worker = self.module().StripeEventWorker(object(), object(), mappings, object())
        result = worker._mapping(
            scope(),
            "stripe-primary",
            {
                "objectType": "refund",
                "objectId": "re_synthetic01",
                "canonical": {
                    "paymentIntentId": None,
                    "chargeId": "ch_synthetic01",
                },
            },
        )
        self.assertEqual(result, {"resourceId": "attempt-1"})
        self.assertEqual(mappings.call[2:], ("charge", "ch_synthetic01"))

    def test_refund_subscription_and_invoice_normalize_only_existing_commerce_contracts(
        self,
    ):
        module = self.module()
        selected_scope = scope()
        base_receipt = {
            "receiptId": "evt-1",
            "eventCreatedAt": 1_800_000_000,
            "eventType": "refund.updated",
        }
        mapping = {
            "resourceType": "checkout",
            "resourceId": "attempt-1",
            "reservationId": "reservation-1",
            "orderId": "order-1",
            "paymentAttemptId": "attempt-1",
            "offerVersionIds": ["offer-v1"],
            "primaryOfferVersionId": "offer-v1",
        }

        refund_events, decision, projection = module._normalized_events(
            selected_scope,
            base_receipt,
            {
                "objectType": "refund",
                "objectId": "re_synthetic01",
                "canonical": {
                    "refundId": "re_synthetic01",
                    "status": "succeeded",
                    "amountMinor": 90000,
                    "currency": "MXN",
                    "paymentIntentId": "pi_synthetic01",
                    "chargeId": None,
                },
            },
            mapping,
            None,
        )
        self.assertEqual(decision, "processed")
        self.assertIsNone(projection)
        self.assertEqual(refund_events[0]["eventType"], "commerce.refund.confirmed.v1")
        self.assertEqual(
            refund_events[0]["data"],
            {
                "orderId": "order-1",
                "refundId": refund_events[0]["data"]["refundId"],
                "amountMinor": 90000,
                "currency": "MXN",
            },
        )
        self.assertNotIn("re_synthetic01", repr(refund_events))

        class ProjectionStore:
            def plan_subscription_projection(self, **kwargs):
                return {
                    "subscriptionId": kwargs["subscription_id"],
                    "offerVersionId": kwargs["offer_version_id"],
                    "status": kwargs["status"],
                    "currentPeriodEnd": kwargs["current_period_end"],
                    "sourceRevision": 3,
                    "expectedRevision": 2,
                    "lastEventId": kwargs["event_id"],
                    "lastEventCreatedAt": kwargs["event_created_at"],
                    "stateHash": kwargs["state_hash"],
                    "stale": False,
                }

        subscription_events, decision, projection = module._normalized_events(
            selected_scope,
            {**base_receipt, "eventType": "customer.subscription.updated"},
            {
                "objectType": "subscription",
                "objectId": "sub_synthetic01",
                "canonical": {
                    "subscriptionId": "sub_synthetic01",
                    "status": "active",
                    "currentPeriodEnd": 1_900_000_000,
                    "latestInvoiceId": "in_synthetic01",
                    "priceId": "price_synthetic01",
                    "pauseCollection": {"behavior": "void"},
                },
            },
            mapping,
            ProjectionStore(),
        )
        self.assertEqual(decision, "processed")
        self.assertEqual(projection["sourceRevision"], 3)
        self.assertEqual(subscription_events[0]["data"]["status"], "active")
        self.assertEqual(
            {event["eventType"] for event in refund_events + subscription_events},
            {
                "commerce.refund.confirmed.v1",
                "commerce.subscription.updated.v1",
            },
        )

    def test_subscription_invoices_emit_only_canonical_subscription_updates(self):
        module = self.module()

        class ProjectionStore:
            def plan_subscription_projection(self, **kwargs):
                return {
                    "subscriptionId": kwargs["subscription_id"],
                    "offerVersionId": kwargs["offer_version_id"],
                    "status": kwargs["status"],
                    "currentPeriodEnd": kwargs["current_period_end"],
                    "sourceRevision": 1,
                    "expectedRevision": 0,
                    "lastEventId": kwargs["event_id"],
                    "lastEventCreatedAt": kwargs["event_created_at"],
                    "stateHash": kwargs["state_hash"],
                    "stale": False,
                }

        mapping = {
            "reservationId": "reservation-1",
            "orderId": "order-1",
            "paymentAttemptId": "initial-attempt",
            "offerVersionIds": ["offer-v1"],
            "primaryOfferVersionId": "offer-v1",
        }
        subscription = {
            "subscriptionId": "sub_synthetic01",
            "status": "active",
            "currentPeriodEnd": 1_900_000_000,
            "latestInvoiceId": "in_synthetic01",
            "priceId": "price_synthetic01",
            "pauseCollection": None,
        }

        events, decision, _ = module._normalized_events(
            scope(),
            {
                "receiptId": "evt-invoice-paid",
                "eventCreatedAt": 1_800_000_000,
                "eventType": "invoice.paid",
            },
            {
                "objectType": "invoice",
                "objectId": "in_synthetic01",
                "canonical": {
                    "invoiceId": "in_synthetic01",
                    "status": "paid",
                    "paid": True,
                    "subscriptionId": "sub_synthetic01",
                    "subscription": subscription,
                },
            },
            mapping,
            ProjectionStore(),
        )

        self.assertEqual(decision, "processed")
        self.assertEqual(
            [event["eventType"] for event in events],
            ["commerce.subscription.updated.v1"],
        )

    def test_all_eleven_allowed_stripe_events_map_only_to_four_commerce_contracts(self):
        module = self.module()
        selected_scope = scope()
        mapping = {
            "resourceType": "checkout",
            "resourceId": "attempt-1",
            "reservationId": "reservation-1",
            "orderId": "order-1",
            "paymentAttemptId": "attempt-1",
            "offerVersionIds": ["offer-v1"],
            "primaryOfferVersionId": "offer-v1",
        }

        class ProjectionStore:
            def plan_subscription_projection(self, **kwargs):
                return {
                    "subscriptionId": kwargs["subscription_id"],
                    "offerVersionId": kwargs["offer_version_id"],
                    "status": kwargs["status"],
                    "currentPeriodEnd": kwargs["current_period_end"],
                    "sourceRevision": 1,
                    "expectedRevision": 0,
                    "lastEventId": kwargs["event_id"],
                    "lastEventCreatedAt": kwargs["event_created_at"],
                    "stateHash": kwargs["state_hash"],
                    "stale": False,
                }

        checkout_paid = {
            "sessionId": "cs_test_synthetic01",
            "status": "complete",
            "paymentStatus": "paid",
            "mode": "payment",
            "paymentIntentId": "pi_synthetic01",
            "subscriptionId": None,
            "latestInvoiceId": None,
        }
        checkout_unpaid = {
            **checkout_paid,
            "status": "expired",
            "paymentStatus": "unpaid",
        }
        refund = {
            "refundId": "re_synthetic01",
            "status": "succeeded",
            "amountMinor": 90000,
            "currency": "MXN",
            "paymentIntentId": "pi_synthetic01",
            "chargeId": None,
        }
        subscription_active = {
            "subscriptionId": "sub_synthetic01",
            "status": "active",
            "currentPeriodEnd": 1_900_000_000,
            "latestInvoiceId": "in_synthetic01",
            "priceId": "price_synthetic01",
            "pauseCollection": None,
        }
        subscription_past_due = {
            **subscription_active,
            "status": "past_due",
        }
        subscription_canceled = {
            **subscription_active,
            "status": "canceled",
        }
        cases = (
            ("checkout.session.completed", "checkout-session", checkout_paid),
            ("checkout.session.expired", "checkout-session", checkout_unpaid),
            (
                "checkout.session.async_payment_succeeded",
                "checkout-session",
                checkout_paid,
            ),
            (
                "checkout.session.async_payment_failed",
                "checkout-session",
                {**checkout_paid, "paymentStatus": "failed"},
            ),
            ("refund.created", "refund", refund),
            ("refund.updated", "refund", refund),
            (
                "customer.subscription.created",
                "subscription",
                subscription_active,
            ),
            (
                "customer.subscription.updated",
                "subscription",
                subscription_past_due,
            ),
            (
                "customer.subscription.deleted",
                "subscription",
                subscription_canceled,
            ),
            (
                "invoice.paid",
                "invoice",
                {
                    "invoiceId": "in_synthetic01",
                    "status": "paid",
                    "paid": True,
                    "subscriptionId": "sub_synthetic01",
                    "subscription": subscription_active,
                },
            ),
            (
                "invoice.payment_failed",
                "invoice",
                {
                    "invoiceId": "in_synthetic01",
                    "status": "open",
                    "paid": False,
                    "subscriptionId": "sub_synthetic01",
                    "subscription": subscription_past_due,
                },
            ),
        )
        produced = set()
        for index, (event_type, object_type, canonical) in enumerate(cases, start=1):
            object_id = {
                "checkout-session": "cs_test_synthetic01",
                "refund": "re_synthetic01",
                "subscription": "sub_synthetic01",
                "invoice": "in_synthetic01",
            }[object_type]
            with self.subTest(event_type=event_type):
                events, decision, _ = module._normalized_events(
                    selected_scope,
                    {
                        "receiptId": f"evt-{index}",
                        "eventCreatedAt": 1_800_000_000 + index,
                        "eventType": event_type,
                    },
                    {
                        "objectType": object_type,
                        "objectId": object_id,
                        "canonical": canonical,
                    },
                    mapping,
                    ProjectionStore(),
                )
                self.assertEqual(decision, "processed")
                produced.update(event["eventType"] for event in events)

        self.assertEqual(
            produced,
            {
                "commerce.payment.succeeded.v1",
                "commerce.payment.terminal_unpaid.v1",
                "commerce.refund.confirmed.v1",
                "commerce.subscription.updated.v1",
            },
        )


if __name__ == "__main__":
    unittest.main()
