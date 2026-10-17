"""Run with `python tests/test_shadow.py` or `pytest tests/`.

These pin the interpretation of the device's lockState strings, which is the
only place a wrong answer turns into a door that reads open when it is shut.
Verbatim readings captured from an HLM04A Smart Door Lock are used as fixtures.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "custom_components" / "qubo")
)

from shadow import parse_locked, parse_percent, parse_vms_events  # noqa: E402

# A doorbell event as returned by the cloud video feed. The device UUID is
# anonymised; the field shapes, timestamps and status values are real, because
# those are what the parsing below has to get right.
VMS_SAMPLE = [
    {
        "transactionUUID": "afa481d6-c749-11f1-926e-e580486088fe_1791996602133",
        "deviceUUID": "00000000-0000-4000-8000-000000000002",
        "type": None,
        "createdTimestamp": 1791996607929,
        "closedTimestamp": 1791996626269,
        "callStatus": "MISSED",
        "isRecorded": True,
        "facesDetected": None,
        "snapshot": {"contentUUID": "69badc31-ea3e-4c89-81d6-cba29d19b302"},
    },
    {
        "transactionUUID": "older-event",
        "createdTimestamp": 1791900000000,
        "closedTimestamp": 1791900001000,
        "callStatus": "ANSWERED",
        "isRecorded": False,
        "facesDetected": None,
        "snapshot": None,
    },
]


def test_locked_never_looks_unlocked() -> None:
    """'locked' must not be read as unlocked, whichever case it arrives in."""
    for value in ("locked", "LOCKED", "Locked"):
        assert parse_locked(value) is True


def test_unlocked_is_always_unlocked() -> None:
    """'unlocked' contains 'lock', so the negative case must be checked first."""
    for value in ("unlocked", "unLocked", "UNLOCKED", "unlock"):
        assert parse_locked(value) is False


def test_unrecognised_state_is_treated_as_locked() -> None:
    """Fail safe: an unknown reading must not claim the door is open."""
    assert parse_locked("") is True
    assert parse_locked("someNewState") is True


def test_parse_percent_from_device_strings() -> None:
    assert parse_percent("100") == 100
    assert parse_percent("8") == 8
    assert parse_percent("") is None
    assert parse_percent("n/a") is None


def test_vms_events_are_newest_first() -> None:
    """The newest event must be first, whichever order the cloud returns."""
    events = parse_vms_events(list(reversed(VMS_SAMPLE)))
    assert [e.transaction_uuid for e in events] == [
        "afa481d6-c749-11f1-926e-e580486088fe_1791996602133",
        "older-event",
    ]


def test_vms_event_fields() -> None:
    newest = parse_vms_events(VMS_SAMPLE)[0]
    assert newest.created == 1791996607929
    assert newest.closed == 1791996626269
    assert newest.call_status == "MISSED"
    assert newest.is_recorded is True
    assert newest.has_snapshot is True


def test_vms_null_snapshot_is_not_a_snapshot() -> None:
    older = parse_vms_events(VMS_SAMPLE)[1]
    assert older.has_snapshot is False
    assert older.call_status == "ANSWERED"


def test_vms_payload_junk_is_dropped_not_guessed() -> None:
    assert parse_vms_events(None) == []
    assert parse_vms_events({"error": "nope"}) == []
    assert parse_vms_events("nope") == []
    assert parse_vms_events(["string", 5, None]) == []
    # missing uuid or timestamp is unusable, not a zeroed event
    assert parse_vms_events([{"createdTimestamp": 1791996607929}]) == []
    assert parse_vms_events([{"transactionUUID": "x"}]) == []
    assert parse_vms_events([{"transactionUUID": "x", "createdTimestamp": None}]) == []


if __name__ == "__main__":
    for name, func in sorted(globals().items()):
        if name.startswith("test_"):
            func()
            print(f"ok  {name}")
    print("all passed")