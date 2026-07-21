"""Immutable draft-scoped integration connection and binding contracts."""

from __future__ import annotations

from dataclasses import dataclass
import re
from types import MappingProxyType
from typing import Any, Mapping


TECHNICAL_TTL_SECONDS = 90 * 24 * 60 * 60
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}", re.ASCII)
_DOMAIN = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+",
    re.ASCII,
)
_STRIPE_ACCOUNT_REFERENCE = re.compile(r"acct_[A-Za-z0-9]{8,64}", re.ASCII)
_STRIPE_RESOURCE_REFERENCES = {
    "product": re.compile(r"prod_[A-Za-z0-9]{8,64}", re.ASCII),
    "price": re.compile(r"price_[A-Za-z0-9]{8,64}", re.ASCII),
    "promotion-code": re.compile(r"promo_[A-Za-z0-9]{8,64}", re.ASCII),
    "checkout-session": re.compile(r"cs_(?:test|live)_[A-Za-z0-9]{8,64}", re.ASCII),
    "subscription": re.compile(r"sub_[A-Za-z0-9]{8,64}", re.ASCII),
    "customer": re.compile(r"cus_[A-Za-z0-9]{8,64}", re.ASCII),
    "payment-intent": re.compile(r"pi_[A-Za-z0-9]{8,64}", re.ASCII),
    "refund": re.compile(r"re_[A-Za-z0-9]{8,64}", re.ASCII),
    "coupon": re.compile(r"[A-Za-z0-9]{8,64}", re.ASCII),
}
_PROVIDER_CAPABILITIES = {
    "stripe": frozenset(
        {
            "connect-onboarding",
            "checkout",
            "one-time-payments",
            "subscriptions",
            "prices",
            "coupons",
            "customer-portal",
        }
    ),
    "email.smtp": frozenset({"send"}),
}
_ADAPTER_VERSIONS = {"stripe": "v1", "email.smtp": "v1"}
_BINDING_KEYS = frozenset(
    {
        "id",
        "provider",
        "adapterVersion",
        "connectionId",
        "status",
        "mode",
        "capabilities",
    }
)
_STRIPE_KEYS = frozenset(
    {
        "accountModel",
        "chargeType",
        "feePayer",
        "taxMode",
        "taxApprovalId",
        "platformFeeMode",
        "webhookIngress",
    }
)
_STRIPE_REQUIRED = _STRIPE_KEYS - {"taxApprovalId"}


def _safe_id(value: object, name: str) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{name} is invalid")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def _expected_mode(environment: str) -> str:
    return "test" if environment == "test" else "live"


def _capabilities(provider: str, value: object) -> frozenset[str]:
    if (
        not isinstance(value, (set, frozenset, list, tuple))
        or not 1 <= len(value) <= 32
    ):
        raise ValueError("capabilities are invalid")
    if any(type(item) is not str or _SAFE_ID.fullmatch(item) is None for item in value):
        raise ValueError("capabilities are invalid")
    selected = frozenset(value)
    if len(selected) != len(value) or not selected.issubset(
        _PROVIDER_CAPABILITIES[provider]
    ):
        raise ValueError("capabilities are invalid")
    return selected


def _provider(provider: object, adapter_version: object) -> str:
    if type(provider) is not str or provider not in _PROVIDER_CAPABILITIES:
        raise ValueError("provider is invalid")
    if adapter_version != _ADAPTER_VERSIONS[provider]:
        raise ValueError("adapter version is invalid")
    return provider


def _stripe_binding_metadata(value: object) -> Mapping[str, Any]:
    if (
        not isinstance(value, Mapping)
        or not _STRIPE_REQUIRED.issubset(value)
        or not set(value).issubset(_STRIPE_KEYS)
    ):
        raise ValueError("Stripe binding metadata is invalid")
    expected = {
        "accountModel": "merchant",
        "chargeType": "direct",
        "feePayer": "connected-account",
        "platformFeeMode": "disabled",
        "webhookIngress": "direct-integrations-api",
    }
    if any(
        value.get(key) != expected_value for key, expected_value in expected.items()
    ):
        raise ValueError("Stripe binding metadata is invalid")
    if value.get("taxMode") not in {"unconfigured", "manual-rate", "stripe-tax"}:
        raise ValueError("Stripe binding metadata is invalid")
    if "taxApprovalId" in value:
        _safe_id(value["taxApprovalId"], "tax approval ID")
    return _freeze(dict(value))


@dataclass(frozen=True, slots=True)
class IntegrationScope:
    environment: str
    tenant_id: str
    draft_id: str
    domain: str

    def __post_init__(self) -> None:
        if self.environment not in {"test", "production"}:
            raise ValueError("environment is invalid")
        _safe_id(self.tenant_id, "tenant ID")
        _safe_id(self.draft_id, "draft ID")
        if (
            type(self.domain) is not str
            or not 4 <= len(self.domain) <= 253
            or _DOMAIN.fullmatch(self.domain) is None
        ):
            raise ValueError("domain is invalid")

    @property
    def partition_key(self) -> str:
        return f"ENV#{self.environment}#TENANT#{self.tenant_id}#DRAFT#{self.draft_id}"

    def fields(self) -> dict[str, str]:
        return {
            "environment": self.environment,
            "tenantId": self.tenant_id,
            "draftId": self.draft_id,
            "domain": self.domain,
        }


@dataclass(frozen=True, slots=True)
class IntegrationBinding:
    scope: IntegrationScope
    binding_id: str
    provider: str
    adapter_version: str
    connection_id: str
    status: str
    mode: str
    capabilities: frozenset[str]
    provider_metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        if type(self.scope) is not IntegrationScope:
            raise ValueError("scope is invalid")
        _safe_id(self.binding_id, "binding ID")
        _safe_id(self.connection_id, "connection ID")
        _provider(self.provider, self.adapter_version)
        if self.status not in {"active", "disabled"} or self.mode != _expected_mode(
            self.scope.environment
        ):
            raise ValueError("binding state is invalid")
        object.__setattr__(
            self, "capabilities", _capabilities(self.provider, self.capabilities)
        )
        if self.provider == "stripe":
            metadata = _stripe_binding_metadata(self.provider_metadata)
        elif self.provider_metadata:
            raise ValueError("provider metadata is invalid")
        else:
            metadata = MappingProxyType({})
        object.__setattr__(self, "provider_metadata", metadata)

    @classmethod
    def from_mapping(
        cls, scope: IntegrationScope, value: object
    ) -> "IntegrationBinding":
        if not isinstance(value, Mapping):
            raise ValueError("binding is invalid")
        provider = value.get("provider")
        keys = _BINDING_KEYS | ({"stripe"} if provider == "stripe" else set())
        if set(value) != keys:
            raise ValueError("binding is invalid")
        validated_provider = _provider(provider, value.get("adapterVersion"))
        selected_capabilities = _capabilities(
            validated_provider, value.get("capabilities")
        )
        return cls(
            scope=scope,
            binding_id=value.get("id"),
            provider=validated_provider,
            adapter_version=value.get("adapterVersion"),
            connection_id=value.get("connectionId"),
            status=value.get("status"),
            mode=value.get("mode"),
            capabilities=selected_capabilities,
            provider_metadata=value.get("stripe") or {},
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "pk": self.scope.partition_key,
            "sk": f"BINDING#{self.binding_id}",
            "itemType": "IntegrationBinding",
            **self.scope.fields(),
            "bindingId": self.binding_id,
            "provider": self.provider,
            "adapterVersion": self.adapter_version,
            "connectionId": self.connection_id,
            "status": self.status,
            "mode": self.mode,
            "capabilities": sorted(self.capabilities),
            "providerMetadata": _plain(self.provider_metadata),
        }


@dataclass(frozen=True, slots=True)
class IntegrationConnection:
    scope: IntegrationScope
    connection_id: str
    provider: str
    adapter_version: str
    status: str
    mode: str
    capabilities: frozenset[str]
    provider_metadata: Mapping[str, Any]
    revision: int = 1

    def __post_init__(self) -> None:
        if type(self.scope) is not IntegrationScope:
            raise ValueError("scope is invalid")
        _safe_id(self.connection_id, "connection ID")
        _provider(self.provider, self.adapter_version)
        if type(self.revision) is not int or self.revision < 1:
            raise ValueError("connection revision is invalid")
        if self.status not in {
            "pending",
            "active",
            "disabled",
        } or self.mode != _expected_mode(self.scope.environment):
            raise ValueError("connection state is invalid")
        object.__setattr__(
            self, "capabilities", _capabilities(self.provider, self.capabilities)
        )
        metadata = self._validated_metadata(dict(self.provider_metadata))
        object.__setattr__(self, "provider_metadata", _freeze(metadata))

    def _validated_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        if self.provider == "stripe":
            if not set(metadata).issubset({"accountReference", "resourceMappings"}):
                raise ValueError("Stripe connection metadata is invalid")
            if "accountReference" in metadata:
                account_reference = metadata["accountReference"]
                if (
                    type(account_reference) is not str
                    or _STRIPE_ACCOUNT_REFERENCE.fullmatch(account_reference) is None
                ):
                    raise ValueError("Stripe connection metadata is invalid")
            elif self.scope.environment == "production" and self.status == "active":
                raise ValueError("Stripe connection metadata is invalid")
            mappings = metadata.get("resourceMappings", {})
            if not isinstance(mappings, Mapping) or len(mappings) > 64:
                raise ValueError("provider resource mappings are invalid")
            for kind, reference in mappings.items():
                pattern = _STRIPE_RESOURCE_REFERENCES.get(kind)
                if (
                    pattern is None
                    or type(reference) is not str
                    or pattern.fullmatch(reference) is None
                ):
                    raise ValueError("provider resource mappings are invalid")
            return metadata
        expected_domain = (
            "zoolandingpage.com.mx"
            if self.scope.environment == "test"
            else self.scope.domain
        )
        if metadata != {
            "adapterId": "smtp2go-smtp-v1",
            "host": "mail.smtp2go.com",
            "port": 465,
            "canonicalSendingDomain": expected_domain,
            "accountOwnershipState": "audited",
        }:
            raise ValueError("SMTP connection metadata is invalid")
        return metadata

    @property
    def credential_reference(self) -> str:
        if self.provider == "stripe":
            return (
                f"/zoolanding/{self.scope.environment}/integrations/{self.scope.tenant_id}/"
                f"{self.scope.draft_id}/stripe/{self.connection_id}"
            )
        return (
            f"/zoolanding/{self.scope.environment}/{self.scope.tenant_id}/{self.scope.draft_id}/"
            f"notifications/smtp/{self.connection_id}"
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "pk": self.scope.partition_key,
            "sk": f"CONNECTION#{self.connection_id}",
            "itemType": "IntegrationConnection",
            **self.scope.fields(),
            "connectionId": self.connection_id,
            "provider": self.provider,
            "adapterVersion": self.adapter_version,
            "status": self.status,
            "revision": self.revision,
            "mode": self.mode,
            "capabilities": sorted(self.capabilities),
            "credentialReference": self.credential_reference,
            "providerMetadata": _plain(self.provider_metadata),
        }

    def isolation_claims(self) -> frozenset[str]:
        """Return server-only uniqueness claims for production registration."""
        if self.scope.environment != "production":
            return frozenset()
        claims = {f"CREDENTIAL#{self.credential_reference}"}
        if self.provider == "stripe" and "accountReference" in self.provider_metadata:
            claims.add(
                f"PROVIDER#stripe#ACCOUNT#{self.provider_metadata['accountReference']}"
            )
        elif self.provider == "email.smtp":
            claims.add(
                "PROVIDER#email.smtp#DOMAIN#"
                f"{self.provider_metadata['canonicalSendingDomain']}"
            )
        return frozenset(claims)


def assert_isolated_connection_assignment(
    candidate: IntegrationConnection,
    existing_connections: object,
) -> None:
    """Reject a production provider/domain/credential claim reused by another scope."""
    if type(candidate) is not IntegrationConnection:
        raise ValueError("connection assignment is invalid")
    if not isinstance(existing_connections, (list, tuple, set, frozenset)):
        raise ValueError("connection assignments are invalid")
    candidate_claims = candidate.isolation_claims()
    if not candidate_claims:
        return
    candidate_identity = (candidate.scope, candidate.connection_id)
    for existing in existing_connections:
        if type(existing) is not IntegrationConnection:
            raise ValueError("connection assignments are invalid")
        if (existing.scope, existing.connection_id) == candidate_identity:
            continue
        if candidate_claims.intersection(existing.isolation_claims()):
            raise ValueError("connection assignment is not isolated")


def technical_expiry(now_epoch: object) -> int:
    if (
        type(now_epoch) is not int
        or not 0 <= now_epoch <= 9_999_999_999 - TECHNICAL_TTL_SECONDS
    ):
        raise ValueError("current time is invalid")
    return now_epoch + TECHNICAL_TTL_SECONDS
