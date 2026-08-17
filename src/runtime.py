"""Fail-closed AWS dependency composition for implemented control-plane routes."""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from typing import Any

try:
    from common.auth_admin import DynamoAuthStore
    from common.published_policy import (
        PublishedCheckoutRouteResolver,
        PublishedPolicyResolver,
    )
    from connection_admin import ConnectionAdmin
    from internal_connections import (
        ConnectionRegistrationService,
        ConnectionResolutionService,
    )
    from registry import (
        BindingResolver,
        ConnectionRegistry,
        DynamoRegistryBackend,
        _deserialize,
        _serialize,
    )
    from onboarding import StripeOnboardingService
    from onboarding_state import DynamoOnboardingStateStore, OnboardingStateManager
    from migration_store import (
        DynamoMigrationStore,
        DynamoMigrationStatusStore,
        DynamoOfferReferenceGuard,
        SqsMigrationQueue,
    )
    from providers.stripe_adapter import (
        SecretsManagerStripeWebhookVerifier,
        SecretsManagerStripeClientFactory,
        StripeAdapter,
    )
    from stripe_commands import StripeCommandService
    from stripe_store import DynamoStripeCommandStore, DynamoStripeWebhookStore
    from smtp_activation import SmtpConnectionActivationService
    from subscription_migrations import (
        SubscriptionMigrationService,
        SubscriptionMigrationStatusService,
    )
except ModuleNotFoundError:
    from src.common.auth_admin import DynamoAuthStore
    from src.common.published_policy import (
        PublishedCheckoutRouteResolver,
        PublishedPolicyResolver,
    )
    from src.connection_admin import ConnectionAdmin
    from src.internal_connections import (
        ConnectionRegistrationService,
        ConnectionResolutionService,
    )
    from src.registry import (
        BindingResolver,
        ConnectionRegistry,
        DynamoRegistryBackend,
        _deserialize,
        _serialize,
    )
    from src.onboarding import StripeOnboardingService
    from src.onboarding_state import DynamoOnboardingStateStore, OnboardingStateManager
    from src.migration_store import (
        DynamoMigrationStore,
        DynamoMigrationStatusStore,
        DynamoOfferReferenceGuard,
        SqsMigrationQueue,
    )
    from src.providers.stripe_adapter import (
        SecretsManagerStripeWebhookVerifier,
        SecretsManagerStripeClientFactory,
        StripeAdapter,
    )
    from src.stripe_commands import StripeCommandService
    from src.stripe_store import DynamoStripeCommandStore, DynamoStripeWebhookStore
    from src.smtp_activation import SmtpConnectionActivationService
    from src.subscription_migrations import (
        SubscriptionMigrationService,
        SubscriptionMigrationStatusService,
    )


class RuntimeCompositionError(RuntimeError):
    pass


class PublishedTaxPolicyVerifier:
    """Require a server-owned, draft-scoped approval for production tax changes."""

    def __init__(self, table_name: str, *, client: Any):
        if type(table_name) is not str or not table_name.strip() or client is None:
            raise RuntimeCompositionError("Tax verification is unavailable")
        self._table_name = table_name
        self._client = client

    def authorize(
        self, resolved: Any, expected_revision: Any
    ) -> tuple[str, str | None] | None:
        scope = getattr(getattr(resolved, "connection", None), "scope", None)
        connection = getattr(resolved, "connection", None)
        binding = getattr(resolved, "binding", None)
        connection_id = getattr(connection, "connection_id", None)
        metadata = getattr(getattr(resolved, "binding", None), "provider_metadata", {})
        tax_mode = metadata.get("taxMode") if isinstance(metadata, Mapping) else None
        approval_id = (
            metadata.get("taxApprovalId") if isinstance(metadata, Mapping) else None
        )
        if type(expected_revision) is not int or expected_revision < 1:
            return None
        if getattr(scope, "environment", None) == "test":
            if tax_mode not in {"manual-rate", "stripe-tax"}:
                return None
            return tax_mode, None
        connection_metadata = getattr(connection, "provider_metadata", {})
        account_reference = (
            connection_metadata.get("accountReference")
            if isinstance(connection_metadata, Mapping)
            else None
        )
        mode = getattr(connection, "mode", None)
        if (
            getattr(scope, "environment", None) != "production"
            or type(connection_id) is not str
            or type(approval_id) is not str
            or tax_mode not in {"manual-rate", "stripe-tax"}
            or type(account_reference) is not str
            or not account_reference
            or mode != "live"
            or getattr(binding, "mode", None) != mode
        ):
            return None
        sk = f"TAX_APPROVAL#{connection_id}#{approval_id}"
        try:
            response = self._client.get_item(
                TableName=self._table_name,
                Key=_serialize({"pk": scope.partition_key, "sk": sk}),
                ConsistentRead=True,
            )
            raw = response.get("Item") if isinstance(response, dict) else None
            record = _deserialize(raw) if raw is not None else None
        except Exception:
            return None
        expected_keys = {
            "pk",
            "sk",
            "itemType",
            *scope.fields().keys(),
            "connectionId",
            "approvalId",
            "provider",
            "taxMode",
            "accountHash",
            "mode",
            "expectedRevision",
            "status",
            "revision",
            "approvalHash",
        }
        account_hash = hashlib.sha256(account_reference.encode("utf-8")).hexdigest()
        if (
            not isinstance(record, dict)
            or set(record) != expected_keys
            or record.get("pk") != scope.partition_key
            or record.get("sk") != sk
            or record.get("itemType") != "StripeTaxApproval"
            or any(
                record.get(key) != expected for key, expected in scope.fields().items()
            )
            or record.get("connectionId") != connection_id
            or record.get("approvalId") != approval_id
            or record.get("provider") != "stripe"
            or record.get("taxMode") != tax_mode
            or record.get("accountHash") != account_hash
            or record.get("mode") != mode
            or record.get("expectedRevision") != expected_revision
            or record.get("status") != "approved"
            or type(record.get("revision")) is not int
            or record["revision"] < 1
            or record.get("approvalHash") != _tax_approval_hash(record)
        ):
            return None
        return tax_mode, record["approvalHash"]

    def validate_state(self, authorization: Any, state: Any) -> bool:
        if (
            not isinstance(authorization, tuple)
            or len(authorization) != 2
            or authorization[0] not in {"manual-rate", "stripe-tax"}
            or (
                authorization[1] is not None
                and (
                    type(authorization[1]) is not str
                    or len(authorization[1]) != 64
                )
            )
            or not isinstance(state, dict)
        ):
            return False
        tax_mode = authorization[0]
        automatic_tax = state.get("automaticTax")
        if (
            not isinstance(automatic_tax, dict)
            or set(automatic_tax) != {"enabled"}
            or type(automatic_tax["enabled"]) is not bool
        ):
            return False
        if tax_mode == "stripe-tax":
            return automatic_tax["enabled"] is True
        if automatic_tax["enabled"] is not False:
            return False
        default_rates = state.get("defaultTaxRateIds")
        items = state.get("items")
        if (
            not isinstance(default_rates, list)
            or any(type(rate) is not str for rate in default_rates)
            or not isinstance(items, list)
            or any(
                not isinstance(item, dict)
                or not isinstance(item.get("taxRateIds"), list)
                or any(type(rate) is not str for rate in item["taxRateIds"])
                for item in items
            )
        ):
            return False
        return bool(default_rates or any(item["taxRateIds"] for item in items))


def _tax_approval_hash(record: Mapping[str, Any]) -> str:
    fields = (
        "environment",
        "tenantId",
        "draftId",
        "domain",
        "connectionId",
        "approvalId",
        "provider",
        "taxMode",
        "accountHash",
        "mode",
        "expectedRevision",
        "status",
        "revision",
    )
    try:
        payload = {field: record[field] for field in fields}
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    except (KeyError, TypeError, UnicodeError, ValueError):
        return ""
    return hashlib.sha256(encoded).hexdigest()


def browser_runtime() -> dict[str, Any]:
    boto3 = _boto3()
    dynamodb = boto3.resource("dynamodb")
    policy_resolver = PublishedPolicyResolver(
        dynamodb.Table(_required("CONFIG_REGISTRY_TABLE_NAME")),
        boto3.client("s3"),
        _required("CONFIG_PAYLOADS_BUCKET_NAME"),
    )
    registry = _registry(boto3)
    return {
        "policy_resolver": policy_resolver,
        "auth_store": DynamoAuthStore(
            _required("AUTH_SESSION_TABLE_NAME"),
            _required("AUTH_USER_STATE_TABLE_NAME"),
            dynamodb=dynamodb,
        ),
        "registry": registry,
        "environment": _environment(),
        "now_epoch": int(time.time()),
    }


def connection_registration_runtime() -> dict[str, Any]:
    boto3 = _boto3()
    dynamodb = boto3.resource("dynamodb")
    policies = PublishedPolicyResolver(
        dynamodb.Table(_required("CONFIG_REGISTRY_TABLE_NAME")),
        boto3.client("s3"),
        _required("CONFIG_PAYLOADS_BUCKET_NAME"),
    )
    admin = ConnectionAdmin(_registry(boto3), boto3.client("secretsmanager"))
    return {"service": ConnectionRegistrationService(policies, admin)}


def connection_resolution_runtime() -> dict[str, Any]:
    return {
        "service": ConnectionResolutionService(
            BindingResolver(_registry(_boto3())),
            _required("SMTP_TEST_SHARED_ACCOUNT_CLAIM_HASH"),
        )
    }


def smtp_connection_activation_runtime() -> dict[str, Any]:
    boto3 = _boto3()
    return {
        "service": SmtpConnectionActivationService(
            _registry(boto3),
            boto3.client("secretsmanager"),
            _required("SMTP_TEST_SHARED_ACCOUNT_CLAIM_HASH"),
        )
    }


def stripe_onboarding_runtime() -> dict[str, Any]:
    boto3 = _boto3()
    dynamodb = boto3.resource("dynamodb")
    policy_resolver = PublishedPolicyResolver(
        dynamodb.Table(_required("CONFIG_REGISTRY_TABLE_NAME")),
        boto3.client("s3"),
        _required("CONFIG_PAYLOADS_BUCKET_NAME"),
    )
    registry = _registry(boto3)
    adapter = StripeAdapter(
        accounts_v2_verified=False,
        client_factory=SecretsManagerStripeClientFactory(
            boto3.client("secretsmanager")
        ),
    )
    return {
        "policy_resolver": policy_resolver,
        "auth_store": DynamoAuthStore(
            _required("AUTH_SESSION_TABLE_NAME"),
            _required("AUTH_USER_STATE_TABLE_NAME"),
            dynamodb=dynamodb,
        ),
        "binding_resolver": BindingResolver(registry),
        "onboarding_service": StripeOnboardingService(
            OnboardingStateManager(
                DynamoOnboardingStateStore(
                    dynamodb.Table(_required("INTEGRATION_REGISTRY_TABLE_NAME"))
                )
            ),
            adapter,
            registry,
        ),
        "environment": _environment(),
        "now_epoch": int(time.time()),
    }


def stripe_command_runtime() -> dict[str, Any]:
    boto3 = _boto3()
    dynamodb = boto3.resource("dynamodb")
    policies = PublishedPolicyResolver(
        dynamodb.Table(_required("CONFIG_REGISTRY_TABLE_NAME")),
        boto3.client("s3"),
        _required("CONFIG_PAYLOADS_BUCKET_NAME"),
    )
    registry = _registry(boto3)
    provider = StripeAdapter(
        accounts_v2_verified=False,
        client_factory=SecretsManagerStripeClientFactory(
            boto3.client("secretsmanager")
        ),
    )
    registry_table_name = _required("INTEGRATION_REGISTRY_TABLE_NAME")
    dynamodb_client = boto3.client("dynamodb")
    return {
        "service": StripeCommandService(
            BindingResolver(registry),
            DynamoStripeCommandStore(
                registry_table_name,
                client=dynamodb_client,
            ),
            provider,
            PublishedCheckoutRouteResolver(policies),
            now_epoch=lambda: int(time.time()),
            tax_verifier=PublishedTaxPolicyVerifier(
                registry_table_name, client=dynamodb_client
            ),
            reference_guard=DynamoOfferReferenceGuard(
                registry_table_name, client=dynamodb_client
            ),
        )
    }


def subscription_migration_runtime() -> dict[str, Any]:
    boto3 = _boto3()
    registry_table_name = _required("INTEGRATION_REGISTRY_TABLE_NAME")
    technical_table_name = _required("WEBHOOK_RECEIPT_TABLE_NAME")
    client = boto3.client("dynamodb")
    registry = _registry(boto3)
    return {
        "service": SubscriptionMigrationService(
            BindingResolver(registry),
            DynamoStripeCommandStore(registry_table_name, client=client),
            DynamoMigrationStore(
                registry_table_name,
                technical_table_name,
                client=client,
                now_epoch=lambda: int(time.time()),
            ),
            SqsMigrationQueue(
                _required("MIGRATION_WORK_QUEUE_URL"), client=boto3.client("sqs")
            ),
            tax_verifier=PublishedTaxPolicyVerifier(
                registry_table_name, client=client
            ),
            now_epoch=lambda: int(time.time()),
        )
    }


def subscription_migration_status_runtime() -> dict[str, Any]:
    boto3 = _boto3()
    registry_table_name = _required("INTEGRATION_REGISTRY_TABLE_NAME")
    return {
        "service": SubscriptionMigrationStatusService(
            BindingResolver(_registry(boto3)),
            DynamoMigrationStatusStore(
                registry_table_name, client=boto3.client("dynamodb")
            ),
        )
    }


def subscription_migration_worker_runtime() -> Any:
    try:
        from handlers.subscription_migration_worker import SubscriptionMigrationWorker
    except ModuleNotFoundError:
        from src.handlers.subscription_migration_worker import (
            SubscriptionMigrationWorker,
        )
    import secrets

    boto3 = _boto3()
    registry_table_name = _required("INTEGRATION_REGISTRY_TABLE_NAME")
    technical_table_name = _required("WEBHOOK_RECEIPT_TABLE_NAME")
    client = boto3.client("dynamodb")
    registry = _registry(boto3)
    return SubscriptionMigrationWorker(
        BindingResolver(registry),
        DynamoStripeCommandStore(registry_table_name, client=client),
        DynamoMigrationStore(
            registry_table_name,
            technical_table_name,
            client=client,
            now_epoch=lambda: int(time.time()),
        ),
        StripeAdapter(
            accounts_v2_verified=False,
            client_factory=SecretsManagerStripeClientFactory(
                boto3.client("secretsmanager")
            ),
        ),
        SqsMigrationQueue(
            _required("MIGRATION_WORK_QUEUE_URL"), client=boto3.client("sqs")
        ),
        PublishedTaxPolicyVerifier(registry_table_name, client=client),
        now_epoch=lambda: int(time.time()),
        jitter=lambda attempt: secrets.randbelow(min(31, max(1, 2**attempt))),
    )


def stripe_webhook_runtime() -> dict[str, Any]:
    boto3 = _boto3()
    environment = _environment()
    secret_id = f"/zoolanding/{environment}/integrations/stripe/connect-webhook"
    try:
        from common.metrics import emit_metric
    except ModuleNotFoundError:
        from src.common.metrics import emit_metric
    return {
        "verifier": SecretsManagerStripeWebhookVerifier(
            boto3.client("secretsmanager"), secret_id
        ),
        "registry": _registry(boto3),
        "store": DynamoStripeWebhookStore(
            _required("WEBHOOK_RECEIPT_TABLE_NAME"),
            client=boto3.client("dynamodb"),
        ),
        "environment": environment,
        "now_epoch": int(time.time()),
        "metric_sink": lambda name, value: emit_metric(
            name, value, environment=environment
        ),
    }


def stripe_event_worker_runtime() -> Any:
    try:
        from handlers.stripe_event_worker import StripeEventWorker
    except ModuleNotFoundError:
        from src.handlers.stripe_event_worker import StripeEventWorker
    boto3 = _boto3()
    provider = StripeAdapter(
        accounts_v2_verified=False,
        client_factory=SecretsManagerStripeClientFactory(
            boto3.client("secretsmanager")
        ),
    )
    registry_table_name = _required("INTEGRATION_REGISTRY_TABLE_NAME")
    technical_table_name = _required("WEBHOOK_RECEIPT_TABLE_NAME")
    client = boto3.client("dynamodb")
    return StripeEventWorker(
        _registry(boto3),
        DynamoStripeWebhookStore(
            technical_table_name,
            projection_table_name=registry_table_name,
            client=client,
        ),
        DynamoStripeCommandStore(
            registry_table_name,
            client=client,
        ),
        provider,
        DynamoMigrationStore(
            registry_table_name,
            technical_table_name,
            client=client,
            now_epoch=lambda: int(time.time()),
        ),
        SqsMigrationQueue(
            _required("MIGRATION_WORK_QUEUE_URL"), client=boto3.client("sqs")
        ),
    )


def integration_outbox_relay_runtime() -> Any:
    try:
        from handlers.integration_outbox_relay import (
            IntegrationOutboxRelay,
            SnsIntegrationEventPublisher,
        )
    except ModuleNotFoundError:
        from src.handlers.integration_outbox_relay import (
            IntegrationOutboxRelay,
            SnsIntegrationEventPublisher,
        )
    boto3 = _boto3()
    return IntegrationOutboxRelay(
        DynamoStripeWebhookStore(
            _required("WEBHOOK_RECEIPT_TABLE_NAME"),
            client=boto3.client("dynamodb"),
        ),
        SnsIntegrationEventPublisher(
            _required("INTEGRATION_EVENTS_TOPIC_ARN"),
            client=boto3.client("sns"),
        ),
    )


def _registry(boto3: Any) -> ConnectionRegistry:
    backend = DynamoRegistryBackend(
        _required("INTEGRATION_REGISTRY_TABLE_NAME"),
        client=boto3.client("dynamodb"),
    )
    return ConnectionRegistry(backend)


def _boto3() -> Any:
    try:
        import boto3  # type: ignore
    except Exception:
        raise RuntimeCompositionError("Runtime dependencies are unavailable") from None
    return boto3


def _environment() -> str:
    value = _required("ENVIRONMENT_NAME")
    if value not in {"test", "production"}:
        raise RuntimeCompositionError("Runtime environment is invalid")
    return value


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not 1 <= len(value) <= 255 or any(ord(character) < 33 for character in value):
        raise RuntimeCompositionError("Runtime configuration is unavailable")
    return value
