"""Register deterministic credential metadata without reading credential values."""

from __future__ import annotations

from typing import Any

try:
    from domain.integrations import IntegrationBinding, IntegrationConnection
except ModuleNotFoundError:
    from src.domain.integrations import IntegrationBinding, IntegrationConnection


class ConnectionAdminError(RuntimeError):
    pass


class ConnectionAdmin:
    def __init__(self, registry: Any, secrets_client: Any):
        if registry is None or secrets_client is None:
            raise ConnectionAdminError("Integration registration is unavailable")
        self._registry = registry
        self._secrets = secrets_client

    def register(
        self,
        connection: IntegrationConnection,
        binding: IntegrationBinding,
        *,
        credential_reference: object,
        idempotency_key: object,
    ) -> dict[str, Any]:
        if (
            type(connection) is not IntegrationConnection
            or type(binding) is not IntegrationBinding
            or credential_reference != connection.credential_reference
        ):
            raise ConnectionAdminError("Integration registration is invalid")
        try:
            metadata = self._secrets.describe_secret(
                SecretId=connection.credential_reference
            )
        except Exception:
            raise ConnectionAdminError("Credential metadata is unavailable") from None
        _validate_secret_metadata(connection, metadata)
        try:
            self._registry.register(connection, binding, idempotency_key)
        except Exception:
            raise ConnectionAdminError("Integration registration failed") from None
        return {
            "connectionId": connection.connection_id,
            "status": connection.status,
            "mode": connection.mode,
            "revision": connection.revision,
        }


def _validate_secret_metadata(
    connection: IntegrationConnection, metadata: object
) -> None:
    if (
        not isinstance(metadata, dict)
        or metadata.get("Name") != connection.credential_reference
        or metadata.get("DeletedDate") is not None
    ):
        raise ConnectionAdminError("Credential metadata is invalid")
    raw_tags = metadata.get("Tags")
    if not isinstance(raw_tags, list) or not 1 <= len(raw_tags) <= 50:
        raise ConnectionAdminError("Credential metadata is invalid")
    tags: dict[str, str] = {}
    for item in raw_tags:
        if (
            not isinstance(item, dict)
            or set(item) != {"Key", "Value"}
            or type(item.get("Key")) is not str
            or type(item.get("Value")) is not str
            or item["Key"] in tags
        ):
            raise ConnectionAdminError("Credential metadata is invalid")
        tags[item["Key"]] = item["Value"]
    required = (
        {
            "zoolanding:environment": connection.scope.environment,
            "zoolanding:secret-purpose": "stripe-connect-platform",
            "zoolanding:enabled": "true",
        }
        if connection.provider == "stripe"
        else {
            "zoolanding:environment": connection.scope.environment,
            "zoolanding:tenant-id": connection.scope.tenant_id,
            "zoolanding:draft-id": connection.scope.draft_id,
            "zoolanding:secret-purpose": "smtp",
            "zoolanding:connection-id": connection.connection_id,
            "zoolanding:enabled": "true",
        }
    )
    if any(tags.get(key) != value for key, value in required.items()):
        raise ConnectionAdminError("Credential metadata is invalid")
