"""AWS_IAM Stripe Checkout canonical-status seam."""

from .internal_command import (
    UnavailableCommandService,
    configured_callers,
    handle_internal_command,
)

PATH = "/internal/v1/stripe/checkout-status"
KIND = "checkout-status"


def handle_request(event, *, service=None, allowed_callers=None):
    return handle_internal_command(
        event,
        path=PATH,
        kind=KIND,
        service=service or UnavailableCommandService(),
        allowed_callers=(
            allowed_callers if allowed_callers is not None else configured_callers()
        ),
        method="GET",
    )


def lambda_handler(event, context):
    del context
    try:
        dependencies = _runtime_dependencies()
    except Exception:
        return handle_request(event)
    return handle_request(event, **dependencies)


def _runtime_dependencies():
    try:
        from runtime import stripe_command_runtime
    except ModuleNotFoundError:
        from src.runtime import stripe_command_runtime
    return stripe_command_runtime()
