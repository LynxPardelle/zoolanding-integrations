"""Raw signed public Stripe Connect webhook ingress."""

from __future__ import annotations

import base64
import hashlib
import re
from typing import Any, Mapping

try:
    from common.http import _response
    from domain.integrations import technical_expiry
    from domain.operations import STRIPE_WEBHOOK_EVENT_TYPES
except ModuleNotFoundError:
    from src.common.http import _response
    from src.domain.integrations import technical_expiry
    from src.domain.operations import STRIPE_WEBHOOK_EVENT_TYPES


PATH = "/webhooks/stripe/connect"
MAX_WEBHOOK_BYTES = 1024 * 1024
_EVENT_ID = re.compile(r"evt_[A-Za-z0-9_-]{1,123}|[a-z0-9][a-z0-9._-]{0,127}", re.ASCII)
_ACCOUNT = re.compile(r"acct_[A-Za-z0-9]{8,64}", re.ASCII)
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}", re.ASCII)


def handle_request(
    event: Any,
    *,
    verifier: Any,
    registry: Any,
    store: Any,
    environment: str,
    now_epoch: int,
    metric_sink: Any,
) -> dict[str, Any]:
    request_id = _request_id(event)
    try:
        if (
            not isinstance(event, dict)
            or _method(event) != "POST"
            or _path(event) != PATH
        ):
            return _response(
                404,
                {"error": {"code": "not_found", "message": "Resource not found."}},
                request_id,
            )
        raw = _raw_body(event)
        signature = _header(event, "stripe-signature")
        if not signature:
            _safe_metric(metric_sink, "WebhookSignatureFailures", 1)
            raise _IngressError(400)
        try:
            signed = verifier.verify(raw, signature)
        except Exception:
            _safe_metric(metric_sink, "WebhookSignatureFailures", 1)
            raise _IngressError(400) from None
        selected = _signed_metadata(signed)
        _safe_metric(
            metric_sink,
            "WebhookAgeSeconds",
            max(0, now_epoch - selected["created"]),
        )
        if selected["event_type"] not in STRIPE_WEBHOOK_EVENT_TYPES:
            return _response(200, {"data": {"status": "ignored"}}, request_id)
        if environment not in {"test", "production"}:
            raise RuntimeError
        expected_mode = "test" if environment == "test" else "live"
        if selected["mode"] != expected_mode:
            _safe_metric(metric_sink, "TestLiveMismatch", 1)
            raise _IngressError(409)
        connection = registry.stripe_webhook_connection(
            environment=environment,
            mode=selected["mode"],
            account_reference=selected["account"],
            event_type=selected["event_type"],
        )
        store.accept_supported(
            scope=connection.scope,
            connection_id=connection.connection_id,
            event_id=selected["event_id"],
            event_type=selected["event_type"],
            account_hash=hashlib.sha256(
                selected["account"].encode("ascii")
            ).hexdigest(),
            mode=selected["mode"],
            payload_hash=hashlib.sha256(raw).hexdigest(),
            event_created_at=selected["created"],
            received_at=now_epoch,
            expires_at=technical_expiry(now_epoch),
        )
        return _response(200, {"data": {"status": "accepted"}}, request_id)
    except _IngressError as error:
        return _error(error.status, request_id)
    except Exception as error:
        if type(error).__name__ in {"WebhookReplayConflict"}:
            return _error(409, request_id)
        if type(error).__name__ in {"RegistryAccessDenied"}:
            return _error(404, request_id)
        if isinstance(error, (TypeError, ValueError)):
            return _error(400, request_id)
        return _error(503, request_id)


def lambda_handler(event: Any, context: Any) -> dict[str, Any]:
    del context
    try:
        dependencies = _runtime_dependencies()
    except Exception:
        return _error(503, _request_id(event))
    return handle_request(event, **dependencies)


def _runtime_dependencies() -> dict[str, Any]:
    try:
        from runtime import stripe_webhook_runtime
    except ModuleNotFoundError:
        from src.runtime import stripe_webhook_runtime
    return stripe_webhook_runtime()


class _IngressError(RuntimeError):
    def __init__(self, status: int):
        self.status = status


def _signed_metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise _IngressError(400)
    event_id = value.get("id")
    event_type = value.get("type")
    account = value.get("account")
    livemode = value.get("livemode")
    created = value.get("created")
    if (
        type(event_id) is not str
        or _EVENT_ID.fullmatch(event_id) is None
        or type(event_type) is not str
        or not 1 <= len(event_type) <= 128
        or type(account) is not str
        or _ACCOUNT.fullmatch(account) is None
        or type(livemode) is not bool
        or type(created) is not int
        or not 0 <= created <= 9_999_999_999
    ):
        raise _IngressError(400)
    return {
        "event_id": event_id,
        "event_type": event_type,
        "account": account,
        "mode": "live" if livemode else "test",
        "created": created,
    }


def _raw_body(event: dict[str, Any]) -> bytes:
    body = event.get("body")
    encoded = event.get("isBase64Encoded", False)
    if type(body) is not str or type(encoded) is not bool:
        raise _IngressError(400)
    try:
        raw = (
            base64.b64decode(body.encode("ascii"), validate=True)
            if encoded
            else body.encode("utf-8")
        )
    except (UnicodeEncodeError, ValueError):
        raise _IngressError(400) from None
    if not raw or len(raw) > MAX_WEBHOOK_BYTES:
        raise _IngressError(400)
    return raw


def _header(event: dict[str, Any], name: str) -> str:
    headers = event.get("headers")
    if not isinstance(headers, Mapping):
        return ""
    matches = [
        value.strip()
        for key, value in headers.items()
        if type(key) is str
        and key.casefold() == name
        and type(value) is str
        and value.strip()
    ]
    return matches[0] if len(matches) == 1 else ""


def _method(event: dict[str, Any]) -> str:
    context = event.get("requestContext")
    http = context.get("http") if isinstance(context, Mapping) else None
    if isinstance(http, Mapping) and type(http.get("method")) is str:
        return http["method"].upper()
    return str(event.get("httpMethod") or "").upper()


def _path(event: dict[str, Any]) -> str:
    return str(event.get("rawPath") or event.get("path") or "")


def _request_id(event: Any) -> str:
    context = event.get("requestContext") if isinstance(event, Mapping) else None
    value = context.get("requestId") if isinstance(context, Mapping) else None
    return (
        value if type(value) is str and _REQUEST_ID.fullmatch(value) else "unavailable"
    )


def _error(status: int, request_id: str) -> dict[str, Any]:
    values = {
        400: ("invalid_request", "Request validation failed."),
        404: ("not_found", "Resource not found."),
        409: ("conflict", "The request conflicted."),
        503: ("unavailable", "Service unavailable."),
    }
    code, message = values.get(status, values[503])
    return _response(status, {"error": {"code": code, "message": message}}, request_id)


def _safe_metric(metric_sink: Any, name: str, value: int) -> None:
    try:
        metric_sink(name, value)
    except Exception:
        pass
