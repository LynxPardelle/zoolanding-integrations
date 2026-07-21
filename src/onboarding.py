"""Orchestrate provider-hosted onboarding with one-use state."""

from __future__ import annotations

from typing import Any

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
        url = self._adapter.create_onboarding_handoff(
            resolved.binding,
            resolved.connection,
            callbacks=build_onboarding_callbacks(context.domain),
            state=state,
        )
        return {"handoffUrl": url}

    def complete_return(
        self,
        resolved: Any,
        context: Any,
        state: str,
        now_epoch: int,
    ) -> dict[str, Any]:
        self._state.consume(
            state,
            resolved.connection.scope,
            resolved.connection.connection_id,
            session_hash=context.session_hash,
            now_epoch=now_epoch,
        )
        status = self._adapter.retrieve_canonical_status(
            resolved.binding, resolved.connection
        )
        if status.get("status") == "ready":
            self._registry.activate_ready(
                resolved.connection.scope,
                resolved.connection.connection_id,
                status,
                resolved.connection.revision,
            )
        return status
