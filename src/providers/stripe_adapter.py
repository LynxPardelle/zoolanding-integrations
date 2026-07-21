"""Stripe Connect adapter boundary with a verified Accounts v2 gate."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import re
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

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
    def __init__(
        self,
        client: StripeClient | None = None,
        *,
        accounts_v2_verified: bool,
        client_factory: Any = None,
    ):
        if (
            type(accounts_v2_verified) is not bool
            or (client is None) == (client_factory is None)
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

    def create_onboarding_handoff(
        self,
        binding: IntegrationBinding,
        connection: IntegrationConnection,
        *,
        callbacks: OnboardingCallbacks,
        state: object,
    ) -> str:
        account_reference = validate_stripe_context(
            binding, connection, "connect-onboarding"
        )
        _validated_callbacks(callbacks, connection.scope.domain)
        if (
            type(state) is not str
            or not 1 <= len(state) <= 1024
            or any(ord(character) < 33 for character in state)
        ):
            raise StripeAdapterError("Stripe onboarding is unavailable")
        client = self._client_for(connection)
        operation = (
            client.create_v2_handoff
            if self._accounts_v2_verified
            else client.create_v1_handoff
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
            or capabilities.get("card_payments") not in {
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

    def deactivate_offer(
        self, resolved, product_id, price_id, idempotency_key
    ) -> None:
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
        params = _checkout_params(
            lines, promotion_code_id, command_input, routes
        )
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
        if session_status == "open" and payment_status in {"unpaid", "no_payment_required"}:
            return "pending"
        return "unknown"

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


class SecretsManagerStripeClientFactory:
    """Read one scoped key only after the adapter validates the connection context."""

    _KEY = re.compile(r"sk_(test|live)_[A-Za-z0-9_]{16,240}", re.ASCII)

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
        match = self._KEY.fullmatch(value) if type(value) is str else None
        expected = "test" if connection.mode == "test" else "live"
        if match is None or match.group(1) != expected:
            raise StripeAdapterError("Stripe operation is unavailable")
        try:
            import stripe  # type: ignore

            client = stripe.StripeClient(value)
        except Exception:
            raise StripeAdapterError("Stripe operation is unavailable") from None
        return StripeSdkClient(client)


class StripeSdkClient:
    """Small wrapper that keeps the pinned StripeClient request shapes in one place."""

    def __init__(self, client: Any):
        self.client = client

    def create_v1_handoff(self, **kwargs: Any) -> dict[str, Any]:
        return_url = _append_state(kwargs["return_url"], kwargs["state"])
        response = self.client.v1.account_links.create(
            {
                "account": kwargs["stripe_account"],
                "refresh_url": kwargs["refresh_url"],
                "return_url": return_url,
                "type": "account_onboarding",
            },
            {},
        )
        return {"url": _mapping_value(response, "url")}

    def create_v2_handoff(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        raise StripeAdapterError("Stripe onboarding is unavailable")

    def retrieve_v1_account(self, **kwargs: Any) -> dict[str, Any]:
        response = self.client.v1.accounts.retrieve(
            kwargs["stripe_account"], {}, {}
        )
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
        coupon_params.update({key: value for key, value in optional.items() if value is not None})
        if kwargs["product_ids"]:
            coupon_params["applies_to"] = {"products": kwargs["product_ids"]}
        coupon = self.client.v1.coupons.create(
            coupon_params,
            _request_options(kwargs, suffix="coupon"),
        )
        coupon_id = _mapping_value(coupon, "id")
        promotion_params = {
            "promotion": {"type": "coupon", "coupon": coupon_id}
        }
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


def _provider_call(operation, **kwargs):
    try:
        return operation(**kwargs)
    except StripeAdapterError:
        raise
    except Exception:
        raise StripeAdapterError("Stripe operation is unavailable") from None


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
    recurring = {
        line["snapshot"]["saleType"] == "recurring"
        for line in command_input["offerBindings"]
    }
    if len(recurring) != 1:
        raise StripeAdapterError("Stripe checkout is unavailable")
    metadata = {
        "order_id": command_input["orderId"],
        "payment_attempt_id": command_input["paymentAttemptId"],
        "revision": str(command_input["revision"]),
    }
    mode = "subscription" if recurring == {True} else "payment"
    params = {
        "mode": mode,
        "line_items": lines,
        "payment_method_types": ["card", "link"],
        "success_url": routes["successUrl"],
        "cancel_url": routes["cancelUrl"],
        "expires_at": command_input["checkoutExpiresAt"],
        "client_reference_id": command_input["orderId"],
        "metadata": metadata,
        "automatic_tax": {
            "enabled": command_input["taxPolicy"]["mode"] == "automatic"
        },
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
