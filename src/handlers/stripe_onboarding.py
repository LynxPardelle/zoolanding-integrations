"""Stripe-specific onboarding route isolated from generic connection handlers."""

from __future__ import annotations

from typing import Any

try:
    from common.auth_admin import authorize_request
    from common.http import (
        closed_object,
        dispatch,
        domain_header,
        safe_id,
        unavailable_response,
        validation_error,
    )
except ModuleNotFoundError:
    from src.common.auth_admin import authorize_request
    from src.common.http import (
        closed_object,
        dispatch,
        domain_header,
        safe_id,
        unavailable_response,
        validation_error,
    )


PATH = "/features/integrations/stripe/onboarding"


def handle_request(
    event: dict[str, Any],
    *,
    policy_resolver: Any,
    auth_store: Any,
    binding_resolver: Any,
    onboarding_service: Any,
    environment: str,
    now_epoch: int,
) -> dict[str, Any]:
    def handle(payload: dict[str, Any]) -> dict[str, Any]:
        request = closed_object(payload, {"operation", "input"})
        operation = request["operation"]
        if type(operation) is not str or operation not in {"start", "return"}:
            raise validation_error()
        required = {"bindingId"} if operation == "start" else {"bindingId", "state"}
        input_value = closed_object(request["input"], required)
        binding_id = safe_id(input_value["bindingId"])
        if operation == "return" and (
            type(input_value["state"]) is not str
            or not 1 <= len(input_value["state"]) <= 1024
        ):
            raise validation_error()
        policies = policy_resolver.resolve(
            environment=environment,
            domain=domain_header(event),
        )
        context = authorize_request(
            event=event,
            policies=policies,
            capability="integration:manage",
            mutation=True,
            store=auth_store,
            now_epoch=now_epoch,
        )
        resolved = binding_resolver.resolve(
            policies.scope,
            binding_id,
            provider="stripe",
            capability="connect-onboarding",
        )
        if operation == "start":
            return onboarding_service.start(resolved, context, now_epoch)
        return onboarding_service.complete_return(
            resolved,
            context,
            input_value["state"],
            now_epoch,
        )

    return dispatch(event, PATH, handle)


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    del context
    try:
        dependencies = _runtime_dependencies()
    except Exception:
        return unavailable_response(event)
    return handle_request(event, **dependencies)


def _runtime_dependencies() -> dict[str, Any]:
    try:
        from runtime import stripe_onboarding_runtime
    except ModuleNotFoundError:
        from src.runtime import stripe_onboarding_runtime
    return stripe_onboarding_runtime()
