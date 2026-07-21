"""Server-only SMTP activation from fresh secret metadata and operator evidence."""

from __future__ import annotations

import hashlib
import re
from typing import Any

try:
    from connection_admin import validated_secret_tags
    from contracts.internal import SmtpConnectionActivation
    from domain.integrations import IntegrationConnection
except ModuleNotFoundError:
    from src.connection_admin import validated_secret_tags
    from src.contracts.internal import SmtpConnectionActivation
    from src.domain.integrations import IntegrationConnection


_HASH = re.compile(r"[a-f0-9]{64}", re.ASCII)
_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", re.ASCII)
_ACCOUNT_TAG = "zoolanding:smtp-account-isolation-id"
_CREDENTIAL_TAG = "zoolanding:smtp-credential-isolation-id"


class SmtpActivationError(RuntimeError):
    pass


class SmtpConnectionActivationService:
    def __init__(
        self,
        registry: Any,
        secrets_client: Any,
        required_test_account_claim_hash: object,
    ):
        if (
            registry is None
            or secrets_client is None
            or type(required_test_account_claim_hash) is not str
            or _HASH.fullmatch(required_test_account_claim_hash) is None
        ):
            raise SmtpActivationError("SMTP activation is unavailable")
        self._registry = registry
        self._secrets = secrets_client
        self._test_account_hash = required_test_account_claim_hash

    def activate(self, command: SmtpConnectionActivation) -> dict[str, Any]:
        if type(command) is not SmtpConnectionActivation:
            raise SmtpActivationError("SMTP activation is invalid")
        try:
            current = self._registry.connection(
                command.scope, command.connection_id
            )
            current_binding = self._registry.binding(
                command.scope, command.connection_id
            )
        except Exception:
            raise SmtpActivationError("SMTP connection is unavailable") from None
        if (
            current.scope != command.scope
            or current.connection_id != command.connection_id
            or current.provider != "email.smtp"
            or current.mode
            != ("test" if command.scope.environment == "test" else "live")
            or "send" not in current.capabilities
            or (
                current.status == "pending"
                and current.revision != command.expected_revision
            )
            or (
                current.status == "active"
                and current.revision != command.expected_revision + 1
            )
            or current.status not in {"pending", "active"}
            or current_binding.scope != command.scope
            or current_binding.binding_id != command.connection_id
            or current_binding.connection_id != command.connection_id
            or current_binding.provider != "email.smtp"
            or current_binding.status != "active"
            or current_binding.mode != current.mode
            or "send" not in current_binding.capabilities
        ):
            raise SmtpActivationError("SMTP connection is unavailable")
        try:
            secret_metadata = self._secrets.describe_secret(
                SecretId=current.credential_reference
            )
            tags = validated_secret_tags(current, secret_metadata)
        except Exception:
            raise SmtpActivationError("SMTP credential metadata is unavailable") from None

        account_hash = _tag_hash(tags, _ACCOUNT_TAG)
        credential_hash = _tag_hash(tags, _CREDENTIAL_TAG)
        if (
            command.scope.environment == "test"
            and account_hash != self._test_account_hash
        ) or (
            command.scope.environment == "production"
            and account_hash == self._test_account_hash
        ):
            raise SmtpActivationError("SMTP account isolation is invalid")

        expected_domain = (
            "zoolandingpage.com.mx"
            if command.scope.environment == "test"
            else command.scope.domain
        )
        candidate = IntegrationConnection(
            scope=command.scope,
            connection_id=command.connection_id,
            provider="email.smtp",
            adapter_version="v1",
            status="active",
            mode=current.mode,
            capabilities=current.capabilities,
            provider_metadata={
                "adapterId": "smtp2go-smtp-v1",
                "host": "mail.smtp2go.com",
                "port": 465,
                "tlsMode": "implicit",
                "canonicalSendingDomain": expected_domain,
                "fromLocalPart": command.from_local_part,
                "replyToLocalPart": command.reply_to_local_part,
                "accountIsolationHash": account_hash,
                "credentialIsolationHash": credential_hash,
                "ownershipEvidenceHash": _digest(command.ownership_evidence_id),
            },
            revision=command.expected_revision + 1,
        )
        activated = self._registry.activate_smtp(
            candidate, command.expected_revision, command.idempotency_key
        )
        return {
            "connectionId": activated.connection_id,
            "status": activated.status,
            "mode": activated.mode,
            "revision": activated.revision,
        }


def _tag_hash(tags: dict[str, str], key: str) -> str:
    value = tags.get(key)
    if type(value) is not str or _OPAQUE_ID.fullmatch(value) is None:
        raise SmtpActivationError("SMTP isolation metadata is invalid")
    return _digest(value)


def _digest(value: str) -> str:
    try:
        return hashlib.sha256(value.encode("ascii")).hexdigest()
    except UnicodeEncodeError:
        raise SmtpActivationError("SMTP isolation metadata is invalid") from None
