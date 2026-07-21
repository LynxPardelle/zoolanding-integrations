"""AWS_IAM connection registration seam."""

try:
    from common.http import dispatch, validation_error
    from contracts.internal import (
        ContractError,
        validate_connection_registration,
        validate_connection_registration_result,
    )
except ModuleNotFoundError:
    from src.common.http import dispatch, validation_error
    from src.contracts.internal import (
        ContractError,
        validate_connection_registration,
        validate_connection_registration_result,
    )
from .internal_command import configured_callers, require_internal_caller

PATH = "/internal/v1/integrations/connection-register"
KIND = "connection-register"


def handle_request(event, *, service=None, allowed_callers=None):
    callers = allowed_callers if allowed_callers is not None else configured_callers()

    def handle(payload):
        require_internal_caller(event, callers)
        if service is None:
            raise RuntimeError("connection registration unavailable")
        try:
            registration = validate_connection_registration(payload)
        except ContractError:
            raise validation_error() from None
        try:
            return validate_connection_registration_result(
                service.register(registration), registration
            )
        except ContractError:
            raise RuntimeError("invalid registration response") from None

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
        from runtime import connection_registration_runtime
    except ModuleNotFoundError:
        from src.runtime import connection_registration_runtime
    return connection_registration_runtime()
