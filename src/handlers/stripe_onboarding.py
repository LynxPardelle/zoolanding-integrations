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
        if type(operation) is not str or operation not in {
            "start",
            "return",
            "deauthorize",
        }:
            raise validation_error()
        if operation in {"start", "deauthorize"}:
            input_value = closed_object(request["input"], {"bindingId"})
        else:
            candidate = request["input"]
            if not isinstance(candidate, dict) or set(candidate) not in (
                {"bindingId", "state"},
                {"bindingId", "state", "code"},
                {"bindingId", "state", "error"},
            ):
                raise validation_error()
            input_value = dict(candidate)
        binding_id = safe_id(input_value["bindingId"])
        if operation == "return" and (
            type(input_value["state"]) is not str
            or not 1 <= len(input_value["state"]) <= 1024
        ):
            raise validation_error()
        if (
            operation == "return"
            and "code" in input_value
            and (
                type(input_value["code"]) is not str
                or not 1 <= len(input_value["code"]) <= 1024
                or any(ord(character) < 33 for character in input_value["code"])
            )
        ):
            raise validation_error()
        if (
            operation == "return"
            and "error" in input_value
            and input_value["error"]
            not in {
                "access_denied",
                "invalid_scope",
                "server_error",
                "temporarily_unavailable",
            }
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
        if operation == "deauthorize":
            return onboarding_service.deauthorize(resolved, context, now_epoch)
        return onboarding_service.complete_return(
            resolved,
            context,
            input_value["state"],
            now_epoch,
            **{
                key: input_value[key] for key in ("code", "error") if key in input_value
            },
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
