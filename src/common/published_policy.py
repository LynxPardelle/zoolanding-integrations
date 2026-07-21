"""Resolve the immutable Integration Bindings policy from its published pointer."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    from domain.integrations import IntegrationBinding, IntegrationScope
except ModuleNotFoundError:
    from src.domain.integrations import IntegrationBinding, IntegrationScope


MAX_DESCRIPTOR_BYTES = 256 * 1024
MAX_JSON_DEPTH = 32
_VERSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", re.ASCII)
_AUTH_PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", re.ASCII)


class PolicyResolutionError(RuntimeError):
    """A redacted, fail-closed policy resolution failure."""


@dataclass(frozen=True, slots=True)
class ResolvedIntegrationPolicy:
    scope: IntegrationScope
    version_id: str
    prefix: str
    bindings: tuple[IntegrationBinding, ...]
    admin_access: Any
    auth_registry: Any


class PublishedPolicyResolver:
    def __init__(self, registry_table: Any, s3_client: Any, bucket_name: str):
        if not bucket_name:
            raise PolicyResolutionError("Published Integration policy is unavailable")
        self._registry = registry_table
        self._s3 = s3_client
        self._bucket = bucket_name
        self._cache: dict[tuple[str, str, str, str, str], ResolvedIntegrationPolicy] = (
            {}
        )

    def resolve(
        self,
        *,
        environment: str,
        domain: str,
        tenant_id: str | None = None,
        draft_id: str | None = None,
    ) -> ResolvedIntegrationPolicy:
        try:
            metadata = self._metadata(domain)
            server_scope = metadata.get("serverScope")
            if (
                not isinstance(server_scope, dict)
                or set(server_scope) != {"tenantId", "draftId"}
                or metadata.get("domain") != domain
            ):
                raise ValueError
            scope = IntegrationScope(
                environment,
                server_scope.get("tenantId"),
                server_scope.get("draftId"),
                domain,
            )
            if tenant_id is not None and tenant_id != scope.tenant_id:
                raise ValueError
            if draft_id is not None and draft_id != scope.draft_id:
                raise ValueError
            pointer = _published_pointer(metadata, environment)
            version_id = _version_id(pointer.get("versionId") if pointer else None)
            prefix = f"sites/{domain}/versions/{version_id}/"
            if not pointer or pointer.get("prefix") != prefix:
                raise ValueError
        except PolicyResolutionError:
            raise
        except (TypeError, ValueError):
            raise PolicyResolutionError(
                "Published Integration policy scope is invalid"
            ) from None

        cache_key = (environment, scope.tenant_id, scope.draft_id, domain, version_id)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        key = f"{prefix}{domain}/server/integration-bindings.json"
        value = self._load_json(key)
        try:
            if (
                set(value)
                != {
                    "version",
                    "scope",
                    "adminAccess",
                    "bindings",
                }
                or value.get("version") != 1
            ):
                raise ValueError
            if value.get("scope") != scope.fields():
                raise ValueError
            raw_bindings = value.get("bindings")
            if not isinstance(raw_bindings, list) or not 1 <= len(raw_bindings) <= 32:
                raise ValueError
            bindings = tuple(
                IntegrationBinding.from_mapping(scope, item) for item in raw_bindings
            )
            if len({item.binding_id for item in bindings}) != len(bindings):
                raise ValueError
            admin_access = _admin_access(value.get("adminAccess"))
        except (TypeError, ValueError):
            raise PolicyResolutionError(
                "Published Integration policy is invalid"
            ) from None
        auth_registry: Any = MappingProxyType({})
        if admin_access["mode"] == "auth-profile":
            auth_registry = _auth_registry(
                self._load_json(f"{prefix}{domain}/server/auth-profile-registry.json")
            )
        resolved = ResolvedIntegrationPolicy(
            scope,
            version_id,
            prefix,
            bindings,
            admin_access,
            auth_registry,
        )
        self._cache[cache_key] = resolved
        return resolved

    def _checkout_policy(self, scope: IntegrationScope) -> dict[str, Any]:
        if type(scope) is not IntegrationScope:
            raise PolicyResolutionError("Published Checkout policy is unavailable")
        resolved = self.resolve(
            environment=scope.environment,
            domain=scope.domain,
            tenant_id=scope.tenant_id,
            draft_id=scope.draft_id,
        )
        return self._load_json(
            f"{resolved.prefix}{scope.domain}/server/commerce.json"
        )

    def _metadata(self, domain: str) -> dict[str, Any]:
        try:
            response = self._registry.get_item(
                Key={"pk": f"SITE#{domain}", "sk": "METADATA"},
                ConsistentRead=True,
            )
            item = response.get("Item") if isinstance(response, dict) else None
        except Exception:
            raise PolicyResolutionError(
                "Published Integration policy is unavailable"
            ) from None
        if not isinstance(item, dict):
            raise PolicyResolutionError("Published Integration policy is unavailable")
        return item

    def _load_json(self, key: str) -> dict[str, Any]:
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=key)
            length = response.get("ContentLength")
            if type(length) is not int or not 0 <= length <= MAX_DESCRIPTOR_BYTES:
                raise PolicyResolutionError("Published Integration policy is invalid")
            raw = response["Body"].read(MAX_DESCRIPTOR_BYTES + 1)
        except PolicyResolutionError:
            raise
        except Exception:
            raise PolicyResolutionError(
                "Published Integration policy is unavailable"
            ) from None
        if (
            not isinstance(raw, bytes)
            or len(raw) != length
            or len(raw) > MAX_DESCRIPTOR_BYTES
        ):
            raise PolicyResolutionError("Published Integration policy is invalid")
        try:
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (UnicodeDecodeError, TypeError, ValueError, RecursionError):
            raise PolicyResolutionError(
                "Published Integration policy is invalid"
            ) from None
        if not isinstance(value, dict) or _json_depth(value) > MAX_JSON_DEPTH:
            raise PolicyResolutionError("Published Integration policy is invalid")
        return value


class PublishedCheckoutRouteResolver:
    """Resolve only code-owned Checkout callback routes from a pinned release."""

    def __init__(self, policy_resolver: PublishedPolicyResolver):
        if type(policy_resolver) is not PublishedPolicyResolver:
            raise PolicyResolutionError("Published Checkout policy is unavailable")
        self._policies = policy_resolver

    def resolve(self, scope: IntegrationScope) -> dict[str, str]:
        try:
            value = self._policies._checkout_policy(scope)
            if set(value) != {"version", "scope", "commerce"} or value["version"] != 1:
                raise ValueError
            if value["scope"] != scope.fields():
                raise ValueError
            commerce = value["commerce"]
            if not isinstance(commerce, dict) or commerce.get("status") != "active":
                raise ValueError
            checkout = commerce.get("checkout")
            expected = {
                "successPath",
                "cancelPath",
                "termsPath",
                "privacyPath",
                "refundPolicyPath",
                "supportPath",
            }
            if not isinstance(checkout, dict) or set(checkout) != expected:
                raise ValueError
            paths = {key: _same_origin_path(value) for key, value in checkout.items()}
            return {
                "successUrl": _checkout_url(scope, paths["successPath"]),
                "cancelUrl": _checkout_url(scope, paths["cancelPath"]),
            }
        except PolicyResolutionError:
            raise
        except (KeyError, TypeError, ValueError):
            raise PolicyResolutionError(
                "Published Checkout policy is invalid"
            ) from None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError
        output[key] = value
    return output


def _json_depth(value: Any) -> int:
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


def _published_pointer(
    metadata: dict[str, Any], environment: str
) -> dict[str, Any] | None:
    environments = metadata.get("publishedEnvironments")
    environments = environments if isinstance(environments, dict) else {}
    pointer = (
        environments.get("test")
        if environment == "test"
        else metadata.get("published") or environments.get("production")
    )
    return pointer if isinstance(pointer, dict) else None


def _version_id(value: object) -> str:
    if type(value) is not str or _VERSION_ID.fullmatch(value) is None:
        raise ValueError
    return value


def _admin_access(value: object) -> Any:
    if not isinstance(value, dict):
        raise ValueError
    if value.get("mode") == "none" and set(value) == {"mode"}:
        return MappingProxyType({"mode": "none"})
    if set(value) != {"mode", "authProfileId", "capabilities"}:
        raise ValueError
    if value.get("mode") != "auth-profile":
        raise ValueError
    auth_profile_id = value.get("authProfileId")
    if (
        type(auth_profile_id) is not str
        or _AUTH_PROFILE_ID.fullmatch(auth_profile_id) is None
    ):
        raise ValueError
    capabilities = value.get("capabilities")
    if (
        not isinstance(capabilities, list)
        or not 1 <= len(capabilities) <= 2
        or len(set(capabilities)) != len(capabilities)
        or not set(capabilities).issubset({"integration:read", "integration:manage"})
    ):
        raise ValueError
    return MappingProxyType(
        {
            "mode": "auth-profile",
            "authProfileId": auth_profile_id,
            "capabilities": tuple(capabilities),
        }
    )


def _auth_registry(value: object) -> Any:
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "profiles"}
        or value.get("version") != 1
        or not isinstance(value.get("profiles"), list)
        or not 1 <= len(value["profiles"]) <= 100
    ):
        raise PolicyResolutionError("Published Integration policy is invalid")
    profile_ids = [
        item.get("authProfileId") if isinstance(item, dict) else None
        for item in value["profiles"]
    ]
    if any(
        type(item) is not str or _AUTH_PROFILE_ID.fullmatch(item) is None
        for item in profile_ids
    ) or len(set(profile_ids)) != len(profile_ids):
        raise PolicyResolutionError("Published Integration policy is invalid")
    return _freeze(value)


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _same_origin_path(value: object) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 256
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(
            character.isspace()
            or ord(character) < 32
            or ord(character) == 127
            for character in value
        )
    ):
        raise ValueError
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        raise ValueError
    query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=False)
    if any(key == "draftDomain" for key, _value in query):
        raise ValueError
    return value


def _checkout_url(scope: IntegrationScope, path: str) -> str:
    parsed = urlsplit(path)
    if scope.environment == "production":
        host = scope.domain
        query = parsed.query
    else:
        host = "test.zoolandingpage.com.mx"
        query_items = parse_qsl(parsed.query, keep_blank_values=True)
        query_items.append(("draftDomain", scope.domain))
        query = urlencode(query_items)
    return urlunsplit(("https", host, parsed.path, query, parsed.fragment))
