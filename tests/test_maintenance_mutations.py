"""Snapshot-level Maintenance mutations: rules, guards, and atomicity."""

from __future__ import annotations

import ast
from collections.abc import Callable
from copy import deepcopy
from dataclasses import fields
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from custom_components.device_lifecycle import maintenance_mutations
from custom_components.device_lifecycle.canonical import CanonicalValueError
from custom_components.device_lifecycle.maintenance import (
    is_baseline_locked,
    validate_maintenance_collections,
)
from custom_components.device_lifecycle.maintenance_mutations import (
    AddIntervalRequest,
    CalendarIntervalInput,
    ConfirmationKind,
    CorrectEventRequest,
    CreateScheduleRequest,
    DeleteScheduleRequest,
    EditScheduleRequest,
    EventGuards,
    InitialAnchorInput,
    IntervalDimension,
    MaintenanceConfirmationRequiredError,
    MaintenanceConflictError,
    MaintenanceMutationContext,
    MaintenanceMutationError,
    MaintenanceMutationResult,
    MaintenanceNotFoundError,
    MaintenanceRequestError,
    MaintenanceSnapshot,
    MutationOutcome,
    PreparationReminderInput,
    RecordEventRequest,
    RemoveIntervalRequest,
    SetEnabledRequest,
    SetInitialAnchorRequest,
    VoidEventRequest,
    add_interval,
    correct_event,
    create_schedule,
    delete_schedule,
    edit_schedule,
    mutate_maintenance,
    preflight_event_guards,
    record_event,
    remove_interval,
    set_enabled,
    set_initial_anchor,
    void_event,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    AssetStoreError,
    _validate_store_data,
)

ASSET_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
ASSET_B = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"
MISSING_ASSET = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa9"
SCHEDULE_1 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
SCHEDULE_2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
SCHEDULE_3 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3"
SCHEDULE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb4"
MISSING_SCHEDULE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb9"
EVENT_1 = "cccccccc-cccc-4ccc-8ccc-ccccccccccc1"
EVENT_2 = "cccccccc-cccc-4ccc-8ccc-ccccccccccc2"
EVENT_3 = "cccccccc-cccc-4ccc-8ccc-ccccccccccc3"
EVENT_4 = "cccccccc-cccc-4ccc-8ccc-ccccccccccc4"
MISSING_EVENT = "cccccccc-cccc-4ccc-8ccc-ccccccccccc9"

TODAY = date(2026, 9, 26)
RECORDED = "2026-09-20T08:00:00+00:00"
OBSERVED = "2026-09-26T12:00:00.123456+00:00"
EARLIER = "2020-01-01T00:00:00+00:00"

MODULE_PATH = Path(maintenance_mutations.__file__)


def _assets() -> dict[str, Any]:
    """Asset records with a Runtime total that Maintenance must never write."""
    return {
        ASSET_A: {"asset_uuid": ASSET_A, "runtime": {"total_seconds": "5000"}},
        ASSET_B: {"asset_uuid": ASSET_B, "runtime": {"total_seconds": "9000"}},
    }


def _context(
    current_runtime: str | None = "5000",
    *,
    today: date = TODAY,
    observed: str = OBSERVED,
) -> MaintenanceMutationContext:
    return MaintenanceMutationContext(today, observed, current_runtime)


def _schedule(key: str = SCHEDULE_1, owner: str = ASSET_A, /, **values: Any) -> dict:
    record: dict[str, Any] = {
        "schedule_uuid": key,
        "asset_uuid": owner,
        "name": "Filter change",
        "enabled": True,
        "calendar_interval": {"value": 6, "unit": "months"},
        "runtime_interval_seconds": None,
        "initial_anchor": None,
        "preparation_reminder": None,
    }
    record.update(values)
    return record


def _event(
    key: str,
    performed_date: str = "2026-09-01",
    runtime_seconds: str | None = None,
    links: tuple[str, ...] = (SCHEDULE_1,),
    /,
    **values: Any,
) -> dict:
    record: dict[str, Any] = {
        "event_uuid": key,
        "asset_uuid": ASSET_A,
        "schedule_uuids": sorted(links),
        "title": "Filter change",
        "performed_date": performed_date,
        "runtime_seconds": runtime_seconds,
        "recorded_at": RECORDED,
        "notes": None,
        "voided_at": None,
        "void_reason": None,
        "corrects_event_uuid": None,
    }
    record.update(values)
    return record


def _snapshot(*schedules: dict, events: tuple[dict, ...] = ()) -> MaintenanceSnapshot:
    """Return a valid detached snapshot."""
    snapshot = MaintenanceSnapshot(
        _assets(),
        {record["schedule_uuid"]: record for record in schedules},
        {record["event_uuid"]: record for record in events},
    )
    validate_maintenance_collections(
        snapshot.assets, snapshot.schedules, snapshot.events
    )
    return snapshot


def _run(
    operation: Callable[..., MaintenanceMutationResult],
    snapshot: MaintenanceSnapshot,
    request: Any,
    context: MaintenanceMutationContext | None = None,
) -> MaintenanceMutationResult:
    """Run a successful operation and prove the input and Assets are untouched."""
    before = deepcopy(snapshot)
    result = operation(snapshot, request, context or _context())
    assert snapshot == before
    assert result.snapshot.assets == _assets()
    if result.changed:
        assert result.snapshot is not snapshot
        validate_maintenance_collections(
            result.snapshot.assets, result.snapshot.schedules, result.snapshot.events
        )
    else:
        assert result.snapshot is snapshot
    return result


def _fails[E: MaintenanceMutationError](
    error: type[E],
    operation: Callable[..., MaintenanceMutationResult],
    snapshot: MaintenanceSnapshot,
    request: Any,
    context: MaintenanceMutationContext | None = None,
    *,
    code: str | None = None,
) -> E:
    """Run a refused operation and prove nothing changed."""
    before = deepcopy(snapshot)
    with pytest.raises(error) as info:
        operation(snapshot, request, context or _context())
    assert snapshot == before
    if code is not None:
        assert info.value.code == code
    return info.value


def _create(**values: Any) -> CreateScheduleRequest:
    request: dict[str, Any] = {
        "schedule_uuid": SCHEDULE_1,
        "asset_uuid": ASSET_A,
        "name": "Filter change",
        "calendar_interval": CalendarIntervalInput(6, "months"),
    }
    request.update(values)
    return CreateScheduleRequest(**request)


def _record(**values: Any) -> RecordEventRequest:
    request: dict[str, Any] = {
        "event_uuid": EVENT_1,
        "asset_uuid": ASSET_A,
        "schedule_uuids": [SCHEDULE_1],
        "title": "Filter change",
        "performed_date": "2026-09-26",
    }
    request.update(values)
    return RecordEventRequest(**request)


def _correct(**values: Any) -> CorrectEventRequest:
    request: dict[str, Any] = {
        "new_event_uuid": EVENT_2,
        "target_event_uuid": EVENT_1,
        "schedule_uuids": [SCHEDULE_1],
        "title": "Filter change",
        "performed_date": "2026-09-01",
    }
    request.update(values)
    return CorrectEventRequest(**request)


# Context and results


def test_context_requires_canonical_values() -> None:
    """today is a civil date; observed time and Runtime are canonical."""
    with pytest.raises(TypeError):
        MaintenanceMutationContext(
            datetime(2026, 9, 26, 12, tzinfo=UTC), OBSERVED, None
        )  # type: ignore[arg-type]
    for observed in ("2026-09-26T12:00:00Z", "2026-09-26T12:00:00", "now"):
        with pytest.raises(CanonicalValueError):
            MaintenanceMutationContext(TODAY, observed, None)
    for runtime in ("1e3", "-1", "01", "NaN"):
        with pytest.raises(CanonicalValueError):
            MaintenanceMutationContext(TODAY, OBSERVED, runtime)


def test_outcomes_are_distinct() -> None:
    """Replay and ordinary no-op are different outcomes; errors carry theirs."""
    assert {outcome.value for outcome in MutationOutcome} == {
        "changed",
        "no_op",
        "replay",
        "conflict",
        "invalid",
        "confirmation_required",
        "not_found",
    }
    assert MaintenanceRequestError.outcome is MutationOutcome.INVALID
    assert MaintenanceConflictError.outcome is MutationOutcome.CONFLICT
    assert MaintenanceNotFoundError.outcome is MutationOutcome.NOT_FOUND
    assert (
        MaintenanceConfirmationRequiredError.outcome
        is MutationOutcome.CONFIRMATION_REQUIRED
    )


def test_dispatch_and_unknown_request() -> None:
    snapshot = _snapshot()
    result = mutate_maintenance(snapshot, _create(), _context())
    assert result.outcome is MutationOutcome.CHANGED
    with pytest.raises(TypeError, match="Unknown Maintenance request"):
        mutate_maintenance(snapshot, object(), _context())


# Create Schedule


def test_create_calendar_schedule() -> None:
    result = _run(create_schedule, _snapshot(), _create(name="  Filter change  "))
    assert result.outcome is MutationOutcome.CHANGED
    assert result.snapshot.schedules == {SCHEDULE_1: _schedule()}


def test_create_runtime_schedule_normalizes_decimals() -> None:
    """Runtime input goes through Decimal(str(x)) and format(..., "f")."""
    request = _create(
        calendar_interval=None,
        runtime_interval_seconds=1.1,
        initial_anchor=InitialAnchorInput(runtime_seconds="3.6E+3"),
    )
    record = _run(create_schedule, _snapshot(), request).snapshot.schedules[SCHEDULE_1]
    assert record["calendar_interval"] is None
    assert record["runtime_interval_seconds"] == "1.1"
    assert record["initial_anchor"] == {"date": None, "runtime_seconds": "3600"}


def test_create_combined_schedule_with_baseline_and_reminder() -> None:
    request = _create(
        enabled=False,
        runtime_interval_seconds="1800000",
        initial_anchor=InitialAnchorInput(date(2026, 6, 1), "3600.0"),
        preparation_reminder=PreparationReminderInput(30, "  Order filters "),
    )
    record = _run(create_schedule, _snapshot(), request).snapshot.schedules[SCHEDULE_1]
    assert record == _schedule(
        enabled=False,
        runtime_interval_seconds="1800000",
        initial_anchor={"date": "2026-06-01", "runtime_seconds": "3600.0"},
        preparation_reminder={"lead_days": 30, "message": "Order filters"},
    )


def test_create_blank_reminder_message_and_empty_anchor_become_null() -> None:
    request = _create(
        initial_anchor=InitialAnchorInput(None, None),
        preparation_reminder=PreparationReminderInput(7, "   "),
    )
    record = _run(create_schedule, _snapshot(), request).snapshot.schedules[SCHEDULE_1]
    assert record["initial_anchor"] is None
    assert record["preparation_reminder"] == {"lead_days": 7, "message": None}


def test_create_keeps_other_schedules() -> None:
    snapshot = _snapshot(_schedule(SCHEDULE_2))
    result = _run(create_schedule, snapshot, _create())
    assert set(result.snapshot.schedules) == {SCHEDULE_1, SCHEDULE_2}
    assert result.snapshot.schedules[SCHEDULE_2] == snapshot.schedules[SCHEDULE_2]


def test_create_missing_asset() -> None:
    _fails(
        MaintenanceNotFoundError,
        create_schedule,
        _snapshot(),
        _create(asset_uuid=MISSING_ASSET),
        code="maintenance_asset_not_found",
    )


def test_create_baseline_date_rules() -> None:
    """A future baseline date is rejected against the injected today."""
    tomorrow = _create(initial_anchor=InitialAnchorInput("2026-09-27"))
    _fails(
        MaintenanceRequestError,
        create_schedule,
        _snapshot(),
        tomorrow,
        code="maintenance_date_in_future",
    )
    # The same request is valid when the caller's today is later.
    _run(create_schedule, _snapshot(), tomorrow, _context(today=date(2026, 9, 27)))
    _run(
        create_schedule, _snapshot(), _create(initial_anchor=InitialAnchorInput(TODAY))
    )


@pytest.mark.parametrize(
    ("current", "anchor_runtime", "accepted"),
    [
        ("5000", "5000", True),
        ("5000", "5000.0", True),
        ("5000", "4999.9", True),
        ("5000", "5000.1", False),
        (None, "999999999", True),
    ],
)
def test_create_baseline_runtime_sanity(
    current: str | None, anchor_runtime: str, accepted: bool
) -> None:
    """M-9: never above a known current Runtime; unknown means no comparison."""
    request = _create(
        runtime_interval_seconds="100",
        initial_anchor=InitialAnchorInput(runtime_seconds=anchor_runtime),
    )
    if accepted:
        _run(create_schedule, _snapshot(), request, _context(current))
    else:
        _fails(
            MaintenanceRequestError,
            create_schedule,
            _snapshot(),
            request,
            _context(current),
            code="maintenance_runtime_above_current",
        )


@pytest.mark.parametrize(
    ("values", "code"),
    [
        ({"calendar_interval": None}, "maintenance_interval_required"),
        (
            {
                "calendar_interval": None,
                "runtime_interval_seconds": "10",
                "preparation_reminder": PreparationReminderInput(5),
            },
            "maintenance_reminder_requires_calendar",
        ),
        (
            {"initial_anchor": InitialAnchorInput(runtime_seconds="10")},
            "maintenance_anchor_requires_interval",
        ),
        (
            {
                "calendar_interval": None,
                "runtime_interval_seconds": "10",
                "initial_anchor": InitialAnchorInput("2026-01-01"),
            },
            "maintenance_anchor_requires_interval",
        ),
        ({"name": "   "}, "maintenance_invalid_request"),
        ({"name": None}, "maintenance_invalid_request"),
        ({"enabled": 1}, "maintenance_invalid_request"),
        (
            {"calendar_interval": CalendarIntervalInput(True, "days")},
            "maintenance_invalid_request",
        ),
        (
            {"calendar_interval": CalendarIntervalInput(0, "days")},
            "maintenance_invalid_request",
        ),
        (
            {"calendar_interval": CalendarIntervalInput(1, "weeks")},
            "maintenance_invalid_request",
        ),
        (
            {"calendar_interval": {"value": 1, "unit": "days"}},
            "maintenance_invalid_request",
        ),
        ({"runtime_interval_seconds": "0"}, "maintenance_invalid_request"),
        ({"runtime_interval_seconds": -1}, "maintenance_invalid_request"),
        ({"runtime_interval_seconds": "NaN"}, "maintenance_invalid_request"),
        ({"runtime_interval_seconds": True}, "maintenance_invalid_request"),
        (
            {"preparation_reminder": PreparationReminderInput(0)},
            "maintenance_invalid_request",
        ),
        ({"preparation_reminder": {"lead_days": 1}}, "maintenance_invalid_request"),
        ({"initial_anchor": {"date": "2026-01-01"}}, "maintenance_invalid_request"),
        (
            {"initial_anchor": InitialAnchorInput("2026-02-30")},
            "maintenance_invalid_request",
        ),
        (
            {"initial_anchor": InitialAnchorInput(datetime(2026, 1, 1, tzinfo=UTC))},
            "maintenance_invalid_request",
        ),
        ({"schedule_uuid": SCHEDULE_1.upper()}, "maintenance_invalid_request"),
        ({"asset_uuid": "not-a-uuid"}, "maintenance_invalid_request"),
    ],
)
def test_create_rejects_invalid_requests(values: dict[str, Any], code: str) -> None:
    _fails(
        MaintenanceRequestError,
        create_schedule,
        _snapshot(),
        _create(**values),
        code=code,
    )


# Edit Schedule configuration


def _edit(**values: Any) -> EditScheduleRequest:
    request: dict[str, Any] = {
        "schedule_uuid": SCHEDULE_1,
        "name": "Filter change",
        "enabled": True,
        "calendar_interval": CalendarIntervalInput(6, "months"),
        "runtime_interval_seconds": None,
        "preparation_reminder": None,
    }
    request.update(values)
    return EditScheduleRequest(**request)


def test_edit_configuration_keeps_baseline_and_history() -> None:
    """Configuration stays editable after history exists; anchors are untouched."""
    schedule = _schedule(
        runtime_interval_seconds="100",
        initial_anchor={"date": "2026-01-01", "runtime_seconds": "10"},
    )
    snapshot = _snapshot(schedule, events=(_event(EVENT_1),))
    request = _edit(
        name=" Filter swap ",
        enabled=False,
        calendar_interval=CalendarIntervalInput(1, "years"),
        runtime_interval_seconds="200",
        preparation_reminder=PreparationReminderInput(14, None),
    )
    result = _run(edit_schedule, snapshot, request)
    assert result.snapshot.schedules[SCHEDULE_1] == _schedule(
        name="Filter swap",
        enabled=False,
        calendar_interval={"value": 1, "unit": "years"},
        runtime_interval_seconds="200",
        initial_anchor={"date": "2026-01-01", "runtime_seconds": "10"},
        preparation_reminder={"lead_days": 14, "message": None},
    )
    assert result.snapshot.events == snapshot.events


def test_edit_same_value_is_no_op() -> None:
    """Numerically equal Runtime keeps the persisted spelling and writes nothing."""
    snapshot = _snapshot(_schedule(runtime_interval_seconds="3600"))
    result = _run(edit_schedule, snapshot, _edit(runtime_interval_seconds="3600.0"))
    assert result.outcome is MutationOutcome.NO_OP
    assert _run(
        edit_schedule, snapshot, _edit(runtime_interval_seconds=3600)
    ).outcome is (MutationOutcome.NO_OP)


def test_edit_removes_reminder() -> None:
    snapshot = _snapshot(
        _schedule(preparation_reminder={"lead_days": 3, "message": None})
    )
    result = _run(edit_schedule, snapshot, _edit())
    assert result.snapshot.schedules[SCHEDULE_1]["preparation_reminder"] is None


@pytest.mark.parametrize(
    "values",
    [
        {"calendar_interval": None, "runtime_interval_seconds": "10"},
        {"runtime_interval_seconds": "10"},
    ],
)
def test_edit_cannot_add_or_remove_interval_types(values: dict[str, Any]) -> None:
    _fails(
        MaintenanceRequestError,
        edit_schedule,
        _snapshot(_schedule()),
        _edit(**values),
        code="maintenance_interval_type_change",
    )


def test_edit_rejects_reminder_without_calendar_and_missing_schedule() -> None:
    runtime_only = _schedule(calendar_interval=None, runtime_interval_seconds="10")
    _fails(
        MaintenanceRequestError,
        edit_schedule,
        _snapshot(runtime_only),
        _edit(
            calendar_interval=None,
            runtime_interval_seconds="10",
            preparation_reminder=PreparationReminderInput(1),
        ),
        code="maintenance_reminder_requires_calendar",
    )
    _fails(
        MaintenanceNotFoundError,
        edit_schedule,
        _snapshot(),
        _edit(),
        code="maintenance_schedule_not_found",
    )


# Baseline edit and lock


def _anchor_request(
    anchor_date: Any = None, runtime: Any = None
) -> SetInitialAnchorRequest:
    anchor = (
        None
        if anchor_date is None and runtime is None
        else InitialAnchorInput(anchor_date, runtime)
    )
    return SetInitialAnchorRequest(SCHEDULE_1, anchor)


def test_baseline_edit_while_unlocked() -> None:
    snapshot = _snapshot(_schedule(runtime_interval_seconds="100"))
    result = _run(set_initial_anchor, snapshot, _anchor_request("2026-09-01", "10"))
    assert result.snapshot.schedules[SCHEDULE_1]["initial_anchor"] == {
        "date": "2026-09-01",
        "runtime_seconds": "10",
    }
    changed = _run(set_initial_anchor, result.snapshot, _anchor_request("2026-08-01"))
    assert changed.snapshot.schedules[SCHEDULE_1]["initial_anchor"] == {
        "date": "2026-08-01",
        "runtime_seconds": None,
    }
    cleared = _run(set_initial_anchor, changed.snapshot, _anchor_request())
    assert cleared.snapshot.schedules[SCHEDULE_1]["initial_anchor"] is None


def test_baseline_same_value_is_no_op() -> None:
    schedule = _schedule(
        runtime_interval_seconds="100",
        initial_anchor={"date": "2026-09-01", "runtime_seconds": "10"},
    )
    for snapshot in (
        _snapshot(schedule),
        _snapshot(schedule, events=(_event(EVENT_1),)),
    ):
        result = _run(
            set_initial_anchor, snapshot, _anchor_request(date(2026, 9, 1), "10.0")
        )
        assert result.outcome is MutationOutcome.NO_OP
    assert _run(
        set_initial_anchor, _snapshot(_schedule()), _anchor_request()
    ).outcome is (MutationOutcome.NO_OP)


@pytest.mark.parametrize(
    "events",
    [
        # First historical reference.
        (_event(EVENT_1),),
        # A voided Event still locks.
        (_event(EVENT_1, voided_at=RECORDED),),
        # Correction history: the corrected Event no longer links SCHEDULE_1,
        # but the voided target still does.
        (
            _event(EVENT_1, voided_at=RECORDED),
            _event(EVENT_2, "2026-09-01", None, (), corrects_event_uuid=EVENT_1),
        ),
    ],
)
def test_baseline_locked_by_any_historical_reference(events: tuple[dict, ...]) -> None:
    snapshot = _snapshot(
        _schedule(initial_anchor={"date": "2026-01-01", "runtime_seconds": None}),
        events=events,
    )
    for request in (_anchor_request("2026-02-01"), _anchor_request()):
        _fails(
            MaintenanceRequestError,
            set_initial_anchor,
            snapshot,
            request,
            code="maintenance_baseline_locked",
        )


def test_baseline_edit_rules() -> None:
    snapshot = _snapshot(_schedule(runtime_interval_seconds="100"))
    _fails(
        MaintenanceRequestError,
        set_initial_anchor,
        snapshot,
        _anchor_request("2026-09-27"),
        code="maintenance_date_in_future",
    )
    _fails(
        MaintenanceRequestError,
        set_initial_anchor,
        snapshot,
        _anchor_request(None, "5001"),
        code="maintenance_runtime_above_current",
    )
    _fails(
        MaintenanceRequestError,
        set_initial_anchor,
        _snapshot(_schedule()),
        _anchor_request(None, "1"),
        code="maintenance_anchor_requires_interval",
    )
    _fails(
        MaintenanceNotFoundError,
        set_initial_anchor,
        _snapshot(),
        _anchor_request("2026-01-01"),
    )


# Add interval type


def _add_runtime(value: Any = "100", component: Any = None) -> AddIntervalRequest:
    return AddIntervalRequest(
        SCHEDULE_1,
        IntervalDimension.RUNTIME,
        runtime_interval_seconds=value,
        anchor_component=component,
    )


def _add_calendar(component: Any = None, value: int = 6) -> AddIntervalRequest:
    return AddIntervalRequest(
        SCHEDULE_1,
        IntervalDimension.CALENDAR,
        calendar_interval=CalendarIntervalInput(value, "months"),
        anchor_component=component,
    )


def test_add_dimension_unlocked_with_anchor_component() -> None:
    snapshot = _snapshot(
        _schedule(initial_anchor={"date": "2026-01-01", "runtime_seconds": None})
    )
    result = _run(add_interval, snapshot, _add_runtime("100", 42))
    assert result.snapshot.schedules[SCHEDULE_1] == _schedule(
        runtime_interval_seconds="100",
        initial_anchor={"date": "2026-01-01", "runtime_seconds": "42"},
    )


def test_add_dimension_creates_baseline_when_unlocked() -> None:
    runtime_only = _schedule(calendar_interval=None, runtime_interval_seconds="10")
    result = _run(
        add_interval, _snapshot(runtime_only), _add_calendar(date(2026, 9, 26))
    )
    assert result.snapshot.schedules[SCHEDULE_1]["initial_anchor"] == {
        "date": "2026-09-26",
        "runtime_seconds": None,
    }


def test_add_dimension_locked_without_anchor_is_allowed() -> None:
    snapshot = _snapshot(
        _schedule(initial_anchor={"date": "2026-01-01", "runtime_seconds": None}),
        events=(_event(EVENT_1),),
    )
    result = _run(add_interval, snapshot, _add_runtime())
    record = result.snapshot.schedules[SCHEDULE_1]
    assert record["runtime_interval_seconds"] == "100"
    assert record["initial_anchor"] == {"date": "2026-01-01", "runtime_seconds": None}
    assert result.snapshot.events == snapshot.events


def test_add_dimension_locked_rejects_explicit_anchor() -> None:
    snapshot = _snapshot(_schedule(), events=(_event(EVENT_1, voided_at=RECORDED),))
    _fails(
        MaintenanceRequestError,
        add_interval,
        snapshot,
        _add_runtime("100", "10"),
        code="maintenance_baseline_locked",
    )


def test_add_existing_dimension() -> None:
    snapshot = _snapshot(
        _schedule(
            runtime_interval_seconds="100",
            initial_anchor={"date": "2026-01-01", "runtime_seconds": "10"},
        )
    )
    for request in (
        _add_runtime("100.0"),
        _add_runtime("100", "10.0"),
        _add_calendar(),
        _add_calendar("2026-01-01"),
    ):
        assert _run(add_interval, snapshot, request).outcome is MutationOutcome.NO_OP
    for request in (
        _add_runtime("200"),
        _add_runtime("100", "11"),
        _add_calendar(value=7),
        _add_calendar("2026-01-02"),
    ):
        _fails(
            MaintenanceRequestError,
            add_interval,
            snapshot,
            request,
            code="maintenance_interval_exists",
        )
    without_anchor = _snapshot(_schedule(runtime_interval_seconds="100"))
    _fails(
        MaintenanceRequestError,
        add_interval,
        without_anchor,
        _add_runtime("100", "1"),
        code="maintenance_interval_exists",
    )


def test_add_dimension_anchor_rules() -> None:
    runtime_only = _snapshot(
        _schedule(calendar_interval=None, runtime_interval_seconds="10")
    )
    _fails(
        MaintenanceRequestError,
        add_interval,
        runtime_only,
        _add_calendar("2026-09-27"),
        code="maintenance_date_in_future",
    )
    _fails(
        MaintenanceRequestError,
        add_interval,
        _snapshot(_schedule()),
        _add_runtime("100", "5000.5"),
        code="maintenance_runtime_above_current",
    )
    _run(
        add_interval,
        _snapshot(_schedule()),
        _add_runtime("100", "5000.5"),
        _context(None),
    )


@pytest.mark.parametrize(
    "request_value",
    [
        AddIntervalRequest(SCHEDULE_1, IntervalDimension.RUNTIME),
        AddIntervalRequest(SCHEDULE_1, IntervalDimension.CALENDAR),
        AddIntervalRequest(
            SCHEDULE_1,
            IntervalDimension.RUNTIME,
            calendar_interval=CalendarIntervalInput(1, "days"),
            runtime_interval_seconds="1",
        ),
        AddIntervalRequest(
            SCHEDULE_1,
            IntervalDimension.CALENDAR,
            calendar_interval=CalendarIntervalInput(1, "days"),
            runtime_interval_seconds="1",
        ),
        AddIntervalRequest(SCHEDULE_1, "weekly", runtime_interval_seconds="1"),  # type: ignore[arg-type]
    ],
)
def test_add_interval_rejects_malformed_requests(
    request_value: AddIntervalRequest,
) -> None:
    _fails(
        MaintenanceRequestError,
        add_interval,
        _snapshot(_schedule(calendar_interval=None, runtime_interval_seconds="10")),
        request_value,
        code="maintenance_invalid_request",
    )


# Remove interval type


def _remove(
    dimension: IntervalDimension, *, confirm: Any = False
) -> RemoveIntervalRequest:
    return RemoveIntervalRequest(
        SCHEDULE_1, dimension, confirm_destroy_baseline=confirm
    )


def _combined(**values: Any) -> dict:
    values.setdefault("runtime_interval_seconds", "100")
    values.setdefault("initial_anchor", {"date": "2026-01-01", "runtime_seconds": "10"})
    values.setdefault("preparation_reminder", {"lead_days": 5, "message": "Order"})
    return _schedule(**values)


def test_remove_calendar_cleans_up_date_and_reminder() -> None:
    result = _run(
        remove_interval, _snapshot(_combined()), _remove(IntervalDimension.CALENDAR)
    )
    assert result.snapshot.schedules[SCHEDULE_1] == _schedule(
        calendar_interval=None,
        runtime_interval_seconds="100",
        initial_anchor={"date": None, "runtime_seconds": "10"},
        preparation_reminder=None,
    )


def test_remove_runtime_cleans_up_runtime_component() -> None:
    result = _run(
        remove_interval, _snapshot(_combined()), _remove(IntervalDimension.RUNTIME)
    )
    assert result.snapshot.schedules[SCHEDULE_1] == _schedule(
        initial_anchor={"date": "2026-01-01", "runtime_seconds": None},
        preparation_reminder={"lead_days": 5, "message": "Order"},
    )


def test_remove_collapses_empty_anchor_to_null() -> None:
    schedule = _combined(initial_anchor={"date": None, "runtime_seconds": "10"})
    result = _run(
        remove_interval, _snapshot(schedule), _remove(IntervalDimension.RUNTIME)
    )
    assert result.snapshot.schedules[SCHEDULE_1]["initial_anchor"] is None


def test_remove_last_interval_is_rejected() -> None:
    for schedule, dimension in (
        (_schedule(), IntervalDimension.CALENDAR),
        (
            _schedule(calendar_interval=None, runtime_interval_seconds="1"),
            IntervalDimension.RUNTIME,
        ),
    ):
        _fails(
            MaintenanceRequestError,
            remove_interval,
            _snapshot(schedule),
            _remove(dimension),
            code="maintenance_last_interval",
        )


def test_remove_unconfigured_dimension_is_no_op() -> None:
    result = _run(
        remove_interval, _snapshot(_schedule()), _remove(IntervalDimension.RUNTIME)
    )
    assert result.outcome is MutationOutcome.NO_OP


def test_remove_locked_component_requires_confirmation() -> None:
    """The destroy confirmation is mandatory, not persisted, and atomic."""
    snapshot = _snapshot(_combined(), events=(_event(EVENT_1, voided_at=RECORDED),))
    for dimension in IntervalDimension:
        error = _fails(
            MaintenanceConfirmationRequiredError,
            remove_interval,
            snapshot,
            _remove(dimension),
            code="maintenance_confirm_destroy_baseline",
        )
        assert error.confirmation is ConfirmationKind.DESTROY_BASELINE
        assert error.schedule_uuids == (SCHEDULE_1,)
        result = _run(remove_interval, snapshot, _remove(dimension, confirm=True))
        assert "confirm_destroy_baseline" not in result.snapshot.schedules[SCHEDULE_1]
    _fails(
        MaintenanceRequestError,
        remove_interval,
        snapshot,
        _remove(IntervalDimension.RUNTIME, confirm="yes"),
        code="maintenance_invalid_request",
    )


def test_remove_without_locked_component_needs_no_confirmation() -> None:
    # Unlocked Schedule with a component.
    _run(remove_interval, _snapshot(_combined()), _remove(IntervalDimension.RUNTIME))
    # Locked Schedule whose removed dimension has no component.
    snapshot = _snapshot(
        _combined(initial_anchor={"date": "2026-01-01", "runtime_seconds": None}),
        events=(_event(EVENT_1),),
    )
    _run(remove_interval, snapshot, _remove(IntervalDimension.RUNTIME))


def test_readding_a_removed_dimension_does_not_resurrect_its_component() -> None:
    snapshot = _snapshot(_combined(), events=(_event(EVENT_1),))
    removed = _run(
        remove_interval, snapshot, _remove(IntervalDimension.CALENDAR, confirm=True)
    ).snapshot
    readded = _run(add_interval, removed, _add_calendar()).snapshot
    record = readded.schedules[SCHEDULE_1]
    assert record["calendar_interval"] == {"value": 6, "unit": "months"}
    assert record["initial_anchor"] == {"date": None, "runtime_seconds": "10"}
    assert record["preparation_reminder"] is None
    _fails(
        MaintenanceRequestError,
        add_interval,
        removed,
        _add_calendar("2026-01-01"),
        code="maintenance_baseline_locked",
    )


# Enable and disable


def test_enable_disable_only_changes_enabled() -> None:
    schedule = _combined()
    snapshot = _snapshot(schedule, events=(_event(EVENT_1),))
    disabled = _run(
        set_enabled, snapshot, SetEnabledRequest(SCHEDULE_1, False)
    ).snapshot
    assert disabled.schedules[SCHEDULE_1] == {**schedule, "enabled": False}
    assert disabled.events == snapshot.events
    enabled = _run(set_enabled, disabled, SetEnabledRequest(SCHEDULE_1, True)).snapshot
    assert enabled.schedules == snapshot.schedules
    assert (
        _run(set_enabled, snapshot, SetEnabledRequest(SCHEDULE_1, True)).outcome
        is MutationOutcome.NO_OP
    )
    _fails(
        MaintenanceRequestError,
        set_enabled,
        snapshot,
        SetEnabledRequest(SCHEDULE_1, 0),  # type: ignore[arg-type]
    )
    _fails(
        MaintenanceNotFoundError,
        set_enabled,
        _snapshot(),
        SetEnabledRequest(SCHEDULE_1, True),
    )


# Hard delete


def test_delete_unused_schedule() -> None:
    snapshot = _snapshot(
        _schedule(),
        _schedule(SCHEDULE_2),
        events=(_event(EVENT_1, "2026-09-01", None, (SCHEDULE_2,)),),
    )
    result = _run(delete_schedule, snapshot, DeleteScheduleRequest(SCHEDULE_1))
    assert set(result.snapshot.schedules) == {SCHEDULE_2}
    assert result.snapshot.events == snapshot.events


@pytest.mark.parametrize("voided_at", [None, RECORDED])
def test_delete_blocked_by_any_reference(voided_at: str | None) -> None:
    snapshot = _snapshot(_schedule(), events=(_event(EVENT_1, voided_at=voided_at),))
    _fails(
        MaintenanceRequestError,
        delete_schedule,
        snapshot,
        DeleteScheduleRequest(SCHEDULE_1),
        code="maintenance_schedule_referenced",
    )


def test_delete_missing_fails_closed() -> None:
    """A repeated delete is not treated as a provable replay."""
    deleted = _run(
        delete_schedule, _snapshot(_schedule()), DeleteScheduleRequest(SCHEDULE_1)
    )
    _fails(
        MaintenanceNotFoundError,
        delete_schedule,
        deleted.snapshot,
        DeleteScheduleRequest(SCHEDULE_1),
        code="maintenance_schedule_not_found",
    )


# Record Event


def test_record_event() -> None:
    """Timestamps come from the context; caller cannot supply them."""
    request = _record(
        title="  Filter change ",
        runtime_seconds="4000",
        notes="  ",
    )
    result = _run(record_event, _snapshot(_schedule()), request)
    assert result.snapshot.events == {
        EVENT_1: _event(
            EVENT_1,
            "2026-09-26",
            "4000",
            recorded_at=OBSERVED,
        )
    }
    names = {item.name for item in fields(RecordEventRequest)}
    assert not names & {
        "recorded_at",
        "voided_at",
        "void_reason",
        "corrects_event_uuid",
    }


def test_record_ad_hoc_and_disabled_schedule() -> None:
    snapshot = _snapshot(_schedule(enabled=False))
    ad_hoc = _run(
        record_event, snapshot, _record(schedule_uuids=[], notes="Extra clean")
    )
    assert ad_hoc.snapshot.events[EVENT_1]["schedule_uuids"] == []
    assert ad_hoc.snapshot.events[EVENT_1]["notes"] == "Extra clean"
    assert ad_hoc.guards == EventGuards()
    disabled = _run(record_event, snapshot, _record())
    assert disabled.snapshot.events[EVENT_1]["schedule_uuids"] == [SCHEDULE_1]


def test_record_multi_schedule_link_order_is_deterministic() -> None:
    snapshot = _snapshot(_schedule(), _schedule(SCHEDULE_2), _schedule(SCHEDULE_3))
    for links in (
        [SCHEDULE_3, SCHEDULE_1, SCHEDULE_2],
        (SCHEDULE_2, SCHEDULE_3, SCHEDULE_1),
    ):
        result = _run(record_event, snapshot, _record(schedule_uuids=links))
        assert result.snapshot.events[EVENT_1]["schedule_uuids"] == [
            SCHEDULE_1,
            SCHEDULE_2,
            SCHEDULE_3,
        ]


@pytest.mark.parametrize(
    ("values", "error", "code"),
    [
        (
            {"asset_uuid": MISSING_ASSET},
            MaintenanceNotFoundError,
            "maintenance_asset_not_found",
        ),
        (
            {"schedule_uuids": [MISSING_SCHEDULE]},
            MaintenanceNotFoundError,
            "maintenance_schedule_not_found",
        ),
        (
            {"schedule_uuids": [SCHEDULE_B]},
            MaintenanceRequestError,
            "maintenance_schedule_other_asset",
        ),
        (
            {"schedule_uuids": [SCHEDULE_1, SCHEDULE_1]},
            MaintenanceRequestError,
            "maintenance_invalid_request",
        ),
        (
            {"schedule_uuids": SCHEDULE_1},
            MaintenanceRequestError,
            "maintenance_invalid_request",
        ),
        (
            {"schedule_uuids": [SCHEDULE_1.upper()]},
            MaintenanceRequestError,
            "maintenance_invalid_request",
        ),
        (
            {"performed_date": "2026-09-27"},
            MaintenanceRequestError,
            "maintenance_date_in_future",
        ),
        (
            {"performed_date": "26.9.2026"},
            MaintenanceRequestError,
            "maintenance_invalid_request",
        ),
        ({"title": " "}, MaintenanceRequestError, "maintenance_invalid_request"),
        ({"notes": 5}, MaintenanceRequestError, "maintenance_invalid_request"),
        (
            {"runtime_seconds": "-1"},
            MaintenanceRequestError,
            "maintenance_invalid_request",
        ),
        (
            {"runtime_seconds": "5000.000001"},
            MaintenanceRequestError,
            "maintenance_runtime_above_current",
        ),
        (
            {"confirmed_baseline_locks": SCHEDULE_1},
            MaintenanceRequestError,
            "maintenance_invalid_request",
        ),
        ({"event_uuid": "x"}, MaintenanceRequestError, "maintenance_invalid_request"),
    ],
)
def test_record_rejects(values: dict[str, Any], error: type, code: str) -> None:
    snapshot = _snapshot(_schedule(), _schedule(SCHEDULE_B, ASSET_B))
    _fails(error, record_event, snapshot, _record(**values), code=code)


@pytest.mark.parametrize(
    ("current", "runtime", "persisted"),
    [
        ("5000", None, None),
        ("5000", "5000.0", "5000.0"),
        ("5000", 4999, "4999"),
        ("5000", 1.1, "1.1"),
        (None, "123456789", "123456789"),
    ],
)
def test_record_runtime_sanity(
    current: str | None, runtime: Any, persisted: str | None
) -> None:
    """M-8 only with a known current Runtime; the Asset Runtime is never written."""
    result = _run(
        record_event,
        _snapshot(_schedule()),
        _record(runtime_seconds=runtime),
        _context(current),
    )
    assert result.snapshot.events[EVENT_1]["runtime_seconds"] == persisted


def test_record_date_uses_injected_today() -> None:
    request = _record(performed_date=date(2026, 9, 27))
    _fails(MaintenanceRequestError, record_event, _snapshot(_schedule()), request)
    _run(
        record_event, _snapshot(_schedule()), request, _context(today=date(2026, 9, 27))
    )


# First-Event baseline lock guard


def _with_baseline(key: str = SCHEDULE_1) -> dict:
    return _schedule(
        key, initial_anchor={"date": "2026-01-01", "runtime_seconds": None}
    )


def test_first_reference_to_baseline_requires_confirmation() -> None:
    snapshot = _snapshot(_with_baseline())
    error = _fails(
        MaintenanceConfirmationRequiredError,
        record_event,
        snapshot,
        _record(),
        code="maintenance_confirm_baseline_lock",
    )
    assert error.confirmation is ConfirmationKind.BASELINE_LOCK
    assert error.schedule_uuids == (SCHEDULE_1,)
    result = _run(
        record_event,
        snapshot,
        _record(confirmed_baseline_locks=frozenset({SCHEDULE_1})),
    )
    assert result.guards.newly_locked_baselines == (SCHEDULE_1,)
    assert is_baseline_locked(result.snapshot.events, SCHEDULE_1)
    assert set(result.snapshot.events[EVENT_1]) == set(_event(EVENT_1))


def test_no_baseline_or_already_locked_needs_no_confirmation() -> None:
    no_baseline = _run(record_event, _snapshot(_schedule()), _record())
    assert no_baseline.guards.newly_locked_baselines == ()
    locked = _snapshot(_with_baseline(), events=(_event(EVENT_2, voided_at=RECORDED),))
    repeated = _run(record_event, locked, _record())
    assert repeated.guards.newly_locked_baselines == ()


def test_multi_schedule_guard_names_only_newly_locked_baselines() -> None:
    snapshot = _snapshot(
        _with_baseline(SCHEDULE_1),
        _with_baseline(SCHEDULE_2),
        _schedule(SCHEDULE_3),
        events=(_event(EVENT_2, "2026-01-01", None, (SCHEDULE_2,)),),
    )
    request = _record(schedule_uuids=[SCHEDULE_3, SCHEDULE_2, SCHEDULE_1])
    error = _fails(
        MaintenanceConfirmationRequiredError, record_event, snapshot, request
    )
    assert error.schedule_uuids == (SCHEDULE_1,)
    assert preflight_event_guards(
        snapshot, [SCHEDULE_3, SCHEDULE_2, SCHEDULE_1], "2026-09-26"
    ).newly_locked_baselines == (SCHEDULE_1,)
    confirmed = _record(
        schedule_uuids=[SCHEDULE_3, SCHEDULE_2, SCHEDULE_1],
        confirmed_baseline_locks=frozenset({SCHEDULE_1}),
    )
    assert _run(record_event, snapshot, confirmed).guards.newly_locked_baselines == (
        SCHEDULE_1,
    )


def test_correction_adding_a_baseline_schedule_requires_confirmation() -> None:
    snapshot = _snapshot(
        _schedule(),
        _with_baseline(SCHEDULE_2),
        events=(_event(EVENT_1),),
    )
    request = _correct(schedule_uuids=[SCHEDULE_1, SCHEDULE_2])
    error = _fails(
        MaintenanceConfirmationRequiredError, correct_event, snapshot, request
    )
    assert error.schedule_uuids == (SCHEDULE_2,)
    result = _run(
        correct_event,
        snapshot,
        _correct(
            schedule_uuids=[SCHEDULE_1, SCHEDULE_2],
            confirmed_baseline_locks=frozenset({SCHEDULE_2}),
        ),
    )
    assert result.guards.newly_locked_baselines == (SCHEDULE_2,)


# Same-day Runtime ambiguity guard


def test_same_day_warning_for_latest_date() -> None:
    snapshot = _snapshot(
        _schedule(),
        _schedule(SCHEDULE_2),
        events=(_event(EVENT_2, "2026-09-26", "100"),),
    )
    request = _record(schedule_uuids=[SCHEDULE_1, SCHEDULE_2], runtime_seconds="100")
    assert preflight_event_guards(
        snapshot, [SCHEDULE_2, SCHEDULE_1], date(2026, 9, 26)
    ).same_day_runtime_ambiguity == (SCHEDULE_1,)
    result = _run(record_event, snapshot, request)
    assert result.guards.same_day_runtime_ambiguity == (SCHEDULE_1,)


@pytest.mark.parametrize(
    ("existing", "performed"),
    [
        # Backdated to a different date.
        ((_event(EVENT_2, "2026-09-26"),), "2026-09-01"),
        # Same date, but a newer Event keeps the latest group unambiguous.
        ((_event(EVENT_2, "2026-09-01"), _event(EVENT_3, "2026-09-20")), "2026-09-01"),
        # A voided Event on the same date is not relevant.
        ((_event(EVENT_2, "2026-09-26", voided_at=RECORDED),), "2026-09-26"),
        # Another Schedule's Event on the same date is unrelated.
        ((_event(EVENT_2, "2026-09-26", None, (SCHEDULE_2,)),), "2026-09-26"),
    ],
)
def test_no_same_day_warning(existing: tuple[dict, ...], performed: str) -> None:
    snapshot = _snapshot(_schedule(), _schedule(SCHEDULE_2), events=existing)
    result = _run(record_event, snapshot, _record(performed_date=performed))
    assert result.guards.same_day_runtime_ambiguity == ()
    assert (
        preflight_event_guards(
            snapshot, [SCHEDULE_1], performed
        ).same_day_runtime_ambiguity
        == ()
    )


def test_same_day_warning_for_correction_ignores_its_own_target() -> None:
    snapshot = _snapshot(
        _schedule(),
        events=(_event(EVENT_1, "2026-09-26"), _event(EVENT_3, "2026-09-26")),
    )
    result = _run(correct_event, snapshot, _correct(performed_date="2026-09-26"))
    assert result.guards.same_day_runtime_ambiguity == (SCHEDULE_1,)
    single = _snapshot(_schedule(), events=(_event(EVENT_1, "2026-09-26"),))
    alone = _run(correct_event, single, _correct(performed_date="2026-09-26"))
    assert alone.guards.same_day_runtime_ambiguity == ()
    assert (
        preflight_event_guards(
            single, [SCHEDULE_1], "2026-09-26", replacing_event_uuid=EVENT_1
        ).same_day_runtime_ambiguity
        == ()
    )


def test_preflight_validates_input() -> None:
    with pytest.raises(MaintenanceNotFoundError):
        preflight_event_guards(_snapshot(), [SCHEDULE_1], "2026-09-26")
    with pytest.raises(MaintenanceRequestError):
        preflight_event_guards(_snapshot(_schedule()), [SCHEDULE_1], "2026-9-26")


# Void Event


@pytest.mark.parametrize(
    ("reason", "persisted"),
    [(None, None), ("  Duplicate entry ", "Duplicate entry"), ("   ", None)],
)
def test_void_event(reason: str | None, persisted: str | None) -> None:
    """Void uses the observed time, even earlier than recorded_at, unclamped."""
    snapshot = _snapshot(_schedule(), events=(_event(EVENT_1),))
    result = _run(
        void_event,
        snapshot,
        VoidEventRequest(EVENT_1, reason),
        _context(observed=EARLIER),
    )
    assert result.snapshot.events[EVENT_1] == _event(
        EVENT_1, voided_at=EARLIER, void_reason=persisted
    )
    assert result.snapshot.schedules == snapshot.schedules


def test_void_missing_target() -> None:
    _fails(
        MaintenanceNotFoundError,
        void_event,
        _snapshot(),
        VoidEventRequest(MISSING_EVENT),
        code="maintenance_event_not_found",
    )


# Correct Event


def test_correct_event_is_one_atomic_change() -> None:
    snapshot = _snapshot(
        _schedule(),
        _schedule(SCHEDULE_2),
        events=(_event(EVENT_1, "2026-09-01", "100", notes="Old"),),
    )
    request = _correct(
        schedule_uuids=[SCHEDULE_2],
        title=" Filter swap ",
        performed_date="2026-09-02",
        runtime_seconds="200",
        notes=" ",
        void_reason=" Wrong date ",
    )
    result = _run(correct_event, snapshot, request, _context(observed=EARLIER))
    assert result.snapshot.events == {
        EVENT_1: _event(
            EVENT_1,
            "2026-09-01",
            "100",
            notes="Old",
            voided_at=EARLIER,
            void_reason="Wrong date",
        ),
        EVENT_2: _event(
            EVENT_2,
            "2026-09-02",
            "200",
            (SCHEDULE_2,),
            title="Filter swap",
            recorded_at=EARLIER,
            corrects_event_uuid=EVENT_1,
        ),
    }


def test_correct_keeps_target_asset() -> None:
    """There is no Asset in the request; links must stay on the target's Asset."""
    assert "asset_uuid" not in {item.name for item in fields(CorrectEventRequest)}
    snapshot = _snapshot(
        _schedule(), _schedule(SCHEDULE_B, ASSET_B), events=(_event(EVENT_1),)
    )
    _fails(
        MaintenanceRequestError,
        correct_event,
        snapshot,
        _correct(schedule_uuids=[SCHEDULE_B]),
        code="maintenance_schedule_other_asset",
    )
    ad_hoc = _run(correct_event, snapshot, _correct(schedule_uuids=[]))
    assert ad_hoc.snapshot.events[EVENT_2]["asset_uuid"] == ASSET_A


def test_correct_target_rules() -> None:
    snapshot = _snapshot(_schedule(), events=(_event(EVENT_1, voided_at=RECORDED),))
    _fails(
        MaintenanceRequestError,
        correct_event,
        snapshot,
        _correct(),
        code="maintenance_event_not_active",
    )
    _fails(
        MaintenanceNotFoundError,
        correct_event,
        snapshot,
        _correct(target_event_uuid=MISSING_EVENT),
        code="maintenance_event_not_found",
    )


def test_correct_future_date_leaves_target_active() -> None:
    snapshot = _snapshot(_schedule(), events=(_event(EVENT_1),))
    _fails(
        MaintenanceRequestError,
        correct_event,
        snapshot,
        _correct(performed_date="2026-09-27"),
        code="maintenance_date_in_future",
    )
    assert snapshot.events[EVENT_1]["voided_at"] is None


@pytest.mark.parametrize(
    ("old", "requested", "persisted"),
    [
        (None, None, None),
        ("3600", "3600.0", "3600"),
        ("3600.0", 3600, "3600.0"),
        ("3600", "3600.000", "3600"),
    ],
)
def test_correct_unchanged_runtime_skips_m8_and_keeps_string(
    old: str | None, requested: Any, persisted: str | None
) -> None:
    """Unchanged is derived from the target; a lower current Runtime is irrelevant."""
    snapshot = _snapshot(_schedule(), events=(_event(EVENT_1, "2026-09-01", old),))
    result = _run(
        correct_event,
        snapshot,
        _correct(runtime_seconds=requested, title="Renamed"),
        _context("1"),
    )
    assert result.snapshot.events[EVENT_2]["runtime_seconds"] == persisted


@pytest.mark.parametrize(
    ("old", "requested", "current", "accepted", "persisted"),
    [
        ("3600", "3601", "5000", True, "3601"),
        ("3600", "5000.1", "5000", False, None),
        (None, "5001", "5000", False, None),
        (None, "4000", "5000", True, "4000"),
        ("3600", None, "1", True, None),
        ("3600", "9999999", None, True, "9999999"),
    ],
)
def test_correct_changed_runtime_runs_m8(
    old: str | None,
    requested: str | None,
    current: str | None,
    accepted: bool,
    persisted: str | None,
) -> None:
    snapshot = _snapshot(_schedule(), events=(_event(EVENT_1, "2026-09-01", old),))
    request = _correct(runtime_seconds=requested)
    if accepted:
        result = _run(correct_event, snapshot, request, _context(current))
        assert result.snapshot.events[EVENT_2]["runtime_seconds"] == persisted
    else:
        _fails(
            MaintenanceRequestError,
            correct_event,
            snapshot,
            request,
            _context(current),
            code="maintenance_runtime_above_current",
        )


def test_correct_request_has_no_trusted_runtime_flag() -> None:
    names = {item.name for item in fields(CorrectEventRequest)}
    assert not any("unchanged" in name for name in names)
    with pytest.raises(TypeError):
        CorrectEventRequest(  # type: ignore[call-arg]
            EVENT_2, EVENT_1, [], "x", "2026-09-01", runtime_unchanged=True
        )


def test_correction_keeps_historical_locks() -> None:
    """Removed links stay locked through the voided target; added links lock."""
    snapshot = _snapshot(
        _with_baseline(SCHEDULE_1),
        _schedule(SCHEDULE_2),
        events=(_event(EVENT_1),),
    )
    corrected = _run(
        correct_event, snapshot, _correct(schedule_uuids=[SCHEDULE_2])
    ).snapshot
    assert is_baseline_locked(corrected.events, SCHEDULE_1)
    assert is_baseline_locked(corrected.events, SCHEDULE_2)
    _fails(
        MaintenanceRequestError,
        set_initial_anchor,
        corrected,
        _anchor_request("2026-02-01"),
        code="maintenance_baseline_locked",
    )
    for schedule_uuid in (SCHEDULE_1, SCHEDULE_2):
        _fails(
            MaintenanceRequestError,
            delete_schedule,
            corrected,
            DeleteScheduleRequest(schedule_uuid),
            code="maintenance_schedule_referenced",
        )
    # Voiding the corrected Event too still unlocks nothing.
    voided = _run(void_event, corrected, VoidEventRequest(EVENT_2)).snapshot
    assert is_baseline_locked(voided.events, SCHEDULE_1)
    assert is_baseline_locked(voided.events, SCHEDULE_2)


# Failure atomicity


def test_failed_operations_leave_the_snapshot_unchanged() -> None:
    """Every refusal leaves the input equal; _fails asserts it with deepcopy."""
    snapshot = _snapshot(
        _combined(),
        _schedule(SCHEDULE_2),
        events=(_event(EVENT_1, "2026-09-01", "100"),),
    )
    before = deepcopy(snapshot)
    # Correction whose corrected Event is invalid: the target is not voided.
    _fails(MaintenanceRequestError, correct_event, snapshot, _correct(title=""))
    _fails(
        MaintenanceRequestError,
        correct_event,
        snapshot,
        _correct(runtime_seconds="6000"),
    )
    # Interval cleanup that needs confirmation is not partially applied.
    _fails(
        MaintenanceConfirmationRequiredError,
        remove_interval,
        snapshot,
        _remove(IntervalDimension.CALENDAR),
    )
    # A failed Record Event does not touch the Event collection.
    _fails(
        MaintenanceRequestError,
        record_event,
        snapshot,
        _record(event_uuid=EVENT_2, performed_date="2027-01-01"),
    )
    # A Create Schedule conflict leaves the existing record unchanged.
    _fails(MaintenanceConflictError, create_schedule, snapshot, _create(name="Other"))
    assert snapshot == before
    assert snapshot.events[EVENT_1]["voided_at"] is None
    assert snapshot.schedules[SCHEDULE_1]["preparation_reminder"] is not None


def test_changed_result_does_not_alias_changed_records() -> None:
    """A changed record is a new dict; mutating the result leaves the input alone."""
    snapshot = _snapshot(_schedule(), events=(_event(EVENT_1),))
    result = _run(void_event, snapshot, VoidEventRequest(EVENT_1, "x"))
    assert result.snapshot.events[EVENT_1] is not snapshot.events[EVENT_1]
    assert result.snapshot.events is not snapshot.events
    result.snapshot.events[EVENT_1]["title"] = "mutated"  # type: ignore[index]
    assert snapshot.events[EVENT_1]["title"] == "Filter change"


# Production boundary


def _tree() -> ast.Module:
    return ast.parse(MODULE_PATH.read_text(encoding="utf-8"))


def test_module_is_pure_and_time_is_injected() -> None:
    """No Home Assistant, clock, disk, lock, or Runtime writer dependency."""
    tree = _tree()
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add("." * node.level + (node.module or ""))
    assert imports == {
        "__future__",
        "collections.abc",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "typing",
        ".canonical",
        ".maintenance",
        ".maintenance_projection",
        ".models",
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not names & {"dt_util", "hass", "utc_now_iso", "open", "Lock"}
    # context.today is read; date.today() / datetime.now() are never called.
    assert not called & {"now", "today", "utcnow", "time"}
    assert not attributes & {
        "async_save",
        "async_commit_runtime_delta",
        "async_request_runtime_checkpoint",
    }
    assert not any(
        isinstance(node, (ast.AsyncFunctionDef, ast.Await)) for node in ast.walk(tree)
    )


def test_module_never_touches_asset_runtime() -> None:
    """Maintenance reads Runtime only from the context and never writes Assets."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert "total_seconds" not in source
    assert "snapshot.assets[" not in source


def test_mutations_are_not_wired_into_production() -> None:
    package = MODULE_PATH.parent
    for path in package.glob("*.py"):
        if path == MODULE_PATH:
            continue
        assert "maintenance_mutations" not in path.read_text(encoding="utf-8"), (
            path.name
        )


@pytest.mark.parametrize("key", ["maintenance_schedules", "maintenance_events"])
def test_store_3_1_still_rejects_maintenance_collections(
    asset_store_data: AssetStoreData, key: str
) -> None:
    data: dict[str, Any] = deepcopy(asset_store_data)
    data[key] = {}
    with pytest.raises(AssetStoreError, match="invalid top-level shape"):
        _validate_store_data(data)  # type: ignore[arg-type]
