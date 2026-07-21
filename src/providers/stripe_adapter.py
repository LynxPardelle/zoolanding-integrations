"""Stripe Connect adapter boundary with a verified Accounts v2 gate."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import re
from typing import Any, Mapping, Protocol
import urllib.request
from urllib.parse import urlencode, urlsplit, urlunsplit

try:
    from domain.integrations import IntegrationBinding, IntegrationConnection
    from domain.operations import STRIPE_WEBHOOK_EVENT_TYPES
except ModuleNotFoundError:
    from src.domain.integrations import IntegrationBinding, IntegrationConnection
    from src.domain.operations import STRIPE_WEBHOOK_EVENT_TYPES


_DOMAIN = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+",
    re.ASCII,
)
_ACCOUNT_REFERENCE = re.compile(r"acct_[A-Za-z0-9]{8,64}", re.ASCII)
_MAPPING_HINT = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}", re.ASCII)
_PROVIDER_HOST = "connect.stripe.com"
_REFRESH_PATH = "/admin/integrations/stripe/refresh"
_RETURN_PATH = "/admin/integrations/stripe/return"


class StripeAdapterError(RuntimeError):
    pass


class StripeWebhookVerifier:
    """Official SDK signature verifier over the unmodified request bytes."""

    _SECRET = re.compile(r"whsec_[A-Za-z0-9_]{16,240}", re.ASCII)

    def __init__(self, secret: object):
        if type(secret) is not str or self._SECRET.fullmatch(secret) is None:
            raise StripeAdapterError("Stripe webhook verification is unavailable")
        self._secret = secret

    def verify(self, raw: object, signature: object) -> Any:
        if (
            type(raw) is not bytes
            or not raw
            or len(raw) > 1024 * 1024
            or type(signature) is not str
            or not 1 <= len(signature) <= 4096
        ):
            raise StripeAdapterError("Stripe webhook verification failed")
        try:
            import stripe  # type: ignore

            return stripe.Webhook.construct_event(
                payload=raw,
                sig_header=signature,
                secret=self._secret,
                tolerance=300,
            )
        except Exception:
            raise StripeAdapterError("Stripe webhook verification failed") from None


class StripeClient(Protocol):
    def create_v1_handoff(self, **kwargs: Any) -> dict[str, Any]: ...

    def create_v2_handoff(self, **kwargs: Any) -> dict[str, Any]: ...

    def retrieve_v1_account(self, **kwargs: Any) -> dict[str, Any]: ...

    def retrieve_v2_account(self, **kwargs: Any) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class OnboardingCallbacks:
    refresh_url: str
    return_url: str


def build_onboarding_callbacks(domain: object, routes: object) -> OnboardingCallbacks:
    if (
        type(domain) is not str
        or _DOMAIN.fullmatch(domain) is None
        or not isinstance(routes, Mapping)
        or set(routes) != {"returnPath", "refreshPath"}
        or any(
            type(routes[key]) is not str
            or not routes[key].startswith("/")
            or routes[key].startswith("//")
            or any(character in routes[key] for character in "\\\t\r\n ?#:")
            for key in ("returnPath", "refreshPath")
        )
    ):
        raise StripeAdapterError("Stripe onboarding is unavailable")
    return OnboardingCallbacks(
        refresh_url=f"https://{domain}{routes['refreshPath']}",
        return_url=f"https://{domain}{routes['returnPath']}",
    )


class StripeAdapter:
    def __init__(
        self,
        client: StripeClient | None = None,
        *,
        accounts_v2_verified: bool,
        client_factory: Any = None,
    ):
        if accounts_v2_verified is not False or (client is None) == (
            client_factory is None
        ):
            raise StripeAdapterError("Stripe adapter is unavailable")
        self._client = client
        self._client_factory = client_factory
        self._accounts_v2_verified = accounts_v2_verified

    def _client_for(self, connection: IntegrationConnection) -> Any:
        if self._client_factory is None:
            return self._client
        try:
            return self._client_factory.client_for(connection)
        except Exception:
            raise StripeAdapterError("Stripe operation is unavailable") from None

    def create_oauth_handoff(
        self,
        binding: IntegrationBinding,
        connection: IntegrationConnection,
        *,
        callbacks: OnboardingCallbacks,
        state: object,
    ) -> str:
        _validate_pending_onboarding_context(binding, connection, "oauth-standard-v1")
        _validated_callbacks(callbacks, connection.scope.domain)
        if (
            type(state) is not str
            or not 1 <= len(state) <= 1024
            or any(ord(character) < 33 for character in state)
        ):
            raise StripeAdapterError("Stripe onboarding is unavailable")
        client = self._client_for(connection)
        try:
            response = client.create_oauth_handoff(
                redirect_uri=callbacks.return_url,
                state=state,
            )
        except Exception:
            raise StripeAdapterError("Stripe onboarding is unavailable") from None
        return _provider_handoff_url(response)

    def exchange_oauth_code(
        self,
        binding: IntegrationBinding,
        connection: IntegrationConnection,
        *,
        code: object,
        redirect_uri: object,
    ) -> str:
        _validate_pending_onboarding_context(binding, connection, "oauth-standard-v1")
        if (
            type(code) is not str
            or not 1 <= len(code) <= 1024
            or any(ord(character) < 33 for character in code)
            or type(redirect_uri) is not str
        ):
            raise StripeAdapterError("Stripe onboarding is unavailable")
        _validated_callbacks(
            OnboardingCallbacks(redirect_uri, redirect_uri), connection.scope.domain
        )
        try:
            response = self._client_for(connection).exchange_oauth_code(
                code=code, redirect_uri=redirect_uri
            )
        except Exception:
            raise StripeAdapterError("Stripe onboarding is unavailable") from None
        return _provider_id(response, "accountReference", "acct_")

    def create_controller_account(
        self,
        binding: IntegrationBinding,
        connection: IntegrationConnection,
        *,
        idempotency_key: object,
    ) -> str:
        _validate_pending_onboarding_context(
            binding, connection, "controller-account-link-v1"
        )
        try:
            response = self._client_for(connection).create_controller_account(
                idempotency_key=_provider_key(idempotency_key, "controller-account")
            )
        except Exception:
            raise StripeAdapterError("Stripe onboarding is unavailable") from None
        return _provider_id(response, "id", "acct_")

    def create_account_link(
        self,
        binding: IntegrationBinding,
        connection: IntegrationConnection,
        *,
        callbacks: OnboardingCallbacks,
        state: object,
        idempotency_key: object,
    ) -> str:
        account = validate_stripe_context(binding, connection, "connect-onboarding")
        if (
            binding.provider_metadata.get("accountStrategy")
            != "controller-account-link-v1"
        ):
            raise StripeAdapterError("Stripe onboarding is unavailable")
        _validated_callbacks(callbacks, connection.scope.domain)
        try:
            response = self._client_for(connection).create_v1_handoff(
                stripe_account=account,
                refresh_url=callbacks.refresh_url,
                return_url=callbacks.return_url,
                state=state,
                idempotency_key=_provider_key(idempotency_key, "account-link"),
            )
        except Exception:
            raise StripeAdapterError("Stripe onboarding is unavailable") from None
        return _provider_handoff_url(response)

    def deauthorize_oauth_account(
        self,
        binding: IntegrationBinding,
        connection: IntegrationConnection,
    ) -> None:
        account = validate_stripe_context(binding, connection, "connect-onboarding")
        if binding.provider_metadata.get("accountStrategy") != "oauth-standard-v1":
            raise StripeAdapterError("Stripe deauthorization is unavailable")
        try:
            result = self._client_for(connection).deauthorize_oauth_account(
                stripe_account=account
            )
        except Exception:
            raise StripeAdapterError("Stripe deauthorization is unavailable") from None
        if result is not None:
            raise StripeAdapterError("Stripe deauthorization is unavailable")

    def retrieve_canonical_status(
        self,
        binding: IntegrationBinding,
        connection: IntegrationConnection,
    ) -> dict[str, Any]:
        account_reference = validate_stripe_context(
            binding, connection, "connect-onboarding"
        )
        client = self._client_for(connection)
        operation = (
            client.retrieve_v2_account
            if self._accounts_v2_verified
            else client.retrieve_v1_account
        )
        try:
            response = operation(stripe_account=account_reference)
        except Exception:
            raise StripeAdapterError("Stripe status is unavailable") from None
        if not isinstance(response, dict):
            raise StripeAdapterError("Stripe status is unavailable")
        charges_enabled = response.get("charges_enabled")
        payouts_enabled = response.get("payouts_enabled")
        details_submitted = response.get("details_submitted")
        capabilities = response.get("capabilities")
        requirements = response.get("requirements")
        due = (
            requirements.get("currently_due")
            if isinstance(requirements, dict)
            else None
        )
        if (
            type(charges_enabled) is not bool
            or type(payouts_enabled) is not bool
            or type(details_submitted) is not bool
            or not isinstance(capabilities, dict)
            or capabilities.get("card_payments")
            not in {
                "active",
                "inactive",
                "pending",
                "unrequested",
            }
            or not isinstance(due, list)
            or len(due) > 100
        ):
            raise StripeAdapterError("Stripe status is unavailable")
        capabilities_ready = capabilities["card_payments"] == "active"
        ready = (
            charges_enabled
            and payouts_enabled
            and details_submitted
            and capabilities_ready
            and not due
        )
        return {
            "status": "ready" if ready else "pending",
            "chargesEnabled": charges_enabled,
            "payoutsEnabled": payouts_enabled,
            "detailsSubmitted": details_submitted,
            "capabilitiesReady": capabilities_ready,
            "requirementsDueCount": len(due),
        }

    def create_product(self, resolved, resource_id, idempotency_key):
        client, account = self._commerce_context(resolved, "prices")
        response = _provider_call(
            client.create_product,
            stripe_account=account,
            resource_id=resource_id,
            idempotency_key=_provider_key(idempotency_key, "product"),
        )
        return _provider_id(response, "id", "prod_")

    def create_price(
        self, resolved, product_id, snapshot, resource_id, idempotency_key
    ):
        client, account = self._commerce_context(resolved, "prices")
        response = _provider_call(
            client.create_price,
            stripe_account=account,
            product_id=product_id,
            snapshot=snapshot,
            resource_id=resource_id,
            idempotency_key=_provider_key(idempotency_key, "price"),
        )
        return _provider_id(response, "id", "price_")

    def deactivate_offer(self, resolved, product_id, price_id, idempotency_key) -> None:
        client, account = self._commerce_context(resolved, "prices")
        _provider_call(
            client.deactivate_offer,
            stripe_account=account,
            product_id=product_id,
            price_id=price_id,
            idempotency_key=_provider_key(idempotency_key, "offer-deactivate"),
        )

    def update_product_presentation(
        self, resolved, product_id, snapshot, idempotency_key
    ) -> None:
        client, account = self._commerce_context(resolved, "prices")
        _provider_call(
            client.update_product_presentation,
            stripe_account=account,
            product_id=product_id,
            snapshot=snapshot,
            idempotency_key=_provider_key(idempotency_key, "presentation"),
        )

    def create_discount(
        self, resolved, snapshot, product_ids, idempotency_key
    ) -> dict[str, str]:
        client, account = self._commerce_context(resolved, "coupons")
        response = _provider_call(
            client.create_discount,
            stripe_account=account,
            snapshot=snapshot,
            product_ids=product_ids,
            idempotency_key=_provider_key(idempotency_key, "discount"),
        )
        return {
            "couponId": _provider_id(response, "couponId", None),
            "promotionCodeId": _provider_id(response, "promotionCodeId", "promo_"),
        }

    def deactivate_discount(
        self,
        resolved,
        coupon_id,
        promotion_code_id,
        target,
        idempotency_key,
    ) -> None:
        client, account = self._commerce_context(resolved, "coupons")
        _provider_call(
            client.deactivate_discount,
            stripe_account=account,
            coupon_id=coupon_id,
            promotion_code_id=promotion_code_id,
            target=target,
            idempotency_key=_provider_key(idempotency_key, "discount-lifecycle"),
        )

    def update_discount_presentation(
        self,
        resolved,
        coupon_id,
        snapshot,
        idempotency_key,
    ) -> None:
        client, account = self._commerce_context(resolved, "coupons")
        response = _provider_call(
            client.update_discount_presentation,
            stripe_account=account,
            coupon_id=coupon_id,
            snapshot=snapshot,
            idempotency_key=_provider_key(idempotency_key, "discount-presentation"),
        )
        if response is not None:
            raise StripeAdapterError("Stripe operation is unavailable")

    def create_checkout(
        self,
        resolved,
        lines,
        promotion_code_id,
        command_input,
        routes,
        idempotency_key,
    ):
        client, account = self._commerce_context(resolved, "checkout")
        params = _checkout_params(lines, promotion_code_id, command_input, routes)
        response = _provider_call(
            client.create_checkout,
            stripe_account=account,
            params=params,
            idempotency_key=_provider_key(idempotency_key, "checkout"),
        )
        return _checkout_result(response, include_session=True)

    def retrieve_checkout_handoff(self, resolved, session_id):
        client, account = self._commerce_context(resolved, "checkout")
        response = _provider_call(
            client.retrieve_checkout,
            stripe_account=account,
            session_id=session_id,
        )
        return _checkout_result(response, include_session=False)

    def retrieve_checkout_status(self, resolved, session_id):
        client, account = self._commerce_context(resolved, "checkout")
        response = _provider_call(
            client.retrieve_checkout,
            stripe_account=account,
            session_id=session_id,
        )
        payment_status = _mapping_value(response, "payment_status")
        session_status = _mapping_value(response, "status")
        if payment_status == "paid":
            return "paid"
        if session_status == "expired" or (
            session_status == "complete" and payment_status == "unpaid"
        ):
            return "terminal_unpaid"
        if session_status == "open" and payment_status in {
            "unpaid",
            "no_payment_required",
        }:
            return "pending"
        return "unknown"

    def retrieve_subscription_operation_state(self, resolved, subscription_id):
        client, account = self._commerce_context(resolved, "subscriptions")
        response = _provider_call(
            client.retrieve_subscription_operation_state,
            stripe_account=account,
            subscription_id=subscription_id,
        )
        return _subscription_operation_state(response, subscription_id)

    def preview_subscription_change(self, resolved, **kwargs):
        client, account = self._commerce_context(resolved, "subscriptions")
        response = _provider_call(
            client.preview_subscription_change,
            stripe_account=account,
            **kwargs,
        )
        if response != {"previewTimestamp": kwargs["preview_timestamp"]}:
            raise StripeAdapterError("Stripe subscription is unavailable")
        return response

    def apply_subscription_change(self, resolved, **kwargs):
        client, account = self._commerce_context(resolved, "subscriptions")
        response = _provider_call(
            client.apply_subscription_change,
            stripe_account=account,
            **kwargs,
        )
        if response is not None:
            raise StripeAdapterError("Stripe subscription is unavailable")

    def schedule_subscription_change(self, resolved, **kwargs):
        client, account = self._commerce_context(resolved, "subscriptions")
        response = _provider_call(
            client.schedule_subscription_change,
            stripe_account=account,
            **kwargs,
        )
        if response is not None:
            raise StripeAdapterError("Stripe subscription is unavailable")

    def update_subscription_discount(self, resolved, **kwargs):
        client, account = self._commerce_context(resolved, "subscriptions")
        response = _provider_call(
            client.update_subscription_discount,
            stripe_account=account,
            **kwargs,
        )
        if response is not None:
            raise StripeAdapterError("Stripe subscription is unavailable")

    def update_subscription_pause(self, resolved, **kwargs):
        client, account = self._commerce_context(resolved, "subscriptions")
        response = _provider_call(
            client.update_subscription_pause,
            stripe_account=account,
            **kwargs,
        )
        if response is not None:
            raise StripeAdapterError("Stripe subscription is unavailable")

    def create_portal_configuration(self, resolved, idempotency_key):
        client, account = self._commerce_context(resolved, "customer-portal")
        response = _provider_call(
            client.create_portal_configuration,
            stripe_account=account,
            idempotency_key=_provider_key(idempotency_key, "portal-configuration"),
        )
        return _provider_id(response, "id", "bpc_")

    def create_portal_session(self, resolved, **kwargs):
        client, account = self._commerce_context(resolved, "customer-portal")
        response = _provider_call(
            client.create_portal_session,
            stripe_account=account,
            **kwargs,
        )
        result = {
            "redirectUrl": _mapping_value(response, "redirectUrl"),
            "expiresAt": _mapping_value(response, "expiresAt"),
        }
        url = result["redirectUrl"]
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except (TypeError, ValueError):
            raise StripeAdapterError("Stripe portal is unavailable") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname != "billing.stripe.com"
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or type(result["expiresAt"]) is not int
        ):
            raise StripeAdapterError("Stripe portal is unavailable")
        return result

    def retrieve_webhook_state(
        self,
        connection: IntegrationConnection,
        event_id: object,
        event_type: object,
    ) -> dict[str, Any]:
        if (
            type(event_id) is not str
            or not 1 <= len(event_id) <= 128
            or type(event_type) is not str
            or event_type not in STRIPE_WEBHOOK_EVENT_TYPES
        ):
            raise StripeAdapterError("Stripe event is unavailable")
        client, account = self._webhook_context(connection, event_type)
        event = _provider_call(
            client.retrieve_event,
            stripe_account=account,
            event_id=event_id,
        )
        selected = _canonical_event(
            event,
            event_id=event_id,
            event_type=event_type,
            account=account,
            mode=connection.mode,
        )
        object_id = selected["objectId"]
        mapping_hint = None
        if selected["objectType"] == "account":
            canonical = {"accountHash": object_id}
        elif selected["objectType"] == "checkout-session":
            canonical = _provider_call(
                client.retrieve_checkout_canonical,
                stripe_account=account,
                session_id=object_id,
            )
            canonical = _checkout_canonical(canonical, object_id)
            mapping_hint = _mapping_hint(canonical.pop("mappingHint"))
            if canonical["paymentIntentId"] is not None:
                payment_intent = _provider_call(
                    client.retrieve_payment_intent,
                    stripe_account=account,
                    payment_intent_id=canonical["paymentIntentId"],
                )
                mapping_hint = _merge_mapping_hint(
                    mapping_hint,
                    _payment_intent_canonical(
                        payment_intent, canonical["paymentIntentId"]
                    ),
                )
            if canonical["subscriptionId"] is not None:
                subscription = _subscription_canonical(
                    _provider_call(
                        client.retrieve_subscription_canonical,
                        stripe_account=account,
                        subscription_id=canonical["subscriptionId"],
                    ),
                    canonical["subscriptionId"],
                )
                mapping_hint = _merge_mapping_hint(
                    mapping_hint, subscription.pop("mappingHint")
                )
                canonical["latestInvoiceId"] = subscription["latestInvoiceId"]
                if subscription["latestInvoiceId"] is not None:
                    _invoice_canonical(
                        _provider_call(
                            client.retrieve_invoice_canonical,
                            stripe_account=account,
                            invoice_id=subscription["latestInvoiceId"],
                        ),
                        subscription["latestInvoiceId"],
                    )
        elif selected["objectType"] == "refund":
            canonical = _refund_canonical(
                _provider_call(
                    client.retrieve_refund_canonical,
                    stripe_account=account,
                    refund_id=object_id,
                ),
                object_id,
            )
            if canonical["paymentIntentId"] is not None:
                payment_intent = _provider_call(
                    client.retrieve_payment_intent,
                    stripe_account=account,
                    payment_intent_id=canonical["paymentIntentId"],
                )
                mapping_hint = _payment_intent_canonical(
                    payment_intent, canonical["paymentIntentId"]
                )
            elif canonical["chargeId"] is not None:
                charge = _provider_call(
                    client.retrieve_charge,
                    stripe_account=account,
                    charge_id=canonical["chargeId"],
                )
                if _mapping_value(charge, "chargeId") != canonical["chargeId"]:
                    raise StripeAdapterError("Stripe event is unavailable")
        elif selected["objectType"] == "subscription":
            canonical = _subscription_canonical(
                _provider_call(
                    client.retrieve_subscription_canonical,
                    stripe_account=account,
                    subscription_id=object_id,
                ),
                object_id,
            )
            mapping_hint = canonical.pop("mappingHint")
            if canonical["latestInvoiceId"] is not None:
                _invoice_canonical(
                    _provider_call(
                        client.retrieve_invoice_canonical,
                        stripe_account=account,
                        invoice_id=canonical["latestInvoiceId"],
                    ),
                    canonical["latestInvoiceId"],
                )
        else:
            canonical = _invoice_canonical(
                _provider_call(
                    client.retrieve_invoice_canonical,
                    stripe_account=account,
                    invoice_id=object_id,
                ),
                object_id,
            )
            subscription_id = canonical["subscriptionId"]
            canonical["subscription"] = (
                _subscription_canonical(
                    _provider_call(
                        client.retrieve_subscription_canonical,
                        stripe_account=account,
                        subscription_id=subscription_id,
                    ),
                    subscription_id,
                )
                if subscription_id is not None
                else None
            )
            if canonical["subscription"] is not None:
                mapping_hint = canonical["subscription"].pop("mappingHint")
        return {
            "eventId": selected["eventId"],
            "eventType": selected["eventType"],
            "eventCreatedAt": selected["eventCreatedAt"],
            "mode": connection.mode,
            "accountHash": hashlib.sha256(account.encode("ascii")).hexdigest(),
            "objectType": selected["objectType"],
            "objectId": object_id,
            "mappingHint": mapping_hint,
            "canonical": canonical,
        }

    def _webhook_context(
        self, connection: IntegrationConnection, event_type: str
    ) -> tuple[Any, str]:
        if (
            type(connection) is not IntegrationConnection
            or connection.provider != "stripe"
            or (
                connection.status != "active"
                and not (
                    event_type == "account.application.deauthorized"
                    and connection.status == "pending"
                )
            )
        ):
            raise StripeAdapterError("Stripe event is unavailable")
        account = connection.provider_metadata.get("accountReference")
        if type(account) is not str or _ACCOUNT_REFERENCE.fullmatch(account) is None:
            raise StripeAdapterError("Stripe event is unavailable")
        return self._client_for(connection), account

    def _commerce_context(self, resolved, capability):
        try:
            from registry import ResolvedBinding
        except ModuleNotFoundError:
            from src.registry import ResolvedBinding
        if type(resolved) is not ResolvedBinding:
            raise StripeAdapterError("Stripe operation is unavailable")
        account = validate_stripe_context(
            resolved.binding, resolved.connection, capability
        )
        return self._client_for(resolved.connection), account


def validate_stripe_context(
    binding: IntegrationBinding,
    connection: IntegrationConnection,
    capability: object,
) -> str:
    if (
        type(binding) is not IntegrationBinding
        or type(connection) is not IntegrationConnection
        or binding.scope != connection.scope
        or binding.provider != "stripe"
        or connection.provider != "stripe"
        or binding.connection_id != connection.connection_id
        or binding.mode != connection.mode
        or type(capability) is not str
        or capability not in binding.capabilities
        or capability not in connection.capabilities
        or binding.provider_metadata.get("chargeType") != "direct"
        or binding.provider_metadata.get("feePayer") != "connected-account"
    ):
        raise StripeAdapterError("Stripe operation is unavailable")
    account_reference = connection.provider_metadata.get("accountReference")
    if (
        type(account_reference) is not str
        or _ACCOUNT_REFERENCE.fullmatch(account_reference) is None
    ):
        raise StripeAdapterError("Stripe operation is unavailable")
    return account_reference


def _validate_pending_onboarding_context(
    binding: IntegrationBinding,
    connection: IntegrationConnection,
    strategy: str,
) -> None:
    if (
        type(binding) is not IntegrationBinding
        or type(connection) is not IntegrationConnection
        or binding.scope != connection.scope
        or binding.connection_id != connection.connection_id
        or binding.provider != "stripe"
        or connection.provider != "stripe"
        or binding.mode != connection.mode
        or binding.status != "active"
        or connection.status != "pending"
        or "connect-onboarding" not in binding.capabilities
        or "connect-onboarding" not in connection.capabilities
        or binding.provider_metadata.get("accountStrategy") != strategy
        or connection.provider_metadata.get("accountReference") is not None
    ):
        raise StripeAdapterError("Stripe onboarding is unavailable")


class SecretsManagerStripeClientFactory:
    """Read one scoped key only after the adapter validates the connection context."""

    _KEY = re.compile(r"sk_(test|live)_[A-Za-z0-9_]{16,240}", re.ASCII)
    _CLIENT_ID = re.compile(r"ca_[A-Za-z0-9_]{8,240}", re.ASCII)

    def __init__(self, secrets_client: Any):
        if secrets_client is None:
            raise StripeAdapterError("Stripe adapter is unavailable")
        self._secrets = secrets_client

    def client_for(self, connection: IntegrationConnection) -> Any:
        if type(connection) is not IntegrationConnection:
            raise StripeAdapterError("Stripe operation is unavailable")
        try:
            response = self._secrets.get_secret_value(
                SecretId=connection.credential_reference
            )
        except Exception:
            raise StripeAdapterError("Stripe operation is unavailable") from None
        value = response.get("SecretString") if isinstance(response, dict) else None
        try:
            secret = json.loads(value) if type(value) is str else None
        except (TypeError, ValueError):
            secret = None
        if not isinstance(secret, dict) or set(secret) != {"clientId", "secretKey"}:
            raise StripeAdapterError("Stripe operation is unavailable")
        client_id = secret.get("clientId")
        secret_key = secret.get("secretKey")
        match = self._KEY.fullmatch(secret_key) if type(secret_key) is str else None
        expected = "test" if connection.mode == "test" else "live"
        if (
            match is None
            or match.group(1) != expected
            or type(client_id) is not str
            or self._CLIENT_ID.fullmatch(client_id) is None
        ):
            raise StripeAdapterError("Stripe operation is unavailable")
        try:
            import stripe  # type: ignore

            http_client = stripe._http_client.UrllibClient(_lib=_TimedUrllib)
            client = stripe.StripeClient(
                secret_key,
                client_id=client_id,
                max_network_retries=2,
                http_client=http_client,
            )
        except Exception:
            raise StripeAdapterError("Stripe operation is unavailable") from None
        return StripeSdkClient(client, client_id=client_id)


class _TimedOpener:
    def __init__(self, opener: Any):
        self._opener = opener

    def open(self, request: Any):
        return self._opener.open(request, timeout=5)


class _TimedUrllib:
    Request = urllib.request.Request
    ProxyHandler = urllib.request.ProxyHandler

    @staticmethod
    def build_opener(*handlers: Any) -> _TimedOpener:
        return _TimedOpener(urllib.request.build_opener(*handlers))

    @staticmethod
    def urlopen(request: Any):
        return urllib.request.urlopen(request, timeout=5)


class StripeSdkClient:
    """Small wrapper that keeps the pinned StripeClient request shapes in one place."""

    def __init__(self, client: Any, *, client_id: str | None = None):
        self.client = client
        self.client_id = client_id or getattr(client, "client_id", None)

    def create_oauth_handoff(self, **kwargs: Any) -> dict[str, Any]:
        if (
            type(self.client_id) is not str
            or SecretsManagerStripeClientFactory._CLIENT_ID.fullmatch(self.client_id)
            is None
        ):
            raise StripeAdapterError("Stripe onboarding is unavailable")
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "scope": "read_write",
                "redirect_uri": kwargs["redirect_uri"],
                "state": kwargs["state"],
            }
        )
        return {"url": f"https://connect.stripe.com/oauth/authorize?{query}"}

    def exchange_oauth_code(self, **kwargs: Any) -> dict[str, Any]:
        response = self.client.oauth.token(
            {"grant_type": "authorization_code", "code": kwargs["code"]}, {}
        )
        return {"accountReference": _mapping_value(response, "stripe_user_id")}

    def deauthorize_oauth_account(self, **kwargs: Any) -> None:
        response = self.client.oauth.deauthorize(
            {"stripe_user_id": kwargs["stripe_account"]}, {}
        )
        if _mapping_value(response, "stripe_user_id") != kwargs["stripe_account"]:
            raise StripeAdapterError("Stripe deauthorization is unavailable")

    def create_controller_account(self, **kwargs: Any) -> dict[str, Any]:
        response = self.client.v1.accounts.create(
            {
                "controller": {
                    "fees": {"payer": "application"},
                    "losses": {"payments": "application"},
                    "requirement_collection": "application",
                    "stripe_dashboard": {"type": "express"},
                },
                "capabilities": {
                    "card_payments": {"requested": True},
                    "transfers": {"requested": True},
                },
            },
            {"idempotency_key": kwargs["idempotency_key"]},
        )
        return {"id": _mapping_value(response, "id")}

    def create_v1_handoff(self, **kwargs: Any) -> dict[str, Any]:
        return_url = _append_state(kwargs["return_url"], kwargs["state"])
        response = self.client.v1.account_links.create(
            {
                "account": kwargs["stripe_account"],
                "refresh_url": kwargs["refresh_url"],
                "return_url": return_url,
                "type": "account_onboarding",
            },
            (
                {"idempotency_key": kwargs["idempotency_key"]}
                if "idempotency_key" in kwargs
                else {}
            ),
        )
        return {"url": _mapping_value(response, "url")}

    def create_v2_handoff(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        raise StripeAdapterError("Stripe onboarding is unavailable")

    def retrieve_v1_account(self, **kwargs: Any) -> dict[str, Any]:
        response = self.client.v1.accounts.retrieve(kwargs["stripe_account"], {}, {})
        return {
            key: _mapping_value(response, key)
            for key in (
                "charges_enabled",
                "payouts_enabled",
                "details_submitted",
                "requirements",
                "capabilities",
            )
        }

    def retrieve_v2_account(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        raise StripeAdapterError("Stripe status is unavailable")

    def create_product(self, **kwargs: Any) -> dict[str, Any]:
        response = self.client.v1.products.create(
            {
                "name": "Item",
                "metadata": {"resource_id": kwargs["resource_id"]},
            },
            _request_options(kwargs),
        )
        return {"id": _mapping_value(response, "id")}

    def create_price(self, **kwargs: Any) -> dict[str, Any]:
        snapshot = kwargs["snapshot"]
        params = {
            "product": kwargs["product_id"],
            "currency": snapshot["currency"].lower(),
            "unit_amount": snapshot["amountMinor"],
            "billing_scheme": snapshot["billingScheme"],
            "tax_behavior": snapshot["taxBehavior"],
            "metadata": {"resource_id": kwargs["resource_id"]},
        }
        if snapshot["saleType"] == "recurring":
            recurrence = snapshot["recurrence"]
            params["recurring"] = {
                "interval": recurrence["interval"],
                "interval_count": recurrence["intervalCount"],
                "usage_type": recurrence["usageType"],
            }
        response = self.client.v1.prices.create(params, _request_options(kwargs))
        return {"id": _mapping_value(response, "id")}

    def deactivate_offer(self, **kwargs: Any) -> None:
        self.client.v1.prices.update(
            kwargs["price_id"],
            {"active": False},
            _request_options(kwargs, suffix="price"),
        )
        self.client.v1.products.update(
            kwargs["product_id"],
            {"active": False},
            _request_options(kwargs, suffix="product"),
        )

    def update_product_presentation(self, **kwargs: Any) -> None:
        snapshot = kwargs["snapshot"]
        params = {"name": snapshot["displayName"]}
        if "displayDescription" in snapshot:
            params["description"] = snapshot["displayDescription"]
        self.client.v1.products.update(
            kwargs["product_id"], params, _request_options(kwargs)
        )

    def create_discount(self, **kwargs: Any) -> dict[str, Any]:
        snapshot = kwargs["snapshot"]
        coupon_params = {"duration": snapshot["duration"]}
        value = snapshot["value"]
        if value["type"] == "percentage":
            coupon_params["percent_off"] = value["basisPoints"] / 100
        else:
            coupon_params.update(
                {
                    "amount_off": value["amountMinor"],
                    "currency": value["currency"].lower(),
                }
            )
        optional = {
            "duration_in_months": snapshot["durationInMonths"],
            "max_redemptions": snapshot["redemptionLimit"],
            "redeem_by": snapshot["redeemByEpoch"],
        }
        coupon_params.update(
            {key: value for key, value in optional.items() if value is not None}
        )
        if kwargs["product_ids"]:
            coupon_params["applies_to"] = {"products": kwargs["product_ids"]}
        coupon = self.client.v1.coupons.create(
            coupon_params,
            _request_options(kwargs, suffix="coupon"),
        )
        coupon_id = _mapping_value(coupon, "id")
        promotion_params = {"promotion": {"type": "coupon", "coupon": coupon_id}}
        if snapshot["customerFacingCode"] is not None:
            promotion_params["code"] = snapshot["customerFacingCode"]
        promotion = self.client.v1.promotion_codes.create(
            promotion_params,
            _request_options(kwargs, suffix="promotion"),
        )
        return {
            "couponId": coupon_id,
            "promotionCodeId": _mapping_value(promotion, "id"),
        }

    def deactivate_discount(self, **kwargs: Any) -> None:
        active = kwargs["target"] == "active"
        self.client.v1.promotion_codes.update(
            kwargs["promotion_code_id"],
            {"active": active},
            _request_options(kwargs, suffix="promotion"),
        )
        if kwargs["target"] == "retired":
            self.client.v1.coupons.delete(
                kwargs["coupon_id"],
                {},
                _request_options(kwargs, suffix="coupon"),
            )

    def update_discount_presentation(self, **kwargs: Any) -> None:
        self.client.v1.coupons.update(
            kwargs["coupon_id"],
            {"name": kwargs["snapshot"]["displayName"]},
            _request_options(kwargs),
        )

    def create_checkout(self, **kwargs: Any) -> dict[str, Any]:
        response = self.client.v1.checkout.sessions.create(
            kwargs["params"], _request_options(kwargs)
        )
        return {
            "sessionId": _mapping_value(response, "id"),
            "redirectUrl": _mapping_value(response, "url"),
            "expiresAt": _mapping_value(response, "expires_at"),
        }

    def retrieve_checkout(self, **kwargs: Any) -> dict[str, Any]:
        response = self.client.v1.checkout.sessions.retrieve(
            kwargs["session_id"], {}, {"stripe_account": kwargs["stripe_account"]}
        )
        return {
            "sessionId": _mapping_value(response, "id"),
            "redirectUrl": _mapping_value(response, "url"),
            "expiresAt": _mapping_value(response, "expires_at"),
            "payment_status": _mapping_value(response, "payment_status"),
            "status": _mapping_value(response, "status"),
        }

    def retrieve_subscription_operation_state(self, **kwargs: Any) -> dict[str, Any]:
        response = self.client.v1.subscriptions.retrieve(
            kwargs["subscription_id"],
            {},
            {"stripe_account": kwargs["stripe_account"]},
        )
        items_value = _mapping_value(_mapping_value(response, "items"), "data")
        discounts_value = _mapping_value(response, "discounts")
        if not isinstance(items_value, list) or not isinstance(discounts_value, list):
            raise StripeAdapterError("Stripe subscription is unavailable")
        items = [
            {
                "itemId": _mapping_value(item, "id"),
                "priceId": _reference_value(_mapping_value(item, "price")),
                "quantity": _mapping_value(item, "quantity"),
            }
            for item in items_value
        ]
        discounts = [
            _reference_value(_mapping_value(item, "promotion_code"))
            for item in discounts_value
        ]
        pause = _mapping_value(response, "pause_collection")
        pause_collection = None
        if pause is not None:
            pause_collection = {"behavior": _mapping_value(pause, "behavior")}
            resumes_at = _mapping_value(pause, "resumes_at")
            if resumes_at is not None:
                pause_collection["resumesAt"] = resumes_at
        return {
            "subscriptionId": _mapping_value(response, "id"),
            "customerId": _reference_value(_mapping_value(response, "customer")),
            "status": _mapping_value(response, "status"),
            "items": items,
            "scheduleId": _reference_value(_mapping_value(response, "schedule")),
            "discounts": discounts,
            "pauseCollection": pause_collection,
        }

    def preview_subscription_change(self, **kwargs: Any) -> dict[str, Any]:
        self.client.v1.invoices.create_preview(
            {
                "subscription": kwargs["subscription_id"],
                "subscription_details": {
                    "items": [
                        {
                            "id": kwargs["item_id"],
                            "price": kwargs["price_id"],
                            "quantity": kwargs["quantity"],
                        }
                    ],
                    "proration_date": kwargs["preview_timestamp"],
                },
            },
            {"stripe_account": kwargs["stripe_account"]},
        )
        return {"previewTimestamp": kwargs["preview_timestamp"]}

    def apply_subscription_change(self, **kwargs: Any) -> None:
        self.client.v1.subscriptions.update(
            kwargs["subscription_id"],
            {
                "items": [
                    {
                        "id": kwargs["item_id"],
                        "price": kwargs["price_id"],
                        "quantity": kwargs["quantity"],
                    }
                ],
                "proration_behavior": "create_prorations",
                "proration_date": kwargs["preview_timestamp"],
            },
            _request_options(kwargs),
        )

    def schedule_subscription_change(self, **kwargs: Any) -> None:
        options = _request_options(kwargs, suffix="create")
        schedule = self.client.v1.subscription_schedules.create(
            {"from_subscription": kwargs["subscription_id"]}, options
        )
        schedule_id = _mapping_value(schedule, "id")
        current = _mapping_value(schedule, "current_phase")
        start = _mapping_value(current, "start_date")
        end = _mapping_value(current, "end_date")
        if (
            type(schedule_id) is not str
            or type(start) is not int
            or type(end) is not int
        ):
            raise StripeAdapterError("Stripe subscription is unavailable")
        self.client.v1.subscription_schedules.update(
            schedule_id,
            {
                "end_behavior": "release",
                "phases": [
                    {
                        "start_date": start,
                        "end_date": end,
                        "items": [
                            {
                                "price": kwargs["current_price_id"],
                                "quantity": kwargs["quantity"],
                            }
                        ],
                    },
                    {
                        "items": [
                            {
                                "price": kwargs["price_id"],
                                "quantity": kwargs["quantity"],
                            }
                        ]
                    },
                ],
            },
            _request_options(kwargs, suffix="update"),
        )

    def update_subscription_discount(self, **kwargs: Any) -> None:
        promotion_code_id = kwargs["promotion_code_id"]
        discounts = (
            [] if promotion_code_id is None else [{"promotion_code": promotion_code_id}]
        )
        self.client.v1.subscriptions.update(
            kwargs["subscription_id"],
            {"discounts": discounts},
            _request_options(kwargs),
        )

    def update_subscription_pause(self, **kwargs: Any) -> None:
        self.client.v1.subscriptions.update(
            kwargs["subscription_id"],
            {
                "pause_collection": (
                    kwargs["pause_collection"]
                    if kwargs["pause_collection"] is not None
                    else ""
                )
            },
            _request_options(kwargs),
        )

    def create_portal_configuration(self, **kwargs: Any) -> dict[str, Any]:
        response = self.client.v1.billing_portal.configurations.create(
            {
                "features": {
                    "invoice_history": {"enabled": True},
                    "payment_method_update": {"enabled": True},
                }
            },
            _request_options(kwargs),
        )
        return {"id": _mapping_value(response, "id")}

    def create_portal_session(self, **kwargs: Any) -> dict[str, Any]:
        response = self.client.v1.billing_portal.sessions.create(
            {
                "customer": kwargs["customer_id"],
                "configuration": kwargs["configuration_id"],
            },
            _request_options(kwargs),
        )
        created = _mapping_value(response, "created")
        if type(created) is not int:
            raise StripeAdapterError("Stripe portal is unavailable")
        return {
            "redirectUrl": _mapping_value(response, "url"),
            "expiresAt": created + 30 * 60,
        }

    def retrieve_event(self, **kwargs: Any) -> dict[str, Any]:
        response = self.client.v1.events.retrieve(
            kwargs["event_id"], {}, {"stripe_account": kwargs["stripe_account"]}
        )
        event_type = _mapping_value(response, "type")
        data = _mapping_value(response, "data")
        provider_object = _mapping_value(data, "object")
        object_type = (
            "account"
            if event_type == "account.application.deauthorized"
            else (
                "checkout-session"
                if isinstance(event_type, str)
                and event_type.startswith("checkout.session.")
                else (
                    "refund"
                    if isinstance(event_type, str) and event_type.startswith("refund.")
                    else (
                        "subscription"
                        if isinstance(event_type, str)
                        and event_type.startswith("customer.subscription.")
                        else (
                            "invoice"
                            if isinstance(event_type, str)
                            and event_type.startswith("invoice.")
                            else None
                        )
                    )
                )
            )
        )
        return {
            "id": _mapping_value(response, "id"),
            "type": event_type,
            "created": _mapping_value(response, "created"),
            "livemode": _mapping_value(response, "livemode"),
            "account": _mapping_value(response, "account"),
            "objectType": object_type,
            "objectId": _mapping_value(provider_object, "id"),
        }

    def retrieve_checkout_canonical(self, **kwargs: Any) -> dict[str, Any]:
        response = self.client.v1.checkout.sessions.retrieve(
            kwargs["session_id"], {}, {"stripe_account": kwargs["stripe_account"]}
        )
        return {
            "sessionId": _mapping_value(response, "id"),
            "status": _mapping_value(response, "status"),
            "paymentStatus": _mapping_value(response, "payment_status"),
            "mode": _mapping_value(response, "mode"),
            "paymentIntentId": _reference_value(
                _mapping_value(response, "payment_intent")
            ),
            "subscriptionId": _reference_value(
                _mapping_value(response, "subscription")
            ),
            "latestInvoiceId": None,
            "mappingHint": _metadata_mapping_hint(response),
        }

    def retrieve_payment_intent(self, **kwargs: Any) -> dict[str, Any]:
        response = self.client.v1.payment_intents.retrieve(
            kwargs["payment_intent_id"],
            {},
            {"stripe_account": kwargs["stripe_account"]},
        )
        return {
            "paymentIntentId": _mapping_value(response, "id"),
            "mappingHint": _metadata_mapping_hint(response),
        }

    def retrieve_refund_canonical(self, **kwargs: Any) -> dict[str, Any]:
        response = self.client.v1.refunds.retrieve(
            kwargs["refund_id"], {}, {"stripe_account": kwargs["stripe_account"]}
        )
        currency = _mapping_value(response, "currency")
        payment_intent_id = _reference_value(_mapping_value(response, "payment_intent"))
        return {
            "refundId": _mapping_value(response, "id"),
            "status": _mapping_value(response, "status"),
            "amountMinor": _mapping_value(response, "amount"),
            "currency": currency.upper() if isinstance(currency, str) else currency,
            "paymentIntentId": payment_intent_id,
            "chargeId": (
                None
                if payment_intent_id is not None
                else _reference_value(_mapping_value(response, "charge"))
            ),
        }

    def retrieve_charge(self, **kwargs: Any) -> dict[str, Any]:
        response = self.client.v1.charges.retrieve(
            kwargs["charge_id"], {}, {"stripe_account": kwargs["stripe_account"]}
        )
        return {"chargeId": _mapping_value(response, "id")}

    def retrieve_subscription_canonical(self, **kwargs: Any) -> dict[str, Any]:
        response = self.client.v1.subscriptions.retrieve(
            kwargs["subscription_id"],
            {},
            {"stripe_account": kwargs["stripe_account"]},
        )
        items = _mapping_value(_mapping_value(response, "items"), "data")
        if not isinstance(items, list) or len(items) != 1:
            raise StripeAdapterError("Stripe event is unavailable")
        item = items[0]
        price_id = _reference_value(_mapping_value(item, "price"))
        period_end = _mapping_value(response, "current_period_end")
        if type(period_end) is not int:
            period_end = _mapping_value(item, "current_period_end")
        pause = _mapping_value(response, "pause_collection")
        sanitized_pause = None
        if pause is not None:
            behavior = _mapping_value(pause, "behavior")
            resumes_at = _mapping_value(pause, "resumes_at")
            sanitized_pause = {"behavior": behavior}
            if resumes_at is not None:
                sanitized_pause["resumesAt"] = resumes_at
        return {
            "subscriptionId": _mapping_value(response, "id"),
            "status": _mapping_value(response, "status"),
            "currentPeriodEnd": period_end,
            "latestInvoiceId": _reference_value(
                _mapping_value(response, "latest_invoice")
            ),
            "priceId": price_id,
            "pauseCollection": sanitized_pause,
            "mappingHint": _metadata_mapping_hint(response),
        }

    def retrieve_invoice_canonical(self, **kwargs: Any) -> dict[str, Any]:
        response = self.client.v1.invoices.retrieve(
            kwargs["invoice_id"], {}, {"stripe_account": kwargs["stripe_account"]}
        )
        return {
            "invoiceId": _mapping_value(response, "id"),
            "status": _mapping_value(response, "status"),
            "paid": _mapping_value(response, "paid"),
            "subscriptionId": _reference_value(
                _mapping_value(response, "subscription")
            ),
        }


def _provider_call(operation, **kwargs):
    try:
        return operation(**kwargs)
    except StripeAdapterError:
        raise
    except Exception:
        raise StripeAdapterError("Stripe operation is unavailable") from None


def _canonical_event(value, *, event_id, event_type, account, mode):
    keys = {"id", "type", "created", "livemode", "account", "objectType", "objectId"}
    expected_object_type = (
        "account"
        if event_type == "account.application.deauthorized"
        else (
            "checkout-session"
            if event_type.startswith("checkout.session.")
            else (
                "refund"
                if event_type.startswith("refund.")
                else (
                    "subscription"
                    if event_type.startswith("customer.subscription.")
                    else "invoice"
                )
            )
        )
    )
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or value.get("id") != event_id
        or value.get("type") != event_type
        or value.get("account") != account
        or value.get("livemode") is not (mode == "live")
        or value.get("objectType") != expected_object_type
        or type(value.get("created")) is not int
        or not 0 <= value["created"] <= 9_999_999_999
    ):
        raise StripeAdapterError("Stripe event is unavailable")
    if expected_object_type == "account":
        object_id = hashlib.sha256(account.encode("ascii")).hexdigest()
    else:
        _reference(value.get("objectId"))
        object_id = value["objectId"]
    return {
        "eventId": event_id,
        "eventType": event_type,
        "eventCreatedAt": value["created"],
        "objectType": expected_object_type,
        "objectId": object_id,
    }


def _checkout_canonical(value, expected_id):
    keys = {
        "sessionId",
        "status",
        "paymentStatus",
        "mode",
        "paymentIntentId",
        "subscriptionId",
        "latestInvoiceId",
        "mappingHint",
    }
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or value.get("sessionId") != expected_id
        or value.get("status") not in {"open", "complete", "expired"}
        or value.get("paymentStatus")
        not in {"paid", "unpaid", "no_payment_required", "failed"}
        or value.get("mode") not in {"payment", "subscription"}
    ):
        raise StripeAdapterError("Stripe event is unavailable")
    for key in ("paymentIntentId", "subscriptionId", "latestInvoiceId"):
        if value[key] is not None:
            _reference(value[key])
    if value["mode"] == "payment" and value["subscriptionId"] is not None:
        raise StripeAdapterError("Stripe event is unavailable")
    return dict(value)


def _payment_intent_canonical(value, expected_id):
    if (
        not isinstance(value, dict)
        or set(value) != {"paymentIntentId", "mappingHint"}
        or value.get("paymentIntentId") != expected_id
    ):
        raise StripeAdapterError("Stripe event is unavailable")
    return _mapping_hint(value.get("mappingHint"))


def _refund_canonical(value, expected_id):
    keys = {
        "refundId",
        "status",
        "amountMinor",
        "currency",
        "paymentIntentId",
        "chargeId",
    }
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or value.get("refundId") != expected_id
        or value.get("status") not in {"pending", "succeeded", "failed", "canceled"}
        or type(value.get("amountMinor")) is not int
        or value["amountMinor"] <= 0
        or type(value.get("currency")) is not str
        or re.fullmatch(r"[A-Z]{3}", value["currency"], re.ASCII) is None
        or (value.get("paymentIntentId") is None) == (value.get("chargeId") is None)
    ):
        raise StripeAdapterError("Stripe event is unavailable")
    _reference(value.get("paymentIntentId") or value.get("chargeId"))
    return dict(value)


def _subscription_canonical(value, expected_id):
    keys = {
        "subscriptionId",
        "status",
        "currentPeriodEnd",
        "latestInvoiceId",
        "priceId",
        "pauseCollection",
        "mappingHint",
    }
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or value.get("subscriptionId") != expected_id
        or value.get("status")
        not in {
            "incomplete",
            "incomplete_expired",
            "trialing",
            "active",
            "past_due",
            "canceled",
            "unpaid",
            "paused",
        }
        or type(value.get("currentPeriodEnd")) is not int
        or not 0 <= value["currentPeriodEnd"] <= 9_999_999_999
    ):
        raise StripeAdapterError("Stripe event is unavailable")
    _reference(value.get("priceId"))
    if value["latestInvoiceId"] is not None:
        _reference(value["latestInvoiceId"])
    pause = value["pauseCollection"]
    if pause is not None and (
        not isinstance(pause, dict)
        or set(pause) not in ({"behavior"}, {"behavior", "resumesAt"})
        or pause.get("behavior") not in {"void", "keep_as_draft", "mark_uncollectible"}
        or (
            "resumesAt" in pause
            and (
                type(pause["resumesAt"]) is not int
                or not 0 <= pause["resumesAt"] <= 9_999_999_999
            )
        )
    ):
        raise StripeAdapterError("Stripe event is unavailable")
    return {
        **value,
        "mappingHint": _mapping_hint(value["mappingHint"]),
        "pauseCollection": None if pause is None else dict(pause),
    }


def _invoice_canonical(value, expected_id):
    keys = {"invoiceId", "status", "paid", "subscriptionId"}
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or value.get("invoiceId") != expected_id
        or value.get("status") not in {"draft", "open", "paid", "uncollectible", "void"}
        or type(value.get("paid")) is not bool
    ):
        raise StripeAdapterError("Stripe event is unavailable")
    if value["subscriptionId"] is not None:
        _reference(value["subscriptionId"])
    return dict(value)


def _subscription_operation_state(value, expected_id):
    keys = {
        "subscriptionId",
        "customerId",
        "status",
        "items",
        "scheduleId",
        "discounts",
        "pauseCollection",
    }
    if (
        not isinstance(value, dict)
        or set(value) != keys
        or value.get("subscriptionId") != expected_id
        or type(value.get("customerId")) is not str
        or value.get("status")
        not in {"active", "trialing", "past_due", "unpaid", "incomplete"}
        or not isinstance(value.get("items"), list)
        or not 1 <= len(value["items"]) <= 20
        or any(
            not isinstance(item, dict)
            or set(item) != {"itemId", "priceId", "quantity"}
            or type(item["itemId"]) is not str
            or type(item["priceId"]) is not str
            or type(item["quantity"]) is not int
            or item["quantity"] < 1
            for item in value["items"]
        )
        or (
            value.get("scheduleId") is not None and type(value["scheduleId"]) is not str
        )
        or not isinstance(value.get("discounts"), list)
        or len(value["discounts"]) > 20
        or any(type(item) is not str for item in value["discounts"])
        or (
            value.get("pauseCollection") is not None
            and not isinstance(value["pauseCollection"], dict)
        )
    ):
        raise StripeAdapterError("Stripe subscription is unavailable")
    return value


def _reference(value):
    if (
        type(value) is not str
        or not 1 <= len(value) <= 255
        or re.fullmatch(r"[A-Za-z0-9_:-]+", value, re.ASCII) is None
    ):
        raise StripeAdapterError("Stripe event is unavailable")
    return value


def _mapping_hint(value):
    if value is None:
        return None
    if type(value) is not str or _MAPPING_HINT.fullmatch(value) is None:
        raise StripeAdapterError("Stripe event is unavailable")
    return value


def _merge_mapping_hint(current, candidate):
    current = _mapping_hint(current)
    candidate = _mapping_hint(candidate)
    if current is not None and candidate is not None and current != candidate:
        raise StripeAdapterError("Stripe event is unavailable")
    return current if current is not None else candidate


def _metadata_mapping_hint(value):
    metadata = _mapping_value(value, "metadata")
    if metadata is None:
        return None
    hint = _mapping_value(metadata, "payment_attempt_id")
    return _mapping_hint(hint)


def _provider_key(value: object, operation: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= 256:
        raise StripeAdapterError("Stripe operation is unavailable")
    digest = hashlib.sha256((value + "\0" + operation).encode("utf-8")).hexdigest()
    return f"integrations-stripe-v1:{digest}"


def _provider_id(value: object, key: str, prefix: str | None) -> str:
    identifier = _mapping_value(value, key)
    if (
        type(identifier) is not str
        or not 1 <= len(identifier) <= 255
        or (prefix is not None and not identifier.startswith(prefix))
        or re.fullmatch(r"[A-Za-z0-9_:-]+", identifier, re.ASCII) is None
    ):
        raise StripeAdapterError("Stripe operation is unavailable")
    return identifier


def _request_options(kwargs: dict[str, Any], *, suffix: str | None = None):
    key = kwargs["idempotency_key"]
    if suffix is not None:
        key = _provider_key(key, suffix)
    return {
        "stripe_account": kwargs["stripe_account"],
        "idempotency_key": key,
    }


def _checkout_params(lines, promotion_code_id, command_input, routes):
    if (
        not isinstance(lines, list)
        or not isinstance(command_input, dict)
        or not isinstance(routes, dict)
        or set(routes) != {"successUrl", "cancelUrl"}
    ):
        raise StripeAdapterError("Stripe checkout is unavailable")
    recurring_count = sum(
        line["snapshot"]["saleType"] == "recurring"
        for line in command_input["offerBindings"]
    )
    if len(lines) != len(command_input["offerBindings"]) or recurring_count > 1:
        raise StripeAdapterError("Stripe checkout is unavailable")
    metadata = {
        "order_id": command_input["orderId"],
        "payment_attempt_id": command_input["paymentAttemptId"],
        "revision": str(command_input["revision"]),
    }
    mode = "subscription" if recurring_count == 1 else "payment"
    params = {
        "mode": mode,
        "line_items": lines,
        "payment_method_types": ["card", "link"],
        "success_url": routes["successUrl"],
        "cancel_url": routes["cancelUrl"],
        "expires_at": command_input["checkoutExpiresAt"],
        "client_reference_id": command_input["orderId"],
        "metadata": metadata,
        "automatic_tax": {"enabled": command_input["taxPolicy"]["mode"] == "automatic"},
    }
    params["subscription_data" if mode == "subscription" else "payment_intent_data"] = {
        "metadata": metadata
    }
    shipping = command_input["shippingPolicy"]
    if shipping["collection"] == "required":
        params["shipping_address_collection"] = {
            "allowed_countries": shipping["allowedCountries"]
        }
    if promotion_code_id is not None:
        params["discounts"] = [{"promotion_code": promotion_code_id}]
    return params


def _checkout_result(value, *, include_session):
    required = ("redirectUrl", "expiresAt")
    result = {key: _mapping_value(value, key) for key in required}
    if include_session:
        result["sessionId"] = _mapping_value(value, "sessionId")
        _provider_id(result, "sessionId", "cs_")
    if type(result["redirectUrl"]) is not str or type(result["expiresAt"]) is not int:
        raise StripeAdapterError("Stripe checkout is unavailable")
    return result


def _append_state(url: str, state: str) -> str:
    parsed = urlsplit(url)
    if parsed.query or parsed.fragment:
        raise StripeAdapterError("Stripe onboarding is unavailable")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode({"state": state}), "")
    )


def _mapping_value(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    getter = getattr(value, "get", None)
    if callable(getter):
        return getter(key)
    return None


def _reference_value(value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    return _mapping_value(value, "id")


def _validated_callbacks(callbacks: OnboardingCallbacks, expected_domain: str) -> None:
    if type(callbacks) is not OnboardingCallbacks:
        raise StripeAdapterError("Stripe onboarding is unavailable")
    if not all(
        _same_origin_callback(url, expected_domain)
        for url in (callbacks.refresh_url, callbacks.return_url)
    ):
        raise StripeAdapterError("Stripe onboarding is unavailable")


def _same_origin_callback(url: object, expected_domain: str) -> bool:
    if type(url) is not str or len(url) > 2048:
        return False
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == expected_domain
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path.startswith("/")
        and not parsed.path.startswith("//")
        and not parsed.query
        and not parsed.fragment
    )


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
