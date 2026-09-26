"""Pure Maintenance projection: effective source, Runtime safety, due, preparation."""

from __future__ import annotations

import ast
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from custom_components.device_lifecycle import maintenance_projection
from custom_components.device_lifecycle.canonical import CanonicalValueError
from custom_components.device_lifecycle.maintenance import (
    validate_maintenance_collections,
)
from custom_components.device_lifecycle.maintenance_projection import (
    CalendarCondition,
    DueState,
    EffectiveAnchor,
    EffectiveSource,
    MaintenanceProjection,
    PreparationState,
    apply_runtime_safety,
    combine_states,
    effective_anchor,
    latest_event_group,
    preparation_state,
    project_schedule,
    relevant_events,
    runtime_condition,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    STORE_TOP_LEVEL_KEYS,
    AssetStoreError,
    _validate_store_data,
)

ASSET_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
SCHEDULE_1 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
SCHEDULE_2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
EVENT_1 = "cccccccc-cccc-4ccc-8ccc-ccccccccccc1"
EVENT_2 = "cccccccc-cccc-4ccc-8ccc-ccccccccccc2"
EVENT_3 = "cccccccc-cccc-4ccc-8ccc-ccccccccccc3"
EVENT_4 = "cccccccc-cccc-4ccc-8ccc-ccccccccccc4"
RECORDED = "2026-09-26T12:00:00+00:00"
VOIDED = "2026-09-26T13:00:00+00:00"
TODAY = date(2026, 9, 26)

MODULE_PATH = Path(maintenance_projection.__file__)


def _schedule(**fields: Any) -> dict:
    """Return a valid calendar-only Schedule without a baseline."""
    record: dict[str, Any] = {
        "schedule_uuid": SCHEDULE_1,
        "asset_uuid": ASSET_A,
        "name": "Ventilation filter change",
        "enabled": True,
        "calendar_interval": {"value": 6, "unit": "months"},
        "runtime_interval_seconds": None,
        "initial_anchor": None,
        "preparation_reminder": None,
    }
    record.update(fields)
    return record


def _event(
    key: str,
    performed_date: str,
    runtime_seconds: str | None = None,
    /,
    **fields: Any,
) -> dict:
    """Return a valid active Event linked to SCHEDULE_1."""
    record: dict[str, Any] = {
        "event_uuid": key,
        "asset_uuid": ASSET_A,
        "schedule_uuids": [SCHEDULE_1],
        "title": "Ventilation filter change",
        "performed_date": performed_date,
        "runtime_seconds": runtime_seconds,
        "recorded_at": RECORDED,
        "notes": None,
        "voided_at": None,
        "void_reason": None,
        "corrects_event_uuid": None,
    }
    record.update(fields)
    return record


def _voided(key: str, performed_date: str, runtime: str | None = None) -> dict:
    return _event(key, performed_date, runtime, voided_at=VOIDED)


def _events(*events: dict) -> dict[str, dict]:
    return {event["event_uuid"]: event for event in events}


def _anchor(date_value: str | None, runtime: str | None) -> dict:
    return {"date": date_value, "runtime_seconds": runtime}


def _both(**fields: Any) -> dict:
    """Return a Schedule with both a calendar and a Runtime interval."""
    fields.setdefault("runtime_interval_seconds", "1000")
    return _schedule(**fields)


def _runtime_only(**fields: Any) -> dict:
    fields.setdefault("runtime_interval_seconds", "1000")
    return _schedule(calendar_interval=None, **fields)


def _checked(schedule: dict, events: dict[str, dict]) -> tuple[dict, dict]:
    """Assert the fixture is a valid persisted Store shape, then return it."""
    schedules = {schedule["schedule_uuid"]: schedule}
    if SCHEDULE_2 not in schedules:
        schedules[SCHEDULE_2] = _schedule(schedule_uuid=SCHEDULE_2)
    validate_maintenance_collections({ASSET_A: {}}, schedules, events)
    return schedule, events


def _anchor_of(
    schedule: dict, *events: dict, current: str | None = None
) -> EffectiveAnchor:
    """Effective anchor after Runtime safety, through the full projection."""
    checked_schedule, checked_events = _checked(schedule, _events(*events))
    return project_schedule(
        checked_schedule, checked_events, today=TODAY, current_runtime=current
    ).anchor


def _project(
    schedule: dict,
    *events: dict,
    today: date = TODAY,
    current: str | None = None,
) -> MaintenanceProjection:
    checked_schedule, checked_events = _checked(schedule, _events(*events))
    return project_schedule(
        checked_schedule, checked_events, today=today, current_runtime=current
    )


def _source(
    source: EffectiveSource, day: str | None, runtime: str | None
) -> EffectiveAnchor:
    return EffectiveAnchor(
        source,
        None if day is None else date.fromisoformat(day),
        None if runtime is None else Decimal(runtime),
    )


# Relevant Events and the latest date group


def test_relevant_events_exclude_voided_and_unlinked() -> None:
    """Only non-voided Events that reference the Schedule are relevant."""
    linked = _event(EVENT_1, "2026-01-01")
    multi = _event(EVENT_2, "2026-02-01", schedule_uuids=[SCHEDULE_2, SCHEDULE_1])
    voided = _voided(EVENT_3, "2026-03-01")
    other = _event(EVENT_4, "2026-04-01", schedule_uuids=[SCHEDULE_2])
    events = _events(linked, multi, voided, other)
    assert relevant_events(events, SCHEDULE_1) == [linked, multi]
    assert relevant_events(events, SCHEDULE_2) == [multi, other]
    assert relevant_events({}, SCHEDULE_1) == []


def test_relevant_events_ignore_schedule_enabled_state() -> None:
    """A disabled Schedule keeps its history; relevance does not depend on it."""
    schedule = _schedule(enabled=False)
    events = _events(_event(EVENT_1, "2026-01-01", "100"))
    assert len(relevant_events(events, schedule["schedule_uuid"])) == 1
    anchor = _anchor_of(schedule, *events.values())
    assert anchor == _source(EffectiveSource.EVENT_GROUP, "2026-01-01", "100")


def test_latest_event_group() -> None:
    """The latest date and every Event on it, in any input order."""
    older = _event(EVENT_1, "2026-01-01")
    latest_a = _event(EVENT_2, "2026-03-01")
    latest_b = _event(EVENT_3, "2026-03-01")
    assert latest_event_group([]) is None
    assert latest_event_group([older]) == (date(2026, 1, 1), [older])
    for ordering in ([older, latest_a, latest_b], [latest_b, older, latest_a]):
        latest, group = latest_event_group(ordering)
        assert latest == date(2026, 3, 1)
        assert sorted(event["event_uuid"] for event in group) == [EVENT_2, EVENT_3]


# Effective source: no baseline


def test_no_baseline_and_no_events_is_unknown() -> None:
    """Nothing to anchor on: calendar and Runtime are UNKNOWN."""
    assert _anchor_of(_both()) == _source(EffectiveSource.UNKNOWN, None, None)


def test_no_baseline_single_event_wins() -> None:
    anchor = _anchor_of(_both(), _event(EVENT_1, "2026-05-01", "500"))
    assert anchor == _source(EffectiveSource.EVENT_GROUP, "2026-05-01", "500")


def test_no_baseline_latest_event_wins_over_older() -> None:
    anchor = _anchor_of(
        _both(),
        _event(EVENT_1, "2026-01-01", "100"),
        _event(EVENT_2, "2026-05-01", "500"),
    )
    assert anchor == _source(EffectiveSource.EVENT_GROUP, "2026-05-01", "500")


def test_event_with_unknown_runtime_is_not_filled_from_older_event() -> None:
    """Same source: a missing Runtime is never taken from an older Event."""
    anchor = _anchor_of(
        _both(),
        _event(EVENT_1, "2026-01-01", "100"),
        _event(EVENT_2, "2026-05-01", None),
    )
    assert anchor == _source(EffectiveSource.EVENT_GROUP, "2026-05-01", None)


def test_missing_runtime_is_not_filled_from_current_runtime() -> None:
    anchor = _anchor_of(_both(), _event(EVENT_1, "2026-05-01"), current="900")
    assert anchor == _source(EffectiveSource.EVENT_GROUP, "2026-05-01", None)


# Effective source: dated baseline


def test_dated_baseline_without_events_wins() -> None:
    schedule = _both(initial_anchor=_anchor("2026-03-01", "300"))
    anchor = _anchor_of(schedule)
    assert anchor == _source(EffectiveSource.INITIAL_ANCHOR, "2026-03-01", "300")


@pytest.mark.parametrize(
    ("performed", "expected"),
    [
        ("2026-04-01", _source(EffectiveSource.EVENT_GROUP, "2026-04-01", "400")),
        # Same date: the Event wins.
        ("2026-03-01", _source(EffectiveSource.EVENT_GROUP, "2026-03-01", "400")),
        # A backdated Event never moves the anchor backward.
        ("2026-02-01", _source(EffectiveSource.INITIAL_ANCHOR, "2026-03-01", "300")),
    ],
)
def test_dated_baseline_against_event(
    performed: str, expected: EffectiveAnchor
) -> None:
    """Chronological comparison of the latest Event date with the baseline date."""
    schedule = _both(initial_anchor=_anchor("2026-03-01", "300"))
    # The older Event's Runtime is kept below the baseline so Rule B is quiet.
    runtime = "400" if performed >= "2026-03-01" else "200"
    assert _anchor_of(schedule, _event(EVENT_1, performed, runtime)) == expected


def test_dated_baseline_without_runtime_is_not_filled_from_older_event() -> None:
    """The winning baseline's missing Runtime stays UNKNOWN."""
    schedule = _both(initial_anchor=_anchor("2026-03-01", None))
    anchor = _anchor_of(schedule, _event(EVENT_1, "2026-02-01", "200"))
    assert anchor == _source(EffectiveSource.INITIAL_ANCHOR, "2026-03-01", None)


def test_winning_event_without_runtime_is_not_filled_from_baseline() -> None:
    schedule = _both(initial_anchor=_anchor("2026-03-01", "300"))
    anchor = _anchor_of(schedule, _event(EVENT_1, "2026-04-01", None))
    assert anchor == _source(EffectiveSource.EVENT_GROUP, "2026-04-01", None)


def test_void_falls_back_to_the_baseline() -> None:
    """When every linked Event is voided the baseline is effective again."""
    schedule = _both(initial_anchor=_anchor("2026-03-01", "300"))
    anchor = _anchor_of(schedule, _voided(EVENT_1, "2026-04-01", "400"))
    assert anchor == _source(EffectiveSource.INITIAL_ANCHOR, "2026-03-01", "300")


def test_void_reselects_among_remaining_events() -> None:
    anchor = _anchor_of(
        _both(),
        _event(EVENT_1, "2026-01-01", "100"),
        _voided(EVENT_2, "2026-05-01", "500"),
    )
    assert anchor == _source(EffectiveSource.EVENT_GROUP, "2026-01-01", "100")


# Effective source: date-less Runtime baseline


def test_dateless_baseline_without_events_wins() -> None:
    schedule = _runtime_only(initial_anchor=_anchor(None, "1000"))
    anchor = _anchor_of(schedule)
    assert anchor == _source(EffectiveSource.INITIAL_ANCHOR, None, "1000")


@pytest.mark.parametrize(
    ("runtimes", "expected"),
    [
        # Case A: every latest Runtime is known and >= rB -> Event group.
        (("1500",), _source(EffectiveSource.EVENT_GROUP, "2026-05-01", "1500")),
        (("1000",), _source(EffectiveSource.EVENT_GROUP, "2026-05-01", "1000")),
        (("1000.0",), _source(EffectiveSource.EVENT_GROUP, "2026-05-01", "1000.0")),
        (("1500", "1200"), _source(EffectiveSource.EVENT_GROUP, "2026-05-01", None)),
        # Case B: every latest Runtime is known and < rB -> baseline.
        (("900",), _source(EffectiveSource.INITIAL_ANCHOR, None, "1000")),
        (("999.9", "10"), _source(EffectiveSource.INITIAL_ANCHOR, None, "1000")),
        # Case C: an unknown Runtime or a mix -> UNKNOWN / UNKNOWN.
        ((None,), _source(EffectiveSource.UNKNOWN, None, None)),
        (("1500", None), _source(EffectiveSource.UNKNOWN, None, None)),
        (("1500", "900"), _source(EffectiveSource.UNKNOWN, None, None)),
        (("900", None), _source(EffectiveSource.UNKNOWN, None, None)),
    ],
)
def test_dateless_baseline_against_latest_group(
    runtimes: tuple[str | None, ...], expected: EffectiveAnchor
) -> None:
    """Order against a date-less baseline is proven only by Runtime evidence."""
    schedule = _both(initial_anchor=_anchor(None, "1000"))
    keys = (EVENT_1, EVENT_2)
    events = [
        _event(key, "2026-05-01", runtime)
        for key, runtime in zip(keys, runtimes, strict=False)
    ]
    assert _anchor_of(schedule, *events, current="5000") == expected


def test_dateless_baseline_uses_only_the_latest_group() -> None:
    """Only the latest date group is compared with rB; older Events are not a mix."""
    schedule = _runtime_only(initial_anchor=_anchor(None, "1000"))
    anchor = _anchor_of(
        schedule,
        _event(EVENT_1, "2026-01-01", "900"),
        _event(EVENT_2, "2026-05-01", "1100"),
        current="5000",
    )
    assert anchor == _source(EffectiveSource.EVENT_GROUP, "2026-05-01", "1100")


def test_dateless_baseline_group_winner_still_checked_by_rule_b() -> None:
    """The Event group wins, then an older, greater Runtime contradicts it."""
    schedule = _runtime_only(initial_anchor=_anchor(None, "1000"))
    anchor = _anchor_of(
        schedule,
        _event(EVENT_1, "2026-01-01", "1200"),
        _event(EVENT_2, "2026-05-01", "1100"),
        current="5000",
    )
    assert anchor == _source(EffectiveSource.EVENT_GROUP, "2026-05-01", None)


# Same-day ambiguity


@pytest.mark.parametrize(
    ("first", "second"),
    [("500", "500"), ("500", "600"), ("500", None), (None, None)],
)
def test_same_day_group_has_unknown_runtime(
    first: str | None, second: str | None
) -> None:
    """No tie-breaker, even when the Runtime values are equal."""
    anchor = _anchor_of(
        _both(),
        _event(EVENT_1, "2026-05-01", first),
        _event(EVENT_2, "2026-05-01", second),
    )
    assert anchor == _source(EffectiveSource.EVENT_GROUP, "2026-05-01", None)


def test_same_day_group_ignores_recorded_at_uuid_and_order() -> None:
    """recorded_at, UUID, and mapping order never pick a winner."""
    first = _event(
        EVENT_1, "2026-05-01", "500", recorded_at="2026-05-01T08:00:00+00:00"
    )
    second = _event(
        EVENT_2, "2026-05-01", "600", recorded_at="2026-05-02T08:00:00+00:00"
    )
    schedule = _both()
    results = {
        project_schedule(schedule, events, today=TODAY, current_runtime="5000")
        for events in (
            {EVENT_1: first, EVENT_2: second},
            {EVENT_2: second, EVENT_1: first},
        )
    }
    assert len(results) == 1
    (result,) = results
    assert result.anchor == _source(EffectiveSource.EVENT_GROUP, "2026-05-01", None)
    assert result.runtime.state is DueState.UNKNOWN
    assert result.calendar.due == date(2026, 11, 1)


def test_same_day_with_baseline_on_that_date() -> None:
    schedule = _both(initial_anchor=_anchor("2026-05-01", "100"))
    anchor = _anchor_of(
        schedule,
        _event(EVENT_1, "2026-05-01", "500"),
        _event(EVENT_2, "2026-05-01", "600"),
    )
    assert anchor == _source(EffectiveSource.EVENT_GROUP, "2026-05-01", None)


# Interval type added to a locked Schedule (T-3)


def test_added_runtime_interval_takes_runtime_only_from_the_winner() -> None:
    """A dimension added later is anchored only by the winning source."""
    # The baseline was calendar-only; a Runtime interval was added later.
    schedule = _both(initial_anchor=_anchor("2026-03-01", None))
    projection = _project(
        schedule, _event(EVENT_1, "2026-01-01", "200"), current="5000"
    )
    assert projection.anchor == _source(
        EffectiveSource.INITIAL_ANCHOR, "2026-03-01", None
    )
    assert projection.runtime.state is DueState.UNKNOWN
    assert projection.runtime.due is None

    projection = _project(schedule, _event(EVENT_1, "2026-04-01", "700"), current="900")
    assert projection.anchor == _source(
        EffectiveSource.EVENT_GROUP, "2026-04-01", "700"
    )
    assert projection.runtime.due == Decimal(1700)
    assert projection.runtime.state is DueState.OK


def test_added_calendar_interval_after_dateless_baseline_is_unknown() -> None:
    schedule = _both(initial_anchor=_anchor(None, "1000"))
    projection = _project(schedule, current="1500")
    assert projection.calendar == CalendarCondition(None, DueState.UNKNOWN)
    assert projection.runtime.state is DueState.OK


# Runtime safety: Rule A


def test_rule_a_anchor_above_current_runtime_is_unknown() -> None:
    """The calendar component stays known."""
    anchor = _anchor_of(_both(), _event(EVENT_1, "2026-05-01", "1000"), current="999")
    assert anchor == _source(EffectiveSource.EVENT_GROUP, "2026-05-01", None)


@pytest.mark.parametrize("current", ["1000", "1000.0", "1000.5", None])
def test_rule_a_not_triggered(current: str | None) -> None:
    """Equal or greater current Runtime, or an unknown one, keeps the anchor."""
    anchor = _anchor_of(_both(), _event(EVENT_1, "2026-05-01", "1000"), current=current)
    assert anchor == _source(EffectiveSource.EVENT_GROUP, "2026-05-01", "1000")


def test_rule_a_applies_to_the_baseline() -> None:
    schedule = _both(initial_anchor=_anchor(None, "1000"))
    assert _anchor_of(schedule, current="999.9") == _source(
        EffectiveSource.INITIAL_ANCHOR, None, None
    )


# Runtime safety: Rule B


def test_rule_b_older_event_with_greater_runtime() -> None:
    """A dated winning Event contradicted by an older, greater Runtime."""
    anchor = _anchor_of(
        _both(),
        _event(EVENT_1, "2026-01-01", "1200"),
        _event(EVENT_2, "2026-05-01", "1000"),
        current="5000",
    )
    assert anchor == _source(EffectiveSource.EVENT_GROUP, "2026-05-01", None)


def test_rule_b_dated_baseline_contradicted_by_older_event() -> None:
    schedule = _both(initial_anchor=_anchor("2026-03-01", "1000"))
    anchor = _anchor_of(schedule, _event(EVENT_1, "2026-02-01", "1200"), current="5000")
    assert anchor == _source(EffectiveSource.INITIAL_ANCHOR, "2026-03-01", None)


def test_rule_b_event_on_the_baseline_date_is_the_winner() -> None:
    """An Event on the baseline date wins, so it cannot contradict the baseline."""
    schedule = _both(initial_anchor=_anchor("2026-03-01", "1200"))
    anchor = _anchor_of(schedule, _event(EVENT_1, "2026-03-01", "1000"), current="5000")
    assert anchor == _source(EffectiveSource.EVENT_GROUP, "2026-03-01", "1000")


@pytest.mark.parametrize("older_runtime", ["900", "1000", "1000.0", None])
def test_rule_b_not_triggered(older_runtime: str | None) -> None:
    """Smaller, equal, or unknown older Runtime values are not contradictions."""
    anchor = _anchor_of(
        _both(),
        _event(EVENT_1, "2026-01-01", older_runtime),
        _event(EVENT_2, "2026-05-01", "1000"),
        current="5000",
    )
    assert anchor == _source(EffectiveSource.EVENT_GROUP, "2026-05-01", "1000")


def test_rule_b_ignores_voided_and_unlinked_events() -> None:
    anchor = _anchor_of(
        _both(),
        _voided(EVENT_1, "2026-01-01", "9000"),
        _event(EVENT_2, "2026-01-01", "9000", schedule_uuids=[SCHEDULE_2]),
        _event(EVENT_3, "2026-05-01", "1000"),
        current="5000",
    )
    assert anchor == _source(EffectiveSource.EVENT_GROUP, "2026-05-01", "1000")


# Runtime safety: Rule B'


def test_rule_b_prime_example() -> None:
    """rB=1000, latest=900 (baseline wins), older=1200 -> Runtime UNKNOWN."""
    schedule = _runtime_only(initial_anchor=_anchor(None, "1000"))
    projection = _project(
        schedule,
        _event(EVENT_1, "2026-01-01", "1200"),
        _event(EVENT_2, "2026-05-01", "900"),
        current="5000",
    )
    assert projection.anchor == _source(EffectiveSource.INITIAL_ANCHOR, None, None)
    assert projection.runtime.state is DueState.UNKNOWN
    assert projection.combined_state is DueState.UNKNOWN


def test_rule_b_prime_ignores_other_schedules_and_voided_events() -> None:
    """A later Event for another Schedule and a voided contradiction are ignored."""
    schedule = _runtime_only(initial_anchor=_anchor(None, "1000"))
    anchor = _anchor_of(
        schedule,
        _event(EVENT_1, "2026-05-01", "900"),
        _event(EVENT_2, "2026-08-01", "4000", schedule_uuids=[SCHEDULE_2]),
        _voided(EVENT_3, "2026-01-01", "1200"),
        current="5000",
    )
    assert anchor == _source(EffectiveSource.INITIAL_ANCHOR, None, "1000")


def test_rule_b_prime_equal_runtime_is_not_a_contradiction() -> None:
    schedule = _runtime_only(initial_anchor=_anchor(None, "1000"))
    anchor = _anchor_of(
        schedule,
        _event(EVENT_1, "2026-01-01", "1000.0"),
        _event(EVENT_2, "2026-05-01", "900"),
        current="5000",
    )
    assert anchor == _source(EffectiveSource.INITIAL_ANCHOR, None, "1000")


def test_apply_runtime_safety_keeps_unknown_runtime_anchor() -> None:
    anchor = EffectiveAnchor(EffectiveSource.EVENT_GROUP, date(2026, 5, 1), None)
    events = _events(_event(EVENT_1, "2026-01-01", "9000"))
    assert apply_runtime_safety(anchor, events, SCHEDULE_1, Decimal(0)) is anchor


def test_effective_anchor_is_before_runtime_safety() -> None:
    """effective_anchor returns the raw selection; safety is a separate step."""
    schedule = _both()
    events = _events(_event(EVENT_1, "2026-05-01", "1000"))
    raw = effective_anchor(schedule, events)
    assert raw.runtime == Decimal(1000)
    safe = apply_runtime_safety(raw, events, SCHEDULE_1, Decimal(10))
    assert safe == EffectiveAnchor(raw.source, raw.calendar, None)


# Due states


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 10, 31), DueState.OK),
        (date(2026, 11, 1), DueState.DUE),
        (date(2026, 11, 2), DueState.OVERDUE),
    ],
)
def test_calendar_due_states(today: date, expected: DueState) -> None:
    projection = _project(_schedule(), _event(EVENT_1, "2026-05-01"), today=today)
    assert projection.calendar == CalendarCondition(date(2026, 11, 1), expected)
    assert projection.runtime is None
    assert projection.combined_state is expected


def test_floating_month_is_not_remembered_by_projection() -> None:
    """Jan 31 -> due Feb 28; maintenance on Feb 28 -> next due Mar 28."""
    schedule = _schedule(
        calendar_interval={"value": 1, "unit": "months"},
        initial_anchor=_anchor("2026-01-31", None),
    )
    first = _project(schedule, today=date(2026, 2, 1))
    assert first.calendar.due == date(2026, 2, 28)
    second = _project(schedule, _event(EVENT_1, "2026-02-28"), today=date(2026, 3, 1))
    assert second.calendar.due == date(2026, 3, 28)


def test_calendar_overflow_is_unknown_in_projection() -> None:
    schedule = _schedule(
        calendar_interval={"value": 9999, "unit": "years"},
        initial_anchor=_anchor("2026-01-01", None),
    )
    projection = _project(schedule)
    assert projection.calendar == CalendarCondition(None, DueState.UNKNOWN)
    assert projection.combined_state is DueState.UNKNOWN


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("1499.99", DueState.OK),
        ("1500", DueState.DUE),
        ("1500.0", DueState.DUE),
        ("1500.000", DueState.DUE),
        ("1500.01", DueState.OVERDUE),
    ],
)
def test_runtime_due_states_use_decimal_equality(
    current: str, expected: DueState
) -> None:
    """Fractional canonical Runtime strings compare numerically, never as text."""
    schedule = _runtime_only(runtime_interval_seconds="999.5")
    projection = _project(
        schedule, _event(EVENT_1, "2026-05-01", "500.5"), current=current
    )
    assert projection.runtime.due == Decimal("1500.0")
    assert projection.runtime.state is expected
    assert projection.calendar is None
    assert projection.combined_state is expected


def test_runtime_unknown_current_is_unknown_with_known_threshold() -> None:
    schedule = _runtime_only()
    projection = _project(schedule, _event(EVENT_1, "2026-05-01", "500"), current=None)
    assert projection.runtime.due == Decimal(1500)
    assert projection.runtime.state is DueState.UNKNOWN


def test_runtime_condition_defensive_rule_a() -> None:
    """A current Runtime below the anchor is never projected as OK."""
    condition = runtime_condition(Decimal(1000), Decimal(100), Decimal(999))
    assert condition.state is DueState.UNKNOWN
    assert runtime_condition(None, Decimal(100), Decimal(999)).due is None


@pytest.mark.parametrize(
    ("calendar_today", "current", "calendar_state", "runtime_state", "combined"),
    [
        (date(2026, 10, 1), "1100", DueState.OK, DueState.OK, DueState.OK),
        (date(2026, 10, 1), None, DueState.OK, DueState.UNKNOWN, DueState.UNKNOWN),
        (date(2026, 11, 1), None, DueState.DUE, DueState.UNKNOWN, DueState.DUE),
        (date(2026, 12, 1), None, DueState.OVERDUE, DueState.UNKNOWN, DueState.OVERDUE),
        (date(2026, 10, 1), "2000", DueState.OK, DueState.DUE, DueState.DUE),
        (date(2026, 11, 1), "2001", DueState.DUE, DueState.OVERDUE, DueState.OVERDUE),
        (date(2026, 12, 1), "2000", DueState.OVERDUE, DueState.DUE, DueState.OVERDUE),
        (date(2026, 11, 1), "1100", DueState.DUE, DueState.OK, DueState.DUE),
    ],
)
def test_combined_state_is_the_maximum(
    calendar_today: date,
    current: str | None,
    calendar_state: DueState,
    runtime_state: DueState,
    combined: DueState,
) -> None:
    """Whichever comes first; OK < UNKNOWN < DUE < OVERDUE."""
    projection = _project(
        _both(),
        _event(EVENT_1, "2026-05-01", "1000"),
        today=calendar_today,
        current=current,
    )
    assert projection.calendar.state is calendar_state
    assert projection.runtime.state is runtime_state
    assert projection.combined_state is combined


def test_combine_states_order_and_empty_input() -> None:
    order = [DueState.OK, DueState.UNKNOWN, DueState.DUE, DueState.OVERDUE]
    for low_index, low in enumerate(order):
        for high in order[low_index:]:
            assert combine_states([low, high]) is high
            assert combine_states([high, low]) is high
    with pytest.raises(ValueError, match="at least one condition"):
        combine_states([])


def test_combined_unknown_calendar_with_ok_runtime() -> None:
    projection = _project(_both(initial_anchor=_anchor(None, "1000")), current="1100")
    assert projection.calendar.state is DueState.UNKNOWN
    assert projection.runtime.state is DueState.OK
    assert projection.combined_state is DueState.UNKNOWN


# Future effective calendar anchor (projection erratum 2026-09-26)


def test_future_initial_anchor_one_day_ahead() -> None:
    """A dated baseline one day in the future stays the source; calendar UNKNOWN."""
    schedule = _schedule(initial_anchor=_anchor("2026-09-27", None))
    projection = _project(schedule, today=date(2026, 9, 26))
    assert projection.anchor == _source(
        EffectiveSource.INITIAL_ANCHOR, "2026-09-27", None
    )
    assert projection.calendar == CalendarCondition(None, DueState.UNKNOWN)
    assert projection.combined_state is DueState.UNKNOWN


def test_far_future_anchor_after_clock_correction() -> None:
    """Accepted while the clock was ahead: never OK with a trusted future due."""
    schedule = _schedule(calendar_interval={"value": 1, "unit": "years"})
    projection = _project(
        schedule, _event(EVENT_1, "2036-09-26"), today=date(2026, 9, 26)
    )
    assert projection.anchor == _source(EffectiveSource.EVENT_GROUP, "2036-09-26", None)
    assert projection.calendar == CalendarCondition(None, DueState.UNKNOWN)
    assert projection.combined_state is DueState.UNKNOWN


def test_anchor_today_is_projected_normally_in_projection() -> None:
    """An anchor equal to today is valid; it is not the same as due today."""
    projection = _project(_schedule(), _event(EVENT_1, "2026-09-26"))
    assert projection.calendar == CalendarCondition(date(2027, 3, 26), DueState.OK)
    assert projection.combined_state is DueState.OK


def test_future_event_remains_the_effective_source() -> None:
    """Source selection is unchanged; only the calendar projection is unsafe."""
    schedule = _schedule(initial_anchor=_anchor("2026-03-01", None))
    projection = _project(schedule, _event(EVENT_1, "2026-10-10"))
    assert projection.anchor == _source(EffectiveSource.EVENT_GROUP, "2026-10-10", None)
    assert projection.calendar == CalendarCondition(None, DueState.UNKNOWN)


def test_future_initial_anchor_is_not_replaced_by_an_older_event() -> None:
    """The future baseline stays selected; no fallback to an older Event."""
    schedule = _both(initial_anchor=_anchor("2036-09-26", "300"))
    projection = _project(
        schedule, _event(EVENT_1, "2026-05-01", "200"), current="1000"
    )
    assert projection.anchor == _source(
        EffectiveSource.INITIAL_ANCHOR, "2036-09-26", "300"
    )
    assert projection.calendar == CalendarCondition(None, DueState.UNKNOWN)
    assert projection.runtime.due == Decimal(1300)
    assert projection.runtime.state is DueState.OK
    assert projection.combined_state is DueState.UNKNOWN


@pytest.mark.parametrize(
    ("current", "runtime_state", "combined"),
    [
        ("1200", DueState.OK, DueState.UNKNOWN),
        ("1500", DueState.DUE, DueState.DUE),
        ("1500.0", DueState.DUE, DueState.DUE),
        ("1600", DueState.OVERDUE, DueState.OVERDUE),
        (None, DueState.UNKNOWN, DueState.UNKNOWN),
    ],
)
def test_future_calendar_anchor_with_runtime(
    current: str | None, runtime_state: DueState, combined: DueState
) -> None:
    """The Runtime projection keeps its own rules; the frozen ranking applies."""
    schedule = _both(runtime_interval_seconds="500")
    projection = _project(
        schedule, _event(EVENT_1, "2036-09-26", "1000"), current=current
    )
    assert projection.anchor == _source(
        EffectiveSource.EVENT_GROUP, "2036-09-26", "1000"
    )
    assert projection.calendar == CalendarCondition(None, DueState.UNKNOWN)
    assert projection.runtime.due == Decimal(1500)
    assert projection.runtime.state is runtime_state
    assert projection.combined_state is combined


def test_future_calendar_anchor_runtime_still_subject_to_rule_a() -> None:
    projection = _project(_both(), _event(EVENT_1, "2036-09-26", "1000"), current="999")
    assert projection.anchor == _source(EffectiveSource.EVENT_GROUP, "2036-09-26", None)
    assert projection.calendar.state is DueState.UNKNOWN
    assert projection.runtime.state is DueState.UNKNOWN
    assert projection.combined_state is DueState.UNKNOWN


@pytest.mark.parametrize(
    ("runtime_interval", "current", "combined"),
    [
        (None, None, DueState.UNKNOWN),
        ("500", "1200", DueState.UNKNOWN),
        ("500", "1500", DueState.DUE),
        ("500", "1600", DueState.OVERDUE),
    ],
)
def test_future_calendar_anchor_preparation_is_unknown(
    runtime_interval: str | None, current: str | None, combined: DueState
) -> None:
    """Unknown calendar due: preparation is UNKNOWN, never INACTIVE."""
    schedule = _prepared(runtime_interval_seconds=runtime_interval)
    projection = _project(
        schedule, _event(EVENT_1, "2036-09-26", "1000"), current=current
    )
    assert projection.calendar.due is None
    assert projection.combined_state is combined
    assert projection.preparation is PreparationState.UNKNOWN


def test_future_calendar_anchor_recovers_when_today_reaches_it() -> None:
    """The same persisted data projects normally once the anchor is not future."""
    schedule = _prepared(calendar_interval={"value": 1, "unit": "years"})
    events = (_event(EVENT_1, "2036-09-26"),)
    before = _project(schedule, *events, today=date(2036, 9, 25))
    after = _project(schedule, *events, today=date(2036, 9, 26))
    assert before.calendar == CalendarCondition(None, DueState.UNKNOWN)
    assert before.preparation is PreparationState.UNKNOWN
    assert after.calendar == CalendarCondition(date(2037, 9, 26), DueState.OK)
    assert after.preparation is PreparationState.INACTIVE


def test_future_calendar_anchor_on_disabled_schedule() -> None:
    """Disabled semantics are unchanged: no active projection at all."""
    projection = _project(_schedule(enabled=False), _event(EVENT_1, "2036-09-26"))
    assert projection.active is False
    assert projection.calendar is None
    assert projection.combined_state is None


def test_future_persisted_dates_remain_load_valid() -> None:
    """The erratum is projection-only; load validation never compares with today."""
    schedules = {
        SCHEDULE_1: _schedule(initial_anchor=_anchor("2036-09-26", None)),
        SCHEDULE_2: _both(
            schedule_uuid=SCHEDULE_2, initial_anchor=_anchor("9999-12-31", "10")
        ),
    }
    events = _events(
        _event(EVENT_1, "2099-01-01", "1"),
        _event(EVENT_2, "9999-12-31", None, schedule_uuids=[SCHEDULE_2]),
        _voided(EVENT_3, "2040-02-29"),
    )
    validate_maintenance_collections({ASSET_A: {}}, schedules, events)


# Disabled Schedule


def test_disabled_schedule_has_no_active_projection() -> None:
    """Disabled is not a DueState value; the due fields are simply absent."""
    schedule = _both(
        enabled=False,
        preparation_reminder={"lead_days": 30, "message": None},
    )
    projection = _project(
        schedule,
        _event(EVENT_1, "2026-01-01", "1000"),
        today=date(2027, 1, 1),
        current="9000",
    )
    assert projection.active is False
    assert projection.calendar is None
    assert projection.runtime is None
    assert projection.combined_state is None
    assert projection.preparation is None
    assert projection.anchor == _source(
        EffectiveSource.EVENT_GROUP, "2026-01-01", "1000"
    )
    assert "disabled" not in {state.value for state in DueState}
    assert set(DueState) == {
        DueState.OK,
        DueState.UNKNOWN,
        DueState.DUE,
        DueState.OVERDUE,
    }


# Preparation


def _prepared(lead_days: int = 10, **fields: Any) -> dict:
    return _schedule(
        preparation_reminder={"lead_days": lead_days, "message": "Order filters"},
        **fields,
    )


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 10, 21), PreparationState.INACTIVE),
        (date(2026, 10, 22), PreparationState.ACTIVE),
        (date(2026, 10, 31), PreparationState.ACTIVE),
        (date(2026, 11, 1), PreparationState.INACTIVE),
        (date(2026, 11, 2), PreparationState.INACTIVE),
    ],
)
def test_preparation_window(today: date, expected: PreparationState) -> None:
    """ACTIVE from due - lead_days until the day before the due date."""
    projection = _project(_prepared(), _event(EVENT_1, "2026-05-01"), today=today)
    assert projection.calendar.due == date(2026, 11, 1)
    assert projection.preparation is expected


def test_preparation_does_not_change_due_state() -> None:
    with_reminder = _project(
        _prepared(), _event(EVENT_1, "2026-05-01"), today=date(2026, 10, 25)
    )
    without = _project(
        _schedule(), _event(EVENT_1, "2026-05-01"), today=date(2026, 10, 25)
    )
    assert with_reminder.calendar == without.calendar
    assert with_reminder.combined_state is without.combined_state is DueState.OK
    assert without.preparation is None


def test_preparation_unknown_calendar_due_is_unknown() -> None:
    """UNKNOWN is never turned into INACTIVE (False)."""
    projection = _project(_prepared())
    assert projection.calendar.due is None
    assert projection.preparation is PreparationState.UNKNOWN


def test_preparation_unknown_calendar_with_overdue_runtime_stays_unknown() -> None:
    """An unknown calendar due date is UNKNOWN before the DUE/OVERDUE rule."""
    schedule = _prepared(
        runtime_interval_seconds="100", initial_anchor=_anchor(None, "1000")
    )
    projection = _project(schedule, current="5000")
    assert projection.combined_state is DueState.OVERDUE
    assert projection.preparation is PreparationState.UNKNOWN


def test_preparation_overflow_due_is_unknown() -> None:
    schedule = _prepared(
        calendar_interval={"value": 9999, "unit": "years"},
        initial_anchor=_anchor("2026-01-01", None),
    )
    assert _project(schedule).preparation is PreparationState.UNKNOWN


def test_preparation_inactive_when_runtime_is_due_inside_window() -> None:
    """Never suggest time remains once the Schedule is DUE or OVERDUE."""
    schedule = _prepared(runtime_interval_seconds="1000")
    for current, state in (("2000", DueState.DUE), ("2500", DueState.OVERDUE)):
        projection = _project(
            schedule,
            _event(EVENT_1, "2026-05-01", "1000"),
            today=date(2026, 10, 25),
            current=current,
        )
        assert projection.calendar.state is DueState.OK
        assert projection.combined_state is state
        assert projection.preparation is PreparationState.INACTIVE


def test_preparation_active_with_unknown_runtime_inside_window() -> None:
    """A known calendar due date keeps the reminder meaningful."""
    schedule = _prepared(runtime_interval_seconds="1000")
    projection = _project(
        schedule,
        _event(EVENT_1, "2026-05-01"),
        today=date(2026, 10, 25),
        current="5000",
    )
    assert projection.combined_state is DueState.UNKNOWN
    assert projection.preparation is PreparationState.ACTIVE


def test_preparation_large_lead_underflow_is_active() -> None:
    """A window starting before date.min has certainly started."""
    schedule = _prepared(
        lead_days=10**6,
        calendar_interval={"value": 1, "unit": "months"},
        initial_anchor=_anchor("0001-01-01", None),
    )
    projection = _project(schedule, today=date(1, 1, 15))
    assert projection.calendar == CalendarCondition(date(1, 2, 1), DueState.OK)
    assert projection.preparation is PreparationState.ACTIVE


def test_preparation_large_lead_is_active_before_due() -> None:
    projection = _project(_prepared(lead_days=100_000), _event(EVENT_1, "2026-05-01"))
    assert projection.preparation is PreparationState.ACTIVE


def test_preparation_state_direct() -> None:
    known = CalendarCondition(date(2026, 11, 1), DueState.OK)
    unknown = CalendarCondition(None, DueState.UNKNOWN)
    assert preparation_state(5, unknown, DueState.OK, TODAY) is PreparationState.UNKNOWN
    assert (
        preparation_state(5, unknown, DueState.OVERDUE, TODAY)
        is PreparationState.UNKNOWN
    )
    assert preparation_state(5, known, DueState.OK, TODAY) is PreparationState.INACTIVE
    assert (
        preparation_state(5, known, DueState.OK, date(2026, 10, 27))
        is PreparationState.ACTIVE
    )
    assert (
        preparation_state(5, known, DueState.DUE, date(2026, 10, 27))
        is PreparationState.INACTIVE
    )


def test_reminder_without_calendar_interval_is_a_programming_error() -> None:
    """Validation forbids it, so projection refuses rather than guessing."""
    schedule = _runtime_only(preparation_reminder={"lead_days": 1, "message": None})
    with pytest.raises(ValueError, match="requires a calendar interval"):
        project_schedule(schedule, {}, today=TODAY, current_runtime=None)


# Purity, determinism, and invalid inputs


def test_inputs_are_not_mutated() -> None:
    schedule = _both(
        initial_anchor=_anchor("2026-03-01", "300"),
        preparation_reminder={"lead_days": 30, "message": "Order filters"},
    )
    events = _events(
        _event(EVENT_1, "2026-01-01", "1200"),
        _event(EVENT_2, "2026-05-01", "500", schedule_uuids=[SCHEDULE_2, SCHEDULE_1]),
        _event(EVENT_3, "2026-05-01", "600"),
        _voided(EVENT_4, "2026-06-01", "700"),
    )
    _checked(schedule, events)
    before = deepcopy((schedule, events))
    first = project_schedule(schedule, events, today=TODAY, current_runtime="5000")
    effective_anchor(schedule, events)
    relevant_events(events, SCHEDULE_1)
    latest_event_group(events.values())
    assert (schedule, events) == before
    second = project_schedule(schedule, events, today=TODAY, current_runtime="5000")
    assert first == second


def test_projection_results_are_immutable() -> None:
    projection = _project(_schedule(), _event(EVENT_1, "2026-05-01"))
    with pytest.raises(AttributeError):
        projection.combined_state = DueState.OVERDUE  # type: ignore[misc]


def test_projection_depends_only_on_supplied_today_and_runtime() -> None:
    """The same persisted data gives different results only via caller inputs."""
    schedule = _both()
    events = _events(_event(EVENT_1, "2026-05-01", "1000"))
    early = project_schedule(
        schedule, events, today=date(2026, 6, 1), current_runtime="1500"
    )
    late = project_schedule(
        schedule, events, today=date(2027, 6, 1), current_runtime="2500"
    )
    assert early.combined_state is DueState.OK
    assert late.combined_state is DueState.OVERDUE


@pytest.mark.parametrize("current", ["1e3", "-1", "01", " 1", "NaN"])
def test_non_canonical_current_runtime_is_rejected(current: str) -> None:
    with pytest.raises(CanonicalValueError):
        project_schedule(_both(), {}, today=TODAY, current_runtime=current)


def test_non_canonical_persisted_runtime_is_rejected() -> None:
    """Projection expects validated records and never repairs them."""
    events = _events(_event(EVENT_1, "2026-05-01", "1.0e3"))
    with pytest.raises(CanonicalValueError):
        project_schedule(_both(), events, today=TODAY, current_runtime=None)


# Production boundary


def _imports(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    return names


def test_module_has_no_home_assistant_clock_or_store_dependencies() -> None:
    """Pure: no Home Assistant, no dt_util, no clock, no Store."""
    source = MODULE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = _imports(tree)
    assert imports == {
        "__future__",
        "calendar",
        "collections.abc",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        ".canonical",
        ".models",
    }
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert not attributes & {"now", "today", "utcnow", "time", "async_save"}
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert "dt_util" not in names
    assert "hass" not in names
    assert not any(isinstance(node, ast.AsyncFunctionDef) for node in ast.walk(tree))


def test_projection_is_not_wired_into_production() -> None:
    """No production module imports the projection before activation."""
    package = MODULE_PATH.parent
    for path in package.glob("*.py"):
        if path == MODULE_PATH:
            continue
        assert "maintenance_projection" not in _imports(
            ast.parse(path.read_text(encoding="utf-8"))
        ) | {
            name.rsplit(".", 1)[-1]
            for name in _imports(ast.parse(path.read_text(encoding="utf-8")))
        }, path.name


def test_store_3_1_top_level_keys_are_unchanged() -> None:
    assert (
        frozenset(
            {
                "next_asset_number",
                "purchases",
                "assets",
                "lifecycle_events",
                "replacement_records",
            }
        )
        == STORE_TOP_LEVEL_KEYS
    )


@pytest.mark.parametrize("key", ["maintenance_schedules", "maintenance_events"])
def test_store_3_1_still_rejects_maintenance_collections(
    asset_store_data: AssetStoreData,
    key: str,
) -> None:
    data: dict[str, Any] = deepcopy(asset_store_data)
    data[key] = {}
    with pytest.raises(AssetStoreError, match="invalid top-level shape"):
        _validate_store_data(data)  # type: ignore[arg-type]
