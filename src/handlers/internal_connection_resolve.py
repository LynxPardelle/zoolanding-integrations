"""AWS_IAM scoped connection resolution seam."""

try:
    from common.http import dispatch, validation_error
    from contracts.internal import (
        ContractError,
        validate_command,
        validate_connection_resolution_result,
    )
except ModuleNotFoundError:
    from src.common.http import dispatch, validation_error
    from src.contracts.internal import (
        ContractError,
        validate_command,
        validate_connection_resolution_result,
    )
from .internal_command import configured_callers, require_internal_caller

PATH = "/internal/v1/integrations/connection-resolve"
KIND = "connection-resolve"


def handle_request(event, *, service=None, allowed_callers=None):
    callers = allowed_callers if allowed_callers is not None else configured_callers()

    def handle(payload):
        require_internal_caller(event, callers)
        if service is None:
            raise RuntimeError("connection resolution unavailable")
        try:
            command = validate_command(KIND, payload)
        except ContractError:
            raise validation_error() from None
        try:
            return validate_connection_resolution_result(
                service.resolve(command), command
            )
        except ContractError:
            raise RuntimeError("invalid resolution response") from None

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
        from runtime import connection_resolution_runtime
    except ModuleNotFoundError:
        from src.runtime import connection_resolution_runtime
    return connection_resolution_runtime()
