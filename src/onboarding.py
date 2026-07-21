"""Orchestrate provider-hosted onboarding with one-use state."""

from __future__ import annotations

from typing import Any

try:
    from providers.stripe_adapter import build_onboarding_callbacks
except ModuleNotFoundError:
    from .providers.stripe_adapter import build_onboarding_callbacks


class StripeOnboardingService:
    def __init__(self, state_manager: Any, stripe_adapter: Any, registry: Any):
        self._state = state_manager
        self._adapter = stripe_adapter
        self._registry = registry

    def start(self, resolved: Any, context: Any, now_epoch: int) -> dict[str, str]:
        state = self._state.issue(
            resolved.connection.scope,
            resolved.connection.connection_id,
            session_hash=context.session_hash,
            now_epoch=now_epoch,
        )
        callbacks = build_onboarding_callbacks(
            context.domain,
            resolved.binding.provider_metadata["onboardingRoutes"],
        )
        strategy = resolved.binding.provider_metadata["accountStrategy"]
        if strategy == "oauth-standard-v1":
            url = self._adapter.create_oauth_handoff(
                resolved.binding,
                resolved.connection,
                callbacks=callbacks,
                state=state,
            )
        elif strategy == "controller-account-link-v1":
            connected = resolved.connection
            if connected.provider_metadata.get("accountReference") is None:
                account = self._adapter.create_controller_account(
                    resolved.binding,
                    resolved.connection,
                    idempotency_key=(
                        "controller-account-v1:"
                        + resolved.connection.scope.partition_key
                        + ":"
                        + resolved.connection.connection_id
                    ),
                )
                connected = self._registry.bind_stripe_account(
                    resolved.connection.scope,
                    resolved.connection.connection_id,
                    account,
                    "platform-controller",
                    resolved.connection.revision,
                )
            elif (
                connected.provider_metadata.get("accountOwnership")
                != "platform-controller"
            ):
                raise RuntimeError("Stripe onboarding is unavailable")
            url = self._adapter.create_account_link(
                resolved.binding,
                connected,
                callbacks=callbacks,
                state=state,
                idempotency_key=state,
            )
        else:
            raise RuntimeError("Stripe onboarding is unavailable")
        return {"handoffUrl": url}

    def complete_return(
        self,
        resolved: Any,
        context: Any,
        state: str,
        now_epoch: int,
        *,
        code: str | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        self._state.consume(
            state,
            resolved.connection.scope,
            resolved.connection.connection_id,
            session_hash=context.session_hash,
            now_epoch=now_epoch,
        )
        strategy = resolved.binding.provider_metadata["accountStrategy"]
        connected = resolved.connection
        if strategy == "oauth-standard-v1":
            if error is not None:
                return {"status": "pending"}
            account = self._adapter.exchange_oauth_code(
                resolved.binding,
                resolved.connection,
                code=code,
                redirect_uri=build_onboarding_callbacks(
                    context.domain,
                    resolved.binding.provider_metadata["onboardingRoutes"],
                ).return_url,
            )
            connected = self._registry.bind_stripe_account(
                resolved.connection.scope,
                resolved.connection.connection_id,
                account,
                "external-oauth",
                resolved.connection.revision,
            )
        elif (
            strategy != "controller-account-link-v1"
            or code is not None
            or error is not None
        ):
            raise RuntimeError("Stripe onboarding is unavailable")
        status = self._adapter.retrieve_canonical_status(resolved.binding, connected)
        if status.get("status") == "ready":
            self._registry.activate_ready(
                resolved.connection.scope,
                resolved.connection.connection_id,
                status,
                connected.revision,
            )
        return status

    def deauthorize(
        self, resolved: Any, context: Any, now_epoch: int
    ) -> dict[str, str]:
        del context, now_epoch
        account = resolved.connection.provider_metadata.get("accountReference")
        strategy = resolved.binding.provider_metadata["accountStrategy"]
        if strategy == "oauth-standard-v1":
            self._adapter.deauthorize_oauth_account(
                resolved.binding, resolved.connection
            )
        elif strategy != "controller-account-link-v1":
            raise RuntimeError("Stripe deauthorization is unavailable")
        disabled = self._registry.disable_stripe_account(
            resolved.connection.scope,
            resolved.connection.connection_id,
            account,
            resolved.connection.revision,
        )
        return {"status": disabled.status}
