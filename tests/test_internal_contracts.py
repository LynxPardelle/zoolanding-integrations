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


def offer_command():
    snapshot = {
        "amountMinor": 90000,
        "currency": "MXN",
        "saleType": "subscription",
        "recurrence": {"interval": "month", "intervalCount": 1},
        "taxBehavior": "exclusive",
    }
    return {
        "version": 1,
        "scope": {
            "environment": "test",
            "tenantId": "tenant-example",
            "draftId": "draft-example",
            "domain": "example.com",
        },
        "connectionId": "stripe-primary",
        "commandId": "command-1",
        "idempotencyKey": "retry-1",
        "input": {
            "resourceId": "offer-v1",
            "revision": 1,
            "schemaVersion": 1,
            "snapshot": snapshot,
            "contentHash": canonical_hash(1, snapshot),
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
