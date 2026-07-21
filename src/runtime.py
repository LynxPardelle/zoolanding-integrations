"""Fail-closed AWS dependency composition for implemented control-plane routes."""

from __future__ import annotations

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
    from providers.stripe_adapter import (
        SecretsManagerStripeClientFactory,
        StripeAdapter,
        StripeWebhookVerifier,
    )
    from stripe_commands import StripeCommandService
    from stripe_store import DynamoStripeCommandStore, DynamoStripeWebhookStore
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
    from src.providers.stripe_adapter import (
        SecretsManagerStripeClientFactory,
        StripeAdapter,
        StripeWebhookVerifier,
    )
    from src.stripe_commands import StripeCommandService
    from src.stripe_store import DynamoStripeCommandStore, DynamoStripeWebhookStore


class RuntimeCompositionError(RuntimeError):
    pass


class PublishedTaxPolicyVerifier:
    """Require a server-owned, draft-scoped approval for production tax changes."""

    def __init__(self, table_name: str, *, client: Any):
        if type(table_name) is not str or not table_name.strip() or client is None:
            raise RuntimeCompositionError("Tax verification is unavailable")
        self._table_name = table_name
        self._client = client

    def __call__(self, resolved: Any, state: Any, target: Any) -> bool:
        del target
        scope = getattr(getattr(resolved, "connection", None), "scope", None)
        if getattr(scope, "environment", None) == "test":
            return True
        connection_id = getattr(
            getattr(resolved, "connection", None), "connection_id", None
        )
        metadata = getattr(getattr(resolved, "binding", None), "provider_metadata", {})
        tax_mode = metadata.get("taxMode") if isinstance(metadata, Mapping) else None
        approval_id = (
            metadata.get("taxApprovalId") if isinstance(metadata, Mapping) else None
        )
        if (
            getattr(scope, "environment", None) != "production"
            or type(connection_id) is not str
            or type(approval_id) is not str
            or tax_mode not in {"manual-rate", "stripe-tax"}
            or not isinstance(state, dict)
        ):
            return False
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
            return False
        expected_keys = {
            "pk",
            "sk",
            "itemType",
            *scope.fields().keys(),
            "connectionId",
            "approvalId",
            "provider",
            "taxMode",
            "status",
            "revision",
        }
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
            or record.get("status") != "approved"
            or type(record.get("revision")) is not int
            or record["revision"] < 1
        ):
            return False
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
        return bool(
            isinstance(default_rates, list)
            and isinstance(items, list)
            and (
                default_rates
                or any(
                    isinstance(item, dict) and item.get("taxRateIds") for item in items
                )
            )
        )


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
        "service": ConnectionResolutionService(BindingResolver(_registry(_boto3())))
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
        )
    }


def stripe_webhook_runtime() -> dict[str, Any]:
    boto3 = _boto3()
    environment = _environment()
    secret_id = f"/zoolanding/{environment}/integrations/stripe/connect-webhook"
    try:
        response = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)
    except Exception:
        raise RuntimeCompositionError("Runtime dependencies are unavailable") from None
    secret = response.get("SecretString") if isinstance(response, dict) else None
    return {
        "verifier": StripeWebhookVerifier(secret),
        "registry": _registry(boto3),
        "store": DynamoStripeWebhookStore(
            _required("WEBHOOK_RECEIPT_TABLE_NAME"),
            client=boto3.client("dynamodb"),
        ),
        "environment": environment,
        "now_epoch": int(time.time()),
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
    return StripeEventWorker(
        _registry(boto3),
        DynamoStripeWebhookStore(
            _required("WEBHOOK_RECEIPT_TABLE_NAME"),
            client=boto3.client("dynamodb"),
        ),
        DynamoStripeCommandStore(
            _required("INTEGRATION_REGISTRY_TABLE_NAME"),
            client=boto3.client("dynamodb"),
        ),
        provider,
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
