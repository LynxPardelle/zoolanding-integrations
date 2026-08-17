"""Shared strict transport for small AWS_IAM command Lambda entrypoints."""

from __future__ import annotations

import os
import re
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


_IAM_ROLE_ARN = re.compile(
    r"arn:(?P<partition>aws|aws-us-gov|aws-cn):iam::"
    r"(?P<account>[0-9]{12}):role/"
    r"(?P<role>(?:[A-Za-z0-9+=,.@_-]+/)*[A-Za-z0-9+=,.@_-]+)",
    re.ASCII,
)
_ASSUMED_ROLE_ARN = re.compile(
    r"arn:(?P<partition>aws|aws-us-gov|aws-cn):sts::"
    r"(?P<account>[0-9]{12}):assumed-role/"
    r"(?P<role>(?:[A-Za-z0-9+=,.@_-]+/)*[A-Za-z0-9+=,.@_-]+)/"
    r"(?P<session>[A-Za-z0-9+=,.@_-]+)",
    re.ASCII,
)


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
    return _configured_callers("INTERNAL_CALLER_ARNS")


def configured_smtp_activation_callers() -> set[str]:
    return _configured_callers("SMTP_ACTIVATION_CALLER_ARNS")


def _configured_callers(variable_name: str) -> set[str]:
    raw = os.getenv(variable_name, "")
    if not raw:
        return set()
    values = [value.strip() for value in raw.split(",")]
    if not values or any(_IAM_ROLE_ARN.fullmatch(value) is None for value in values):
        return set()
    return set(values)


def require_internal_caller(event: dict[str, Any], allowed: set[str]) -> None:
    context = event.get("requestContext") if isinstance(event, dict) else None
    identity = context.get("identity") if isinstance(context, dict) else None
    caller = identity.get("userArn") if isinstance(identity, dict) else None
    normalized = _normalize_role_arn(caller, allowed)
    if normalized is None or normalized not in allowed:
        raise HttpError(403, "forbidden", "You do not have access to this resource.")


def _normalize_role_arn(value: object, allowed: set[str]) -> str | None:
    if type(value) is not str:
        return None
    direct = _IAM_ROLE_ARN.fullmatch(value)
    if direct is not None:
        return value if value in allowed else None
    match = _ASSUMED_ROLE_ARN.fullmatch(value)
    if match is None:
        return None
    candidates: list[tuple[str, str]] = []
    for role_arn in allowed:
        role = _IAM_ROLE_ARN.fullmatch(role_arn)
        if (
            role is not None
            and role["partition"] == match["partition"]
            and role["account"] == match["account"]
        ):
            candidates.append((role_arn, role["role"]))
    exact = [role_arn for role_arn, role_name in candidates if role_name == match["role"]]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    by_name = [
        role_arn
        for role_arn, role_name in candidates
        if role_name.rsplit("/", 1)[-1] == match["role"]
    ]
    return by_name[0] if len(by_name) == 1 else None


_require_caller = require_internal_caller
