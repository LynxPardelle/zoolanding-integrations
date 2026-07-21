import hashlib
import json
import unittest

from src.contracts.internal import validate_command
from src.registry import ResolvedBinding
from tests.test_internal_contracts import canonical_hash, integration_key, offer_command
from tests.test_registry import binding, connection


CAPABILITIES = [
    "connect-onboarding",
    "checkout",
    "one-time-payments",
    "subscriptions",
    "prices",
    "coupons",
    "customer-portal",
]


def resolved(*, tax_mode="stripe-tax"):
    return ResolvedBinding(
        binding(capabilities=CAPABILITIES, tax_mode=tax_mode),
        connection_with_capabilities(),
    )


def connection_with_capabilities():
    current = connection()
    return type(current)(
        scope=current.scope,
        connection_id=current.connection_id,
        provider=current.provider,
        adapter_version=current.adapter_version,
        status=current.status,
        mode=current.mode,
        capabilities=frozenset(CAPABILITIES),
        provider_metadata=current.provider_metadata,
        revision=current.revision,
    )


def command(kind, payload):
    return validate_command(kind, payload)


def presentation_command(revision=1):
    payload = offer_command()
    snapshot = {"displayName": "Landing plan", "displayDescription": "Safe copy"}
    content_hash = canonical_hash(1, snapshot)
    payload["input"] = {
        "resourceId": "offer-v1",
        "revision": revision,
        "schemaVersion": 1,
        "snapshot": snapshot,
        "contentHash": content_hash,
    }
    payload["idempotencyKey"] = integration_key(
        payload["scope"],
        payload["connectionId"],
        "product-presentation",
        "offer-v1",
        revision,
        content_hash,
    )
    return payload


def discount_command(code="WELCOME15", revision=1):
    payload = offer_command()
    snapshot = {
        "schemaVersion": 1,
        "customerFacingCode": code,
        "duration": "once",
        "durationInMonths": None,
        "eligibleOfferVersionIds": ["offer-v1"],
        "redeemByEpoch": 1_900_000_000,
        "redemptionLimit": 100,
        "value": {"basisPoints": 1_500, "type": "percentage"},
    }
    content_hash = canonical_hash(1, snapshot)
    payload["input"] = {
        "resourceId": "discount-v1",
        "revision": revision,
        "schemaVersion": 1,
        "snapshot": snapshot,
        "contentHash": content_hash,
    }
    payload["idempotencyKey"] = integration_key(
        payload["scope"],
        payload["connectionId"],
        "discount",
        "discount-v1",
        revision,
        content_hash,
    )
    return payload


def lifecycle_command(target="retired", revision=2):
    payload = offer_command()
    snapshot = {"targetState": target}
    content_hash = canonical_hash(1, snapshot)
    payload["input"] = {
        "resourceId": "discount-v1",
        "revision": revision,
        "schemaVersion": 1,
        "snapshot": snapshot,
        "contentHash": content_hash,
    }
    payload["idempotencyKey"] = integration_key(
        payload["scope"],
        payload["connectionId"],
        target,
        "discount-v1",
        revision,
        content_hash,
    )
    return payload


def checkout_command(discount=True):
    payload = offer_command()
    snapshot = payload["input"]["snapshot"]
    input_value = {
        "orderId": "order-1",
        "paymentAttemptId": "attempt-1",
        "revision": 1,
        "reservationIds": ["reservation-1"],
        "checkoutExpiresAt": 1_800_002_100,
        "offerBindings": [
            {
                "offerVersionId": "offer-v1",
                "revision": 1,
                "quantity": 2,
                "sellableType": "subscription",
                "snapshot": snapshot,
                "contentHash": canonical_hash(1, snapshot),
            }
        ],
        "taxPolicy": {"mode": "automatic"},
        "shippingPolicy": {"collection": "none"},
        "paymentCollection": "immediate_card_link",
    }
    if discount:
        input_value["discountVersionId"] = "discount-v1"
    content_hash = hashlib.sha256(
        json.dumps(input_value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload["input"] = input_value
    payload["idempotencyKey"] = integration_key(
        payload["scope"],
        payload["connectionId"],
        "checkout",
        "attempt-1",
        1,
        content_hash,
    )
    return payload


def one_time_offer_command(*, currency="MXN"):
    payload = offer_command()
    snapshot = {
        **payload["input"]["snapshot"],
        "amountMinor": 20_000,
        "currency": currency,
        "saleType": "one_time",
        "recurrence": None,
    }
    content_hash = canonical_hash(1, snapshot)
    payload["input"] = {
        "operation": "provision",
        "resourceId": "offer-addon-v1",
        "revision": 1,
        "schemaVersion": 1,
        "snapshot": snapshot,
        "contentHash": content_hash,
    }
    payload["idempotencyKey"] = integration_key(
        payload["scope"],
        payload["connectionId"],
        "provision",
        "offer-addon-v1",
        1,
        content_hash,
    )
    return payload


def checkout_with_add_on(*, add_on_currency="MXN"):
    payload = checkout_command(False)
    add_on_offer = one_time_offer_command(currency=add_on_currency)["input"]
    payload["input"]["offerBindings"].insert(
        0,
        {
            "offerVersionId": "offer-addon-v1",
            "revision": 1,
            "quantity": 1,
            "sellableType": "add_on",
            "snapshot": add_on_offer["snapshot"],
            "contentHash": add_on_offer["contentHash"],
        },
    )
    content_hash = hashlib.sha256(
        json.dumps(payload["input"], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload["idempotencyKey"] = integration_key(
        payload["scope"],
        payload["connectionId"],
        "checkout",
        "attempt-1",
        1,
        content_hash,
    )
    return payload


class Resolver:
    def __init__(self, *, tax_mode="stripe-tax"):
        self.calls = []
        self.tax_mode = tax_mode

    def resolve(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return resolved(tax_mode=self.tax_mode)


class Store:
    def __init__(self):
        self.receipts = {}
        self.mappings = {}
        self.code_claims = {}
        self.operation_claims = {}
        self.persisted_values = []

    def claim(
        self,
        scope,
        connection_id,
        key,
        request_hash,
        command_id,
        expires_at,
        attempted_at,
        operation_claim,
    ):
        identity = (scope.partition_key, connection_id, key)
        existing = self.receipts.get(identity)
        if existing is not None:
            if existing["requestHash"] != request_hash:
                from src.stripe_commands import StripeCommandConflict

                raise StripeCommandConflict("conflict")
            return dict(existing)
        claim_identity = (
            scope.partition_key,
            connection_id,
            operation_claim["resourceType"],
            operation_claim["resourceId"],
            operation_claim["dimension"],
        )
        prior_claim = self.operation_claims.get(claim_identity)
        if prior_claim is not None:
            exact = (
                prior_claim["revision"] == operation_claim["revision"]
                and prior_claim["contentHash"] == operation_claim["contentHash"]
                and prior_claim["requestHash"] == request_hash
                and prior_claim["commandId"] == command_id
            )
            if exact:
                receipt = {
                    "requestHash": request_hash,
                    "commandId": command_id,
                    "status": prior_claim["status"],
                    "attemptedAt": prior_claim["attemptedAt"],
                    "expiresAt": expires_at,
                    "operationClaim": dict(operation_claim),
                }
                self.receipts[identity] = receipt
                return dict(receipt)
            if (
                prior_claim["status"] != "accepted"
                or operation_claim["revision"] <= prior_claim["revision"]
            ):
                from src.stripe_commands import StripeCommandConflict

                raise StripeCommandConflict("conflict")
        receipt = {
            "requestHash": request_hash,
            "commandId": command_id,
            "status": "pending",
            "attemptedAt": attempted_at,
            "expiresAt": expires_at,
            "operationClaim": dict(operation_claim),
        }
        self.receipts[identity] = receipt
        self.operation_claims[claim_identity] = {
            **dict(operation_claim),
            "requestHash": request_hash,
            "commandId": command_id,
            "status": "pending",
            "attemptedAt": attempted_at,
        }
        return None

    def get_mapping(self, scope, connection_id, resource_type, resource_id):
        value = self.mappings.get(
            (scope.partition_key, connection_id, resource_type, resource_id)
        )
        return dict(value) if value is not None else None

    def code_owner(self, scope, connection_id, code_hash):
        return self.code_claims.get((scope.partition_key, connection_id, code_hash))

    def complete(
        self,
        scope,
        connection_id,
        key,
        request_hash,
        result,
        mappings,
        code_claim=None,
    ):
        identity = (scope.partition_key, connection_id, key)
        receipt = self.receipts[identity]
        if receipt["requestHash"] != request_hash:
            raise AssertionError("hash mismatch")
        receipt.update(result)
        operation = receipt["operationClaim"]
        operation_identity = (
            scope.partition_key,
            connection_id,
            operation["resourceType"],
            operation["resourceId"],
            operation["dimension"],
        )
        self.operation_claims[operation_identity]["status"] = "accepted"
        for mapping in mappings:
            map_identity = (
                scope.partition_key,
                connection_id,
                mapping["resourceType"],
                mapping["resourceId"],
            )
            self.mappings[map_identity] = dict(mapping)
            if (
                mapping["resourceType"] == "discount"
                and mapping.get("status") != "active"
                and "codeHash" in mapping
            ):
                self.code_claims.pop(
                    (scope.partition_key, connection_id, mapping["codeHash"]),
                    None,
                )
        if code_claim is not None:
            self.code_claims[(scope.partition_key, connection_id, code_claim)] = (
                mappings[0]["resourceId"]
            )
        self.persisted_values.append((dict(result), [dict(item) for item in mappings]))

    def mark_unknown(self, scope, connection_id, key, request_hash):
        receipt = self.receipts[(scope.partition_key, connection_id, key)]
        receipt["status"] = "unknown"
        operation = receipt["operationClaim"]
        operation_identity = (
            scope.partition_key,
            connection_id,
            operation["resourceType"],
            operation["resourceId"],
            operation["dimension"],
        )
        self.operation_claims[operation_identity]["status"] = "unknown"

    def mark_needs_review(self, scope, connection_id, key, request_hash):
        receipt = self.receipts[(scope.partition_key, connection_id, key)]
        receipt["status"] = "needs_review"
        operation = receipt["operationClaim"]
        operation_identity = (
            scope.partition_key,
            connection_id,
            operation["resourceType"],
            operation["resourceId"],
            operation["dimension"],
        )
        self.operation_claims[operation_identity]["status"] = "needs_review"

    def mark_rejected(self, scope, connection_id, key, request_hash):
        receipt = self.receipts[(scope.partition_key, connection_id, key)]
        receipt["status"] = "rejected"
        operation = receipt["operationClaim"]
        operation_identity = (
            scope.partition_key,
            connection_id,
            operation["resourceType"],
            operation["resourceId"],
            operation["dimension"],
        )
        self.operation_claims[operation_identity]["status"] = "rejected"


class Provider:
    def __init__(self):
        self.calls = []
        self.fail_checkout_once = False

    def create_product(self, resolved_binding, resource_id, idempotency_key):
        self.calls.append(("product", resource_id, idempotency_key))
        return "prod_synthetic01"

    def create_price(
        self, resolved_binding, product_id, snapshot, resource_id, idempotency_key
    ):
        self.calls.append(("price", product_id, snapshot, resource_id, idempotency_key))
        return "price_synthetic01"

    def deactivate_offer(self, resolved_binding, product_id, price_id, idempotency_key):
        self.calls.append(("deactivate-offer", product_id, price_id, idempotency_key))

    def update_product_presentation(
        self, resolved_binding, product_id, snapshot, idempotency_key
    ):
        self.calls.append(("presentation", product_id, snapshot, idempotency_key))

    def create_discount(self, resolved_binding, snapshot, product_ids, idempotency_key):
        self.calls.append(("discount", snapshot, tuple(product_ids), idempotency_key))
        return {"couponId": "couponSynthetic01", "promotionCodeId": "promo_synthetic01"}

    def deactivate_discount(
        self, resolved_binding, coupon_id, promotion_code_id, target, idempotency_key
    ):
        self.calls.append(
            (
                "deactivate-discount",
                coupon_id,
                promotion_code_id,
                target,
                idempotency_key,
            )
        )

    def update_discount_presentation(
        self, resolved_binding, coupon_id, snapshot, idempotency_key
    ):
        self.calls.append(
            (
                "discount-presentation",
                coupon_id,
                snapshot,
                idempotency_key,
            )
        )

    def create_checkout(
        self,
        resolved_binding,
        lines,
        promotion_code_id,
        command_input,
        routes,
        idempotency_key,
    ):
        self.calls.append(
            ("checkout", lines, promotion_code_id, routes, idempotency_key)
        )
        if self.fail_checkout_once:
            self.fail_checkout_once = False
            raise RuntimeError("synthetic provider detail")
        return {
            "sessionId": "cs_test_synthetic01",
            "redirectUrl": "https://checkout.stripe.com/c/pay/synthetic",
            "expiresAt": command_input["checkoutExpiresAt"],
        }

    def retrieve_checkout_handoff(self, resolved_binding, session_id):
        self.calls.append(("retrieve-checkout", session_id))
        return {
            "redirectUrl": "https://checkout.stripe.com/c/pay/synthetic",
            "expiresAt": 1_800_002_100,
        }

    def retrieve_checkout_status(self, resolved_binding, session_id):
        self.calls.append(("checkout-status", session_id))
        return "pending"


class Routes:
    def resolve(self, scope):
        self.scope = scope
        return {
            "successUrl": "https://test.zoolandingpage.com.mx/checkout/success?draftDomain=example.com",
            "cancelUrl": "https://test.zoolandingpage.com.mx/checkout/cancel?draftDomain=example.com",
        }


class StripeCommandTests(unittest.TestCase):
    def setUp(self):
        from src.stripe_commands import StripeCommandService

        self.now = 1_800_000_000
        self.provider = Provider()
        self.store = Store()
        self.resolver = Resolver()
        self.routes = Routes()
        self.service = StripeCommandService(
            self.resolver,
            self.store,
            self.provider,
            self.routes,
            now_epoch=lambda: self.now,
        )

    def provision_offer(self):
        return self.service.execute("offer", command("offer", offer_command()))

    def provision_discount(self, code="WELCOME15"):
        return self.service.execute(
            "discount", command("discount", discount_command(code))
        )

    def test_offer_provisions_product_and_immutable_price_then_replays_without_provider(
        self,
    ):
        result = self.provision_offer()
        replay = self.provision_offer()

        self.assertEqual(result, {"commandId": "command-1", "status": "accepted"})
        self.assertEqual(replay, result)
        self.assertEqual(
            [call[0] for call in self.provider.calls], ["product", "price"]
        )
        mapping = self.store.get_mapping(
            resolved().connection.scope, "stripe-primary", "offer", "offer-v1"
        )
        self.assertEqual(mapping["productId"], "prod_synthetic01")
        self.assertEqual(mapping["priceId"], "price_synthetic01")
        self.assertNotIn("prod_synthetic01", str(result))

    def test_offer_economics_cannot_mutate_an_existing_immutable_version_id(self):
        self.provision_offer()
        payload = offer_command()
        payload["input"]["revision"] = 2
        payload["input"]["snapshot"]["amountMinor"] = 100_000
        payload["input"]["contentHash"] = canonical_hash(
            1, payload["input"]["snapshot"]
        )
        payload["idempotencyKey"] = integration_key(
            payload["scope"],
            payload["connectionId"],
            "provision",
            "offer-v1",
            2,
            payload["input"]["contentHash"],
        )
        with self.assertRaisesRegex(Exception, "conflict"):
            self.service.execute("offer", command("offer", payload))
        self.assertEqual(
            [call[0] for call in self.provider.calls], ["product", "price"]
        )

    def test_offer_presentation_and_deactivation_are_revisioned_separately(self):
        self.provision_offer()
        presentation = self.service.execute(
            "product-presentation",
            command("product-presentation", presentation_command()),
        )
        payload = offer_command()
        lifecycle = {"targetState": "retired"}
        content_hash = canonical_hash(1, lifecycle)
        payload["input"] = {
            "operation": "deactivate",
            "resourceId": "offer-v1",
            "revision": 2,
            "schemaVersion": 1,
            "snapshot": lifecycle,
            "contentHash": content_hash,
        }
        payload["idempotencyKey"] = integration_key(
            payload["scope"],
            "stripe-primary",
            "deactivate",
            "offer-v1",
            2,
            content_hash,
        )
        deactivated = self.service.execute("offer", command("offer", payload))

        self.assertEqual(presentation["status"], "accepted")
        self.assertEqual(deactivated["status"], "accepted")
        mapping = self.store.get_mapping(
            resolved().connection.scope, "stripe-primary", "offer", "offer-v1"
        )
        self.assertEqual(mapping["presentationRevision"], 1)
        self.assertEqual(mapping["status"], "inactive")

    def test_discount_maps_coupon_and_promotion_code_and_casefolds_active_uniqueness(
        self,
    ):
        self.provision_offer()
        self.provision_discount("WELCOME15")
        same_code = discount_command("welcome15", revision=2)
        same_code["input"]["resourceId"] = "discount-v2"
        content_hash = same_code["input"]["contentHash"]
        same_code["idempotencyKey"] = integration_key(
            same_code["scope"],
            "stripe-primary",
            "discount",
            "discount-v2",
            2,
            content_hash,
        )
        with self.assertRaisesRegex(Exception, "conflict"):
            self.service.execute("discount", command("discount", same_code))

        mapping = self.store.get_mapping(
            resolved().connection.scope, "stripe-primary", "discount", "discount-v1"
        )
        self.assertEqual(mapping["couponId"], "couponSynthetic01")
        self.assertEqual(mapping["promotionCodeId"], "promo_synthetic01")
        self.assertEqual(mapping["eligibleOfferVersionIds"], ["offer-v1"])
        self.assertEqual(mapping["duration"], "once")
        self.assertEqual(mapping["redemptionLimit"], 100)
        self.assertEqual(mapping["redeemByEpoch"], 1_900_000_000)
        self.assertEqual(mapping["value"], {"basisPoints": 1_500, "type": "percentage"})
        self.assertNotIn("presentationRevision", mapping)
        self.assertNotIn("presentationHash", mapping)
        self.assertNotIn("WELCOME15", repr(mapping))

    def test_discount_presentation_has_independent_copy_and_does_not_mutate_code(self):
        self.provision_offer()
        self.provision_discount()
        payload = discount_command()
        snapshot = {
            "displayName": "Summer promotion",
            "displayDescription": "Applies to the selected plan.",
        }
        content_hash = canonical_hash(1, snapshot)
        payload["input"] = {
            "operation": "presentation",
            "resourceId": "discount-v1",
            "revision": 1,
            "schemaVersion": 1,
            "snapshot": snapshot,
            "contentHash": content_hash,
        }
        payload["idempotencyKey"] = integration_key(
            payload["scope"],
            payload["connectionId"],
            "discount-presentation",
            "discount-v1",
            1,
            content_hash,
        )

        result = self.service.execute("discount", command("discount", payload))

        self.assertEqual(result["status"], "accepted")
        mapping = self.store.get_mapping(
            resolved().connection.scope, "stripe-primary", "discount", "discount-v1"
        )
        self.assertEqual(mapping["revision"], 1)
        self.assertEqual(mapping["presentationRevision"], 1)
        self.assertEqual(mapping["presentationHash"], content_hash)
        self.assertEqual(mapping["displayName"], "Summer promotion")
        self.assertEqual(mapping["displayDescription"], "Applies to the selected plan.")
        self.assertEqual(mapping["promotionCodeId"], "promo_synthetic01")
        presentation_call = next(
            call for call in self.provider.calls if call[0] == "discount-presentation"
        )
        self.assertNotIn("promo_synthetic", repr(presentation_call))

    def test_discount_lifecycle_deactivates_mapped_resources_and_retains_history(self):
        self.provision_offer()
        self.provision_discount()
        result = self.service.execute(
            "discount-lifecycle",
            command("discount-lifecycle", lifecycle_command()),
        )
        self.assertEqual(result["status"], "accepted")
        mapping = self.store.get_mapping(
            resolved().connection.scope, "stripe-primary", "discount", "discount-v1"
        )
        self.assertEqual(mapping["status"], "retired")
        self.assertIn("couponId", mapping)

    def test_retired_discount_releases_only_its_hashed_active_code_claim(self):
        self.provision_offer()
        self.provision_discount("WELCOME15")
        self.service.execute(
            "discount-lifecycle",
            command("discount-lifecycle", lifecycle_command()),
        )
        replacement = discount_command("welcome15", revision=1)
        replacement["input"]["resourceId"] = "discount-v2"
        replacement["idempotencyKey"] = integration_key(
            replacement["scope"],
            replacement["connectionId"],
            "discount",
            "discount-v2",
            1,
            replacement["input"]["contentHash"],
        )
        result = self.service.execute("discount", command("discount", replacement))
        self.assertEqual(result["status"], "accepted")

    def test_checkout_uses_only_scoped_mapped_prices_discount_and_published_routes(
        self,
    ):
        self.provision_offer()
        self.provision_discount()
        result = self.service.execute(
            "checkout", command("checkout", checkout_command())
        )

        self.assertEqual(result["redirectUrl"].split("/")[2], "checkout.stripe.com")
        checkout_call = next(
            call for call in self.provider.calls if call[0] == "checkout"
        )
        self.assertEqual(
            checkout_call[1], [{"price": "price_synthetic01", "quantity": 2}]
        )
        self.assertEqual(checkout_call[2], "promo_synthetic01")
        self.assertNotIn(result["redirectUrl"], repr(self.store.persisted_values))
        mapping = self.store.get_mapping(
            resolved().connection.scope, "stripe-primary", "checkout", "attempt-1"
        )
        self.assertNotIn("redirectUrl", mapping)
        self.assertNotIn("email", repr(mapping).lower())
        self.assertEqual(mapping["reservationId"], "reservation-1")
        self.assertEqual(mapping["orderId"], "order-1")
        self.assertEqual(mapping["paymentAttemptId"], "attempt-1")
        self.assertEqual(mapping["offerVersionIds"], ["offer-v1"])
        self.assertEqual(mapping["primaryOfferVersionId"], "offer-v1")
        self.assertEqual(mapping["mode"], "subscription")

    def test_subscription_checkout_allows_one_time_add_on_and_tracks_primary(self):
        self.provision_offer()
        self.service.execute("offer", command("offer", one_time_offer_command()))
        result = self.service.execute(
            "checkout", command("checkout", checkout_with_add_on())
        )
        self.assertEqual(result["status"], "accepted")
        mapping = self.store.get_mapping(
            resolved().connection.scope, "stripe-primary", "checkout", "attempt-1"
        )
        self.assertEqual(mapping["offerVersionIds"], ["offer-addon-v1", "offer-v1"])
        self.assertEqual(mapping["primaryOfferVersionId"], "offer-v1")
        self.assertEqual(mapping["mode"], "subscription")
        checkout_call = next(
            call for call in self.provider.calls if call[0] == "checkout"
        )
        self.assertEqual(len(checkout_call[1]), 2)

    def test_checkout_rejects_mixed_line_currencies_before_provider(self):
        self.provision_offer()
        self.service.execute(
            "offer", command("offer", one_time_offer_command(currency="USD"))
        )
        with self.assertRaisesRegex(Exception, "conflict"):
            self.service.execute(
                "checkout",
                command("checkout", checkout_with_add_on(add_on_currency="USD")),
            )
        self.assertFalse(any(call[0] == "checkout" for call in self.provider.calls))

    def test_checkout_rejects_automatic_tax_when_binding_is_unconfigured(self):
        self.resolver.tax_mode = "unconfigured"
        self.provision_offer()
        with self.assertRaisesRegex(Exception, "conflict"):
            self.service.execute(
                "checkout", command("checkout", checkout_command(False))
            )
        self.assertFalse(any(call[0] == "checkout" for call in self.provider.calls))

    def test_checkout_rejects_expired_or_currency_incompatible_discount(self):
        self.provision_offer()
        self.provision_discount()
        mapping_key = (
            resolved().connection.scope.partition_key,
            "stripe-primary",
            "discount",
            "discount-v1",
        )
        self.store.mappings[mapping_key]["redeemByEpoch"] = self.now
        with self.assertRaisesRegex(Exception, "conflict"):
            self.service.execute("checkout", command("checkout", checkout_command()))
        self.assertFalse(any(call[0] == "checkout" for call in self.provider.calls))

        self.store.receipts.clear()
        self.store.operation_claims.clear()
        self.store.mappings[mapping_key]["redeemByEpoch"] = self.now + 1_000
        self.store.mappings[mapping_key]["value"] = {
            "type": "fixed_amount",
            "amountMinor": 1_000,
            "currency": "USD",
        }
        with self.assertRaisesRegex(Exception, "conflict"):
            self.service.execute("checkout", command("checkout", checkout_command()))
        self.assertFalse(any(call[0] == "checkout" for call in self.provider.calls))

    def test_unknown_checkout_retries_the_same_derived_key_and_safe_replay_refetches_url(
        self,
    ):
        self.provision_offer()
        self.provider.fail_checkout_once = True
        parsed = command("checkout", checkout_command(discount=False))
        first = self.service.execute("checkout", parsed)
        second = self.service.execute("checkout", parsed)
        replay = self.service.execute("checkout", parsed)

        self.assertEqual(first["status"], "pending")
        self.assertEqual(second["status"], "accepted")
        self.assertEqual(replay["status"], "accepted")
        keys = [call[-1] for call in self.provider.calls if call[0] == "checkout"]
        self.assertEqual(keys, [parsed.idempotency_key, parsed.idempotency_key])
        self.assertTrue(
            any(call[0] == "retrieve-checkout" for call in self.provider.calls)
        )

    def test_unknown_command_is_never_blindly_retried_at_or_after_24_hours(self):
        self.provision_offer()
        self.provider.fail_checkout_once = True
        parsed = command("checkout", checkout_command(discount=False))
        first = self.service.execute("checkout", parsed)
        self.now += 24 * 60 * 60
        second = self.service.execute("checkout", parsed)

        self.assertEqual(first["status"], "pending")
        self.assertEqual(second, {"commandId": "command-1", "status": "needs_review"})
        self.assertEqual([call[0] for call in self.provider.calls].count("checkout"), 1)

    def test_unknown_command_with_future_attempt_timestamp_requires_review(self):
        self.provision_offer()
        self.provider.fail_checkout_once = True
        parsed = command("checkout", checkout_command(discount=False))
        first = self.service.execute("checkout", parsed)
        receipt = next(
            value
            for value in self.store.receipts.values()
            if value["status"] == "unknown"
        )
        receipt["attemptedAt"] = self.now + 1

        second = self.service.execute("checkout", parsed)

        self.assertEqual(first["status"], "pending")
        self.assertEqual(second, {"commandId": "command-1", "status": "needs_review"})
        self.assertEqual([call[0] for call in self.provider.calls].count("checkout"), 1)

    def test_operation_revision_is_reserved_before_any_provider_mutation(self):
        parsed = command("offer", offer_command())
        self.service.execute("offer", parsed)
        claim = next(iter(self.store.operation_claims.values()))
        self.assertEqual(claim["dimension"], "immutable")
        self.assertEqual(claim["revision"], 1)
        self.assertEqual(claim["contentHash"], parsed.content_hash)
        self.assertEqual(claim["attemptedAt"], self.now)

    def test_durable_mappings_prevent_duplicate_provider_writes_after_receipt_ttl(self):
        self.provision_offer()
        parsed = command("checkout", checkout_command(False))
        self.service.execute("checkout", parsed)
        self.store.receipts.clear()

        offer_replay = self.provision_offer()
        checkout_replay = self.service.execute("checkout", parsed)

        self.assertEqual(offer_replay["status"], "accepted")
        self.assertEqual(checkout_replay["status"], "accepted")
        self.assertEqual([call[0] for call in self.provider.calls].count("product"), 1)
        self.assertEqual([call[0] for call in self.provider.calls].count("checkout"), 1)
        self.assertEqual(
            [call[0] for call in self.provider.calls].count("retrieve-checkout"),
            1,
        )

    def test_checkout_status_is_typed_and_never_returns_provider_ids(self):
        self.provision_offer()
        self.service.execute("checkout", command("checkout", checkout_command(False)))
        payload = offer_command()
        payload["input"] = {
            "orderId": "order-1",
            "paymentAttemptId": "attempt-1",
            "revision": 1,
        }
        result = self.service.execute(
            "checkout-status", command("checkout-status", payload)
        )
        self.assertEqual(
            result,
            {
                "orderId": "order-1",
                "paymentAttemptId": "attempt-1",
                "revision": 1,
                "status": "pending",
            },
        )
        self.assertNotIn("cs_test", repr(result))

    def test_same_business_key_with_a_different_command_id_conflicts(self):
        self.provision_offer()
        changed = offer_command()
        changed["commandId"] = "command-2"
        with self.assertRaisesRegex(Exception, "conflict"):
            self.service.execute("offer", command("offer", changed))

    def test_checkout_expiration_must_match_the_provider_supported_window(self):
        self.provision_offer()
        for expires_at in (1_800_000_001, 1_800_086_401):
            payload = checkout_command(False)
            payload["input"]["checkoutExpiresAt"] = expires_at
            content_hash = hashlib.sha256(
                json.dumps(
                    payload["input"], sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            payload["idempotencyKey"] = integration_key(
                payload["scope"],
                payload["connectionId"],
                "checkout",
                "attempt-1",
                1,
                content_hash,
            )
            with (
                self.subTest(expires_at=expires_at),
                self.assertRaisesRegex(Exception, "conflict"),
            ):
                self.service.execute("checkout", command("checkout", payload))


if __name__ == "__main__":
    unittest.main()
