"""AWS_IAM-only SMTP connection activation seam."""

try:
    from common.http import dispatch, validation_error
    from contracts.internal import (
        ContractError,
        validate_smtp_connection_activation,
        validate_smtp_connection_activation_result,
    )
except ModuleNotFoundError:
    from src.common.http import dispatch, validation_error
    from src.contracts.internal import (
        ContractError,
        validate_smtp_connection_activation,
        validate_smtp_connection_activation_result,
    )
from .internal_command import (
    configured_smtp_activation_callers,
    require_internal_caller,
)


PATH = "/internal/v1/integrations/smtp-connection-activate"


def handle_request(event, *, service=None, allowed_callers=None):
    callers = (
        allowed_callers
        if allowed_callers is not None
        else configured_smtp_activation_callers()
    )

    def handle(payload):
        require_internal_caller(event, callers)
        if service is None:
            raise RuntimeError("SMTP activation unavailable")
        try:
            command = validate_smtp_connection_activation(payload)
        except ContractError:
            raise validation_error() from None
        try:
            return validate_smtp_connection_activation_result(
                service.activate(command), command
            )
        except ContractError:
            raise RuntimeError("invalid SMTP activation response") from None

    return dispatch(event, PATH, handle)


def lambda_handler(event, context):
    del context
    try:
        dependencies = _runtime_dependencies()
    except Exception:
        return handle_request(event)
    return handle_request(event, **dependencies)


def _runtime_dependencies():
    try:
        from runtime import smtp_connection_activation_runtime
    except ModuleNotFoundError:
        from src.runtime import smtp_connection_activation_runtime
    return smtp_connection_activation_runtime()
