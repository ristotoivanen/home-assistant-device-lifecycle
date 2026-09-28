"""Archived-Asset rules of the pure Maintenance domain (WP5).

Frozen in docs/maintenance-store-v4-schema.md (Archived Assets) and
docs/asset-archive-store-v4.md: an archived Asset has no active projection,
its current Maintenance management is blocked after replay, and Void and
Correct Event stay available. The Archive state is only ``archived_at``.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import date
from typing import Any

import pytest

from custom_components.device_lifecycle.maintenance import (
    validate_maintenance_collections,
)
from custom_components.device_lifecycle.maintenance_mutations import (
    AddIntervalRequest,
    CalendarIntervalInput,
    DeleteScheduleRequest,
    EditScheduleRequest,
    InitialAnchorInput,
    IntervalDimension,
    MaintenanceArchivedAssetError,
    MaintenanceMutationContext,
    MaintenanceMutationError,
    MaintenanceNotFoundError,
    MaintenanceSnapshot,
    MutationOutcome,
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
    record_event,
    remove_interval,
    set_enabled,
    set_initial_anchor,
    void_event,
)
from custom_components.device_lifecycle.maintenance_projection import (
    DueState,
    InactiveReason,
    MaintenanceProjection,
    PreparationState,
    project_schedule,
)

from .test_maintenance_mutations import (
    ASSET_A,
    ASSET_B,
    EVENT_1,
    EVENT_2,
    MISSING_ASSET,
    OBSERVED,
    SCHEDULE_1,
    _assets,
    _correct,
    _create,
    _event,
    _record,
    _schedule,
)

ARCHIVED_AT = "2026-09-20T10:00:00+00:00"
TODAY = date(2026, 9, 26)


def _archived_assets() -> dict[str, Any]:
    assets = _assets()
    assets[ASSET_A]["archived_at"] = ARCHIVED_AT
    return assets


def _snapshot(
    *schedules: dict, events: tuple[dict, ...] = (), archived: bool = True
) -> MaintenanceSnapshot:
    snapshot = MaintenanceSnapshot(
        _archived_assets() if archived else _assets(),
        {record["schedule_uuid"]: record for record in schedules},
        {record["event_uuid"]: record for record in events},
    )
    validate_maintenance_collections(
        snapshot.assets, snapshot.schedules, snapshot.events
    )
    return snapshot


def _restored(snapshot: MaintenanceSnapshot) -> MaintenanceSnapshot:
    """The same persisted Maintenance data after Restore: only the Asset changes."""
    assets = deepcopy(dict(snapshot.assets))
    assets[ASSET_A]["archived_at"] = None
    return MaintenanceSnapshot(assets, snapshot.schedules, snapshot.events)


def _context() -> MaintenanceMutationContext:
    return MaintenanceMutationContext(TODAY, OBSERVED, "5000")


def _blocked(
    operation: Callable[..., Any], snapshot: MaintenanceSnapshot, request: Any
) -> MaintenanceArchivedAssetError:
    before = deepcopy(snapshot)
    with pytest.raises(MaintenanceArchivedAssetError) as info:
        operation(snapshot, request, _context())
    assert info.value.code == "maintenance_asset_archived"
    assert ASSET_A in str(info.value)
    assert "runtime" not in str(info.value)
    assert snapshot == before
    return info.value


def _succeeds(
    operation: Callable[..., Any], snapshot: MaintenanceSnapshot, request: Any
):
    before = deepcopy(snapshot)
    result = operation(snapshot, request, _context())
    assert snapshot == before
    # Maintenance never changes an Asset, including its Archive state.
    assert result.snapshot.assets == snapshot.assets
    return result


# Projection


def _project(
    schedule: dict, events: dict | None = None, *, archived: bool, today: date = TODAY
) -> MaintenanceProjection:
    return project_schedule(
        schedule,
        events or {},
        today=today,
        current_runtime="5000",
        asset_archived=archived,
    )


def _overdue_schedule() -> tuple[dict, dict]:
    """Last maintenance 2026-01-01 every 6 months: overdue since 2026-07-01."""
    schedule = _schedule()
    events = {EVENT_1: _event(EVENT_1, "2026-01-01")}
    validate_maintenance_collections(_assets(), {SCHEDULE_1: schedule}, events)
    return schedule, events


@pytest.mark.parametrize("enabled", [True, False])
def test_archived_asset_has_no_active_projection(enabled: bool) -> None:
    schedule, events = _overdue_schedule()
    schedule["enabled"] = enabled
    before = deepcopy((schedule, events))

    projection = _project(schedule, events, archived=True)

    assert projection.active is False
    assert projection.inactive_reason is InactiveReason.ASSET_ARCHIVED
    assert projection.calendar is None
    assert projection.runtime is None
    assert projection.combined_state is None
    assert projection.preparation is None
    # The effective anchor is still derived from unchanged history.
    assert projection.anchor.calendar == date(2026, 1, 1)
    # Nothing is written, including ``enabled``.
    assert (schedule, events) == before


def test_restore_resumes_overdue_projection_immediately() -> None:
    schedule, events = _overdue_schedule()
    archived = _project(schedule, events, archived=True)
    restored = _project(schedule, events, archived=False)

    assert archived.combined_state is None
    assert restored.active is True
    assert restored.inactive_reason is None
    assert restored.combined_state is DueState.OVERDUE
    assert restored.calendar is not None
    assert restored.calendar.due == date(2026, 7, 1)
    assert restored.anchor == archived.anchor


def test_restore_resumes_preparation_reminder() -> None:
    schedule = _schedule(preparation_reminder={"lead_days": 30, "message": None})
    events = {EVENT_1: _event(EVENT_1, "2026-04-10")}
    validate_maintenance_collections(_assets(), {SCHEDULE_1: schedule}, events)

    archived = _project(schedule, events, archived=True)
    restored = _project(schedule, events, archived=False)

    assert archived.preparation is None
    assert restored.calendar is not None
    assert restored.calendar.due == date(2026, 10, 10)
    assert restored.combined_state is DueState.OK
    assert restored.preparation is PreparationState.ACTIVE


def test_disabled_schedule_of_active_asset_reports_disabled() -> None:
    schedule, events = _overdue_schedule()
    schedule["enabled"] = False
    projection = _project(schedule, events, archived=False)
    assert projection.active is False
    assert projection.inactive_reason is InactiveReason.DISABLED
    assert projection.combined_state is None
    assert projection.preparation is None


def test_inactive_reasons_are_not_due_states() -> None:
    assert {reason.value for reason in InactiveReason} == {
        "disabled",
        "asset_archived",
    }
    assert not {reason.value for reason in InactiveReason} & {
        state.value for state in DueState
    }


def test_asset_archived_is_required_and_strict() -> None:
    schedule, events = _overdue_schedule()
    with pytest.raises(TypeError):
        project_schedule(  # type: ignore[call-arg]
            schedule, events, today=TODAY, current_runtime="5000"
        )
    for value in (None, 0, 1, "false"):
        with pytest.raises(TypeError, match="asset_archived must be a bool"):
            project_schedule(
                schedule,
                events,
                today=TODAY,
                current_runtime="5000",
                asset_archived=value,  # type: ignore[arg-type]
            )


def test_archived_projection_still_validates_runtime_input() -> None:
    """Archive suppresses only the active projection, not input checking."""
    schedule, events = _overdue_schedule()
    with pytest.raises(ValueError):
        project_schedule(
            schedule, events, today=TODAY, current_runtime="1e3", asset_archived=True
        )


# Blocked current-management operations


def test_create_schedule_is_blocked() -> None:
    _blocked(create_schedule, _snapshot(), _create())


def test_create_schedule_replay_precedes_archive_guard() -> None:
    created = create_schedule(_snapshot(archived=False), _create(), _context())
    archived = MaintenanceSnapshot(
        _archived_assets(), created.snapshot.schedules, created.snapshot.events
    )
    result = _succeeds(create_schedule, archived, _create())
    assert result.outcome is MutationOutcome.REPLAY


def test_create_schedule_conflict_is_still_reported() -> None:
    """A different request with an existing identity is not a replay."""
    snapshot = _snapshot(_schedule())
    with pytest.raises(MaintenanceMutationError) as info:
        create_schedule(snapshot, _create(name="Other"), _context())
    assert info.value.code == "maintenance_replay_conflict"


@pytest.mark.parametrize(
    ("operation", "request_value"),
    [
        (
            edit_schedule,
            EditScheduleRequest(
                SCHEDULE_1,
                "Renamed",
                True,
                CalendarIntervalInput(6, "months"),
                None,
                None,
            ),
        ),
        (
            edit_schedule,
            EditScheduleRequest(
                SCHEDULE_1,
                "Filter change",
                True,
                CalendarIntervalInput(6, "months"),
                None,
                None,
            ),
        ),
        (
            set_initial_anchor,
            SetInitialAnchorRequest(SCHEDULE_1, InitialAnchorInput(date="2026-09-01")),
        ),
        (
            add_interval,
            AddIntervalRequest(
                SCHEDULE_1, IntervalDimension.RUNTIME, runtime_interval_seconds="3600"
            ),
        ),
        (set_enabled, SetEnabledRequest(SCHEDULE_1, False)),
        (set_enabled, SetEnabledRequest(SCHEDULE_1, True)),
        (delete_schedule, DeleteScheduleRequest(SCHEDULE_1)),
    ],
)
def test_schedule_management_is_blocked(operation: Any, request_value: Any) -> None:
    """Blocked even when the request would be a NO_OP on an active Asset."""
    _blocked(operation, _snapshot(_schedule()), request_value)


def test_remove_interval_is_blocked() -> None:
    schedule = _schedule(runtime_interval_seconds="3600")
    _blocked(
        remove_interval,
        _snapshot(schedule),
        RemoveIntervalRequest(SCHEDULE_1, IntervalDimension.RUNTIME),
    )


def test_schedule_management_of_other_active_asset_is_allowed() -> None:
    snapshot = _snapshot(_schedule(SCHEDULE_1, ASSET_B))
    result = _succeeds(set_enabled, snapshot, SetEnabledRequest(SCHEDULE_1, False))
    assert result.outcome is MutationOutcome.CHANGED


def test_record_event_is_blocked() -> None:
    _blocked(record_event, _snapshot(_schedule()), _record())


def test_ad_hoc_record_event_is_blocked() -> None:
    _blocked(record_event, _snapshot(), _record(schedule_uuids=[]))


def test_record_event_replay_precedes_archive_guard() -> None:
    recorded = record_event(
        _snapshot(_schedule(), archived=False), _record(), _context()
    )
    archived = MaintenanceSnapshot(
        _archived_assets(), recorded.snapshot.schedules, recorded.snapshot.events
    )
    result = _succeeds(record_event, archived, _record())
    assert result.outcome is MutationOutcome.REPLAY


def test_missing_asset_keeps_not_found() -> None:
    with pytest.raises(MaintenanceNotFoundError) as info:
        create_schedule(_snapshot(), _create(asset_uuid=MISSING_ASSET), _context())
    assert info.value.code == "maintenance_asset_not_found"


def test_asset_without_archived_at_fails_closed() -> None:
    """A missing field is a schema error, never read as active."""
    assets = _assets()
    del assets[ASSET_A]["archived_at"]
    snapshot = MaintenanceSnapshot(assets, {}, {})
    with pytest.raises(KeyError):
        create_schedule(snapshot, _create(), _context())


# Historical corrections stay available


def test_void_event_is_allowed() -> None:
    snapshot = _snapshot(_schedule(), events=(_event(EVENT_1, "2026-09-01"),))
    result = _succeeds(void_event, snapshot, VoidEventRequest(EVENT_1, "mistake"))
    assert result.outcome is MutationOutcome.CHANGED
    assert result.snapshot.events[EVENT_1]["voided_at"] == OBSERVED
    # The Schedule, including ``enabled``, is untouched.
    assert result.snapshot.schedules == snapshot.schedules


def test_void_event_replay_is_allowed() -> None:
    voided = _event(EVENT_1, "2026-09-01", voided_at=OBSERVED, void_reason="mistake")
    snapshot = _snapshot(_schedule(), events=(voided,))
    result = _succeeds(void_event, snapshot, VoidEventRequest(EVENT_1, "mistake"))
    assert result.outcome is MutationOutcome.REPLAY


def test_correct_event_is_allowed() -> None:
    snapshot = _snapshot(_schedule(), events=(_event(EVENT_1, "2026-09-01"),))
    result = _succeeds(correct_event, snapshot, _correct(performed_date="2026-08-31"))
    assert result.outcome is MutationOutcome.CHANGED
    corrected = result.snapshot.events[EVENT_2]
    assert corrected["corrects_event_uuid"] == EVENT_1
    assert corrected["asset_uuid"] == ASSET_A
    assert result.snapshot.events[EVENT_1]["voided_at"] == OBSERVED


def test_correct_event_replay_is_allowed() -> None:
    snapshot = _snapshot(_schedule(), events=(_event(EVENT_1, "2026-09-01"),))
    corrected = correct_event(snapshot, _correct(), _context()).snapshot
    result = _succeeds(correct_event, corrected, _correct())
    assert result.outcome is MutationOutcome.REPLAY


def test_correction_while_archived_keeps_projection_suppressed() -> None:
    schedule = _schedule()
    snapshot = _snapshot(schedule, events=(_event(EVENT_1, "2026-01-01"),))
    corrected = correct_event(
        snapshot, _correct(performed_date="2026-02-01"), _context()
    ).snapshot
    projection = _project(schedule, dict(corrected.events), archived=True)
    assert projection.inactive_reason is InactiveReason.ASSET_ARCHIVED
    assert projection.combined_state is None
    # Restore projects the corrected history.
    restored = _project(schedule, dict(_restored(corrected).events), archived=False)
    assert restored.anchor.calendar == date(2026, 2, 1)
    assert restored.combined_state is DueState.OVERDUE


def test_dispatcher_applies_the_same_rules() -> None:
    snapshot = _snapshot(_schedule(), events=(_event(EVENT_1, "2026-09-01"),))
    with pytest.raises(MaintenanceArchivedAssetError):
        mutate_maintenance(snapshot, SetEnabledRequest(SCHEDULE_1, False), _context())
    result = mutate_maintenance(snapshot, VoidEventRequest(EVENT_1), _context())
    assert result.outcome is MutationOutcome.CHANGED


def test_archived_error_is_an_invalid_mutation() -> None:
    assert issubclass(MaintenanceArchivedAssetError, MaintenanceMutationError)
    assert MaintenanceArchivedAssetError.outcome is MutationOutcome.INVALID


def test_restore_reenables_management_without_other_changes() -> None:
    snapshot = _snapshot(_schedule())
    _blocked(set_enabled, snapshot, SetEnabledRequest(SCHEDULE_1, False))
    result = set_enabled(
        _restored(snapshot), SetEnabledRequest(SCHEDULE_1, False), _context()
    )
    assert result.outcome is MutationOutcome.CHANGED
