"""Pure Maintenance projection for the frozen Maintenance Store schema.

The authoritative rules are in docs/maintenance-store-v4-schema.md. Every
function here is synchronous, deterministic, and free of side effects: it
reads no clock, no Home Assistant state, and no Store, and it never mutates
its inputs. The caller always supplies ``today`` (Home Assistant's local
civil date) and ``current_runtime`` (the canonical Asset Runtime string).

Records are expected to have passed the standalone validation in
maintenance.py. Invalid persisted shapes or non-canonical values are
programming errors and raise. Normal domain uncertainty - an unknown
Runtime snapshot, contradictory Runtime history, several Events on the
latest date, a date-less baseline whose order cannot be proven, a calendar
anchor later than ``today``, or a date that cannot be represented - is
returned as UNKNOWN, never raised.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from .canonical import parse_canonical_decimal, parse_civil_date
from .models import MaintenanceEventData, MaintenanceScheduleData


class DueState(StrEnum):
    """Frozen per-condition and combined due states, not persisted."""

    OK = "ok"
    UNKNOWN = "unknown"
    DUE = "due"
    OVERDUE = "overdue"


# Frozen combination order: the combined state is the maximum.
_DUE_STATE_RANK = {
    DueState.OK: 0,
    DueState.UNKNOWN: 1,
    DueState.DUE: 2,
    DueState.OVERDUE: 3,
}


class PreparationState(StrEnum):
    """Preparation reminder activity. UNKNOWN is never turned into INACTIVE."""

    ACTIVE = "active"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


class EffectiveSource(StrEnum):
    """Which source the effective anchor was taken from."""

    INITIAL_ANCHOR = "initial_anchor"
    EVENT_GROUP = "event_group"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EffectiveAnchor:
    """The effective anchor components; ``None`` means UNKNOWN."""

    source: EffectiveSource
    calendar: date | None
    runtime: Decimal | None


@dataclass(frozen=True, slots=True)
class CalendarCondition:
    """The calendar condition of a Schedule with a calendar interval.

    ``due`` is ``None`` when the due date is UNKNOWN.
    """

    due: date | None
    state: DueState


@dataclass(frozen=True, slots=True)
class RuntimeCondition:
    """The Runtime condition of a Schedule with a Runtime interval.

    ``due`` is the Runtime threshold in seconds, ``None`` when UNKNOWN.
    """

    due: Decimal | None
    state: DueState


@dataclass(frozen=True, slots=True)
class MaintenanceProjection:
    """The live projection of one Schedule. Never persisted.

    ``anchor`` is the effective anchor after Runtime safety. When ``active``
    is False the Schedule is disabled: there is no active due projection, so
    ``calendar``, ``runtime``, ``combined_state``, and ``preparation`` are
    ``None``. When ``active`` is True, ``calendar`` or ``runtime`` is
    ``None`` only when that interval is not configured, ``combined_state``
    is always set, and ``preparation`` is ``None`` only when no preparation
    reminder is configured.
    """

    active: bool
    anchor: EffectiveAnchor
    calendar: CalendarCondition | None
    runtime: RuntimeCondition | None
    combined_state: DueState | None
    preparation: PreparationState | None


def relevant_events(
    events: Mapping[str, MaintenanceEventData],
    schedule_uuid: str,
) -> list[MaintenanceEventData]:
    """Return non-voided Events that reference the Schedule.

    The Schedule's enabled state does not matter. The result is not
    ordered by any timestamp.
    """
    return [
        event
        for event in events.values()
        if event["voided_at"] is None and schedule_uuid in event["schedule_uuids"]
    ]


def latest_event_group(
    events: Iterable[MaintenanceEventData],
) -> tuple[date, list[MaintenanceEventData]] | None:
    """Return the latest performed date and every Event on it, or None.

    There is no tie-breaker within the group.
    """
    dated = [(parse_civil_date(event["performed_date"]), event) for event in events]
    if not dated:
        return None
    latest = max(performed for performed, _event in dated)
    return latest, [event for performed, event in dated if performed == latest]


def _runtime(value: str | None) -> Decimal | None:
    """Parse an optional canonical Runtime string; None stays UNKNOWN."""
    return None if value is None else parse_canonical_decimal(value)


def _event_group_anchor(
    latest: date,
    group: list[MaintenanceEventData],
) -> EffectiveAnchor:
    """Anchor from the latest date group: Runtime only from a single Event."""
    runtime = _runtime(group[0]["runtime_seconds"]) if len(group) == 1 else None
    return EffectiveAnchor(EffectiveSource.EVENT_GROUP, latest, runtime)


def effective_anchor(
    schedule: MaintenanceScheduleData,
    events: Mapping[str, MaintenanceEventData],
) -> EffectiveAnchor:
    """Select the effective source and its raw anchor, before Runtime safety.

    Calendar and Runtime components always come from the same source; a
    missing component is never filled from another source.
    """
    relevant = relevant_events(events, schedule["schedule_uuid"])
    grouped = latest_event_group(relevant)
    initial = schedule["initial_anchor"]

    if initial is None:
        if grouped is None:
            return EffectiveAnchor(EffectiveSource.UNKNOWN, None, None)
        return _event_group_anchor(*grouped)

    initial_runtime = _runtime(initial["runtime_seconds"])
    initial_anchor = EffectiveAnchor(
        EffectiveSource.INITIAL_ANCHOR,
        None if initial["date"] is None else parse_civil_date(initial["date"]),
        initial_runtime,
    )
    if grouped is None:
        return initial_anchor
    latest, group = grouped

    if initial_anchor.calendar is not None:
        # Dated baseline: chronological comparison; the same date goes to the
        # Event, and a backdated Event never moves the anchor backward.
        if latest >= initial_anchor.calendar:
            return _event_group_anchor(latest, group)
        return initial_anchor

    # Date-less Runtime baseline: order is proven only by Runtime evidence.
    # Validation guarantees the Runtime component is known here.
    group_runtimes = [_runtime(event["runtime_seconds"]) for event in group]
    if all(value is not None and value >= initial_runtime for value in group_runtimes):
        return _event_group_anchor(latest, group)
    if all(value is not None and value < initial_runtime for value in group_runtimes):
        return initial_anchor
    return EffectiveAnchor(EffectiveSource.UNKNOWN, None, None)


def apply_runtime_safety(
    anchor: EffectiveAnchor,
    events: Mapping[str, MaintenanceEventData],
    schedule_uuid: str,
    current_runtime: Decimal | None,
) -> EffectiveAnchor:
    """Make the Runtime anchor UNKNOWN when history or the present contradict it.

    Rule A: the anchor exceeds the known current canonical Runtime.
    Rule B: a dated winning source has a known Runtime, and a relevant Event
    dated on or before it has a greater known Runtime.
    Rule B': a date-less initial anchor wins, and any relevant Event has a
    greater known Runtime.

    The calendar component is never changed, and history is never edited.
    """
    runtime = anchor.runtime
    if runtime is None:
        return anchor

    relevant = relevant_events(events, schedule_uuid)
    if anchor.calendar is not None:
        contradicted = any(
            event["runtime_seconds"] is not None
            and parse_civil_date(event["performed_date"]) <= anchor.calendar
            and parse_canonical_decimal(event["runtime_seconds"]) > runtime
            for event in relevant
        )
    else:
        # Only a date-less initial anchor has a Runtime without a date.
        contradicted = any(
            event["runtime_seconds"] is not None
            and parse_canonical_decimal(event["runtime_seconds"]) > runtime
            for event in relevant
        )

    if contradicted or (current_runtime is not None and runtime > current_runtime):
        return EffectiveAnchor(anchor.source, anchor.calendar, None)
    return anchor


def add_calendar_interval(anchor: date, value: int, unit: str) -> date | None:
    """Add a calendar interval to a civil date; None when not representable.

    ``days`` adds civil days. ``months`` moves to the target month and clamps
    the day to that month's last valid day. ``years`` is 12 x N months. The
    interval is always added to the effective anchor itself, so a clamped
    day is never carried into the next cycle.
    """
    if unit == "days":
        try:
            return anchor + timedelta(days=value)
        except OverflowError:
            return None
    if unit == "years":
        return add_calendar_interval(anchor, value * 12, "months")
    if unit != "months":
        raise ValueError(f"Unknown calendar interval unit: {unit}")
    month_index = anchor.year * 12 + (anchor.month - 1) + value
    year, month_zero = divmod(month_index, 12)
    if not date.min.year <= year <= date.max.year:
        return None
    month = month_zero + 1
    return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))


def _compare[T: (date, Decimal)](current: T, due: T) -> DueState:
    if current < due:
        return DueState.OK
    if current == due:
        return DueState.DUE
    return DueState.OVERDUE


def calendar_condition(
    anchor: date | None,
    interval_value: int,
    interval_unit: str,
    today: date,
) -> CalendarCondition:
    """Project the calendar condition from the effective calendar anchor.

    An anchor later than ``today`` is not trusted: it can remain after the
    Home Assistant clock was wrongly ahead and later corrected. The calendar
    projection is then UNKNOWN with no due date, while the persisted source
    itself stays selected and unchanged.
    """
    if anchor is None or anchor > today:
        return CalendarCondition(None, DueState.UNKNOWN)
    due = add_calendar_interval(anchor, interval_value, interval_unit)
    if due is None:
        return CalendarCondition(None, DueState.UNKNOWN)
    return CalendarCondition(due, _compare(today, due))


def runtime_condition(
    anchor: Decimal | None,
    interval_seconds: Decimal,
    current_runtime: Decimal | None,
) -> RuntimeCondition:
    """Project the Runtime condition from the effective Runtime anchor."""
    if anchor is None:
        return RuntimeCondition(None, DueState.UNKNOWN)
    due = anchor + interval_seconds
    if current_runtime is None or current_runtime < anchor:
        return RuntimeCondition(due, DueState.UNKNOWN)
    return RuntimeCondition(due, _compare(current_runtime, due))


def combine_states(states: Iterable[DueState]) -> DueState:
    """Combine condition states by the frozen order OK < UNKNOWN < DUE < OVERDUE."""
    present = list(states)
    if not present:
        raise ValueError("A Maintenance Schedule has at least one condition")
    return max(present, key=_DUE_STATE_RANK.__getitem__)


def preparation_state(
    lead_days: int,
    calendar: CalendarCondition,
    combined_state: DueState,
    today: date,
) -> PreparationState:
    """Project the preparation reminder; UNKNOWN is never reported as inactive.

    An unknown calendar due date is UNKNOWN. Once the Schedule is DUE or
    OVERDUE the reminder is INACTIVE, so it never suggests that time remains.
    Otherwise it is ACTIVE from ``lead_days`` before the due date until the
    day before it.
    """
    if calendar.due is None:
        return PreparationState.UNKNOWN
    if combined_state in (DueState.DUE, DueState.OVERDUE):
        return PreparationState.INACTIVE
    try:
        start = calendar.due - timedelta(days=lead_days)
    except OverflowError:
        # The window starts before the first representable date, so it has
        # certainly started; the combined state is below DUE, so the calendar
        # condition is OK and today < due.
        return PreparationState.ACTIVE
    if start <= today < calendar.due:
        return PreparationState.ACTIVE
    return PreparationState.INACTIVE


def project_schedule(
    schedule: MaintenanceScheduleData,
    events: Mapping[str, MaintenanceEventData],
    *,
    today: date,
    current_runtime: str | None,
) -> MaintenanceProjection:
    """Project one Schedule in the frozen order.

    1. Select the effective source and raw anchors.
    2. Apply Runtime safety (Rules A, B, B').
    3. Derive due dates and thresholds, then condition states.
    4. Combine the states.
    5. Derive the preparation state.
    """
    current = _runtime(current_runtime)
    anchor = apply_runtime_safety(
        effective_anchor(schedule, events),
        events,
        schedule["schedule_uuid"],
        current,
    )
    if not schedule["enabled"]:
        return MaintenanceProjection(False, anchor, None, None, None, None)

    calendar_config = schedule["calendar_interval"]
    calendar_result = (
        None
        if calendar_config is None
        else calendar_condition(
            anchor.calendar,
            calendar_config["value"],
            calendar_config["unit"],
            today,
        )
    )
    runtime_interval = schedule["runtime_interval_seconds"]
    runtime_result = (
        None
        if runtime_interval is None
        else runtime_condition(
            anchor.runtime,
            parse_canonical_decimal(runtime_interval, positive=True),
            current,
        )
    )
    combined = combine_states(
        condition.state
        for condition in (calendar_result, runtime_result)
        if condition is not None
    )

    reminder = schedule["preparation_reminder"]
    preparation = None
    if reminder is not None:
        if calendar_result is None:
            raise ValueError("A preparation reminder requires a calendar interval")
        preparation = preparation_state(
            reminder["lead_days"],
            calendar_result,
            combined,
            today,
        )
    return MaintenanceProjection(
        True,
        anchor,
        calendar_result,
        runtime_result,
        combined,
        preparation,
    )
