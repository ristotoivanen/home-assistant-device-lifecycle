"""Snapshot-level Maintenance mutations and idempotent replay.

The authoritative rules are the mutation and replay rules in
docs/maintenance-store-v4-schema.md. Every operation here is a synchronous
function over a detached ``MaintenanceSnapshot``:

- it never mutates its inputs; a successful change returns a new snapshot
  whose changed records are new dicts, so a failed operation leaves nothing
  behind and atomicity is structural;
- every changed snapshot passes ``validate_maintenance_collections`` before
  it is returned;
- the observed time, Home Assistant's local civil date, and the current
  canonical Runtime come only from ``MaintenanceMutationContext``, never from
  the request and never from a clock read here;
- Asset Runtime is read only through the context and is never written.

Nothing in production calls this module before Store 4 activation. The Store
manager later runs it inside its lock, deep copy, validate, save, readback,
and publish pipeline.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar

from .canonical import (
    CanonicalValueError,
    decimal_from_input,
    format_decimal,
    normalize_optional_text,
    normalize_required_text,
    parse_canonical_decimal,
    parse_canonical_utc,
    parse_civil_date,
    require_canonical_uuid,
)
from .maintenance import (
    CALENDAR_INTERVAL_UNITS,
    can_hard_delete_schedule,
    is_baseline_locked,
    referenced_schedule_uuids,
    validate_maintenance_collections,
)
from .maintenance_projection import latest_event_group, relevant_events
from .models import (
    MaintenanceCalendarIntervalData,
    MaintenanceEventData,
    MaintenanceInitialAnchorData,
    MaintenancePreparationReminderData,
    MaintenanceScheduleData,
)


class MutationOutcome(StrEnum):
    """Every distinct outcome of a Maintenance mutation.

    CHANGED, NO_OP, and REPLAY are successful results. NO_OP means the
    request already matches the current state; REPLAY means an identical
    earlier request with the same pre-generated identity already succeeded.
    Neither writes. The other outcomes are raised as errors.
    """

    CHANGED = "changed"
    NO_OP = "no_op"
    REPLAY = "replay"
    CONFLICT = "conflict"
    INVALID = "invalid"
    CONFIRMATION_REQUIRED = "confirmation_required"
    NOT_FOUND = "not_found"


class IntervalDimension(StrEnum):
    """A Schedule interval type."""

    CALENDAR = "calendar"
    RUNTIME = "runtime"


class ConfirmationKind(StrEnum):
    """A semantic UX guard that the caller must confirm explicitly."""

    BASELINE_LOCK = "baseline_lock"
    DESTROY_BASELINE = "destroy_baseline"


class MaintenanceMutationError(ValueError):
    """A Maintenance mutation was refused; nothing was changed."""

    outcome: ClassVar[MutationOutcome] = MutationOutcome.INVALID

    def __init__(self, message: str, *, code: str) -> None:
        """Store a stable error code for the caller."""
        super().__init__(message)
        self.code = code


class MaintenanceRequestError(MaintenanceMutationError):
    """The request is invalid or violates a mutation rule."""


class MaintenanceConflictError(MaintenanceMutationError):
    """A pre-generated identity already exists with a different state."""

    outcome = MutationOutcome.CONFLICT


class MaintenanceNotFoundError(MaintenanceMutationError):
    """The target or a referenced record does not exist."""

    outcome = MutationOutcome.NOT_FOUND


class MaintenanceConfirmationRequiredError(MaintenanceMutationError):
    """A semantic UX guard has not been confirmed for this request."""

    outcome = MutationOutcome.CONFIRMATION_REQUIRED

    def __init__(
        self,
        message: str,
        *,
        confirmation: ConfirmationKind,
        schedule_uuids: tuple[str, ...],
    ) -> None:
        """Name the guard and every Schedule it concerns."""
        super().__init__(message, code=f"maintenance_confirm_{confirmation}")
        self.confirmation = confirmation
        self.schedule_uuids = schedule_uuids


@dataclass(frozen=True, slots=True)
class MaintenanceMutationContext:
    """System-observed operation context, never user business input.

    ``today`` is Home Assistant's local civil date (``dt_util.now().date()``
    in the caller), ``observed_utc`` the canonical observed UTC timestamp
    used for ``recorded_at`` and ``voided_at``, and ``current_runtime`` the
    canonical Asset Runtime string or None when unknown.
    """

    today: date
    observed_utc: str
    current_runtime: str | None

    def __post_init__(self) -> None:
        """Reject a context that is not already canonical."""
        if type(self.today) is not date:
            raise TypeError("today must be a civil date, not a datetime")
        parse_canonical_utc(self.observed_utc)
        if self.current_runtime is not None:
            parse_canonical_decimal(self.current_runtime)


@dataclass(frozen=True, slots=True)
class MaintenanceSnapshot:
    """A detached Maintenance snapshot: Assets are read, never changed."""

    assets: Mapping[str, Any]
    schedules: Mapping[str, MaintenanceScheduleData]
    events: Mapping[str, MaintenanceEventData]


@dataclass(frozen=True, slots=True)
class EventGuards:
    """Warnings for a new or corrected Event; never persisted.

    ``newly_locked_baselines`` are the Schedules with an ``initial_anchor``
    that this Event references for the first time. ``same_day_runtime_ambiguity``
    are the Schedules whose latest relevant date will hold more than one
    Event, making their Runtime anchor UNKNOWN.
    """

    newly_locked_baselines: tuple[str, ...] = ()
    same_day_runtime_ambiguity: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MaintenanceMutationResult:
    """A successful mutation: CHANGED, NO_OP, or REPLAY.

    For NO_OP and REPLAY ``snapshot`` is the input snapshot itself.
    """

    outcome: MutationOutcome
    snapshot: MaintenanceSnapshot
    guards: EventGuards = field(default_factory=EventGuards)

    @property
    def changed(self) -> bool:
        """Return whether the snapshot must be saved."""
        return self.outcome is MutationOutcome.CHANGED


@dataclass(frozen=True, slots=True)
class CalendarIntervalInput:
    """Requested calendar interval."""

    value: Any
    unit: Any


@dataclass(frozen=True, slots=True)
class InitialAnchorInput:
    """Requested baseline components; ``date`` is a ``date`` or civil string."""

    date: Any = None
    runtime_seconds: Any = None


@dataclass(frozen=True, slots=True)
class PreparationReminderInput:
    """Requested preparation reminder."""

    lead_days: Any
    message: Any = None


@dataclass(frozen=True, slots=True)
class CreateScheduleRequest:
    """Create a Schedule with a pre-generated ``schedule_uuid``."""

    schedule_uuid: str
    asset_uuid: str
    name: str
    enabled: bool = True
    calendar_interval: CalendarIntervalInput | None = None
    runtime_interval_seconds: Any = None
    initial_anchor: InitialAnchorInput | None = None
    preparation_reminder: PreparationReminderInput | None = None


@dataclass(frozen=True, slots=True)
class EditScheduleRequest:
    """Replace a Schedule's current configuration.

    The configured interval types must stay the same; adding or removing an
    interval type uses the dedicated requests. The baseline is not edited
    here.
    """

    schedule_uuid: str
    name: str
    enabled: bool
    calendar_interval: CalendarIntervalInput | None
    runtime_interval_seconds: Any
    preparation_reminder: PreparationReminderInput | None


@dataclass(frozen=True, slots=True)
class SetInitialAnchorRequest:
    """Set, change, or clear the baseline of an unlocked Schedule."""

    schedule_uuid: str
    initial_anchor: InitialAnchorInput | None


@dataclass(frozen=True, slots=True)
class AddIntervalRequest:
    """Add an interval type, optionally with its baseline component.

    For CALENDAR, ``calendar_interval`` is required and ``anchor_component``
    is a baseline date. For RUNTIME, ``runtime_interval_seconds`` is required
    and ``anchor_component`` is a baseline Runtime.
    """

    schedule_uuid: str
    dimension: IntervalDimension
    calendar_interval: CalendarIntervalInput | None = None
    runtime_interval_seconds: Any = None
    anchor_component: Any = None


@dataclass(frozen=True, slots=True)
class RemoveIntervalRequest:
    """Remove an interval type with its baseline component cleanup.

    ``confirm_destroy_baseline`` proves that the caller showed the mandatory
    confirmation before a locked baseline component is destroyed. It is not
    persisted.
    """

    schedule_uuid: str
    dimension: IntervalDimension
    confirm_destroy_baseline: bool = False


@dataclass(frozen=True, slots=True)
class SetEnabledRequest:
    """Enable or disable a Schedule."""

    schedule_uuid: str
    enabled: bool


@dataclass(frozen=True, slots=True)
class DeleteScheduleRequest:
    """Hard-delete a Schedule that no Event has ever referenced."""

    schedule_uuid: str


@dataclass(frozen=True, slots=True)
class RecordEventRequest:
    """Record a new Event with a pre-generated ``event_uuid``.

    ``confirmed_baseline_locks`` names the Schedules whose first-reference
    baseline lock the caller has shown and the person has confirmed.
    """

    event_uuid: str
    asset_uuid: str
    schedule_uuids: Sequence[str]
    title: str
    performed_date: Any
    runtime_seconds: Any = None
    notes: str | None = None
    confirmed_baseline_locks: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class VoidEventRequest:
    """Manually void an active Event."""

    event_uuid: str
    void_reason: str | None = None


@dataclass(frozen=True, slots=True)
class CorrectEventRequest:
    """Void ``target_event_uuid`` and create its complete replacement.

    The corrected Event keeps the target's Asset. There is deliberately no
    Runtime-unchanged flag: the mutation compares the Runtime with the
    target's persisted value itself.
    """

    new_event_uuid: str
    target_event_uuid: str
    schedule_uuids: Sequence[str]
    title: str
    performed_date: Any
    runtime_seconds: Any = None
    notes: str | None = None
    void_reason: str | None = None
    confirmed_baseline_locks: frozenset[str] = frozenset()


# Request normalization


def _invalid(field_name: str, err: Exception | str) -> MaintenanceRequestError:
    return MaintenanceRequestError(
        f"Invalid {field_name}: {err}", code="maintenance_invalid_request"
    )


def _normalized[T](field_name: str, normalizer: Callable[..., T], value: Any) -> T:
    try:
        return normalizer(value)
    except CanonicalValueError as err:
        raise _invalid(field_name, err) from err


def _uuid(field_name: str, value: Any) -> str:
    """Require a pre-generated identity in canonical form; never rewrite it."""
    return _normalized(field_name, require_canonical_uuid, value)


def _bool(field_name: str, value: Any) -> bool:
    if type(value) is not bool:
        raise _invalid(field_name, "must be a bool")
    return value


def _positive_int(field_name: str, value: Any) -> int:
    if type(value) is not int or value <= 0:
        raise _invalid(field_name, "must be an integer greater than zero")
    return value


def _civil_date(field_name: str, value: Any) -> str:
    """Return a canonical civil date from a ``date`` or a canonical string."""
    if isinstance(value, datetime):
        raise _invalid(field_name, "must be a civil date, not a datetime")
    if isinstance(value, date):
        return value.isoformat()
    return _normalized(field_name, parse_civil_date, value).isoformat()


def _runtime(field_name: str, value: Any, *, positive: bool = False) -> str | None:
    """Return a canonical Decimal string, or None for an unknown value."""
    if value is None:
        return None
    parsed = _normalized(field_name, decimal_from_input, value)
    if positive and parsed.is_zero():
        raise _invalid(field_name, "must be greater than zero")
    return format_decimal(parsed)


def _schedule_links(value: Any) -> list[str]:
    """Return canonical, unique Schedule UUIDs in deterministic sorted order."""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _invalid("schedule_uuids", "must be a sequence of UUIDs")
    links = [_uuid("schedule_uuids", item) for item in value]
    if len(set(links)) != len(links):
        raise _invalid("schedule_uuids", "contains a duplicate")
    return sorted(links)


def _confirmed(value: Any) -> frozenset[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (set, frozenset)):
        raise _invalid("confirmed_baseline_locks", "must be a set of Schedule UUIDs")
    return frozenset(_uuid("confirmed_baseline_locks", item) for item in value)


def _calendar_interval(
    value: CalendarIntervalInput | None,
) -> MaintenanceCalendarIntervalData | None:
    if value is None:
        return None
    if not isinstance(value, CalendarIntervalInput):
        raise _invalid("calendar_interval", "must be a CalendarIntervalInput")
    interval_value = _positive_int("calendar_interval.value", value.value)
    if not isinstance(value.unit, str) or value.unit not in CALENDAR_INTERVAL_UNITS:
        raise _invalid("calendar_interval.unit", "must be days, months, or years")
    return {"value": interval_value, "unit": value.unit}


def _reminder(
    value: PreparationReminderInput | None,
) -> MaintenancePreparationReminderData | None:
    if value is None:
        return None
    if not isinstance(value, PreparationReminderInput):
        raise _invalid("preparation_reminder", "must be a PreparationReminderInput")
    return {
        "lead_days": _positive_int("preparation_reminder.lead_days", value.lead_days),
        "message": _normalized(
            "preparation_reminder.message", normalize_optional_text, value.message
        ),
    }


def _anchor(value: InitialAnchorInput | None) -> MaintenanceInitialAnchorData | None:
    """Normalize a baseline; a baseline with no known component is None."""
    if value is None:
        return None
    if not isinstance(value, InitialAnchorInput):
        raise _invalid("initial_anchor", "must be an InitialAnchorInput")
    anchor_date = (
        None if value.date is None else _civil_date("initial_anchor.date", value.date)
    )
    runtime = _runtime("initial_anchor.runtime_seconds", value.runtime_seconds)
    if anchor_date is None and runtime is None:
        return None
    return {"date": anchor_date, "runtime_seconds": runtime}


def _require_anchor_dimensions(
    anchor: MaintenanceInitialAnchorData | None,
    calendar_interval: MaintenanceCalendarIntervalData | None,
    runtime_interval: str | None,
) -> None:
    """A baseline component is allowed only for a configured interval type."""
    if anchor is None:
        return
    if anchor["date"] is not None and calendar_interval is None:
        raise MaintenanceRequestError(
            "A baseline date requires a calendar interval",
            code="maintenance_anchor_requires_interval",
        )
    if anchor["runtime_seconds"] is not None and runtime_interval is None:
        raise MaintenanceRequestError(
            "A baseline Runtime requires a Runtime interval",
            code="maintenance_anchor_requires_interval",
        )


def _require_reminder_calendar(
    reminder: MaintenancePreparationReminderData | None,
    calendar_interval: MaintenanceCalendarIntervalData | None,
) -> None:
    if reminder is not None and calendar_interval is None:
        raise MaintenanceRequestError(
            "A preparation reminder requires a calendar interval",
            code="maintenance_reminder_requires_calendar",
        )


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else parse_canonical_decimal(value)


def _same_runtime(first: str | None, second: str | None) -> bool:
    """Compare optional canonical Runtime strings numerically."""
    return _decimal(first) == _decimal(second)


# Mutation-time rules


def _reject_future_date(
    field_name: str, value: str, context: MaintenanceMutationContext
) -> None:
    """Schema mutation rule 8: no user-entered date later than local today."""
    if parse_civil_date(value) > context.today:
        raise MaintenanceRequestError(
            f"{field_name} is later than today", code="maintenance_date_in_future"
        )


def _reject_runtime_above_current(
    field_name: str,
    value: str | None,
    context: MaintenanceMutationContext,
) -> None:
    """M-8 (Event) and M-9 (baseline): never above the known current Runtime."""
    if value is None or context.current_runtime is None:
        return
    if parse_canonical_decimal(value) > parse_canonical_decimal(
        context.current_runtime
    ):
        raise MaintenanceRequestError(
            f"{field_name} is greater than the current Runtime",
            code="maintenance_runtime_above_current",
        )


def _check_new_anchor(
    anchor: MaintenanceInitialAnchorData | None,
    context: MaintenanceMutationContext,
) -> None:
    if anchor is None:
        return
    if anchor["date"] is not None:
        _reject_future_date("initial_anchor.date", anchor["date"], context)
    _reject_runtime_above_current(
        "initial_anchor.runtime_seconds", anchor["runtime_seconds"], context
    )


def _require_schedule(
    snapshot: MaintenanceSnapshot, schedule_uuid: str
) -> MaintenanceScheduleData:
    schedule = snapshot.schedules.get(schedule_uuid)
    if schedule is None:
        raise MaintenanceNotFoundError(
            f"Maintenance Schedule {schedule_uuid} does not exist",
            code="maintenance_schedule_not_found",
        )
    return schedule


def _require_asset(snapshot: MaintenanceSnapshot, asset_uuid: str) -> None:
    if asset_uuid not in snapshot.assets:
        raise MaintenanceNotFoundError(
            f"Asset {asset_uuid} does not exist", code="maintenance_asset_not_found"
        )


def _require_links(
    snapshot: MaintenanceSnapshot, asset_uuid: str, links: list[str]
) -> None:
    """Every linked Schedule exists on the Event's Asset; disabled is allowed."""
    for schedule_uuid in links:
        schedule = _require_schedule(snapshot, schedule_uuid)
        if schedule["asset_uuid"] != asset_uuid:
            raise MaintenanceRequestError(
                f"Maintenance Schedule {schedule_uuid} belongs to another Asset",
                code="maintenance_schedule_other_asset",
            )


# Results


def _unchanged(
    outcome: MutationOutcome, snapshot: MaintenanceSnapshot
) -> MaintenanceMutationResult:
    return MaintenanceMutationResult(outcome, snapshot)


def _changed(
    snapshot: MaintenanceSnapshot,
    *,
    schedules: dict[str, MaintenanceScheduleData] | None = None,
    events: dict[str, MaintenanceEventData] | None = None,
    guards: EventGuards | None = None,
) -> MaintenanceMutationResult:
    """Build the candidate and prove it satisfies every load invariant.

    This is the single post-mutation validation path. A failure here is a
    programming error in this module; the input snapshot is still untouched.
    """
    candidate = MaintenanceSnapshot(
        snapshot.assets,
        dict(snapshot.schedules) if schedules is None else schedules,
        dict(snapshot.events) if events is None else events,
    )
    validate_maintenance_collections(
        candidate.assets, candidate.schedules, candidate.events
    )
    return MaintenanceMutationResult(
        MutationOutcome.CHANGED, candidate, guards or EventGuards()
    )


def _with_schedule(
    snapshot: MaintenanceSnapshot, record: MaintenanceScheduleData
) -> dict[str, MaintenanceScheduleData]:
    return {**snapshot.schedules, record["schedule_uuid"]: record}


# Schedule mutations


def create_schedule(
    snapshot: MaintenanceSnapshot,
    request: CreateScheduleRequest,
    context: MaintenanceMutationContext,
) -> MaintenanceMutationResult:
    """Create a Schedule; an identical existing Schedule is a REPLAY."""
    schedule_uuid = _uuid("schedule_uuid", request.schedule_uuid)
    asset_uuid = _uuid("asset_uuid", request.asset_uuid)
    calendar_interval = _calendar_interval(request.calendar_interval)
    runtime_interval = _runtime(
        "runtime_interval_seconds", request.runtime_interval_seconds, positive=True
    )
    if calendar_interval is None and runtime_interval is None:
        raise MaintenanceRequestError(
            "A Maintenance Schedule needs at least one interval",
            code="maintenance_interval_required",
        )
    anchor = _anchor(request.initial_anchor)
    _require_anchor_dimensions(anchor, calendar_interval, runtime_interval)
    reminder = _reminder(request.preparation_reminder)
    _require_reminder_calendar(reminder, calendar_interval)
    record: MaintenanceScheduleData = {
        "schedule_uuid": schedule_uuid,
        "asset_uuid": asset_uuid,
        "name": _normalized("name", normalize_required_text, request.name),
        "enabled": _bool("enabled", request.enabled),
        "calendar_interval": calendar_interval,
        "runtime_interval_seconds": runtime_interval,
        "initial_anchor": anchor,
        "preparation_reminder": reminder,
    }

    existing = snapshot.schedules.get(schedule_uuid)
    if existing is not None:
        if existing == record:
            return _unchanged(MutationOutcome.REPLAY, snapshot)
        raise MaintenanceConflictError(
            f"Maintenance Schedule {schedule_uuid} already exists with other content",
            code="maintenance_replay_conflict",
        )

    _require_asset(snapshot, asset_uuid)
    _check_new_anchor(anchor, context)
    return _changed(snapshot, schedules=_with_schedule(snapshot, record))


def edit_schedule(
    snapshot: MaintenanceSnapshot,
    request: EditScheduleRequest,
    context: MaintenanceMutationContext,
) -> MaintenanceMutationResult:
    """Replace name, enabled state, interval values, and reminder."""
    existing = _require_schedule(
        snapshot, _uuid("schedule_uuid", request.schedule_uuid)
    )
    calendar_interval = _calendar_interval(request.calendar_interval)
    runtime_interval = _runtime(
        "runtime_interval_seconds", request.runtime_interval_seconds, positive=True
    )
    if (calendar_interval is None) != (existing["calendar_interval"] is None) or (
        runtime_interval is None
    ) != (existing["runtime_interval_seconds"] is None):
        raise MaintenanceRequestError(
            "Adding or removing an interval type uses its own operation",
            code="maintenance_interval_type_change",
        )
    if _same_runtime(runtime_interval, existing["runtime_interval_seconds"]):
        # Numerically equal: keep the persisted spelling, write nothing new.
        runtime_interval = existing["runtime_interval_seconds"]
    reminder = _reminder(request.preparation_reminder)
    _require_reminder_calendar(reminder, calendar_interval)
    updated: MaintenanceScheduleData = {
        **existing,
        "name": _normalized("name", normalize_required_text, request.name),
        "enabled": _bool("enabled", request.enabled),
        "calendar_interval": calendar_interval,
        "runtime_interval_seconds": runtime_interval,
        "preparation_reminder": reminder,
    }
    if updated == existing:
        return _unchanged(MutationOutcome.NO_OP, snapshot)
    return _changed(snapshot, schedules=_with_schedule(snapshot, updated))


def _same_anchor(
    first: MaintenanceInitialAnchorData | None,
    second: MaintenanceInitialAnchorData | None,
) -> bool:
    if first is None or second is None:
        return first is None and second is None
    return first["date"] == second["date"] and _same_runtime(
        first["runtime_seconds"], second["runtime_seconds"]
    )


def _reject_locked(snapshot: MaintenanceSnapshot, schedule_uuid: str) -> None:
    if is_baseline_locked(snapshot.events, schedule_uuid):
        raise MaintenanceRequestError(
            f"The baseline of Maintenance Schedule {schedule_uuid} is locked",
            code="maintenance_baseline_locked",
        )


def set_initial_anchor(
    snapshot: MaintenanceSnapshot,
    request: SetInitialAnchorRequest,
    context: MaintenanceMutationContext,
) -> MaintenanceMutationResult:
    """Set, change, or clear the baseline while no Event has referenced it."""
    existing = _require_schedule(
        snapshot, _uuid("schedule_uuid", request.schedule_uuid)
    )
    anchor = _anchor(request.initial_anchor)
    _require_anchor_dimensions(
        anchor, existing["calendar_interval"], existing["runtime_interval_seconds"]
    )
    if _same_anchor(anchor, existing["initial_anchor"]):
        return _unchanged(MutationOutcome.NO_OP, snapshot)
    _reject_locked(snapshot, existing["schedule_uuid"])
    _check_new_anchor(anchor, context)
    updated: MaintenanceScheduleData = {**existing, "initial_anchor": anchor}
    return _changed(snapshot, schedules=_with_schedule(snapshot, updated))


def _dimension(value: Any) -> IntervalDimension:
    try:
        return IntervalDimension(value)
    except ValueError as err:
        raise _invalid("dimension", "must be calendar or runtime") from err


def add_interval(
    snapshot: MaintenanceSnapshot,
    request: AddIntervalRequest,
    context: MaintenanceMutationContext,
) -> MaintenanceMutationResult:
    """Add an interval type; a locked Schedule never gets a new baseline component."""
    existing = _require_schedule(
        snapshot, _uuid("schedule_uuid", request.schedule_uuid)
    )
    anchor = existing["initial_anchor"]
    same_value: bool
    if _dimension(request.dimension) is IntervalDimension.CALENDAR:
        if request.runtime_interval_seconds is not None:
            raise _invalid(
                "runtime_interval_seconds", "not allowed for a calendar interval"
            )
        calendar_interval = _calendar_interval(request.calendar_interval)
        if calendar_interval is None:
            raise _invalid("calendar_interval", "is required")
        component = (
            None
            if request.anchor_component is None
            else _civil_date("anchor_component", request.anchor_component)
        )
        current = existing["calendar_interval"]
        same_value = current == calendar_interval
        current_component = None if anchor is None else anchor["date"]
        same_component = component is None or component == current_component
        updates: dict[str, Any] = {"calendar_interval": calendar_interval}
        component_key = "date"
    else:
        if request.calendar_interval is not None:
            raise _invalid("calendar_interval", "not allowed for a Runtime interval")
        runtime_interval = _runtime(
            "runtime_interval_seconds", request.runtime_interval_seconds, positive=True
        )
        if runtime_interval is None:
            raise _invalid("runtime_interval_seconds", "is required")
        component = _runtime("anchor_component", request.anchor_component)
        current = existing["runtime_interval_seconds"]
        same_value = current is not None and _same_runtime(current, runtime_interval)
        current_component = None if anchor is None else anchor["runtime_seconds"]
        same_component = component is None or (
            current_component is not None
            and _same_runtime(component, current_component)
        )
        updates = {"runtime_interval_seconds": runtime_interval}
        component_key = "runtime_seconds"

    if current is not None:
        if same_value and same_component:
            return _unchanged(MutationOutcome.NO_OP, snapshot)
        raise MaintenanceRequestError(
            "The interval type is already configured; edit its value instead",
            code="maintenance_interval_exists",
        )

    if component is not None:
        _reject_locked(snapshot, existing["schedule_uuid"])
        if component_key == "date":
            _reject_future_date("anchor_component", component, context)
        else:
            _reject_runtime_above_current("anchor_component", component, context)
        new_anchor: MaintenanceInitialAnchorData = (
            {"date": None, "runtime_seconds": None} if anchor is None else {**anchor}
        )
        new_anchor[component_key] = component
        updates["initial_anchor"] = new_anchor
    updated: MaintenanceScheduleData = {**existing, **updates}
    return _changed(snapshot, schedules=_with_schedule(snapshot, updated))


def remove_interval(
    snapshot: MaintenanceSnapshot,
    request: RemoveIntervalRequest,
    context: MaintenanceMutationContext,
) -> MaintenanceMutationResult:
    """Remove an interval type and clean up its baseline component atomically."""
    existing = _require_schedule(
        snapshot, _uuid("schedule_uuid", request.schedule_uuid)
    )
    confirmed = _bool("confirm_destroy_baseline", request.confirm_destroy_baseline)
    if _dimension(request.dimension) is IntervalDimension.CALENDAR:
        interval_key, other_key, component_key = (
            "calendar_interval",
            "runtime_interval_seconds",
            "date",
        )
    else:
        interval_key, other_key, component_key = (
            "runtime_interval_seconds",
            "calendar_interval",
            "runtime_seconds",
        )
    if existing[interval_key] is None:
        return _unchanged(MutationOutcome.NO_OP, snapshot)
    if existing[other_key] is None:
        raise MaintenanceRequestError(
            "The last interval cannot be removed", code="maintenance_last_interval"
        )

    anchor = existing["initial_anchor"]
    new_anchor = anchor
    if anchor is not None and anchor[component_key] is not None:
        if (
            is_baseline_locked(snapshot.events, existing["schedule_uuid"])
            and not confirmed
        ):
            raise MaintenanceConfirmationRequiredError(
                "Removing this interval destroys a locked baseline component",
                confirmation=ConfirmationKind.DESTROY_BASELINE,
                schedule_uuids=(existing["schedule_uuid"],),
            )
        new_anchor = {**anchor, component_key: None}
        if new_anchor["date"] is None and new_anchor["runtime_seconds"] is None:
            new_anchor = None

    updated: MaintenanceScheduleData = {
        **existing,
        interval_key: None,
        "initial_anchor": new_anchor,
    }
    if component_key == "date":
        updated["preparation_reminder"] = None
    return _changed(snapshot, schedules=_with_schedule(snapshot, updated))


def set_enabled(
    snapshot: MaintenanceSnapshot,
    request: SetEnabledRequest,
    context: MaintenanceMutationContext,
) -> MaintenanceMutationResult:
    """Change only ``enabled``; history, baseline, and links are untouched."""
    existing = _require_schedule(
        snapshot, _uuid("schedule_uuid", request.schedule_uuid)
    )
    enabled = _bool("enabled", request.enabled)
    if existing["enabled"] is enabled:
        return _unchanged(MutationOutcome.NO_OP, snapshot)
    updated: MaintenanceScheduleData = {**existing, "enabled": enabled}
    return _changed(snapshot, schedules=_with_schedule(snapshot, updated))


def delete_schedule(
    snapshot: MaintenanceSnapshot,
    request: DeleteScheduleRequest,
    context: MaintenanceMutationContext,
) -> MaintenanceMutationResult:
    """Hard-delete an unreferenced Schedule. A missing Schedule fails closed.

    A delete replay cannot be proven without persisted metadata, so a
    missing UUID is NOT_FOUND, never a silent success.
    """
    schedule_uuid = _uuid("schedule_uuid", request.schedule_uuid)
    _require_schedule(snapshot, schedule_uuid)
    if not can_hard_delete_schedule(snapshot.events, schedule_uuid):
        raise MaintenanceRequestError(
            f"Maintenance Schedule {schedule_uuid} is referenced by an Event",
            code="maintenance_schedule_referenced",
        )
    schedules = {
        key: record
        for key, record in snapshot.schedules.items()
        if key != schedule_uuid
    }
    return _changed(snapshot, schedules=schedules)


# Event replay equality and guards


def event_business_key(event: Mapping[str, Any]) -> tuple[Any, ...]:
    """Return the replay-equality key of a canonical Event.

    Links compare as a set and Runtime numerically. Generated timestamps and
    the void state are excluded; the caller checks the state separately.
    """
    return (
        event["asset_uuid"],
        frozenset(event["schedule_uuids"]),
        event["title"],
        event["performed_date"],
        _decimal(event["runtime_seconds"]),
        event["notes"],
        event["corrects_event_uuid"],
    )


def _has_corrector(events: Mapping[str, MaintenanceEventData], event_uuid: str) -> bool:
    return any(event["corrects_event_uuid"] == event_uuid for event in events.values())


def _is_active_uncorrected(
    events: Mapping[str, MaintenanceEventData], event: MaintenanceEventData
) -> bool:
    return (
        event["voided_at"] is None
        and event["void_reason"] is None
        and not _has_corrector(events, event["event_uuid"])
    )


def _newly_locked(
    snapshot: MaintenanceSnapshot, links: Sequence[str]
) -> tuple[str, ...]:
    """Schedules with a baseline that no Event has referenced before."""
    referenced = referenced_schedule_uuids(snapshot.events)
    return tuple(
        sorted(
            schedule_uuid
            for schedule_uuid in links
            if schedule_uuid not in referenced
            and snapshot.schedules[schedule_uuid]["initial_anchor"] is not None
        )
    )


def _same_day_ambiguity(
    candidate_events: Mapping[str, MaintenanceEventData],
    links: Sequence[str],
    performed_date: str,
) -> tuple[str, ...]:
    """Schedules whose latest relevant date holds this Event and another one."""
    performed = parse_civil_date(performed_date)
    affected = []
    for schedule_uuid in links:
        grouped = latest_event_group(relevant_events(candidate_events, schedule_uuid))
        if grouped is not None and grouped[0] == performed and len(grouped[1]) > 1:
            affected.append(schedule_uuid)
    return tuple(sorted(affected))


def _require_baseline_confirmation(
    newly_locked: tuple[str, ...], confirmed: frozenset[str]
) -> None:
    missing = tuple(uuid for uuid in newly_locked if uuid not in confirmed)
    if missing:
        raise MaintenanceConfirmationRequiredError(
            "Saving this Event locks the starting point of a Maintenance Schedule",
            confirmation=ConfirmationKind.BASELINE_LOCK,
            schedule_uuids=missing,
        )


_PREFLIGHT_KEY = "preflight"


def preflight_event_guards(
    snapshot: MaintenanceSnapshot,
    schedule_uuids: Sequence[str],
    performed_date: Any,
    *,
    replacing_event_uuid: str | None = None,
) -> EventGuards:
    """Return the guards a new or corrected Event would raise, without saving.

    A flow calls this before the confirmation step. ``replacing_event_uuid``
    is the correction target, which the correction voids. The same guards
    are recomputed inside ``record_event`` and ``correct_event``.
    """
    links = _schedule_links(schedule_uuids)
    for schedule_uuid in links:
        _require_schedule(snapshot, schedule_uuid)
    performed = _civil_date("performed_date", performed_date)
    candidate: dict[str, Any] = {
        key: event
        for key, event in snapshot.events.items()
        if key != replacing_event_uuid
    }
    candidate[_PREFLIGHT_KEY] = {
        "schedule_uuids": links,
        "performed_date": performed,
        "voided_at": None,
    }
    return EventGuards(
        _newly_locked(snapshot, links), _same_day_ambiguity(candidate, links, performed)
    )


def _event_fields(
    schedule_uuids: Any,
    title: Any,
    performed_date: Any,
    runtime_seconds: Any,
    notes: Any,
) -> dict[str, Any]:
    """Normalize the business fields shared by Record and Correct."""
    return {
        "schedule_uuids": _schedule_links(schedule_uuids),
        "title": _normalized("title", normalize_required_text, title),
        "performed_date": _civil_date("performed_date", performed_date),
        "runtime_seconds": _runtime("runtime_seconds", runtime_seconds),
        "notes": _normalized("notes", normalize_optional_text, notes),
    }


def _replay_conflict(event_uuid: str) -> MaintenanceConflictError:
    return MaintenanceConflictError(
        f"Maintenance Event {event_uuid} already exists in another state",
        code="maintenance_replay_conflict",
    )


# Event mutations


def record_event(
    snapshot: MaintenanceSnapshot,
    request: RecordEventRequest,
    context: MaintenanceMutationContext,
) -> MaintenanceMutationResult:
    """Record an Event. Replay is checked before every state precondition."""
    event_uuid = _uuid("event_uuid", request.event_uuid)
    asset_uuid = _uuid("asset_uuid", request.asset_uuid)
    fields = _event_fields(
        request.schedule_uuids,
        request.title,
        request.performed_date,
        request.runtime_seconds,
        request.notes,
    )
    confirmed = _confirmed(request.confirmed_baseline_locks)

    existing = snapshot.events.get(event_uuid)
    if existing is not None:
        requested = {**fields, "asset_uuid": asset_uuid, "corrects_event_uuid": None}
        if event_business_key(existing) == event_business_key(
            requested
        ) and _is_active_uncorrected(snapshot.events, existing):
            return _unchanged(MutationOutcome.REPLAY, snapshot)
        raise _replay_conflict(event_uuid)

    _require_asset(snapshot, asset_uuid)
    _require_links(snapshot, asset_uuid, fields["schedule_uuids"])
    _reject_future_date("performed_date", fields["performed_date"], context)
    _reject_runtime_above_current("runtime_seconds", fields["runtime_seconds"], context)
    newly_locked = _newly_locked(snapshot, fields["schedule_uuids"])
    _require_baseline_confirmation(newly_locked, confirmed)

    record: MaintenanceEventData = {
        "event_uuid": event_uuid,
        "asset_uuid": asset_uuid,
        "schedule_uuids": fields["schedule_uuids"],
        "title": fields["title"],
        "performed_date": fields["performed_date"],
        "runtime_seconds": fields["runtime_seconds"],
        "recorded_at": context.observed_utc,
        "notes": fields["notes"],
        "voided_at": None,
        "void_reason": None,
        "corrects_event_uuid": None,
    }
    events = {**snapshot.events, event_uuid: record}
    guards = EventGuards(
        newly_locked,
        _same_day_ambiguity(events, record["schedule_uuids"], record["performed_date"]),
    )
    return _changed(snapshot, events=events, guards=guards)


def void_event(
    snapshot: MaintenanceSnapshot,
    request: VoidEventRequest,
    context: MaintenanceMutationContext,
) -> MaintenanceMutationResult:
    """Manually void an active Event; void is final and never clamped."""
    event_uuid = _uuid("event_uuid", request.event_uuid)
    reason = _normalized("void_reason", normalize_optional_text, request.void_reason)
    target = snapshot.events.get(event_uuid)
    if target is None:
        raise MaintenanceNotFoundError(
            f"Maintenance Event {event_uuid} does not exist",
            code="maintenance_event_not_found",
        )
    if target["voided_at"] is not None:
        # A correction-void is never a manual-void replay.
        if (
            not _has_corrector(snapshot.events, event_uuid)
            and target["void_reason"] == reason
        ):
            return _unchanged(MutationOutcome.REPLAY, snapshot)
        raise _replay_conflict(event_uuid)
    voided: MaintenanceEventData = {
        **target,
        "voided_at": context.observed_utc,
        "void_reason": reason,
    }
    return _changed(snapshot, events={**snapshot.events, event_uuid: voided})


def correct_event(
    snapshot: MaintenanceSnapshot,
    request: CorrectEventRequest,
    context: MaintenanceMutationContext,
) -> MaintenanceMutationResult:
    """Void the target and create its complete replacement in one change.

    Replay of ``new_event_uuid`` is checked before the target-active rule.
    Whether the Runtime changed is derived from the target's persisted value;
    an unchanged Runtime skips M-8 and keeps the target's exact string.
    """
    new_uuid = _uuid("new_event_uuid", request.new_event_uuid)
    target_uuid = _uuid("target_event_uuid", request.target_event_uuid)
    fields = _event_fields(
        request.schedule_uuids,
        request.title,
        request.performed_date,
        request.runtime_seconds,
        request.notes,
    )
    reason = _normalized("void_reason", normalize_optional_text, request.void_reason)
    confirmed = _confirmed(request.confirmed_baseline_locks)
    target = snapshot.events.get(target_uuid)

    existing = snapshot.events.get(new_uuid)
    if existing is not None:
        if (
            target is not None
            and event_business_key(existing)
            == event_business_key(
                {
                    **fields,
                    "asset_uuid": target["asset_uuid"],
                    "corrects_event_uuid": target_uuid,
                }
            )
            and target["voided_at"] is not None
            and target["void_reason"] == reason
            and _is_active_uncorrected(snapshot.events, existing)
        ):
            return _unchanged(MutationOutcome.REPLAY, snapshot)
        raise _replay_conflict(new_uuid)

    if target is None:
        raise MaintenanceNotFoundError(
            f"Maintenance Event {target_uuid} does not exist",
            code="maintenance_event_not_found",
        )
    if target["voided_at"] is not None:
        raise MaintenanceRequestError(
            f"Maintenance Event {target_uuid} is not active",
            code="maintenance_event_not_active",
        )
    asset_uuid = target["asset_uuid"]
    _require_links(snapshot, asset_uuid, fields["schedule_uuids"])
    _reject_future_date("performed_date", fields["performed_date"], context)

    runtime = fields["runtime_seconds"]
    old_runtime = target["runtime_seconds"]
    unchanged = (old_runtime is None and runtime is None) or (
        old_runtime is not None
        and runtime is not None
        and _same_runtime(old_runtime, runtime)
    )
    if unchanged:
        runtime = old_runtime
    else:
        _reject_runtime_above_current("runtime_seconds", runtime, context)

    newly_locked = _newly_locked(snapshot, fields["schedule_uuids"])
    _require_baseline_confirmation(newly_locked, confirmed)

    voided_target: MaintenanceEventData = {
        **target,
        "voided_at": context.observed_utc,
        "void_reason": reason,
    }
    corrected: MaintenanceEventData = {
        "event_uuid": new_uuid,
        "asset_uuid": asset_uuid,
        "schedule_uuids": fields["schedule_uuids"],
        "title": fields["title"],
        "performed_date": fields["performed_date"],
        "runtime_seconds": runtime,
        "recorded_at": context.observed_utc,
        "notes": fields["notes"],
        "voided_at": None,
        "void_reason": None,
        "corrects_event_uuid": target_uuid,
    }
    events = {**snapshot.events, target_uuid: voided_target, new_uuid: corrected}
    guards = EventGuards(
        newly_locked,
        _same_day_ambiguity(
            events, corrected["schedule_uuids"], corrected["performed_date"]
        ),
    )
    return _changed(snapshot, events=events, guards=guards)


_OPERATIONS: dict[type, Callable[..., MaintenanceMutationResult]] = {
    CreateScheduleRequest: create_schedule,
    EditScheduleRequest: edit_schedule,
    SetInitialAnchorRequest: set_initial_anchor,
    AddIntervalRequest: add_interval,
    RemoveIntervalRequest: remove_interval,
    SetEnabledRequest: set_enabled,
    DeleteScheduleRequest: delete_schedule,
    RecordEventRequest: record_event,
    VoidEventRequest: void_event,
    CorrectEventRequest: correct_event,
}


def mutate_maintenance(
    snapshot: MaintenanceSnapshot,
    request: Any,
    context: MaintenanceMutationContext,
) -> MaintenanceMutationResult:
    """Dispatch one request to its operation."""
    operation = _OPERATIONS.get(type(request))
    if operation is None:
        raise TypeError(f"Unknown Maintenance request: {type(request).__name__}")
    return operation(snapshot, request, context)
