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
    metadata = {"accountReference": account} if account is not None else {}
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


def binding(
    resolved_scope=None, *, capabilities=None, mode="test", tax_mode="unconfigured"
):
    selected_capabilities = capabilities or ["connect-onboarding", "checkout"]
    stripe_metadata = {
        "accountStrategy": "oauth-standard-v1",
        "accountModel": "merchant",
        "chargeType": "direct",
        "feePayer": "connected-account",
        "taxMode": tax_mode,
        "platformFeeMode": "disabled",
        "webhookIngress": "direct-integrations-api",
    }
    if "connect-onboarding" in selected_capabilities:
        stripe_metadata["onboardingRoutes"] = {
            "returnPath": "/admin/integrations/stripe/return",
            "refreshPath": "/admin/integrations/stripe/refresh",
        }
    if "customer-portal" in selected_capabilities:
        stripe_metadata["customerPortalReturnPath"] = "/admin/billing"
    return IntegrationBinding.from_mapping(
        resolved_scope or scope(),
        {
            "id": "stripe-primary",
            "provider": "stripe",
            "adapterVersion": "v1",
            "connectionId": "stripe-primary",
            "status": "active",
            "mode": mode,
            "capabilities": selected_capabilities,
            "stripe": stripe_metadata,
        },
    )


class MemoryBackend:
    def __init__(self):
        self.records = {}
        self.registrations = []
        self.get_calls = []

    def put_registration(self, records, sentinels, idempotency_key):
        self.registrations.append((records, sentinels, idempotency_key))
        for record in records + sentinels:
            if (record["pk"], record["sk"]) in self.records:
                from src.registry import RegistryConflict

                raise RegistryConflict("conditional conflict")
        for record in records + sentinels:
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

    def bind_stripe_account(
        self,
        pk,
        sk,
        account_reference,
        sentinel,
        ownership,
        expected_revision,
        registration_hash,
        old_sentinel_pk,
    ):
        item = self.records[(pk, sk)]
        if (
            item["revision"] != expected_revision
            or item["registrationHash"] != registration_hash
            or (sentinel["pk"], sentinel["sk"]) in self.records
        ):
            raise RuntimeError("conflict")
        del self.records[(old_sentinel_pk, "CLAIM")]
        updated = {
            **item,
            "revision": expected_revision + 1,
            "providerMetadata": {
                **item["providerMetadata"],
                "accountReference": account_reference,
                "accountOwnership": ownership,
            },
        }
        self.records[(pk, sk)] = updated
        self.records[(sentinel["pk"], sentinel["sk"])] = sentinel
        return updated

    def rebind_stripe_account(
        self,
        pk,
        sk,
        account_reference,
        ownership,
        expected_revision,
        registration_hash,
        expected_sentinel,
    ):
        item = self.records[(pk, sk)]
        sentinel = self.records[(expected_sentinel["pk"], expected_sentinel["sk"])]
        if (
            item["revision"] != expected_revision
            or item["registrationHash"] != registration_hash
            or item["providerMetadata"].get("accountReference") != account_reference
            or item["providerMetadata"].get("accountOwnership") != ownership
            or sentinel.get("connectionId") != item["connectionId"]
            or any(
                sentinel.get(key) != expected_sentinel.get(key)
                for key in (
                    "environment",
                    "tenantId",
                    "draftId",
                    "domain",
                    "provider",
                    "connectionId",
                    "authorizes",
                    "registrationHash",
                )
            )
        ):
            raise RuntimeError("conflict")
        metadata = {
            key: value
            for key, value in item["providerMetadata"].items()
            if key != "readiness"
        }
        updated = {
            **item,
            "revision": expected_revision + 1,
            "providerMetadata": metadata,
        }
        self.records[(pk, sk)] = updated
        return updated

    def disable_stripe_account(
        self, pk, sk, sentinel_pk, expected_revision, registration_hash
    ):
        item = self.records[(pk, sk)]
        if item["revision"] != expected_revision:
            raise RuntimeError("conflict")
        self.records.pop((sentinel_pk, "CLAIM"), None)
        metadata = {
            key: value
            for key, value in item["providerMetadata"].items()
            if key not in {"accountReference", "accountOwnership", "readiness"}
        }
        updated = {
            **item,
            "status": "disabled",
            "revision": expected_revision + 1,
            "providerMetadata": metadata,
        }
        self.records[(pk, sk)] = updated
        return updated


class RegistryTests(unittest.TestCase):
    def test_pending_stripe_account_is_bound_and_disabled_with_hashed_atomic_claims(
        self,
    ):
        registry_api = registry_module(self)
        backend = MemoryBackend()
        registry = registry_api.ConnectionRegistry(backend)
        registry.register(
            connection(status="pending", account=None, ready=False),
            binding(),
            "registration-request",
        )

        bound = registry.bind_stripe_account(
            scope(),
            "stripe-primary",
            "acct_synthetic",
            "external-oauth",
            1,
        )

        digest = hashlib.sha256(b"acct_synthetic").hexdigest()
        sentinel = backend.records[(f"ROUTING#test#test#{digest}", "CLAIM")]
        self.assertEqual(bound.provider_metadata["accountReference"], "acct_synthetic")
        self.assertEqual(bound.provider_metadata["accountOwnership"], "external-oauth")
        self.assertNotIn("acct_synthetic", repr(sentinel))
        self.assertFalse(sentinel["authorizes"])

        disabled = registry.disable_stripe_account(
            scope(), "stripe-primary", "acct_synthetic", 2
        )
        self.assertEqual(disabled.status, "disabled")
        self.assertNotIn("accountReference", disabled.provider_metadata)
        self.assertNotIn((f"ROUTING#test#test#{digest}", "CLAIM"), backend.records)

    def test_account_binding_rejects_replay_to_another_draft(self):
        registry_api = registry_module(self)
        backend = MemoryBackend()
        first = registry_api.ConnectionRegistry(backend)
        first.register(
            connection(status="pending", account=None, ready=False),
            binding(),
            "first",
        )
        first.bind_stripe_account(
            scope(), "stripe-primary", "acct_synthetic", "external-oauth", 1
        )
        second_scope = scope("draft-other")
        first.register(
            connection(second_scope, status="pending", account=None, ready=False),
            binding(second_scope),
            "second",
        )
        with self.assertRaises(registry_api.RegistryConflict):
            first.bind_stripe_account(
                second_scope,
                "stripe-primary",
                "acct_synthetic",
                "platform-controller",
                1,
            )

    def test_oauth_reconnect_is_atomic_only_for_the_same_exact_account(self):
        registry_api = registry_module(self)
        backend = MemoryBackend()
        registry = registry_api.ConnectionRegistry(backend)
        registry.register(
            connection(status="pending", account=None, ready=False),
            binding(),
            "registration-request",
        )
        first = registry.bind_stripe_account(
            scope(), "stripe-primary", "acct_synthetic", "external-oauth", 1
        )
        digest = hashlib.sha256(b"acct_synthetic").hexdigest()
        sentinel_key = (f"ROUTING#test#test#{digest}", "CLAIM")
        sentinel = dict(backend.records[sentinel_key])

        reconnected = registry.bind_stripe_account(
            scope(),
            "stripe-primary",
            "acct_synthetic",
            "external-oauth",
            first.revision,
        )
        self.assertEqual(reconnected.revision, first.revision + 1)
        self.assertEqual(backend.records[sentinel_key], sentinel)

        with self.assertRaises(registry_api.RegistryConflict):
            registry.bind_stripe_account(
                scope(),
                "stripe-primary",
                "acct_different1",
                "external-oauth",
                reconnected.revision,
            )
        self.assertEqual(
            registry.connection(scope(), "stripe-primary").provider_metadata[
                "accountReference"
            ],
            "acct_synthetic",
        )
        self.assertIn(sentinel_key, backend.records)

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

        records, sentinels, token = backend.registrations[0]
        sentinel = sentinels[0]
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

    def test_production_smtp_domain_is_atomically_isolated_but_test_domain_is_shared(
        self,
    ):
        def pair(draft_id, environment, domain):
            selected_scope = scope(draft_id, environment=environment, domain=domain)
            mode = "live" if environment == "production" else "test"
            sending_domain = (
                domain if environment == "production" else "zoolandingpage.com.mx"
            )
            selected_connection = IntegrationConnection(
                scope=selected_scope,
                connection_id="billing-mailbox",
                provider="email.smtp",
                adapter_version="v1",
                status="active",
                mode=mode,
                capabilities=frozenset({"send"}),
                provider_metadata={
                    "adapterId": "smtp2go-smtp-v1",
                    "host": "mail.smtp2go.com",
                    "port": 465,
                    "canonicalSendingDomain": sending_domain,
                    "accountOwnershipState": "audited",
                },
            )
            selected_binding = IntegrationBinding(
                scope=selected_scope,
                binding_id="billing-mailbox",
                provider="email.smtp",
                adapter_version="v1",
                connection_id="billing-mailbox",
                status="active",
                mode=mode,
                capabilities=frozenset({"send"}),
                provider_metadata={},
            )
            return selected_connection, selected_binding

        backend = MemoryBackend()
        registry = registry_module(self).ConnectionRegistry(backend)
        first = pair("draft-one", "production", "shared.example.com")
        second = pair("draft-two", "production", "shared.example.com")
        registry.register(*first, "smtp-prod-one")
        with self.assertRaises(registry_module(self).RegistryConflict):
            registry.register(*second, "smtp-prod-two")

        _, production_claims, _ = backend.registrations[0]
        self.assertEqual(len(production_claims), 2)
        isolation = production_claims[1]
        self.assertEqual(isolation["itemType"], "ConnectionIsolationSentinel")
        self.assertNotIn("shared.example.com", repr(isolation))
        self.assertFalse(isolation["authorizes"])

        test_backend = MemoryBackend()
        test_registry = registry_module(self).ConnectionRegistry(test_backend)
        test_registry.register(
            *pair("draft-one", "test", "one.example.com"), "smtp-test-one"
        )
        test_registry.register(
            *pair("draft-two", "test", "two.example.com"), "smtp-test-two"
        )
        self.assertEqual(len(test_backend.registrations), 2)
        self.assertTrue(
            all(len(sentinels) == 1 for _, sentinels, _ in test_backend.registrations)
        )

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
        resolver = registry_api.BindingResolver(
            registry_api.ConnectionRegistry(backend)
        )
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

    def test_dynamo_same_account_rebind_checks_full_scope_and_keeps_routing_claim(self):
        registry_api = registry_module(self)
        current = connection(status="pending", ready=False)
        record = {
            **current.to_record(),
            "registrationHash": "a" * 64,
        }

        class Client:
            def __init__(self):
                self.transaction = None

            def transact_write_items(self, **kwargs):
                self.transaction = kwargs

            def get_item(self, **kwargs):
                del kwargs
                return {"Item": registry_api._serialize(record)}

        client = Client()
        backend = registry_api.DynamoRegistryBackend("registry-table", client=client)
        sentinel = {
            **registry_api._routing_sentinel(current),
            "registrationHash": "a" * 64,
        }
        backend.rebind_stripe_account(
            current.scope.partition_key,
            "CONNECTION#stripe-primary",
            "acct_synthetic",
            "external-oauth",
            1,
            "a" * 64,
            sentinel,
        )

        operations = client.transaction["TransactItems"]
        self.assertEqual(len(operations), 2)
        update = operations[0]["Update"]
        check = operations[1]["ConditionCheck"]
        self.assertIn(
            "providerMetadata.accountReference = :account",
            update["ConditionExpression"],
        )
        for field in ("environment", "tenantId", "draftId", "domain"):
            self.assertIn(field, check["ConditionExpression"])
        self.assertNotIn("acct_synthetic", repr(check))
        self.assertEqual(
            set(update["ExpressionAttributeValues"]),
            {
                ":account",
                ":ownership",
                ":pending",
                ":next_revision",
                ":expected_revision",
                ":provider",
                ":registration_hash",
            },
        )

    def test_non_ascii_identifier_is_rejected_before_registry_access(self):
        registry_api = registry_module(self)
        backend = MemoryBackend()
        with self.assertRaises(registry_api.RegistryAccessDenied):
            registry_api.ConnectionRegistry(backend).connection(scope(), "strípe")
        self.assertEqual(backend.get_calls, [])

    def test_stripe_webhook_routing_resolves_only_the_hashed_active_account_claim(self):
        registry_api = registry_module(self)
        backend = MemoryBackend()
        registry = registry_api.ConnectionRegistry(backend)
        registry.register(connection(), binding(), "request-1")

        routed = registry.stripe_webhook_connection(
            environment="test",
            mode="test",
            account_reference="acct_synthetic",
        )

        digest = hashlib.sha256(b"acct_synthetic").hexdigest()
        self.assertEqual(routed.scope, scope())
        self.assertEqual(routed.connection_id, "stripe-primary")
        self.assertIn((f"ROUTING#test#test#{digest}", "CLAIM"), backend.get_calls)
        self.assertNotIn("acct_synthetic", repr(backend.get_calls))

    def test_deauthorization_webhook_alone_can_route_a_bound_pending_account(self):
        registry_api = registry_module(self)
        backend = MemoryBackend()
        registry = registry_api.ConnectionRegistry(backend)
        registry.register(
            connection(status="pending", account=None, ready=False),
            binding(),
            "request-1",
        )
        registry.bind_stripe_account(
            scope(), "stripe-primary", "acct_synthetic", "external-oauth", 1
        )

        routed = registry.stripe_webhook_connection(
            environment="test",
            mode="test",
            account_reference="acct_synthetic",
            event_type="account.application.deauthorized",
        )
        self.assertEqual(routed.status, "pending")
        with self.assertRaises(registry_api.RegistryAccessDenied):
            registry.stripe_webhook_connection(
                environment="test",
                mode="test",
                account_reference="acct_synthetic",
                event_type="invoice.paid",
            )

    def test_stripe_webhook_routing_rejects_mode_scope_and_account_tampering(self):
        registry_api = registry_module(self)
        backend = MemoryBackend()
        registry = registry_api.ConnectionRegistry(backend)
        registry.register(connection(), binding(), "request-1")
        digest = hashlib.sha256(b"acct_synthetic").hexdigest()
        sentinel_key = (f"ROUTING#test#test#{digest}", "CLAIM")

        for changed in (
            {**backend.records[sentinel_key], "draftId": "draft-other"},
            {**backend.records[sentinel_key], "provider": "email.smtp"},
            {**backend.records[sentinel_key], "authorizes": True},
        ):
            with self.subTest(changed=changed):
                backend.records[sentinel_key] = changed
                with self.assertRaises(registry_api.RegistryAccessDenied):
                    registry.stripe_webhook_connection(
                        environment="test",
                        mode="test",
                        account_reference="acct_synthetic",
                    )

        with self.assertRaises(registry_api.RegistryAccessDenied):
            registry.stripe_webhook_connection(
                environment="production",
                mode="test",
                account_reference="acct_synthetic",
            )


if __name__ == "__main__":
    unittest.main()
