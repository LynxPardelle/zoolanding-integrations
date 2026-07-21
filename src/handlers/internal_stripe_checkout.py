"""AWS_IAM Stripe Checkout command seam."""

from .internal_command import (
    UnavailableCommandService,
    configured_callers,
    handle_internal_command,
)

PATH = "/internal/v1/stripe/checkout"
KIND = "checkout"


def handle_request(event, *, service=None, allowed_callers=None):
    return handle_internal_command(
        event,
        path=PATH,
        kind=KIND,
        service=service or UnavailableCommandService(),
        allowed_callers=(
            allowed_callers if allowed_callers is not None else configured_callers()
        ),
    )


def lambda_handler(event, context):
    del context
    return handle_request(event)
