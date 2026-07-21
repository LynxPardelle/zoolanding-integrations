"""Fail-closed AWS dependency composition for implemented control-plane routes."""

from __future__ import annotations

import os
import time
from typing import Any

try:
    from common.auth_admin import DynamoAuthStore
    from common.published_policy import PublishedPolicyResolver
    from connection_admin import ConnectionAdmin
    from internal_connections import (
        ConnectionRegistrationService,
        ConnectionResolutionService,
    )
    from registry import BindingResolver, ConnectionRegistry, DynamoRegistryBackend
except ModuleNotFoundError:
    from src.common.auth_admin import DynamoAuthStore
    from src.common.published_policy import PublishedPolicyResolver
    from src.connection_admin import ConnectionAdmin
    from src.internal_connections import (
        ConnectionRegistrationService,
        ConnectionResolutionService,
    )
    from src.registry import BindingResolver, ConnectionRegistry, DynamoRegistryBackend


class RuntimeCompositionError(RuntimeError):
    pass


def browser_runtime() -> dict[str, Any]:
    boto3 = _boto3()
    dynamodb = boto3.resource("dynamodb")
    policy_resolver = PublishedPolicyResolver(
        dynamodb.Table(_required("CONFIG_REGISTRY_TABLE_NAME")),
        boto3.client("s3"),
        _required("CONFIG_PAYLOADS_BUCKET_NAME"),
    )
    registry = _registry(boto3)
    return {
        "policy_resolver": policy_resolver,
        "auth_store": DynamoAuthStore(
            _required("AUTH_SESSION_TABLE_NAME"),
            _required("AUTH_USER_STATE_TABLE_NAME"),
            dynamodb=dynamodb,
        ),
        "registry": registry,
        "environment": _environment(),
        "now_epoch": int(time.time()),
    }


def connection_registration_runtime() -> dict[str, Any]:
    boto3 = _boto3()
    dynamodb = boto3.resource("dynamodb")
    policies = PublishedPolicyResolver(
        dynamodb.Table(_required("CONFIG_REGISTRY_TABLE_NAME")),
        boto3.client("s3"),
        _required("CONFIG_PAYLOADS_BUCKET_NAME"),
    )
    admin = ConnectionAdmin(_registry(boto3), boto3.client("secretsmanager"))
    return {"service": ConnectionRegistrationService(policies, admin)}


def connection_resolution_runtime() -> dict[str, Any]:
    return {
        "service": ConnectionResolutionService(BindingResolver(_registry(_boto3())))
    }


def _registry(boto3: Any) -> ConnectionRegistry:
    backend = DynamoRegistryBackend(
        _required("INTEGRATION_REGISTRY_TABLE_NAME"),
        client=boto3.client("dynamodb"),
    )
    return ConnectionRegistry(backend)


def _boto3() -> Any:
    try:
        import boto3  # type: ignore
    except Exception:
        raise RuntimeCompositionError("Runtime dependencies are unavailable") from None
    return boto3


def _environment() -> str:
    value = _required("ENVIRONMENT_NAME")
    if value not in {"test", "production"}:
        raise RuntimeCompositionError("Runtime environment is invalid")
    return value


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not 1 <= len(value) <= 255 or any(ord(character) < 33 for character in value):
        raise RuntimeCompositionError("Runtime configuration is unavailable")
    return value
