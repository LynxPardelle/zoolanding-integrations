import importlib
import importlib.util
import unittest

from tests.test_registry import binding, connection
from tests.test_stripe_commands import checkout_command, resolved


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

            class Checkout:
                sessions = Resource(
                    {
                        "id": "cs_test_synthetic01",
                        "url": "https://checkout.stripe.com/c/pay/synthetic",
                        "expires_at": 1_800_002_100,
                        "payment_status": "unpaid",
                        "status": "open",
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
        self.assertEqual(
            OfficialClient.v1.checkout.sessions.calls[-1][1][1], options
        )
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


if __name__ == "__main__":
    unittest.main()
