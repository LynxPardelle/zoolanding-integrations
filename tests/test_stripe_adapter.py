import copy
import importlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_registry import binding, connection
from tests.test_stripe_commands import checkout_command, checkout_with_add_on, resolved


def stripe_module(testcase):
    testcase.assertIsNotNone(
        importlib.util.find_spec("src.providers.stripe_adapter"),
        "Stripe adapter boundary is not implemented",
    )
    return importlib.import_module("src.providers.stripe_adapter")


class StripeClient:
    def __init__(self, url="https://connect.stripe.com/setup/synthetic"):
        self.url = url
        self.calls = []

    def create_v1_handoff(self, **kwargs):
        self.calls.append(("v1", kwargs))
        return {"url": self.url}

    def create_oauth_handoff(self, **kwargs):
        self.calls.append(("oauth", kwargs))
        return {"url": self.url}

    def exchange_oauth_code(self, **kwargs):
        self.calls.append(("oauth-exchange", kwargs))
        return {"accountReference": "acct_synthetic"}

    def deauthorize_oauth_account(self, **kwargs):
        self.calls.append(("oauth-deauthorize", kwargs))

    def create_controller_account(self, **kwargs):
        self.calls.append(("controller-create", kwargs))
        return {"id": "acct_synthetic"}

    def create_v2_handoff(self, **kwargs):
        self.calls.append(("v2", kwargs))
        return {"url": self.url}

    def retrieve_v1_account(self, **kwargs):
        self.calls.append(("retrieve-v1", kwargs))
        return {
            "id": "acct_synthetic",
            "charges_enabled": True,
            "payouts_enabled": True,
            "details_submitted": True,
            "capabilities": {"card_payments": "active"},
            "requirements": {"currently_due": []},
        }

    def retrieve_v2_account(self, **kwargs):
        self.calls.append(("retrieve-v2", kwargs))
        return {
            "id": "acct_synthetic",
            "charges_enabled": False,
            "payouts_enabled": True,
            "details_submitted": True,
            "capabilities": {"card_payments": "pending"},
            "requirements": {"currently_due": []},
        }


class StripeAdapterTests(unittest.TestCase):
    def test_schedule_restore_uses_unique_current_anchor_suffix(self):
        stripe = stripe_module(self)
        from tests.test_subscription_migration_domain import snapshot

        class Schedules:
            def __init__(self):
                self.calls = []

            def update(self, *args):
                self.calls.append(args)
                return {
                    "id": "sub_sched_synthetic",
                    "subscription": "sub_synthetic",
                }

        schedules = Schedules()
        sdk = stripe.StripeSdkClient(
            SimpleNamespace(v1=SimpleNamespace(subscription_schedules=schedules))
        )
        original = snapshot()["schedule"]
        anchor = original["phases"][1]["startDate"]

        result = sdk.release_migration_schedule(
            stripe_account="acct_synthetic",
            schedule_id="sub_sched_synthetic",
            subscription_id="sub_synthetic",
            original_schedule=original,
            current_phase_anchor=anchor,
            idempotency_key="migration-key",
        )

        self.assertEqual(result, {"status": "reverted"})
        payload = schedules.calls[0][1]
        self.assertEqual(len(payload["phases"]), 1)
        self.assertEqual(payload["phases"][0]["start_date"], anchor)

        ambiguous = copy.deepcopy(original)
        ambiguous["phases"][0]["startDate"] = anchor
        call_count = len(schedules.calls)
        with self.assertRaisesRegex(stripe.MigrationNeedsReview, "source-drift"):
            sdk.release_migration_schedule(
                stripe_account="acct_synthetic",
                schedule_id="sub_sched_synthetic",
                subscription_id="sub_synthetic",
                original_schedule=ambiguous,
                current_phase_anchor=anchor,
                idempotency_key="migration-key-ambiguous",
            )
        self.assertEqual(len(schedules.calls), call_count)

    def test_subscription_send_invoice_reads_days_until_due_from_subscription(self):
        stripe = stripe_module(self)
        raw = {
            "id": "sub_send_invoice",
            "status": "active",
            "current_period_start": 1_800_000_000,
            "current_period_end": 1_802_678_400,
            "collection_method": "send_invoice",
            "days_until_due": 21,
            "default_payment_method": None,
            "items": {
                "data": [{
                    "id": "si_send_invoice",
                    "price": {
                        "id": "price_old",
                        "currency": "mxn",
                        "recurring": {
                            "interval": "month",
                            "interval_count": 1,
                            "usage_type": "licensed",
                        },
                    },
                    "quantity": 1,
                    "current_period_start": 1_800_000_000,
                    "current_period_end": 1_802_678_400,
                    "tax_rates": [],
                    "billing_thresholds": None,
                    "discounts": [],
                    "metadata": {},
                }],
                "has_more": False,
            },
            "discounts": [],
            "automatic_tax": {
                "enabled": False,
                "liability": None,
                "disabled_reason": None,
            },
            "billing_thresholds": None,
            "default_tax_rates": [],
            "invoice_settings": {"issuer": {"type": "self"}},
            "metadata": {},
            "pending_update": None,
            "latest_invoice": None,
        }

        try:
            selected = stripe._migration_snapshot_from_stripe(
                raw,
                schedule=None,
                pending_invoice_items={"data": [], "has_more": False},
            )
        except stripe.MigrationNeedsReview as error:
            self.fail(f"top-level days_until_due was not preserved: {error}")

        self.assertEqual(selected["collectionMethod"], "send_invoice")
        self.assertEqual(selected["invoiceSettings"]["daysUntilDue"], 21)

        legacy = dict(raw)
        legacy["default_source"] = "src_legacy"
        with self.assertRaisesRegex(
            stripe.MigrationNeedsReview, "unsupported-payment-method"
        ):
            stripe._migration_snapshot_from_stripe(
                legacy,
                schedule=None,
                pending_invoice_items={"data": [], "has_more": False},
            )

    def test_schedule_round_trip_preserves_safe_defaults_thresholds_invoice_terms_and_item_discounts(self):
        stripe = stripe_module(self)
        raw = {
            "id": "sub_sched_synthetic",
            "status": "active",
            "end_behavior": "release",
            "current_phase": {
                "start_date": 1_800_000_000,
                "end_date": 1_800_001_000,
            },
            "default_settings": {
                "application_fee_percent": None,
                "automatic_tax": {
                    "enabled": False,
                    "liability": None,
                    "disabled_reason": None,
                },
                "billing_cycle_anchor": "automatic",
                "billing_thresholds": {
                    "amount_gte": 50_000,
                    "reset_billing_cycle_anchor": False,
                },
                "collection_method": "send_invoice",
                "default_payment_method": "pm_synthetic",
                "default_source": None,
                "description": None,
                "invoice_settings": {
                    "issuer": {"type": "self"},
                    "days_until_due": 15,
                },
                "on_behalf_of": None,
                "transfer_data": None,
            },
            "phases": [{
                "start_date": 1_800_000_000,
                "end_date": 1_800_001_000,
                "items": [{
                    "price": {
                        "id": "price_old",
                        "currency": "mxn",
                        "recurring": {
                            "interval": "month",
                            "interval_count": 1,
                            "usage_type": "licensed",
                        },
                    },
                    "quantity": 2,
                    "tax_rates": [],
                    "billing_thresholds": {"usage_gte": 25},
                    "discounts": [{"id": "di_item_synthetic"}],
                    "metadata": {},
                }],
                "discounts": [],
                "automatic_tax": None,
                "billing_cycle_anchor": None,
                "billing_thresholds": None,
                "collection_method": None,
                "default_payment_method": None,
                "default_tax_rates": [],
                "description": None,
                "invoice_settings": None,
                "metadata": {},
                "proration_behavior": "none",
            }],
        }

        selected = stripe._migration_schedule_from_stripe(raw)

        self.assertEqual(selected["defaultSettings"]["collectionMethod"], "send_invoice")
        self.assertEqual(selected["defaultSettings"]["invoiceSettings"]["daysUntilDue"], 15)
        self.assertEqual(selected["phases"][0]["billingThresholds"]["amountGte"], 50_000)
        self.assertEqual(selected["phases"][0]["items"][0]["billingThresholds"], {"usageGte": 25})
        self.assertEqual(selected["phases"][0]["items"][0]["discountIds"], ["di_item_synthetic"])
        encoded_phase = stripe._stripe_migration_phase(selected["phases"][0])
        self.assertEqual(encoded_phase["invoice_settings"]["days_until_due"], 15)
        self.assertEqual(encoded_phase["billing_thresholds"]["amount_gte"], 50_000)
        self.assertEqual(encoded_phase["items"][0]["billing_thresholds"], {"usage_gte": 25})
        self.assertEqual(encoded_phase["items"][0]["discounts"], [{"discount": "di_item_synthetic"}])
        encoded_defaults = stripe._stripe_migration_default_settings(
            selected["defaultSettings"]
        )
        self.assertEqual(encoded_defaults["collection_method"], "send_invoice")
        self.assertEqual(encoded_defaults["invoice_settings"]["days_until_due"], 15)

        raw["phases"][0]["description"] = "customer content"
        with self.assertRaises(stripe.MigrationNeedsReview):
            stripe._migration_schedule_from_stripe(raw)

    def test_schedule_create_update_partial_failure_preserves_id_for_safe_restart(self):
        stripe = stripe_module(self)
        from src.subscription_migrations import build_next_renewal_plan
        from tests.test_subscription_migration_domain import snapshot

        class Throttled(RuntimeError):
            http_status = 503

        class Schedules:
            def __init__(self):
                self.create_calls = []
                self.update_calls = []
                self.fail = True
                self.invalid_response = False

            def create(self, *args):
                self.create_calls.append(args)
                return {
                    "id": "sub_sched_partial",
                    "subscription": "sub_synthetic",
                }

            def update(self, *args):
                self.update_calls.append(args)
                if self.fail:
                    raise Throttled("synthetic")
                if self.invalid_response:
                    return {"id": "sub_sched_unexpected", "subscription": "sub_synthetic"}
                return {
                    "id": "sub_sched_partial",
                    "subscription": "sub_synthetic",
                }

        schedules = Schedules()
        sdk = stripe.StripeSdkClient(
            SimpleNamespace(v1=SimpleNamespace(subscription_schedules=schedules))
        )
        selected = snapshot()
        selected["schedule"] = None
        plan = build_next_renewal_plan(selected, "price_old", "price_new")

        with self.assertRaises(stripe.StripeMigrationPartialError) as caught:
            sdk.apply_next_renewal_migration(
                stripe_account="acct_synthetic",
                subscription_id="sub_synthetic",
                plan=plan,
                existing_schedule_id=None,
                idempotency_key="migration-key",
            )
        self.assertEqual(caught.exception.schedule_id, "sub_sched_partial")
        self.assertTrue(caught.exception.retryable)

        schedules.fail = False
        result = sdk.apply_next_renewal_migration(
            stripe_account="acct_synthetic",
            subscription_id="sub_synthetic",
            plan=plan,
            existing_schedule_id=caught.exception.schedule_id,
            idempotency_key="migration-key",
        )
        self.assertEqual(result["scheduleId"], "sub_sched_partial")
        self.assertEqual(len(schedules.create_calls), 1)
        self.assertEqual(len(schedules.update_calls), 2)

        schedules.invalid_response = True
        with self.assertRaises(stripe.StripeMigrationPartialError) as invalid:
            sdk.apply_next_renewal_migration(
                stripe_account="acct_synthetic",
                subscription_id="sub_synthetic",
                plan=plan,
                existing_schedule_id="sub_sched_partial",
                idempotency_key="migration-key-invalid-response",
            )
        self.assertEqual(invalid.exception.schedule_id, "sub_sched_partial")
        self.assertFalse(invalid.exception.retryable)

    def test_migration_sdk_uses_real_invoice_items_surface_and_exact_mutations(self):
        stripe = stripe_module(self)

        class Resource:
            def __init__(self, *, retrieved=None, listed=None, created=None, updated=None, preview=None, released=None):
                self.retrieved = retrieved
                self.listed = listed
                self.created = created
                self.updated = updated
                self.preview = preview
                self.released = released
                self.calls = []

            def retrieve(self, *args):
                self.calls.append(("retrieve", args))
                return self.retrieved

            def list(self, *args):
                self.calls.append(("list", args))
                return self.listed

            def create(self, *args):
                self.calls.append(("create", args))
                return self.created

            def update(self, *args):
                self.calls.append(("update", args))
                return self.updated

            def create_preview(self, *args):
                self.calls.append(("create_preview", args))
                return self.preview

            def release(self, *args):
                self.calls.append(("release", args))
                return self.released

        raw_subscription = {
            "id": "sub_synthetic01",
            "customer": "cus_synthetic01",
            "status": "active",
            "current_period_start": 1_800_000_000,
            "current_period_end": 1_802_678_400,
            "collection_method": "charge_automatically",
            "default_payment_method": {"id": "pm_synthetic01", "type": "card"},
            "items": {
                "data": [{
                    "id": "si_synthetic01",
                    "price": {
                        "id": "price_old",
                        "currency": "mxn",
                        "recurring": {
                            "interval": "month",
                            "interval_count": 1,
                            "usage_type": "licensed",
                        },
                    },
                    "quantity": 2,
                    "current_period_start": 1_800_000_000,
                    "current_period_end": 1_802_678_400,
                    "tax_rates": [],
                    "billing_thresholds": {"usage_gte": 25},
                    "discounts": [{"id": "di_item_synthetic"}],
                    "metadata": {"resource_id": "offer-old"},
                }],
                "has_more": False,
            },
            "schedule": None,
            "discounts": [],
            "automatic_tax": {"enabled": True},
            "default_tax_rates": [],
            "invoice_settings": {"issuer": {"type": "self"}},
            "metadata": {"order_id": "order-1"},
            "pending_update": None,
            "latest_invoice": {
                "id": "in_synthetic01",
                "status": "paid",
                "payments": {
                    "data": [{"is_default": True, "status": "paid"}],
                    "has_more": False,
                },
            },
        }
        subscriptions = Resource(
            retrieved=raw_subscription,
            listed={"data": [raw_subscription], "has_more": False},
            updated={
                "id": "sub_synthetic01",
                "pending_update": {"expires_at": 1_800_001_000},
                "latest_invoice": {
                    "hosted_invoice_url": "https://invoice.stripe.com/i/synthetic",
                    "payments": {
                        "data": [{
                            "is_default": True,
                            "status": "open",
                            "payment": {
                                "type": "payment_intent",
                                "payment_intent": {
                                    "id": "pi_synthetic01",
                                    "status": "requires_action",
                                    "next_action": {"type": "use_stripe_sdk"},
                                    "client_secret": "must-never-be-returned",
                                },
                            },
                        }],
                        "has_more": False,
                    }
                },
            },
        )
        invoice_items = Resource(listed={"data": [], "has_more": False})
        invoices = Resource(
            preview={
                "lines": {
                    "data": [
                        {"amount": -500, "proration": True, "subscription_item": "si_synthetic01"},
                        {"amount": 1500, "parent": {"subscription_item_details": {"proration": True, "subscription_item": "si_synthetic01"}}},
                        {"amount": 100_000, "proration": False},
                    ],
                    "has_more": False,
                }
            }
        )
        schedules = Resource(
            created={"id": "sub_sched_synthetic01", "subscription": "sub_synthetic01"},
            updated={"id": "sub_sched_synthetic01", "subscription": "sub_synthetic01"},
            released={"id": "sub_sched_synthetic01", "released_subscription": "sub_synthetic01"},
        )
        client = SimpleNamespace(
            v1=SimpleNamespace(
                subscriptions=subscriptions,
                invoice_items=invoice_items,
                invoices=invoices,
                subscription_schedules=schedules,
            )
        )
        sdk = stripe.StripeSdkClient(client)

        listed = sdk.list_migration_candidates(
            stripe_account="acct_synthetic",
            source_price_id="price_old",
            cursor=None,
        )
        self.assertEqual(listed, {"subscriptionIds": ["sub_synthetic01"], "nextCursor": None})
        selected = sdk.retrieve_migration_snapshot(
            stripe_account="acct_synthetic", subscription_id="sub_synthetic01"
        )
        self.assertEqual(selected["items"][0]["priceId"], "price_old")
        self.assertEqual(selected["items"][0]["billingThresholds"], {"usageGte": 25})
        self.assertEqual(selected["items"][0]["discountIds"], ["di_item_synthetic"])
        self.assertEqual(invoice_items.calls[0][0], "list")

        preview = sdk.preview_migration_proration(
            stripe_account="acct_synthetic",
            subscription_id="sub_synthetic01",
            item_id="si_synthetic01",
            quantity=2,
            source_price_id="price_old",
            target_price_id="price_new",
            proration_timestamp=1_800_000_100,
        )
        self.assertEqual(preview["amountMinor"], 1000)
        preview_params = invoices.calls[-1][1][0]["subscription_details"]
        self.assertEqual(
            preview_params["items"],
            [{"id": "si_synthetic01", "price": "price_new", "quantity": 2}],
        )

        immediate = sdk.apply_immediate_migration(
            stripe_account="acct_synthetic",
            plan={
                "subscriptionId": "sub_synthetic01",
                "itemId": "si_synthetic01",
                "priceId": "price_new",
                "quantity": 2,
                "prorationTimestamp": 1_800_000_100,
                "prorationBehavior": "always_invoice",
                "paymentBehavior": "pending_if_incomplete",
            },
            idempotency_key="migration-key",
        )
        self.assertEqual(immediate, {"status": "pending_customer_action"})
        self.assertNotIn("must-never-be-returned", repr(immediate))
        immediate_params = subscriptions.calls[-1][1][1]
        self.assertEqual(immediate_params["proration_date"], 1_800_000_100)
        self.assertEqual(
            immediate_params["expand"],
            ["latest_invoice.payments.data.payment.payment_intent"],
        )

        subscriptions.updated["latest_invoice"]["hosted_invoice_url"] = None
        with self.assertRaisesRegex(stripe.MigrationNeedsReview, "payment-failed"):
            sdk.apply_immediate_migration(
                stripe_account="acct_synthetic",
                plan={
                    "subscriptionId": "sub_synthetic01",
                    "itemId": "si_synthetic01",
                    "priceId": "price_new",
                    "quantity": 2,
                    "prorationTimestamp": 1_800_000_100,
                    "prorationBehavior": "always_invoice",
                    "paymentBehavior": "pending_if_incomplete",
                },
                idempotency_key="migration-key-recovery-missing",
            )
        subscriptions.updated["latest_invoice"]["hosted_invoice_url"] = (
            "https://invoice.stripe.com/i/synthetic"
        )

        plan = {
            "scheduleId": None,
            "defaultSettings": None,
            "endBehavior": "release",
            "prorationBehavior": "none",
            "phases": [{
                "startDate": 1_800_000_000,
                "endDate": 1_802_678_400,
                "items": [{
                    "priceId": "price_old",
                    "quantity": 2,
                    "taxRateIds": [],
                    "billingThresholds": None,
                    "discountIds": [],
                    "metadata": {},
                    "priceConfiguration": {"currency": "MXN", "recurring": {"interval": "month", "intervalCount": 1, "usageType": "licensed"}},
                }],
                "discountIds": [],
                "automaticTax": {"enabled": True},
                "billingThresholds": None,
                "defaultTaxRateIds": [],
                "collectionMethod": "charge_automatically",
                "defaultPaymentMethodId": "pm_synthetic01",
                "invoiceSettings": {"issuerType": "self", "daysUntilDue": None},
                "metadata": {},
                "prorationBehavior": "none",
            }],
        }
        scheduled = sdk.apply_next_renewal_migration(
            stripe_account="acct_synthetic",
            subscription_id="sub_synthetic01",
            plan=plan,
            idempotency_key="migration-key",
        )
        self.assertEqual(scheduled["scheduleId"], "sub_sched_synthetic01")
        create_key = schedules.calls[-2][1][-1]["idempotency_key"]
        update_key = schedules.calls[-1][1][-1]["idempotency_key"]
        self.assertNotEqual(create_key, update_key)
        self.assertNotIn("priceConfiguration", repr(schedules.calls[-1]))
        self.assertEqual(schedules.calls[-1][1][1]["proration_behavior"], "none")

        self.assertEqual(
            sdk.release_migration_schedule(
                stripe_account="acct_synthetic",
                schedule_id="sub_sched_synthetic01",
                subscription_id="sub_synthetic01",
                original_schedule=None,
                idempotency_key="migration-key",
            ),
            {"status": "reverted"},
        )

    def test_migration_provider_retryability_and_unsupported_data_fail_closed(self):
        stripe = stripe_module(self)

        class Throttled(RuntimeError):
            http_status = 429

        class Client(StripeClient):
            def list_migration_candidates(self, **kwargs):
                del kwargs
                raise Throttled("synthetic")

        with self.assertRaises(stripe.StripeAdapterRetryable):
            stripe.StripeAdapter(Client(), accounts_v2_verified=False).list_migration_candidates(
                resolved(), "price_old", None
            )

        raw = {
            "transfer_data": {"destination": "acct_other"},
            "items": {"data": [], "has_more": False},
            "collection_method": "charge_automatically",
        }
        with self.assertRaises(stripe.MigrationNeedsReview):
            stripe._migration_snapshot_from_stripe(
                raw, schedule=None, pending_invoice_items={"data": [], "has_more": False}
            )

        class InvalidPages:
            def __init__(self, response):
                self.response = response

            def list(self, *args):
                return self.response

        sdk = stripe.StripeSdkClient(
            SimpleNamespace(
                v1=SimpleNamespace(
                    subscriptions=InvalidPages({"data": [], "has_more": True})
                )
            )
        )
        with self.assertRaises(stripe.StripeAdapterError):
            sdk.list_migration_candidates(
                stripe_account="acct_synthetic",
                source_price_id="price_old",
                cursor="sub_same",
            )
        sdk.client.v1.subscriptions.response = {
            "data": [{"id": "sub_same"}],
            "has_more": True,
        }
        with self.assertRaises(stripe.StripeAdapterError):
            sdk.list_migration_candidates(
                stripe_account="acct_synthetic",
                source_price_id="price_old",
                cursor="sub_same",
            )

    def test_subscription_facade_accepts_the_complete_sdk_operation_state(self):
        stripe = stripe_module(self)

        class Client(StripeClient):
            def retrieve_subscription_operation_state(self, **kwargs):
                self.calls.append(("subscription-state", kwargs))
                return {
                    "subscriptionId": "sub_synthetic01",
                    "customerId": "cus_synthetic01",
                    "status": "active",
                    "items": [{
                        "itemId": "si_synthetic01",
                        "priceId": "price_synthetic01",
                        "quantity": 2,
                        "taxRateIds": ["txr_synthetic01"],
                    }],
                    "scheduleId": None,
                    "discounts": [],
                    "pauseCollection": None,
                    "latestInvoice": {
                        "invoiceId": "in_synthetic01",
                        "status": "paid",
                        "paymentStatus": "paid",
                    },
                    "pendingUpdate": False,
                    "automaticTax": {"enabled": True},
                    "defaultTaxRateIds": [],
                }

        client = Client()
        state = stripe.StripeAdapter(
            client, accounts_v2_verified=False
        ).retrieve_subscription_operation_state(resolved(), "sub_synthetic01")

        self.assertEqual(state["items"][0]["taxRateIds"], ["txr_synthetic01"])
        self.assertEqual(state["latestInvoice"]["paymentStatus"], "paid")
        self.assertEqual(
            client.calls,
            [("subscription-state", {
                "stripe_account": "acct_synthetic",
                "subscription_id": "sub_synthetic01",
            })],
        )

    def test_client_factory_accepts_only_structured_platform_secret_and_bounded_retry_client(
        self,
    ):
        stripe = stripe_module(self)
        constructed = []

        class HttpClient:
            def __init__(self, *, _lib):
                self.library = _lib

        class OfficialClient:
            def __init__(self, api_key, **kwargs):
                constructed.append((api_key, kwargs))

        fake_stripe = SimpleNamespace(
            StripeClient=OfficialClient,
            _http_client=SimpleNamespace(UrllibClient=HttpClient),
        )

        class Secrets:
            def __init__(self, value):
                self.value = value
                self.calls = []

            def get_secret_value(self, **kwargs):
                self.calls.append(kwargs)
                return {"SecretString": self.value}

        value = json.dumps(
            {
                "clientId": "ca_syntheticclient1234",
                "secretKey": "sk_test_syntheticsecret123456",
            }
        )
        secrets = Secrets(value)
        with patch.dict(sys.modules, {"stripe": fake_stripe}):
            client = stripe.SecretsManagerStripeClientFactory(secrets).client_for(
                connection(status="pending", account=None, ready=False)
            )

        self.assertIsInstance(client, stripe.StripeSdkClient)
        self.assertEqual(
            secrets.calls,
            [{"SecretId": "/zoolanding/test/integrations/stripe/connect-platform"}],
        )
        self.assertEqual(constructed[0][0], "sk_test_syntheticsecret123456")
        self.assertEqual(constructed[0][1]["client_id"], "ca_syntheticclient1234")
        self.assertEqual(constructed[0][1]["max_network_retries"], 1)
        self.assertIsInstance(constructed[0][1]["http_client"], HttpClient)

        for invalid in (
            "sk_test_syntheticsecret123456",
            json.dumps({"secretKey": "sk_test_syntheticsecret123456"}),
            json.dumps(
                {
                    "clientId": "ca_syntheticclient1234",
                    "secretKey": "sk_live_syntheticsecret123456",
                }
            ),
            json.dumps(
                {
                    "clientId": "ca_syntheticclient1234",
                    "secretKey": "sk_test_syntheticsecret123456",
                    "oauthToken": "synthetic",
                }
            ),
        ):
            with (
                self.subTest(invalid=invalid),
                patch.dict(sys.modules, {"stripe": fake_stripe}),
                self.assertRaises(stripe.StripeAdapterError),
            ):
                stripe.SecretsManagerStripeClientFactory(Secrets(invalid)).client_for(
                    connection(status="pending", account=None, ready=False)
                )

    def test_stdlib_transport_always_applies_the_three_second_network_timeout(self):
        stripe = stripe_module(self)
        with patch("urllib.request.urlopen", return_value="response") as opened:
            self.assertEqual(stripe._TimedUrllib.urlopen("request"), "response")
        opened.assert_called_once_with("request", timeout=3)

        opener = SimpleNamespace(open=lambda *args, **kwargs: (args, kwargs))
        timed = stripe._TimedOpener(opener)
        args, kwargs = timed.open("request")
        self.assertEqual(args, ("request",))
        self.assertEqual(kwargs, {"timeout": 3})

    def test_official_stripe_http_client_retries_429_timeout_and_5xx_once_with_backoff(
        self,
    ):
        try:
            import stripe as official_stripe
        except ModuleNotFoundError:
            build = (
                Path(__file__).parents[1]
                / ".aws-sam"
                / "build"
                / "InternalStripeCustomerPortalFunction"
            )
            with patch.object(sys, "path", [str(build), *sys.path]):
                official_stripe = importlib.import_module("stripe")

        HTTPClient = official_stripe._http_client.HTTPClient
        APIConnectionError = official_stripe._error.APIConnectionError

        class FakeHttp(HTTPClient):
            def __init__(self, responses):
                super().__init__()
                self.responses = list(responses)
                self.calls = 0

            def request(self, method, url, headers, post_data=None, **kwargs):
                del method, url, headers, post_data, kwargs
                self.calls += 1
                response = self.responses.pop(0)
                if isinstance(response, Exception):
                    raise response
                return response

        cases = (
            (("{}", 429, {"stripe-should-retry": "true"}), "429"),
            (APIConnectionError("timeout", should_retry=True), "timeout"),
            (("{}", 503, {}), "5xx"),
        )
        for first, label in cases:
            with (
                self.subTest(label=label),
                patch("stripe._http_client.time.sleep") as slept,
            ):
                client = FakeHttp([first, ("{}", 200, {})])
                response = client.request_with_retries(
                    "post", "https://api.stripe.com/v1/test", {}, max_network_retries=1
                )
                self.assertEqual(response[1], 200)
                self.assertEqual(client.calls, 2)
                slept.assert_called_once()
                self.assertLessEqual(slept.call_args.args[0], 0.5)

    def test_webhook_verifier_uses_official_raw_signature_api_and_fixed_tolerance(self):
        stripe = stripe_module(self)
        calls = []

        class Webhook:
            @staticmethod
            def construct_event(payload, sig_header, secret, tolerance):
                calls.append((payload, sig_header, secret, tolerance))
                return {"id": "evt-1"}

        with patch.dict(sys.modules, {"stripe": SimpleNamespace(Webhook=Webhook)}):
            result = stripe.StripeWebhookVerifier("whsec_syntheticsecret1234").verify(
                b'{"id":"evt-1"}', "t=1,v1=signature"
            )

        self.assertEqual(result, {"id": "evt-1"})
        self.assertEqual(
            calls,
            [
                (
                    b'{"id":"evt-1"}',
                    "t=1,v1=signature",
                    "whsec_syntheticsecret1234",
                    300,
                )
            ],
        )
        with self.assertRaises(stripe.StripeAdapterError):
            stripe.StripeWebhookVerifier("sk_test_not_a_webhook_secret")

    def test_webhook_secret_is_read_only_after_bounded_verification_input(self):
        stripe = stripe_module(self)
        verifier_type = getattr(stripe, "SecretsManagerStripeWebhookVerifier", None)
        self.assertTrue(callable(verifier_type))
        if verifier_type is None:
            return

        class Secrets:
            def __init__(self):
                self.calls = []

            def get_secret_value(self, **kwargs):
                self.calls.append(kwargs)
                return {"SecretString": "whsec_" + "syntheticsecret1234"}

        calls = []

        class Webhook:
            @staticmethod
            def construct_event(payload, sig_header, secret, tolerance):
                calls.append((payload, sig_header, secret, tolerance))
                return {"id": "evt-1"}

        secrets = Secrets()
        verifier = verifier_type(
            secrets, "/zoolanding/test/integrations/stripe/connect-webhook"
        )
        with self.assertRaises(stripe.StripeAdapterError):
            verifier.verify(b"", "t=1,v1=signature")
        self.assertEqual(secrets.calls, [])

        with patch.dict(sys.modules, {"stripe": SimpleNamespace(Webhook=Webhook)}):
            result = verifier.verify(b'{"id":"evt-1"}', "t=1,v1=signature")

        self.assertEqual(result, {"id": "evt-1"})
        self.assertEqual(
            secrets.calls,
            [
                {
                    "SecretId": "/zoolanding/test/integrations/stripe/connect-webhook"
                }
            ],
        )
        self.assertEqual(len(calls), 1)

    def test_webhook_state_refetches_event_then_every_referenced_checkout_object(self):
        stripe = stripe_module(self)

        class Client(StripeClient):
            def retrieve_event(self, **kwargs):
                self.calls.append(("event", kwargs))
                return {
                    "id": "evt-1",
                    "type": "checkout.session.completed",
                    "created": 1_799_999_995,
                    "livemode": False,
                    "account": "acct_synthetic",
                    "objectType": "checkout-session",
                    "objectId": "cs_test_synthetic01",
                }

            def retrieve_checkout_canonical(self, **kwargs):
                self.calls.append(("checkout", kwargs))
                return {
                    "sessionId": "cs_test_synthetic01",
                    "status": "complete",
                    "paymentStatus": "paid",
                    "mode": "subscription",
                    "paymentIntentId": "pi_synthetic01",
                    "subscriptionId": "sub_synthetic01",
                    "latestInvoiceId": None,
                    "mappingHint": "attempt-1",
                }

            def retrieve_payment_intent(self, **kwargs):
                self.calls.append(("payment-intent", kwargs))
                return {
                    "paymentIntentId": "pi_synthetic01",
                    "mappingHint": "attempt-1",
                }

            def retrieve_subscription_canonical(self, **kwargs):
                self.calls.append(("subscription", kwargs))
                return {
                    "subscriptionId": "sub_synthetic01",
                    "status": "active",
                    "currentPeriodEnd": 1_900_000_000,
                    "latestInvoiceId": "in_synthetic01",
                    "items": [{
                        "itemId": "si_synthetic01",
                        "priceId": "price_synthetic01",
                        "currentPeriodStart": 1_800_000_000,
                        "currentPeriodEnd": 1_900_000_000,
                    }],
                    "pauseCollection": None,
                    "pendingUpdate": None,
                    "mappingHint": "attempt-1",
                }

            def retrieve_invoice_canonical(self, **kwargs):
                self.calls.append(("invoice", kwargs))
                return {
                    "invoiceId": "in_synthetic01",
                    "status": "paid",
                    "paid": True,
                    "subscriptionId": "sub_synthetic01",
                }

        client = Client()
        state = stripe.StripeAdapter(
            client, accounts_v2_verified=False
        ).retrieve_webhook_state(connection(), "evt-1", "checkout.session.completed")

        self.assertEqual(state["objectType"], "checkout-session")
        self.assertEqual(state["mappingHint"], "attempt-1")
        self.assertEqual(
            state["accountHash"],
            __import__("hashlib").sha256(b"acct_synthetic").hexdigest(),
        )
        self.assertNotIn("acct_synthetic", repr(state))
        self.assertEqual(
            [kind for kind, _ in client.calls],
            ["event", "checkout", "payment-intent", "subscription", "invoice"],
        )
        self.assertTrue(
            all(call["stripe_account"] == "acct_synthetic" for _, call in client.calls)
        )

    def test_pending_connection_accepts_only_deauthorization_webhook_refetch(self):
        stripe = stripe_module(self)

        class Client(StripeClient):
            def retrieve_event(self, **kwargs):
                self.calls.append(("event", kwargs))
                return {
                    "id": "evt-deauthorized",
                    "type": "account.application.deauthorized",
                    "created": 1_799_999_995,
                    "livemode": False,
                    "account": "acct_synthetic",
                    "objectType": "account",
                    "objectId": "ca_syntheticclient1234",
                }

        adapter = stripe.StripeAdapter(Client(), accounts_v2_verified=False)
        pending = connection(status="pending", ready=False)
        state = adapter.retrieve_webhook_state(
            pending,
            "evt-deauthorized",
            "account.application.deauthorized",
        )

        account_hash = __import__("hashlib").sha256(b"acct_synthetic").hexdigest()
        self.assertEqual(state["objectId"], account_hash)
        self.assertEqual(state["canonical"], {"accountHash": account_hash})
        with self.assertRaises(stripe.StripeAdapterError):
            adapter.retrieve_webhook_state(
                pending,
                "evt-deauthorized",
                "checkout.session.completed",
            )

    def test_strategy_selects_oauth_or_controller_without_browser_provider_fields(self):
        stripe = stripe_module(self)
        client = StripeClient()
        adapter = stripe.StripeAdapter(client, accounts_v2_verified=False)
        callbacks = stripe.build_onboarding_callbacks(
            "example.com", binding().provider_metadata["onboardingRoutes"]
        )

        url = adapter.create_oauth_handoff(
            binding(),
            connection(status="pending", account=None, ready=False),
            callbacks=callbacks,
            state="opaque-state",
        )

        self.assertEqual(url, "https://connect.stripe.com/setup/synthetic")
        self.assertEqual(client.calls[0][0], "oauth")
        self.assertNotIn("stripe_account", client.calls[0][1])

        oauth_binding = binding()
        controller_binding = oauth_binding.__class__(
            scope=oauth_binding.scope,
            binding_id=oauth_binding.binding_id,
            provider=oauth_binding.provider,
            adapter_version=oauth_binding.adapter_version,
            connection_id=oauth_binding.connection_id,
            status=oauth_binding.status,
            mode=oauth_binding.mode,
            capabilities=oauth_binding.capabilities,
            provider_metadata={
                **dict(oauth_binding.provider_metadata),
                "accountStrategy": "controller-account-link-v1",
            },
        )
        account = adapter.create_controller_account(
            controller_binding,
            connection(status="pending", account=None, ready=False),
            idempotency_key="opaque-state",
        )
        self.assertEqual(account, "acct_synthetic")
        self.assertEqual(client.calls[-1][0], "controller-create")

    def test_accounts_v2_requires_an_exact_verified_flag_otherwise_v1_remains_selected(
        self,
    ):
        stripe = stripe_module(self)
        with self.assertRaises(stripe.StripeAdapterError):
            stripe.StripeAdapter(StripeClient(), accounts_v2_verified=True)

        with self.assertRaises(stripe.StripeAdapterError):
            stripe.StripeAdapter(StripeClient(), accounts_v2_verified="true")

    def test_callbacks_and_provider_urls_are_https_same_origin_and_public(self):
        stripe = stripe_module(self)
        callbacks = stripe.build_onboarding_callbacks(
            "example.com", binding().provider_metadata["onboardingRoutes"]
        )
        self.assertEqual(
            callbacks.refresh_url,
            "https://example.com/admin/integrations/stripe/refresh",
        )
        self.assertEqual(
            callbacks.return_url,
            "https://example.com/admin/integrations/stripe/return",
        )
        for unsafe_url in (
            "http://connect.stripe.com/setup/synthetic",
            "https://127.0.0.1/setup/synthetic",
            "https://evil.example/setup/synthetic",
            "https://user@connect.stripe.com/setup/synthetic",
            "https://connect.stripe.com:invalid/setup/synthetic",
        ):
            with (
                self.subTest(url=unsafe_url),
                self.assertRaises(stripe.StripeAdapterError),
            ):
                stripe.StripeAdapter(
                    StripeClient(unsafe_url), accounts_v2_verified=False
                ).create_oauth_handoff(
                    binding(),
                    connection(status="pending", account=None, ready=False),
                    callbacks=callbacks,
                    state="opaque-state",
                )

    def test_canonical_status_recheck_is_sanitized_and_contains_no_account_id(self):
        stripe = stripe_module(self)
        client = StripeClient()
        result = stripe.StripeAdapter(
            client, accounts_v2_verified=False
        ).retrieve_canonical_status(binding(), connection())

        self.assertEqual(
            result,
            {
                "status": "ready",
                "chargesEnabled": True,
                "payoutsEnabled": True,
                "detailsSubmitted": True,
                "capabilitiesReady": True,
                "requirementsDueCount": 0,
            },
        )
        self.assertNotIn("acct_synthetic", str(result))
        self.assertEqual(client.calls[0][0], "retrieve-v1")

    def test_commerce_adapter_validates_context_and_delegates_only_safe_snapshots(self):
        stripe = stripe_module(self)

        class CommerceClient(StripeClient):
            def create_product(self, **kwargs):
                self.calls.append(("product", kwargs))
                return {"id": "prod_synthetic01"}

            def create_price(self, **kwargs):
                self.calls.append(("price", kwargs))
                return {"id": "price_synthetic01"}

            def create_checkout(self, **kwargs):
                self.calls.append(("checkout", kwargs))
                return {
                    "sessionId": "cs_test_synthetic01",
                    "redirectUrl": "https://checkout.stripe.com/c/pay/synthetic",
                    "expiresAt": 1_800_002_100,
                }

        client = CommerceClient()
        adapter = stripe.StripeAdapter(client, accounts_v2_verified=False)
        context = resolved()
        product_id = adapter.create_product(context, "offer-v1", "idem-1")
        price_id = adapter.create_price(
            context,
            product_id,
            {
                "schemaVersion": 1,
                "amountMinor": 90000,
                "billingScheme": "per_unit",
                "currency": "MXN",
                "saleType": "recurring",
                "recurrence": {
                    "interval": "month",
                    "intervalCount": 1,
                    "usageType": "licensed",
                },
                "taxBehavior": "exclusive",
            },
            "offer-v1",
            "idem-2",
        )

        self.assertEqual(product_id, "prod_synthetic01")
        self.assertEqual(price_id, "price_synthetic01")
        self.assertEqual(client.calls[0][1]["stripe_account"], "acct_synthetic")
        self.assertNotIn("email", repr(client.calls).lower())
        checkout_input = checkout_command(False)["input"]
        adapter.create_checkout(
            context,
            [{"price": price_id, "quantity": 1}],
            None,
            checkout_input,
            {
                "successUrl": "https://test.zoolandingpage.com.mx/success?draftDomain=example.com",
                "cancelUrl": "https://test.zoolandingpage.com.mx/cancel?draftDomain=example.com",
            },
            "idem-3",
        )
        checkout_params = client.calls[-1][1]["params"]
        self.assertEqual(checkout_params["payment_method_types"], ["card", "link"])
        self.assertEqual(checkout_params["mode"], "subscription")

        mixed_input = checkout_with_add_on()["input"]
        mixed = adapter.create_checkout(
            context,
            [
                {"price": "price_addon_synthetic01", "quantity": 1},
                {"price": price_id, "quantity": 1},
            ],
            None,
            mixed_input,
            {
                "successUrl": "https://test.zoolandingpage.com.mx/success?draftDomain=example.com",
                "cancelUrl": "https://test.zoolandingpage.com.mx/cancel?draftDomain=example.com",
            },
            "idem-4",
        )
        self.assertEqual(mixed["sessionId"], "cs_test_synthetic01")
        self.assertEqual(client.calls[-1][1]["params"]["mode"], "subscription")

    def test_sdk_client_uses_pinned_v1_resource_shapes(self):
        stripe = stripe_module(self)

        class Resource:
            def __init__(self, response):
                self.response = response
                self.calls = []

            def create(self, *args):
                self.calls.append(("create", args))
                return self.response

            def __call__(self, *args):
                self.calls.append(("call", args))
                return self.response

            def update(self, *args):
                self.calls.append(("update", args))
                return self.response

            def retrieve(self, *args):
                self.calls.append(("retrieve", args))
                return self.response

            def delete(self, *args):
                self.calls.append(("delete", args))
                return self.response

            def create_preview(self, *args):
                self.calls.append(("create_preview", args))
                return self.response

        class V1:
            accounts = Resource({"id": "acct_synthetic"})
            account_links = Resource(
                {"url": "https://connect.stripe.com/setup/synthetic"}
            )
            products = Resource({"id": "prod_synthetic01"})
            prices = Resource({"id": "price_synthetic01"})
            coupons = Resource({"id": "couponSynthetic01"})
            promotion_codes = Resource({"id": "promo_synthetic01"})
            events = Resource(
                {
                    "id": "evt_synthetic01",
                    "type": "checkout.session.completed",
                    "created": 1_799_999_995,
                    "livemode": False,
                    "account": "acct_synthetic",
                    "data": {"object": {"id": "cs_test_synthetic01"}},
                }
            )
            payment_intents = Resource(
                {
                    "id": "pi_synthetic01",
                    "metadata": {"payment_attempt_id": "attempt-1"},
                }
            )
            refunds = Resource(
                {
                    "id": "re_synthetic01",
                    "status": "succeeded",
                    "amount": 90000,
                    "currency": "mxn",
                    "payment_intent": "pi_synthetic01",
                    "charge": "ch_synthetic01",
                }
            )
            charges = Resource({"id": "ch_synthetic01"})
            subscriptions = Resource(
                {
                    "id": "sub_synthetic01",
                    "customer": "cus_synthetic01",
                    "status": "active",
                    "items": {
                        "data": [
                            {
                                "id": "si_synthetic01",
                                "price": "price_synthetic01",
                                "quantity": 2,
                                "current_period_start": 1_800_000_000,
                                "current_period_end": 1_900_000_000,
                                "tax_rates": [],
                            }
                        ]
                    },
                    "schedule": None,
                    "discounts": [],
                    "pause_collection": None,
                    "latest_invoice": {
                        "id": "in_synthetic01",
                        "status": "paid",
                        "payments": {"data": [{"is_default": True, "status": "paid"}]},
                    },
                    "pending_update": None,
                    "automatic_tax": {"enabled": True},
                    "default_tax_rates": [],
                }
            )
            invoices = Resource({"id": "in_synthetic01"})
            subscription_schedules = Resource(
                {
                    "id": "sub_sched_synthetic01",
                    "current_phase": {
                        "start_date": 1_800_000_000,
                        "end_date": 1_900_000_000,
                    },
                }
            )

            class BillingPortal:
                configurations = Resource({"id": "bpc_synthetic01"})
                sessions = Resource(
                    {
                        "url": "https://billing.stripe.com/p/session/synthetic",
                        "created": 1_800_000_000,
                    }
                )

            billing_portal = BillingPortal()

            class Checkout:
                sessions = Resource(
                    {
                        "id": "cs_test_synthetic01",
                        "url": "https://checkout.stripe.com/c/pay/synthetic",
                        "expires_at": 1_800_002_100,
                        "payment_status": "unpaid",
                        "status": "open",
                        "mode": "payment",
                        "payment_intent": "pi_synthetic01",
                        "subscription": None,
                        "metadata": {"payment_attempt_id": "attempt-1"},
                    }
                )

            checkout = Checkout()

        class OfficialClient:
            v1 = V1()
            client_id = "ca_syntheticclient1234"
            oauth = SimpleNamespace(
                token=Resource({"stripe_user_id": "acct_synthetic"}),
                deauthorize=Resource({"stripe_user_id": "acct_synthetic"}),
            )

        sdk = stripe.StripeSdkClient(OfficialClient())
        options = {"stripe_account": "acct_synthetic", "idempotency_key": "idem"}
        self.assertEqual(
            sdk.create_product(
                stripe_account="acct_synthetic",
                resource_id="offer-v1",
                idempotency_key="idem",
            ),
            {"id": "prod_synthetic01"},
        )
        self.assertEqual(
            OfficialClient.v1.products.calls[-1],
            (
                "create",
                (
                    {"name": "Item", "metadata": {"resource_id": "offer-v1"}},
                    options,
                ),
            ),
        )
        checkout = sdk.create_checkout(
            stripe_account="acct_synthetic",
            params={"mode": "subscription", "line_items": []},
            idempotency_key="idem",
        )
        self.assertEqual(checkout["sessionId"], "cs_test_synthetic01")
        self.assertEqual(OfficialClient.v1.checkout.sessions.calls[-1][1][1], options)
        sdk.deactivate_offer(
            stripe_account="acct_synthetic",
            product_id="prod_synthetic01",
            price_id="price_synthetic01",
            idempotency_key="idem",
        )
        price_options = OfficialClient.v1.prices.calls[-1][1][-1]
        product_options = OfficialClient.v1.products.calls[-1][1][-1]
        self.assertNotEqual(
            price_options["idempotency_key"], product_options["idempotency_key"]
        )
        event = sdk.retrieve_event(
            stripe_account="acct_synthetic", event_id="evt_synthetic01"
        )
        self.assertEqual(event["objectType"], "checkout-session")
        self.assertEqual(event["objectId"], "cs_test_synthetic01")
        canonical = sdk.retrieve_checkout_canonical(
            stripe_account="acct_synthetic", session_id="cs_test_synthetic01"
        )
        self.assertEqual(canonical["paymentIntentId"], "pi_synthetic01")
        self.assertEqual(canonical["mappingHint"], "attempt-1")
        self.assertEqual(
            sdk.retrieve_payment_intent(
                stripe_account="acct_synthetic",
                payment_intent_id="pi_synthetic01",
            ),
            {"paymentIntentId": "pi_synthetic01", "mappingHint": "attempt-1"},
        )
        self.assertEqual(
            sdk.retrieve_refund_canonical(
                stripe_account="acct_synthetic", refund_id="re_synthetic01"
            ),
            {
                "refundId": "re_synthetic01",
                "status": "succeeded",
                "amountMinor": 90000,
                "currency": "MXN",
                "paymentIntentId": "pi_synthetic01",
                "chargeId": None,
            },
        )
        self.assertEqual(
            sdk.update_discount_presentation(
                stripe_account="acct_synthetic",
                coupon_id="couponSynthetic01",
                snapshot={
                    "displayName": "Summer promotion",
                    "displayDescription": "Server-only copy",
                },
                idempotency_key="idem",
            ),
            None,
        )
        coupon_update = OfficialClient.v1.coupons.calls[-1]
        self.assertEqual(coupon_update[0], "update")
        self.assertEqual(coupon_update[1][1], {"name": "Summer promotion"})
        self.assertNotIn("displayDescription", repr(coupon_update))

        oauth_url = sdk.create_oauth_handoff(
            redirect_uri="https://example.com/admin/integrations/stripe/return",
            state="opaque-state",
        )["url"]
        self.assertIn("client_id=ca_syntheticclient1234", oauth_url)
        self.assertIn("state=opaque-state", oauth_url)
        self.assertEqual(
            sdk.exchange_oauth_code(
                code="code_synthetic",
                redirect_uri="https://example.com/admin/integrations/stripe/return",
            ),
            {"accountReference": "acct_synthetic"},
        )
        self.assertEqual(
            OfficialClient.oauth.token.calls[-1][1][0],
            {"grant_type": "authorization_code", "code": "code_synthetic"},
        )
        self.assertEqual(
            sdk.create_controller_account(idempotency_key="idem"),
            {"id": "acct_synthetic"},
        )
        controller_params = OfficialClient.v1.accounts.calls[-1][1][0]
        self.assertEqual(
            controller_params["controller"],
            {
                "fees": {"payer": "account"},
                "losses": {"payments": "stripe"},
                "requirement_collection": "stripe",
                "stripe_dashboard": {"type": "full"},
            },
        )
        self.assertEqual(
            controller_params["capabilities"],
            {
                "card_payments": {"requested": True},
                "transfers": {"requested": True},
            },
        )
        self.assertIsNone(
            sdk.deauthorize_oauth_account(stripe_account="acct_synthetic")
        )
        state = sdk.retrieve_subscription_operation_state(
            stripe_account="acct_synthetic",
            subscription_id="sub_synthetic01",
        )
        self.assertEqual(state["items"][0]["quantity"], 2)
        self.assertEqual(state["latestInvoice"]["paymentStatus"], "paid")
        self.assertEqual(state["automaticTax"], {"enabled": True})
        shared = {
            "stripe_account": "acct_synthetic",
            "subscription_id": "sub_synthetic01",
            "item_id": "si_synthetic01",
            "price_id": "price_synthetic02",
            "quantity": 2,
            "preview_timestamp": 1_800_000_100,
            "idempotency_key": "idem",
        }
        self.assertEqual(
            sdk.preview_subscription_change(**shared),
            {"previewTimestamp": 1_800_000_100},
        )
        sdk.apply_subscription_change(**shared)
        preview_params = OfficialClient.v1.invoices.calls[-1][1][0]
        apply_params = OfficialClient.v1.subscriptions.calls[-1][1][1]
        self.assertEqual(
            preview_params["subscription_details"]["items"][0],
            apply_params["items"][0],
        )
        self.assertEqual(
            preview_params["subscription_details"]["proration_date"],
            apply_params["proration_date"],
        )
        self.assertEqual(
            preview_params["subscription_details"]["proration_behavior"],
            "always_invoice",
        )
        self.assertEqual(apply_params["proration_behavior"], "always_invoice")
        self.assertEqual(apply_params["payment_behavior"], "pending_if_incomplete")
        configuration = sdk.create_portal_configuration(
            stripe_account="acct_synthetic", idempotency_key="idem"
        )
        self.assertEqual(configuration, {"id": "bpc_synthetic01"})
        features = OfficialClient.v1.billing_portal.configurations.calls[-1][1][0][
            "features"
        ]
        self.assertEqual(set(features), {"invoice_history", "payment_method_update"})
        portal = sdk.create_portal_session(
            stripe_account="acct_synthetic",
            customer_id="cus_synthetic01",
            configuration_id="bpc_synthetic01",
            return_url="https://example.com/admin/billing",
            idempotency_key="idem",
        )
        self.assertEqual(portal["expiresAt"], 1_800_001_800)
        portal_params = OfficialClient.v1.billing_portal.sessions.calls[-1][1][0]
        self.assertEqual(
            portal_params["return_url"], "https://example.com/admin/billing"
        )


if __name__ == "__main__":
    unittest.main()
