import importlib
import importlib.util
import json
import copy
import unittest

from tests.test_authorization import auth_store, event as auth_event, policies
from tests.test_registry import connection


def handler_module(testcase, name):
    module_name = f"src.handlers.{name}"
    testcase.assertIsNotNone(
        importlib.util.find_spec(module_name),
        f"{name} handler is not implemented",
    )
    return importlib.import_module(module_name)


class Resolver:
    def __init__(self, resolved=None):
        self.resolved = resolved or policies()
        self.calls = []

    def resolve(self, **kwargs):
        self.calls.append(kwargs)
        return self.resolved


class Registry:
    def __init__(self):
        self.updated = []

    def list_connections(self, resolved_scope):
        self.scope = resolved_scope
        return (connection(),)

    def update_status(self, resolved_scope, connection_id, status, expected_revision):
        self.updated.append((resolved_scope, connection_id, status, expected_revision))
        return connection(status=status)

    def connection(self, resolved_scope, connection_id):
        self.read = (resolved_scope, connection_id)
        return connection()

    def disable_stripe_account(
        self, resolved_scope, connection_id, account_reference, expected_revision
    ):
        self.disabled = (
            resolved_scope,
            connection_id,
            account_reference,
            expected_revision,
        )
        return connection(status="disabled", account=None, ready=False)


class BindingResolver:
    def __init__(self):
        self.calls = []

    def resolve(self, *args, **kwargs):
        from src.registry import ResolvedBinding
        from tests.test_registry import binding

        self.calls.append((args, kwargs))
        return ResolvedBinding(binding(), connection())


class OnboardingService:
    def __init__(self):
        self.calls = []

    def start(self, resolved, context, now_epoch):
        self.calls.append(("start", resolved, context, now_epoch))
        return {"handoffUrl": "https://connect.stripe.com/setup/synthetic"}

    def complete_return(self, resolved, context, state, now_epoch, **outcome):
        self.calls.append(("return", resolved, context, state, now_epoch, outcome))
        return {
            "status": "ready",
            "chargesEnabled": True,
            "detailsSubmitted": True,
            "requirementsDueCount": 0,
        }

    def deauthorize(self, resolved, context, now_epoch):
        self.calls.append(("deauthorize", resolved, context, now_epoch))
        return {"status": "disabled"}


def request(path, body, *, csrf=True):
    value = auth_event(csrf=csrf)
    value.update(
        {
            "rawPath": path,
            "requestContext": {
                "http": {"method": "POST"},
                "requestId": "request-1",
            },
            "body": json.dumps(body),
            "isBase64Encoded": False,
        }
    )
    return value


def body(response):
    return json.loads(response["body"])


class BrowserHandlerTests(unittest.TestCase):
    def test_read_and_action_lambda_entrypoints_use_server_runtime_dependencies(self):
        read_handler = handler_module(self, "connection_read")
        action_handler = handler_module(self, "connection_action")
        registry = Registry()
        dependencies = {
            "policy_resolver": Resolver(),
            "auth_store": auth_store(),
            "registry": registry,
            "environment": "test",
            "now_epoch": 1_000,
        }
        read_handler._runtime_dependencies = lambda: dependencies
        action_handler._runtime_dependencies = lambda: dependencies

        read = read_handler.lambda_handler(
            request("/features/integrations/read", {"operation": "list"}), None
        )
        action = action_handler.lambda_handler(
            request(
                "/features/integrations/action",
                {
                    "operation": "disable",
                    "input": {
                        "connectionId": "stripe-primary",
                        "expectedRevision": 1,
                    },
                },
            ),
            None,
        )

        self.assertEqual(read["statusCode"], 200)
        self.assertEqual(action["statusCode"], 200)

    def test_connection_read_is_sanitized_no_store_and_scope_derived(self):
        handler = handler_module(self, "connection_read")
        registry = Registry()
        resolver = Resolver()

        response = handler.handle_request(
            request("/features/integrations/read", {"operation": "list"}),
            policy_resolver=resolver,
            auth_store=auth_store(),
            registry=registry,
            environment="test",
            now_epoch=1_000,
        )

        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(response["headers"]["Cache-Control"], "no-store")
        result = body(response)
        self.assertEqual(
            result["data"]["connections"][0]["connectionId"], "stripe-primary"
        )
        self.assertNotIn("acct_synthetic", response["body"])
        self.assertNotIn("credentialReference", response["body"])
        self.assertEqual(registry.scope.draft_id, "draft-example")
        self.assertEqual(
            resolver.calls, [{"environment": "test", "domain": "example.com"}]
        )
        malformed = handler.handle_request(
            request("/features/integrations/read", {"operation": {"invalid": True}}),
            policy_resolver=resolver,
            auth_store=auth_store(),
            registry=registry,
            environment="test",
            now_epoch=1_000,
        )
        self.assertEqual(malformed["statusCode"], 422)

    def test_connection_action_requires_manage_and_csrf_then_updates_exact_scope(self):
        handler = handler_module(self, "connection_action")
        registry = Registry()
        payload = {
            "operation": "disable",
            "input": {
                "connectionId": "stripe-primary",
                "expectedRevision": 1,
            },
        }
        denied = handler.handle_request(
            request("/features/integrations/action", payload, csrf=False),
            policy_resolver=Resolver(),
            auth_store=auth_store(),
            registry=registry,
            environment="test",
            now_epoch=1_000,
        )
        self.assertEqual(denied["statusCode"], 403)

        response = handler.handle_request(
            request("/features/integrations/action", payload),
            policy_resolver=Resolver(),
            auth_store=auth_store(),
            registry=registry,
            environment="test",
            now_epoch=1_000,
        )
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(registry.disabled[0].draft_id, "draft-example")
        self.assertEqual(registry.disabled[1:], ("stripe-primary", "acct_synthetic", 1))

        reconnect = {**payload, "operation": "requestReconnect"}
        response = handler.handle_request(
            request("/features/integrations/action", reconnect),
            policy_resolver=Resolver(),
            auth_store=auth_store(),
            registry=registry,
            environment="test",
            now_epoch=1_000,
        )
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(registry.updated[-1][1:], ("stripe-primary", "pending", 1))

    def test_browser_cannot_mark_a_pending_connection_active(self):
        handler = handler_module(self, "connection_action")
        registry = Registry()
        for payload in (
            {
                "operation": "setStatus",
                "input": {
                    "connectionId": "stripe-primary",
                    "status": "active",
                    "expectedRevision": 1,
                },
            },
            {
                "operation": "active",
                "input": {"connectionId": "stripe-primary", "expectedRevision": 1},
            },
        ):
            with self.subTest(payload=payload):
                response = handler.handle_request(
                    request("/features/integrations/action", payload),
                    policy_resolver=Resolver(),
                    auth_store=auth_store(),
                    registry=registry,
                    environment="test",
                    now_epoch=1_000,
                )
                self.assertEqual(response["statusCode"], 422)
        self.assertEqual(registry.updated, [])

    def test_browser_cannot_mutate_smtp_status_or_orphan_isolation_claims(self):
        from src.contracts.internal import validate_smtp_connection_activation
        from src.registry import ConnectionRegistry
        from src.smtp_activation import SmtpConnectionActivationService
        from tests.test_registry import MemoryBackend
        from tests.test_smtp_activation import (
            TEST_ACCOUNT_HASH,
            Secrets,
            activation_payload,
            binding as smtp_binding,
            pending_connection,
            secret_metadata,
        )

        selected_scope = policies().scope
        smtp = pending_connection(selected_scope)
        backend = MemoryBackend()
        registry = ConnectionRegistry(backend)
        registry.register(smtp, smtp_binding(selected_scope), "register-smtp")
        SmtpConnectionActivationService(
            registry, Secrets(secret_metadata(smtp)), TEST_ACCOUNT_HASH
        ).activate(
            validate_smtp_connection_activation(
                activation_payload(selected_scope)
            )
        )
        before = copy.deepcopy(backend.records)
        handler = handler_module(self, "connection_action")
        for operation in ("disable", "requestReconnect"):
            response = handler.handle_request(
                request(
                    handler.PATH,
                    {
                        "operation": operation,
                        "input": {
                            "connectionId": "billing-mailbox",
                            "expectedRevision": 2,
                        },
                    },
                ),
                policy_resolver=Resolver(),
                auth_store=auth_store(),
                registry=registry,
                environment="test",
                now_epoch=1_000,
            )
            self.assertEqual(response["statusCode"], 422)
            self.assertEqual(backend.records, before)
        current = registry.connection(selected_scope, "billing-mailbox")
        self.assertEqual((current.status, current.revision), ("active", 2))
        self.assertTrue(
            any(
                item.get("claimType") == "credential-isolation"
                for item in backend.records.values()
            )
        )

    def test_onboarding_lambda_entrypoint_uses_runtime_dependencies(self):
        handler = handler_module(self, "stripe_onboarding")
        service = OnboardingService()
        handler._runtime_dependencies = lambda: {
            "policy_resolver": Resolver(),
            "auth_store": auth_store(),
            "binding_resolver": BindingResolver(),
            "onboarding_service": service,
            "environment": "test",
            "now_epoch": 1_000,
        }
        response = handler.lambda_handler(
            request(
                handler.PATH,
                {"operation": "start", "input": {"bindingId": "stripe-primary"}},
            ),
            None,
        )
        self.assertEqual(response["statusCode"], 200)
        self.assertEqual(service.calls[0][0], "start")

    def test_onboarding_accepts_no_account_or_callback_urls_and_return_rechecks_status(
        self,
    ):
        handler = handler_module(self, "stripe_onboarding")
        service = OnboardingService()
        dependencies = {
            "policy_resolver": Resolver(),
            "auth_store": auth_store(),
            "binding_resolver": BindingResolver(),
            "onboarding_service": service,
            "environment": "test",
            "now_epoch": 1_000,
        }
        unsafe = handler.handle_request(
            request(
                "/features/integrations/stripe/onboarding",
                {
                    "operation": "start",
                    "input": {
                        "bindingId": "stripe-primary",
                        "accountId": "acct_browser",
                        "returnUrl": "https://evil.example",
                    },
                },
            ),
            **dependencies,
        )
        self.assertEqual(unsafe["statusCode"], 422)
        self.assertEqual(service.calls, [])

        started = handler.handle_request(
            request(
                "/features/integrations/stripe/onboarding",
                {"operation": "start", "input": {"bindingId": "stripe-primary"}},
            ),
            **dependencies,
        )
        self.assertEqual(started["statusCode"], 200)
        self.assertIn(
            "https://connect.stripe.com/", body(started)["data"]["handoffUrl"]
        )

        returned = handler.handle_request(
            request(
                "/features/integrations/stripe/onboarding",
                {
                    "operation": "return",
                    "input": {
                        "bindingId": "stripe-primary",
                        "state": "a" * 43,
                        "code": "code_synthetic",
                    },
                },
            ),
            **dependencies,
        )
        self.assertEqual(returned["statusCode"], 200)
        self.assertEqual(body(returned)["data"]["status"], "ready")
        self.assertEqual([call[0] for call in service.calls], ["start", "return"])

        denied = handler.handle_request(
            request(
                handler.PATH,
                {
                    "operation": "return",
                    "input": {
                        "bindingId": "stripe-primary",
                        "state": "b" * 43,
                        "error": "access_denied",
                    },
                },
            ),
            **dependencies,
        )
        self.assertEqual(denied["statusCode"], 200)
        self.assertEqual(service.calls[-1][-1], {"error": "access_denied"})

        deauthorized = handler.handle_request(
            request(
                handler.PATH,
                {"operation": "deauthorize", "input": {"bindingId": "stripe-primary"}},
            ),
            **dependencies,
        )
        self.assertEqual(deauthorized["statusCode"], 200)
        self.assertEqual(service.calls[-1][0], "deauthorize")
        malformed = handler.handle_request(
            request(
                "/features/integrations/stripe/onboarding",
                {"operation": {"invalid": True}, "input": {}},
            ),
            **dependencies,
        )
        self.assertEqual(malformed["statusCode"], 422)


if __name__ == "__main__":
    unittest.main()
