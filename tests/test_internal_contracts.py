import hashlib
import importlib
import importlib.util
import json
import os
import unittest
from unittest.mock import patch

from src.domain.operations import COMMERCE_EVENT_TYPES
from src.common.published_policy import ResolvedIntegrationPolicy
from src.registry import ResolvedBinding
from tests.test_registry import binding, connection


ALLOWED_CALLER = "arn:aws:iam::123456789012:role/zoolanding-commerce-test"


def contracts_module(testcase):
    try:
        spec = importlib.util.find_spec("src.contracts.internal")
    except ModuleNotFoundError:
        spec = None
    testcase.assertIsNotNone(spec, "closed internal contracts are not implemented")
    return importlib.import_module("src.contracts.internal")


def handler_module(testcase, name):
    module_name = f"src.handlers.{name}"
    testcase.assertIsNotNone(
        importlib.util.find_spec(module_name),
        f"{name} seam is not implemented",
    )
    return importlib.import_module(module_name)


def canonical_hash(schema_version, snapshot):
    return hashlib.sha256(
        json.dumps(
            {"schemaVersion": schema_version, "snapshot": snapshot},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def integration_key(scope, connection_id, operation, resource_id, revision, content_hash):
    digest = hashlib.sha256(
        json.dumps(
            {
                "scope": scope,
                "connectionId": connection_id,
                "operation": operation,
                "resourceId": resource_id,
                "revision": revision,
                "contentHash": content_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"integrations-command-v1:{digest}"


def offer_command():
    snapshot = {
        "schemaVersion": 1,
        "amountMinor": 90000,
        "billingScheme": "per_unit",
        "currency": "MXN",
        "saleType": "recurring",
        "recurrence": {
            "interval": "month",
            "intervalCount": 1,
            "usageType": "licensed",
        },
        "taxBehavior": "exclusive",
    }
    scope = {
        "environment": "test",
        "tenantId": "tenant-example",
        "draftId": "draft-example",
        "domain": "example.com",
    }
    content_hash = canonical_hash(1, snapshot)
    return {
        "version": 1,
        "scope": scope,
        "connectionId": "stripe-primary",
        "commandId": "command-1",
        "idempotencyKey": integration_key(
            scope, "stripe-primary", "provision", "offer-v1", 1, content_hash
        ),
        "input": {
            "operation": "provision",
            "resourceId": "offer-v1",
            "revision": 1,
            "schemaVersion": 1,
            "snapshot": snapshot,
            "contentHash": content_hash,
        },
    }


def request(path, payload, *, caller=ALLOWED_CALLER, method="POST"):
    return {
        "rawPath": path,
        "requestContext": {
            "http": {"method": method},
            "requestId": "request-1",
            "identity": {"userArn": caller},
        },
        "body": json.dumps(payload),
        "isBase64Encoded": False,
    }


class CommandService:
    def __init__(self, result=None):
        self.result = result or {"commandId": "command-1", "status": "accepted"}
        self.calls = []

    def execute(self, kind, command):
        self.calls.append((kind, command))
        return self.result


class InternalContractTests(unittest.TestCase):
    def test_offer_snapshot_is_closed_hashed_and_rejects_provider_ids_or_urls(self):
        contracts = contracts_module(self)
        parsed = contracts.validate_command("offer", offer_command())
        self.assertEqual(parsed.scope.draft_id, "draft-example")
        self.assertEqual(parsed.content_hash, offer_command()["input"]["contentHash"])

        for forbidden in (
            {"accountId": "acct_browser"},
            {"priceId": "price_browser"},
            {"returnUrl": "https://evil.example"},
        ):
            payload = offer_command()
            payload["input"].update(forbidden)
            with (
                self.subTest(field=next(iter(forbidden))),
                self.assertRaises(contracts.ContractError),
            ):
                contracts.validate_command("offer", payload)

        wrong_hash = offer_command()
        wrong_hash["input"]["contentHash"] = "0" * 64
        with self.assertRaises(contracts.ContractError):
            contracts.validate_command("offer", wrong_hash)

    def test_offer_deactivate_is_closed_and_requires_a_verified_lifecycle_hash(self):
        contracts = contracts_module(self)
        payload = offer_command()
        lifecycle = {"targetState": "retired"}
        payload["input"] = {
            "operation": "deactivate",
            "resourceId": "offer-v1",
            "revision": 2,
            "schemaVersion": 1,
            "snapshot": lifecycle,
            "contentHash": canonical_hash(1, lifecycle),
        }
        payload["idempotencyKey"] = integration_key(
            payload["scope"],
            payload["connectionId"],
            "deactivate",
            "offer-v1",
            2,
            payload["input"]["contentHash"],
        )

        parsed = contracts.validate_command("offer", payload)
        self.assertEqual(parsed.input["operation"], "deactivate")
        for invalid in (
            {**payload["input"], "operation": "archive"},
            {**payload["input"], "contentHash": "0" * 64},
            {**payload["input"], "priceId": "price_browser"},
        ):
            changed = offer_command()
            changed["input"] = invalid
            with self.assertRaises(contracts.ContractError):
                contracts.validate_command("offer", changed)

    def test_checkout_command_contains_only_trusted_snapshots_and_closed_policy(self):
        contracts = contracts_module(self)
        payload = offer_command()
        snapshot = offer_command()["input"]["snapshot"]
        payload["input"] = {
            "orderId": "order-1",
            "paymentAttemptId": "attempt-1",
            "revision": 1,
            "reservationIds": ["reservation-1"],
            "checkoutExpiresAt": 1_800_002_100,
            "offerBindings": [
                {
                    "offerVersionId": "offer-v1",
                    "revision": 1,
                    "quantity": 1,
                    "sellableType": "subscription",
                    "snapshot": snapshot,
                    "contentHash": canonical_hash(1, snapshot),
                }
            ],
            "taxPolicy": {"mode": "automatic"},
            "shippingPolicy": {"collection": "none"},
            "paymentCollection": "immediate_card_link",
        }
        payload["idempotencyKey"] = integration_key(
            payload["scope"],
            payload["connectionId"],
            "checkout",
            "attempt-1",
            1,
            hashlib.sha256(
                json.dumps(
                    payload["input"], sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest(),
        )

        parsed = contracts.validate_command("checkout", payload)
        self.assertEqual(parsed.input["reservationIds"], ["reservation-1"])
        self.assertRegex(parsed.content_hash, r"^[a-f0-9]{64}$")
        for field, value in (
            ("successUrl", "https://evil.example/success"),
            ("priceId", "price_browser"),
            ("customerEmail", "person@example.com"),
        ):
            changed = json.loads(json.dumps(payload))
            changed["input"][field] = value
            with self.subTest(field=field), self.assertRaises(contracts.ContractError):
                contracts.validate_command("checkout", changed)

        multiple_reservations = json.loads(json.dumps(payload))
        multiple_reservations["input"]["reservationIds"].append("reservation-2")
        with self.assertRaises(contracts.ContractError):
            contracts.validate_command("checkout", multiple_reservations)

        multiple_recurring_offers = json.loads(json.dumps(payload))
        second = dict(multiple_recurring_offers["input"]["offerBindings"][0])
        second["offerVersionId"] = "offer-v2"
        multiple_recurring_offers["input"]["offerBindings"].append(second)
        with self.assertRaises(contracts.ContractError):
            contracts.validate_command("checkout", multiple_recurring_offers)

    def test_typed_redirect_and_checkout_status_responses_are_safe(self):
        contracts = contracts_module(self)
        command = contracts.validate_command("checkout", self._checkout_command())
        result = contracts.validate_service_result(
            {
                "commandId": command.command_id,
                "status": "accepted",
                "redirectUrl": "https://checkout.stripe.com/c/pay/synthetic",
                "expiresAt": 1_800_002_100,
            },
            command,
        )
        self.assertEqual(result["status"], "accepted")
        for unsafe in (
            "http://checkout.stripe.com/c/pay/synthetic",
            "https://evil.example/c/pay/synthetic",
            "https://user@checkout.stripe.com/c/pay/synthetic",
        ):
            with self.subTest(url=unsafe), self.assertRaises(contracts.ContractError):
                contracts.validate_service_result(
                    {
                        "commandId": command.command_id,
                        "status": "accepted",
                        "redirectUrl": unsafe,
                        "expiresAt": 1_800_002_100,
                    },
                    command,
                )

        status_payload = offer_command()
        status_payload["input"] = {
            "orderId": "order-1",
            "paymentAttemptId": "attempt-1",
            "revision": 1,
        }
        status_command = contracts.validate_command("checkout-status", status_payload)
        self.assertEqual(
            contracts.validate_service_result(
                {
                    "orderId": "order-1",
                    "paymentAttemptId": "attempt-1",
                    "revision": 1,
                    "status": "pending",
                },
                status_command,
            )["status"],
            "pending",
        )
        with self.assertRaises(contracts.ContractError):
            contracts.validate_service_result(
                {
                    "orderId": "order-1",
                    "paymentAttemptId": "attempt-1",
                    "revision": 1,
                    "status": "paid",
                    "sessionId": "cs_test_synthetic",
                },
                status_command,
            )

    def test_phase4_idempotency_is_derived_and_scope_bound(self):
        contracts = contracts_module(self)
        payload = offer_command()
        parsed = contracts.validate_command("offer", payload)
        self.assertEqual(parsed.idempotency_key, payload["idempotencyKey"])

        for mutate in (
            lambda value: value["scope"].update({"draftId": "draft-other"}),
            lambda value: value["input"].update({"revision": 2}),
            lambda value: value.update({"idempotencyKey": "retry-arbitrary"}),
        ):
            changed = json.loads(json.dumps(payload))
            mutate(changed)
            with self.assertRaises(contracts.ContractError):
                contracts.validate_command("offer", changed)

    def test_subscription_policy_commands_are_closed_before_task45(self):
        contracts = contracts_module(self)
        payload = offer_command()
        payload["input"] = {
            "subscriptionId": "subscription-1",
            "expectedRevision": 2,
            "targetOfferVersionId": "offer-v2",
            "planChangePolicy": {"mode": "immediate-prorated"},
            "previewTimestamp": 1_800_000_000,
        }
        payload["idempotencyKey"] = integration_key(
            payload["scope"],
            payload["connectionId"],
            "subscription-change",
            "subscription-1",
            2,
            hashlib.sha256(
                json.dumps(
                    payload["input"], sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest(),
        )
        contracts.validate_command("subscription-change", payload)
        changed = json.loads(json.dumps(payload))
        changed["input"]["planChangePolicy"] = {"mode": "operator-selectable"}
        with self.assertRaises(contracts.ContractError):
            contracts.validate_command("subscription-change", changed)

        pause = offer_command()
        pause["input"] = {
            "subscriptionId": "subscription-1",
            "expectedRevision": 2,
            "action": "pause",
            "pausePolicy": {
                "enabled": True,
                "newInvoiceBehavior": "void",
                "existingInvoiceBehavior": "unchanged",
                "accessBehavior": "suspend",
                "resume": {"mode": "manual"},
                "onResume": {
                    "collection": "restore",
                    "access": "restore-if-suspended",
                },
            },
        }
        pause["idempotencyKey"] = integration_key(
            pause["scope"],
            pause["connectionId"],
            "pause",
            "subscription-1",
            2,
            hashlib.sha256(
                json.dumps(
                    pause["input"], sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest(),
        )
        contracts.validate_command("subscription-pause", pause)
        pause["input"]["pausePolicy"]["resume"] = {"mode": "automatic"}
        with self.assertRaises(contracts.ContractError):
            contracts.validate_command("subscription-pause", pause)

    def _checkout_command(self):
        payload = offer_command()
        snapshot = payload["input"]["snapshot"]
        payload["input"] = {
            "orderId": "order-1",
            "paymentAttemptId": "attempt-1",
            "revision": 1,
            "reservationIds": ["reservation-1"],
            "checkoutExpiresAt": 1_800_002_100,
            "offerBindings": [
                {
                    "offerVersionId": "offer-v1",
                    "revision": 1,
                    "quantity": 1,
                    "sellableType": "subscription",
                    "snapshot": snapshot,
                    "contentHash": canonical_hash(1, snapshot),
                }
            ],
            "taxPolicy": {"mode": "automatic"},
            "shippingPolicy": {"collection": "none"},
            "paymentCollection": "immediate_card_link",
        }
        content_hash = hashlib.sha256(
            json.dumps(
                payload["input"], sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
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

    def test_discount_and_operation_values_are_typed_not_just_key_closed(self):
        contracts = contracts_module(self)
        discount = offer_command()
        snapshot = {
            "discountType": "percentage",
            "duration": "once",
            "percentageBasisPoints": "not-an-integer",
            "amountMinor": 500,
        }
        discount["input"] = {
            "resourceId": "discount-v1",
            "revision": 1,
            "schemaVersion": 1,
            "snapshot": snapshot,
            "contentHash": canonical_hash(1, snapshot),
        }
        with self.assertRaises(contracts.ContractError):
            contracts.validate_command("discount", discount)

        pause = offer_command()
        pause["input"] = {
            "subscriptionId": "subscription-1",
            "expectedRevision": 1,
            "action": {"unexpected": "object"},
        }
        with self.assertRaises(contracts.ContractError):
            contracts.validate_command("subscription-pause", pause)

        unhashable_snapshot = {
            "discountType": "percentage",
            "duration": "once",
            "percentageBasisPoints": 100,
            "eligibleOfferVersionIds": [{"invalid": True}],
        }
        unhashable_discount = offer_command()
        unhashable_discount["input"] = {
            "resourceId": "discount-v2",
            "revision": 1,
            "schemaVersion": 1,
            "snapshot": unhashable_snapshot,
            "contentHash": canonical_hash(1, unhashable_snapshot),
        }
        with self.assertRaises(contracts.ContractError):
            contracts.validate_command("discount", unhashable_discount)

    def test_offer_seam_enforces_exact_aws_caller_and_validates_response(self):
        handler = handler_module(self, "internal_stripe_offer")
        service = CommandService()
        denied = handler.handle_request(
            request(
                handler.PATH,
                offer_command(),
                caller="arn:aws:iam::123456789012:role/other",
            ),
            service=service,
            allowed_callers={ALLOWED_CALLER},
        )
        self.assertEqual(denied["statusCode"], 403)
        self.assertEqual(service.calls, [])

        accepted = handler.handle_request(
            request(handler.PATH, offer_command()),
            service=service,
            allowed_callers={ALLOWED_CALLER},
        )
        self.assertEqual(accepted["statusCode"], 200)
        self.assertEqual(service.calls[0][0], "offer")
        self.assertEqual(json.loads(accepted["body"])["data"]["status"], "accepted")
        self.assertEqual(accepted["headers"]["Cache-Control"], "no-store")

        invalid_service = CommandService(
            {"status": "success", "providerId": "price_synthetic"}
        )
        invalid = handler.handle_request(
            request(handler.PATH, offer_command()),
            service=invalid_service,
            allowed_callers={ALLOWED_CALLER},
        )
        self.assertEqual(invalid["statusCode"], 503)
        self.assertNotIn("price_synthetic", invalid["body"])

    def test_checkout_seam_preserves_typed_ephemeral_redirect_and_conflicts(self):
        checkout_handler = handler_module(self, "internal_stripe_checkout")
        redirect = checkout_handler.handle_request(
            request(checkout_handler.PATH, self._checkout_command()),
            service=CommandService(
                {
                    "commandId": "command-1",
                    "status": "accepted",
                    "redirectUrl": "https://checkout.stripe.com/c/pay/synthetic",
                    "expiresAt": 1_800_002_100,
                }
            ),
            allowed_callers={ALLOWED_CALLER},
        )
        self.assertEqual(redirect["statusCode"], 200)
        self.assertEqual(
            json.loads(redirect["body"])["data"]["redirectUrl"],
            "https://checkout.stripe.com/c/pay/synthetic",
        )
        self.assertEqual(redirect["headers"]["Cache-Control"], "no-store")

        class ConflictService:
            def execute(self, kind, command):
                del kind, command
                from src.stripe_commands import StripeCommandConflict

                raise StripeCommandConflict("provider detail must not escape")

        conflict = checkout_handler.handle_request(
            request(checkout_handler.PATH, self._checkout_command()),
            service=ConflictService(),
            allowed_callers={ALLOWED_CALLER},
        )
        self.assertEqual(conflict["statusCode"], 409)
        self.assertNotIn("provider detail", conflict["body"])

    def test_task_041_042_lambda_entrypoints_use_the_command_runtime(self):
        names = (
            "internal_stripe_offer",
            "internal_stripe_product_presentation",
            "internal_stripe_discount",
            "internal_stripe_discount_lifecycle",
            "internal_stripe_checkout",
            "internal_stripe_checkout_status",
        )
        for name in names:
            module = handler_module(self, name)
            self.assertTrue(hasattr(module, "_runtime_dependencies"), name)

        handler = handler_module(self, "internal_stripe_offer")
        service = CommandService()
        with patch.object(
            handler,
            "_runtime_dependencies",
            return_value={
                "service": service,
                "allowed_callers": {ALLOWED_CALLER},
            },
        ):
            result = handler.lambda_handler(
                request(handler.PATH, offer_command()), None
            )
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(service.calls[0][0], "offer")

    def test_all_task_040_seams_exist_with_literal_paths_and_migrations_fail_closed(
        self,
    ):
        expected = {
            "internal_stripe_offer": "/internal/v1/stripe/offer",
            "internal_stripe_product_presentation": "/internal/v1/stripe/product-presentation",
            "internal_stripe_discount": "/internal/v1/stripe/discount",
            "internal_stripe_discount_lifecycle": "/internal/v1/stripe/discount-lifecycle",
            "internal_stripe_checkout": "/internal/v1/stripe/checkout",
            "internal_stripe_checkout_status": "/internal/v1/stripe/checkout-status",
            "internal_stripe_subscription_change": "/internal/v1/stripe/subscription/change",
            "internal_stripe_subscription_discount": "/internal/v1/stripe/subscription/discount",
            "internal_stripe_subscription_pause": "/internal/v1/stripe/subscription/pause",
            "internal_stripe_customer_portal": "/internal/v1/stripe/customer-portal",
            "internal_stripe_migrations_preview": "/internal/v1/stripe/migrations/preview",
            "internal_stripe_migrations_execute": "/internal/v1/stripe/migrations/execute",
            "internal_stripe_migrations_control": "/internal/v1/stripe/migrations/control",
            "internal_stripe_migrations_status": "/internal/v1/stripe/migrations/status",
            "internal_connection_register": "/internal/v1/integrations/connection-register",
            "internal_connection_resolve": "/internal/v1/integrations/connection-resolve",
        }
        modules = {name: handler_module(self, name) for name in expected}
        self.assertEqual(
            {name: module.PATH for name, module in modules.items()}, expected
        )

        preview = offer_command()
        preview["input"] = {
            "sourceOfferVersionId": "offer-source",
            "targetOfferVersionId": "offer-target",
            "commercialRequestId": "commercial-request-1",
        }
        result = modules["internal_stripe_migrations_preview"].handle_request(
            request(expected["internal_stripe_migrations_preview"], preview),
            allowed_callers={ALLOWED_CALLER},
        )
        self.assertEqual(result["statusCode"], 503)

    def test_connection_registration_contract_rejects_secret_values(self):
        contracts = contracts_module(self)
        payload = {
            "version": 1,
            "scope": offer_command()["scope"],
            "connectionId": "stripe-primary",
            "commandId": "command-1",
            "idempotencyKey": "retry-1",
            "credentialReference": "/zoolanding/test/integrations/tenant-example/draft-example/stripe/stripe-primary",
            "provider": "stripe",
            "mode": "test",
            "capabilities": ["checkout"],
            "accountReference": "acct_synthetic",
        }
        contracts.validate_connection_registration(payload)
        payload["secretValue"] = "synthetic"
        with self.assertRaises(contracts.ContractError):
            contracts.validate_connection_registration(payload)
        payload.pop("secretValue")
        payload["provider"] = {"invalid": True}
        with self.assertRaises(contracts.ContractError):
            contracts.validate_connection_registration(payload)

    def test_event_contract_is_exactly_the_four_existing_commerce_names(self):
        self.assertEqual(
            COMMERCE_EVENT_TYPES,
            frozenset(
                {
                    "commerce.payment.succeeded.v1",
                    "commerce.payment.terminal_unpaid.v1",
                    "commerce.refund.confirmed.v1",
                    "commerce.subscription.updated.v1",
                }
            ),
        )

    def test_connection_register_and_resolve_are_typed_scoped_and_server_only(self):
        register_handler = handler_module(self, "internal_connection_register")
        resolve_handler = handler_module(self, "internal_connection_resolve")
        self.assertIsNotNone(
            importlib.util.find_spec("src.internal_connections"),
            "typed internal connection services are not implemented",
        )
        services = importlib.import_module("src.internal_connections")
        published_binding = binding()
        published = ResolvedIntegrationPolicy(
            scope=published_binding.scope,
            version_id="version-1",
            prefix="sites/example.com/versions/version-1/",
            bindings=(published_binding,),
            admin_access={"mode": "none"},
            auth_registry={},
        )

        class PolicyResolver:
            def resolve(self, **kwargs):
                self.kwargs = kwargs
                return published

        class Admin:
            def __init__(self):
                self.calls = []

            def register(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return {
                    "connectionId": "stripe-primary",
                    "status": "pending",
                    "mode": "test",
                    "revision": 1,
                }

        registration = {
            "version": 1,
            "scope": offer_command()["scope"],
            "connectionId": "stripe-primary",
            "commandId": "command-1",
            "idempotencyKey": "retry-1",
            "credentialReference": "/zoolanding/test/integrations/tenant-example/draft-example/stripe/stripe-primary",
            "provider": "stripe",
            "mode": "test",
            "capabilities": ["connect-onboarding", "checkout"],
            "accountReference": "acct_synthetic",
        }
        admin = Admin()
        registered = register_handler.handle_request(
            request(register_handler.PATH, registration),
            service=services.ConnectionRegistrationService(PolicyResolver(), admin),
            allowed_callers={ALLOWED_CALLER},
        )
        self.assertEqual(registered["statusCode"], 200)
        self.assertNotIn("credentialReference", registered["body"])
        self.assertEqual(admin.calls[0][0][0].scope.draft_id, "draft-example")

        class Resolver:
            def resolve(self, *args, **kwargs):
                self.calls = (args, kwargs)
                return ResolvedBinding(published_binding, connection())

        resolution_command = offer_command()
        resolution_command["input"] = {
            "provider": "stripe",
            "capability": "checkout",
        }
        resolved = resolve_handler.handle_request(
            request(resolve_handler.PATH, resolution_command),
            service=services.ConnectionResolutionService(Resolver()),
            allowed_callers={ALLOWED_CALLER},
        )
        self.assertEqual(resolved["statusCode"], 200)
        resolved_body = json.loads(resolved["body"])["data"]
        self.assertEqual(resolved_body["provider"], "stripe")
        self.assertIn("credentialReference", resolved_body)
        self.assertNotIn("acct_synthetic", resolved["body"])

        class CrossScopeResolution:
            def resolve(self, command):
                return {
                    "connectionId": command.connection_id,
                    "provider": "stripe",
                    "mode": "test",
                    "adapterVersion": "v1",
                    "credentialReference": "/zoolanding/test/integrations/tenant-other/draft-other/stripe/stripe-primary",
                }

        rejected_resolution = resolve_handler.handle_request(
            request(resolve_handler.PATH, resolution_command),
            service=CrossScopeResolution(),
            allowed_callers={ALLOWED_CALLER},
        )
        self.assertEqual(rejected_resolution["statusCode"], 503)

        register_handler._runtime_dependencies = lambda: {
            "service": services.ConnectionRegistrationService(PolicyResolver(), admin)
        }
        resolve_handler._runtime_dependencies = lambda: {
            "service": services.ConnectionResolutionService(Resolver())
        }
        with patch.dict(os.environ, {"INTERNAL_CALLER_ARNS": ALLOWED_CALLER}):
            registered_entrypoint = register_handler.lambda_handler(
                request(register_handler.PATH, registration), None
            )
            resolved_entrypoint = resolve_handler.lambda_handler(
                request(resolve_handler.PATH, resolution_command), None
            )
        self.assertEqual(registered_entrypoint["statusCode"], 200)
        self.assertEqual(resolved_entrypoint["statusCode"], 200)


if __name__ == "__main__":
    unittest.main()
