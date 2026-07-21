"""Fail closed until the verified webhook and outbox workers replace this boundary."""

from __future__ import annotations

from typing import Any


def lambda_handler(
    event: dict[str, Any], context: Any
) -> dict[str, list[dict[str, str]]]:
    del context
    records = event.get("Records") if isinstance(event, dict) else None
    if not isinstance(records, list):
        raise ValueError("stream event is invalid")
    failures: list[dict[str, str]] = []
    for record in records:
        event_id = record.get("eventID") if isinstance(record, dict) else None
        if type(event_id) is not str or not event_id:
            raise ValueError("stream record is invalid")
        failures.append({"itemIdentifier": event_id})
    return {"batchItemFailures": failures}
