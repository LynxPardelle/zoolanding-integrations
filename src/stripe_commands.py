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


_CAPABILITIES = {
    "offer": "prices",
    "product-presentation": "prices",
    "discount": "coupons",
    "discount-lifecycle": "coupons",
    "checkout": "checkout",
    "checkout-status": "checkout",
}
_CHECKOUT_MIN_TTL_SECONDS = 30 * 60
_CHECKOUT_MAX_TTL_SECONDS = 24 * 60 * 60


class StripeCommandService:
    def __init__(self, resolver, store, provider, routes, *, now_epoch):
        if any(value is None for value in (resolver, store, provider, routes, now_epoch)):
            raise StripeCommandError("Stripe command service is unavailable")
        self._resolver = resolver
        self._store = store
        self._provider = provider
        self._routes = routes
        self._now_epoch = now_epoch

    def execute(self, kind: str, command: InternalCommand) -> dict[str, Any]:
        capability = _CAPABILITIES.get(kind)
        if capability is None or type(command) is not InternalCommand or command.kind != kind:
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
        if kind == "checkout" and not (
            now_epoch + _CHECKOUT_MIN_TTL_SECONDS
            <= command.input["checkoutExpiresAt"]
            <= now_epoch + _CHECKOUT_MAX_TTL_SECONDS
        ):
            raise StripeCommandConflict("Stripe command conflicted")
        request_hash = _request_hash(command)
        existing = self._store.claim(
            command.scope,
            command.connection_id,
            command.idempotency_key,
            request_hash,
            command.command_id,
            technical_expiry(now_epoch),
        )
        if existing is not None and existing.get("status") == "accepted":
            if kind == "checkout":
                return self._replay_checkout(command, resolved)
            return {
                "commandId": existing["commandId"],
                "status": "accepted",
            }

        try:
            result, mappings, code_claim = self._perform(kind, command, resolved)
        except StripeCommandConflict:
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
            return None, [{
                **mapping,
                "status": "inactive",
                "lifecycleRevision": value["revision"],
                "lifecycleHash": value["contentHash"],
            }], None

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
        if (
            value["revision"] == previous_revision
            and value["contentHash"] == mapping.get("presentationHash")
        ):
            return None, [], None
        if value["revision"] <= previous_revision:
            raise StripeCommandConflict("Stripe command conflicted")
        self._provider.update_product_presentation(
            resolved,
            mapping["productId"],
            value["snapshot"],
            command.idempotency_key,
        )
        return None, [{
            **mapping,
            "presentationRevision": value["revision"],
            "presentationHash": value["contentHash"],
        }], None

    def _discount(self, command, resolved):
        value = command.input
        snapshot = value["snapshot"]
        code = snapshot.get("customerFacingCode")
        code_hash = _code_hash(code) if code is not None else None
        if code_hash is not None:
            owner = self._store.code_owner(command.scope, command.connection_id, code_hash)
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
            "status": "active",
        }
        if code_hash is not None:
            mapping["codeHash"] = code_hash
        return None, [mapping], code_hash

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
        return None, [{
            **mapping,
            "status": target,
            "lifecycleRevision": value["revision"],
            "lifecycleHash": value["contentHash"],
        }], code_claim

    def _checkout(self, command, resolved):
        value = command.input
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

        promotion_code_id = None
        discount_id = value.get("discountVersionId")
        if discount_id is not None:
            discount = self._mapping(command, "discount", discount_id)
            if discount is None or discount.get("status") != "active":
                raise StripeCommandConflict("Stripe command conflicted")
            eligible = discount["eligibleOfferVersionIds"]
            if eligible and any(offer_id not in eligible for offer_id in offer_ids):
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
        mode = (
            "subscription"
            if value["offerBindings"][0]["snapshot"]["saleType"] == "recurring"
            else "payment"
        )
        mapping = {
            "resourceType": "checkout",
            "resourceId": value["paymentAttemptId"],
            "orderId": value["orderId"],
            "paymentAttemptId": value["paymentAttemptId"],
            "reservationId": value["reservationIds"][0],
            "revision": value["revision"],
            "offerVersionIds": offer_ids,
            "contentHash": command.content_hash,
            "mode": mode,
            "sessionId": result["sessionId"],
            "expiresAt": result["expiresAt"],
            "status": "pending",
        }
        return result, [mapping], None

    def _replay_checkout(self, command, resolved):
        mapping = self._mapping(
            command, "checkout", command.input["paymentAttemptId"]
        )
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


def _same_immutable_version(mapping, revision, content_hash):
    current_revision = mapping.get("revision", 0)
    if revision < current_revision or mapping.get("contentHash") != content_hash:
        raise StripeCommandConflict("Stripe command conflicted")
    return revision == current_revision


def _current_mapping(mapping, revision):
    if mapping is None or revision <= mapping.get("lifecycleRevision", mapping.get("revision", 0)):
        raise StripeCommandConflict("Stripe command conflicted")
    return mapping


def _checkout_redirect(value, *, require_session=False):
    expected = {"redirectUrl", "expiresAt"} | ({"sessionId"} if require_session else set())
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
