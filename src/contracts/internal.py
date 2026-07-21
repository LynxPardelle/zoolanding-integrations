"""Closed, provider-ID-free commands accepted from trusted internal callers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping

try:
    from domain.integrations import IntegrationScope
except ModuleNotFoundError:
    from src.domain.integrations import IntegrationScope


_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}", re.ASCII)
_HASH = re.compile(r"[a-f0-9]{64}", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}", re.ASCII)
_COUPON_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", re.ASCII)
_STRIPE_ACCOUNT = re.compile(r"acct_[A-Za-z0-9]{8,64}", re.ASCII)
_PROVIDER_CAPABILITIES = {
    "stripe": frozenset(
        {
            "connect-onboarding",
            "checkout",
            "one-time-payments",
            "subscriptions",
            "prices",
            "coupons",
            "customer-portal",
        }
    ),
    "email.smtp": frozenset({"send"}),
}


class ContractError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class InternalCommand:
    kind: str
    scope: IntegrationScope
    connection_id: str
    command_id: str
    idempotency_key: str
    input: Any
    content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectionRegistration:
    scope: IntegrationScope
    connection_id: str
    command_id: str
    idempotency_key: str
    credential_reference: str
    provider: str
    mode: str
    capabilities: tuple[str, ...]
    account_reference: str | None


def validate_command(kind: str, value: object) -> InternalCommand:
    request = _closed(
        value,
        {
            "version",
            "scope",
            "connectionId",
            "commandId",
            "idempotencyKey",
            "input",
        },
    )
    if request["version"] != 1:
        raise ContractError("command version is invalid")
    scope = _scope(request["scope"])
    connection_id = _id(request["connectionId"])
    command_id = _id(request["commandId"])
    idempotency_key = _idempotency(request["idempotencyKey"])
    content_hash: str | None = None
    if kind in {"offer", "product-presentation", "discount", "discount-lifecycle"}:
        parsed_input, content_hash = _snapshot_input(kind, request["input"])
    else:
        parsed_input = _operation_input(kind, request["input"])
    return InternalCommand(
        kind,
        scope,
        connection_id,
        command_id,
        idempotency_key,
        parsed_input,
        content_hash,
    )


def validate_connection_registration(value: object) -> ConnectionRegistration:
    request = _closed(
        value,
        {
            "version",
            "scope",
            "connectionId",
            "commandId",
            "idempotencyKey",
            "credentialReference",
            "provider",
            "mode",
            "capabilities",
            "accountReference",
        },
    )
    if request["version"] != 1:
        raise ContractError("registration version is invalid")
    scope = _scope(request["scope"])
    connection_id = _id(request["connectionId"])
    provider = request["provider"]
    if type(provider) is not str or provider not in {"stripe", "email.smtp"}:
        raise ContractError("registration provider is invalid")
    mode = request["mode"]
    expected_mode = "test" if scope.environment == "test" else "live"
    if type(mode) is not str or mode != expected_mode:
        raise ContractError("registration mode is invalid")
    capabilities = _capabilities(request["capabilities"])
    reference = request["credentialReference"]
    expected_reference = (
        f"/zoolanding/{scope.environment}/integrations/{scope.tenant_id}/"
        f"{scope.draft_id}/stripe/{connection_id}"
        if provider == "stripe"
        else f"/zoolanding/{scope.environment}/{scope.tenant_id}/{scope.draft_id}/"
        f"notifications/smtp/{connection_id}"
    )
    if reference != expected_reference:
        raise ContractError("registration reference is invalid")
    account_reference = request["accountReference"]
    if provider == "stripe":
        if (
            type(account_reference) is not str
            or _STRIPE_ACCOUNT.fullmatch(account_reference) is None
        ):
            raise ContractError("registration account is invalid")
    elif account_reference is not None:
        raise ContractError("registration account is invalid")
    return ConnectionRegistration(
        scope,
        connection_id,
        _id(request["commandId"]),
        _idempotency(request["idempotencyKey"]),
        reference,
        provider,
        mode,
        capabilities,
        account_reference,
    )


def validate_service_result(value: object, expected_command_id: str) -> dict[str, str]:
    result = _closed(value, {"commandId", "status"})
    if result["commandId"] != expected_command_id or result["status"] not in {
        "accepted",
        "pending",
    }:
        raise ContractError("command result is invalid")
    return {"commandId": expected_command_id, "status": result["status"]}


def validate_connection_registration_result(
    value: object, expected: ConnectionRegistration
) -> dict[str, Any]:
    result = _closed(value, {"connectionId", "status", "mode", "revision"})
    if (
        result["connectionId"] != expected.connection_id
        or result["status"] != "pending"
        or result["mode"] != expected.mode
    ):
        raise ContractError("registration result is invalid")
    _positive_int(result["revision"])
    return dict(result)


def validate_connection_resolution_result(
    value: object, expected: InternalCommand
) -> dict[str, Any]:
    result = _closed(
        value,
        {
            "connectionId",
            "provider",
            "mode",
            "adapterVersion",
            "credentialReference",
        },
        {"endpoint"},
    )
    provider = result["provider"]
    if (
        expected.kind != "connection-resolve"
        or result["connectionId"] != expected.connection_id
        or provider != expected.input["provider"]
    ):
        raise ContractError("resolution result is invalid")
    expected_mode = "test" if expected.scope.environment == "test" else "live"
    if result["mode"] != expected_mode or result["adapterVersion"] != "v1":
        raise ContractError("resolution result is invalid")
    reference = result["credentialReference"]
    expected_reference = (
        f"/zoolanding/{expected.scope.environment}/integrations/"
        f"{expected.scope.tenant_id}/{expected.scope.draft_id}/stripe/"
        f"{expected.connection_id}"
        if provider == "stripe"
        else f"/zoolanding/{expected.scope.environment}/{expected.scope.tenant_id}/"
        f"{expected.scope.draft_id}/notifications/smtp/{expected.connection_id}"
    )
    if reference != expected_reference:
        raise ContractError("resolution result is invalid")
    if provider == "stripe" and "endpoint" in result:
        raise ContractError("resolution result is invalid")
    if provider == "email.smtp":
        endpoint = _closed(
            result.get("endpoint"),
            {"host", "port", "canonicalSendingDomain"},
        )
        if endpoint["host"] != "mail.smtp2go.com" or endpoint["port"] != 465:
            raise ContractError("resolution result is invalid")
        expected_domain = (
            "zoolandingpage.com.mx"
            if expected.scope.environment == "test"
            else expected.scope.domain
        )
        if endpoint["canonicalSendingDomain"] != expected_domain:
            raise ContractError("resolution result is invalid")
    return dict(result)


def _snapshot_input(kind: str, value: object) -> tuple[Any, str]:
    item = _closed(
        value,
        {"resourceId", "revision", "schemaVersion", "snapshot", "contentHash"},
    )
    _id(item["resourceId"])
    _positive_int(item["revision"])
    if item["schemaVersion"] != 1:
        raise ContractError("snapshot schema is invalid")
    snapshot = _snapshot(kind, item["snapshot"])
    content_hash = item["contentHash"]
    if type(content_hash) is not str or _HASH.fullmatch(content_hash) is None:
        raise ContractError("snapshot hash is invalid")
    encoded = json.dumps(
        {"schemaVersion": 1, "snapshot": snapshot},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    if hashlib.sha256(encoded).hexdigest() != content_hash:
        raise ContractError("snapshot hash is invalid")
    return item, content_hash


def _snapshot(kind: str, value: object) -> dict[str, Any]:
    if kind == "offer":
        item = _closed(
            value,
            {"amountMinor", "currency", "saleType", "recurrence", "taxBehavior"},
        )
        _nonnegative_int(item["amountMinor"])
        if (
            type(item["currency"]) is not str
            or _CURRENCY.fullmatch(item["currency"]) is None
        ):
            raise ContractError("offer currency is invalid")
        if type(item["saleType"]) is not str or item["saleType"] not in {
            "one_time",
            "subscription",
        }:
            raise ContractError("offer sale type is invalid")
        recurrence = item["recurrence"]
        if recurrence is not None:
            recurrence = _closed(recurrence, {"interval", "intervalCount"})
            if (
                recurrence["interval"] not in {"month", "year"}
                or recurrence["intervalCount"] != 1
            ):
                raise ContractError("offer recurrence is invalid")
        if type(item["taxBehavior"]) is not str or item["taxBehavior"] not in {
            "exclusive",
            "inclusive",
            "unspecified",
        }:
            raise ContractError("offer tax behavior is invalid")
        return item
    if kind == "product-presentation":
        item = _closed(value, {"displayName"}, {"displayDescription"})
        _plain_text(item["displayName"], 160)
        if "displayDescription" in item:
            _plain_text(item["displayDescription"], 1000)
        return item
    if kind == "discount":
        item = _closed(
            value,
            {"discountType", "duration"},
            {
                "percentageBasisPoints",
                "amountMinor",
                "currency",
                "durationInMonths",
                "eligibleOfferVersionIds",
                "redemptionLimit",
                "redeemByEpoch",
                "customerFacingCode",
            },
        )
        if type(item["discountType"]) is not str or item["discountType"] not in {
            "percentage",
            "fixed",
        }:
            raise ContractError("discount type is invalid")
        if type(item["duration"]) is not str or item["duration"] not in {
            "once",
            "forever",
            "repeating",
        }:
            raise ContractError("discount duration is invalid")
        if item["discountType"] == "percentage":
            if (
                "percentageBasisPoints" not in item
                or "amountMinor" in item
                or "currency" in item
            ):
                raise ContractError("discount value is invalid")
            basis_points = item["percentageBasisPoints"]
            if type(basis_points) is not int or not 1 <= basis_points <= 10_000:
                raise ContractError("discount value is invalid")
        else:
            if (
                "amountMinor" not in item
                or "currency" not in item
                or "percentageBasisPoints" in item
            ):
                raise ContractError("discount value is invalid")
            _positive_int(item["amountMinor"])
            if (
                type(item["currency"]) is not str
                or _CURRENCY.fullmatch(item["currency"]) is None
            ):
                raise ContractError("discount value is invalid")
        if item["duration"] == "repeating":
            _positive_int(item.get("durationInMonths"))
        elif "durationInMonths" in item:
            raise ContractError("discount duration is invalid")
        if "eligibleOfferVersionIds" in item:
            eligible = item["eligibleOfferVersionIds"]
            if (
                not isinstance(eligible, list)
                or not 1 <= len(eligible) <= 100
                or any(type(offer_id) is not str for offer_id in eligible)
                or len(set(eligible)) != len(eligible)
            ):
                raise ContractError("discount eligibility is invalid")
            for offer_id in eligible:
                _id(offer_id)
        for integer_field in ("redemptionLimit", "redeemByEpoch"):
            if integer_field in item:
                _positive_int(item[integer_field])
        if "customerFacingCode" in item and (
            type(item["customerFacingCode"]) is not str
            or _COUPON_CODE.fullmatch(item["customerFacingCode"]) is None
        ):
            raise ContractError("discount code is invalid")
        return item
    item = _closed(value, {"targetState"})
    if type(item["targetState"]) is not str or item["targetState"] not in {
        "active",
        "existing_only",
        "retired",
    }:
        raise ContractError("discount lifecycle is invalid")
    return item


def _operation_input(kind: str, value: object) -> dict[str, Any]:
    specifications: dict[str, tuple[set[str], set[str]]] = {
        "checkout": (
            {"orderId", "paymentAttemptId", "revision", "offerBindings"},
            {"discountVersionId"},
        ),
        "checkout-status": ({"orderId", "paymentAttemptId", "revision"}, set()),
        "subscription-change": (
            {"subscriptionId", "expectedRevision", "targetOfferVersionId", "proration"},
            set(),
        ),
        "subscription-discount": (
            {"subscriptionId", "expectedRevision", "discountVersionId"},
            set(),
        ),
        "subscription-pause": (
            {"subscriptionId", "expectedRevision", "action"},
            set(),
        ),
        "customer-portal": ({"subscriptionId"}, set()),
        "migration-preview": (
            {"sourceOfferVersionId", "targetOfferVersionId", "commercialRequestId"},
            set(),
        ),
        "migration-execute": (
            {"jobId", "dryRunRevision", "dryRunHash", "confirmation"},
            set(),
        ),
        "migration-control": ({"jobId", "expectedRevision", "action"}, set()),
        "migration-status": ({"jobId"}, set()),
        "connection-resolve": ({"provider", "capability"}, set()),
    }
    if kind not in specifications:
        raise ContractError("command kind is invalid")
    required, optional = specifications[kind]
    item = _closed(value, required, optional)
    _validate_operation_ids(item)
    if kind == "connection-resolve":
        provider = item["provider"]
        capability = item["capability"]
        if (
            type(provider) is not str
            or provider not in _PROVIDER_CAPABILITIES
            or type(capability) is not str
            or capability not in _PROVIDER_CAPABILITIES[provider]
        ):
            raise ContractError("connection resolution is invalid")
    if kind == "migration-execute" and item["confirmation"] is not True:
        raise ContractError("migration confirmation is invalid")
    for field in ("proration", "action"):
        if field in item and (
            type(item[field]) is not str or _SAFE_ID.fullmatch(item[field]) is None
        ):
            raise ContractError("command option is invalid")
    return item


def _validate_operation_ids(item: Mapping[str, Any]) -> None:
    for key, value in item.items():
        if key.endswith("Id") or key.endswith("VersionId"):
            _id(value)
        elif key in {"revision", "expectedRevision", "dryRunRevision"}:
            _positive_int(value)
        elif key == "dryRunHash":
            if type(value) is not str or _HASH.fullmatch(value) is None:
                raise ContractError("command hash is invalid")
    if "offerBindings" in item:
        bindings = item["offerBindings"]
        if not isinstance(bindings, list) or not 1 <= len(bindings) <= 100:
            raise ContractError("offer bindings are invalid")
        for binding in bindings:
            parsed = _closed(binding, {"offerVersionId", "revision", "quantity"})
            _id(parsed["offerVersionId"])
            _positive_int(parsed["revision"])
            _positive_int(parsed["quantity"])


def _scope(value: object) -> IntegrationScope:
    item = _closed(value, {"environment", "tenantId", "draftId", "domain"})
    try:
        return IntegrationScope(
            item["environment"], item["tenantId"], item["draftId"], item["domain"]
        )
    except (TypeError, ValueError, RecursionError):
        raise ContractError("command scope is invalid") from None


def _closed(
    value: object, required: set[str], optional: set[str] | None = None
) -> dict[str, Any]:
    optional = optional or set()
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or not set(value).issubset(required | optional)
    ):
        raise ContractError("command shape is invalid")
    return value


def _id(value: object) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise ContractError("command identifier is invalid")
    return value


def _idempotency(value: object) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 256
        or any(ord(character) < 32 for character in value)
    ):
        raise ContractError("idempotency key is invalid")
    return value


def _capabilities(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 32
        or any(type(item) is not str for item in value)
        or len(set(value)) != len(value)
        or any(
            type(item) is not str or _SAFE_ID.fullmatch(item) is None for item in value
        )
    ):
        raise ContractError("registration capabilities are invalid")
    return tuple(value)


def _positive_int(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ContractError("command integer is invalid")
    return value


def _nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ContractError("command integer is invalid")
    return value


def _plain_text(value: object, maximum: int) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= maximum
        or any(ord(character) < 32 for character in value)
        or "<" in value
        or ">" in value
    ):
        raise ContractError("presentation text is invalid")
    return value
