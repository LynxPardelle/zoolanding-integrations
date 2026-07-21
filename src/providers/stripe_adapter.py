"""Stripe Connect adapter boundary with a verified Accounts v2 gate."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
from typing import Any, Protocol
from urllib.parse import urlsplit

try:
    from domain.integrations import IntegrationBinding, IntegrationConnection
except ModuleNotFoundError:
    from src.domain.integrations import IntegrationBinding, IntegrationConnection


_DOMAIN = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+",
    re.ASCII,
)
_ACCOUNT_REFERENCE = re.compile(r"acct_[A-Za-z0-9]{8,64}", re.ASCII)
_PROVIDER_HOST = "connect.stripe.com"
_REFRESH_PATH = "/admin/integrations/stripe/refresh"
_RETURN_PATH = "/admin/integrations/stripe/return"


class StripeAdapterError(RuntimeError):
    pass


class StripeClient(Protocol):
    def create_v1_handoff(self, **kwargs: Any) -> dict[str, Any]: ...

    def create_v2_handoff(self, **kwargs: Any) -> dict[str, Any]: ...

    def retrieve_v1_account(self, **kwargs: Any) -> dict[str, Any]: ...

    def retrieve_v2_account(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class OnboardingCallbacks:
    refresh_url: str
    return_url: str


def build_onboarding_callbacks(domain: object) -> OnboardingCallbacks:
    if type(domain) is not str or _DOMAIN.fullmatch(domain) is None:
        raise StripeAdapterError("Stripe onboarding is unavailable")
    return OnboardingCallbacks(
        refresh_url=f"https://{domain}{_REFRESH_PATH}",
        return_url=f"https://{domain}{_RETURN_PATH}",
    )


class StripeAdapter:
    def __init__(self, client: StripeClient, *, accounts_v2_verified: bool):
        if client is None or type(accounts_v2_verified) is not bool:
            raise StripeAdapterError("Stripe adapter is unavailable")
        self._client = client
        self._accounts_v2_verified = accounts_v2_verified

    def create_onboarding_handoff(
        self,
        binding: IntegrationBinding,
        connection: IntegrationConnection,
        *,
        callbacks: OnboardingCallbacks,
        state: object,
    ) -> str:
        account_reference = _validated_context(binding, connection)
        _validated_callbacks(callbacks, connection.scope.domain)
        if (
            type(state) is not str
            or not 1 <= len(state) <= 1024
            or any(ord(character) < 33 for character in state)
        ):
            raise StripeAdapterError("Stripe onboarding is unavailable")
        operation = (
            self._client.create_v2_handoff
            if self._accounts_v2_verified
            else self._client.create_v1_handoff
        )
        try:
            response = operation(
                stripe_account=account_reference,
                charge_type="direct",
                refresh_url=callbacks.refresh_url,
                return_url=callbacks.return_url,
                state=state,
            )
        except Exception:
            raise StripeAdapterError("Stripe onboarding is unavailable") from None
        return _provider_handoff_url(response)

    def retrieve_canonical_status(
        self,
        binding: IntegrationBinding,
        connection: IntegrationConnection,
    ) -> dict[str, Any]:
        account_reference = _validated_context(binding, connection)
        operation = (
            self._client.retrieve_v2_account
            if self._accounts_v2_verified
            else self._client.retrieve_v1_account
        )
        try:
            response = operation(stripe_account=account_reference)
        except Exception:
            raise StripeAdapterError("Stripe status is unavailable") from None
        if not isinstance(response, dict):
            raise StripeAdapterError("Stripe status is unavailable")
        charges_enabled = response.get("charges_enabled")
        details_submitted = response.get("details_submitted")
        requirements = response.get("requirements")
        due = (
            requirements.get("currently_due")
            if isinstance(requirements, dict)
            else None
        )
        if (
            type(charges_enabled) is not bool
            or type(details_submitted) is not bool
            or not isinstance(due, list)
            or len(due) > 100
        ):
            raise StripeAdapterError("Stripe status is unavailable")
        return {
            "status": "ready" if charges_enabled and details_submitted else "pending",
            "chargesEnabled": charges_enabled,
            "detailsSubmitted": details_submitted,
            "requirementsDueCount": len(due),
        }


def _validated_context(
    binding: IntegrationBinding,
    connection: IntegrationConnection,
) -> str:
    if (
        type(binding) is not IntegrationBinding
        or type(connection) is not IntegrationConnection
        or binding.scope != connection.scope
        or binding.provider != "stripe"
        or connection.provider != "stripe"
        or binding.connection_id != connection.connection_id
        or binding.mode != connection.mode
        or "connect-onboarding" not in binding.capabilities
        or "connect-onboarding" not in connection.capabilities
        or binding.provider_metadata.get("chargeType") != "direct"
        or binding.provider_metadata.get("feePayer") != "connected-account"
    ):
        raise StripeAdapterError("Stripe onboarding is unavailable")
    account_reference = connection.provider_metadata.get("accountReference")
    if (
        type(account_reference) is not str
        or _ACCOUNT_REFERENCE.fullmatch(account_reference) is None
    ):
        raise StripeAdapterError("Stripe onboarding is unavailable")
    return account_reference


def _validated_callbacks(callbacks: OnboardingCallbacks, expected_domain: str) -> None:
    if type(callbacks) is not OnboardingCallbacks:
        raise StripeAdapterError("Stripe onboarding is unavailable")
    expected = build_onboarding_callbacks(expected_domain)
    if callbacks != expected:
        raise StripeAdapterError("Stripe onboarding is unavailable")


def _provider_handoff_url(value: object) -> str:
    if not isinstance(value, dict) or set(value) != {"url"}:
        raise StripeAdapterError("Stripe onboarding is unavailable")
    url = value.get("url")
    if type(url) is not str or len(url) > 2048:
        raise StripeAdapterError("Stripe onboarding is unavailable")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise StripeAdapterError("Stripe onboarding is unavailable") from None
    try:
        ipaddress.ip_address(parsed.hostname or "")
        is_ip = True
    except ValueError:
        is_ip = False
    if (
        parsed.scheme != "https"
        or parsed.hostname != _PROVIDER_HOST
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or is_ip
        or not parsed.path.startswith("/")
        or parsed.fragment
    ):
        raise StripeAdapterError("Stripe onboarding is unavailable")
    return url
