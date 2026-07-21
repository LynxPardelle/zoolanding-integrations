"""Strict no-store HTTP boundary for Integrations browser routes."""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Callable

from .auth_admin import AuthError
from .published_policy import PolicyResolutionError


MAX_BODY_BYTES = 64 * 1024
_DOMAIN = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+",
    re.ASCII,
)
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}", re.ASCII)
_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}", re.ASCII)


class HttpError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def dispatch(
    event: Any,
    exact_path: str,
    callback: Callable[[dict[str, Any]], Any],
    method: str = "POST",
) -> dict[str, Any]:
    request_id = _request_id(event)
    try:
        if (
            not isinstance(event, dict)
            or _method(event) != method
            or _path(event) != exact_path
        ):
            raise HttpError(404, "not_found", "Resource not found.")
        return _response(200, {"data": callback(_body(event))}, request_id)
    except HttpError as error:
        return _response(
            error.status_code,
            {"error": {"code": error.code, "message": error.message}},
            request_id,
        )
    except AuthError as error:
        return _response(
            error.status_code,
            {
                "error": {
                    "code": error.error_code,
                    "message": (
                        "Authentication required."
                        if error.status_code == 401
                        else "You do not have access to this resource."
                    ),
                }
            },
            request_id,
        )
    except PolicyResolutionError:
        return _response(
            503,
            {"error": {"code": "unavailable", "message": "Service unavailable."}},
            request_id,
        )
    except Exception as error:
        name = type(error).__name__
        if name in {"RegistryAccessDenied"}:
            status, code, message = 404, "not_found", "Resource not found."
        elif name in {"RegistryConflict", "StripeCommandConflict"}:
            status, code, message = 409, "conflict", "The resource changed."
        else:
            status, code, message = 503, "unavailable", "Service unavailable."
        return _response(
            status, {"error": {"code": code, "message": message}}, request_id
        )


def closed_object(
    value: object, required: set[str], optional: set[str] | None = None
) -> dict[str, Any]:
    optional = optional or set()
    if (
        not isinstance(value, dict)
        or not required.issubset(value)
        or not set(value).issubset(required | optional)
    ):
        raise validation_error()
    return value


def safe_id(value: object) -> str:
    if type(value) is not str or _SAFE_ID.fullmatch(value) is None:
        raise validation_error()
    return value


def positive_int(value: object) -> int:
    if type(value) is not int or value < 1:
        raise validation_error()
    return value


def domain_header(event: dict[str, Any]) -> str:
    domain = header(event, "x-zlp-domain").lower()
    if _DOMAIN.fullmatch(domain) is None:
        raise validation_error()
    return domain


def header(event: dict[str, Any], name: str) -> str:
    headers = event.get("headers") if isinstance(event, dict) else None
    if not isinstance(headers, dict):
        return ""
    for key, value in headers.items():
        if str(key).lower() == name.lower() and isinstance(value, str):
            return value.strip()
    return ""


def validation_error() -> HttpError:
    return HttpError(422, "invalid_request", "Request validation failed.")


def unavailable_response(event: Any) -> dict[str, Any]:
    return _response(
        503,
        {"error": {"code": "unavailable", "message": "Service unavailable."}},
        _request_id(event),
    )


def _body(event: dict[str, Any]) -> dict[str, Any]:
    body = event.get("body")
    encoded = event.get("isBase64Encoded", False)
    if type(body) is not str or type(encoded) is not bool:
        raise validation_error()
    try:
        raw = (
            base64.b64decode(body.encode("ascii"), validate=True)
            if encoded
            else body.encode("utf-8")
        )
    except (UnicodeEncodeError, ValueError):
        raise validation_error() from None
    if not raw or len(raw) > MAX_BODY_BYTES:
        raise validation_error()
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except (UnicodeDecodeError, TypeError, ValueError, RecursionError):
        raise validation_error() from None
    if not isinstance(value, dict) or _depth(value) > 32:
        raise validation_error()
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _depth(value: Any) -> int:
    deepest = 0
    stack = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        deepest = max(deepest, depth)
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return deepest


def _method(event: dict[str, Any]) -> str:
    context = event.get("requestContext")
    http = context.get("http") if isinstance(context, dict) else None
    if isinstance(http, dict) and isinstance(http.get("method"), str):
        return http["method"].upper()
    return str(event.get("httpMethod") or "").upper()


def _path(event: dict[str, Any]) -> str:
    return str(event.get("rawPath") or event.get("path") or "")


def _request_id(event: Any) -> str:
    context = event.get("requestContext") if isinstance(event, dict) else None
    value = context.get("requestId") if isinstance(context, dict) else None
    return (
        value
        if isinstance(value, str) and _REQUEST_ID.fullmatch(value)
        else "unavailable"
    )


def _response(status: int, value: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
        },
        "body": json.dumps(
            {**value, "requestId": request_id},
            sort_keys=True,
            separators=(",", ":"),
        ),
    }
