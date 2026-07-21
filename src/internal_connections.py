"""Typed server-only services for registration and binding resolution."""

from __future__ import annotations

from typing import Any

try:
    from contracts.internal import ConnectionRegistration, InternalCommand
    from domain.integrations import IntegrationConnection
except ModuleNotFoundError:
    from src.contracts.internal import ConnectionRegistration, InternalCommand
    from src.domain.integrations import IntegrationConnection


class InternalConnectionError(RuntimeError):
    pass


class ConnectionRegistrationService:
    def __init__(self, policy_resolver: Any, connection_admin: Any):
        if policy_resolver is None or connection_admin is None:
            raise InternalConnectionError("Connection registration is unavailable")
        self._policies = policy_resolver
        self._admin = connection_admin

    def register(self, registration: ConnectionRegistration) -> dict[str, Any]:
        if type(registration) is not ConnectionRegistration:
            raise InternalConnectionError("Connection registration is invalid")
        scope = registration.scope
        try:
            policy = self._policies.resolve(
                environment=scope.environment,
                domain=scope.domain,
                tenant_id=scope.tenant_id,
                draft_id=scope.draft_id,
            )
        except Exception:
            raise InternalConnectionError(
                "Published connection policy is unavailable"
            ) from None
        matches = tuple(
            item
            for item in policy.bindings
            if item.binding_id == registration.connection_id
            and item.connection_id == registration.connection_id
            and item.provider == registration.provider
            and item.mode == registration.mode
            and item.capabilities == frozenset(registration.capabilities)
        )
        if policy.scope != scope or len(matches) != 1:
            raise InternalConnectionError("Connection registration is not published")
        binding = matches[0]
        metadata = (
            {"accountReference": registration.account_reference}
            if registration.provider == "stripe"
            else {
                "adapterId": "smtp2go-smtp-v1",
                "host": "mail.smtp2go.com",
                "port": 465,
                "canonicalSendingDomain": (
                    "zoolandingpage.com.mx"
                    if scope.environment == "test"
                    else scope.domain
                ),
                "accountOwnershipState": "audited",
            }
        )
        try:
            connection = IntegrationConnection(
                scope=scope,
                connection_id=registration.connection_id,
                provider=registration.provider,
                adapter_version="v1",
                status="pending",
                mode=registration.mode,
                capabilities=frozenset(registration.capabilities),
                provider_metadata=metadata,
            )
            return self._admin.register(
                connection,
                binding,
                credential_reference=registration.credential_reference,
                idempotency_key=registration.idempotency_key,
            )
        except Exception:
            raise InternalConnectionError("Connection registration failed") from None


class ConnectionResolutionService:
    def __init__(self, binding_resolver: Any):
        if binding_resolver is None:
            raise InternalConnectionError("Connection resolution is unavailable")
        self._bindings = binding_resolver

    def resolve(self, command: InternalCommand) -> dict[str, Any]:
        if type(command) is not InternalCommand or command.kind != "connection-resolve":
            raise InternalConnectionError("Connection resolution is invalid")
        try:
            resolved = self._bindings.resolve(
                command.scope,
                command.connection_id,
                provider=command.input["provider"],
                capability=command.input["capability"],
            )
            connection = resolved.connection
        except Exception:
            raise InternalConnectionError("Connection resolution failed") from None
        result = {
            "connectionId": connection.connection_id,
            "provider": connection.provider,
            "mode": connection.mode,
            "adapterVersion": connection.adapter_version,
            "credentialReference": connection.credential_reference,
        }
        if connection.provider == "email.smtp":
            result["endpoint"] = {
                "host": connection.provider_metadata["host"],
                "port": connection.provider_metadata["port"],
                "canonicalSendingDomain": connection.provider_metadata[
                    "canonicalSendingDomain"
                ],
            }
        return result
