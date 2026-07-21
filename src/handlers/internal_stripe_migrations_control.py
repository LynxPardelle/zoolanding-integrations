"""Fail-closed Phase 5 migration control seam."""

from .internal_command import (
    UnavailableCommandService,
    configured_callers,
    handle_internal_command,
)

PATH = "/internal/v1/stripe/migrations/control"
KIND = "migration-control"


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
    try:
        dependencies = _runtime_dependencies()
    except Exception:
        return handle_request(event)
    return handle_request(event, **dependencies)


def _runtime_dependencies():
    try:
        from runtime import subscription_migration_runtime
    except ModuleNotFoundError:
        from src.runtime import subscription_migration_runtime
    return subscription_migration_runtime()
