"""Closed, provider-ID-free commands accepted from trusted internal callers."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import urlsplit

try:
    from domain.integrations import IntegrationScope
except ModuleNotFoundError:
    from src.domain.integrations import IntegrationScope


_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}", re.ASCII)
_HASH = re.compile(r"[a-f0-9]{64}", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}", re.ASCII)
_COUPON_CODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", re.ASCII)
_COUNTRY = re.compile(r"[A-Z]{2}", re.ASCII)
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
_DERIVED_IDEMPOTENCY_KINDS = frozenset(
    {
        "offer",
        "product-presentation",
        "discount",
        "discount-lifecycle",
        "checkout",
        "subscription-change",
        "subscription-discount",
        "subscription-pause",
    }
)


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
        content_hash = _canonical_hash(parsed_input)
    if kind in _DERIVED_IDEMPOTENCY_KINDS:
        operation, resource_id, revision = _command_identity(kind, parsed_input)
        if request["idempotencyKey"] != derive_command_idempotency_key(
            scope,
            connection_id,
            operation,
            resource_id,
            revision,
            content_hash,
        ):
            raise ContractError("idempotency key is invalid")
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
        else f"/zoolanding/{scope.environment}/integrations/{scope.tenant_id}/"
        f"{scope.draft_id}/smtp/{connection_id}"
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


def validate_service_result(
    value: object, expected: InternalCommand | str
) -> dict[str, Any]:
    command = expected if type(expected) is InternalCommand else None
    expected_command_id = command.command_id if command is not None else expected
    if command is not None and command.kind == "checkout-status":
        result = _closed(
            value, {"orderId", "paymentAttemptId", "revision", "status"}
        )
        if (
            result["orderId"] != command.input["orderId"]
            or result["paymentAttemptId"] != command.input["paymentAttemptId"]
            or result["revision"] != command.input["revision"]
            or result["status"]
            not in {"not_created", "pending", "paid", "terminal_unpaid", "unknown"}
        ):
            raise ContractError("command result is invalid")
        return dict(result)

    if command is not None and command.kind in {"checkout", "customer-portal"}:
        if isinstance(value, dict) and set(value) == {
            "commandId",
            "status",
            "redirectUrl",
            "expiresAt",
        }:
            result = value
            if (
                result["commandId"] != expected_command_id
                or result["status"] != "accepted"
                or not _provider_redirect(result["redirectUrl"], command.kind)
            ):
                raise ContractError("command result is invalid")
            _positive_int(result["expiresAt"])
            return dict(result)

    result = _closed(value, {"commandId", "status"})
    if result["commandId"] != expected_command_id or result["status"] not in {
        "accepted",
        "pending",
        "needs_review",
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
        else f"/zoolanding/{expected.scope.environment}/integrations/"
        f"{expected.scope.tenant_id}/{expected.scope.draft_id}/smtp/"
        f"{expected.connection_id}"
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
    required = {"resourceId", "revision", "schemaVersion", "snapshot", "contentHash"}
    if kind == "offer":
        required.add("operation")
    item = _closed(
        value,
        required,
    )
    _id(item["resourceId"])
    _positive_int(item["revision"])
    if item["schemaVersion"] != 1:
        raise ContractError("snapshot schema is invalid")
    snapshot_kind = kind
    if kind == "offer":
        operation = item["operation"]
        if type(operation) is not str or operation not in {"provision", "deactivate"}:
            raise ContractError("offer operation is invalid")
        snapshot_kind = "offer" if operation == "provision" else "offer-lifecycle"
    snapshot = _snapshot(snapshot_kind, item["snapshot"])
    content_hash = item["contentHash"]
    if type(content_hash) is not str or _HASH.fullmatch(content_hash) is None:
        raise ContractError("snapshot hash is invalid")
    if _canonical_hash({"schemaVersion": 1, "snapshot": snapshot}) != content_hash:
        raise ContractError("snapshot hash is invalid")
    return item, content_hash


def _snapshot(kind: str, value: object) -> dict[str, Any]:
    if kind == "offer":
        item = _closed(
            value,
            {
                "schemaVersion",
                "amountMinor",
                "billingScheme",
                "currency",
                "saleType",
                "recurrence",
                "taxBehavior",
            },
        )
        if item["schemaVersion"] != 1 or item["billingScheme"] != "per_unit":
            raise ContractError("offer billing is invalid")
        _nonnegative_int(item["amountMinor"])
        if (
            type(item["currency"]) is not str
            or _CURRENCY.fullmatch(item["currency"]) is None
        ):
            raise ContractError("offer currency is invalid")
        if type(item["saleType"]) is not str or item["saleType"] not in {
            "one_time", "recurring"
        }:
            raise ContractError("offer sale type is invalid")
        recurrence = item["recurrence"]
        if item["saleType"] == "recurring":
            recurrence = _closed(
                recurrence, {"interval", "intervalCount", "usageType"}
            )
            if (
                recurrence["interval"] not in {"month", "year"}
                or recurrence["intervalCount"] != 1
                or recurrence["usageType"] != "licensed"
            ):
                raise ContractError("offer recurrence is invalid")
        elif recurrence is not None:
            raise ContractError("offer recurrence is invalid")
        if type(item["taxBehavior"]) is not str or item["taxBehavior"] not in {
            "exclusive",
            "inclusive",
            "unspecified",
        }:
            raise ContractError("offer tax behavior is invalid")
        return item
    if kind == "offer-lifecycle":
        item = _closed(value, {"targetState"})
        if item["targetState"] != "retired":
            raise ContractError("offer lifecycle is invalid")
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
            {
                "schemaVersion",
                "customerFacingCode",
                "duration",
                "durationInMonths",
                "eligibleOfferVersionIds",
                "redeemByEpoch",
                "redemptionLimit",
                "value",
            },
        )
        if item["schemaVersion"] != 1:
            raise ContractError("discount schema is invalid")
        if type(item["duration"]) is not str or item["duration"] not in {
            "once",
            "forever",
            "repeating",
        }:
            raise ContractError("discount duration is invalid")
        discount_value = item["value"]
        if isinstance(discount_value, dict) and discount_value.get("type") == "percentage":
            discount_value = _closed(discount_value, {"type", "basisPoints"})
            basis_points = discount_value["basisPoints"]
            if type(basis_points) is not int or not 1 <= basis_points <= 10_000:
                raise ContractError("discount value is invalid")
        else:
            discount_value = _closed(
                discount_value, {"type", "amountMinor", "currency"}
            )
            if discount_value["type"] != "fixed_amount":
                raise ContractError("discount value is invalid")
            _positive_int(discount_value["amountMinor"])
            if (
                type(discount_value["currency"]) is not str
                or _CURRENCY.fullmatch(discount_value["currency"]) is None
            ):
                raise ContractError("discount value is invalid")
        if item["duration"] == "repeating":
            _positive_int(item["durationInMonths"])
        elif item["durationInMonths"] is not None:
            raise ContractError("discount duration is invalid")
        eligible = item["eligibleOfferVersionIds"]
        if (
            not isinstance(eligible, list)
            or len(eligible) > 200
            or any(type(offer_id) is not str for offer_id in eligible)
            or eligible != sorted(set(eligible))
        ):
            raise ContractError("discount eligibility is invalid")
        for offer_id in eligible:
            _id(offer_id)
        for integer_field in ("redemptionLimit", "redeemByEpoch"):
            if item[integer_field] is not None:
                _positive_int(item[integer_field])
        code = item["customerFacingCode"]
        if code is not None and (
            type(code) is not str or _COUPON_CODE.fullmatch(code) is None
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
            {
                "orderId",
                "paymentAttemptId",
                "revision",
                "reservationIds",
                "checkoutExpiresAt",
                "offerBindings",
                "taxPolicy",
                "shippingPolicy",
                "paymentCollection",
            },
            {"discountVersionId"},
        ),
        "checkout-status": ({"orderId", "paymentAttemptId", "revision"}, set()),
        "subscription-change": (
            {
                "subscriptionId",
                "expectedRevision",
                "targetOfferVersionId",
                "planChangePolicy",
            },
            {"previewTimestamp"},
        ),
        "subscription-discount": (
            {"subscriptionId", "expectedRevision", "action"},
            {"discountVersionId"},
        ),
        "subscription-pause": (
            {"subscriptionId", "expectedRevision", "action", "pausePolicy"},
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
    if kind == "checkout":
        _validate_checkout(item)
    elif kind == "subscription-change":
        policy = _closed(item["planChangePolicy"], {"mode"})
        if policy["mode"] not in {
            "disabled",
            "next-renewal",
            "immediate-prorated",
        }:
            raise ContractError("plan change policy is invalid")
        if policy["mode"] == "immediate-prorated":
            _positive_int(item.get("previewTimestamp"))
        elif "previewTimestamp" in item:
            raise ContractError("plan change policy is invalid")
    elif kind == "subscription-discount":
        action = item["action"]
        if action not in {"apply", "remove"}:
            raise ContractError("subscription discount action is invalid")
        if (action == "apply") != ("discountVersionId" in item):
            raise ContractError("subscription discount action is invalid")
    elif kind == "subscription-pause":
        if item["action"] not in {"pause", "resume"}:
            raise ContractError("subscription pause action is invalid")
        _pause_policy(item["pausePolicy"])
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
    for field in ("action",):
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
        if not isinstance(bindings, list) or not 1 <= len(bindings) <= 20:
            raise ContractError("offer bindings are invalid")
        for binding in bindings:
            parsed = _closed(
                binding,
                {
                    "offerVersionId",
                    "revision",
                    "quantity",
                    "sellableType",
                    "snapshot",
                    "contentHash",
                },
            )
            _id(parsed["offerVersionId"])
            _positive_int(parsed["revision"])
            _positive_int(parsed["quantity"])
            if parsed["quantity"] > 1_000_000:
                raise ContractError("offer bindings are invalid")
            if parsed["sellableType"] not in {
                "physical",
                "service",
                "subscription",
                "add_on",
            }:
                raise ContractError("offer bindings are invalid")
            snapshot = _snapshot("offer", parsed["snapshot"])
            content_hash = parsed["contentHash"]
            if (
                type(content_hash) is not str
                or _HASH.fullmatch(content_hash) is None
                or _canonical_hash({"schemaVersion": 1, "snapshot": snapshot})
                != content_hash
            ):
                raise ContractError("offer bindings are invalid")


def _validate_checkout(item: Mapping[str, Any]) -> None:
    reservations = item["reservationIds"]
    if (
        not isinstance(reservations, list)
        or len(reservations) != 1
        or len(set(reservations)) != len(reservations)
    ):
        raise ContractError("checkout reservations are invalid")
    for reservation_id in reservations:
        _id(reservation_id)
    _positive_int(item["checkoutExpiresAt"])
    tax = _closed(item["taxPolicy"], {"mode"})
    if tax["mode"] not in {"disabled", "automatic"}:
        raise ContractError("checkout tax policy is invalid")
    shipping = item["shippingPolicy"]
    if isinstance(shipping, dict) and shipping.get("collection") == "none":
        _closed(shipping, {"collection"})
    else:
        shipping = _closed(shipping, {"collection", "allowedCountries"})
        countries = shipping["allowedCountries"]
        if (
            shipping["collection"] != "required"
            or not isinstance(countries, list)
            or not 1 <= len(countries) <= 50
            or len(set(countries)) != len(countries)
            or any(
                type(country) is not str or _COUNTRY.fullmatch(country) is None
                for country in countries
            )
        ):
            raise ContractError("checkout shipping policy is invalid")
    if item["paymentCollection"] != "immediate_card_link":
        raise ContractError("checkout payment policy is invalid")

    sellable_types = {line["sellableType"] for line in item["offerBindings"]}
    sale_types = {line["snapshot"]["saleType"] for line in item["offerBindings"]}
    offer_ids = [line["offerVersionId"] for line in item["offerBindings"]]
    if (
        len(set(offer_ids)) != len(offer_ids)
        or ("recurring" in sale_types and len(offer_ids) != 1)
        or ("physical" in sellable_types and "recurring" in sale_types)
        or ("physical" in sellable_types and "subscription" in sellable_types)
        or ("physical" in sellable_types and shipping["collection"] != "required")
        or ("physical" not in sellable_types and shipping["collection"] != "none")
        or ("recurring" in sale_types and "one_time" in sale_types)
        or any(
            line["sellableType"] == "subscription"
            and line["snapshot"]["saleType"] != "recurring"
            for line in item["offerBindings"]
        )
    ):
        raise ContractError("checkout cart is invalid")


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def derive_command_idempotency_key(
    scope: IntegrationScope,
    connection_id: str,
    operation: str,
    resource_id: str,
    revision: int,
    content_hash: str | None,
) -> str:
    if (
        type(scope) is not IntegrationScope
        or type(connection_id) is not str
        or type(operation) is not str
        or type(resource_id) is not str
        or type(revision) is not int
        or type(content_hash) is not str
    ):
        raise ContractError("idempotency key is invalid")
    digest = _canonical_hash(
        {
            "scope": scope.fields(),
            "connectionId": connection_id,
            "operation": operation,
            "resourceId": resource_id,
            "revision": revision,
            "contentHash": content_hash,
        }
    )
    return f"integrations-command-v1:{digest}"


def _command_identity(kind: str, item: Mapping[str, Any]) -> tuple[str, str, int]:
    operation = kind
    if kind == "offer":
        operation = item["operation"]
    elif kind == "discount-lifecycle":
        operation = item["snapshot"]["targetState"]
    elif kind in {"subscription-discount", "subscription-pause"}:
        operation = item["action"]
    resource_id = (
        item.get("resourceId")
        or item.get("paymentAttemptId")
        or item.get("subscriptionId")
    )
    revision = item.get("revision") or item.get("expectedRevision")
    if type(resource_id) is not str or type(revision) is not int:
        raise ContractError("idempotency key is invalid")
    return operation, resource_id, revision


def _pause_policy(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
        raise ContractError("pause policy is invalid")
    if value["enabled"] is False:
        return _closed(value, {"enabled"})
    item = _closed(
        value,
        {
            "enabled",
            "newInvoiceBehavior",
            "existingInvoiceBehavior",
            "accessBehavior",
            "resume",
            "onResume",
        },
    )
    if (
        item["newInvoiceBehavior"]
        not in {"void", "keep-as-draft", "mark-uncollectible"}
        or item["existingInvoiceBehavior"] != "unchanged"
        or item["accessBehavior"] not in {"retain", "suspend"}
        or _closed(item["resume"], {"mode"})["mode"] != "manual"
        or _closed(item["onResume"], {"collection", "access"})
        != {"collection": "restore", "access": "restore-if-suspended"}
    ):
        raise ContractError("pause policy is invalid")
    return item


def _provider_redirect(value: object, kind: str) -> bool:
    if type(value) is not str or not 1 <= len(value) <= 2048:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    expected_host = "checkout.stripe.com" if kind == "checkout" else "billing.stripe.com"
    return (
        parsed.scheme == "https"
        and parsed.hostname == expected_host
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path.startswith("/")
    )


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
