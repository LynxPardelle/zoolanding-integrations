"""Provider-neutral bulk subscription migration invariants and orchestration."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

try:
    from contracts.internal import InternalCommand
except ModuleNotFoundError:
    from src.contracts.internal import InternalCommand


_HASH = re.compile(r"[a-f0-9]{64}", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}", re.ASCII)
_SAFE_METADATA_VALUE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,127}", re.ASCII)
_PLATFORM_METADATA_KEYS = frozenset(
    {"order_id", "payment_attempt_id", "revision", "resource_id"}
)
_PROVIDER_VALUE = re.compile(r"[\x21-\x7e]{1,255}", re.ASCII)
_MAX_SNAPSHOT_BYTES = 300 * 1024


class MigrationError(RuntimeError):
    pass


class MigrationConflict(MigrationError):
    pass


class MigrationNeedsReview(MigrationError):
    pass


class SubscriptionMigrationStatusService:
    """Read-only, scope-authorized migration status boundary."""

    def __init__(self, resolver: Any, store: Any):
        if resolver is None or store is None:
            raise MigrationError("migration service is unavailable")
        self._resolver = resolver
        self._store = store

    def execute(self, kind: str, command: InternalCommand) -> dict[str, Any]:
        if (
            kind != "migration-status"
            or type(command) is not InternalCommand
            or command.kind != kind
        ):
            raise MigrationConflict("migration command conflicted")
        self._resolver.resolve(
            command.scope,
            command.connection_id,
            provider="stripe",
            capability="subscriptions",
        )
        value = command.input
        result = self._store.status(
            scope=command.scope,
            connectionId=command.connection_id,
            jobId=value["jobId"],
            commercialRequestId=value["commercialRequestId"],
            limit=value.get("limit", 25),
            cursor=value.get("cursor"),
        )
        if not isinstance(result, Mapping):
            raise MigrationConflict("migration command conflicted")
        return dict(result)


class SubscriptionMigrationService:
    """Internal command boundary; provider work remains asynchronous."""

    def __init__(
        self,
        resolver: Any,
        mappings: Any,
        store: Any,
        queue: Any,
        *,
        tax_verifier: Any,
        now_epoch: Any,
    ):
        if any(
            value is None
            for value in (resolver, mappings, store, queue, tax_verifier, now_epoch)
        ):
            raise MigrationError("migration service is unavailable")
        self._resolver = resolver
        self._mappings = mappings
        self._store = store
        self._queue = queue
        self._tax_verifier = tax_verifier
        self._now_epoch = now_epoch

    def execute(self, kind: str, command: InternalCommand) -> dict[str, Any]:
        if (
            type(command) is not InternalCommand
            or command.kind != kind
            or kind
            not in {
                "migration-preview",
                "migration-execute",
                "migration-control",
                "migration-status",
            }
        ):
            raise MigrationError("migration command is unavailable")
        resolved = self._resolver.resolve(
            command.scope,
            command.connection_id,
            provider="stripe",
            capability="subscriptions",
        )
        if kind == "migration-status":
            return self._status(command)
        if kind == "migration-preview":
            return self._preview(command)
        if kind == "migration-execute":
            return self._execute(command, resolved)
        return self._control(command)

    def _preview(self, command: InternalCommand) -> dict[str, Any]:
        value = command.input
        source_mapping = self._exact_offer_mapping(
            command, value["sourceOffer"], target=False
        )
        target_mapping = self._exact_offer_mapping(
            command, value["targetOffer"], target=True
        )
        if source_mapping["priceId"] == target_mapping["priceId"]:
            raise MigrationConflict("migration command conflicted")
        now_epoch = self._server_time()
        job_id = _job_id(command)
        request_hash = _migration_request_hash(command)
        job, _ = self._store.create_preview(
            scope=command.scope,
            connectionId=command.connection_id,
            jobId=job_id,
            commercialRequestId=value["commercialRequestId"],
            sourceOffer=copy.deepcopy(value["sourceOffer"]),
            targetOffer=copy.deepcopy(value["targetOffer"]),
            sourcePriceId=source_mapping["priceId"],
            targetPriceId=target_mapping["priceId"],
            requestedPolicy=copy.deepcopy(value["requestedPolicy"]),
            candidateScope=copy.deepcopy(value["candidateScope"]),
            canarySize=value["canarySize"],
            accountConcurrency=value["accountConcurrency"],
            idempotencyKeyHash=_key_hash(command.idempotency_key),
            requestHash=request_hash,
            commandId=command.command_id,
            createdAt=now_epoch,
        )
        status = self._enqueue(command, job, "preview")
        return {
            "commandId": command.command_id,
            "status": status,
            "jobId": job_id,
            "revision": job["revision"],
        }

    def _execute(self, command: InternalCommand, resolved: Any) -> dict[str, Any]:
        value = command.input
        job = self._store.get_job(
            scope=command.scope,
            connectionId=command.connection_id,
            jobId=value["jobId"],
            commercialRequestId=value["commercialRequestId"],
        )
        if not isinstance(job, Mapping):
            return self._job_result(command, value["jobId"], 1, "needs_review")
        target = job.get("targetOffer")
        if not isinstance(target, Mapping) or target.get("revision") is None:
            return self._job_result(
                command, value["jobId"], job.get("revision", 1), "needs_review"
            )
        try:
            authorization = self._tax_verifier.authorize(
                resolved, target["revision"]
            )
        except Exception:
            authorization = None
        if authorization is None:
            return self._job_result(
                command, value["jobId"], job.get("revision", 1), "needs_review"
            )
        updated = self._store.schedule_execution(
            scope=command.scope,
            connectionId=command.connection_id,
            jobId=value["jobId"],
            commercialRequestId=value["commercialRequestId"],
            dryRunRevision=value["dryRunRevision"],
            dryRunHash=value["dryRunHash"],
            taxAuthorization=authorization,
            idempotencyKeyHash=_key_hash(command.idempotency_key),
            requestHash=_migration_request_hash(command),
            commandId=command.command_id,
            nowEpoch=self._server_time(),
        )
        if not isinstance(updated, Mapping):
            return self._job_result(
                command, value["jobId"], job.get("revision", 1), "needs_review"
            )
        status = self._enqueue(command, updated, "execute")
        return self._job_result(
            command, value["jobId"], updated["revision"], status
        )

    def _control(self, command: InternalCommand) -> dict[str, Any]:
        value = command.input
        updated = self._store.control(
            scope=command.scope,
            connectionId=command.connection_id,
            jobId=value["jobId"],
            commercialRequestId=value["commercialRequestId"],
            expectedRevision=value["expectedRevision"],
            action=value["action"],
            idempotencyKeyHash=_key_hash(command.idempotency_key),
            requestHash=_migration_request_hash(command),
            commandId=command.command_id,
            nowEpoch=self._server_time(),
        )
        status = self._enqueue(command, updated, "control")
        return self._job_result(
            command, value["jobId"], updated["revision"], status
        )

    def _status(self, command: InternalCommand) -> dict[str, Any]:
        value = command.input
        result = self._store.status(
            scope=command.scope,
            connectionId=command.connection_id,
            jobId=value["jobId"],
            commercialRequestId=value["commercialRequestId"],
            limit=value.get("limit", 25),
            cursor=value.get("cursor"),
        )
        if not isinstance(result, Mapping):
            raise MigrationConflict("migration command conflicted")
        return dict(result)

    def _exact_offer_mapping(
        self, command: InternalCommand, offer: Mapping[str, Any], *, target: bool
    ) -> dict[str, Any]:
        mapping = self._mappings.get_mapping(
            command.scope,
            command.connection_id,
            "offer",
            offer["offerVersionId"],
        )
        allowed_statuses = {"active", "existing_only"}
        if (
            not isinstance(mapping, Mapping)
            or mapping.get("resourceType") != "offer"
            or mapping.get("resourceId") != offer["offerVersionId"]
            or mapping.get("revision") != offer["revision"]
            or mapping.get("contentHash") != offer["contentHash"]
            or mapping.get("status") not in allowed_statuses
            or type(mapping.get("priceId")) is not str
        ):
            raise MigrationConflict("migration command conflicted")
        _provider_value(mapping["priceId"])
        return dict(mapping)

    def _enqueue(
        self, command: InternalCommand, job: Mapping[str, Any], action: str
    ) -> str:
        message = {
            "version": 1,
            **command.scope.fields(),
            "connectionId": command.connection_id,
            "jobId": job["jobId"],
            "action": action,
            "revision": job["revision"],
        }
        try:
            self._queue.send(message)
        except Exception:
            return "pending"
        return "accepted"

    def _server_time(self) -> int:
        try:
            value = self._now_epoch()
        except Exception:
            raise MigrationError("migration service is unavailable") from None
        if type(value) is not int or not 1 <= value <= 9_999_999_999:
            raise MigrationError("migration service is unavailable")
        return value

    @staticmethod
    def _job_result(
        command: InternalCommand, job_id: str, revision: int, status: str
    ) -> dict[str, Any]:
        return {
            "commandId": command.command_id,
            "status": status,
            "jobId": job_id,
            "revision": revision,
        }


def canonical_migration_snapshot(value: object) -> dict[str, Any]:
    """Validate the exact non-PII provider state used by one migration item."""

    item = _closed(
        value,
        {
            "subscriptionId",
            "providerRevision",
            "status",
            "currency",
            "currentPeriodStart",
            "currentPeriodEnd",
            "collectionMethod",
            "defaultPaymentMethodId",
            "defaultPaymentMethodType",
            "items",
            "discountIds",
            "automaticTax",
            "billingThresholds",
            "defaultTaxRateIds",
            "invoiceSettings",
            "metadata",
            "schedule",
            "pendingUpdate",
            "latestInvoice",
            "pendingInvoiceItemCount",
        },
        "unsupported-schedule",
    )
    _provider_value(item["subscriptionId"])
    _digest(item["providerRevision"])
    if item["status"] not in {"active", "trialing", "past_due"}:
        _review("source-drift")
    if type(item["currency"]) is not str or _CURRENCY.fullmatch(item["currency"]) is None:
        _review("source-drift")
    _positive_epoch(item["currentPeriodStart"])
    _positive_epoch(item["currentPeriodEnd"])
    if item["currentPeriodStart"] >= item["currentPeriodEnd"]:
        _review("source-drift")
    if item["collectionMethod"] not in {"charge_automatically", "send_invoice"}:
        _review("unsupported-collection-mode")
    payment_method = item["defaultPaymentMethodType"]
    if item["defaultPaymentMethodId"] is not None:
        _provider_value(item["defaultPaymentMethodId"])
    if payment_method is not None:
        _safe_token(payment_method)
    item["items"] = _subscription_items(item["items"])
    item["discountIds"] = _provider_values(item["discountIds"], 20)
    item["automaticTax"] = _automatic_tax(item["automaticTax"])
    item["billingThresholds"] = _subscription_billing_thresholds(
        item["billingThresholds"]
    )
    item["defaultTaxRateIds"] = _provider_values(item["defaultTaxRateIds"], 20)
    item["invoiceSettings"] = _invoice_settings(item["invoiceSettings"])
    _validate_collection_invoice_settings(
        item["collectionMethod"], item["invoiceSettings"]
    )
    item["metadata"] = _safe_metadata(item["metadata"])
    item["schedule"] = _schedule(item["schedule"])
    item["pendingUpdate"] = _pending_update(item["pendingUpdate"])
    item["latestInvoice"] = _latest_invoice(item["latestInvoice"])
    if (
        type(item["pendingInvoiceItemCount"]) is not int
        or not 0 <= item["pendingInvoiceItemCount"] <= 1_000_000
    ):
        _review("pending-invoice-items")
    try:
        encoded = json.dumps(
            item,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, UnicodeError, ValueError):
        _review("source-drift")
    if len(encoded) > _MAX_SNAPSHOT_BYTES:
        _review("snapshot-too-large")
    return copy.deepcopy(item)


def migration_snapshot_hash(value: object) -> str:
    selected = canonical_migration_snapshot(value)
    return hashlib.sha256(
        json.dumps(
            selected,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()


def build_next_renewal_plan(
    snapshot: object, source_price_id: str, target_price_id: str
) -> dict[str, Any]:
    """Create a lossless Schedule reconstruction without a cancel operation."""

    selected = canonical_migration_snapshot(snapshot)
    _provider_value(source_price_id)
    _provider_value(target_price_id)
    if source_price_id == target_price_id:
        _review("source-drift")
    _selected_subscription_item(selected["items"], source_price_id)
    _reject_existing_target(selected["items"], target_price_id)
    if selected["pendingUpdate"] is not None:
        _review("pending-update")

    schedule = selected["schedule"]
    if schedule is None:
        current = _phase_from_subscription(selected)
        future = copy.deepcopy(current)
        future["startDate"] = selected["currentPeriodEnd"]
        future.pop("endDate", None)
        current["endDate"] = selected["currentPeriodEnd"]
        _replace_phase_price(future, source_price_id, target_price_id)
        phases = [current, future]
        schedule_id = None
        default_settings = None
    else:
        if schedule["status"] not in {"active", "not_started"}:
            _review("unsupported-schedule")
        if schedule["endBehavior"] != "release":
            _review("unsupported-schedule")
        phases = copy.deepcopy(schedule["phases"][schedule["currentPhaseIndex"] :])
        if not 1 <= len(phases) <= 10:
            _review("phase-limit")
        current_matches = _phase_price_count(phases[0], source_price_id)
        if current_matches != 1:
            _review("ambiguous-price" if current_matches > 1 else "source-drift")
        if len(phases) == 1:
            future = copy.deepcopy(phases[0])
            if "endDate" not in phases[0]:
                _review("unsupported-schedule")
            future["startDate"] = phases[0]["endDate"]
            future.pop("endDate", None)
            phases.append(future)
        for phase in phases[1:]:
            _reject_existing_target(phase["items"], target_price_id)
            _replace_phase_price(phase, source_price_id, target_price_id)
        schedule_id = schedule["scheduleId"]
        default_settings = copy.deepcopy(schedule["defaultSettings"])

    if len(phases) > 10:
        _review("phase-limit")
    for phase in phases:
        for line in phase["items"]:
            if "itemId" in line:
                _review("unsupported-schedule")
    return {
        "scheduleId": schedule_id,
        "defaultSettings": default_settings,
        "endBehavior": "release",
        "prorationBehavior": "none",
        "phases": phases,
    }


def validate_migration_offer_compatibility(
    snapshot: object,
    source_price_id: object,
    source_offer: object,
    target_offer: object,
) -> None:
    """Bind provider Price cadence/currency to immutable draft offer snapshots."""

    selected = canonical_migration_snapshot(snapshot)
    _provider_value(source_price_id)
    source = _offer_terms(source_offer)
    target = _offer_terms(target_offer)
    if source != target or selected["currency"] != source["currency"]:
        _review("source-drift")
    expected_price = {
        "currency": source["currency"],
        "recurring": copy.deepcopy(source["recurrence"]),
    }
    current = _selected_subscription_item(selected["items"], source_price_id)
    if current["priceConfiguration"] != expected_price:
        _review("source-drift")
    schedule = selected["schedule"]
    if schedule is not None:
        for phase in schedule["phases"][schedule["currentPhaseIndex"] :]:
            for line in phase["items"]:
                if (
                    line["priceId"] == source_price_id
                    and line["priceConfiguration"] != expected_price
                ):
                    _review("source-drift")


def build_immediate_plan(
    snapshot: object,
    source_price_id: str,
    target_price_id: str,
    *,
    proration_timestamp: int,
    preview_amount_minor: int,
) -> dict[str, Any]:
    """Build the exact pending-update mutation after strict eligibility checks."""

    selected = canonical_migration_snapshot(snapshot)
    _provider_value(source_price_id)
    _provider_value(target_price_id)
    if selected["collectionMethod"] != "charge_automatically":
        _review("unsupported-collection-mode")
    if selected["defaultPaymentMethodType"] not in {"card", "link"}:
        _review("unsupported-payment-method")
    latest = selected["latestInvoice"]
    if latest is None or latest["status"] != "paid" or latest["paymentStatus"] not in {
        "paid",
        "succeeded",
    }:
        _review("unpaid-invoice")
    if selected["pendingInvoiceItemCount"] != 0:
        _review("pending-invoice-items")
    if selected["pendingUpdate"] is not None:
        _review("pending-update")
    if selected["schedule"] is not None:
        _review("near-term-schedule")
    if type(preview_amount_minor) is not int or preview_amount_minor <= 0:
        _review("nonpositive-proration")
    if (
        type(proration_timestamp) is not int
        or proration_timestamp < 1
        or proration_timestamp >= selected["currentPeriodEnd"]
    ):
        _review("source-drift")
    target = _selected_subscription_item(selected["items"], source_price_id)
    _reject_existing_target(selected["items"], target_price_id)
    if source_price_id == target_price_id:
        _review("source-drift")
    return {
        "subscriptionId": selected["subscriptionId"],
        "itemId": target["itemId"],
        "priceId": target_price_id,
        "quantity": target["quantity"],
        "prorationTimestamp": proration_timestamp,
        "prorationBehavior": "always_invoice",
        "paymentBehavior": "pending_if_incomplete",
    }


def _subscription_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        _review("source-drift")
    result = []
    identities = set()
    for value_item in value:
        selected = _closed(
            value_item,
            {
                "itemId",
                "priceId",
                "quantity",
                "taxRateIds",
                "billingThresholds",
                "discountIds",
                "metadata",
                "priceConfiguration",
            },
            "source-drift",
        )
        _provider_value(selected["itemId"])
        _provider_value(selected["priceId"])
        if selected["itemId"] in identities:
            _review("source-drift")
        identities.add(selected["itemId"])
        selected["quantity"] = _quantity(selected["quantity"])
        selected["taxRateIds"] = _provider_values(selected["taxRateIds"], 20)
        selected["billingThresholds"] = _billing_thresholds(
            selected["billingThresholds"]
        )
        selected["discountIds"] = _provider_values(selected["discountIds"], 20)
        selected["metadata"] = _safe_metadata(selected["metadata"])
        selected["priceConfiguration"] = _price_configuration(
            selected["priceConfiguration"]
        )
        result.append(dict(selected))
    return result


def _phase_items(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        _review("unsupported-schedule")
    result = []
    for value_item in value:
        selected = _closed(
            value_item,
            {
                "priceId",
                "quantity",
                "taxRateIds",
                "billingThresholds",
                "discountIds",
                "metadata",
                "priceConfiguration",
            },
            "unsupported-schedule",
        )
        _provider_value(selected["priceId"])
        selected["quantity"] = _quantity(selected["quantity"])
        selected["taxRateIds"] = _provider_values(selected["taxRateIds"], 20)
        selected["billingThresholds"] = _billing_thresholds(
            selected["billingThresholds"]
        )
        selected["discountIds"] = _provider_values(selected["discountIds"], 20)
        selected["metadata"] = _safe_metadata(selected["metadata"])
        selected["priceConfiguration"] = _price_configuration(
            selected["priceConfiguration"]
        )
        result.append(dict(selected))
    return result


def _schedule(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    selected = _closed(
        value,
        {
            "scheduleId",
            "status",
            "endBehavior",
            "currentPhaseIndex",
            "defaultSettings",
            "phases",
        },
        "unsupported-schedule",
    )
    _provider_value(selected["scheduleId"])
    if selected["status"] not in {"active", "not_started"}:
        _review("unsupported-schedule")
    if selected["endBehavior"] not in {"release", "cancel"}:
        _review("unsupported-schedule")
    phases = selected["phases"]
    if not isinstance(phases, list) or not 1 <= len(phases) <= 20:
        _review("unsupported-schedule")
    selected["phases"] = [_phase(phase) for phase in phases]
    selected["defaultSettings"] = _default_settings(selected["defaultSettings"])
    current = selected["currentPhaseIndex"]
    if type(current) is not int or not 0 <= current < len(phases):
        _review("unsupported-schedule")
    return dict(selected)


def _phase(value: object) -> dict[str, Any]:
    selected = _closed(
        value,
        {
            "startDate",
            "items",
            "discountIds",
            "automaticTax",
            "billingThresholds",
            "defaultTaxRateIds",
            "collectionMethod",
            "defaultPaymentMethodId",
            "invoiceSettings",
            "metadata",
            "prorationBehavior",
        },
        "unsupported-schedule",
        {"endDate", "duration"},
    )
    _positive_epoch(selected["startDate"])
    if ("endDate" in selected) == ("duration" in selected):
        _review("unsupported-schedule")
    if "endDate" in selected:
        _positive_epoch(selected["endDate"])
        if selected["endDate"] <= selected["startDate"]:
            _review("unsupported-schedule")
    if "duration" in selected:
        selected["duration"] = _duration(selected["duration"])
    selected["items"] = _phase_items(selected["items"])
    selected["discountIds"] = _provider_values(selected["discountIds"], 20)
    selected["automaticTax"] = _automatic_tax(selected["automaticTax"])
    selected["billingThresholds"] = _subscription_billing_thresholds(
        selected["billingThresholds"]
    )
    selected["defaultTaxRateIds"] = _provider_values(
        selected["defaultTaxRateIds"], 20
    )
    if selected["collectionMethod"] not in {"charge_automatically", "send_invoice"}:
        _review("unsupported-schedule")
    if selected["defaultPaymentMethodId"] is not None:
        _provider_value(selected["defaultPaymentMethodId"])
    selected["invoiceSettings"] = _invoice_settings(selected["invoiceSettings"])
    _validate_collection_invoice_settings(
        selected["collectionMethod"], selected["invoiceSettings"]
    )
    selected["metadata"] = _safe_metadata(selected["metadata"])
    if selected["prorationBehavior"] not in {
        "none",
        "create_prorations",
        "always_invoice",
    }:
        _review("unsupported-schedule")
    return dict(selected)


def _phase_from_subscription(selected: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "startDate": selected["currentPeriodStart"],
        "endDate": selected["currentPeriodEnd"],
        "items": [
            {key: copy.deepcopy(value) for key, value in item.items() if key != "itemId"}
            for item in selected["items"]
        ],
        "discountIds": copy.deepcopy(selected["discountIds"]),
        "automaticTax": copy.deepcopy(selected["automaticTax"]),
        "billingThresholds": copy.deepcopy(selected["billingThresholds"]),
        "defaultTaxRateIds": copy.deepcopy(selected["defaultTaxRateIds"]),
        "collectionMethod": selected["collectionMethod"],
        "defaultPaymentMethodId": selected["defaultPaymentMethodId"],
        "invoiceSettings": copy.deepcopy(selected["invoiceSettings"]),
        "metadata": copy.deepcopy(selected["metadata"]),
        "prorationBehavior": "none",
    }


def _replace_phase_price(
    phase: dict[str, Any], source_price_id: str, target_price_id: str
) -> None:
    matches = [line for line in phase["items"] if line["priceId"] == source_price_id]
    if len(matches) != 1:
        _review("ambiguous-price" if len(matches) > 1 else "unsupported-schedule")
    matches[0]["priceId"] = target_price_id
    phase["prorationBehavior"] = "none"


def _phase_price_count(phase: Mapping[str, Any], price_id: str) -> int:
    return sum(line["priceId"] == price_id for line in phase["items"])


def _selected_subscription_item(
    items: list[dict[str, Any]], source_price_id: str
) -> dict[str, Any]:
    matches = [item for item in items if item["priceId"] == source_price_id]
    if len(matches) != 1:
        _review("ambiguous-price" if len(matches) > 1 else "source-drift")
    return matches[0]


def _reject_existing_target(items: list[dict[str, Any]], target_price_id: str) -> None:
    if any(item["priceId"] == target_price_id for item in items):
        _review("ambiguous-price")


def _pending_update(value: object) -> dict[str, int] | None:
    if value is None:
        return None
    selected = _closed(value, {"expiresAt"}, "pending-update")
    _positive_epoch(selected["expiresAt"])
    return dict(selected)


def _latest_invoice(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    selected = _closed(
        value, {"invoiceId", "status", "paymentStatus"}, "unpaid-invoice"
    )
    _provider_value(selected["invoiceId"])
    if selected["status"] not in {
        "draft",
        "open",
        "paid",
        "uncollectible",
        "void",
    }:
        _review("unpaid-invoice")
    if selected["paymentStatus"] is not None:
        _safe_token(selected["paymentStatus"])
    return dict(selected)


def _automatic_tax(value: object) -> dict[str, bool]:
    selected = _closed(value, {"enabled"}, "tax-approval")
    if type(selected["enabled"]) is not bool:
        _review("tax-approval")
    return dict(selected)


def _invoice_settings(value: object) -> dict[str, Any]:
    selected = _closed(
        value, {"issuerType", "daysUntilDue"}, "unsupported-schedule"
    )
    if selected["issuerType"] != "self":
        _review("unsupported-schedule")
    days_until_due = selected["daysUntilDue"]
    if days_until_due is not None and (
        type(days_until_due) is not int or not 1 <= days_until_due <= 3650
    ):
        _review("unsupported-schedule")
    return dict(selected)


def _validate_collection_invoice_settings(
    collection_method: object, invoice_settings: Mapping[str, Any]
) -> None:
    days_until_due = invoice_settings["daysUntilDue"]
    if (
        collection_method == "send_invoice" and days_until_due is None
    ) or (
        collection_method == "charge_automatically" and days_until_due is not None
    ):
        _review("unsupported-collection-mode")


def _subscription_billing_thresholds(value: object) -> dict[str, Any] | None:
    if value is None:
        return None
    selected = _closed(
        value,
        {"amountGte", "resetBillingCycleAnchor"},
        "unsupported-schedule",
    )
    if type(selected["amountGte"]) is not int or not 1 <= selected["amountGte"] <= 99_999_999:
        _review("unsupported-schedule")
    if type(selected["resetBillingCycleAnchor"]) is not bool:
        _review("unsupported-schedule")
    return dict(selected)


def _default_settings(value: object) -> dict[str, Any]:
    selected = _closed(
        value,
        {
            "automaticTax",
            "billingCycleAnchor",
            "billingThresholds",
            "collectionMethod",
            "defaultPaymentMethodId",
            "invoiceSettings",
        },
        "unsupported-schedule",
    )
    selected["automaticTax"] = _automatic_tax(selected["automaticTax"])
    if selected["billingCycleAnchor"] not in {"automatic", "phase_start"}:
        _review("unsupported-schedule")
    selected["billingThresholds"] = _subscription_billing_thresholds(
        selected["billingThresholds"]
    )
    if selected["collectionMethod"] not in {
        "charge_automatically",
        "send_invoice",
    }:
        _review("unsupported-schedule")
    if selected["defaultPaymentMethodId"] is not None:
        _provider_value(selected["defaultPaymentMethodId"])
    selected["invoiceSettings"] = _invoice_settings(selected["invoiceSettings"])
    _validate_collection_invoice_settings(
        selected["collectionMethod"], selected["invoiceSettings"]
    )
    return dict(selected)


def _billing_thresholds(value: object) -> dict[str, int] | None:
    if value is None:
        return None
    selected = _closed(value, {"usageGte"}, "unsupported-schedule")
    selected["usageGte"] = _quantity(selected["usageGte"])
    return dict(selected)


def _duration(value: object) -> dict[str, Any]:
    selected = _closed(value, {"interval", "intervalCount"}, "unsupported-schedule")
    if selected["interval"] not in {"day", "week", "month", "year"}:
        _review("unsupported-schedule")
    if type(selected["intervalCount"]) is not int or not 1 <= selected["intervalCount"] <= 12:
        _review("unsupported-schedule")
    return dict(selected)


def _safe_metadata(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping) or len(value) > 20:
        _review("unsupported-schedule")
    result = {}
    for key, nested in value.items():
        if (
            type(key) is not str
            or key not in _PLATFORM_METADATA_KEYS
            or type(nested) is not str
            or _SAFE_METADATA_VALUE.fullmatch(nested) is None
        ):
            _review("unsupported-schedule")
        result[key] = nested
    return result


def _price_configuration(value: object) -> dict[str, Any]:
    selected = _closed(value, {"currency", "recurring"}, "source-drift")
    if (
        type(selected["currency"]) is not str
        or _CURRENCY.fullmatch(selected["currency"]) is None
    ):
        _review("source-drift")
    recurrence = _closed(
        selected["recurring"],
        {"interval", "intervalCount", "usageType"},
        "source-drift",
    )
    if (
        recurrence["interval"] not in {"month", "year"}
        or recurrence["intervalCount"] != 1
        or recurrence["usageType"] != "licensed"
    ):
        _review("source-drift")
    return {"currency": selected["currency"], "recurring": recurrence}


def _offer_terms(value: object) -> dict[str, Any]:
    selected = _closed(
        value,
        {"offerVersionId", "revision", "schemaVersion", "snapshot", "contentHash"},
        "source-drift",
    )
    snapshot = _closed(
        selected["snapshot"],
        {
            "schemaVersion",
            "amountMinor",
            "billingScheme",
            "currency",
            "saleType",
            "recurrence",
            "taxBehavior",
        },
        "source-drift",
    )
    if snapshot.get("saleType") != "recurring":
        _review("source-drift")
    recurrence = _price_configuration(
        {"currency": snapshot.get("currency"), "recurring": snapshot.get("recurrence")}
    )
    expected_hash = hashlib.sha256(
        json.dumps(
            {"schemaVersion": 1, "snapshot": snapshot},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()
    if (
        selected.get("schemaVersion") != 1
        or snapshot.get("schemaVersion") != 1
        or selected.get("contentHash") != expected_hash
    ):
        _review("source-drift")
    return {"currency": recurrence["currency"], "recurrence": recurrence["recurring"]}


def _provider_values(value: object, maximum: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        _review("unsupported-schedule")
    result = []
    for nested in value:
        _provider_value(nested)
        result.append(nested)
    if len(set(result)) != len(result):
        _review("unsupported-schedule")
    return result


def _closed(
    value: object,
    required: set[str],
    reason: str,
    optional: set[str] | None = None,
) -> dict[str, Any]:
    optional = optional or set()
    if not isinstance(value, Mapping) or not required.issubset(value) or set(value) - required - optional:
        _review(reason)
    return dict(value)


def _provider_value(value: object) -> str:
    if type(value) is not str or _PROVIDER_VALUE.fullmatch(value) is None:
        _review("source-drift")
    return value


def _safe_token(value: object) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 64
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in value)
    ):
        _review("source-drift")
    return value


def _digest(value: object) -> str:
    if type(value) is not str or _HASH.fullmatch(value) is None:
        _review("source-drift")
    return value


def _positive_epoch(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 9_999_999_999:
        _review("source-drift")
    return value


def _quantity(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 1_000_000:
        _review("source-drift")
    return value


def _migration_request_hash(command: InternalCommand) -> str:
    try:
        encoded = json.dumps(
            {
                "kind": command.kind,
                "scope": command.scope.fields(),
                "connectionId": command.connection_id,
                "commandId": command.command_id,
                "input": command.input,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (AttributeError, TypeError, UnicodeError, ValueError):
        raise MigrationConflict("migration command conflicted") from None
    return hashlib.sha256(encoded).hexdigest()


def _key_hash(value: object) -> str:
    if type(value) is not str or not 1 <= len(value) <= 256:
        raise MigrationConflict("migration command conflicted")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _job_id(command: InternalCommand) -> str:
    digest = hashlib.sha256(
        (
            command.scope.partition_key
            + "\0"
            + command.connection_id
            + "\0"
            + command.idempotency_key
        ).encode("utf-8")
    ).hexdigest()
    return "migration-" + digest[:40]


def _review(reason: str) -> None:
    raise MigrationNeedsReview(reason)
