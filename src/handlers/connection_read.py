"""Authenticated sanitized Integrations connection reads."""

from __future__ import annotations

from typing import Any

try:
    from common.auth_admin import authorize_request
    from common.http import (
        closed_object,
        dispatch,
        domain_header,
        unavailable_response,
        validation_error,
    )
except ModuleNotFoundError:
    from src.common.auth_admin import authorize_request
    from src.common.http import (
        closed_object,
        dispatch,
        domain_header,
        unavailable_response,
        validation_error,
    )


PATH = "/features/integrations/read"


def handle_request(
    event: dict[str, Any],
    *,
    policy_resolver: Any,
    auth_store: Any,
    registry: Any,
    environment: str,
    now_epoch: int,
) -> dict[str, Any]:
    def handle(payload: dict[str, Any]) -> dict[str, Any]:
        request = closed_object(payload, {"operation"})
        if request["operation"] != "list":
            raise validation_error()
        policies = policy_resolver.resolve(
            environment=environment,
            domain=domain_header(event),
        )
        authorize_request(
            event=event,
            policies=policies,
            capability="integration:read",
            store=auth_store,
            now_epoch=now_epoch,
        )
        return {
            "connections": [
                _sanitized(connection)
                for connection in registry.list_connections(policies.scope)
            ]
        }

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
        from runtime import browser_runtime
    except ModuleNotFoundError:
        from src.runtime import browser_runtime
    return browser_runtime()


def _sanitized(connection: Any) -> dict[str, Any]:
    return {
        "connectionId": connection.connection_id,
        "provider": connection.provider,
        "status": connection.status,
        "mode": connection.mode,
        "capabilities": sorted(connection.capabilities),
        "revision": connection.revision,
    }
