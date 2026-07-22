"""Redacted AWS_IAM readiness smoke for the Integrations internal API."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import sys
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}", re.ASCII)
_DOMAIN = re.compile(
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+",
    re.ASCII,
)
_REGION = re.compile(r"[a-z]{2}(?:-gov)?-[a-z]+-[0-9]", re.ASCII)
_API_HOST = re.compile(
    r"[a-z0-9]{10}\.execute-api\.(?P<region>[a-z0-9-]+)\.amazonaws\.com(?:\.cn)?",
    re.ASCII,
)
_REQUIRED = (
    "ZLP_INTEGRATIONS_SMOKE_API_URL",
    "ZLP_INTEGRATIONS_SMOKE_TENANT_ID",
    "ZLP_INTEGRATIONS_SMOKE_DRAFT_ID",
    "ZLP_INTEGRATIONS_SMOKE_DOMAIN",
    "ZLP_INTEGRATIONS_SMOKE_CONNECTION_ID",
    "AWS_REGION",
)


@dataclass(frozen=True, slots=True)
class SmokeRequest:
    url: str
    region: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SmokeResponse:
    status: int


class SmokeAuthError(RuntimeError):
    """Credential resolution failed without exposing credential-provider details."""


def run(
    environment: Mapping[str, str],
    *,
    sender: Callable[[SmokeRequest], SmokeResponse] | None = None,
    now_epoch: Callable[[], int] | None = None,
) -> dict[str, Any]:
    values = {name: environment.get(name, "").strip() for name in _REQUIRED}
    if any(not values[name] for name in _REQUIRED):
        return _result(False, "missing_input", attempts=0)
    try:
        request = _request(values)
    except (UnicodeError, ValueError):
        request = None
    if request is None:
        return _result(False, "missing_input", attempts=0)
    transport = sender or _send
    try:
        response = transport(request)
        status = response.status
    except SmokeAuthError:
        return _result(False, "auth_failure", attempts=1)
    except Exception:
        return _result(False, "provider_failure", attempts=1)
    if type(status) is not int or not 100 <= status <= 599:
        return _result(False, "provider_failure", attempts=1)
    if 200 <= status <= 299:
        return _result(True, "ready", status=status, attempts=1)
    if status in {401, 403}:
        return _result(False, "auth_failure", status=status, attempts=1)
    if status == 404 and _before_propagation_deadline(
        environment, (now_epoch or (lambda: int(time.time())))()
    ):
        return _result(False, "propagation_delay", status=status, attempts=1)
    if 400 <= status <= 499:
        return _result(False, "configuration_failure", status=status, attempts=1)
    return _result(False, "provider_failure", status=status, attempts=1)


def _request(values: Mapping[str, str]) -> SmokeRequest | None:
    parsed = urlsplit(values["ZLP_INTEGRATIONS_SMOKE_API_URL"])
    host = parsed.hostname or ""
    port = parsed.port
    host_match = _API_HOST.fullmatch(host)
    region = values["AWS_REGION"]
    stage = parsed.path.strip("/")
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
        or host_match is None
        or host_match["region"] != region
        or _REGION.fullmatch(region) is None
        or stage not in {"test", "production"}
    ):
        return None
    tenant_id = values["ZLP_INTEGRATIONS_SMOKE_TENANT_ID"]
    draft_id = values["ZLP_INTEGRATIONS_SMOKE_DRAFT_ID"]
    domain = values["ZLP_INTEGRATIONS_SMOKE_DOMAIN"].lower()
    connection_id = values["ZLP_INTEGRATIONS_SMOKE_CONNECTION_ID"]
    if (
        _SAFE_ID.fullmatch(tenant_id) is None
        or _SAFE_ID.fullmatch(draft_id) is None
        or _SAFE_ID.fullmatch(connection_id) is None
        or _DOMAIN.fullmatch(domain) is None
    ):
        return None
    scope = {
        "environment": stage,
        "tenantId": tenant_id,
        "draftId": draft_id,
        "domain": domain,
    }
    fingerprint = hashlib.sha256(
        json.dumps(scope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    payload = {
        "version": 1,
        "scope": scope,
        "connectionId": connection_id,
        "commandId": "readiness-smoke",
        "idempotencyKey": f"readiness-smoke:{fingerprint}",
        "input": {"provider": "email.smtp", "capability": "send"},
    }
    return SmokeRequest(
        url=(
            f"{values['ZLP_INTEGRATIONS_SMOKE_API_URL'].rstrip('/')}"
            "/internal/v1/integrations/connection-resolve"
        ),
        region=region,
        payload=payload,
    )


def _send(smoke_request: SmokeRequest) -> SmokeResponse:
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.session import get_session

    body = json.dumps(
        smoke_request.payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    try:
        credentials = get_session().get_credentials()
        frozen_credentials = credentials.get_frozen_credentials() if credentials else None
    except Exception as error:
        raise SmokeAuthError("AWS credentials unavailable") from error
    if frozen_credentials is None:
        raise SmokeAuthError("AWS credentials unavailable")
    aws_request = AWSRequest(
        method="POST",
        url=smoke_request.url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    SigV4Auth(frozen_credentials, "execute-api", smoke_request.region).add_auth(
        aws_request
    )
    outgoing = Request(
        smoke_request.url,
        data=body,
        headers=dict(aws_request.headers.items()),
        method="POST",
    )
    try:
        with urlopen(outgoing, timeout=10) as response:
            return SmokeResponse(response.status)
    except HTTPError as error:
        return SmokeResponse(error.code)
    except URLError as error:
        raise RuntimeError("Readiness transport unavailable") from error


def _before_propagation_deadline(environment: Mapping[str, str], now_epoch: int) -> bool:
    raw = environment.get("ZLP_INTEGRATIONS_SMOKE_PROPAGATION_UNTIL_EPOCH", "").strip()
    if (
        type(now_epoch) is not int
        or re.fullmatch(r"[0-9]{1,10}", raw, re.ASCII) is None
    ):
        return False
    deadline = int(raw)
    return 0 <= now_epoch < deadline <= now_epoch + 900


def _result(
    ok: bool,
    classification: str,
    *,
    status: int | None = None,
    attempts: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": ok,
        "classification": classification,
        "attempts": attempts,
    }
    if status is not None:
        result["httpStatus"] = status
    return result


def main() -> int:
    import os

    result = run(os.environ)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
