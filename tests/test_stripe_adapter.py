import importlib
import importlib.util
import sys
import unittest
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
                    "priceId": "price_synthetic01",
                    "pauseCollection": None,
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

    def test_accounts_v1_is_the_fallback_and_direct_account_context_is_mandatory(self):
        stripe = stripe_module(self)
        client = StripeClient()
        adapter = stripe.StripeAdapter(client, accounts_v2_verified=False)
        callbacks = stripe.build_onboarding_callbacks("example.com")

        url = adapter.create_onboarding_handoff(
            binding(),
            connection(),
            callbacks=callbacks,
            state="opaque-state",
        )

        self.assertEqual(url, "https://connect.stripe.com/setup/synthetic")
        self.assertEqual(client.calls[0][0], "v1")
        self.assertEqual(client.calls[0][1]["stripe_account"], "acct_synthetic")
        self.assertEqual(client.calls[0][1]["charge_type"], "direct")

    def test_accounts_v2_requires_an_exact_verified_flag_otherwise_v1_remains_selected(
        self,
    ):
        stripe = stripe_module(self)
        verified_client = StripeClient()
        stripe.StripeAdapter(
            verified_client, accounts_v2_verified=True
        ).create_onboarding_handoff(
            binding(),
            connection(),
            callbacks=stripe.build_onboarding_callbacks("example.com"),
            state="opaque-state",
        )
        self.assertEqual(verified_client.calls[0][0], "v2")

        with self.assertRaises(stripe.StripeAdapterError):
            stripe.StripeAdapter(StripeClient(), accounts_v2_verified="true")

    def test_callbacks_and_provider_urls_are_https_same_origin_and_public(self):
        stripe = stripe_module(self)
        callbacks = stripe.build_onboarding_callbacks("example.com")
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
                ).create_onboarding_handoff(
                    binding(),
                    connection(),
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

            def update(self, *args):
                self.calls.append(("update", args))
                return self.response

            def retrieve(self, *args):
                self.calls.append(("retrieve", args))
                return self.response

            def delete(self, *args):
                self.calls.append(("delete", args))
                return self.response

        class V1:
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


if __name__ == "__main__":
    unittest.main()
