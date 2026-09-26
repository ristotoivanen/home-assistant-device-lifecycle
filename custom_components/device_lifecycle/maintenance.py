"""Standalone load-time validation for the frozen Maintenance Store schema.

The authoritative schema is docs/maintenance-store-v4-schema.md. This module
validates persisted Maintenance Schedules and Events and derives reference
facts from them. It is library code only: Store 3.1 does not contain the
Maintenance collections, and nothing in production calls this module until
Store 4 activation.

Validation never reads the clock or the current Runtime, never writes or
normalizes data, and uses no Home Assistant state. Mutation-time rules such
as future-date rejection, the current-Runtime upper bound, and the baseline
lock are enforced by the mutation layer, not here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any

from .canonical import (
    CanonicalValueError,
    optional_text,
    parse_canonical_decimal,
    parse_canonical_utc,
    parse_civil_date,
    require_canonical_uuid,
    require_text,
)
from .models import MaintenanceEventData, MaintenanceScheduleData

SCHEDULE_KEYS = frozenset(
    {
        "schedule_uuid",
        "asset_uuid",
        "name",
        "enabled",
        "calendar_interval",
        "runtime_interval_seconds",
        "initial_anchor",
        "preparation_reminder",
    }
)
EVENT_KEYS = frozenset(
    {
        "event_uuid",
        "asset_uuid",
        "schedule_uuids",
        "title",
        "performed_date",
        "runtime_seconds",
        "recorded_at",
        "notes",
        "voided_at",
        "void_reason",
        "corrects_event_uuid",
    }
)
CALENDAR_INTERVAL_KEYS = frozenset({"value", "unit"})
INITIAL_ANCHOR_KEYS = frozenset({"date", "runtime_seconds"})
PREPARATION_REMINDER_KEYS = frozenset({"lead_days", "message"})
CALENDAR_INTERVAL_UNITS = frozenset({"days", "months", "years"})

MAINTENANCE_COLLECTION_KEYS = ("maintenance_schedules", "maintenance_events")


class MaintenanceValidationError(ValueError):
    """Persisted Maintenance data violates the frozen schema."""


class MaintenanceCompositionError(ValueError):
    """A Store candidate cannot receive the Maintenance collections."""


def _canonical[T](
    context: str,
    field: str,
    parser: Callable[..., T],
    value: Any,
    **kwargs: Any,
) -> T:
    """Run one canonical parser and name the record and field on failure."""
    try:
        return parser(value, **kwargs)
    except CanonicalValueError as err:
        raise MaintenanceValidationError(f"{context}: invalid {field}: {err}") from err


def _require_exact_keys(
    context: str,
    record: Any,
    expected: frozenset[str],
) -> Mapping[str, Any]:
    """Require a mapping with exactly the expected keys."""
    if not isinstance(record, dict):
        raise MaintenanceValidationError(f"{context}: record must be a mapping")
    keys = set(record)
    if keys != expected:
        missing = sorted(expected - keys)
        unexpected = sorted(str(key) for key in keys - expected)
        raise MaintenanceValidationError(
            f"{context}: invalid shape: missing={missing}, unexpected={unexpected}"
        )
    return record


def _require_positive_int(context: str, field: str, value: Any) -> int:
    """Require an exact int, never a bool, greater than zero."""
    if type(value) is not int or value <= 0:
        raise MaintenanceValidationError(
            f"{context}: {field} must be an integer greater than zero"
        )
    return value


def _require_identity(
    context: str,
    key: Any,
    record: Mapping[str, Any],
    uuid_field: str,
) -> str:
    """Require a canonical map key that equals the record's own UUID."""
    map_key = _canonical(context, "map key", require_canonical_uuid, key)
    record_uuid = _canonical(context, uuid_field, require_canonical_uuid, record[uuid_field])
    if record_uuid != map_key:
        raise MaintenanceValidationError(
            f"{context}: {uuid_field} does not match its map key"
        )
    return map_key


def _require_asset(
    context: str,
    record: Mapping[str, Any],
    assets: Mapping[str, Any],
) -> str:
    """Require a canonical Asset UUID that exists in the supplied Assets."""
    asset_uuid = _canonical(context, "asset_uuid", require_canonical_uuid, record["asset_uuid"])
    if asset_uuid not in assets:
        raise MaintenanceValidationError(f"{context}: Asset does not exist")
    return asset_uuid


def validate_maintenance_schedule(
    key: Any,
    record: Any,
    assets: Mapping[str, Any],
) -> None:
    """Validate one persisted Maintenance Schedule against the frozen schema."""
    context = f"Maintenance Schedule {key}"
    schedule = _require_exact_keys(context, record, SCHEDULE_KEYS)
    _require_identity(context, key, schedule, "schedule_uuid")
    _require_asset(context, schedule, assets)
    _canonical(context, "name", require_text, schedule["name"])
    if type(schedule["enabled"]) is not bool:
        raise MaintenanceValidationError(f"{context}: enabled must be a bool")

    calendar_interval = schedule["calendar_interval"]
    if calendar_interval is not None:
        interval = _require_exact_keys(
            f"{context} calendar_interval",
            calendar_interval,
            CALENDAR_INTERVAL_KEYS,
        )
        _require_positive_int(context, "calendar_interval.value", interval["value"])
        if (
            not isinstance(interval["unit"], str)
            or interval["unit"] not in CALENDAR_INTERVAL_UNITS
        ):
            raise MaintenanceValidationError(
                f"{context}: calendar_interval.unit must be days, months, or years"
            )

    runtime_interval = schedule["runtime_interval_seconds"]
    if runtime_interval is not None:
        _canonical(
            context,
            "runtime_interval_seconds",
            parse_canonical_decimal,
            runtime_interval,
            positive=True,
        )

    if calendar_interval is None and runtime_interval is None:
        raise MaintenanceValidationError(f"{context}: at least one interval is required")

    initial_anchor = schedule["initial_anchor"]
    if initial_anchor is not None:
        anchor = _require_exact_keys(
            f"{context} initial_anchor",
            initial_anchor,
            INITIAL_ANCHOR_KEYS,
        )
        if anchor["date"] is None and anchor["runtime_seconds"] is None:
            raise MaintenanceValidationError(
                f"{context}: initial_anchor must have at least one known component"
            )
        if anchor["date"] is not None:
            _canonical(context, "initial_anchor.date", parse_civil_date, anchor["date"])
            if calendar_interval is None:
                raise MaintenanceValidationError(
                    f"{context}: initial_anchor.date requires a calendar interval"
                )
        if anchor["runtime_seconds"] is not None:
            _canonical(
                context,
                "initial_anchor.runtime_seconds",
                parse_canonical_decimal,
                anchor["runtime_seconds"],
            )
            if runtime_interval is None:
                raise MaintenanceValidationError(
                    f"{context}: initial_anchor.runtime_seconds requires a "
                    "Runtime interval"
                )

    preparation_reminder = schedule["preparation_reminder"]
    if preparation_reminder is not None:
        reminder = _require_exact_keys(
            f"{context} preparation_reminder",
            preparation_reminder,
            PREPARATION_REMINDER_KEYS,
        )
        _require_positive_int(context, "preparation_reminder.lead_days", reminder["lead_days"])
        _canonical(
            context,
            "preparation_reminder.message",
            optional_text,
            reminder["message"],
        )
        if calendar_interval is None:
            raise MaintenanceValidationError(
                f"{context}: preparation_reminder requires a calendar interval"
            )


def validate_maintenance_event(
    key: Any,
    record: Any,
    assets: Mapping[str, Any],
    schedules: Mapping[str, MaintenanceScheduleData],
) -> None:
    """Validate one persisted Maintenance Event against the frozen schema.

    The correction graph across Events is validated separately.
    """
    context = f"Maintenance Event {key}"
    event = _require_exact_keys(context, record, EVENT_KEYS)
    _require_identity(context, key, event, "event_uuid")
    asset_uuid = _require_asset(context, event, assets)

    schedule_uuids = event["schedule_uuids"]
    if type(schedule_uuids) is not list:
        raise MaintenanceValidationError(f"{context}: schedule_uuids must be a list")
    seen: set[str] = set()
    for value in schedule_uuids:
        schedule_uuid = _canonical(context, "schedule_uuids", require_canonical_uuid, value)
        if schedule_uuid in seen:
            raise MaintenanceValidationError(
                f"{context}: schedule_uuids contains a duplicate"
            )
        seen.add(schedule_uuid)
        schedule = schedules.get(schedule_uuid)
        if schedule is None:
            raise MaintenanceValidationError(
                f"{context}: references missing Maintenance Schedule {schedule_uuid}"
            )
        if schedule["asset_uuid"] != asset_uuid:
            raise MaintenanceValidationError(
                f"{context}: references Maintenance Schedule {schedule_uuid} "
                "of another Asset"
            )

    _canonical(context, "title", require_text, event["title"])
    _canonical(context, "performed_date", parse_civil_date, event["performed_date"])
    if event["runtime_seconds"] is not None:
        _canonical(context, "runtime_seconds", parse_canonical_decimal, event["runtime_seconds"])
    _canonical(context, "recorded_at", parse_canonical_utc, event["recorded_at"])
    _canonical(context, "notes", optional_text, event["notes"])
    # No ordering between recorded_at and voided_at: both are observed audit
    # timestamps, not ordering keys.
    if event["voided_at"] is not None:
        _canonical(context, "voided_at", parse_canonical_utc, event["voided_at"])
    _canonical(context, "void_reason", optional_text, event["void_reason"])
    if event["void_reason"] is not None and event["voided_at"] is None:
        raise MaintenanceValidationError(
            f"{context}: void_reason requires voided_at"
        )
    if event["corrects_event_uuid"] is not None:
        _canonical(
            context,
            "corrects_event_uuid",
            require_canonical_uuid,
            event["corrects_event_uuid"],
        )


def validate_correction_graph(events: Mapping[str, MaintenanceEventData]) -> None:
    """Validate the correction links between already validated Events.

    Every link must reference another existing, voided Event of the same
    Asset; each Event has at most one corrector; and the graph is acyclic.
    Each Event has at most one outgoing link, so following every chain once
    with a visited state is O(E). No timestamp or ordering field is used.
    """
    correctors: dict[str, str] = {}
    for event_uuid in sorted(events):
        event = events[event_uuid]
        target_uuid = event["corrects_event_uuid"]
        if target_uuid is None:
            continue
        context = f"Correction graph: Maintenance Event {event_uuid}"
        if target_uuid == event_uuid:
            raise MaintenanceValidationError(f"{context} corrects itself")
        target = events.get(target_uuid)
        if target is None:
            raise MaintenanceValidationError(
                f"{context} corrects missing Maintenance Event {target_uuid}"
            )
        if target["asset_uuid"] != event["asset_uuid"]:
            raise MaintenanceValidationError(
                f"{context} corrects Maintenance Event {target_uuid} of another Asset"
            )
        if target["voided_at"] is None:
            raise MaintenanceValidationError(
                f"{context} corrects Maintenance Event {target_uuid}, which is not voided"
            )
        if target_uuid in correctors:
            raise MaintenanceValidationError(
                f"Correction graph: Maintenance Event {target_uuid} has more than "
                "one correcting Event"
            )
        correctors[target_uuid] = event_uuid

    finished: set[str] = set()
    for start in sorted(events):
        path: list[str] = []
        on_path: set[str] = set()
        current: str | None = start
        while current is not None and current not in finished:
            if current in on_path:
                raise MaintenanceValidationError(
                    f"Correction graph: cycle through Maintenance Event {current}"
                )
            on_path.add(current)
            path.append(current)
            current = events[current]["corrects_event_uuid"]
        finished.update(path)


def validate_maintenance_collections(
    assets: Mapping[str, Any],
    maintenance_schedules: Any,
    maintenance_events: Any,
) -> None:
    """Validate the complete Maintenance collections of one Store candidate.

    ``assets`` is the Store's Asset mapping; only Asset UUID membership is
    read. The collections are validated in order: Schedules, Events with
    their Schedule references, then the correction graph.
    """
    if not isinstance(maintenance_schedules, dict):
        raise MaintenanceValidationError("maintenance_schedules must be a mapping")
    if not isinstance(maintenance_events, dict):
        raise MaintenanceValidationError("maintenance_events must be a mapping")
    for key, record in maintenance_schedules.items():
        validate_maintenance_schedule(key, record, assets)
    for key, record in maintenance_events.items():
        validate_maintenance_event(key, record, assets, maintenance_schedules)
    validate_correction_graph(maintenance_events)


def referenced_schedule_uuids(
    events: Mapping[str, MaintenanceEventData],
) -> set[str]:
    """Return every Schedule UUID referenced by any Event, voided or not."""
    return {
        schedule_uuid
        for event in events.values()
        for schedule_uuid in event["schedule_uuids"]
    }


def is_baseline_locked(
    events: Mapping[str, MaintenanceEventData],
    schedule_uuid: str,
) -> bool:
    """Return whether a Schedule's initial_anchor is locked.

    The baseline locks once any Event has referenced the Schedule. Voided
    Events and corrected Events keep their references, so voiding or
    correcting never unlocks it.
    """
    return schedule_uuid in referenced_schedule_uuids(events)


def can_hard_delete_schedule(
    events: Mapping[str, MaintenanceEventData],
    schedule_uuid: str,
) -> bool:
    """Return whether no Event, including a voided Event, references a Schedule."""
    return schedule_uuid not in referenced_schedule_uuids(events)


def add_maintenance_collections(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of a pre-Maintenance Store candidate with empty collections.

    This is only the Maintenance component of the future Store upgrade; it
    does not own the upgrade order, and it is not called by any migration
    before Store 4 activation. Every existing top-level value is carried over
    unchanged and nothing is inferred: no Schedule, Event, or baseline is
    derived from Purchases, Lifecycle, Runtime, or Deployment. The input is
    never modified, and the result shares no mutable data with it.

    It applies exactly once. A candidate that already has either Maintenance
    collection is rejected rather than overwritten.
    """
    if not isinstance(candidate, dict):
        raise MaintenanceCompositionError("Store candidate must be a mapping")
    present = [key for key in MAINTENANCE_COLLECTION_KEYS if key in candidate]
    if present:
        raise MaintenanceCompositionError(
            f"Store candidate already contains Maintenance collections: {present}"
        )
    result = deepcopy(candidate)
    result["maintenance_schedules"] = {}
    result["maintenance_events"] = {}
    return result
