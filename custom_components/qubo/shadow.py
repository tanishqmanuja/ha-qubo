"""Pure helpers for interpreting Qubo device readings.

Kept free of Home Assistant imports so the fiddly bits can be unit tested.
"""

from __future__ import annotations

from typing import NamedTuple

__all__ = ["DoorbellEvent", "parse_locked", "parse_percent", "parse_vms_events"]


def parse_locked(value: str) -> bool:
    """Return whether a lockControl.lockState value means the bolt is engaged."""
    return "unlock" not in (value or "").strip().lower()


def parse_percent(value: str) -> int | None:
    """Return a 0-100 percentage from a device string, or None if absent."""
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return int(digits[:3]) if digits else None


class DoorbellEvent(NamedTuple):
    """One doorbell event as recorded by the cloud video service."""

    transaction_uuid: str
    created: int
    closed: int
    call_status: str
    is_recorded: bool
    has_snapshot: bool


def _ms(value: object) -> int:
    """Coerce a cloud timestamp to epoch milliseconds, 0 when unusable."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def parse_vms_events(payload: object) -> list[DoorbellEvent]:
    """Return doorbell events from a ``/vms`` payload, newest first.

    The cloud has never been observed to classify these (``type`` and
    ``facesDetected`` are always null on this hardware), so an event only means
    the doorbell rang. Anything without a uuid or timestamp is dropped rather
    than guessed at.
    """
    if not isinstance(payload, list):
        return []
    events: list[DoorbellEvent] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        uuid = item.get("transactionUUID")
        created = _ms(item.get("createdTimestamp"))
        if not uuid or not created:
            continue
        events.append(
            DoorbellEvent(
                transaction_uuid=str(uuid),
                created=created,
                closed=_ms(item.get("closedTimestamp")),
                call_status=str(item.get("callStatus") or ""),
                is_recorded=bool(item.get("isRecorded")),
                has_snapshot=bool(item.get("snapshot")),
            )
        )
    events.sort(key=lambda event: event.created, reverse=True)
    return events