"""Reuse fresh Auth Admin sessions for draft-scoped Integrations authorization."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import hmac
import os
import re
import time
from typing import Any, Mapping

from .published_policy import ResolvedIntegrationPolicy


SESSION_COOKIE_NAME = "__Host-zlp_session"
DOMAIN_HEADER = "x-zlp-domain"
AUTH_PROFILE_HEADER = "x-zlp-auth-profile-id"
DEFAULT_CSRF_COOKIE_NAME = "zlp_csrf"
DEFAULT_CSRF_HEADER_NAME = "x-zlp-csrf"
HUMAN_CAPABILITIES = frozenset({"integration:read", "integration:manage"})
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII)
_COOKIE_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}", re.ASCII)
_HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,64}", re.ASCII)
_FORBIDDEN_SCOPE_HEADERS = (
    "x-zlp-tenant-id",
    "x-zlp-draft-id",
    "x-zlp-environment",
)


class AuthError(Exception):
    status_code = 403
    error_code = "forbidden"


class AuthenticationError(AuthError):
    status_code = 401
    error_code = "auth_required"


class AuthorizationError(AuthError):
    status_code = 403
    error_code = "forbidden"


@dataclass(frozen=True, slots=True)
class AuthorizedContext:
    environment: str
    tenant_id: str
    draft_id: str
    domain: str
    subject: str
    session_hash: str
    roles: tuple[str, ...]
    profile: Mapping[str, Any]
    session: Mapping[str, Any]


class DynamoAuthStore:
    def __init__(
        self,
        session_table_name: str,
        user_table_name: str,
        dynamodb: Any = None,
    ):
        if not session_table_name or not user_table_name:
            raise AuthenticationError("Authentication is unavailable")
        if dynamodb is None:
            try:
                import boto3  # type: ignore

                dynamodb = boto3.resource("dynamodb")
            except Exception:
                raise AuthenticationError("Authentication is unavailable") from None
        self._session_table = dynamodb.Table(session_table_name)
        self._user_table = dynamodb.Table(user_table_name)

    def get_session(self, session_hash: str) -> dict[str, Any] | None:
        try:
            item = self._session_table.get_item(
                Key={"sessionIdHash": session_hash},
                ConsistentRead=True,
            ).get("Item")
        except Exception:
            raise AuthenticationError("Authentication is unavailable") from None
        return item if isinstance(item, dict) else None

    def get_user(self, tenant_profile_key: str, user_key: str) -> dict[str, Any] | None:
        try:
            item = self._user_table.get_item(
                Key={"tenantProfileKey": tenant_profile_key, "userKey": user_key},
                ConsistentRead=True,
            ).get("Item")
        except Exception:
            raise AuthenticationError("Authentication is unavailable") from None
        return item if isinstance(item, dict) else None


def authorize_request(
    *,
    event: dict[str, Any],
    policies: ResolvedIntegrationPolicy,
    capability: str,
    mutation: bool = False,
    store: Any = None,
    now_epoch: int | None = None,
) -> AuthorizedContext:
    if capability not in HUMAN_CAPABILITIES:
        raise AuthorizationError("Integrations access denied")
    if any(_header(event, name) for name in _FORBIDDEN_SCOPE_HEADERS):
        raise AuthenticationError("Authentication required")
    domain = _header(event, DOMAIN_HEADER).lower()
    auth_profile_id = _header(event, AUTH_PROFILE_HEADER)
    if domain != policies.scope.domain or _SAFE_ID.fullmatch(auth_profile_id) is None:
        raise AuthenticationError("Authentication required")

    access = policies.admin_access
    if not isinstance(access, Mapping) or access.get("mode") != "auth-profile":
        raise AuthorizationError("Integrations access denied")
    if access.get(
        "authProfileId"
    ) != auth_profile_id or capability not in _string_values(
        access.get("capabilities")
    ):
        raise AuthorizationError("Integrations access denied")

    profile = _unique_profile(policies.auth_registry, auth_profile_id)
    if (
        profile.get("status") != "active"
        or profile.get("tenantId") != policies.scope.tenant_id
        or profile.get("domain", policies.scope.domain) != policies.scope.domain
    ):
        raise AuthorizationError("Integrations access denied")
    if profile.get("environment") is not None and _auth_environment(
        profile.get("environment")
    ) != _auth_environment(policies.scope.environment):
        raise AuthorizationError("Integrations access denied")
    admin_groups = set(_string_values(profile.get("adminGroups")))
    if not admin_groups:
        raise AuthorizationError("Integrations access denied")

    session_value = _cookie_value(event, SESSION_COOKIE_NAME)
    if not session_value:
        raise AuthenticationError("Authentication required")
    session_hash = _sha256(session_value)
    if store is None:
        store = DynamoAuthStore(
            os.getenv("AUTH_SESSION_TABLE_NAME", "").strip(),
            os.getenv("AUTH_USER_STATE_TABLE_NAME", "").strip(),
        )
    session = store.get_session(session_hash)
    now = int(time.time()) if now_epoch is None else int(now_epoch)
    auth_environment = _auth_environment(policies.scope.environment)
    tenant_profile_key = f"{policies.scope.domain}#{auth_profile_id}#{auth_environment}"
    if not isinstance(session, dict) or not _valid_session(
        session,
        now,
        tenant_profile_key,
        policies,
        auth_profile_id,
        auth_environment,
    ):
        raise AuthenticationError("Authentication required")

    subject_value = session.get("subject")
    subject = subject_value.strip() if isinstance(subject_value, str) else ""
    if _SAFE_ID.fullmatch(subject) is None:
        raise AuthenticationError("Authentication required")
    user = store.get_user(tenant_profile_key, f"USER#{subject}")
    if not isinstance(user, dict):
        raise AuthenticationError("Authentication required")
    session_version = _positive_int(session.get("sessionVersion"))
    user_version = _positive_int(user.get("sessionVersion"))
    if session_version == 0 or user_version != session_version:
        raise AuthenticationError("Authentication required")
    if user.get("enabled") is not True or user.get("approvalStatus") != "approved":
        raise AuthorizationError("Integrations access denied")

    roles = set(_string_values(user.get("roles")))
    allowed_groups = set(_string_values(profile.get("allowedGroups", ())))
    if allowed_groups:
        roles.intersection_update(allowed_groups)
    if not roles.intersection(admin_groups):
        raise AuthorizationError("Integrations access denied")
    if mutation:
        _require_csrf(event, session, profile)

    return AuthorizedContext(
        environment=policies.scope.environment,
        tenant_id=policies.scope.tenant_id,
        draft_id=policies.scope.draft_id,
        domain=policies.scope.domain,
        subject=subject,
        session_hash=session_hash,
        roles=tuple(sorted(roles)),
        profile=profile,
        session=dict(session),
    )


def _valid_session(
    session: dict[str, Any],
    now: int,
    tenant_profile_key: str,
    policies: ResolvedIntegrationPolicy,
    auth_profile_id: str,
    auth_environment: str,
) -> bool:
    return (
        not session.get("revokedAt")
        and _positive_int(session.get("expiresAt")) > now
        and session.get("recordType") in {None, "authSession"}
        and session.get("tenantProfileKey") == tenant_profile_key
        and session.get("domain") == policies.scope.domain
        and session.get("authProfileId") == auth_profile_id
        and session.get("environment") == auth_environment
        and session.get("tenantId") == policies.scope.tenant_id
    )


def _require_csrf(
    event: dict[str, Any],
    session: dict[str, Any],
    profile: Mapping[str, Any],
) -> None:
    session_policy = profile.get("session")
    session_policy = session_policy if isinstance(session_policy, Mapping) else {}
    cookie_name = str(session_policy.get("csrfCookieName") or "").strip()
    header_name = str(session_policy.get("csrfHeaderName") or "").strip()
    if _COOKIE_NAME.fullmatch(cookie_name) is None:
        cookie_name = DEFAULT_CSRF_COOKIE_NAME
    if _HEADER_NAME.fullmatch(header_name) is None:
        header_name = DEFAULT_CSRF_HEADER_NAME
    header_value = _header(event, header_name)
    cookie_value = _cookie_value(event, cookie_name)
    expected_hash = str(session.get("csrfHash") or "")
    if (
        not header_value
        or not cookie_value
        or not expected_hash
        or not hmac.compare_digest(header_value, cookie_value)
        or not hmac.compare_digest(_sha256(header_value), expected_hash)
    ):
        raise AuthorizationError("CSRF validation failed")


def _unique_profile(registry: Any, auth_profile_id: str) -> Mapping[str, Any]:
    profiles = registry.get("profiles") if isinstance(registry, Mapping) else None
    if not isinstance(profiles, (list, tuple)):
        raise AuthorizationError("Integrations access denied")
    matches = [
        item
        for item in profiles
        if isinstance(item, Mapping) and item.get("authProfileId") == auth_profile_id
    ]
    if len(matches) != 1:
        raise AuthorizationError("Integrations access denied")
    return matches[0]


def _header(event: dict[str, Any], name: str) -> str:
    headers = event.get("headers") if isinstance(event, dict) else None
    if not isinstance(headers, dict):
        return ""
    for key, value in headers.items():
        if str(key).lower() == name.lower() and isinstance(value, str):
            return value.strip()
    return ""


def _cookie_value(event: dict[str, Any], name: str) -> str:
    cookies = event.get("cookies") if isinstance(event, dict) else None
    if isinstance(cookies, list):
        for raw_cookie in cookies:
            parts = str(raw_cookie).split(";", 1)[0].split("=", 1)
            if len(parts) == 2 and parts[0].strip() == name:
                return parts[1].strip()
    for raw_cookie in _header(event, "cookie").split(";"):
        parts = raw_cookie.strip().split("=", 1)
        if len(parts) == 2 and parts[0] == name:
            return parts[1].strip()
    return ""


def _string_values(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise AuthorizationError("Integrations access denied")
    return tuple(item.strip() for item in value)


def _positive_int(value: Any) -> int:
    if type(value) is int:
        return value if value > 0 else 0
    if (
        type(value) is Decimal
        and value.is_finite()
        and value == value.to_integral_value()
    ):
        parsed = int(value)
        return parsed if parsed > 0 else 0
    return 0


def _auth_environment(value: Any) -> str:
    environment = str(value or "").strip().lower()
    return "prod" if environment == "production" else environment


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
