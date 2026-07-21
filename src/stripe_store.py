"""Conditional DynamoDB receipts and server-only Stripe resource mappings."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

try:
    from domain.integrations import IntegrationScope
    from registry import _deserialize, _serialize
    from stripe_commands import StripeCommandConflict
except ModuleNotFoundError:
    from src.domain.integrations import IntegrationScope
    from src.registry import _deserialize, _serialize
    from src.stripe_commands import StripeCommandConflict


_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}", re.ASCII)
_HASH = re.compile(r"[a-f0-9]{64}", re.ASCII)
_FORBIDDEN_KEYS = {
    "address",
    "customer",
    "customeremail",
    "email",
    "name",
    "phone",
    "redirecturl",
    "secret",
    "token",
}
_PROVIDER_OBJECT_FIELDS = {
    "product": "productId",
    "price": "priceId",
    "coupon": "couponId",
    "promotion-code": "promotionCodeId",
    "checkout-session": "sessionId",
}


class StripeStoreError(RuntimeError):
    pass


class DynamoStripeCommandStore:
    def __init__(self, table_name: str, *, client=None):
        if type(table_name) is not str or not table_name.strip():
            raise StripeStoreError("Stripe command store is unavailable")
        if client is None:
            try:
                import boto3  # type: ignore

                client = boto3.client("dynamodb")
            except Exception:
                raise StripeStoreError("Stripe command store is unavailable") from None
        self._table_name = table_name
        self._client = client

    def claim(
        self,
        scope: IntegrationScope,
        connection_id: str,
        key: str,
        request_hash: str,
        command_id: str,
        expires_at: int,
    ) -> dict[str, Any] | None:
        _identity(scope, connection_id)
        _digest(request_hash)
        _identifier(command_id)
        if type(key) is not str or not 1 <= len(key) <= 256 or type(expires_at) is not int:
            raise StripeCommandConflict("Stripe command conflicted")
        sk = _receipt_key(connection_id, key)
        existing = self._get(scope.partition_key, sk)
        if existing is not None:
            return _same_receipt(existing, request_hash)
        record = {
            "pk": scope.partition_key,
            "sk": sk,
            "itemType": "StripeCommandReceipt",
            "connectionId": connection_id,
            "requestHash": request_hash,
            "commandId": command_id,
            "status": "pending",
            "expiresAt": expires_at,
        }
        try:
            self._client.put_item(
                TableName=self._table_name,
                Item=_serialize(record),
                ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
            )
            return None
        except Exception:
            existing = self._get(scope.partition_key, sk)
            if existing is None:
                raise StripeStoreError("Stripe command store is unavailable") from None
            return _same_receipt(existing, request_hash)

    def get_mapping(
        self,
        scope: IntegrationScope,
        connection_id: str,
        resource_type: str,
        resource_id: str,
    ) -> dict[str, Any] | None:
        _identity(scope, connection_id)
        _identifier(resource_type)
        _identifier(resource_id)
        record = self._get(
            scope.partition_key,
            _mapping_key(connection_id, resource_type, resource_id),
        )
        if record is None:
            return None
        expected = {
            "pk",
            "sk",
            "itemType",
            "connectionId",
        }
        if (
            not expected.issubset(record)
            or record["itemType"] != "StripeResourceMapping"
            or record["connectionId"] != connection_id
            or record.get("resourceType") != resource_type
            or record.get("resourceId") != resource_id
        ):
            raise StripeStoreError("Stripe command store is unavailable")
        return {key: value for key, value in record.items() if key not in expected}

    def code_owner(
        self, scope: IntegrationScope, connection_id: str, code_hash: str
    ) -> str | None:
        _identity(scope, connection_id)
        _digest(code_hash)
        record = self._get(
            scope.partition_key, f"STRIPECODE#{connection_id}#{code_hash}"
        )
        if record is None:
            return None
        owner = record.get("resourceId")
        if (
            record.get("itemType") != "StripeDiscountCodeClaim"
            or record.get("connectionId") != connection_id
            or type(owner) is not str
        ):
            raise StripeStoreError("Stripe command store is unavailable")
        return owner

    def object_owner(
        self,
        scope: IntegrationScope,
        connection_id: str,
        object_type: str,
        provider_id: str,
    ) -> dict[str, Any] | None:
        _identity(scope, connection_id)
        field = _PROVIDER_OBJECT_FIELDS.get(object_type)
        if field is None:
            raise StripeCommandConflict("Stripe command conflicted")
        provider_hash = _provider_hash(provider_id)
        record = self._get(
            scope.partition_key,
            _object_key(connection_id, object_type, provider_hash),
        )
        if record is None:
            return None
        resource_type = record.get("resourceType")
        resource_id = record.get("resourceId")
        if (
            record.get("itemType") != "StripeObjectIndex"
            or record.get("connectionId") != connection_id
            or record.get("objectType") != object_type
            or record.get("providerIdHash") != provider_hash
            or type(resource_type) is not str
            or type(resource_id) is not str
        ):
            raise StripeStoreError("Stripe command store is unavailable")
        mapping = self.get_mapping(
            scope, connection_id, resource_type, resource_id
        )
        if mapping is None or mapping.get(field) != provider_id:
            raise StripeStoreError("Stripe command store is unavailable")
        return mapping

    def complete(
        self,
        scope: IntegrationScope,
        connection_id: str,
        key: str,
        request_hash: str,
        result: Mapping[str, Any],
        mappings: list[Mapping[str, Any]],
        code_claim: str | None = None,
    ) -> None:
        _identity(scope, connection_id)
        _digest(request_hash)
        if result != {"status": "accepted"} or not 0 <= len(mappings) <= 20:
            raise ValueError("Stripe command persistence is invalid")
        if code_claim is not None and not mappings:
            raise ValueError("Stripe command persistence is invalid")
        operations = []
        for mapping in mappings:
            operations.append(self._mapping_write(scope, connection_id, mapping))
            operations.extend(
                self._object_index_writes(scope, connection_id, mapping)
            )
        if code_claim is not None:
            _digest(code_claim)
            resource_id = mappings[0].get("resourceId")
            _identifier(resource_id)
            operations.append(
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": _serialize(
                            {
                                "pk": scope.partition_key,
                                "sk": f"STRIPECODE#{connection_id}#{code_claim}",
                                "itemType": "StripeDiscountCodeClaim",
                                "connectionId": connection_id,
                                "resourceId": resource_id,
                            }
                        ),
                        "ConditionExpression": (
                            "attribute_not_exists(pk) OR resourceId = :resourceId"
                        ),
                        "ExpressionAttributeValues": {
                            ":resourceId": {"S": resource_id}
                        },
                    }
                }
            )
        else:
            for mapping in mappings:
                code_hash = mapping.get("codeHash")
                if (
                    mapping.get("resourceType") != "discount"
                    or mapping.get("status") == "active"
                    or code_hash is None
                ):
                    continue
                _digest(code_hash)
                resource_id = _identifier(mapping.get("resourceId"))
                operations.append(
                    {
                        "Delete": {
                            "TableName": self._table_name,
                            "Key": _serialize(
                                {
                                    "pk": scope.partition_key,
                                    "sk": f"STRIPECODE#{connection_id}#{code_hash}",
                                }
                            ),
                            "ConditionExpression": "resourceId = :resourceId",
                            "ExpressionAttributeValues": {
                                ":resourceId": {"S": resource_id}
                            },
                        }
                    }
                )
        operations.append(
            {
                "Update": {
                    "TableName": self._table_name,
                    "Key": _serialize(
                        {
                            "pk": scope.partition_key,
                            "sk": _receipt_key(connection_id, key),
                        }
                    ),
                    "UpdateExpression": "SET #status = :accepted",
                    "ConditionExpression": (
                        "requestHash = :requestHash AND "
                        "#status IN (:pending, :unknown)"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": _serialize_values(
                        {
                            ":accepted": "accepted",
                            ":pending": "pending",
                            ":unknown": "unknown",
                            ":requestHash": request_hash,
                        }
                    ),
                }
            }
        )
        if len(operations) > 25:
            raise ValueError("Stripe command persistence is invalid")
        try:
            self._client.transact_write_items(
                TransactItems=operations,
                ClientRequestToken=hashlib.sha256(
                    (key + "\0" + request_hash).encode("utf-8")
                ).hexdigest()[:36],
            )
        except Exception:
            receipt = self._get(
                scope.partition_key, _receipt_key(connection_id, key)
            )
            if (
                receipt is not None
                and receipt.get("requestHash") == request_hash
                and receipt.get("status") == "accepted"
            ):
                return
            raise StripeCommandConflict("Stripe command conflicted") from None

    def mark_unknown(
        self,
        scope: IntegrationScope,
        connection_id: str,
        key: str,
        request_hash: str,
    ) -> None:
        _identity(scope, connection_id)
        _digest(request_hash)
        try:
            self._client.update_item(
                TableName=self._table_name,
                Key=_serialize(
                    {
                        "pk": scope.partition_key,
                        "sk": _receipt_key(connection_id, key),
                    }
                ),
                UpdateExpression="SET #status = :unknown",
                ConditionExpression="requestHash = :requestHash AND #status = :pending",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=_serialize_values(
                    {":unknown": "unknown", ":pending": "pending", ":requestHash": request_hash}
                ),
            )
        except Exception:
            receipt = self._get(scope.partition_key, _receipt_key(connection_id, key))
            if (
                receipt is not None
                and receipt.get("requestHash") == request_hash
                and receipt.get("status") in {"unknown", "accepted"}
            ):
                return
            raise StripeStoreError("Stripe command store is unavailable") from None

    def _mapping_write(self, scope, connection_id, mapping):
        _safe_mapping(mapping)
        resource_type = mapping["resourceType"]
        resource_id = mapping["resourceId"]
        sk = _mapping_key(connection_id, resource_type, resource_id)
        existing = self._get(scope.partition_key, sk)
        if existing is None:
            return {
                "Put": {
                    "TableName": self._table_name,
                    "Item": _serialize(
                        {
                            "pk": scope.partition_key,
                            "sk": sk,
                            "itemType": "StripeResourceMapping",
                            "connectionId": connection_id,
                            **dict(mapping),
                        }
                    ),
                    "ConditionExpression": "attribute_not_exists(pk) AND attribute_not_exists(sk)",
                }
            }

        plain_existing = {
            key: value
            for key, value in existing.items()
            if key not in {"pk", "sk", "itemType", "connectionId"}
        }
        dimension, fields = _advanced_dimension(plain_existing, mapping)
        names = {f"#f{index}": field for index, field in enumerate(fields)}
        values = {f":v{index}": mapping[field] for index, field in enumerate(fields)}
        expected = plain_existing.get(dimension)
        values[":expected"] = expected if expected is not None else 0
        condition = (
            f"{dimension} = :expected"
            if expected is not None
            else f"attribute_not_exists({dimension})"
        )
        return {
            "Update": {
                "TableName": self._table_name,
                "Key": _serialize({"pk": scope.partition_key, "sk": sk}),
                "UpdateExpression": "SET "
                + ", ".join(f"{alias} = :v{index}" for index, alias in enumerate(names)),
                "ConditionExpression": condition,
                "ExpressionAttributeNames": names,
                "ExpressionAttributeValues": _serialize_values(values),
            }
        }

    def _object_index_writes(self, scope, connection_id, mapping):
        operations = []
        for object_type, field in _PROVIDER_OBJECT_FIELDS.items():
            if field not in mapping:
                continue
            provider_hash = _provider_hash(mapping[field])
            resource_type = mapping["resourceType"]
            resource_id = mapping["resourceId"]
            operations.append(
                {
                    "Put": {
                        "TableName": self._table_name,
                        "Item": _serialize(
                            {
                                "pk": scope.partition_key,
                                "sk": _object_key(
                                    connection_id, object_type, provider_hash
                                ),
                                "itemType": "StripeObjectIndex",
                                "connectionId": connection_id,
                                "objectType": object_type,
                                "providerIdHash": provider_hash,
                                "resourceType": resource_type,
                                "resourceId": resource_id,
                            }
                        ),
                        "ConditionExpression": (
                            "attribute_not_exists(pk) OR "
                            "(#resourceType = :resourceType AND #resourceId = :resourceId)"
                        ),
                        "ExpressionAttributeNames": {
                            "#resourceType": "resourceType",
                            "#resourceId": "resourceId",
                        },
                        "ExpressionAttributeValues": _serialize_values(
                            {
                                ":resourceType": resource_type,
                                ":resourceId": resource_id,
                            }
                        ),
                    }
                }
            )
        return operations

    def _get(self, pk: str, sk: str) -> dict[str, Any] | None:
        try:
            response = self._client.get_item(
                TableName=self._table_name,
                Key=_serialize({"pk": pk, "sk": sk}),
                ConsistentRead=True,
            )
            item = response.get("Item")
            return None if item is None else _deserialize(item)
        except StripeStoreError:
            raise
        except Exception:
            raise StripeStoreError("Stripe command store is unavailable") from None


def _mapping_key(connection_id: str, resource_type: str, resource_id: str) -> str:
    return f"STRIPEMAP#{connection_id}#{resource_type}#{resource_id}"


def _object_key(
    connection_id: str, object_type: str, provider_hash: str
) -> str:
    return f"STRIPEOBJECT#{connection_id}#{object_type}#{provider_hash}"


def _receipt_key(connection_id: str, key: str) -> str:
    return f"STRIPECMD#{connection_id}#{hashlib.sha256(key.encode('utf-8')).hexdigest()}"


def _same_receipt(record, request_hash):
    if (
        record.get("itemType") != "StripeCommandReceipt"
        or record.get("requestHash") != request_hash
        or record.get("status") not in {"pending", "unknown", "accepted"}
    ):
        raise StripeCommandConflict("Stripe command conflicted")
    return record


def _identity(scope, connection_id):
    if type(scope) is not IntegrationScope:
        raise StripeCommandConflict("Stripe command conflicted")
    _identifier(connection_id)


def _identifier(value):
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise StripeCommandConflict("Stripe command conflicted")
    return value


def _digest(value):
    if type(value) is not str or _HASH.fullmatch(value) is None:
        raise StripeCommandConflict("Stripe command conflicted")


def _provider_hash(value):
    if (
        type(value) is not str
        or not 1 <= len(value) <= 255
        or any(ord(character) < 33 or ord(character) > 126 for character in value)
    ):
        raise ValueError("Stripe command persistence is invalid")
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _safe_mapping(value):
    if not isinstance(value, Mapping) or not {
        "resourceType",
        "resourceId",
        "revision",
    }.issubset(value):
        raise ValueError("Stripe command persistence is invalid")
    _identifier(value["resourceType"])
    _identifier(value["resourceId"])
    if type(value["revision"]) is not int or value["revision"] < 1:
        raise ValueError("Stripe command persistence is invalid")
    _reject_sensitive(value)


def _reject_sensitive(value):
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if type(key) is not str or key.casefold().replace("_", "") in _FORBIDDEN_KEYS:
                raise ValueError("Stripe command persistence is invalid")
            _reject_sensitive(nested)
    elif isinstance(value, (list, tuple)):
        for nested in value:
            _reject_sensitive(nested)


def _advanced_dimension(existing, mapping):
    dimensions = []
    for field in ("revision", "presentationRevision", "lifecycleRevision"):
        if field in mapping and mapping[field] > existing.get(field, 0):
            dimensions.append(field)
    if len(dimensions) != 1:
        raise StripeCommandConflict("Stripe command conflicted")
    dimension = dimensions[0]
    fields_by_dimension = {
        "revision": tuple(
            field
            for field in mapping
            if field not in {"presentationRevision", "presentationHash", "lifecycleRevision"}
        ),
        "presentationRevision": ("presentationRevision", "presentationHash"),
        "lifecycleRevision": ("lifecycleRevision", "status"),
    }
    fields = fields_by_dimension[dimension]
    if any(field not in mapping for field in fields):
        raise StripeCommandConflict("Stripe command conflicted")
    return dimension, fields


def _serialize_values(values):
    return {key: next(iter(_serialize({"value": value}).values())) for key, value in values.items()}
