import importlib
import importlib.util
import unittest

from tests.test_registry import binding, connection


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
            "details_submitted": True,
            "requirements": {"currently_due": ["business_profile.url"]},
        }

    def retrieve_v2_account(self, **kwargs):
        self.calls.append(("retrieve-v2", kwargs))
        return {
            "id": "acct_synthetic",
            "charges_enabled": False,
            "details_submitted": True,
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
                "detailsSubmitted": True,
                "requirementsDueCount": 1,
            },
        )
        self.assertNotIn("acct_synthetic", str(result))
        self.assertEqual(client.calls[0][0], "retrieve-v1")


if __name__ == "__main__":
    unittest.main()
