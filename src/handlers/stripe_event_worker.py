"""DynamoDB Stream consumer for verified Stripe ingress outboxes."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping

try:
    from domain.integrations import IntegrationScope
    from domain.operations import IntegrationEventEnvelope, canonical_hash
    from registry import _deserialize
except ModuleNotFoundError:
    from src.domain.integrations import IntegrationScope
    from src.domain.operations import IntegrationEventEnvelope, canonical_hash
    from src.registry import _deserialize


_MAPPING_HINT = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}", re.ASCII)
_MAX_MAPPING_ATTEMPTS = 3


class StripeEventWorker:
    def __init__(
        self,
        registry: Any,
        store: Any,
        mappings: Any,
        provider: Any,
        migration_store: Any = None,
        migration_queue: Any = None,
    ):
        if any(value is None for value in (registry, store, mappings, provider)):
            raise ValueError("Stripe event worker is unavailable")
        self._registry = registry
        self._store = store
        self._mappings = mappings
        self._provider = provider
        self._migration_store = migration_store
        self._migration_queue = migration_queue

    def process(self, record: dict[str, Any], sequence: str) -> None:
        scope = record.get("scope")
        if type(scope) is not IntegrationScope:
            raise ValueError("Stripe ingress record is invalid")
        claim = self._store.claim_ingress(
            scope=scope,
            outbox_id=record["outboxId"],
            receipt_id=record["receiptId"],
            expected_revision=record["processingRevision"],
            sequence=sequence,
        )
        if claim is None:
            return
        claimed_revision = claim.get("processingRevision")
        if type(claimed_revision) is not int or claimed_revision < 2:
            raise RuntimeError("Stripe ingress claim is invalid")
        receipt = self._store.receipt(scope, record["receiptId"])
        selected = _receipt(receipt, scope, record["receiptId"])
        connection = self._registry.connection(scope, selected["connectionId"])
        account = connection.provider_metadata.get("accountReference")
        if (
            connection.provider != "stripe"
            or (
                connection.status != "active"
                and not (
                    selected["eventType"] == "account.application.deauthorized"
                    and connection.status == "pending"
                )
            )
            or connection.mode != selected["mode"]
            or type(account) is not str
            or hashlib.sha256(account.encode("ascii")).hexdigest()
            != selected["accountHash"]
        ):
            raise RuntimeError("Stripe ingress scope is invalid")
        state = _canonical_state(
            self._provider.retrieve_webhook_state(
                connection, selected["receiptId"], selected["eventType"]
            ),
            selected,
        )
        if selected["eventType"] == "account.application.deauthorized":
            if (
                state["objectType"] != "account"
                or state["objectId"] != selected["accountHash"]
                or state["canonical"] != {"accountHash": selected["accountHash"]}
            ):
                raise RuntimeError("Canonical Stripe event is invalid")
            self._registry.disable_stripe_account(
                scope,
                selected["connectionId"],
                account,
                connection.revision,
            )
            self._store.complete_ingress(
                scope=scope,
                outbox_id=record["outboxId"],
                receipt_id=record["receiptId"],
                claimed_revision=claimed_revision,
                sequence=sequence,
                decision_code="processed",
                envelopes=[],
                projection=None,
            )
            return
        migration_result = self._reconcile_pending_update(
            scope, selected, state
        )
        mapping = self._mapping(scope, selected["connectionId"], state)
        if mapping is None:
            if migration_result is not None:
                self._store.complete_ingress(
                    scope=scope,
                    outbox_id=record["outboxId"],
                    receipt_id=record["receiptId"],
                    claimed_revision=claimed_revision,
                    sequence=sequence,
                    decision_code="processed",
                    envelopes=[],
                    projection=None,
                )
                return
            if state["mappingHint"] is not None:
                if record["attemptCount"] + 1 < _MAX_MAPPING_ATTEMPTS:
                    self._store.retry_ingress(
                        scope=scope,
                        outbox_id=record["outboxId"],
                        receipt_id=record["receiptId"],
                        claimed_revision=claimed_revision,
                        sequence=sequence,
                    )
                else:
                    self._store.complete_ingress(
                        scope=scope,
                        outbox_id=record["outboxId"],
                        receipt_id=record["receiptId"],
                        claimed_revision=claimed_revision,
                        sequence=sequence,
                        decision_code="needs_review",
                        envelopes=[],
                        projection=None,
                    )
                return
            self._store.complete_ingress(
                scope=scope,
                outbox_id=record["outboxId"],
                receipt_id=record["receiptId"],
                claimed_revision=claimed_revision,
                sequence=sequence,
                decision_code="ignored_unmapped",
                envelopes=[],
                projection=None,
            )
            return
        if mapping.get("resourceType") == "migration-subscription":
            self._store.complete_ingress(
                scope=scope,
                outbox_id=record["outboxId"],
                receipt_id=record["receiptId"],
                claimed_revision=claimed_revision,
                sequence=sequence,
                decision_code=("processed" if migration_result is not None else "needs_review"),
                envelopes=[],
                projection=None,
            )
            return
        if state["objectType"] in {
            "subscription",
            "invoice",
        }:
            primary_offer = self._subscription_primary_offer(
                scope, selected["connectionId"], state, mapping
            )
            if primary_offer is None:
                self._store.complete_ingress(
                    scope=scope,
                    outbox_id=record["outboxId"],
                    receipt_id=record["receiptId"],
                    claimed_revision=claimed_revision,
                    sequence=sequence,
                    decision_code="needs_review",
                    envelopes=[],
                    projection=None,
                )
                return
            projected_offers = set(mapping.get("offerVersionIds", []))
            previous_primary = mapping.get("primaryOfferVersionId")
            if primary_offer != previous_primary:
                projected_offers.discard(previous_primary)
            projected_offers = sorted(projected_offers | {primary_offer})
            if len(projected_offers) > 20:
                raise RuntimeError("Stripe subscription authorization is unavailable")
            mapping = {
                **mapping,
                "offerVersionIds": projected_offers,
                "primaryOfferVersionId": primary_offer,
            }
        payment_intent_id, subscription_id = _provider_links(state)
        if payment_intent_id is not None or subscription_id is not None:
            self._mappings.bind_checkout_objects(
                scope,
                selected["connectionId"],
                mapping,
                payment_intent_id=payment_intent_id,
                subscription_id=subscription_id,
            )
        envelopes, decision, projection = _normalized_events(
            scope, selected, state, mapping, self._store
        )
        self._store.complete_ingress(
            scope=scope,
            outbox_id=record["outboxId"],
            receipt_id=record["receiptId"],
            claimed_revision=claimed_revision,
            sequence=sequence,
            decision_code=decision,
            envelopes=envelopes,
            projection=projection,
        )

    def _reconcile_pending_update(self, scope, receipt, state):
        if receipt["eventType"] not in {
            "customer.subscription.pending_update_applied",
            "customer.subscription.pending_update_expired",
        }:
            return None
        reconcile = getattr(self._migration_store, "reconcile_migration_webhook", None)
        if not callable(reconcile):
            return None
        canonical = state["canonical"]
        items = canonical.get("items") if isinstance(canonical, Mapping) else None
        if (
            state["objectType"] != "subscription"
            or not isinstance(items, list)
            or not 1 <= len(items) <= 20
        ):
            raise RuntimeError("Canonical Stripe migration event is invalid")
        result = reconcile(
            scope=scope,
            connectionId=receipt["connectionId"],
            providerSubscriptionId=canonical.get("subscriptionId"),
            eventId=receipt["receiptId"],
            eventType=receipt["eventType"],
            eventCreatedAt=receipt["eventCreatedAt"],
            priceIds=[item.get("priceId") for item in items],
            pendingUpdate=canonical.get("pendingUpdate") is not None,
        )
        if isinstance(result, Mapping) and result.get("enqueue") is True:
            if self._migration_queue is None:
                raise RuntimeError("Stripe migration continuation is unavailable")
            self._migration_queue.send(
                {
                    "version": 1,
                    **scope.fields(),
                    "connectionId": receipt["connectionId"],
                    "jobId": result["jobId"],
                    "action": "reconcile",
                    "revision": result["revision"],
                },
                delay_seconds=result.get("workDelaySeconds", 0),
            )
        return result

    def _mapping(
        self, scope: IntegrationScope, connection_id: str, state: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        object_type = state["objectType"]
        provider_id = state["objectId"]
        if object_type == "invoice":
            provider_id = state["canonical"].get("subscriptionId")
            object_type = "subscription"
        elif object_type == "refund":
            provider_id = state["canonical"].get("paymentIntentId")
            object_type = "payment-intent"
            if provider_id is None:
                provider_id = state["canonical"].get("chargeId")
                object_type = "charge"
        if type(provider_id) is not str:
            mapping = None
        else:
            mapping = self._mappings.object_owner(
                scope, connection_id, object_type, provider_id
            )
        hint = state.get("mappingHint")
        if mapping is None and type(hint) is str:
            mapping = self._mappings.get_mapping(scope, connection_id, "checkout", hint)
            if mapping is not None and (
                mapping.get("resourceType") != "checkout"
                or mapping.get("resourceId") != hint
                or mapping.get("paymentAttemptId") != hint
            ):
                raise RuntimeError("Stripe object mapping is invalid")
        return mapping

    def _subscription_offer_is_authorized(
        self,
        scope: IntegrationScope,
        connection_id: str,
        state: Mapping[str, Any],
        checkout_mapping: Mapping[str, Any],
    ) -> bool:
        return (
            self._subscription_primary_offer(
                scope, connection_id, state, checkout_mapping
            )
            is not None
        )

    def _subscription_primary_offer(
        self,
        scope: IntegrationScope,
        connection_id: str,
        state: Mapping[str, Any],
        checkout_mapping: Mapping[str, Any],
    ) -> str | None:
        canonical = state["canonical"]
        subscription = (
            canonical.get("subscription")
            if state["objectType"] == "invoice"
            else canonical
        )
        if not isinstance(subscription, Mapping):
            return None
        items = subscription.get("items")
        primary_offer_id = checkout_mapping.get("primaryOfferVersionId")
        checkout_offers = checkout_mapping.get("offerVersionIds")
        if (
            not isinstance(items, list)
            or not 1 <= len(items) <= 20
            or type(primary_offer_id) is not str
            or not isinstance(checkout_offers, list)
            or primary_offer_id not in checkout_offers
        ):
            return None
        subscription_id = subscription.get("subscriptionId")
        migration = None
        active_migration = getattr(self._migration_store, "active_migration", None)
        if callable(active_migration) and type(subscription_id) is str:
            migration = active_migration(scope, connection_id, subscription_id)
        allowed = set(checkout_offers)
        if isinstance(migration, Mapping):
            migration_offers = migration.get("offerVersionIds")
            if (
                not isinstance(migration_offers, list)
                or not 1 <= len(migration_offers) <= 21
                or any(type(value) is not str for value in migration_offers)
            ):
                return None
            allowed = set(migration_offers)
        mapped = []
        for item in items:
            price_id = item.get("priceId") if isinstance(item, Mapping) else None
            if type(price_id) is not str:
                return None
            offer = self._mappings.object_owner(
                scope, connection_id, "price", price_id
            )
            if (
                not isinstance(offer, Mapping)
                or offer.get("resourceType") != "offer"
                or offer.get("priceId") != price_id
                or offer.get("resourceId") not in allowed
                or offer.get("status") not in {"active", "existing_only"}
            ):
                return None
            mapped.append((price_id, offer["resourceId"]))
        if isinstance(migration, Mapping):
            target = [
                offer_id
                for price_id, offer_id in mapped
                if price_id == migration.get("targetPriceId")
                and offer_id == migration.get("targetOfferVersionId")
            ]
            source = [
                offer_id
                for price_id, offer_id in mapped
                if price_id == migration.get("sourcePriceId")
                and offer_id == migration.get("sourceOfferVersionId")
            ]
            if len(target) == 1 and not source:
                return target[0]
            if len(source) == 1 and not target:
                return source[0]
            return None
        matches = [offer_id for _, offer_id in mapped if offer_id == primary_offer_id]
        return primary_offer_id if len(matches) == 1 else None


def handle_records(event: Any, *, worker: Any) -> dict[str, list[dict[str, str]]]:
    records = event.get("Records") if isinstance(event, Mapping) else None
    if not isinstance(records, list) or not 1 <= len(records) <= 100:
        raise RuntimeError("Stripe event batch is invalid")
    sequences = [_sequence(record) for record in records]
    for index, record in enumerate(records):
        try:
            worker.process(_ingress_record(record), sequences[index])
        except Exception:
            return {
                "batchItemFailures": [
                    {"itemIdentifier": sequence} for sequence in sequences[index:]
                ]
            }
    return {"batchItemFailures": []}


def lambda_handler(event: Any, context: Any) -> dict[str, list[dict[str, str]]]:
    del context
    return handle_records(event, worker=_runtime_worker())


def _runtime_worker() -> Any:
    try:
        from runtime import stripe_event_worker_runtime
    except ModuleNotFoundError:
        from src.runtime import stripe_event_worker_runtime
    return stripe_event_worker_runtime()


def _sequence(record: object) -> str:
    dynamodb = record.get("dynamodb") if isinstance(record, Mapping) else None
    value = dynamodb.get("SequenceNumber") if isinstance(dynamodb, Mapping) else None
    if type(value) is not str or not value.isdecimal() or len(value) > 128:
        raise RuntimeError("Stripe event batch is invalid")
    return value


def _ingress_record(record: object) -> dict[str, Any]:
    if not isinstance(record, Mapping) or record.get("eventName") not in {
        "INSERT",
        "MODIFY",
    }:
        raise ValueError("Stripe ingress record is invalid")
    dynamodb = record.get("dynamodb")
    image = dynamodb.get("NewImage") if isinstance(dynamodb, Mapping) else None
    value = _deserialize(image)
    required = {
        "pk",
        "sk",
        "itemType",
        "environment",
        "tenantId",
        "draftId",
        "domain",
        "outboxId",
        "receiptId",
        "processingStatus",
        "processingRevision",
        "attemptCount",
        "createdAt",
        "expiresAt",
    }
    if set(value) != required and set(value) != required | {"processingSequence"}:
        raise ValueError("Stripe ingress record is invalid")
    scope = IntegrationScope(
        value["environment"], value["tenantId"], value["draftId"], value["domain"]
    )
    if (
        value["pk"] != scope.partition_key
        or value["sk"] != f"WEBHOOK_INGRESS_OUTBOX#{value['outboxId']}"
        or value["itemType"] != "WebhookIngressOutbox"
        or value["processingStatus"] != "pending"
        or value["receiptId"] != value["outboxId"]
        or type(value["processingRevision"]) is not int
        or value["processingRevision"] < 1
        or type(value["attemptCount"]) is not int
        or value["attemptCount"] < 0
    ):
        raise ValueError("Stripe ingress record is invalid")
    return {**value, "scope": scope}


def _receipt(value: object, scope: IntegrationScope, receipt_id: str) -> dict[str, Any]:
    required = {
        "scope",
        "receiptId",
        "connectionId",
        "provider",
        "mode",
        "eventType",
        "accountHash",
        "payloadHash",
        "status",
        "revision",
        "decisionCode",
        "eventCreatedAt",
        "receivedAt",
        "expiresAt",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != required
        or value.get("scope") != scope
        or value.get("receiptId") != receipt_id
        or value.get("provider") != "stripe"
        or value.get("status") != "processing"
        or value.get("decisionCode") != "processing"
    ):
        raise RuntimeError("Stripe webhook receipt is invalid")
    return dict(value)


def _canonical_state(value: object, receipt: Mapping[str, Any]) -> dict[str, Any]:
    keys = {
        "eventId",
        "eventType",
        "eventCreatedAt",
        "mode",
        "accountHash",
        "objectType",
        "objectId",
        "mappingHint",
        "canonical",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != keys
        or value.get("eventId") != receipt["receiptId"]
        or value.get("eventType") != receipt["eventType"]
        or value.get("eventCreatedAt") != receipt["eventCreatedAt"]
        or value.get("mode") != receipt["mode"]
        or value.get("accountHash") != receipt["accountHash"]
        or value.get("objectType")
        not in {"checkout-session", "refund", "subscription", "invoice", "account"}
        or type(value.get("objectId")) is not str
        or (
            value.get("mappingHint") is not None
            and (
                type(value.get("mappingHint")) is not str
                or _MAPPING_HINT.fullmatch(value["mappingHint"]) is None
            )
        )
        or not isinstance(value.get("canonical"), Mapping)
    ):
        raise RuntimeError("Canonical Stripe event is invalid")
    return {**value, "canonical": dict(value["canonical"])}


def _provider_links(state: Mapping[str, Any]) -> tuple[str | None, str | None]:
    canonical = state["canonical"]
    if state["objectType"] == "checkout-session":
        return canonical.get("paymentIntentId"), canonical.get("subscriptionId")
    if state["objectType"] == "refund":
        return canonical.get("paymentIntentId"), None
    if state["objectType"] == "subscription":
        return None, canonical.get("subscriptionId")
    if state["objectType"] == "invoice":
        return None, canonical.get("subscriptionId")
    return None, None


def _normalized_events(scope, receipt, state, mapping, store):
    event_type = receipt["eventType"]
    canonical = state["canonical"]
    if event_type.startswith("checkout.session."):
        required = {
            "sessionId",
            "status",
            "paymentStatus",
            "mode",
            "paymentIntentId",
            "subscriptionId",
            "latestInvoiceId",
        }
        if set(canonical) != required or canonical["sessionId"] != state["objectId"]:
            raise RuntimeError("Canonical Stripe checkout is invalid")
        if (
            event_type
            in {
                "checkout.session.completed",
                "checkout.session.async_payment_succeeded",
            }
            and canonical["status"] == "complete"
            and canonical["paymentStatus"] == "paid"
        ):
            output_type = "commerce.payment.succeeded.v1"
            decision = "processed"
        elif event_type in {
            "checkout.session.expired",
            "checkout.session.async_payment_failed",
        } and (
            canonical["status"] == "expired"
            or canonical["paymentStatus"] in {"unpaid", "failed"}
        ):
            output_type = "commerce.payment.terminal_unpaid.v1"
            decision = "processed"
        else:
            return [], "ignored_nonterminal", None
        data = _payment_data(mapping)
        return (
            [
                IntegrationEventEnvelope(
                    scope=scope,
                    event_id=_event_id(receipt["receiptId"], output_type),
                    event_type=output_type,
                    occurred_at=receipt["eventCreatedAt"],
                    data=data,
                ).to_dict()
            ],
            decision,
            None,
        )
    if event_type.startswith("refund."):
        required = {
            "refundId",
            "status",
            "amountMinor",
            "currency",
            "paymentIntentId",
            "chargeId",
        }
        if set(canonical) != required or canonical["refundId"] != state["objectId"]:
            raise RuntimeError("Canonical Stripe refund is invalid")
        if canonical["status"] != "succeeded":
            return [], "ignored_nonterminal", None
        data = {
            "orderId": _payment_data(mapping)["orderId"],
            "refundId": "refund-"
            + hashlib.sha256(canonical["refundId"].encode("ascii")).hexdigest()[:48],
            "amountMinor": canonical["amountMinor"],
            "currency": canonical["currency"],
        }
        output_type = "commerce.refund.confirmed.v1"
        return (
            [
                IntegrationEventEnvelope(
                    scope=scope,
                    event_id=_event_id(receipt["receiptId"], output_type),
                    event_type=output_type,
                    occurred_at=receipt["eventCreatedAt"],
                    data=data,
                ).to_dict()
            ],
            "processed",
            None,
        )
    if event_type.startswith("customer.subscription."):
        return _subscription_events(scope, receipt, state, mapping, canonical, store)
    if event_type.startswith("invoice."):
        required = {"invoiceId", "status", "paid", "subscriptionId", "subscription"}
        if (
            set(canonical) != required
            or canonical["invoiceId"] != state["objectId"]
            or not isinstance(canonical["subscription"], Mapping)
        ):
            raise RuntimeError("Canonical Stripe invoice is invalid")
        subscription_events, decision, projection = _subscription_events(
            scope,
            receipt,
            {
                **state,
                "objectType": "subscription",
                "objectId": canonical["subscriptionId"],
            },
            mapping,
            canonical["subscription"],
            store,
        )
        return subscription_events, decision, projection
    raise RuntimeError("Canonical Stripe event is invalid")


def _subscription_events(scope, receipt, state, mapping, canonical, store):
    required = {
        "subscriptionId",
        "status",
        "currentPeriodEnd",
        "latestInvoiceId",
        "items",
        "pauseCollection",
        "pendingUpdate",
    }
    if (
        set(canonical) != required
        or canonical["subscriptionId"] != state["objectId"]
        or store is None
        or not isinstance(canonical.get("items"), list)
        or not 1 <= len(canonical["items"]) <= 20
    ):
        raise RuntimeError("Canonical Stripe subscription is invalid")
    try:
        status = _subscription_status(
            {
                "status": canonical["status"],
                "pauseCollection": canonical["pauseCollection"],
            }
        )
    except ValueError:
        return [], "needs_review", None
    if receipt["eventType"] == "customer.subscription.deleted" and status != "canceled":
        return [], "needs_review", None
    offer_ids = mapping.get("offerVersionIds") if isinstance(mapping, Mapping) else None
    primary_offer_id = (
        mapping.get("primaryOfferVersionId") if isinstance(mapping, Mapping) else None
    )
    subscription_id = (
        mapping.get("paymentAttemptId") if isinstance(mapping, Mapping) else None
    )
    if (
        not isinstance(offer_ids, list)
        or not 1 <= len(offer_ids) <= 20
        or any(type(offer_id) is not str for offer_id in offer_ids)
        or type(primary_offer_id) is not str
        or primary_offer_id not in offer_ids
        or type(subscription_id) is not str
        or type(canonical["currentPeriodEnd"]) is not int
    ):
        return [], "needs_review", None
    projection = store.plan_subscription_projection(
        scope=scope,
        subscription_id=subscription_id,
        offer_version_id=primary_offer_id,
        status=status,
        current_period_end=canonical["currentPeriodEnd"],
        event_id=receipt["receiptId"],
        event_created_at=receipt["eventCreatedAt"],
        state_hash=canonical_hash(canonical),
    )
    if not isinstance(projection, Mapping):
        raise RuntimeError("Stripe subscription projection is unavailable")
    if projection.get("stale") is True:
        return [], "ignored_no_change", None
    output_type = "commerce.subscription.updated.v1"
    envelope = IntegrationEventEnvelope(
        scope=scope,
        event_id=_event_id(receipt["receiptId"], output_type),
        event_type=output_type,
        occurred_at=receipt["eventCreatedAt"],
        data={
            "subscriptionId": subscription_id,
            "offerVersionId": primary_offer_id,
            "status": status,
            "currentPeriodEnd": canonical["currentPeriodEnd"],
            "sourceRevision": projection["sourceRevision"],
        },
    ).to_dict()
    return [envelope], "processed", dict(projection)


def _payment_data(mapping: object) -> dict[str, str]:
    if not isinstance(mapping, Mapping):
        raise RuntimeError("Stripe object mapping is invalid")
    result = {
        "reservationId": mapping.get("reservationId"),
        "orderId": mapping.get("orderId"),
        "paymentAttemptId": mapping.get("paymentAttemptId"),
    }
    if any(type(value) is not str for value in result.values()):
        raise RuntimeError("Stripe object mapping is invalid")
    return result


def _event_id(provider_event_id: str, event_type: str) -> str:
    digest = hashlib.sha256(
        (provider_event_id + "\0" + event_type).encode("ascii")
    ).hexdigest()
    return "stripe-" + digest[:48]


def _subscription_status(value: object) -> str:
    if not isinstance(value, Mapping) or set(value) != {"status", "pauseCollection"}:
        raise ValueError("Canonical Stripe subscription is invalid")
    status = value["status"]
    if status in {"active", "trialing"}:
        return "active"
    if status in {"past_due", "unpaid"}:
        return "past_due"
    if status in {"canceled", "incomplete_expired"}:
        return "canceled"
    raise ValueError("Canonical Stripe subscription is invalid")
