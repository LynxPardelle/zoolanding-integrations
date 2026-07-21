import hashlib
import importlib
import importlib.util
import unittest

from src.domain.integrations import (
    IntegrationBinding,
    IntegrationConnection,
    IntegrationScope,
)


def registry_module(testcase):
    testcase.assertIsNotNone(
        importlib.util.find_spec("src.registry"),
        "connection registry is not implemented",
    )
    return importlib.import_module("src.registry")


def scope(draft_id="draft-example", environment="test", domain="example.com"):
    return IntegrationScope(environment, "tenant-example", draft_id, domain)


def connection(
    resolved_scope=None,
    *,
    status="active",
    mode="test",
    account="acct_synthetic",
    ready=True,
):
    metadata = {"accountReference": account}
    if ready and status == "active":
        metadata["readiness"] = {
            "chargesEnabled": True,
            "payoutsEnabled": True,
            "detailsSubmitted": True,
            "capabilitiesReady": True,
            "requirementsDueCount": 0,
        }
    return IntegrationConnection(
        scope=resolved_scope or scope(),
        connection_id="stripe-primary",
        provider="stripe",
        adapter_version="v1",
        status=status,
        mode=mode,
        capabilities=frozenset({"connect-onboarding", "checkout"}),
        provider_metadata=metadata,
    )


def binding(resolved_scope=None, *, capabilities=None, mode="test"):
    return IntegrationBinding.from_mapping(
        resolved_scope or scope(),
        {
            "id": "stripe-primary",
            "provider": "stripe",
            "adapterVersion": "v1",
            "connectionId": "stripe-primary",
            "status": "active",
            "mode": mode,
            "capabilities": capabilities or ["connect-onboarding", "checkout"],
            "stripe": {
                "accountModel": "merchant",
                "chargeType": "direct",
                "feePayer": "connected-account",
                "taxMode": "unconfigured",
                "platformFeeMode": "disabled",
                "webhookIngress": "direct-integrations-api",
            },
        },
    )


class MemoryBackend:
    def __init__(self):
        self.records = {}
        self.registrations = []
        self.get_calls = []

    def put_registration(self, records, sentinel, idempotency_key):
        self.registrations.append((records, sentinel, idempotency_key))
        for record in records + (sentinel,):
            self.records[(record["pk"], record["sk"])] = record

    def get(self, pk, sk):
        self.get_calls.append((pk, sk))
        return self.records.get((pk, sk))

    def query_connections(self, pk):
        return [
            item
            for (record_pk, record_sk), item in self.records.items()
            if record_pk == pk and record_sk.startswith("CONNECTION#")
        ]

    def update_status(self, pk, sk, status, expected_revision):
        item = self.records[(pk, sk)]
        if item["revision"] != expected_revision:
            raise RuntimeError("conflict")
        updated = {**item, "status": status, "revision": expected_revision + 1}
        self.records[(pk, sk)] = updated
        return updated

    def activate_ready(self, pk, sk, readiness, expected_revision):
        item = self.records[(pk, sk)]
        if item["revision"] != expected_revision:
            raise RuntimeError("conflict")
        updated = {
            **item,
            "status": "active",
            "revision": expected_revision + 1,
            "providerMetadata": {
                **item["providerMetadata"],
                "readiness": dict(readiness),
            },
        }
        self.records[(pk, sk)] = updated
        return updated


class RegistryTests(unittest.TestCase):
    def test_registration_is_draft_partitioned_and_uses_hashed_non_authorizing_sentinel(
        self,
    ):
        registry_api = registry_module(self)
        backend = MemoryBackend()
        registry = registry_api.ConnectionRegistry(backend)
        prod_scope = scope(
            "draft-one", environment="production", domain="one.example.com"
        )
        candidate = connection(prod_scope, mode="live", account="acct_synthetic")

        registry.register(candidate, binding(prod_scope, mode="live"), "request-1")

        records, sentinel, token = backend.registrations[0]
        self.assertEqual(records[0]["pk"], prod_scope.partition_key)
        self.assertEqual(records[0]["revision"], 1)
        self.assertEqual(token, "request-1")
        self.assertEqual(
            sentinel["pk"],
            "ROUTING#production#live#" + hashlib.sha256(b"acct_synthetic").hexdigest(),
        )
        self.assertEqual(sentinel["authorizes"], False)
        self.assertNotIn("acct_synthetic", str(sentinel))
        self.assertRegex(records[0]["registrationHash"], r"^[a-f0-9]{64}$")
        self.assertEqual(records[0]["registrationHash"], sentinel["registrationHash"])

    def test_exact_replay_is_noop_but_same_id_rebind_is_rejected(self):
        registry_api = registry_module(self)
        backend = MemoryBackend()
        registry = registry_api.ConnectionRegistry(backend)
        registry.register(connection(), binding(), "request-1")

        registry.register(connection(), binding(), "request-2")
        self.assertEqual(len(backend.registrations), 1)

        with self.assertRaises(registry_api.RegistryConflict):
            registry.register(
                connection(account="acct_different"),
                binding(),
                "request-3",
            )

    def test_binding_resolver_rejects_cross_draft_wrong_mode_and_missing_capability(
        self,
    ):
        registry_api = registry_module(self)
        backend = MemoryBackend()
        registry = registry_api.ConnectionRegistry(backend)
        registry.register(connection(), binding(), "request-1")
        resolver = registry_api.BindingResolver(registry)

        resolved = resolver.resolve(
            scope(), "stripe-primary", provider="stripe", capability="checkout"
        )
        self.assertEqual(resolved.connection.connection_id, "stripe-primary")

        cases = (
            (scope("draft-other"), "stripe", "checkout"),
            (scope(), "email.smtp", "checkout"),
            (scope(), "stripe", "subscriptions"),
        )
        for resolved_scope, provider, capability in cases:
            with (
                self.subTest(capability=capability),
                self.assertRaises(registry_api.RegistryAccessDenied),
            ):
                resolver.resolve(
                    resolved_scope,
                    "stripe-primary",
                    provider=provider,
                    capability=capability,
                )

    def test_checkout_resolution_requires_persisted_canonical_provider_readiness(self):
        registry_api = registry_module(self)
        backend = MemoryBackend()
        not_ready = connection(ready=False)
        registry_api.ConnectionRegistry(backend).register(
            not_ready, binding(), "request-not-ready"
        )
        resolver = registry_api.BindingResolver(registry_api.ConnectionRegistry(backend))
        with self.assertRaises(registry_api.RegistryAccessDenied):
            resolver.resolve(
                scope(), "stripe-primary", provider="stripe", capability="checkout"
            )

        activated = registry_api.ConnectionRegistry(backend).activate_ready(
            scope(),
            "stripe-primary",
            {
                "status": "ready",
                "chargesEnabled": True,
                "payoutsEnabled": True,
                "detailsSubmitted": True,
                "capabilitiesReady": True,
                "requirementsDueCount": 0,
            },
            1,
        )
        self.assertEqual(activated.status, "active")
        resolver.resolve(
            scope(), "stripe-primary", provider="stripe", capability="checkout"
        )

        pending_backend = MemoryBackend()
        pending_registry = registry_api.ConnectionRegistry(pending_backend)
        pending_registry.register(
            connection(status="pending"), binding(), "pending-registration"
        )
        pending_resolver = registry_api.BindingResolver(pending_registry)
        pending_resolver.resolve(
            scope(),
            "stripe-primary",
            provider="stripe",
            capability="connect-onboarding",
        )
        with self.assertRaises(registry_api.RegistryAccessDenied):
            pending_resolver.resolve(
                scope(),
                "stripe-primary",
                provider="stripe",
                capability="checkout",
            )

    def test_dynamo_registration_uses_one_conditional_transaction(self):
        registry_api = registry_module(self)

        class Client:
            def __init__(self):
                self.calls = []

            def transact_write_items(self, **kwargs):
                self.calls.append(kwargs)

            def get_item(self, **kwargs):
                return {}

        client = Client()
        backend = registry_api.DynamoRegistryBackend("registry-table", client=client)
        prod_scope = scope(
            "draft-one", environment="production", domain="one.example.com"
        )
        registry_api.ConnectionRegistry(backend).register(
            connection(prod_scope, mode="live"),
            binding(prod_scope, mode="live"),
            "registration-request",
        )

        call = client.calls[0]
        self.assertEqual(len(call["TransactItems"]), 3)
        self.assertTrue(
            all("ConditionExpression" in item["Put"] for item in call["TransactItems"])
        )
        self.assertNotIn("*", str(call["TransactItems"]))
        self.assertLessEqual(len(call["ClientRequestToken"]), 36)

        second_scope = scope("draft-two")
        registry_api.ConnectionRegistry(backend).register(
            connection(second_scope),
            binding(second_scope),
            "registration-request",
        )
        self.assertNotEqual(
            client.calls[0]["ClientRequestToken"],
            client.calls[1]["ClientRequestToken"],
        )

    def test_dynamo_query_fails_closed_instead_of_returning_a_partial_page(self):
        registry_api = registry_module(self)

        class Client:
            def query(self, **kwargs):
                return {"Items": [], "LastEvaluatedKey": {"pk": {"S": "next"}}}

        backend = registry_api.DynamoRegistryBackend("registry-table", client=Client())
        with self.assertRaises(registry_api.RegistryError):
            backend.query_connections(scope().partition_key)

    def test_non_ascii_identifier_is_rejected_before_registry_access(self):
        registry_api = registry_module(self)
        backend = MemoryBackend()
        with self.assertRaises(registry_api.RegistryAccessDenied):
            registry_api.ConnectionRegistry(backend).connection(scope(), "strípe")
        self.assertEqual(backend.get_calls, [])


if __name__ == "__main__":
    unittest.main()
