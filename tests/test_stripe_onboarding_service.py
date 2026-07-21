import unittest
from types import SimpleNamespace

from src.onboarding import StripeOnboardingService
from src.registry import ResolvedBinding
from tests.test_registry import binding, connection, scope


class State:
    def __init__(self):
        self.calls = []

    def issue(self, selected_scope, connection_id, **kwargs):
        self.calls.append(("issue", selected_scope, connection_id, kwargs))
        return "opaque-state"

    def consume(self, token, selected_scope, connection_id, **kwargs):
        self.calls.append(("consume", token, selected_scope, connection_id, kwargs))


class Registry:
    def __init__(self):
        self.calls = []

    def bind_stripe_account(
        self, selected_scope, connection_id, account, ownership, expected_revision
    ):
        self.calls.append(
            (
                "bind",
                selected_scope,
                connection_id,
                account,
                ownership,
                expected_revision,
            )
        )
        return connection(
            selected_scope,
            status="pending",
            account=account,
            ready=False,
        ).__class__(
            scope=selected_scope,
            connection_id=connection_id,
            provider="stripe",
            adapter_version="v1",
            status="pending",
            mode="test",
            capabilities=frozenset({"connect-onboarding", "checkout"}),
            provider_metadata={
                "accountReference": account,
                "accountOwnership": ownership,
            },
            revision=expected_revision + 1,
        )

    def activate_ready(self, *args):
        self.calls.append(("activate", *args))

    def disable_stripe_account(self, *args):
        self.calls.append(("disable", *args))
        return connection(status="disabled", ready=False)


class Adapter:
    def __init__(self):
        self.calls = []

    def create_oauth_handoff(self, *args, **kwargs):
        self.calls.append(("oauth-start", args, kwargs))
        return "https://connect.stripe.com/oauth/authorize?state=opaque-state"

    def exchange_oauth_code(self, *args, **kwargs):
        self.calls.append(("oauth-exchange", args, kwargs))
        return "acct_synthetic"

    def create_controller_account(self, *args, **kwargs):
        self.calls.append(("controller-create", args, kwargs))
        return "acct_synthetic"

    def create_account_link(self, *args, **kwargs):
        self.calls.append(("account-link", args, kwargs))
        return "https://connect.stripe.com/setup/synthetic"

    def retrieve_canonical_status(self, *args):
        self.calls.append(("status", args, {}))
        return {
            "status": "ready",
            "chargesEnabled": True,
            "payoutsEnabled": True,
            "detailsSubmitted": True,
            "capabilitiesReady": True,
            "requirementsDueCount": 0,
        }

    def deauthorize_oauth_account(self, *args):
        self.calls.append(("deauthorize", args, {}))


def context():
    return SimpleNamespace(session_hash="a" * 64, domain="example.com")


def pending(strategy="oauth-standard-v1"):
    selected_binding = binding()
    descriptor = selected_binding.to_record()["providerMetadata"]
    descriptor["accountStrategy"] = strategy
    selected_binding = selected_binding.__class__(
        scope=selected_binding.scope,
        binding_id=selected_binding.binding_id,
        provider="stripe",
        adapter_version="v1",
        connection_id="stripe-primary",
        status="active",
        mode="test",
        capabilities=selected_binding.capabilities,
        provider_metadata=descriptor,
    )
    return ResolvedBinding(
        selected_binding,
        connection(status="pending", account=None, ready=False),
    )


class StripeOnboardingServiceTests(unittest.TestCase):
    def test_oauth_start_and_return_consume_state_bind_account_then_recheck(self):
        state = State()
        adapter = Adapter()
        registry = Registry()
        service = StripeOnboardingService(state, adapter, registry)
        resolved = pending()

        started = service.start(resolved, context(), 1_000)
        returned = service.complete_return(
            resolved, context(), "opaque-state", 1_001, code="code_synthetic"
        )

        self.assertIn("connect.stripe.com", started["handoffUrl"])
        self.assertEqual(returned["status"], "ready")
        self.assertEqual(
            [call[0] for call in adapter.calls],
            ["oauth-start", "oauth-exchange", "status"],
        )
        self.assertEqual(registry.calls[0][3:5], ("acct_synthetic", "external-oauth"))
        self.assertEqual(registry.calls[1][0], "activate")
        self.assertEqual(registry.calls[1][-1], 2)

    def test_controller_start_creates_and_claims_account_before_one_use_link(self):
        state = State()
        adapter = Adapter()
        registry = Registry()
        service = StripeOnboardingService(state, adapter, registry)

        result = service.start(pending("controller-account-link-v1"), context(), 1_000)

        self.assertIn("connect.stripe.com", result["handoffUrl"])
        self.assertEqual(
            [call[0] for call in adapter.calls],
            ["controller-create", "account-link"],
        )
        self.assertEqual(registry.calls[0][4], "platform-controller")

    def test_controller_retry_reuses_already_claimed_account_and_only_renews_link(self):
        state = State()
        adapter = Adapter()
        registry = Registry()
        service = StripeOnboardingService(state, adapter, registry)
        base = pending("controller-account-link-v1")
        claimed = base.connection.__class__(
            scope=base.connection.scope,
            connection_id=base.connection.connection_id,
            provider="stripe",
            adapter_version="v1",
            status="pending",
            mode="test",
            capabilities=base.connection.capabilities,
            provider_metadata={
                "accountReference": "acct_synthetic",
                "accountOwnership": "platform-controller",
            },
            revision=2,
        )

        service.start(ResolvedBinding(base.binding, claimed), context(), 1_002)

        self.assertEqual([call[0] for call in adapter.calls], ["account-link"])
        self.assertEqual(registry.calls, [])

    def test_oauth_error_consumes_state_without_exchanging_or_leaking_provider_error(
        self,
    ):
        state = State()
        adapter = Adapter()
        service = StripeOnboardingService(state, adapter, Registry())

        result = service.complete_return(
            pending(), context(), "opaque-state", 1_001, error="access_denied"
        )

        self.assertEqual(result, {"status": "pending"})
        self.assertEqual(adapter.calls, [])
        self.assertEqual(state.calls[0][0], "consume")

    def test_deauthorize_calls_provider_only_for_external_oauth_then_disables_claim(
        self,
    ):
        for strategy, expected_provider_calls in (
            ("oauth-standard-v1", ["deauthorize"]),
            ("controller-account-link-v1", []),
        ):
            with self.subTest(strategy=strategy):
                adapter = Adapter()
                registry = Registry()
                service = StripeOnboardingService(State(), adapter, registry)
                active = ResolvedBinding(
                    pending(strategy).binding,
                    connection(ready=True),
                )
                result = service.deauthorize(active, context(), 1_005)
                self.assertEqual(result, {"status": "disabled"})
                self.assertEqual(
                    [call[0] for call in adapter.calls], expected_provider_calls
                )
                self.assertEqual(registry.calls[-1][0], "disable")


if __name__ == "__main__":
    unittest.main()
