"""Standalone load-time validation of the frozen Maintenance Store schema."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from custom_components.device_lifecycle.maintenance import (
    EVENT_KEYS,
    SCHEDULE_KEYS,
    MaintenanceValidationError,
    can_hard_delete_schedule,
    is_baseline_locked,
    referenced_schedule_uuids,
    validate_correction_graph,
    validate_maintenance_collections,
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
SCHEDULE_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb3"
MISSING_SCHEDULE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb9"
EVENT_1 = "cccccccc-cccc-4ccc-8ccc-ccccccccccc1"
EVENT_2 = "cccccccc-cccc-4ccc-8ccc-ccccccccccc2"
EVENT_3 = "cccccccc-cccc-4ccc-8ccc-ccccccccccc3"
EVENT_B = "cccccccc-cccc-4ccc-8ccc-ccccccccccc4"
MISSING_EVENT = "cccccccc-cccc-4ccc-8ccc-ccccccccccc9"
RECORDED = "2026-09-26T12:00:00+00:00"
VOIDED = "2026-09-26T13:00:00+00:00"

ASSETS: dict[str, Any] = {ASSET_A: {}, ASSET_B: {}}


def _schedule(key: str, owner: str = ASSET_A, /, **fields: Any) -> dict:
    """Return a valid combined Schedule, optionally overriding fields."""
    record: dict[str, Any] = {
        "schedule_uuid": key,
        "asset_uuid": owner,
        "name": "Ventilation filter change",
        "enabled": True,
        "calendar_interval": {"value": 6, "unit": "months"},
        "runtime_interval_seconds": "1800000",
        "initial_anchor": {"date": "2026-06-01", "runtime_seconds": "3600000"},
        "preparation_reminder": {
            "lead_days": 30,
            "message": "Remember to order new filters",
        },
    }
    record.update(fields)
    return record


_DEFAULT_LINKS = object()


def _event(
    key: str,
    owner: str = ASSET_A,
    links: Any = _DEFAULT_LINKS,
    /,
    **fields: Any,
) -> dict:
    """Return a valid active Event, optionally overriding fields."""
    record: dict[str, Any] = {
        "event_uuid": key,
        "asset_uuid": owner,
        "schedule_uuids": [SCHEDULE_1] if links is _DEFAULT_LINKS else links,
        "title": "Ventilation filter change",
        "performed_date": "2026-09-26",
        "runtime_seconds": "5400000",
        "recorded_at": RECORDED,
        "notes": None,
        "voided_at": None,
        "void_reason": None,
        "corrects_event_uuid": None,
    }
    record.update(fields)
    return record


def _schedules() -> dict[str, dict]:
    return {
        SCHEDULE_1: _schedule(SCHEDULE_1),
        SCHEDULE_2: _schedule(SCHEDULE_2),
        SCHEDULE_B: _schedule(SCHEDULE_B, ASSET_B),
    }


def _validate(schedules: dict, events: dict | None = None) -> None:
    validate_maintenance_collections(ASSETS, schedules, events or {})


def _validate_schedule(record: Any, key: Any = SCHEDULE_1) -> None:
    _validate({key: record})


def _validate_events(*events: dict) -> None:
    _validate(_schedules(), {event["event_uuid"]: event for event in events})


def _reject_schedule(record: Any, match: str, key: Any = SCHEDULE_1) -> None:
    with pytest.raises(MaintenanceValidationError, match=match):
        _validate_schedule(record, key)


def _reject_events(match: str, *events: dict) -> None:
    with pytest.raises(MaintenanceValidationError, match=match):
        _validate_events(*events)


# Collections and exact shape


def test_empty_collections_are_valid() -> None:
    """No Schedules and no Events is the migrated starting state."""
    validate_maintenance_collections(ASSETS, {}, {})
    validate_maintenance_collections({}, {}, {})


@pytest.mark.parametrize(
    ("schedules", "events"),
    [([], {}), ({}, []), (None, {}), ({}, None)],
)
def test_collections_must_be_mappings(schedules: Any, events: Any) -> None:
    """Both top-level Maintenance collections are UUID-keyed mappings."""
    with pytest.raises(MaintenanceValidationError, match="must be a mapping"):
        validate_maintenance_collections(ASSETS, schedules, events)


def test_valid_schedule_and_event_records_pass() -> None:
    """A complete valid Schedule and Event pass with their exact key sets."""
    schedule = _schedule(SCHEDULE_1)
    event = _event(EVENT_1)
    assert set(schedule) == SCHEDULE_KEYS
    assert set(event) == EVENT_KEYS
    _validate_events(event)


@pytest.mark.parametrize("key", sorted(SCHEDULE_KEYS))
def test_schedule_missing_key_is_rejected(key: str) -> None:
    record = _schedule(SCHEDULE_1)
    del record[key]
    _reject_schedule(record, rf"missing=\['{key}'\]")


def test_schedule_extra_key_is_rejected() -> None:
    record = _schedule(SCHEDULE_1, baseline_locked=True)
    _reject_schedule(record, r"unexpected=\['baseline_locked'\]")


@pytest.mark.parametrize("key", sorted(EVENT_KEYS))
def test_event_missing_key_is_rejected(key: str) -> None:
    event = _event(EVENT_1)
    del event[key]
    with pytest.raises(MaintenanceValidationError, match=rf"missing=\['{key}'\]"):
        _validate(_schedules(), {EVENT_1: event})


def test_event_extra_key_is_rejected() -> None:
    _reject_events(r"unexpected=\['sequence'\]", _event(EVENT_1, sequence=1))


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("calendar_interval", {"value": 6}, r"calendar_interval: invalid shape"),
        (
            "calendar_interval",
            {"value": 6, "unit": "months", "days": 180},
            r"unexpected=\['days'\]",
        ),
        ("initial_anchor", {"date": "2026-06-01"}, r"initial_anchor: invalid shape"),
        (
            "initial_anchor",
            {"date": "2026-06-01", "runtime_seconds": None, "locked": True},
            r"unexpected=\['locked'\]",
        ),
        (
            "preparation_reminder",
            {"lead_days": 30},
            r"preparation_reminder: invalid shape",
        ),
        (
            "preparation_reminder",
            {"lead_days": 30, "message": None, "active": False},
            r"unexpected=\['active'\]",
        ),
        ("calendar_interval", [6, "months"], "record must be a mapping"),
    ],
)
def test_nested_objects_have_exact_shapes(field: str, value: Any, match: str) -> None:
    _reject_schedule(_schedule(SCHEDULE_1, **{field: value}), match)


@pytest.mark.parametrize("record", ["schedule", None, [], 1])
def test_schedule_record_must_be_a_mapping(record: Any) -> None:
    _reject_schedule(record, "record must be a mapping")


def test_event_record_must_be_a_mapping() -> None:
    with pytest.raises(MaintenanceValidationError, match="record must be a mapping"):
        _validate(_schedules(), {EVENT_1: "event"})


# Identity and Asset ownership


@pytest.mark.parametrize(
    "key",
    [SCHEDULE_1.upper(), "not-a-uuid", "{" + SCHEDULE_1 + "}"],
)
def test_schedule_map_key_must_be_canonical(key: str) -> None:
    _reject_schedule(_schedule(SCHEDULE_1), "invalid map key", key)


def test_schedule_uuid_must_match_its_map_key() -> None:
    _reject_schedule(_schedule(SCHEDULE_2), "does not match its map key")


def test_schedule_uuid_must_be_canonical() -> None:
    record = _schedule(SCHEDULE_1, schedule_uuid=SCHEDULE_1.upper())
    _reject_schedule(record, "invalid schedule_uuid")


def test_event_uuid_must_match_its_canonical_map_key() -> None:
    with pytest.raises(MaintenanceValidationError, match="does not match its map key"):
        _validate(_schedules(), {EVENT_2: _event(EVENT_1)})
    with pytest.raises(MaintenanceValidationError, match="invalid map key"):
        _validate(_schedules(), {EVENT_1.upper(): _event(EVENT_1)})
    with pytest.raises(MaintenanceValidationError, match="invalid event_uuid"):
        _validate(_schedules(), {EVENT_1: _event(EVENT_1, event_uuid="bad")})


def test_schedule_asset_must_exist() -> None:
    _reject_schedule(_schedule(SCHEDULE_1, MISSING_ASSET), "Asset does not exist")
    _reject_schedule(_schedule(SCHEDULE_1, "bad"), "invalid asset_uuid")


def test_event_asset_must_exist() -> None:
    _reject_events("Asset does not exist", _event(EVENT_1, MISSING_ASSET, []))
    _reject_events("invalid asset_uuid", _event(EVENT_1, ASSET_A.upper(), []))


# Schedule fields


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_enabled_must_be_an_exact_bool(value: Any) -> None:
    _reject_schedule(_schedule(SCHEDULE_1, enabled=value), "enabled must be a bool")


def test_disabled_schedule_is_valid() -> None:
    _validate_schedule(_schedule(SCHEDULE_1, enabled=False))


@pytest.mark.parametrize("name", ["", " ", " Filter", "Filter ", None, 7])
def test_name_must_be_canonical_required_text(name: Any) -> None:
    _reject_schedule(_schedule(SCHEDULE_1, name=name), "invalid name")


def test_at_least_one_interval_is_required() -> None:
    record = _schedule(
        SCHEDULE_1,
        calendar_interval=None,
        runtime_interval_seconds=None,
        initial_anchor=None,
        preparation_reminder=None,
    )
    _reject_schedule(record, "at least one interval is required")


def test_calendar_only_and_runtime_only_schedules_are_valid() -> None:
    _validate_schedule(
        _schedule(
            SCHEDULE_1,
            runtime_interval_seconds=None,
            initial_anchor={"date": "2026-06-01", "runtime_seconds": None},
        )
    )
    _validate_schedule(
        _schedule(
            SCHEDULE_1,
            calendar_interval=None,
            initial_anchor={"date": None, "runtime_seconds": "3600000"},
            preparation_reminder=None,
        )
    )


@pytest.mark.parametrize("value", [0, -1, True, False, 1.0, "6", None])
def test_calendar_interval_value_must_be_positive_int(value: Any) -> None:
    record = _schedule(SCHEDULE_1, calendar_interval={"value": value, "unit": "days"})
    _reject_schedule(record, "calendar_interval.value must be an integer")


@pytest.mark.parametrize("unit", ["weeks", "day", "Months", "", None, 1])
def test_calendar_interval_unit_is_restricted(unit: Any) -> None:
    record = _schedule(SCHEDULE_1, calendar_interval={"value": 1, "unit": unit})
    _reject_schedule(record, "calendar_interval.unit must be")


@pytest.mark.parametrize("unit", ["days", "months", "years"])
def test_calendar_interval_has_no_upper_bound(unit: str) -> None:
    record = _schedule(SCHEDULE_1, calendar_interval={"value": 10**9, "unit": unit})
    _validate_schedule(record)


@pytest.mark.parametrize(
    "value",
    ["0", "0.0", "-1", "-0", "3.6E+3", "+3600", "03600", " 3600", "NaN", 3600, 1.5],
)
def test_runtime_interval_must_be_positive_canonical_decimal(value: Any) -> None:
    record = _schedule(SCHEDULE_1, runtime_interval_seconds=value)
    _reject_schedule(record, "invalid runtime_interval_seconds")


@pytest.mark.parametrize("value", ["0.5", "3600", "3600.0"])
def test_runtime_interval_accepts_positive_plain_decimals(value: str) -> None:
    _validate_schedule(_schedule(SCHEDULE_1, runtime_interval_seconds=value))


# Initial anchor


@pytest.mark.parametrize(
    "anchor",
    [
        None,
        {"date": "2026-06-01", "runtime_seconds": None},
        {"date": None, "runtime_seconds": "0"},
        {"date": "2026-06-01", "runtime_seconds": "3600000.5"},
    ],
)
def test_valid_initial_anchors(anchor: Any) -> None:
    _validate_schedule(_schedule(SCHEDULE_1, initial_anchor=anchor))


def test_initial_anchor_with_no_known_component_is_rejected() -> None:
    record = _schedule(
        SCHEDULE_1,
        initial_anchor={"date": None, "runtime_seconds": None},
    )
    _reject_schedule(record, "at least one known component")


def test_initial_anchor_date_requires_calendar_interval() -> None:
    record = _schedule(
        SCHEDULE_1,
        calendar_interval=None,
        initial_anchor={"date": "2026-06-01", "runtime_seconds": None},
        preparation_reminder=None,
    )
    _reject_schedule(record, "initial_anchor.date requires a calendar interval")


def test_initial_anchor_runtime_requires_runtime_interval() -> None:
    record = _schedule(
        SCHEDULE_1,
        runtime_interval_seconds=None,
        initial_anchor={"date": None, "runtime_seconds": "3600000"},
    )
    _reject_schedule(
        record,
        "initial_anchor.runtime_seconds requires a Runtime interval",
    )


@pytest.mark.parametrize("value", ["2026-02-30", "20260601", "2026-W22-1", 20260601])
def test_initial_anchor_date_must_be_canonical(value: Any) -> None:
    record = _schedule(
        SCHEDULE_1,
        initial_anchor={"date": value, "runtime_seconds": None},
    )
    _reject_schedule(record, "invalid initial_anchor.date")


@pytest.mark.parametrize("value", ["-1", "-0", "3.6E+3", "03600", 3600])
def test_initial_anchor_runtime_must_be_canonical(value: Any) -> None:
    record = _schedule(
        SCHEDULE_1,
        initial_anchor={"date": None, "runtime_seconds": value},
    )
    _reject_schedule(record, "invalid initial_anchor.runtime_seconds")


def test_future_initial_anchor_date_is_structurally_valid() -> None:
    """The future-date rule is mutation-time only."""
    record = _schedule(
        SCHEDULE_1,
        initial_anchor={"date": "9999-12-31", "runtime_seconds": None},
    )
    _validate_schedule(record)


# Preparation reminder


@pytest.mark.parametrize("message", [None, "Order filters"])
def test_valid_preparation_reminders(message: Any) -> None:
    record = _schedule(
        SCHEDULE_1,
        preparation_reminder={"lead_days": 1, "message": message},
    )
    _validate_schedule(record)


def test_preparation_reminder_requires_calendar_interval() -> None:
    record = _schedule(
        SCHEDULE_1,
        calendar_interval=None,
        initial_anchor={"date": None, "runtime_seconds": "3600000"},
    )
    _reject_schedule(record, "preparation_reminder requires a calendar interval")


@pytest.mark.parametrize("lead_days", [0, -1, True, 1.0, "30", None])
def test_preparation_lead_days_must_be_positive_int(lead_days: Any) -> None:
    record = _schedule(
        SCHEDULE_1,
        preparation_reminder={"lead_days": lead_days, "message": None},
    )
    _reject_schedule(record, "preparation_reminder.lead_days must be an integer")


@pytest.mark.parametrize("message", ["", "   ", " Order", 5])
def test_preparation_message_must_be_canonical_optional_text(message: Any) -> None:
    record = _schedule(
        SCHEDULE_1,
        preparation_reminder={"lead_days": 30, "message": message},
    )
    _reject_schedule(record, "invalid preparation_reminder.message")


# Event Schedule links


@pytest.mark.parametrize(
    "schedule_uuids",
    [[], [SCHEDULE_1], [SCHEDULE_1, SCHEDULE_2], [SCHEDULE_2, SCHEDULE_1]],
)
def test_valid_event_schedule_links(schedule_uuids: list[str]) -> None:
    """Zero links is ad-hoc maintenance; order has no meaning."""
    _validate_events(_event(EVENT_1, schedule_uuids=schedule_uuids))


def test_event_may_reference_a_disabled_schedule() -> None:
    schedules = _schedules()
    schedules[SCHEDULE_1]["enabled"] = False
    _validate(schedules, {EVENT_1: _event(EVENT_1)})


def test_duplicate_schedule_link_is_rejected() -> None:
    _reject_events("contains a duplicate", _event(EVENT_1, schedule_uuids=[SCHEDULE_1, SCHEDULE_1]))


def test_missing_schedule_link_is_rejected() -> None:
    _reject_events(
        "references missing Maintenance Schedule",
        _event(EVENT_1, schedule_uuids=[MISSING_SCHEDULE]),
    )


def test_cross_asset_schedule_link_is_rejected() -> None:
    _reject_events("of another Asset", _event(EVENT_1, schedule_uuids=[SCHEDULE_B]))


@pytest.mark.parametrize(
    "schedule_uuids",
    [(SCHEDULE_1,), {SCHEDULE_1}, SCHEDULE_1, None],
)
def test_schedule_links_must_be_an_exact_list(schedule_uuids: Any) -> None:
    _reject_events(
        "schedule_uuids must be a list",
        _event(EVENT_1, schedule_uuids=schedule_uuids),
    )


@pytest.mark.parametrize("value", [SCHEDULE_1.upper(), "bad", 1])
def test_schedule_links_must_be_canonical_uuids(value: Any) -> None:
    _reject_events("invalid schedule_uuids", _event(EVENT_1, schedule_uuids=[value]))


# Event values


@pytest.mark.parametrize("title", ["", " ", " Filter", "Filter\n", None])
def test_event_title_must_be_canonical_required_text(title: Any) -> None:
    _reject_events("invalid title", _event(EVENT_1, title=title))


@pytest.mark.parametrize("performed_date", ["2001-01-01", "9999-12-31"])
def test_backdated_and_future_performed_dates_are_structurally_valid(
    performed_date: str,
) -> None:
    """No date is compared with today at load."""
    _validate_events(_event(EVENT_1, performed_date=performed_date))


@pytest.mark.parametrize("performed_date", ["2026-02-30", "20260926", None])
def test_performed_date_must_be_canonical(performed_date: Any) -> None:
    _reject_events("invalid performed_date", _event(EVENT_1, performed_date=performed_date))


@pytest.mark.parametrize("runtime", [None, "0", "0.0", "5400000", "5400000.25"])
def test_valid_event_runtime_snapshots(runtime: Any) -> None:
    _validate_events(_event(EVENT_1, runtime_seconds=runtime))


@pytest.mark.parametrize("runtime", ["-1", "-0", "5.4E+6", "05400000", 5400000, "NaN"])
def test_event_runtime_must_be_canonical(runtime: Any) -> None:
    _reject_events("invalid runtime_seconds", _event(EVENT_1, runtime_seconds=runtime))


def test_runtime_monotonicity_is_not_a_load_invariant() -> None:
    """A later Event with a lower Runtime is accepted; projection handles it."""
    _validate_events(
        _event(EVENT_1, performed_date="2026-01-01", runtime_seconds="9000000"),
        _event(EVENT_2, performed_date="2026-06-01", runtime_seconds="100"),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("recorded_at", "2026-09-26T12:00:00Z"),
        ("recorded_at", "2026-09-26T12:00:00"),
        ("recorded_at", None),
        ("voided_at", "2026-09-26T13:00:00.000000+00:00"),
        ("voided_at", "2026-09-26T15:00:00+02:00"),
    ],
)
def test_event_timestamps_must_be_canonical_utc(field: str, value: Any) -> None:
    _reject_events(f"invalid {field}", _event(EVENT_1, **{field: value}))


def test_voided_at_before_recorded_at_is_accepted() -> None:
    """Timestamps are observed audit values, not ordering keys."""
    _validate_events(
        _event(
            EVENT_1,
            recorded_at="2026-09-26T12:00:00.123456+00:00",
            voided_at="2020-01-01T00:00:00+00:00",
        )
    )


@pytest.mark.parametrize("notes", [None, "Replaced both filters"])
def test_valid_event_notes(notes: Any) -> None:
    _validate_events(_event(EVENT_1, notes=notes))


@pytest.mark.parametrize("notes", ["", "  ", "note ", 3])
def test_event_notes_must_be_canonical_optional_text(notes: Any) -> None:
    _reject_events("invalid notes", _event(EVENT_1, notes=notes))


def test_void_without_reason_is_accepted() -> None:
    _validate_events(_event(EVENT_1, voided_at=VOIDED, void_reason=None))
    _validate_events(_event(EVENT_1, voided_at=VOIDED, void_reason="Duplicate entry"))


def test_void_reason_requires_voided_at() -> None:
    _reject_events("void_reason requires voided_at", _event(EVENT_1, void_reason="Mistake"))


@pytest.mark.parametrize("reason", ["", " ", "Mistake "])
def test_void_reason_must_be_canonical_text(reason: str) -> None:
    _reject_events("invalid void_reason", _event(EVENT_1, voided_at=VOIDED, void_reason=reason))


@pytest.mark.parametrize("value", [EVENT_2.upper(), "bad", 1])
def test_corrects_event_uuid_must_be_canonical(value: Any) -> None:
    _reject_events("invalid corrects_event_uuid", _event(EVENT_1, corrects_event_uuid=value))


# Correction graph


def _voided(key: str, *args: Any, **fields: Any) -> dict:
    return _event(key, *args, voided_at=VOIDED, **fields)


def test_valid_correction_and_chain() -> None:
    """A correction of a correction forms a valid chain."""
    _validate_events(
        _voided(EVENT_1),
        _voided(EVENT_2, corrects_event_uuid=EVENT_1),
        _event(EVENT_3, corrects_event_uuid=EVENT_2),
    )


def test_correction_target_must_exist() -> None:
    _reject_events(
        "corrects missing Maintenance Event",
        _event(EVENT_1, corrects_event_uuid=MISSING_EVENT),
    )


def test_correction_cannot_target_itself() -> None:
    _reject_events("corrects itself", _voided(EVENT_1, corrects_event_uuid=EVENT_1))


def test_correction_target_must_share_the_asset() -> None:
    _reject_events(
        "of another Asset",
        _voided(EVENT_B, ASSET_B, []),
        _event(EVENT_1, corrects_event_uuid=EVENT_B),
    )


def test_correction_target_must_be_voided() -> None:
    _reject_events(
        "which is not voided",
        _event(EVENT_1),
        _event(EVENT_2, corrects_event_uuid=EVENT_1),
    )


def test_target_may_have_only_one_corrector() -> None:
    _reject_events(
        "more than one correcting Event",
        _voided(EVENT_1),
        _event(EVENT_2, corrects_event_uuid=EVENT_1),
        _event(EVENT_3, corrects_event_uuid=EVENT_1),
    )


def test_correction_cycle_is_rejected() -> None:
    _reject_events(
        "cycle through Maintenance Event",
        _voided(EVENT_1, corrects_event_uuid=EVENT_2),
        _voided(EVENT_2, corrects_event_uuid=EVENT_1),
    )


def test_longer_correction_cycle_is_rejected() -> None:
    _reject_events(
        "cycle through Maintenance Event",
        _voided(EVENT_1, corrects_event_uuid=EVENT_3),
        _voided(EVENT_2, corrects_event_uuid=EVENT_1),
        _voided(EVENT_3, corrects_event_uuid=EVENT_2),
    )


def test_graph_validator_accepts_graph_without_corrections() -> None:
    validate_correction_graph({})
    validate_correction_graph({EVENT_1: _event(EVENT_1)})


# Derived reference helpers


def test_baseline_is_unlocked_without_references() -> None:
    events = {EVENT_1: _event(EVENT_1, schedule_uuids=[SCHEDULE_2])}
    assert not is_baseline_locked({}, SCHEDULE_1)
    assert not is_baseline_locked(events, SCHEDULE_1)
    assert can_hard_delete_schedule(events, SCHEDULE_1)


def test_active_and_voided_events_lock_the_baseline() -> None:
    for event in (_event(EVENT_1), _voided(EVENT_1)):
        events = {EVENT_1: event}
        assert is_baseline_locked(events, SCHEDULE_1)
        assert not can_hard_delete_schedule(events, SCHEDULE_1)


def test_correction_keeps_the_old_target_reference() -> None:
    """A correction that drops a Schedule link leaves that Schedule locked."""
    events = {
        EVENT_1: _voided(EVENT_1, schedule_uuids=[SCHEDULE_1]),
        EVENT_2: _event(EVENT_2, schedule_uuids=[SCHEDULE_2], corrects_event_uuid=EVENT_1),
    }
    _validate(_schedules(), events)
    assert referenced_schedule_uuids(events) == {SCHEDULE_1, SCHEDULE_2}
    assert is_baseline_locked(events, SCHEDULE_1)
    assert not can_hard_delete_schedule(events, SCHEDULE_1)


def test_validation_does_not_modify_the_candidate() -> None:
    schedules = _schedules()
    events = {EVENT_1: _event(EVENT_1, schedule_uuids=[SCHEDULE_2, SCHEDULE_1])}
    before = deepcopy((schedules, events))
    _validate(schedules, events)
    assert (schedules, events) == before


# Production boundary


@pytest.mark.parametrize(
    "keys",
    [("maintenance_schedules",), ("maintenance_events",), ("maintenance_schedules", "maintenance_events")],
)
def test_store_3_1_still_rejects_maintenance_collections(
    asset_store_data: AssetStoreData,
    keys: tuple[str, ...],
) -> None:
    """The standalone validator is not wired into the Store 3.1 payload."""
    data: dict[str, Any] = deepcopy(asset_store_data)
    for key in keys:
        data[key] = {}
    with pytest.raises(AssetStoreError, match="invalid top-level shape"):
        _validate_store_data(data)  # type: ignore[arg-type]
