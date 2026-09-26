"""Idempotent replay of Maintenance mutations with pre-generated identities."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from custom_components.device_lifecycle.maintenance_mutations import (
    CalendarIntervalInput,
    CreateScheduleRequest,
    DeleteScheduleRequest,
    EditScheduleRequest,
    InitialAnchorInput,
    MaintenanceConflictError,
    MaintenanceNotFoundError,
    MaintenanceSnapshot,
    MutationOutcome,
    VoidEventRequest,
    correct_event,
    create_schedule,
    delete_schedule,
    edit_schedule,
    event_business_key,
    record_event,
    void_event,
)

from .test_maintenance_mutations import (
    EARLIER,
    EVENT_1,
    EVENT_2,
    EVENT_3,
    EVENT_4,
    SCHEDULE_1,
    SCHEDULE_2,
    _context,
    _correct,
    _create,
    _event,
    _fails,
    _record,
    _run,
    _schedule,
    _snapshot,
)

LATER = "2026-09-27T09:00:00+00:00"


def _conflict(
    operation: Any, snapshot: MaintenanceSnapshot, request: Any, **kwargs: Any
) -> None:
    _fails(
        MaintenanceConflictError,
        operation,
        snapshot,
        request,
        code="maintenance_replay_conflict",
        **kwargs,
    )


# Business key


def test_event_business_key_equality_rules() -> None:
    """Links as a set, Runtime numerically; timestamps and void state excluded."""
    base = _event(EVENT_1, "2026-09-01", "3600", (SCHEDULE_1, SCHEDULE_2))
    same = _event(
        EVENT_2,
        "2026-09-01",
        "3600.0",
        (SCHEDULE_2, SCHEDULE_1),
        recorded_at=LATER,
        voided_at=LATER,
        void_reason="x",
    )
    same["schedule_uuids"] = [SCHEDULE_2, SCHEDULE_1]
    assert event_business_key(base) == event_business_key(same)
    for field, value in (
        ("asset_uuid", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"),
        ("schedule_uuids", [SCHEDULE_1]),
        ("title", "Other"),
        ("performed_date", "2026-09-02"),
        ("runtime_seconds", "3600.1"),
        ("runtime_seconds", None),
        ("notes", "note"),
        ("corrects_event_uuid", EVENT_3),
    ):
        assert event_business_key(base) != event_business_key({**base, field: value})


# Record Event replay


def _recorded(**values: Any) -> MaintenanceSnapshot:
    snapshot = _snapshot(_schedule(), _schedule(SCHEDULE_2))
    return _run(record_event, snapshot, _record(**values)).snapshot


def test_record_lost_ack_replays() -> None:
    request = _record(runtime_seconds="3600", notes="Note")
    stored = _run(record_event, _snapshot(_schedule()), request).snapshot
    result = _run(record_event, stored, request)
    assert result.outcome is MutationOutcome.REPLAY
    assert result.snapshot is stored


@pytest.mark.parametrize(
    "values",
    [
        {"runtime_seconds": "3600.0"},
        {"runtime_seconds": 3600},
        {"title": "  Filter change  "},
        {"notes": "  Note "},
        {"performed_date": date(2026, 9, 26)},
    ],
)
def test_record_replay_uses_canonical_equality(values: dict[str, Any]) -> None:
    stored = _recorded(runtime_seconds="3600", notes="Note")
    request = _record(**{"runtime_seconds": "3600", "notes": "Note", **values})
    assert _run(record_event, stored, request).outcome is MutationOutcome.REPLAY


def test_record_replay_compares_link_membership_not_order() -> None:
    stored = _recorded(schedule_uuids=[SCHEDULE_2, SCHEDULE_1])
    request = _record(schedule_uuids=(SCHEDULE_1, SCHEDULE_2))
    assert _run(record_event, stored, request).outcome is MutationOutcome.REPLAY


def test_record_replay_precedes_state_checks() -> None:
    """A retry replays even when today, Runtime, or confirmation would now refuse it."""
    stored = _run(
        record_event,
        _snapshot(
            _schedule(initial_anchor={"date": "2026-01-01", "runtime_seconds": None})
        ),
        _record(
            runtime_seconds="5000", confirmed_baseline_locks=frozenset({SCHEDULE_1})
        ),
    ).snapshot
    retry = _record(runtime_seconds="5000")
    context = _context("1", today=date(2026, 1, 1), observed=EARLIER)
    assert _run(record_event, stored, retry, context).outcome is MutationOutcome.REPLAY


@pytest.mark.parametrize(
    "values",
    [
        {"title": "Other"},
        {"runtime_seconds": "3601"},
        {"runtime_seconds": None},
        {"performed_date": "2026-09-25"},
        {"schedule_uuids": [SCHEDULE_1, SCHEDULE_2]},
        {"schedule_uuids": []},
        {"notes": "Changed"},
        {"asset_uuid": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"},
    ],
)
def test_record_same_uuid_different_content_conflicts(values: dict[str, Any]) -> None:
    stored = _recorded(runtime_seconds="3600")
    _conflict(record_event, stored, _record(**{"runtime_seconds": "3600", **values}))


def test_record_retry_after_later_void_conflicts() -> None:
    stored = _recorded()
    voided = _run(void_event, stored, VoidEventRequest(EVENT_1)).snapshot
    _conflict(record_event, voided, _record())


def test_record_retry_after_later_correction_conflicts() -> None:
    stored = _recorded(performed_date="2026-09-01")
    corrected = _run(correct_event, stored, _correct()).snapshot
    _conflict(record_event, corrected, _record(performed_date="2026-09-01"))
    # The corrected Event's UUID is not an ordinary Record Event either.
    _conflict(
        record_event,
        corrected,
        _record(event_uuid=EVENT_2, performed_date="2026-09-01"),
    )


# Void replay


@pytest.mark.parametrize(
    ("stored_reason", "retry_reason"),
    [(None, None), (None, "   "), ("Duplicate", "  Duplicate  ")],
)
def test_void_replay(stored_reason: str | None, retry_reason: str | None) -> None:
    """Void timestamps are never compared; only state and reason are."""
    stored = _recorded()
    voided = _run(void_event, stored, VoidEventRequest(EVENT_1, stored_reason)).snapshot
    result = _run(
        void_event,
        voided,
        VoidEventRequest(EVENT_1, retry_reason),
        _context(observed=LATER),
    )
    assert result.outcome is MutationOutcome.REPLAY
    assert result.snapshot is voided


@pytest.mark.parametrize(("first", "second"), [("A", "B"), ("A", None), (None, "B")])
def test_void_different_reason_conflicts(first: str | None, second: str | None) -> None:
    """A second, different void is refused; void is final."""
    voided = _run(void_event, _recorded(), VoidEventRequest(EVENT_1, first)).snapshot
    _conflict(void_event, voided, VoidEventRequest(EVENT_1, second))


def test_correction_void_is_not_a_manual_void_replay() -> None:
    stored = _recorded(performed_date="2026-09-01")
    corrected = _run(correct_event, stored, _correct(void_reason="Typo")).snapshot
    for reason in ("Typo", None):
        _conflict(void_event, corrected, VoidEventRequest(EVENT_1, reason))


# Correction replay


def _corrected(**values: Any) -> MaintenanceSnapshot:
    snapshot = _snapshot(
        _schedule(),
        _schedule(SCHEDULE_2),
        events=(_event(EVENT_1, "2026-09-01", "3600"),),
    )
    return _run(correct_event, snapshot, _correct(**values)).snapshot


def test_correction_lost_ack_replays_before_active_target_check() -> None:
    """The target is already voided, yet the exact retry is a REPLAY."""
    request = _correct(
        schedule_uuids=[SCHEDULE_2],
        runtime_seconds="3600",
        notes="n",
        void_reason="Wrong schedule",
    )
    stored = _corrected(
        schedule_uuids=[SCHEDULE_2],
        runtime_seconds="3600",
        notes="n",
        void_reason="Wrong schedule",
    )
    assert stored.events[EVENT_1]["voided_at"] is not None
    context = _context("1", today=date(2020, 1, 1), observed=LATER)
    result = _run(correct_event, stored, request, context)
    assert result.outcome is MutationOutcome.REPLAY
    assert result.snapshot is stored


def test_correction_replay_with_preserved_runtime_spelling() -> None:
    stored = _corrected(runtime_seconds="3600.0")
    assert stored.events[EVENT_2]["runtime_seconds"] == "3600"
    for runtime in ("3600.0", "3600", 3600):
        request = _correct(runtime_seconds=runtime)
        assert _run(correct_event, stored, request).outcome is MutationOutcome.REPLAY


@pytest.mark.parametrize(
    "values",
    [
        {"title": "Other"},
        {"runtime_seconds": "3601"},
        {"performed_date": "2026-08-31"},
        {"schedule_uuids": [SCHEDULE_2]},
        {"notes": "Different"},
        {"void_reason": "Different reason"},
    ],
)
def test_correction_same_uuid_different_content_conflicts(
    values: dict[str, Any],
) -> None:
    stored = _corrected(runtime_seconds="3600")
    _conflict(correct_event, stored, _correct(**{"runtime_seconds": "3600", **values}))


def test_correction_replay_requires_the_same_target() -> None:
    snapshot = _snapshot(
        _schedule(),
        events=(_event(EVENT_1, "2026-09-01"), _event(EVENT_3, "2026-09-01")),
    )
    stored = _run(correct_event, snapshot, _correct()).snapshot
    _conflict(correct_event, stored, _correct(target_event_uuid=EVENT_3))
    # A target that no longer exists cannot prove the replay either.
    _conflict(
        correct_event,
        stored,
        _correct(target_event_uuid="cccccccc-cccc-4ccc-8ccc-ccccccccccc8"),
    )


def test_correction_retry_after_corrected_event_voided_conflicts() -> None:
    stored = _corrected(runtime_seconds="3600")
    voided = _run(void_event, stored, VoidEventRequest(EVENT_2)).snapshot
    _conflict(correct_event, voided, _correct(runtime_seconds="3600"))


def test_correction_retry_after_corrected_event_corrected_again_conflicts() -> None:
    stored = _corrected(runtime_seconds="3600")
    again = _run(
        correct_event,
        stored,
        _correct(new_event_uuid=EVENT_3, target_event_uuid=EVENT_2, title="Third"),
    ).snapshot
    _conflict(correct_event, again, _correct(runtime_seconds="3600"))


def test_correction_uuid_colliding_with_other_events_conflicts() -> None:
    snapshot = _snapshot(
        _schedule(),
        events=(_event(EVENT_1, "2026-09-01"), _event(EVENT_4, "2026-09-01")),
    )
    # An unrelated ordinary Event already uses the new UUID.
    _conflict(correct_event, snapshot, _correct(new_event_uuid=EVENT_4))
    # The new UUID equals the target itself.
    _conflict(correct_event, snapshot, _correct(new_event_uuid=EVENT_1))


# Schedule replay


def _full_create() -> CreateScheduleRequest:
    return _create(
        runtime_interval_seconds="100",
        initial_anchor=InitialAnchorInput("2026-09-26", "5000"),
    )


def test_create_schedule_lost_ack_replays_without_state_checks() -> None:
    stored = _run(create_schedule, _snapshot(), _full_create()).snapshot
    context = _context("1", today=date(2020, 1, 1))
    result = _run(create_schedule, stored, _full_create(), context)
    assert result.outcome is MutationOutcome.REPLAY
    assert result.snapshot is stored


def test_create_schedule_replay_conflicts() -> None:
    stored = _run(create_schedule, _snapshot(), _create()).snapshot
    _conflict(create_schedule, stored, _create(name="Other"))
    _conflict(
        create_schedule,
        stored,
        _create(asset_uuid="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"),
    )
    _conflict(create_schedule, stored, _create(enabled=False))
    edited = _run(
        edit_schedule,
        stored,
        EditScheduleRequest(
            SCHEDULE_1, "Renamed", True, CalendarIntervalInput(6, "months"), None, None
        ),
    ).snapshot
    _conflict(create_schedule, edited, _create())


def test_schedule_delete_has_no_unprovable_replay() -> None:
    stored = _run(create_schedule, _snapshot(), _create()).snapshot
    deleted = _run(delete_schedule, stored, DeleteScheduleRequest(SCHEDULE_1)).snapshot
    _fails(
        MaintenanceNotFoundError,
        delete_schedule,
        deleted,
        DeleteScheduleRequest(SCHEDULE_1),
    )
    # Re-creating with the same UUID is an ordinary new create, not a replay.
    recreated = _run(create_schedule, deleted, _create())
    assert recreated.outcome is MutationOutcome.CHANGED


def test_replay_never_rewrites_timestamps() -> None:
    stored = _recorded()
    assert stored.events[EVENT_1]["recorded_at"] != LATER
    result = _run(record_event, stored, _record(), _context(observed=LATER))
    assert (
        result.snapshot.events[EVENT_1]["recorded_at"]
        == stored.events[EVENT_1]["recorded_at"]
    )
