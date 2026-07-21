"""Draft-partitioned connection registry and non-authorizing routing claims."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping

try:
    from domain.integrations import (
        IntegrationBinding,
        IntegrationConnection,
        IntegrationScope,
    )
except ModuleNotFoundError:
    from src.domain.integrations import (
        IntegrationBinding,
        IntegrationConnection,
        IntegrationScope,
    )


class RegistryError(RuntimeError):
    pass


class RegistryAccessDenied(RegistryError):
    pass


class RegistryConflict(RegistryError):
    pass


_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}", re.ASCII)
_STRIPE_ACCOUNT = re.compile(r"acct_[A-Za-z0-9]{8,64}", re.ASCII)


@dataclass(frozen=True, slots=True)
class ResolvedBinding:
    binding: IntegrationBinding
    connection: IntegrationConnection


class ConnectionRegistry:
    def __init__(self, backend: Any):
        if backend is None:
            raise RegistryError("Integration registry is unavailable")
        self._backend = backend

    def register(
        self,
        connection: IntegrationConnection,
        binding: IntegrationBinding,
        idempotency_key: object,
    ) -> None:
        if (
            type(connection) is not IntegrationConnection
            or type(binding) is not IntegrationBinding
        ):
            raise RegistryAccessDenied("Integration registration is invalid")
        if (
            connection.scope != binding.scope
            or connection.connection_id != binding.connection_id
            or connection.provider != binding.provider
            or connection.mode != binding.mode
            or not binding.capabilities.issubset(connection.capabilities)
        ):
            raise RegistryAccessDenied("Integration registration is invalid")
        token = _idempotency_key(idempotency_key)
        records = (connection.to_record(), binding.to_record())
        registration_hash = _registration_hash(records)
        records = tuple(
            {**record, "registrationHash": registration_hash} for record in records
        )
        sentinels = tuple(
            {**sentinel, "registrationHash": registration_hash}
            for sentinel in (
                _routing_sentinel(connection),
                *_isolation_sentinels(connection),
            )
        )
        replay = self._exact_replay(records, sentinels)
        if replay is True:
            return
        if replay is False:
            raise RegistryConflict("Integration registration conflicted")
        try:
            self._backend.put_registration(
                records,
                sentinels,
                token,
            )
        except RegistryConflict:
            if self._exact_replay(records, sentinels) is True:
                return
            raise
        except Exception:
            raise RegistryConflict("Integration registration conflicted") from None

    def _exact_replay(
        self,
        records: tuple[dict[str, Any], ...],
        sentinels: tuple[dict[str, Any], ...],
    ) -> bool | None:
        expected = records + sentinels
        try:
            existing = tuple(
                self._backend.get(item["pk"], item["sk"]) for item in expected
            )
        except Exception:
            raise RegistryError("Integration registry is unavailable") from None
        if all(item is None for item in existing):
            return None
        if any(item is None for item in existing):
            return False
        return existing == expected

    def connection(
        self, scope: IntegrationScope, connection_id: object
    ) -> IntegrationConnection:
        connection_id = _safe_id(connection_id)
        try:
            record = self._backend.get(
                scope.partition_key, f"CONNECTION#{connection_id}"
            )
            return _connection_from_record(scope, record)
        except RegistryError:
            raise
        except Exception:
            raise RegistryAccessDenied(
                "Integration connection is unavailable"
            ) from None

    def binding(
        self, scope: IntegrationScope, binding_id: object
    ) -> IntegrationBinding:
        binding_id = _safe_id(binding_id)
        try:
            record = self._backend.get(scope.partition_key, f"BINDING#{binding_id}")
            return _binding_from_record(scope, record)
        except RegistryError:
            raise
        except Exception:
            raise RegistryAccessDenied("Integration binding is unavailable") from None

    def list_connections(
        self, scope: IntegrationScope
    ) -> tuple[IntegrationConnection, ...]:
        try:
            records = self._backend.query_connections(scope.partition_key)
            if not isinstance(records, (list, tuple)) or len(records) > 100:
                raise ValueError
            return tuple(_connection_from_record(scope, item) for item in records)
        except Exception:
            raise RegistryAccessDenied(
                "Integration connections are unavailable"
            ) from None

    def update_status(
        self,
        scope: IntegrationScope,
        connection_id: object,
        status: object,
        expected_revision: object,
    ) -> IntegrationConnection:
        connection_id = _safe_id(connection_id)
        if type(status) is not str or status not in {"pending", "disabled"}:
            raise RegistryAccessDenied("Integration status is invalid")
        if type(expected_revision) is not int or expected_revision < 1:
            raise RegistryAccessDenied("Integration revision is invalid")
        try:
            record = self._backend.update_status(
                scope.partition_key,
                f"CONNECTION#{connection_id}",
                status,
                expected_revision,
            )
            return _connection_from_record(scope, record)
        except RegistryError:
            raise
        except Exception:
            raise RegistryConflict("Integration update conflicted") from None

    def activate_ready(
        self,
        scope: IntegrationScope,
        connection_id: object,
        readiness: object,
        expected_revision: object,
    ) -> IntegrationConnection:
        connection_id = _safe_id(connection_id)
        if type(expected_revision) is not int or expected_revision < 1:
            raise RegistryAccessDenied("Integration revision is invalid")
        sanitized = _provider_readiness(readiness)
        try:
            record = self._backend.activate_ready(
                scope.partition_key,
                f"CONNECTION#{connection_id}",
                sanitized,
                expected_revision,
            )
            return _connection_from_record(scope, record)
        except RegistryError:
            raise
        except Exception:
            raise RegistryConflict("Integration update conflicted") from None

    def activate_smtp(
        self,
        candidate: IntegrationConnection,
        expected_revision: object,
        idempotency_key: object,
    ) -> IntegrationConnection:
        if (
            type(candidate) is not IntegrationConnection
            or candidate.provider != "email.smtp"
            or candidate.status != "active"
            or type(expected_revision) is not int
            or expected_revision < 1
            or candidate.revision != expected_revision + 1
        ):
            raise RegistryAccessDenied("SMTP activation is invalid")
        token = _idempotency_key(idempotency_key)
        pk = candidate.scope.partition_key
        sk = f"CONNECTION#{candidate.connection_id}"
        try:
            current_record = self._backend.get(pk, sk)
            current = _connection_from_record(candidate.scope, current_record)
            activation_hash = _registration_hash((candidate.to_record(),))
            if current.status == "active":
                if (
                    current == candidate
                    and current_record.get("activationHash") == activation_hash
                ):
                    return current
                raise RegistryConflict("SMTP activation conflicted")
            base_metadata = {
                key: candidate.provider_metadata[key]
                for key in (
                    "adapterId",
                    "host",
                    "port",
                    "tlsMode",
                    "canonicalSendingDomain",
                )
            }
            registration_hash = current_record.get("registrationHash")
            if (
                current.status != "pending"
                or current.revision != expected_revision
                or current.provider != candidate.provider
                or current.mode != candidate.mode
                or current.capabilities != candidate.capabilities
                or dict(current.provider_metadata) != base_metadata
                or type(registration_hash) is not str
                or re.fullmatch(r"[a-f0-9]{64}", registration_hash, re.ASCII) is None
            ):
                raise RegistryConflict("SMTP activation conflicted")
            sentinels = tuple(
                {
                    **item,
                    "registrationHash": registration_hash,
                    "activationHash": activation_hash,
                }
                for item in _smtp_isolation_sentinels(candidate)
            )
            record = self._backend.activate_smtp(
                pk,
                sk,
                dict(candidate.provider_metadata),
                expected_revision,
                registration_hash,
                activation_hash,
                sentinels,
                token,
            )
            return _connection_from_record(candidate.scope, record)
        except RegistryConflict:
            try:
                record = self._backend.get(pk, sk)
                active = _connection_from_record(candidate.scope, record)
                if (
                    active == candidate
                    and record.get("activationHash")
                    == _registration_hash((candidate.to_record(),))
                ):
                    return active
            except Exception:
                pass
            raise
        except RegistryError:
            raise
        except Exception:
            raise RegistryConflict("SMTP activation conflicted") from None

    def bind_stripe_account(
        self,
        scope: IntegrationScope,
        connection_id: object,
        account_reference: object,
        ownership: object,
        expected_revision: object,
    ) -> IntegrationConnection:
        connection_id = _safe_id(connection_id)
        if (
            type(account_reference) is not str
            or _STRIPE_ACCOUNT.fullmatch(account_reference) is None
            or ownership not in {"external-oauth", "platform-controller"}
            or type(expected_revision) is not int
            or expected_revision < 1
        ):
            raise RegistryAccessDenied("Stripe account binding is invalid")
        try:
            pk = scope.partition_key
            sk = f"CONNECTION#{connection_id}"
            current_record = self._backend.get(pk, sk)
            current = _connection_from_record(scope, current_record)
            registration_hash = current_record.get("registrationHash")
            if (
                current.provider != "stripe"
                or current.status != "pending"
                or current.revision != expected_revision
                or type(registration_hash) is not str
                or re.fullmatch(r"[a-f0-9]{64}", registration_hash, re.ASCII) is None
            ):
                raise ValueError
            current_account = current.provider_metadata.get("accountReference")
            if current_account is not None:
                if (
                    current_account != account_reference
                    or current.provider_metadata.get("accountOwnership") != ownership
                ):
                    raise ValueError
                sentinel = {
                    **_routing_sentinel(current),
                    "registrationHash": registration_hash,
                }
                record = self._backend.rebind_stripe_account(
                    pk,
                    sk,
                    account_reference,
                    ownership,
                    expected_revision,
                    registration_hash,
                    sentinel,
                )
                return _connection_from_record(scope, record)
            sentinel = {
                **_routing_sentinel(
                    IntegrationConnection(
                        scope=scope,
                        connection_id=connection_id,
                        provider="stripe",
                        adapter_version=current.adapter_version,
                        status="pending",
                        mode=current.mode,
                        capabilities=current.capabilities,
                        provider_metadata={
                            **dict(current.provider_metadata),
                            "accountReference": account_reference,
                            "accountOwnership": ownership,
                        },
                        revision=current.revision,
                    )
                ),
                "registrationHash": registration_hash,
            }
            record = self._backend.bind_stripe_account(
                pk,
                sk,
                account_reference,
                sentinel,
                ownership,
                expected_revision,
                registration_hash,
                _routing_sentinel(current)["pk"],
            )
            return _connection_from_record(scope, record)
        except RegistryError:
            raise
        except Exception:
            raise RegistryConflict("Stripe account binding conflicted") from None

    def disable_stripe_account(
        self,
        scope: IntegrationScope,
        connection_id: object,
        account_reference: object,
        expected_revision: object,
    ) -> IntegrationConnection:
        connection_id = _safe_id(connection_id)
        if (
            type(account_reference) is not str
            or _STRIPE_ACCOUNT.fullmatch(account_reference) is None
            or type(expected_revision) is not int
            or expected_revision < 1
        ):
            raise RegistryAccessDenied("Stripe account disable is invalid")
        try:
            pk = scope.partition_key
            sk = f"CONNECTION#{connection_id}"
            current_record = self._backend.get(pk, sk)
            current = _connection_from_record(scope, current_record)
            registration_hash = current_record.get("registrationHash")
            if (
                current.provider != "stripe"
                or current.revision != expected_revision
                or current.provider_metadata.get("accountReference")
                != account_reference
                or type(registration_hash) is not str
            ):
                raise ValueError
            sentinel_pk = _routing_sentinel(current)["pk"]
            record = self._backend.disable_stripe_account(
                pk,
                sk,
                sentinel_pk,
                expected_revision,
                registration_hash,
            )
            return _connection_from_record(scope, record)
        except RegistryError:
            raise
        except Exception:
            raise RegistryConflict("Stripe account disable conflicted") from None

    def stripe_webhook_connection(
        self,
        *,
        environment: object,
        mode: object,
        account_reference: object,
        event_type: object = None,
    ) -> IntegrationConnection:
        """Resolve a Connect account through its non-authorizing hashed claim."""
        if environment not in {"test", "production"}:
            raise RegistryAccessDenied("Stripe webhook routing is unavailable")
        expected_mode = "test" if environment == "test" else "live"
        if mode != expected_mode or (
            type(account_reference) is not str
            or _STRIPE_ACCOUNT.fullmatch(account_reference) is None
        ):
            raise RegistryAccessDenied("Stripe webhook routing is unavailable")
        digest = hashlib.sha256(account_reference.encode("ascii")).hexdigest()
        claim_pk = f"ROUTING#{environment}#{mode}#{digest}"
        try:
            claim = self._backend.get(claim_pk, "CLAIM")
            if not isinstance(claim, Mapping):
                raise ValueError
            expected_claim_keys = {
                "pk",
                "sk",
                "itemType",
                "authorizes",
                "environment",
                "tenantId",
                "draftId",
                "domain",
                "provider",
                "connectionId",
                "registrationHash",
            }
            if (
                set(claim) != expected_claim_keys
                or claim.get("pk") != claim_pk
                or claim.get("sk") != "CLAIM"
                or claim.get("itemType") != "AccountRoutingSentinel"
                or claim.get("authorizes") is not False
                or claim.get("environment") != environment
                or claim.get("provider") != "stripe"
                or type(claim.get("registrationHash")) is not str
                or re.fullmatch(r"[a-f0-9]{64}", claim["registrationHash"], re.ASCII)
                is None
            ):
                raise ValueError
            routed_scope = IntegrationScope(
                environment=claim["environment"],
                tenant_id=claim["tenantId"],
                draft_id=claim["draftId"],
                domain=claim["domain"],
            )
            connection_id = _safe_id(claim["connectionId"])
            record = self._backend.get(
                routed_scope.partition_key, f"CONNECTION#{connection_id}"
            )
            connection = _connection_from_record(routed_scope, record)
            if (
                not isinstance(record, Mapping)
                or record.get("registrationHash") != claim["registrationHash"]
                or connection.connection_id != connection_id
                or connection.provider != "stripe"
                or connection.mode != mode
                or (
                    event_type == "account.application.deauthorized"
                    and connection.status not in {"pending", "active"}
                )
                or (
                    event_type != "account.application.deauthorized"
                    and (
                        connection.status != "active"
                        or not _readiness_is_complete(
                            connection.provider_metadata.get("readiness")
                        )
                    )
                )
                or connection.provider_metadata.get("accountReference")
                != account_reference
            ):
                raise ValueError
            return connection
        except RegistryAccessDenied:
            raise
        except Exception:
            raise RegistryAccessDenied(
                "Stripe webhook routing is unavailable"
            ) from None


class BindingResolver:
    def __init__(self, registry: ConnectionRegistry):
        self._registry = registry

    def resolve(
        self,
        scope: IntegrationScope,
        binding_id: object,
        *,
        provider: str,
        capability: str,
    ) -> ResolvedBinding:
        try:
            binding = self._registry.binding(scope, binding_id)
            connection = self._registry.connection(scope, binding.connection_id)
        except RegistryError:
            raise RegistryAccessDenied("Integration binding is unavailable") from None
        if (
            binding.status != "active"
            or (
                connection.status != "active"
                and not (
                    connection.status == "pending"
                    and capability == "connect-onboarding"
                )
            )
            or binding.provider != provider
            or connection.provider != provider
            or binding.mode != connection.mode
            or capability not in binding.capabilities
            or capability not in connection.capabilities
            or (
                provider == "stripe"
                and capability != "connect-onboarding"
                and not _readiness_is_complete(
                    connection.provider_metadata.get("readiness")
                )
            )
        ):
            raise RegistryAccessDenied("Integration binding is unavailable")
        return ResolvedBinding(binding, connection)


class DynamoRegistryBackend:
    def __init__(self, table_name: str, client: Any = None):
        if not isinstance(table_name, str) or not table_name.strip():
            raise RegistryError("Integration registry is unavailable")
        if client is None:
            try:
                import boto3  # type: ignore

                client = boto3.client("dynamodb")
            except Exception:
                raise RegistryError("Integration registry is unavailable") from None
        self._table_name = table_name
        self._client = client

    def put_registration(
        self,
        records: tuple[dict[str, Any], ...],
        sentinels: tuple[dict[str, Any], ...],
        idempotency_key: str,
    ) -> None:
        items = records + sentinels
        transact_items = [
            {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _serialize(item),
                    "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)",
                }
            }
            for item in items
        ]
        try:
            self._client.transact_write_items(
                TransactItems=transact_items,
                ClientRequestToken=hashlib.sha256(
                    (
                        idempotency_key
                        + "\0"
                        + str(records[0].get("registrationHash", ""))
                    ).encode("utf-8")
                ).hexdigest()[:36],
            )
        except Exception:
            raise RegistryConflict("Integration registration conflicted") from None

    def get(self, pk: str, sk: str) -> dict[str, Any] | None:
        try:
            response = self._client.get_item(
                TableName=self._table_name,
                Key=_serialize({"pk": pk, "sk": sk}),
                ConsistentRead=True,
            )
        except Exception:
            raise RegistryError("Integration registry is unavailable") from None
        return _deserialize(response.get("Item")) if response.get("Item") else None

    def query_connections(self, pk: str) -> list[dict[str, Any]]:
        try:
            response = self._client.query(
                TableName=self._table_name,
                KeyConditionExpression="pk = :pk AND begins_with(sk, :prefix)",
                ExpressionAttributeValues=_serialize_values(
                    {":pk": pk, ":prefix": "CONNECTION#"}
                ),
                ConsistentRead=True,
                Limit=100,
            )
            if (
                not isinstance(response, Mapping)
                or response.get("LastEvaluatedKey")
                or not isinstance(response.get("Items", []), list)
            ):
                raise ValueError
            return [_deserialize(item) for item in response.get("Items", [])]
        except Exception:
            raise RegistryError("Integration registry is unavailable") from None

    def update_status(
        self,
        pk: str,
        sk: str,
        status: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        try:
            response = self._client.update_item(
                TableName=self._table_name,
                Key=_serialize({"pk": pk, "sk": sk}),
                UpdateExpression="SET #status = :status, revision = :next_revision",
                ConditionExpression=(
                    "itemType = :item_type AND revision = :expected_revision"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=_serialize_values(
                    {
                        ":status": status,
                        ":next_revision": expected_revision + 1,
                        ":item_type": "IntegrationConnection",
                        ":expected_revision": expected_revision,
                    }
                ),
                ReturnValues="ALL_NEW",
            )
        except Exception:
            raise RegistryConflict("Integration update conflicted") from None
        return _deserialize(response.get("Attributes"))

    def activate_smtp(
        self,
        pk: str,
        sk: str,
        provider_metadata: dict[str, Any],
        expected_revision: int,
        registration_hash: str,
        activation_hash: str,
        sentinels: tuple[dict[str, Any], ...],
        idempotency_key: str,
    ) -> dict[str, Any]:
        values = _serialize_values(
            {
                ":active": "active",
                ":pending": "pending",
                ":next_revision": expected_revision + 1,
                ":expected_revision": expected_revision,
                ":provider": "email.smtp",
                ":item_type": "IntegrationConnection",
                ":registration_hash": registration_hash,
                ":activation_hash": activation_hash,
                ":metadata": provider_metadata,
            }
        )
        operations = [
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _serialize({"pk": pk, "sk": sk}),
                    "UpdateExpression": (
                        "SET #status = :active, revision = :next_revision, "
                        "providerMetadata = :metadata, activationHash = :activation_hash"
                    ),
                    "ConditionExpression": (
                        "itemType = :item_type AND provider = :provider "
                        "AND #status = :pending AND revision = :expected_revision "
                        "AND registrationHash = :registration_hash "
                        "AND attribute_not_exists(activationHash)"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": values,
                }
            },
            *(
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": _serialize(sentinel),
                        "ConditionExpression": (
                            "attribute_not_exists(pk) AND attribute_not_exists(sk)"
                        ),
                    }
                }
                for sentinel in sentinels
            ),
        ]
        try:
            self._client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=hashlib.sha256(
                    (idempotency_key + "\0" + activation_hash).encode("utf-8")
                ).hexdigest()[:36],
            )
            return self.get(pk, sk)
        except Exception:
            raise RegistryConflict("SMTP activation conflicted") from None

    def activate_ready(
        self,
        pk: str,
        sk: str,
        readiness: dict[str, Any],
        expected_revision: int,
    ) -> dict[str, Any]:
        try:
            response = self._client.update_item(
                TableName=self._table_name,
                Key=_serialize({"pk": pk, "sk": sk}),
                UpdateExpression=(
                    "SET #status = :status, revision = :next_revision, "
                    "providerMetadata.readiness = :readiness"
                ),
                ConditionExpression=(
                    "itemType = :item_type AND revision = :expected_revision "
                    "AND provider = :provider"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=_serialize_values(
                    {
                        ":status": "active",
                        ":next_revision": expected_revision + 1,
                        ":readiness": readiness,
                        ":item_type": "IntegrationConnection",
                        ":expected_revision": expected_revision,
                        ":provider": "stripe",
                    }
                ),
                ReturnValues="ALL_NEW",
            )
        except Exception:
            raise RegistryConflict("Integration update conflicted") from None
        return _deserialize(response.get("Attributes"))

    def bind_stripe_account(
        self,
        pk: str,
        sk: str,
        account_reference: str,
        sentinel: dict[str, Any],
        ownership: str,
        expected_revision: int,
        registration_hash: str,
        old_sentinel_pk: str,
    ) -> dict[str, Any]:
        values = _serialize_values(
            {
                ":account": account_reference,
                ":ownership": ownership,
                ":next_revision": expected_revision + 1,
                ":expected_revision": expected_revision,
                ":provider": "stripe",
                ":registration_hash": registration_hash,
            }
        )
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": _serialize({"pk": pk, "sk": sk}),
                            "UpdateExpression": (
                                "SET revision = :next_revision, "
                                "providerMetadata.accountReference = :account, "
                                "providerMetadata.accountOwnership = :ownership"
                            ),
                            "ConditionExpression": (
                                "revision = :expected_revision AND provider = :provider "
                                "AND registrationHash = :registration_hash "
                                "AND attribute_not_exists(providerMetadata.accountReference)"
                            ),
                            "ExpressionAttributeValues": values,
                        }
                    },
                    {
                        "Delete": {
                            "TableName": self._table_name,
                            "Key": _serialize({"pk": old_sentinel_pk, "sk": "CLAIM"}),
                            "ConditionExpression": "registrationHash = :registration_hash",
                            "ExpressionAttributeValues": _serialize_values(
                                {":registration_hash": registration_hash}
                            ),
                        }
                    },
                    {
                        "Put": {
                            "TableName": self._table_name,
                            "Item": _serialize(sentinel),
                            "ConditionExpression": (
                                "attribute_not_exists(pk) AND attribute_not_exists(sk)"
                            ),
                        }
                    },
                ]
            )
            return self.get(pk, sk)
        except Exception:
            raise RegistryConflict("Stripe account binding conflicted") from None

    def rebind_stripe_account(
        self,
        pk: str,
        sk: str,
        account_reference: str,
        ownership: str,
        expected_revision: int,
        registration_hash: str,
        sentinel: Mapping[str, Any],
    ) -> dict[str, Any]:
        update_values = _serialize_values(
            {
                ":account": account_reference,
                ":ownership": ownership,
                ":pending": "pending",
                ":next_revision": expected_revision + 1,
                ":expected_revision": expected_revision,
                ":provider": "stripe",
                ":registration_hash": registration_hash,
            }
        )
        sentinel_values = _serialize_values(
            {
                ":sentinel_type": "AccountRoutingSentinel",
                ":environment": sentinel["environment"],
                ":tenant_id": sentinel["tenantId"],
                ":draft_id": sentinel["draftId"],
                ":domain": sentinel["domain"],
                ":provider": "stripe",
                ":connection_id": sk.removeprefix("CONNECTION#"),
                ":false": False,
                ":registration_hash": registration_hash,
            }
        )
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": _serialize({"pk": pk, "sk": sk}),
                            "UpdateExpression": (
                                "SET revision = :next_revision "
                                "REMOVE providerMetadata.readiness"
                            ),
                            "ConditionExpression": (
                                "revision = :expected_revision AND provider = :provider "
                                "AND #status = :pending "
                                "AND registrationHash = :registration_hash "
                                "AND providerMetadata.accountReference = :account "
                                "AND providerMetadata.accountOwnership = :ownership"
                            ),
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": update_values,
                        }
                    },
                    {
                        "ConditionCheck": {
                            "TableName": self._table_name,
                            "Key": _serialize(
                                {"pk": sentinel["pk"], "sk": sentinel["sk"]}
                            ),
                            "ConditionExpression": (
                                "itemType = :sentinel_type "
                                "AND environment = :environment "
                                "AND tenantId = :tenant_id "
                                "AND draftId = :draft_id "
                                "AND domain = :domain "
                                "AND provider = :provider "
                                "AND connectionId = :connection_id "
                                "AND authorizes = :false "
                                "AND registrationHash = :registration_hash"
                            ),
                            "ExpressionAttributeValues": sentinel_values,
                        }
                    },
                ]
            )
            return self.get(pk, sk)
        except Exception:
            raise RegistryConflict("Stripe account binding conflicted") from None

    def disable_stripe_account(
        self,
        pk: str,
        sk: str,
        sentinel_pk: str,
        expected_revision: int,
        registration_hash: str,
    ) -> dict[str, Any]:
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table_name,
                            "Key": _serialize({"pk": pk, "sk": sk}),
                            "UpdateExpression": (
                                "SET #status = :disabled, revision = :next_revision "
                                "REMOVE providerMetadata.accountReference, "
                                "providerMetadata.accountOwnership, providerMetadata.readiness"
                            ),
                            "ConditionExpression": (
                                "revision = :expected_revision AND provider = :provider "
                                "AND registrationHash = :registration_hash"
                            ),
                            "ExpressionAttributeNames": {"#status": "status"},
                            "ExpressionAttributeValues": _serialize_values(
                                {
                                    ":disabled": "disabled",
                                    ":next_revision": expected_revision + 1,
                                    ":expected_revision": expected_revision,
                                    ":provider": "stripe",
                                    ":registration_hash": registration_hash,
                                }
                            ),
                        }
                    },
                    {
                        "Delete": {
                            "TableName": self._table_name,
                            "Key": _serialize({"pk": sentinel_pk, "sk": "CLAIM"}),
                            "ConditionExpression": "registrationHash = :registration_hash",
                            "ExpressionAttributeValues": _serialize_values(
                                {":registration_hash": registration_hash}
                            ),
                        }
                    },
                ]
            )
            return self.get(pk, sk)
        except Exception:
            raise RegistryConflict("Stripe account disable conflicted") from None


def _routing_sentinel(connection: IntegrationConnection) -> dict[str, Any]:
    account_reference = connection.provider_metadata.get("accountReference")
    if connection.provider != "stripe" or not isinstance(account_reference, str):
        digest_source = f"{connection.scope.partition_key}\0{connection.connection_id}"
    else:
        digest_source = account_reference
    digest = hashlib.sha256(digest_source.encode("utf-8")).hexdigest()
    return {
        "pk": f"ROUTING#{connection.scope.environment}#{connection.mode}#{digest}",
        "sk": "CLAIM",
        "itemType": "AccountRoutingSentinel",
        "authorizes": False,
        **connection.scope.fields(),
        "provider": connection.provider,
        "connectionId": connection.connection_id,
    }


def _isolation_sentinels(
    connection: IntegrationConnection,
) -> tuple[dict[str, Any], ...]:
    if connection.provider != "email.smtp" or connection.status != "active":
        return ()
    return _smtp_isolation_sentinels(connection)


def _smtp_isolation_sentinels(
    connection: IntegrationConnection,
) -> tuple[dict[str, Any], ...]:
    metadata = connection.provider_metadata
    credential_hash = metadata.get("credentialIsolationHash")
    if type(credential_hash) is not str:
        raise RegistryAccessDenied("SMTP activation is invalid")
    claims = [
        {
            "pk": f"ISOLATION#email.smtp#credential#{credential_hash}",
            "sk": "CLAIM",
            "itemType": "ConnectionIsolationSentinel",
            "authorizes": False,
            "environment": connection.scope.environment,
            "tenantId": connection.scope.tenant_id,
            "draftId": connection.scope.draft_id,
            "provider": "email.smtp",
            "claimType": "credential-isolation",
            "connectionId": connection.connection_id,
        },
    ]
    if connection.scope.environment == "production":
        account_hash = metadata.get("accountIsolationHash")
        domain = metadata.get("canonicalSendingDomain")
        if type(account_hash) is not str or type(domain) is not str:
            raise RegistryAccessDenied("SMTP activation is invalid")
        domain_hash = hashlib.sha256(domain.casefold().encode("ascii")).hexdigest()
        claims.extend(
            (
                {
                    **claims[0],
                    "pk": f"ISOLATION#production#email.smtp#account#{account_hash}",
                    "claimType": "account-isolation",
                },
                {
                    **claims[0],
                    "pk": f"ISOLATION#production#email.smtp#domain#{domain_hash}",
                    "claimType": "canonical-sending-domain",
                },
            )
        )
    return tuple(claims)


def _registration_hash(records: tuple[dict[str, Any], ...]) -> str:
    try:
        encoded = json.dumps(
            records,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError):
        raise RegistryAccessDenied("Integration registration is invalid") from None
    return hashlib.sha256(encoded).hexdigest()


def _connection_from_record(
    expected_scope: IntegrationScope, value: object
) -> IntegrationConnection:
    if not isinstance(value, Mapping):
        raise RegistryAccessDenied("Integration connection is unavailable")
    if (
        value.get("pk") != expected_scope.partition_key
        or value.get("sk") != f"CONNECTION#{value.get('connectionId')}"
        or value.get("itemType") != "IntegrationConnection"
        or any(
            value.get(key) != expected
            for key, expected in expected_scope.fields().items()
        )
    ):
        raise RegistryAccessDenied("Integration connection is unavailable")
    connection = IntegrationConnection(
        scope=expected_scope,
        connection_id=value.get("connectionId"),
        provider=value.get("provider"),
        adapter_version=value.get("adapterVersion"),
        status=value.get("status"),
        mode=value.get("mode"),
        capabilities=frozenset(value.get("capabilities", ())),
        provider_metadata=value.get("providerMetadata", {}),
        revision=value.get("revision"),
    )
    if value.get("credentialReference") != connection.credential_reference:
        raise RegistryAccessDenied("Integration connection is unavailable")
    return connection


def _binding_from_record(
    expected_scope: IntegrationScope, value: object
) -> IntegrationBinding:
    if not isinstance(value, Mapping):
        raise RegistryAccessDenied("Integration binding is unavailable")
    if (
        value.get("pk") != expected_scope.partition_key
        or value.get("sk") != f"BINDING#{value.get('bindingId')}"
        or value.get("itemType") != "IntegrationBinding"
        or any(
            value.get(key) != expected
            for key, expected in expected_scope.fields().items()
        )
    ):
        raise RegistryAccessDenied("Integration binding is unavailable")
    descriptor = {
        "id": value.get("bindingId"),
        "provider": value.get("provider"),
        "adapterVersion": value.get("adapterVersion"),
        "connectionId": value.get("connectionId"),
        "status": value.get("status"),
        "mode": value.get("mode"),
        "capabilities": value.get("capabilities"),
    }
    if value.get("provider") == "stripe":
        descriptor["stripe"] = value.get("providerMetadata")
    return IntegrationBinding.from_mapping(expected_scope, descriptor)


def _idempotency_key(value: object) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 256
        or any(ord(character) < 32 for character in value)
    ):
        raise RegistryAccessDenied("Idempotency key is invalid")
    return value


def _safe_id(value: object) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise RegistryAccessDenied("Integration identifier is invalid")
    return value


def _provider_readiness(value: object) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value)
        != {
            "status",
            "chargesEnabled",
            "payoutsEnabled",
            "detailsSubmitted",
            "capabilitiesReady",
            "requirementsDueCount",
        }
        or value.get("status") != "ready"
    ):
        raise RegistryAccessDenied("Provider readiness is invalid")
    sanitized = {key: value[key] for key in value if key != "status"}
    if not _readiness_is_complete(sanitized):
        raise RegistryAccessDenied("Provider readiness is invalid")
    if (
        type(sanitized["requirementsDueCount"]) is not int
        or sanitized["requirementsDueCount"] != 0
    ):
        raise RegistryAccessDenied("Provider readiness is invalid")
    return sanitized


def _readiness_is_complete(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value)
        == {
            "chargesEnabled",
            "payoutsEnabled",
            "detailsSubmitted",
            "capabilitiesReady",
            "requirementsDueCount",
        }
        and all(
            value.get(field) is True
            for field in (
                "chargesEnabled",
                "payoutsEnabled",
                "detailsSubmitted",
                "capabilitiesReady",
            )
        )
        and value.get("requirementsDueCount") == 0
    )


def _serialize(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _serialize_value(item) for key, item in value.items()}


def _serialize_values(value: Mapping[str, Any]) -> dict[str, Any]:
    return _serialize(value)


def _deserialize(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RegistryError("Integration registry is unavailable")
    return {key: _deserialize_value(item) for key, item in value.items()}


def _serialize_value(value: Any) -> dict[str, Any]:
    if type(value) is bool:
        return {"BOOL": value}
    if type(value) is str:
        return {"S": value}
    if type(value) is int:
        return {"N": str(value)}
    if value is None:
        return {"NULL": True}
    if isinstance(value, Mapping):
        return {"M": {key: _serialize_value(item) for key, item in value.items()}}
    if isinstance(value, (list, tuple)):
        return {"L": [_serialize_value(item) for item in value]}
    raise RegistryError("Integration registry value is invalid")


def _deserialize_value(value: Any) -> Any:
    if not isinstance(value, Mapping) or len(value) != 1:
        raise RegistryError("Integration registry is unavailable")
    key, item = next(iter(value.items()))
    if key == "BOOL" and type(item) is bool:
        return item
    if key == "S" and type(item) is str:
        return item
    if key == "N" and type(item) is str:
        try:
            return int(item)
        except ValueError:
            raise RegistryError("Integration registry is unavailable") from None
    if key == "NULL" and item is True:
        return None
    if key == "M" and isinstance(item, Mapping):
        return {
            nested_key: _deserialize_value(nested)
            for nested_key, nested in item.items()
        }
    if key == "L" and isinstance(item, list):
        return [_deserialize_value(nested) for nested in item]
    raise RegistryError("Integration registry is unavailable")
