"""Shared strict transport for small AWS_IAM command Lambda entrypoints."""

from __future__ import annotations

import os
from typing import Any

try:
    from common.http import HttpError, dispatch, validation_error
    from contracts.internal import (
        ContractError,
        validate_command,
        validate_service_result,
    )
except ModuleNotFoundError:
    from src.common.http import HttpError, dispatch, validation_error
    from src.contracts.internal import (
        ContractError,
        validate_command,
        validate_service_result,
    )


class UnavailableCommandService:
    def execute(self, kind: str, command: Any) -> dict[str, str]:
        del kind, command
        raise RuntimeError("command service unavailable")


def handle_internal_command(
    event: dict[str, Any],
    *,
    path: str,
    kind: str,
    service: Any,
    allowed_callers: set[str],
    method: str = "POST",
) -> dict[str, Any]:
    def handle(payload: dict[str, Any]) -> dict[str, str]:
        _require_caller(event, allowed_callers)
        try:
            command = validate_command(kind, payload)
        except ContractError:
            raise validation_error() from None
        try:
            result = service.execute(kind, command)
            return validate_service_result(result, command)
        except ContractError:
            raise RuntimeError("invalid service response") from None

    return dispatch(event, path, handle, method)


def configured_callers() -> set[str]:
    values = {
        value.strip()
        for value in os.getenv("INTERNAL_CALLER_ARNS", "").split(",")
        if value.strip()
    }
    if any("*" in value for value in values):
        return set()
    return values


def require_internal_caller(event: dict[str, Any], allowed: set[str]) -> None:
    context = event.get("requestContext") if isinstance(event, dict) else None
    identity = context.get("identity") if isinstance(context, dict) else None
    caller = identity.get("userArn") if isinstance(identity, dict) else None
    if type(caller) is not str or caller not in allowed:
        raise HttpError(403, "forbidden", "You do not have access to this resource.")


_require_caller = require_internal_caller
