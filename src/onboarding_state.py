"""Opaque, scope-bound and one-use Stripe onboarding state."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from typing import Any

try:
    from domain.integrations import IntegrationScope
except ModuleNotFoundError:
    from src.domain.integrations import IntegrationScope


STATE_TTL_SECONDS = 10 * 60
_STATE = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)
_HASH = re.compile(r"[a-f0-9]{64}", re.ASCII)
_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}", re.ASCII)


class OnboardingStateError(RuntimeError):
    pass


class OnboardingStateManager:
    def __init__(self, store: Any, *, token_bytes: Any = secrets.token_bytes):
        if store is None or not callable(token_bytes):
            raise OnboardingStateError("Onboarding state is unavailable")
        self._store = store
        self._token_bytes = token_bytes

    def issue(
        self,
        scope: IntegrationScope,
        connection_id: object,
        *,
        session_hash: object,
        now_epoch: object,
    ) -> str:
        connection_id, session_hash, now_epoch = _inputs(
            scope, connection_id, session_hash, now_epoch
        )
        raw = self._token_bytes(32)
        if not isinstance(raw, bytes) or len(raw) != 32:
            raise OnboardingStateError("Onboarding state is unavailable")
        token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        state_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        record = {
            "pk": scope.partition_key,
            "sk": f"ONBOARDING_STATE#{state_hash}",
            "itemType": "StripeOnboardingState",
            **scope.fields(),
            "connectionId": connection_id,
            "sessionHash": session_hash,
            "stateHash": state_hash,
            "expiresAt": now_epoch + STATE_TTL_SECONDS,
        }
        try:
            self._store.put_once(record)
        except Exception:
            raise OnboardingStateError("Onboarding state is unavailable") from None
        return token

    def consume(
        self,
        token: object,
        scope: IntegrationScope,
        connection_id: object,
        *,
        session_hash: object,
        now_epoch: object,
    ) -> None:
        connection_id, session_hash, now_epoch = _inputs(
            scope, connection_id, session_hash, now_epoch
        )
        state_hash = _state_hash(token)
        expected = {
            **scope.fields(),
            "connectionId": connection_id,
            "sessionHash": session_hash,
            "stateHash": state_hash,
        }
        try:
            self._store.consume_once(state_hash, expected, now_epoch)
        except Exception:
            raise OnboardingStateError("Onboarding state is invalid") from None


class DynamoOnboardingStateStore:
    def __init__(self, table: Any):
        if table is None:
            raise OnboardingStateError("Onboarding state is unavailable")
        self._table = table

    def put_once(self, record: dict[str, Any]) -> None:
        self._table.put_item(
            Item=record,
            ConditionExpression="attribute_not_exists(pk) AND attribute_not_exists(sk)",
        )

    def consume_once(
        self, state_hash: str, expected: dict[str, Any], now_epoch: int
    ) -> dict[str, Any]:
        names = {f"#f{index}": key for index, key in enumerate(expected)}
        values = {f":v{index}": value for index, value in enumerate(expected.values())}
        conditions = [f"{name} = :v{index}" for index, name in enumerate(names)]
        values.update({":now": now_epoch, ":consumed": now_epoch})
        response = self._table.update_item(
            Key={
                "pk": (
                    f"ENV#{expected['environment']}#TENANT#{expected['tenantId']}"
                    f"#DRAFT#{expected['draftId']}"
                ),
                "sk": f"ONBOARDING_STATE#{state_hash}",
            },
            UpdateExpression="SET consumedAt = :consumed",
            ConditionExpression=(
                "attribute_not_exists(consumedAt) AND expiresAt > :now AND "
                + " AND ".join(conditions)
            ),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
        return response.get("Attributes", {})


def _inputs(
    scope: object,
    connection_id: object,
    session_hash: object,
    now_epoch: object,
) -> tuple[str, str, int]:
    if type(scope) is not IntegrationScope:
        raise OnboardingStateError("Onboarding state is invalid")
    if (
        type(connection_id) is not str
        or _SAFE_ID.fullmatch(connection_id) is None
        or type(session_hash) is not str
        or _HASH.fullmatch(session_hash) is None
        or type(now_epoch) is not int
        or now_epoch < 0
    ):
        raise OnboardingStateError("Onboarding state is invalid")
    return connection_id, session_hash, now_epoch


def _state_hash(value: object) -> str:
    if type(value) is not str or _STATE.fullmatch(value) is None:
        raise OnboardingStateError("Onboarding state is invalid")
    try:
        decoded = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    except ValueError:
        raise OnboardingStateError("Onboarding state is invalid") from None
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if len(decoded) != 32 or not hmac.compare_digest(value, canonical):
        raise OnboardingStateError("Onboarding state is invalid")
    return hashlib.sha256(value.encode("ascii")).hexdigest()
