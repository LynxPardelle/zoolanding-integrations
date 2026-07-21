"""Fail-closed Phase 5 migration preview seam."""

from .internal_command import (
    UnavailableCommandService,
    configured_callers,
    handle_internal_command,
)

PATH = "/internal/v1/stripe/migrations/preview"
KIND = "migration-preview"


def handle_request(event, *, allowed_callers=None):
    return handle_internal_command(
        event,
        path=PATH,
        kind=KIND,
        service=UnavailableCommandService(),
        allowed_callers=(
            allowed_callers if allowed_callers is not None else configured_callers()
        ),
    )


def lambda_handler(event, context):
    del context
    return handle_request(event)
