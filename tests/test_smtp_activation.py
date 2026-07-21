import hashlib
import importlib
import json
import os
import unittest
from unittest.mock import patch

from src.domain.integrations import (
    IntegrationBinding,
    IntegrationConnection,
    IntegrationScope,
)


TEST_ACCOUNT_ID = "smtp-account-isolation-test"
TEST_ACCOUNT_HASH = hashlib.sha256(TEST_ACCOUNT_ID.encode("ascii")).hexdigest()
ALLOWED_CALLER = "arn:aws:iam::123456789012:role/zoolanding-notifications-test"
OPERATOR_CALLER = "arn:aws:iam::123456789012:role/zoolanding-smtp-operator-test"


def scope(draft_id="draft-email", environment="test", domain="example.com"):
    return IntegrationScope(environment, "tenant-example", draft_id, domain)


def pending_connection(selected_scope=None):
    selected_scope = selected_scope or scope()
    return IntegrationConnection(
        scope=selected_scope,
        connection_id="billing-mailbox",
        provider="email.smtp",
        adapter_version="v1",
        status="pending",
        mode="test" if selected_scope.environment == "test" else "live",
        capabilities=frozenset({"send"}),
        provider_metadata={
            "adapterId": "smtp2go-smtp-v1",
            "host": "mail.smtp2go.com",
            "port": 465,
            "tlsMode": "implicit",
            "canonicalSendingDomain": (
                "zoolandingpage.com.mx"
                if selected_scope.environment == "test"
                else selected_scope.domain
            ),
        },
    )


def binding(selected_scope=None):
    selected_scope = selected_scope or scope()
    return IntegrationBinding(
        scope=selected_scope,
        binding_id="billing-mailbox",
        provider="email.smtp",
        adapter_version="v1",
        connection_id="billing-mailbox",
        status="active",
        mode="test" if selected_scope.environment == "test" else "live",
        capabilities=frozenset({"send"}),
        provider_metadata={},
    )


def activation_payload(selected_scope=None, **changes):
    selected_scope = selected_scope or scope()
    value = {
        "version": 1,
        "scope": selected_scope.fields(),
        "connectionId": "billing-mailbox",
        "commandId": "activate-smtp-1",
        "idempotencyKey": "activate-smtp-retry-1",
        "expectedRevision": 1,
        "fromLocalPart": "billing",
        "replyToLocalPart": "support",
        "ownershipEvidenceId": "ownership-evidence-2026",
    }
    value.update(changes)
    return value


def secret_metadata(connection, *, account_id=TEST_ACCOUNT_ID, credential_id=None):
    credential_id = credential_id or f"credential-{connection.scope.draft_id}"
    return {
        "Name": connection.credential_reference,
        "Tags": [
            {"Key": "zoolanding:environment", "Value": connection.scope.environment},
            {"Key": "zoolanding:tenant-id", "Value": connection.scope.tenant_id},
            {"Key": "zoolanding:draft-id", "Value": connection.scope.draft_id},
            {"Key": "zoolanding:secret-purpose", "Value": "smtp"},
            {"Key": "zoolanding:connection-id", "Value": connection.connection_id},
            {"Key": "zoolanding:enabled", "Value": "true"},
            {"Key": "zoolanding:smtp-account-isolation-id", "Value": account_id},
            {
                "Key": "zoolanding:smtp-credential-isolation-id",
                "Value": credential_id,
            },
        ],
    }


class Secrets:
    def __init__(self, metadata):
        self.metadata = metadata
        self.calls = []

    def describe_secret(self, **kwargs):
        self.calls.append(("describe_secret", kwargs))
        return self.metadata

    def get_secret_value(self, **kwargs):
        self.calls.append(("get_secret_value", kwargs))
        raise AssertionError("activation must not read secret values")


class Registry:
    def __init__(self, connection):
        self.current = connection
        self.calls = []

    def connection(self, selected_scope, connection_id):
        if selected_scope != self.current.scope or connection_id != self.current.connection_id:
            raise RuntimeError("scope mismatch")
        return self.current

    def binding(self, selected_scope, binding_id):
        if selected_scope != self.current.scope or binding_id != self.current.connection_id:
            raise RuntimeError("scope mismatch")
        return binding(selected_scope)

    def activate_smtp(self, candidate, expected_revision, idempotency_key):
        self.calls.append((candidate, expected_revision, idempotency_key))
        self.current = candidate
        return candidate


class Resolver:
    def __init__(self, connection):
        self.resolved = type("Resolved", (), {"binding": binding(connection.scope), "connection": connection})()

    def resolve(self, *args, **kwargs):
        self.calls = (args, kwargs)
        return self.resolved


def request(path, payload, caller=ALLOWED_CALLER):
    return {
        "rawPath": path,
        "requestContext": {
            "http": {"method": "POST"},
            "requestId": "request-1",
            "identity": {"userArn": caller},
        },
        "body": json.dumps(payload),
        "isBase64Encoded": False,
    }


class SmtpActivationTests(unittest.TestCase):
    def test_registration_metadata_is_pending_and_does_not_claim_audit(self):
        services = importlib.import_module("src.internal_connections")
        selected_binding = binding()
        published = type(
            "Policy",
            (),
            {"scope": scope(), "bindings": (selected_binding,)},
        )()

        class Policies:
            def resolve(self, **kwargs):
                return published

        class Admin:
            def register(self, connection, *args, **kwargs):
                self.connection = connection
                return {
                    "connectionId": connection.connection_id,
                    "status": connection.status,
                    "mode": connection.mode,
                    "revision": connection.revision,
                }

        from src.contracts.internal import validate_connection_registration

        registration = validate_connection_registration(
            {
                "version": 1,
                "scope": scope().fields(),
                "connectionId": "billing-mailbox",
                "commandId": "register-smtp-1",
                "idempotencyKey": "register-smtp-retry-1",
                "provider": "email.smtp",
                "mode": "test",
                "capabilities": ["send"],
                "accountReference": None,
            }
        )
        admin = Admin()
        result = services.ConnectionRegistrationService(Policies(), admin).register(
            registration
        )
        self.assertEqual(result["status"], "pending")
        self.assertNotIn("accountOwnershipState", admin.connection.provider_metadata)
        self.assertEqual(admin.connection.provider_metadata["tlsMode"], "implicit")

    def test_disabled_published_binding_cannot_register_or_reserve_claims(self):
        services = importlib.import_module("src.internal_connections")
        selected_scope = scope()
        disabled = IntegrationBinding(
            scope=selected_scope,
            binding_id="billing-mailbox",
            provider="email.smtp",
            adapter_version="v1",
            connection_id="billing-mailbox",
            status="disabled",
            mode="test",
            capabilities=frozenset({"send"}),
            provider_metadata={},
        )
        published = type(
            "Policy", (), {"scope": selected_scope, "bindings": (disabled,)}
        )()

        class Policies:
            def resolve(self, **kwargs):
                return published

        class Admin:
            def __init__(self):
                self.calls = []

            def register(self, *args, **kwargs):
                self.calls.append((args, kwargs))

        from src.contracts.internal import validate_connection_registration

        registration = validate_connection_registration(
            {
                "version": 1,
                "scope": selected_scope.fields(),
                "connectionId": "billing-mailbox",
                "commandId": "register-smtp-disabled",
                "idempotencyKey": "register-smtp-disabled-retry",
                "provider": "email.smtp",
                "mode": "test",
                "capabilities": ["send"],
                "accountReference": None,
            }
        )
        admin = Admin()
        with self.assertRaises(services.InternalConnectionError):
            services.ConnectionRegistrationService(Policies(), admin).register(
                registration
            )
        self.assertEqual(admin.calls, [])

    def test_activation_contract_is_closed_and_local_parts_are_conservative(self):
        contracts = importlib.import_module("src.contracts.internal")
        parsed = contracts.validate_smtp_connection_activation(activation_payload())
        self.assertEqual(parsed.from_local_part, "billing")
        for changed in (
            {"fromLocalPart": "Billing <billing>"},
            {"replyToLocalPart": ".support"},
            {"ownershipEvidenceId": "short"},
            {"accountIsolationId": "must-not-enter-the-request"},
            {"canonicalSendingDomain": "must-not-enter-the-request.example"},
        ):
            with self.subTest(changed=changed), self.assertRaises(contracts.ContractError):
                contracts.validate_smtp_connection_activation(
                    activation_payload(**changed)
                )

    def test_activation_freshly_describes_tags_hashes_evidence_and_never_gets_value(self):
        service_api = importlib.import_module("src.smtp_activation")
        candidate = pending_connection()
        registry = Registry(candidate)
        secrets = Secrets(secret_metadata(candidate))
        service = service_api.SmtpConnectionActivationService(
            registry, secrets, TEST_ACCOUNT_HASH
        )
        from src.contracts.internal import validate_smtp_connection_activation

        result = service.activate(
            validate_smtp_connection_activation(activation_payload())
        )
        self.assertEqual(result["status"], "active")
        activated = registry.current
        self.assertEqual(activated.revision, 2)
        self.assertEqual(
            secrets.calls,
            [("describe_secret", {"SecretId": candidate.credential_reference})],
        )
        serialized = repr(activated.to_record())
        for raw in (
            TEST_ACCOUNT_ID,
            "credential-draft-email",
            "ownership-evidence-2026",
        ):
            self.assertNotIn(raw, serialized)
        self.assertRegex(
            activated.provider_metadata["credentialIsolationHash"], r"^[a-f0-9]{64}$"
        )
        self.assertEqual(activated.provider_metadata["fromLocalPart"], "billing")

    def test_activation_rejects_secret_scope_state_and_test_account_mismatch(self):
        service_api = importlib.import_module("src.smtp_activation")
        from src.contracts.internal import validate_smtp_connection_activation

        command = validate_smtp_connection_activation(activation_payload())
        candidate = pending_connection()
        cases = []
        wrong_tag = secret_metadata(candidate)
        wrong_tag["Tags"] = [
            {**tag, "Value": "draft-other"}
            if tag["Key"] == "zoolanding:draft-id"
            else tag
            for tag in wrong_tag["Tags"]
        ]
        disabled = secret_metadata(candidate)
        disabled["Tags"] = [
            {**tag, "Value": "false"}
            if tag["Key"] == "zoolanding:enabled"
            else tag
            for tag in disabled["Tags"]
        ]
        cases.extend(
            (
                wrong_tag,
                disabled,
                {**secret_metadata(candidate), "DeletedDate": "synthetic"},
                secret_metadata(candidate, account_id="smtp-account-other"),
            )
        )
        for metadata in cases:
            with self.subTest(metadata=metadata), self.assertRaises(
                service_api.SmtpActivationError
            ):
                service_api.SmtpConnectionActivationService(
                    Registry(candidate), Secrets(metadata), TEST_ACCOUNT_HASH
                ).activate(command)

    def test_production_rejects_the_shared_test_account_claim(self):
        service_api = importlib.import_module("src.smtp_activation")
        from src.contracts.internal import validate_smtp_connection_activation

        selected_scope = scope(
            "draft-production", "production", "merchant.example.com"
        )
        candidate = pending_connection(selected_scope)
        command = validate_smtp_connection_activation(
            activation_payload(selected_scope)
        )
        with self.assertRaises(service_api.SmtpActivationError):
            service_api.SmtpConnectionActivationService(
                Registry(candidate),
                Secrets(secret_metadata(candidate, account_id=TEST_ACCOUNT_ID)),
                TEST_ACCOUNT_HASH,
            ).activate(command)

    def test_private_resolution_returns_only_the_fixed_active_smtp_contract(self):
        service_api = importlib.import_module("src.internal_connections")
        activation_api = importlib.import_module("src.smtp_activation")
        contracts = importlib.import_module("src.contracts.internal")
        candidate = pending_connection()
        registry = Registry(candidate)
        activated = activation_api.SmtpConnectionActivationService(
            registry, Secrets(secret_metadata(candidate)), TEST_ACCOUNT_HASH
        ).activate(contracts.validate_smtp_connection_activation(activation_payload()))
        self.assertEqual(activated["status"], "active")

        service = service_api.ConnectionResolutionService(
            Resolver(registry.current), TEST_ACCOUNT_HASH
        )
        command_payload = {
            "version": 1,
            "scope": scope().fields(),
            "connectionId": "billing-mailbox",
            "commandId": "resolve-smtp-1",
            "idempotencyKey": "resolve-smtp-retry-1",
            "input": {"provider": "email.smtp", "capability": "send"},
        }
        resolve_command = contracts.validate_command(
            "connection-resolve", command_payload
        )
        resolved = service.resolve(resolve_command)
        contracts.validate_connection_resolution_result(resolved, resolve_command)
        self.assertEqual(
            resolved["endpoint"],
            {
                "host": "mail.smtp2go.com",
                "port": 465,
                "tlsMode": "implicit",
                "canonicalSendingDomain": "zoolandingpage.com.mx",
            },
        )
        self.assertEqual(
            resolved["senderPolicy"],
            {"fromLocalPart": "billing", "replyToLocalPart": "support"},
        )
        self.assertEqual(resolved["adapterId"], "smtp2go-smtp-v1")
        self.assertRegex(resolved["rateCircuitNamespace"], r"^smtp-rate-v1:[a-f0-9]{64}$")
        for forbidden in ("accountIsolationHash", "credentialIsolationHash", "ownershipEvidenceHash"):
            self.assertNotIn(forbidden, repr(resolved))

        browser_view = importlib.import_module(
            "src.handlers.connection_read"
        )._sanitized(registry.current)
        self.assertEqual(
            set(browser_view),
            {"connectionId", "provider", "status", "mode", "capabilities", "revision"},
        )
        self.assertNotIn("fromLocalPart", repr(browser_view))

    def test_activation_handler_requires_exact_aws_iam_caller_and_shape(self):
        handler = importlib.import_module(
            "src.handlers.internal_smtp_connection_activate"
        )

        class Service:
            def activate(self, command):
                self.command = command
                return {
                    "connectionId": command.connection_id,
                    "status": "active",
                    "mode": "test",
                    "revision": 2,
                }

        service = Service()
        accepted = handler.handle_request(
            request(
                handler.PATH, activation_payload(), caller=OPERATOR_CALLER
            ),
            service=service,
            allowed_callers={OPERATOR_CALLER},
        )
        self.assertEqual(accepted["statusCode"], 200)
        denied = handler.handle_request(
            request(handler.PATH, activation_payload()),
            service=service,
            allowed_callers={OPERATOR_CALLER},
        )
        self.assertEqual(denied["statusCode"], 403)

    def test_notifications_can_resolve_but_only_operator_allowlist_can_activate(self):
        activation_handler = importlib.import_module(
            "src.handlers.internal_smtp_connection_activate"
        )
        resolution_handler = importlib.import_module(
            "src.handlers.internal_connection_resolve"
        )

        class ActivationService:
            def activate(self, command):
                return {
                    "connectionId": command.connection_id,
                    "status": "active",
                    "mode": "test",
                    "revision": 2,
                }

        class ResolutionService:
            def resolve(self, command):
                return {
                    "connectionId": command.connection_id,
                    "provider": "email.smtp",
                    "mode": "test",
                    "adapterVersion": "v1",
                    "adapterId": "smtp2go-smtp-v1",
                    "credentialReference": (
                        "/zoolanding/test/tenant-example/draft-email/"
                        "notifications/smtp/billing-mailbox"
                    ),
                    "endpoint": {
                        "host": "mail.smtp2go.com",
                        "port": 465,
                        "tlsMode": "implicit",
                        "canonicalSendingDomain": "zoolandingpage.com.mx",
                    },
                    "senderPolicy": {
                        "fromLocalPart": "billing",
                        "replyToLocalPart": "support",
                    },
                    "rateCircuitNamespace": "smtp-rate-v1:" + "a" * 64,
                }

        resolve_payload = {
            "version": 1,
            "scope": scope().fields(),
            "connectionId": "billing-mailbox",
            "commandId": "resolve-smtp-1",
            "idempotencyKey": "resolve-smtp-retry-1",
            "input": {"provider": "email.smtp", "capability": "send"},
        }
        environment = {
            "INTERNAL_CALLER_ARNS": ALLOWED_CALLER,
            "SMTP_ACTIVATION_CALLER_ARNS": OPERATOR_CALLER,
        }
        with patch.dict(os.environ, environment, clear=True):
            resolved = resolution_handler.handle_request(
                request(resolution_handler.PATH, resolve_payload),
                service=ResolutionService(),
            )
            denied = activation_handler.handle_request(
                request(activation_handler.PATH, activation_payload()),
                service=ActivationService(),
            )
            activated = activation_handler.handle_request(
                request(
                    activation_handler.PATH,
                    activation_payload(),
                    caller=OPERATOR_CALLER,
                ),
                service=ActivationService(),
            )
        self.assertEqual(resolved["statusCode"], 200)
        self.assertEqual(denied["statusCode"], 403)
        self.assertEqual(activated["statusCode"], 200)

        with patch.dict(
            os.environ, {"INTERNAL_CALLER_ARNS": ALLOWED_CALLER}, clear=True
        ):
            missing_operator_allowlist = activation_handler.handle_request(
                request(activation_handler.PATH, activation_payload()),
                service=ActivationService(),
            )
        self.assertEqual(missing_operator_allowlist["statusCode"], 403)

    def test_registry_activation_is_atomic_exactly_replayable_and_hash_only(self):
        from src.contracts.internal import validate_smtp_connection_activation
        from src.registry import ConnectionRegistry, RegistryConflict
        from src.smtp_activation import SmtpConnectionActivationService
        from tests.test_registry import MemoryBackend

        backend = MemoryBackend()
        registry = ConnectionRegistry(backend)
        candidate = pending_connection()
        registry.register(candidate, binding(), "register-request")
        service = SmtpConnectionActivationService(
            registry, Secrets(secret_metadata(candidate)), TEST_ACCOUNT_HASH
        )
        command = validate_smtp_connection_activation(activation_payload())

        first = service.activate(command)
        replay = service.activate(command)
        self.assertEqual(replay, first)
        self.assertEqual(len(backend.smtp_activations), 1)
        self.assertEqual(len(backend.smtp_activations[0][0]), 1)
        stored = repr(backend.records)
        for raw in (
            TEST_ACCOUNT_ID,
            "credential-draft-email",
            "ownership-evidence-2026",
        ):
            self.assertNotIn(raw, stored)

        changed = validate_smtp_connection_activation(
            activation_payload(ownershipEvidenceId="ownership-evidence-changed")
        )
        with self.assertRaises(RegistryConflict):
            service.activate(changed)

    def test_credential_claim_is_unique_across_test_drafts(self):
        from src.contracts.internal import validate_smtp_connection_activation
        from src.registry import ConnectionRegistry, RegistryConflict
        from src.smtp_activation import SmtpConnectionActivationService
        from tests.test_registry import MemoryBackend

        backend = MemoryBackend()
        registry = ConnectionRegistry(backend)
        shared_credential = "credential-shared-across-drafts"
        for selected_scope in (scope("draft-one"), scope("draft-two")):
            registry.register(
                pending_connection(selected_scope),
                binding(selected_scope),
                f"register-{selected_scope.draft_id}",
            )

        first = pending_connection(scope("draft-one"))
        SmtpConnectionActivationService(
            registry,
            Secrets(secret_metadata(first, credential_id=shared_credential)),
            TEST_ACCOUNT_HASH,
        ).activate(
            validate_smtp_connection_activation(activation_payload(first.scope))
        )
        second = pending_connection(scope("draft-two"))
        with self.assertRaises(RegistryConflict):
            SmtpConnectionActivationService(
                registry,
                Secrets(secret_metadata(second, credential_id=shared_credential)),
                TEST_ACCOUNT_HASH,
            ).activate(
                validate_smtp_connection_activation(activation_payload(second.scope))
            )

    def test_production_account_and_domain_claims_are_each_unique(self):
        from src.contracts.internal import validate_smtp_connection_activation
        from src.registry import ConnectionRegistry, RegistryConflict
        from src.smtp_activation import SmtpConnectionActivationService
        from tests.test_registry import MemoryBackend

        def activate_pair(second_domain, second_account):
            backend = MemoryBackend()
            registry = ConnectionRegistry(backend)
            scopes = (
                scope("draft-prod-one", "production", "one.example.com"),
                scope("draft-prod-two", "production", second_domain),
            )
            for item in scopes:
                registry.register(
                    pending_connection(item), binding(item), f"register-{item.draft_id}"
                )
            for index, item in enumerate(scopes):
                candidate = pending_connection(item)
                account = "production-account-one" if index == 0 else second_account
                command = validate_smtp_connection_activation(
                    activation_payload(item)
                )
                service = SmtpConnectionActivationService(
                    registry,
                    Secrets(
                        secret_metadata(
                            candidate,
                            account_id=account,
                            credential_id=f"credential-{item.draft_id}",
                        )
                    ),
                    TEST_ACCOUNT_HASH,
                )
                if index == 0:
                    service.activate(command)
                else:
                    with self.assertRaises(RegistryConflict):
                        service.activate(command)

        activate_pair("two.example.com", "production-account-one")
        activate_pair("one.example.com", "production-account-two")

    def test_real_binding_resolution_is_active_only(self):
        from src.contracts.internal import validate_smtp_connection_activation
        from src.registry import BindingResolver, ConnectionRegistry, RegistryAccessDenied
        from src.smtp_activation import SmtpConnectionActivationService
        from tests.test_registry import MemoryBackend

        backend = MemoryBackend()
        registry = ConnectionRegistry(backend)
        candidate = pending_connection()
        registry.register(candidate, binding(), "register-request")
        resolver = BindingResolver(registry)
        with self.assertRaises(RegistryAccessDenied):
            resolver.resolve(
                candidate.scope,
                candidate.connection_id,
                provider="email.smtp",
                capability="send",
            )
        SmtpConnectionActivationService(
            registry, Secrets(secret_metadata(candidate)), TEST_ACCOUNT_HASH
        ).activate(validate_smtp_connection_activation(activation_payload()))
        resolved = resolver.resolve(
            candidate.scope,
            candidate.connection_id,
            provider="email.smtp",
            capability="send",
        )
        self.assertEqual(resolved.connection.status, "active")

    def test_binding_disabled_after_registration_blocks_activation_without_claims(self):
        from src.contracts.internal import validate_smtp_connection_activation
        from src.registry import ConnectionRegistry
        from src.smtp_activation import (
            SmtpActivationError,
            SmtpConnectionActivationService,
        )
        from tests.test_registry import MemoryBackend

        backend = MemoryBackend()
        registry = ConnectionRegistry(backend)
        candidate = pending_connection()
        registry.register(candidate, binding(), "register-request")
        binding_key = (
            candidate.scope.partition_key,
            "BINDING#billing-mailbox",
        )
        backend.records[binding_key] = {
            **backend.records[binding_key],
            "status": "disabled",
        }
        secrets = Secrets(secret_metadata(candidate))
        with self.assertRaises(SmtpActivationError):
            SmtpConnectionActivationService(
                registry, secrets, TEST_ACCOUNT_HASH
            ).activate(
                validate_smtp_connection_activation(activation_payload())
            )
        self.assertEqual(secrets.calls, [])
        self.assertEqual(backend.smtp_activations, [])
        self.assertFalse(
            any(
                item.get("itemType") == "ConnectionIsolationSentinel"
                for item in backend.records.values()
            )
        )
        current = registry.connection(candidate.scope, candidate.connection_id)
        self.assertEqual((current.status, current.revision), ("pending", 1))


if __name__ == "__main__":
    unittest.main()
