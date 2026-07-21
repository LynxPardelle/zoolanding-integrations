"""Draft-scoped Stripe commerce commands with replay-safe provider writes."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import urlsplit

try:
    from contracts.internal import InternalCommand
    from domain.integrations import IntegrationScope, technical_expiry
except ModuleNotFoundError:
    from src.contracts.internal import InternalCommand
    from src.domain.integrations import IntegrationScope, technical_expiry


class StripeCommandError(RuntimeError):
    pass


class StripeCommandConflict(StripeCommandError):
    pass


class StripeNeedsReview(StripeCommandError):
    pass


_CAPABILITIES = {
    "offer": "prices",
    "product-presentation": "prices",
    "discount": "coupons",
    "discount-lifecycle": "coupons",
    "checkout": "checkout",
    "checkout-status": "checkout",
    "subscription-change": "subscriptions",
    "subscription-discount": "subscriptions",
    "subscription-pause": "subscriptions",
    "customer-portal": "customer-portal",
}
_CHECKOUT_MIN_TTL_SECONDS = 30 * 60
_CHECKOUT_MAX_TTL_SECONDS = 24 * 60 * 60
_PROVIDER_IDEMPOTENCY_RETRY_SECONDS = 24 * 60 * 60


class StripeCommandService:
    def __init__(
        self, resolver, store, provider, routes, *, now_epoch, tax_verifier=None
    ):
        if any(
            value is None for value in (resolver, store, provider, routes, now_epoch)
        ):
            raise StripeCommandError("Stripe command service is unavailable")
        self._resolver = resolver
        self._store = store
        self._provider = provider
        self._routes = routes
        self._now_epoch = now_epoch
        self._tax_verifier = tax_verifier

    def execute(self, kind: str, command: InternalCommand) -> dict[str, Any]:
        capability = _CAPABILITIES.get(kind)
        if (
            capability is None
            or type(command) is not InternalCommand
            or command.kind != kind
        ):
            raise StripeCommandError("Stripe command is unavailable")
        resolved = self._resolver.resolve(
            command.scope,
            command.connection_id,
            provider="stripe",
            capability=capability,
        )
        if kind == "checkout-status":
            return self._checkout_status(command, resolved)

        now_epoch = self._now_epoch()
        request_hash = _request_hash(command)
        operation_claim = _operation_claim(command)
        existing = self._store.claim(
            command.scope,
            command.connection_id,
            command.idempotency_key,
            request_hash,
            command.command_id,
            technical_expiry(now_epoch),
            now_epoch,
            operation_claim,
        )
        if existing is not None:
            status = existing.get("status")
            if status == "accepted":
                if kind == "checkout":
                    return self._replay_checkout(command, resolved)
                if kind == "customer-portal":
                    return self._replay_portal(command, resolved)
                return {
                    "commandId": existing["commandId"],
                    "status": "accepted",
                }
            if status == "rejected":
                raise StripeCommandConflict("Stripe command conflicted")
            if status == "needs_review":
                return {"commandId": command.command_id, "status": "needs_review"}
            attempted_at = existing.get("attemptedAt")
            if (
                type(attempted_at) is not int
                or attempted_at < 0
                or attempted_at > now_epoch
                or now_epoch - attempted_at >= _PROVIDER_IDEMPOTENCY_RETRY_SECONDS
            ):
                self._store.mark_needs_review(
                    command.scope,
                    command.connection_id,
                    command.idempotency_key,
                    request_hash,
                )
                return {"commandId": command.command_id, "status": "needs_review"}
            if status == "pending":
                return {"commandId": command.command_id, "status": "pending"}

        if kind == "checkout" and not (
            now_epoch + _CHECKOUT_MIN_TTL_SECONDS
            <= command.input["checkoutExpiresAt"]
            <= now_epoch + _CHECKOUT_MAX_TTL_SECONDS
        ):
            self._store.mark_rejected(
                command.scope,
                command.connection_id,
                command.idempotency_key,
                request_hash,
            )
            raise StripeCommandConflict("Stripe command conflicted")

        try:
            result, mappings, code_claim = self._perform(kind, command, resolved)
        except StripeNeedsReview:
            self._store.mark_needs_review(
                command.scope,
                command.connection_id,
                command.idempotency_key,
                request_hash,
            )
            return {"commandId": command.command_id, "status": "needs_review"}
        except StripeCommandConflict:
            self._store.mark_rejected(
                command.scope,
                command.connection_id,
                command.idempotency_key,
                request_hash,
            )
            raise
        except Exception:
            self._store.mark_unknown(
                command.scope,
                command.connection_id,
                command.idempotency_key,
                request_hash,
            )
            return {"commandId": command.command_id, "status": "pending"}

        self._store.complete(
            command.scope,
            command.connection_id,
            command.idempotency_key,
            request_hash,
            {"status": "accepted"},
            mappings,
            code_claim=code_claim,
        )
        if kind == "checkout":
            return {
                "commandId": command.command_id,
                "status": "accepted",
                "redirectUrl": result["redirectUrl"],
                "expiresAt": result["expiresAt"],
            }
        if kind == "customer-portal":
            return {
                "commandId": command.command_id,
                "status": "accepted",
                "redirectUrl": result["redirectUrl"],
                "expiresAt": result["expiresAt"],
            }
        return {"commandId": command.command_id, "status": "accepted"}

    def _perform(self, kind, command, resolved):
        if kind == "offer":
            return self._offer(command, resolved)
        if kind == "product-presentation":
            return self._presentation(command, resolved)
        if kind == "discount":
            return self._discount(command, resolved)
        if kind == "discount-lifecycle":
            return self._discount_lifecycle(command, resolved)
        if kind == "checkout":
            return self._checkout(command, resolved)
        if kind == "subscription-change":
            return self._subscription_change(command, resolved)
        if kind == "subscription-discount":
            return self._subscription_discount(command, resolved)
        if kind == "subscription-pause":
            return self._subscription_pause(command, resolved)
        if kind == "customer-portal":
            return self._customer_portal(command, resolved)
        raise StripeCommandError("Stripe command is unavailable")

    def _offer(self, command, resolved):
        value = command.input
        mapping = self._mapping(command, "offer", value["resourceId"])
        if value["operation"] == "deactivate":
            if (
                mapping is not None
                and mapping.get("lifecycleRevision") == value["revision"]
                and mapping.get("lifecycleHash") == value["contentHash"]
                and mapping.get("status") == "inactive"
            ):
                return None, [], None
            mapping = _current_mapping(mapping, value["revision"])
            self._provider.deactivate_offer(
                resolved,
                mapping["productId"],
                mapping["priceId"],
                command.idempotency_key,
            )
            return (
                None,
                [
                    {
                        **mapping,
                        "status": "inactive",
                        "lifecycleRevision": value["revision"],
                        "lifecycleHash": value["contentHash"],
                    }
                ],
                None,
            )

        if mapping is not None:
            exact_replay = _same_immutable_version(
                mapping, value["revision"], value["contentHash"]
            )
            if mapping.get("status") != "active":
                raise StripeCommandConflict("Stripe command conflicted")
            if exact_replay:
                return None, [], None
            return None, [{**mapping, "revision": value["revision"]}], None
        product_id = self._provider.create_product(
            resolved, value["resourceId"], command.idempotency_key
        )
        price_id = self._provider.create_price(
            resolved,
            product_id,
            value["snapshot"],
            value["resourceId"],
            command.idempotency_key,
        )
        next_mapping = {
            **(mapping or {}),
            "resourceType": "offer",
            "resourceId": value["resourceId"],
            "revision": value["revision"],
            "contentHash": value["contentHash"],
            "productId": product_id,
            "priceId": price_id,
            "status": "active",
        }
        return None, [next_mapping], None

    def _presentation(self, command, resolved):
        value = command.input
        mapping = self._mapping(command, "offer", value["resourceId"])
        if mapping is None or mapping.get("status") != "active":
            raise StripeCommandConflict("Stripe command conflicted")
        previous_revision = mapping.get("presentationRevision", 0)
        if value["revision"] == previous_revision and value[
            "contentHash"
        ] == mapping.get("presentationHash"):
            return None, [], None
        if value["revision"] <= previous_revision:
            raise StripeCommandConflict("Stripe command conflicted")
        self._provider.update_product_presentation(
            resolved,
            mapping["productId"],
            value["snapshot"],
            command.idempotency_key,
        )
        return (
            None,
            [
                {
                    **mapping,
                    "presentationRevision": value["revision"],
                    "presentationHash": value["contentHash"],
                }
            ],
            None,
        )

    def _discount(self, command, resolved):
        value = command.input
        if value.get("operation") == "presentation":
            return self._discount_presentation(command, resolved)
        snapshot = value["snapshot"]
        code = snapshot.get("customerFacingCode")
        code_hash = _code_hash(code) if code is not None else None
        if code_hash is not None:
            owner = self._store.code_owner(
                command.scope, command.connection_id, code_hash
            )
            if owner is not None and owner != value["resourceId"]:
                raise StripeCommandConflict("Stripe command conflicted")
        existing = self._mapping(command, "discount", value["resourceId"])
        if existing is not None:
            exact_replay = _same_immutable_version(
                existing, value["revision"], value["contentHash"]
            )
            if existing.get("status") != "active":
                raise StripeCommandConflict("Stripe command conflicted")
            if exact_replay:
                return None, [], None
            return None, [{**existing, "revision": value["revision"]}], code_hash
        products = []
        for offer_id in snapshot["eligibleOfferVersionIds"]:
            offer = self._mapping(command, "offer", offer_id)
            if offer is None or offer.get("status") != "active":
                raise StripeCommandConflict("Stripe command conflicted")
            products.append(offer["productId"])
        provider_mapping = self._provider.create_discount(
            resolved,
            snapshot,
            products,
            command.idempotency_key,
        )
        mapping = {
            "resourceType": "discount",
            "resourceId": value["resourceId"],
            "revision": value["revision"],
            "contentHash": value["contentHash"],
            "couponId": provider_mapping["couponId"],
            "promotionCodeId": provider_mapping["promotionCodeId"],
            "eligibleOfferVersionIds": list(snapshot["eligibleOfferVersionIds"]),
            "duration": snapshot["duration"],
            "durationInMonths": snapshot["durationInMonths"],
            "redeemByEpoch": snapshot["redeemByEpoch"],
            "redemptionLimit": snapshot["redemptionLimit"],
            "value": dict(snapshot["value"]),
            "status": "active",
        }
        if code_hash is not None:
            mapping["codeHash"] = code_hash
        return None, [mapping], code_hash

    def _discount_presentation(self, command, resolved):
        value = command.input
        mapping = self._mapping(command, "discount", value["resourceId"])
        if mapping is None or mapping.get("status") != "active":
            raise StripeCommandConflict("Stripe command conflicted")
        previous_revision = mapping.get("presentationRevision", 0)
        if value["revision"] == previous_revision and value[
            "contentHash"
        ] == mapping.get("presentationHash"):
            return None, [], None
        if value["revision"] <= previous_revision:
            raise StripeCommandConflict("Stripe command conflicted")
        self._provider.update_discount_presentation(
            resolved,
            mapping["couponId"],
            value["snapshot"],
            command.idempotency_key,
        )
        return (
            None,
            [
                {
                    **mapping,
                    "presentationRevision": value["revision"],
                    "presentationHash": value["contentHash"],
                    "displayName": value["snapshot"]["displayName"],
                    "displayDescription": value["snapshot"].get("displayDescription"),
                }
            ],
            None,
        )

    def _discount_lifecycle(self, command, resolved):
        value = command.input
        mapping = self._mapping(command, "discount", value["resourceId"])
        target = value["snapshot"]["targetState"]
        if (
            mapping is not None
            and mapping.get("lifecycleRevision") == value["revision"]
            and mapping.get("lifecycleHash") == value["contentHash"]
            and mapping.get("status") == target
        ):
            return None, [], None
        mapping = _current_mapping(mapping, value["revision"])
        if target == "active" and mapping.get("status") != "active":
            raise StripeCommandConflict("Stripe command conflicted")
        self._provider.deactivate_discount(
            resolved,
            mapping["couponId"],
            mapping["promotionCodeId"],
            target,
            command.idempotency_key,
        )
        code_claim = mapping.get("codeHash") if target == "active" else None
        return (
            None,
            [
                {
                    **mapping,
                    "status": target,
                    "lifecycleRevision": value["revision"],
                    "lifecycleHash": value["contentHash"],
                }
            ],
            code_claim,
        )

    def _checkout(self, command, resolved):
        value = command.input
        tax_mode = resolved.binding.provider_metadata.get("taxMode")
        if value["taxPolicy"]["mode"] == "automatic" and tax_mode != "stripe-tax":
            raise StripeCommandConflict("Stripe command conflicted")
        existing_checkout = self._mapping(
            command, "checkout", value["paymentAttemptId"]
        )
        if existing_checkout is not None:
            expected = {
                "orderId": value["orderId"],
                "paymentAttemptId": value["paymentAttemptId"],
                "reservationId": value["reservationIds"][0],
                "revision": value["revision"],
                "offerVersionIds": [
                    line["offerVersionId"] for line in value["offerBindings"]
                ],
                "primaryOfferVersionId": next(
                    (
                        line["offerVersionId"]
                        for line in value["offerBindings"]
                        if line["snapshot"]["saleType"] == "recurring"
                    ),
                    None,
                ),
                "contentHash": command.content_hash,
            }
            if any(
                existing_checkout.get(key) != expected_value
                for key, expected_value in expected.items()
            ):
                raise StripeCommandConflict("Stripe command conflicted")
            result = self._provider.retrieve_checkout_handoff(
                resolved, existing_checkout["sessionId"]
            )
            _checkout_redirect(result)
            return result, [], None
        lines = []
        offer_ids = []
        primary_offer_ids = []
        currencies = set()
        for line in value["offerBindings"]:
            offer = self._mapping(command, "offer", line["offerVersionId"])
            if (
                offer is None
                or offer.get("status") != "active"
                or offer.get("revision") != line["revision"]
                or offer.get("contentHash") != line["contentHash"]
            ):
                raise StripeCommandConflict("Stripe command conflicted")
            lines.append({"price": offer["priceId"], "quantity": line["quantity"]})
            offer_ids.append(line["offerVersionId"])
            currencies.add(line["snapshot"]["currency"])
            if line["snapshot"]["saleType"] == "recurring":
                primary_offer_ids.append(line["offerVersionId"])

        if len(currencies) != 1 or len(primary_offer_ids) > 1:
            raise StripeCommandConflict("Stripe command conflicted")

        promotion_code_id = None
        discount_id = value.get("discountVersionId")
        if discount_id is not None:
            discount = self._mapping(command, "discount", discount_id)
            if discount is None or discount.get("status") != "active":
                raise StripeCommandConflict("Stripe command conflicted")
            eligible = discount["eligibleOfferVersionIds"]
            if eligible and any(offer_id not in eligible for offer_id in offer_ids):
                raise StripeCommandConflict("Stripe command conflicted")
            redeem_by = discount.get("redeemByEpoch")
            if redeem_by is not None and redeem_by <= self._now_epoch():
                raise StripeCommandConflict("Stripe command conflicted")
            discount_value = discount.get("value")
            if not isinstance(discount_value, dict):
                raise StripeCommandConflict("Stripe command conflicted")
            if discount_value.get("type") == "fixed_amount" and currencies != {
                discount_value.get("currency")
            }:
                raise StripeCommandConflict("Stripe command conflicted")
            promotion_code_id = discount["promotionCodeId"]

        routes = self._routes.resolve(command.scope)
        result = self._provider.create_checkout(
            resolved,
            lines,
            promotion_code_id,
            value,
            routes,
            command.idempotency_key,
        )
        _checkout_redirect(result, require_session=True)
        if result["expiresAt"] != value["checkoutExpiresAt"]:
            raise StripeCommandError("Stripe checkout is unavailable")
        mode = "subscription" if primary_offer_ids else "payment"
        mapping = {
            "resourceType": "checkout",
            "resourceId": value["paymentAttemptId"],
            "orderId": value["orderId"],
            "paymentAttemptId": value["paymentAttemptId"],
            "reservationId": value["reservationIds"][0],
            "revision": value["revision"],
            "offerVersionIds": offer_ids,
            "primaryOfferVersionId": (
                primary_offer_ids[0] if primary_offer_ids else None
            ),
            "contentHash": command.content_hash,
            "mode": mode,
            "sessionId": result["sessionId"],
            "expiresAt": result["expiresAt"],
            "status": "pending",
        }
        return result, [mapping], None

    def _replay_checkout(self, command, resolved):
        mapping = self._mapping(command, "checkout", command.input["paymentAttemptId"])
        if mapping is None:
            raise StripeCommandConflict("Stripe command conflicted")
        result = self._provider.retrieve_checkout_handoff(
            resolved, mapping["sessionId"]
        )
        _checkout_redirect(result)
        return {
            "commandId": command.command_id,
            "status": "accepted",
            "redirectUrl": result["redirectUrl"],
            "expiresAt": result["expiresAt"],
        }

    def _checkout_status(self, command, resolved):
        value = command.input
        mapping = self._mapping(command, "checkout", value["paymentAttemptId"])
        status = "not_created"
        if mapping is not None:
            if (
                mapping.get("orderId") != value["orderId"]
                or mapping.get("revision") != value["revision"]
            ):
                raise StripeCommandConflict("Stripe command conflicted")
            status = self._provider.retrieve_checkout_status(
                resolved, mapping["sessionId"]
            )
            if status not in {"pending", "paid", "terminal_unpaid", "unknown"}:
                status = "unknown"
        return {
            "orderId": value["orderId"],
            "paymentAttemptId": value["paymentAttemptId"],
            "revision": value["revision"],
            "status": status,
        }

    def _subscription_context(self, command, resolved):
        mapping = self._mapping(command, "checkout", command.input["subscriptionId"])
        if (
            mapping is None
            or mapping.get("resourceType") != "checkout"
            or mapping.get("resourceId") != command.input["subscriptionId"]
            or mapping.get("mode") not in {None, "subscription"}
            or type(mapping.get("providerSubscriptionId")) is not str
        ):
            raise StripeNeedsReview("Stripe subscription needs review")
        state = self._provider.retrieve_subscription_operation_state(
            resolved, mapping["providerSubscriptionId"]
        )
        required = {
            "subscriptionId",
            "customerId",
            "status",
            "items",
            "scheduleId",
            "discounts",
            "pauseCollection",
        }
        if (
            not isinstance(state, dict)
            or set(state) != required
            or state["subscriptionId"] != mapping["providerSubscriptionId"]
            or type(state["customerId"]) is not str
            or state["status"]
            not in {"active", "trialing", "past_due", "unpaid", "incomplete"}
            or not isinstance(state["items"], list)
            or not isinstance(state["discounts"], list)
        ):
            raise StripeNeedsReview("Stripe subscription needs review")
        return mapping, state

    def _subscription_change(self, command, resolved):
        _, state = self._subscription_context(command, resolved)
        value = command.input
        target = self._mapping(command, "offer", value["targetOfferVersionId"])
        if (
            target is None
            or target.get("status") != "active"
            or type(target.get("priceId")) is not str
        ):
            raise StripeNeedsReview("Stripe subscription needs review")
        if len(state["items"]) != 1:
            raise StripeNeedsReview("Stripe subscription needs review")
        item = state["items"][0]
        if (
            not isinstance(item, dict)
            or set(item) != {"itemId", "priceId", "quantity"}
            or type(item["itemId"]) is not str
            or type(item["priceId"]) is not str
            or type(item["quantity"]) is not int
            or item["quantity"] < 1
        ):
            raise StripeNeedsReview("Stripe subscription needs review")
        mode = value["planChangePolicy"]["mode"]
        if mode == "disabled":
            raise StripeNeedsReview("Stripe subscription needs review")
        if mode == "next-renewal":
            if state["scheduleId"] is not None:
                raise StripeNeedsReview("Stripe subscription needs review")
            self._provider.schedule_subscription_change(
                resolved,
                subscription_id=state["subscriptionId"],
                item_id=item["itemId"],
                current_price_id=item["priceId"],
                price_id=target["priceId"],
                quantity=item["quantity"],
                idempotency_key=command.idempotency_key,
            )
            return None, [], None
        if (
            self._tax_verifier is None
            or self._tax_verifier(resolved, state, target) is not True
        ):
            raise StripeNeedsReview("Stripe subscription needs review")
        arguments = {
            "subscription_id": state["subscriptionId"],
            "item_id": item["itemId"],
            "price_id": target["priceId"],
            "quantity": item["quantity"],
            "preview_timestamp": value["previewTimestamp"],
            "idempotency_key": command.idempotency_key,
        }
        preview = self._provider.preview_subscription_change(resolved, **arguments)
        if preview != {"previewTimestamp": value["previewTimestamp"]}:
            raise StripeNeedsReview("Stripe subscription needs review")
        self._provider.apply_subscription_change(resolved, **arguments)
        return None, [], None

    def _subscription_discount(self, command, resolved):
        _, state = self._subscription_context(command, resolved)
        value = command.input
        discounts = state["discounts"]
        if any(type(item) is not str for item in discounts) or len(discounts) > 1:
            raise StripeNeedsReview("Stripe subscription needs review")
        if value["action"] == "apply":
            mapping = self._mapping(command, "discount", value["discountVersionId"])
            if (
                mapping is None
                or mapping.get("status") != "active"
                or type(mapping.get("promotionCodeId")) is not str
            ):
                raise StripeNeedsReview("Stripe subscription needs review")
            promotion_code_id = mapping["promotionCodeId"]
            if discounts == [promotion_code_id]:
                return None, [], None
            if discounts:
                raise StripeNeedsReview("Stripe subscription needs review")
        else:
            if not discounts:
                return None, [], None
            promotion_code_id = discounts[0]
            owner = self._store.object_owner(
                command.scope,
                command.connection_id,
                "promotion-code",
                promotion_code_id,
            )
            if (
                owner is None
                or owner.get("resourceType") != "discount"
                or owner.get("promotionCodeId") != promotion_code_id
            ):
                raise StripeNeedsReview("Stripe subscription needs review")
            promotion_code_id = None
        self._provider.update_subscription_discount(
            resolved,
            subscription_id=state["subscriptionId"],
            promotion_code_id=promotion_code_id,
            idempotency_key=command.idempotency_key,
        )
        return None, [], None

    def _subscription_pause(self, command, resolved):
        _, state = self._subscription_context(command, resolved)
        value = command.input
        if value["action"] == "pause":
            if (
                value["pausePolicy"].get("enabled") is not True
                or state["pauseCollection"] is not None
            ):
                raise StripeNeedsReview("Stripe subscription needs review")
            pause_collection = {
                "behavior": value["pausePolicy"]["newInvoiceBehavior"].replace("-", "_")
            }
        else:
            if state["pauseCollection"] is None:
                return None, [], None
            pause_collection = None
        self._provider.update_subscription_pause(
            resolved,
            subscription_id=state["subscriptionId"],
            pause_collection=pause_collection,
            idempotency_key=command.idempotency_key,
        )
        return None, [], None

    def _customer_portal(self, command, resolved):
        _, state = self._subscription_context(command, resolved)
        configuration = self._mapping(
            command, "portal-configuration", "restricted-default"
        )
        mappings = []
        if configuration is None:
            configuration_id = self._provider.create_portal_configuration(
                resolved,
                "portal-configuration-v1:" + command.connection_id,
            )
            configuration = {
                "resourceType": "portal-configuration",
                "resourceId": "restricted-default",
                "revision": 1,
                "contentHash": hashlib.sha256(
                    b"payment-method-and-invoice-history-only-v1"
                ).hexdigest(),
                "configurationId": configuration_id,
                "status": "active",
            }
            mappings.append(configuration)
        if (
            configuration.get("status") != "active"
            or type(configuration.get("configurationId")) is not str
        ):
            raise StripeNeedsReview("Stripe subscription needs review")
        result = self._provider.create_portal_session(
            resolved,
            customer_id=state["customerId"],
            configuration_id=configuration["configurationId"],
            idempotency_key=command.idempotency_key,
        )
        _portal_redirect(result)
        return result, mappings, None

    def _replay_portal(self, command, resolved):
        result, _, _ = self._customer_portal(command, resolved)
        return {
            "commandId": command.command_id,
            "status": "accepted",
            "redirectUrl": result["redirectUrl"],
            "expiresAt": result["expiresAt"],
        }

    def _mapping(self, command, resource_type, resource_id):
        return self._store.get_mapping(
            command.scope, command.connection_id, resource_type, resource_id
        )


def _request_hash(command: InternalCommand) -> str:
    return hashlib.sha256(
        json.dumps(
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
        ).encode("ascii")
    ).hexdigest()


def _code_hash(value: str) -> str:
    return hashlib.sha256(value.casefold().encode("utf-8")).hexdigest()


def _operation_claim(command: InternalCommand) -> dict[str, Any]:
    value = command.input
    if command.kind == "offer":
        resource_type = "offer"
        resource_id = value["resourceId"]
        dimension = "immutable" if value["operation"] == "provision" else "lifecycle"
        revision = value["revision"]
    elif command.kind == "product-presentation":
        resource_type = "offer"
        resource_id = value["resourceId"]
        dimension = "presentation"
        revision = value["revision"]
    elif command.kind == "discount":
        resource_type = "discount"
        resource_id = value["resourceId"]
        dimension = (
            "presentation" if value.get("operation") == "presentation" else "immutable"
        )
        revision = value["revision"]
    elif command.kind == "discount-lifecycle":
        resource_type = "discount"
        resource_id = value["resourceId"]
        dimension = "lifecycle"
        revision = value["revision"]
    elif command.kind == "checkout":
        resource_type = "checkout"
        resource_id = value["paymentAttemptId"]
        dimension = "immutable"
        revision = value["revision"]
    elif command.kind in {
        "subscription-change",
        "subscription-discount",
        "subscription-pause",
        "customer-portal",
    }:
        resource_type = "subscription"
        resource_id = value["subscriptionId"]
        dimension = (
            "portal"
            if command.kind == "customer-portal"
            else command.kind.removeprefix("subscription-")
        )
        revision = value.get("expectedRevision", 1)
    else:
        raise StripeCommandError("Stripe command is unavailable")
    if type(command.content_hash) is not str:
        raise StripeCommandError("Stripe command is unavailable")
    return {
        "resourceType": resource_type,
        "resourceId": resource_id,
        "dimension": dimension,
        "revision": revision,
        "contentHash": command.content_hash,
    }


def _same_immutable_version(mapping, revision, content_hash):
    current_revision = mapping.get("revision", 0)
    if revision < current_revision or mapping.get("contentHash") != content_hash:
        raise StripeCommandConflict("Stripe command conflicted")
    return revision == current_revision


def _current_mapping(mapping, revision):
    if mapping is None or revision <= mapping.get(
        "lifecycleRevision", mapping.get("revision", 0)
    ):
        raise StripeCommandConflict("Stripe command conflicted")
    return mapping


def _checkout_redirect(value, *, require_session=False):
    expected = {"redirectUrl", "expiresAt"} | (
        {"sessionId"} if require_session else set()
    )
    if not isinstance(value, dict) or set(value) != expected:
        raise StripeCommandError("Stripe checkout is unavailable")
    try:
        parsed = urlsplit(value["redirectUrl"])
    except (TypeError, ValueError):
        raise StripeCommandError("Stripe checkout is unavailable") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "checkout.stripe.com"
        or parsed.port not in {None, 443}
        or type(value["expiresAt"]) is not int
        or (require_session and type(value["sessionId"]) is not str)
    ):
        raise StripeCommandError("Stripe checkout is unavailable")


def _portal_redirect(value):
    if not isinstance(value, dict) or set(value) != {"redirectUrl", "expiresAt"}:
        raise StripeCommandError("Stripe portal is unavailable")
    try:
        parsed = urlsplit(value["redirectUrl"])
        port = parsed.port
    except (TypeError, ValueError):
        raise StripeCommandError("Stripe portal is unavailable") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != "billing.stripe.com"
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.startswith("/")
        or type(value["expiresAt"]) is not int
        or value["expiresAt"] < 1
    ):
        raise StripeCommandError("Stripe portal is unavailable")
