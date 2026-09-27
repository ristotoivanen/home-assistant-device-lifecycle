"""Maintenance management in the Asset OptionsFlow (WP17).

Every write is one request to ``async_mutate_maintenance``; the pure layer
owns the rules and the Store publish refreshes the WP16 entities, so the
flow never reloads. Identities are pre-generated once per draft and reused
by retries. The guards come from the pure preflight and are shown before
anything is saved. An archived Asset reaches only History.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.device_lifecycle.config_flow import (
    NOT_SELECTED,
    _short_name,
    _unique_labels,
)
from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_CALENDAR_UNIT,
    CONF_CALENDAR_VALUE,
    CONF_CONFIRM_BASELINE_LOCK,
    CONF_CONFIRM_DELETE_SCHEDULE,
    CONF_CONFIRM_DESTROY_BASELINE,
    CONF_CONFIRM_REMOVE_INTERVAL,
    CONF_CONFIRM_VOID,
    CONF_DATE_FROM,
    CONF_DATE_TO,
    CONF_EVENT_RUNTIME_HOURS,
    CONF_EVENT_TITLE,
    CONF_EVENT_UUID,
    CONF_LEAD_DAYS,
    CONF_NOTES,
    CONF_PERFORMED_DATE,
    CONF_REMINDER_MESSAGE,
    CONF_RUNTIME_HOURS,
    CONF_SCHEDULE_NAME,
    CONF_SCHEDULE_UUID,
    CONF_SCHEDULE_UUIDS,
    CONF_STARTING_DATE,
    CONF_STARTING_RUNTIME_HOURS,
    CONF_VOID_REASON,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DOMAIN,
    MAINTENANCE_HISTORY_SUMMARY_LIMIT,
)
from custom_components.device_lifecycle.maintenance_entities import (
    maintenance_due_date_unique_id,
    maintenance_preparation_unique_id,
    maintenance_status_unique_id,
)
from custom_components.device_lifecycle.maintenance_mutations import (
    IntervalDimension,
    MutationOutcome,
    RecordEventRequest,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    AssetStoreError,
    AssetStoreManager,
    AssetStorePersistenceError,
)

from .conftest import ASSET_UUID, capture_reloads
from .test_exposure_options_reload import _verified_store_readback
from .test_options_flow import _manager, _options_flow
from .test_runtime import _Clock, _sensor
from .test_runtime_checkpoint import _add
from .test_stale_device_registry_references import _load

TODAY = "2026-09-20 12:00:00-07:00"
CAL = "c0000000-0000-4000-8000-000000000001"
RUN = "c0000000-0000-4000-8000-000000000002"
BOTH = "c0000000-0000-4000-8000-000000000003"
OTHER_ASSET_SCHEDULE = "c0000000-0000-4000-8000-000000000009"
EVENT = "e0000000-0000-4000-8000-000000000001"


def _schedule(
    schedule_uuid: str,
    name: str,
    *,
    calendar: bool = True,
    runtime: str | None = None,
    anchor_date: str | None = None,
    anchor_runtime: str | None = None,
    enabled: bool = True,
    lead_days: int | None = None,
) -> dict[str, Any]:
    return {
        "schedule_uuid": schedule_uuid,
        "asset_uuid": ASSET_UUID,
        "name": name,
        "enabled": enabled,
        "calendar_interval": {"value": 6, "unit": "months"} if calendar else None,
        "runtime_interval_seconds": runtime,
        "initial_anchor": (
            None
            if anchor_date is None and anchor_runtime is None
            else {"date": anchor_date, "runtime_seconds": anchor_runtime}
        ),
        "preparation_reminder": (
            None if lead_days is None else {"lead_days": lead_days, "message": None}
        ),
    }


def _event(
    event_uuid: str,
    performed: str,
    *,
    schedules: list[str],
    title: str = "Service",
    runtime: str | None = None,
    voided: bool = False,
) -> dict[str, Any]:
    return {
        "event_uuid": event_uuid,
        "asset_uuid": ASSET_UUID,
        "schedule_uuids": sorted(schedules),
        "title": title,
        "performed_date": performed,
        "runtime_seconds": runtime,
        "recorded_at": "2026-09-01T00:00:00+00:00",
        "notes": None,
        "voided_at": "2026-09-02T00:00:00+00:00" if voided else None,
        "void_reason": None,
        "corrects_event_uuid": None,
    }


def _data(
    asset_store_data: AssetStoreData,
    *schedules: dict[str, Any],
    events: tuple[dict[str, Any], ...] = (),
    archived: bool = False,
) -> AssetStoreData:
    data: dict[str, Any] = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    asset["deployment_state"] = DEPLOYMENT_STATE_NOT_DEPLOYED
    asset["runtime"]["total_seconds"] = "36000"
    if archived:
        asset["archived_at"] = "2026-09-19T12:00:00+00:00"
    data["maintenance_schedules"] = {item["schedule_uuid"]: item for item in schedules}
    data["maintenance_events"] = {item["event_uuid"]: item for item in events}
    return data  # type: ignore[return-value]


async def _flow(
    hass: HomeAssistant, freezer: Any, data: AssetStoreData
) -> tuple[Any, Any, AssetStoreManager]:
    freezer.move_to(TODAY)
    manager = _manager(hass, data)
    flow, entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: ASSET_UUID})
    return flow, entry, manager


async def _open(flow: Any, schedule_uuid: str) -> dict[str, Any]:
    return await flow.async_step_maintenance_open_schedule(
        {CONF_SCHEDULE_UUID: schedule_uuid}
    )


def _options(form: dict[str, Any], key: str) -> list[str]:
    for marker, field in form["data_schema"].schema.items():
        if marker == key:
            return [option["value"] for option in field.config["options"]]
    raise AssertionError(key)


def _default(form: dict[str, Any], key: str) -> Any:
    for marker in form["data_schema"].schema:
        if marker == key:
            return marker.default() if callable(marker.default) else marker.default
    raise AssertionError(key)


def _suggested(form: dict[str, Any], key: str) -> Any:
    for marker in form["data_schema"].schema:
        if marker == key:
            return (marker.description or {}).get("suggested_value")
    raise AssertionError(key)


def _keys(form: dict[str, Any]) -> set[str]:
    return {str(marker) for marker in form["data_schema"].schema}


def _schedules(manager: AssetStoreManager) -> dict[str, Any]:
    return deepcopy(manager._data["maintenance_schedules"])


def _events(manager: AssetStoreManager) -> dict[str, Any]:
    return deepcopy(manager._data["maintenance_events"])


# --- Navigation ------------------------------------------------------------------


async def test_the_hub_row_opens_maintenance_for_an_active_asset(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data,
        _schedule(CAL, "Filter", anchor_date="2026-03-01"),
        _schedule(RUN, "Oil", calendar=False, runtime="3600", anchor_runtime="0"),
    )
    freezer.move_to(TODAY)
    manager = _manager(hass, data)
    flow, _entry = _options_flow(hass, manager)

    hub = await flow.async_step_manage_asset({CONF_ASSET_UUID: ASSET_UUID})
    assert "maintenance_menu" in hub["menu_options"]
    assert hub["description_placeholders"]["maintenance"] == (
        "Schedules: 2 · Due or overdue: 2"
    )

    menu = await flow.async_step_maintenance_menu()
    assert menu["type"] is FlowResultType.MENU
    assert menu["menu_options"] == [
        "maintenance_add_schedule",
        "maintenance_open_schedule",
        "maintenance_record",
        "maintenance_history",
        "manage_asset_menu",
    ]
    schedules = menu["description_placeholders"]["schedules"]
    assert "Filter · Overdue · due 1 Sep 2026" in schedules
    assert "Oil · Overdue" in schedules
    # No history in the summary.
    assert "Service" not in schedules


async def test_an_asset_without_schedules_offers_no_open(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    flow, _entry, _unused = await _flow(hass, freezer, _data(asset_store_data))
    hub = await flow.async_step_manage_asset_menu()
    assert hub["description_placeholders"]["maintenance"] == "No schedules"
    menu = await flow.async_step_maintenance_menu()
    assert "maintenance_open_schedule" not in menu["menu_options"]
    assert (
        "No maintenance schedules yet."
        in (menu["description_placeholders"]["schedules"])
    )


async def test_open_schedule_starts_unselected_and_lists_only_this_asset(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    other = _schedule(OTHER_ASSET_SCHEDULE, "Foreign")
    other["asset_uuid"] = "99999999-9999-4999-8999-999999999999"
    data = _data(
        asset_store_data,
        _schedule(CAL, "Filter"),
        _schedule(BOTH, "filter", runtime="3600"),
        _schedule(RUN, "Belt", calendar=False, runtime="7200"),
    )
    flow, _entry, _unused = await _flow(hass, freezer, data)

    form = await flow.async_step_maintenance_open_schedule()
    assert _default(form, CONF_SCHEDULE_UUID) == NOT_SELECTED
    assert _options(form, CONF_SCHEDULE_UUID) == [NOT_SELECTED, RUN, CAL, BOTH]
    field = next(iter(form["data_schema"].schema.values()))
    labels = [option["label"] for option in field.config["options"][1:]]
    # Names; a shared name gets its interval, never a UUID.
    assert labels[0] == "Belt"
    assert labels[1].startswith("Filter (") and labels[2].startswith("filter (")
    assert all(CAL not in label and BOTH not in label for label in labels)

    required = await flow.async_step_maintenance_open_schedule(
        {CONF_SCHEDULE_UUID: NOT_SELECTED}
    )
    assert required["errors"] == {CONF_SCHEDULE_UUID: "maintenance_schedule_required"}
    foreign = await _open(flow, OTHER_ASSET_SCHEDULE)
    assert foreign["errors"] == {"base": "maintenance_schedule_not_found"}
    view = await _open(flow, CAL)
    assert view["step_id"] == "maintenance_schedule"


async def test_the_schedule_view_offers_what_its_state_allows(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data,
        _schedule(CAL, "Filter", anchor_date="2026-04-01", lead_days=7),
        _schedule(BOTH, "Pump", runtime="3600", enabled=False),
        events=(_event(EVENT, "2026-09-01", schedules=[BOTH], voided=True),),
    )
    flow, _entry, _unused = await _flow(hass, freezer, data)

    unused = await _open(flow, CAL)
    assert unused["menu_options"] == [
        "maintenance_mark_done",
        "maintenance_edit_schedule",
        "maintenance_intervals",
        "maintenance_starting_point",
        "maintenance_disable_schedule",
        "maintenance_delete_schedule",
        "maintenance_menu",
    ]
    facts = unused["description_placeholders"]["facts"]
    assert "Status: OK" in facts
    assert "Next maintenance: 1 Oct 2026" in facts
    assert "Starting point: 1 Apr 2026" in facts
    assert "Preparation reminder: 7 day(s) before" in facts

    # Referenced only by a voided Event: still locked and not deletable.
    used = await _open(flow, BOTH)
    assert used["menu_options"] == [
        "maintenance_mark_done",
        "maintenance_edit_schedule",
        "maintenance_intervals",
        "maintenance_enable_schedule",
        "maintenance_menu",
    ]
    assert "Status: Disabled" in used["description_placeholders"]["facts"]
    assert "locked by maintenance history" in used["description_placeholders"]["facts"]


# --- Create ----------------------------------------------------------------------


async def test_create_a_calendar_schedule_with_a_reminder_and_unknown_start(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    flow, entry, manager = await _flow(hass, freezer, _data(asset_store_data))

    form = await flow.async_step_maintenance_add_schedule()
    assert _keys(form) == {
        CONF_SCHEDULE_NAME,
        CONF_CALENDAR_VALUE,
        CONF_CALENDAR_UNIT,
        CONF_RUNTIME_HOURS,
    }
    reminder = await flow.async_step_maintenance_add_schedule(
        {
            CONF_SCHEDULE_NAME: "Filter",
            CONF_CALENDAR_VALUE: 6.0,
            CONF_CALENDAR_UNIT: "months",
        }
    )
    assert reminder["step_id"] == "maintenance_schedule_reminder"
    choice = await flow.async_step_maintenance_schedule_reminder(
        {CONF_LEAD_DAYS: 7, CONF_REMINDER_MESSAGE: "Buy a filter"}
    )
    # An explicit choice, as menu rows: nothing is chosen by default.
    assert choice["type"] is FlowResultType.MENU
    assert choice["menu_options"] == [
        "maintenance_start_unknown",
        "maintenance_start_known",
        "maintenance_start_back",
    ]
    manager._store.async_save.assert_not_awaited()

    with capture_reloads(hass) as reloads:
        view = await flow.async_step_maintenance_start_unknown()

    reloads.assert_not_called()
    assert entry.runtime_data is manager
    assert view["step_id"] == "maintenance_schedule"
    assert view["description_placeholders"]["result"] == "Schedule added."
    (created,) = manager._data["maintenance_schedules"].values()
    assert created == {
        "schedule_uuid": created["schedule_uuid"],
        "asset_uuid": ASSET_UUID,
        "name": "Filter",
        "enabled": True,
        "calendar_interval": {"value": 6, "unit": "months"},
        "runtime_interval_seconds": None,
        "initial_anchor": None,
        "preparation_reminder": {"lead_days": 7, "message": "Buy a filter"},
    }


async def test_create_a_runtime_schedule_converts_hours_exactly(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    flow, _entry, manager = await _flow(hass, freezer, _data(asset_store_data))

    choice = await flow.async_step_maintenance_add_schedule(
        {CONF_SCHEDULE_NAME: "Oil", CONF_RUNTIME_HOURS: 1.1}
    )
    # No calendar interval, so no preparation reminder is asked.
    assert choice["step_id"] == "maintenance_start_choice"
    known = await flow.async_step_maintenance_start_known()
    assert _keys(known) == {CONF_STARTING_RUNTIME_HOURS}
    missing = await flow.async_step_maintenance_start_known({})
    assert missing["errors"] == {"base": "maintenance_starting_point_required"}

    await flow.async_step_maintenance_start_known({CONF_STARTING_RUNTIME_HOURS: 0.1})

    (created,) = manager._data["maintenance_schedules"].values()
    # 1.1 h and 0.1 h exactly, never through a binary float product.
    assert created["runtime_interval_seconds"] == "3960.0"
    assert Decimal(created["runtime_interval_seconds"]) == Decimal(3960)
    assert created["initial_anchor"] == {"date": None, "runtime_seconds": "360.0"}
    assert created["calendar_interval"] is None
    assert created["preparation_reminder"] is None


async def test_create_a_combined_schedule_with_a_known_date_only(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    flow, _entry, manager = await _flow(hass, freezer, _data(asset_store_data))
    await flow.async_step_maintenance_add_schedule(
        {
            CONF_SCHEDULE_NAME: "Pump",
            CONF_CALENDAR_VALUE: 1,
            CONF_CALENDAR_UNIT: "years",
            CONF_RUNTIME_HOURS: 100,
        }
    )
    await flow.async_step_maintenance_schedule_reminder({})
    known = await flow.async_step_maintenance_start_known()
    assert _keys(known) == {CONF_STARTING_DATE, CONF_STARTING_RUNTIME_HOURS}
    await flow.async_step_maintenance_start_known({CONF_STARTING_DATE: "2026-01-15"})

    (created,) = manager._data["maintenance_schedules"].values()
    assert created["calendar_interval"] == {"value": 1, "unit": "years"}
    assert created["runtime_interval_seconds"] == "360000"
    # The unknown component stays unknown; nothing is inferred.
    assert created["initial_anchor"] == {"date": "2026-01-15", "runtime_seconds": None}
    assert created["preparation_reminder"] is None


async def test_create_needs_an_interval_and_valid_numbers(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    flow, _entry, manager = await _flow(hass, freezer, _data(asset_store_data))

    empty = await flow.async_step_maintenance_add_schedule({CONF_SCHEDULE_NAME: "X"})
    assert empty["errors"] == {"base": "maintenance_interval_required"}
    fraction = await flow.async_step_maintenance_add_schedule(
        {CONF_SCHEDULE_NAME: "X", CONF_CALENDAR_VALUE: 1.5, CONF_CALENDAR_UNIT: "days"}
    )
    assert fraction["errors"] == {"base": "maintenance_invalid_number"}
    negative = await flow.async_step_maintenance_add_schedule(
        {CONF_SCHEDULE_NAME: "X", CONF_RUNTIME_HOURS: -1}
    )
    assert negative["errors"] == {"base": "maintenance_invalid_number"}
    await flow.async_step_maintenance_add_schedule(
        {CONF_SCHEDULE_NAME: "X", CONF_CALENDAR_VALUE: 1, CONF_CALENDAR_UNIT: "days"}
    )
    bad_reminder = await flow.async_step_maintenance_schedule_reminder(
        {CONF_LEAD_DAYS: 0}
    )
    assert bad_reminder["errors"] == {"base": "maintenance_invalid_number"}
    await flow.async_step_maintenance_schedule_reminder({})
    future = await flow.async_step_maintenance_start_known(
        {CONF_STARTING_DATE: "2026-12-01"}
    )
    assert future["errors"] == {"base": "maintenance_date_in_future"}
    assert manager._data["maintenance_schedules"] == {}
    # Back from the starting-point question saves nothing.
    back = await flow.async_step_maintenance_start_back()
    assert back["step_id"] == "maintenance_menu"
    assert manager._data["maintenance_schedules"] == {}


async def test_a_retried_create_reuses_its_schedule_identity(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    flow, _entry, manager = await _flow(hass, freezer, _data(asset_store_data))
    await flow.async_step_maintenance_add_schedule(
        {CONF_SCHEDULE_NAME: "Oil", CONF_RUNTIME_HOURS: 10}
    )
    manager._store.async_save = AsyncMock(side_effect=OSError("disk"))
    failed = await flow.async_step_maintenance_start_known(
        {CONF_STARTING_RUNTIME_HOURS: 1}
    )
    assert failed["step_id"] == "maintenance_start_known"
    assert failed["errors"]["base"]
    # The person's input is kept on the form.
    assert _suggested(failed, CONF_STARTING_RUNTIME_HOURS) == 1
    uuid = flow._schedule_draft["schedule_uuid"]
    assert manager._data["maintenance_schedules"] == {}

    manager._store.async_save = AsyncMock()
    await flow.async_step_maintenance_start_known({CONF_STARTING_RUNTIME_HOURS: 1})
    assert list(manager._data["maintenance_schedules"]) == [uuid]


# --- Edit, starting point, enable, delete --------------------------------------------


async def test_edit_changes_values_and_the_reminder_without_new_entities(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(asset_store_data, _schedule(BOTH, "Pump", runtime="3600"))
    flow, _entry, manager = await _flow(hass, freezer, data)
    await _open(flow, BOTH)

    form = await flow.async_step_maintenance_edit_schedule()
    assert _suggested(form, CONF_SCHEDULE_NAME) == "Pump"
    assert _suggested(form, CONF_RUNTIME_HOURS) == 1.0
    reminder = await flow.async_step_maintenance_edit_schedule(
        {
            CONF_SCHEDULE_NAME: "Pump service",
            CONF_CALENDAR_VALUE: 3,
            CONF_CALENDAR_UNIT: "months",
            CONF_RUNTIME_HOURS: 1.0,
        }
    )
    assert reminder["step_id"] == "maintenance_schedule_reminder"
    view = await flow.async_step_maintenance_schedule_reminder({CONF_LEAD_DAYS: 5})

    assert view["description_placeholders"]["result"] == "Schedule saved."
    edited = manager._data["maintenance_schedules"][BOTH]
    assert edited["name"] == "Pump service"
    assert edited["calendar_interval"] == {"value": 3, "unit": "months"}
    assert edited["runtime_interval_seconds"] == "3600"
    assert edited["preparation_reminder"] == {"lead_days": 5, "message": None}

    # The same values again: nothing changes.
    await flow.async_step_maintenance_edit_schedule(
        {
            CONF_SCHEDULE_NAME: "Pump service",
            CONF_CALENDAR_VALUE: 3,
            CONF_CALENDAR_UNIT: "months",
            CONF_RUNTIME_HOURS: 1.0,
        }
    )
    unchanged = await flow.async_step_maintenance_schedule_reminder({CONF_LEAD_DAYS: 5})
    assert unchanged["description_placeholders"]["result"] == (
        "Nothing changed; it was already so."
    )


async def test_a_runtime_only_edit_saves_without_a_reminder_step(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data, _schedule(RUN, "Oil", calendar=False, runtime="3600")
    )
    flow, _entry, manager = await _flow(hass, freezer, data)
    await _open(flow, RUN)
    form = await flow.async_step_maintenance_edit_schedule()
    assert _keys(form) == {CONF_SCHEDULE_NAME, CONF_RUNTIME_HOURS}
    view = await flow.async_step_maintenance_edit_schedule(
        {CONF_SCHEDULE_NAME: "Oil", CONF_RUNTIME_HOURS: 2.5}
    )
    assert view["step_id"] == "maintenance_schedule"
    assert manager._data["maintenance_schedules"][RUN]["runtime_interval_seconds"] == (
        "9000.0"
    )


async def test_the_starting_point_is_editable_only_until_it_is_locked(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(asset_store_data, _schedule(CAL, "Filter", anchor_date="2026-04-01"))
    flow, _entry, manager = await _flow(hass, freezer, data)
    await _open(flow, CAL)

    choice = await flow.async_step_maintenance_starting_point()
    assert choice["step_id"] == "maintenance_start_choice"
    view = await flow.async_step_maintenance_start_known(
        {CONF_STARTING_DATE: "2026-05-01"}
    )
    assert view["description_placeholders"]["result"] == "Starting point saved."
    assert manager._data["maintenance_schedules"][CAL]["initial_anchor"] == {
        "date": "2026-05-01",
        "runtime_seconds": None,
    }
    await flow.async_step_maintenance_starting_point()
    await flow.async_step_maintenance_start_unknown()
    assert manager._data["maintenance_schedules"][CAL]["initial_anchor"] is None

    manager._data["maintenance_events"][EVENT] = _event(
        EVENT, "2026-09-01", schedules=[CAL], voided=True
    )
    locked = await flow.async_step_maintenance_starting_point()
    assert locked["step_id"] == "maintenance_schedule"
    assert "locked" in locked["description_placeholders"]["result"]
    assert "maintenance_starting_point" not in locked["menu_options"]


async def test_enable_and_disable_change_nothing_else(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data,
        _schedule(CAL, "Filter", anchor_date="2026-04-01"),
        events=(_event(EVENT, "2026-09-01", schedules=[CAL]),),
    )
    flow, _entry, manager = await _flow(hass, freezer, data)
    await _open(flow, CAL)
    before = _schedules(manager)[CAL]
    events = _events(manager)

    disabled = await flow.async_step_maintenance_disable_schedule()
    assert "maintenance_enable_schedule" in disabled["menu_options"]
    assert "Status: Disabled" in disabled["description_placeholders"]["facts"]
    assert manager._data["maintenance_schedules"][CAL] == {**before, "enabled": False}
    assert _events(manager) == events

    enabled = await flow.async_step_maintenance_enable_schedule()
    assert enabled["description_placeholders"]["result"] == "Schedule enabled."
    assert manager._data["maintenance_schedules"][CAL] == before


async def test_only_an_unused_schedule_is_deleted_and_a_stale_delete_fails(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(asset_store_data, _schedule(CAL, "Filter"), _schedule(RUN, "Belt"))
    flow, _entry, manager = await _flow(hass, freezer, data)
    await _open(flow, CAL)

    form = await flow.async_step_maintenance_delete_schedule()
    unconfirmed = await flow.async_step_maintenance_delete_schedule(
        {CONF_CONFIRM_DELETE_SCHEDULE: False}
    )
    assert unconfirmed["errors"] == {
        CONF_CONFIRM_DELETE_SCHEDULE: "maintenance_confirmation_required"
    }
    assert form["step_id"] == "maintenance_delete_schedule"
    menu = await flow.async_step_maintenance_delete_schedule(
        {CONF_CONFIRM_DELETE_SCHEDULE: True}
    )
    assert menu["step_id"] == "maintenance_menu"
    assert menu["description_placeholders"]["result"] == "Schedule deleted."
    assert CAL not in manager._data["maintenance_schedules"]

    await _open(flow, RUN)
    await flow.async_step_maintenance_delete_schedule()
    # Maintenance recorded elsewhere after the confirmation was shown.
    manager._data["maintenance_events"][EVENT] = _event(
        EVENT, "2026-09-01", schedules=[RUN], voided=True
    )
    stale = await flow.async_step_maintenance_delete_schedule(
        {CONF_CONFIRM_DELETE_SCHEDULE: True}
    )
    assert stale["errors"] == {"base": "maintenance_schedule_referenced"}
    assert RUN in manager._data["maintenance_schedules"]
    # Rendering it again for a referenced Schedule returns to the view.
    view = await flow.async_step_maintenance_delete_schedule()
    assert view["step_id"] == "maintenance_schedule"
    assert "maintenance_delete_schedule" not in view["menu_options"]


# --- Intervals -------------------------------------------------------------------


async def test_add_an_interval_with_a_starting_point_while_unlocked(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(asset_store_data, _schedule(CAL, "Filter", anchor_date="2026-04-01"))
    flow, _entry, manager = await _flow(hass, freezer, data)
    await _open(flow, CAL)

    intervals = await flow.async_step_maintenance_intervals()
    assert intervals["menu_options"] == [
        "maintenance_add_runtime",
        "maintenance_schedule",
    ]
    form = await flow.async_step_maintenance_add_runtime()
    assert _keys(form) == {CONF_RUNTIME_HOURS}
    choice = await flow.async_step_maintenance_add_runtime({CONF_RUNTIME_HOURS: 500})
    assert choice["step_id"] == "maintenance_start_choice"
    known = await flow.async_step_maintenance_start_known()
    assert _keys(known) == {CONF_STARTING_RUNTIME_HOURS}
    done = await flow.async_step_maintenance_start_known(
        {CONF_STARTING_RUNTIME_HOURS: 2}
    )

    assert done["step_id"] == "maintenance_intervals"
    assert done["description_placeholders"]["result"] == "Interval added."
    schedule = manager._data["maintenance_schedules"][CAL]
    assert schedule["runtime_interval_seconds"] == "1800000"
    assert schedule["initial_anchor"] == {
        "date": "2026-04-01",
        "runtime_seconds": "7200",
    }
    assert done["menu_options"] == [
        "maintenance_remove_calendar",
        "maintenance_remove_runtime",
        "maintenance_schedule",
    ]


async def test_a_locked_schedule_gets_a_new_interval_without_a_starting_point(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data,
        _schedule(RUN, "Oil", calendar=False, runtime="3600", anchor_runtime="0"),
        events=(_event(EVENT, "2026-09-01", schedules=[RUN], runtime="3600"),),
    )
    flow, _entry, manager = await _flow(hass, freezer, data)
    await _open(flow, RUN)

    done = await flow.async_step_maintenance_add_calendar(
        {CONF_CALENDAR_VALUE: 1, CONF_CALENDAR_UNIT: "years"}
    )

    assert done["step_id"] == "maintenance_intervals"
    schedule = manager._data["maintenance_schedules"][RUN]
    assert schedule["calendar_interval"] == {"value": 1, "unit": "years"}
    assert schedule["initial_anchor"] == {"date": None, "runtime_seconds": "0"}


async def test_removing_a_locked_component_needs_its_own_confirmation(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data,
        _schedule(
            BOTH,
            "Pump",
            runtime="3600",
            anchor_date="2026-04-01",
            anchor_runtime="100",
            lead_days=3,
        ),
        events=(_event(EVENT, "2026-09-01", schedules=[BOTH]),),
    )
    flow, _entry, manager = await _flow(hass, freezer, data)
    await _open(flow, BOTH)
    before = _schedules(manager)

    confirm = await flow.async_step_maintenance_remove_calendar()
    assert confirm["step_id"] == "maintenance_remove_interval"
    assert "preparation reminder" in confirm["description_placeholders"]["effects"]
    unconfirmed = await flow.async_step_maintenance_remove_interval(
        {CONF_CONFIRM_REMOVE_INTERVAL: False}
    )
    assert unconfirmed["errors"] == {
        CONF_CONFIRM_REMOVE_INTERVAL: "maintenance_confirmation_required"
    }
    destroy = await flow.async_step_maintenance_remove_interval(
        {CONF_CONFIRM_REMOVE_INTERVAL: True}
    )
    # The first submit did not remove the locked starting date.
    assert destroy["step_id"] == "maintenance_confirm_destroy_baseline"
    assert (
        "calendar starting date 1 Apr 2026"
        in destroy["description_placeholders"]["lost"]
    )
    assert _schedules(manager) == before
    declined = await flow.async_step_maintenance_confirm_destroy_baseline(
        {CONF_CONFIRM_DESTROY_BASELINE: False}
    )
    assert declined["errors"] == {
        CONF_CONFIRM_DESTROY_BASELINE: "maintenance_confirmation_required"
    }
    assert _schedules(manager) == before

    done = await flow.async_step_maintenance_confirm_destroy_baseline(
        {CONF_CONFIRM_DESTROY_BASELINE: True}
    )
    assert done["description_placeholders"]["result"] == "Interval removed."
    schedule = manager._data["maintenance_schedules"][BOTH]
    assert schedule["calendar_interval"] is None
    assert schedule["preparation_reminder"] is None
    assert schedule["initial_anchor"] == {"date": None, "runtime_seconds": "100"}

    # Adding the interval again never brings the date back.
    await flow.async_step_maintenance_add_calendar(
        {CONF_CALENDAR_VALUE: 6, CONF_CALENDAR_UNIT: "months"}
    )
    assert manager._data["maintenance_schedules"][BOTH]["initial_anchor"] == {
        "date": None,
        "runtime_seconds": "100",
    }

    # The Runtime component names its value.
    await flow.async_step_maintenance_remove_runtime()
    runtime = await flow.async_step_maintenance_remove_interval(
        {CONF_CONFIRM_REMOVE_INTERVAL: True}
    )
    assert (
        "Runtime starting value 0.03 h" in runtime["description_placeholders"]["lost"]
    )


async def test_an_unlocked_removal_needs_only_the_first_confirmation(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data,
        _schedule(BOTH, "Pump", runtime="3600", anchor_runtime="100"),
    )
    flow, _entry, manager = await _flow(hass, freezer, data)
    await _open(flow, BOTH)
    await flow.async_step_maintenance_remove_runtime()
    done = await flow.async_step_maintenance_remove_interval(
        {CONF_CONFIRM_REMOVE_INTERVAL: True}
    )
    assert done["step_id"] == "maintenance_intervals"
    schedule = manager._data["maintenance_schedules"][BOTH]
    assert schedule["runtime_interval_seconds"] is None
    assert schedule["initial_anchor"] is None
    # The last interval is never offered for removal.
    assert done["menu_options"] == ["maintenance_add_runtime", "maintenance_schedule"]
    await flow.async_step_maintenance_remove_calendar()
    refused = await flow.async_step_maintenance_remove_interval(
        {CONF_CONFIRM_REMOVE_INTERVAL: True}
    )
    assert refused["description_placeholders"]["result"] == (
        "The last interval cannot be removed."
    )


# --- Recording ---------------------------------------------------------------------


async def test_record_starts_with_nothing_selected_and_allows_ad_hoc(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(asset_store_data, _schedule(CAL, "Filter"), _schedule(RUN, "Belt"))
    flow, _entry, manager = await _flow(hass, freezer, data)

    form = await flow.async_step_maintenance_record()
    assert _default(form, CONF_SCHEDULE_UUIDS) == []
    assert set(_options(form, CONF_SCHEDULE_UUIDS)) == {CAL, RUN}
    timing = await flow.async_step_maintenance_record(
        {CONF_SCHEDULE_UUIDS: [], CONF_EVENT_TITLE: "Cleaned", CONF_NOTES: "Dusty"}
    )
    assert timing["menu_options"] == [
        "maintenance_event_just_now",
        "maintenance_event_earlier",
        "maintenance_event_details",
    ]
    back = await flow.async_step_maintenance_event_details()
    assert _suggested(back, CONF_EVENT_TITLE) == "Cleaned"
    await flow.async_step_maintenance_event_timing()
    menu = await flow.async_step_maintenance_event_earlier(
        {CONF_PERFORMED_DATE: "2026-09-10"}
    )

    assert menu["step_id"] == "maintenance_menu"
    assert menu["description_placeholders"]["result"] == "Maintenance recorded."
    (event,) = manager._data["maintenance_events"].values()
    assert event["schedule_uuids"] == []
    assert event["title"] == "Cleaned"
    assert event["notes"] == "Dusty"
    assert event["runtime_seconds"] is None


async def test_mark_done_preselects_only_its_own_schedule(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(asset_store_data, _schedule(CAL, "Filter"), _schedule(RUN, "Belt"))
    flow, _entry, manager = await _flow(hass, freezer, data)
    await _open(flow, CAL)

    form = await flow.async_step_maintenance_mark_done()
    assert form["step_id"] == "maintenance_record"
    assert _default(form, CONF_SCHEDULE_UUIDS) == [CAL]
    assert _suggested(form, CONF_EVENT_TITLE) == "Filter"
    await flow.async_step_maintenance_record(
        {CONF_SCHEDULE_UUIDS: [CAL], CONF_EVENT_TITLE: "Filter"}
    )
    view = await flow.async_step_maintenance_event_earlier(
        {CONF_PERFORMED_DATE: "2026-09-19"}
    )
    # Back where it was opened from.
    assert view["step_id"] == "maintenance_schedule"
    (event,) = manager._data["maintenance_events"].values()
    assert event["schedule_uuids"] == [CAL]
    assert "Status: OK" in view["description_placeholders"]["facts"]


async def test_just_now_checkpoints_first_and_keeps_its_capture_for_retries(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data, _schedule(RUN, "Oil", calendar=False, runtime="3600")
    )
    flow, _entry, manager = await _flow(hass, freezer, data)
    calls: list[str] = []
    real_mutate = manager.async_mutate_maintenance

    async def _checkpoint(asset_uuid: str) -> Decimal:
        calls.append("checkpoint")
        return Decimal(36000)

    async def _mutate(request: Any) -> Any:
        calls.append("mutate")
        return await real_mutate(request)

    manager.async_checkpoint_runtime = _checkpoint  # type: ignore[method-assign]
    manager.async_mutate_maintenance = _mutate  # type: ignore[method-assign]
    await flow.async_step_maintenance_record(
        {CONF_SCHEDULE_UUIDS: [RUN], CONF_EVENT_TITLE: "Oil change"}
    )

    shown = await flow.async_step_maintenance_event_just_now()
    assert calls == ["checkpoint"]
    assert shown["step_id"] == "maintenance_event_just_now"
    assert shown["description_placeholders"]["date"] == "20 Sep 2026"
    assert shown["description_placeholders"]["runtime"] == "10.00 h"
    event_uuid = flow._event_draft["event_uuid"]

    manager._store.async_save = AsyncMock(side_effect=OSError("disk"))
    failed = await flow.async_step_maintenance_event_just_now({})
    assert failed["errors"]["base"]
    manager._store.async_save = AsyncMock()
    freezer.move_to("2026-09-21 12:00:00-07:00")
    await flow.async_step_maintenance_event_just_now({})

    # One checkpoint, before any mutation; the retry keeps the capture.
    assert calls == ["checkpoint", "mutate", "mutate"]
    assert list(manager._data["maintenance_events"]) == [event_uuid]
    event = manager._data["maintenance_events"][event_uuid]
    assert event["performed_date"] == "2026-09-20"
    assert event["runtime_seconds"] == "36000"


async def test_a_failed_checkpoint_saves_nothing(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data, _schedule(RUN, "Oil", calendar=False, runtime="3600")
    )
    flow, _entry, manager = await _flow(hass, freezer, data)
    manager._runtime_unresolved.add(ASSET_UUID)
    await flow.async_step_maintenance_record(
        {CONF_SCHEDULE_UUIDS: [RUN], CONF_EVENT_TITLE: "Oil"}
    )
    failed = await flow.async_step_maintenance_event_just_now()
    assert failed["errors"] == {"base": "runtime_checkpoint_unresolved"}
    assert failed["description_placeholders"]["date"] == "—"
    assert manager._data["maintenance_events"] == {}

    manager.async_checkpoint_runtime = AsyncMock(  # type: ignore[method-assign]
        side_effect=OSError("disk")
    )
    other = await flow.async_step_maintenance_event_just_now({})
    assert other["errors"] == {"base": "runtime_checkpoint_failed"}


async def test_an_earlier_date_never_copies_the_current_runtime(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data, _schedule(RUN, "Oil", calendar=False, runtime="3600")
    )
    flow, _entry, manager = await _flow(hass, freezer, data)
    manager.async_checkpoint_runtime = AsyncMock()  # type: ignore[method-assign]
    await flow.async_step_maintenance_record(
        {CONF_SCHEDULE_UUIDS: [RUN], CONF_EVENT_TITLE: "Oil"}
    )
    form = await flow.async_step_maintenance_event_earlier()
    assert _suggested(form, CONF_EVENT_RUNTIME_HOURS) is None
    await flow.async_step_maintenance_event_earlier({CONF_PERFORMED_DATE: "2026-09-01"})

    manager.async_checkpoint_runtime.assert_not_awaited()
    (event,) = manager._data["maintenance_events"].values()
    assert event["runtime_seconds"] is None
    assert manager.asset(ASSET_UUID)["runtime"]["total_seconds"] == "36000"  # type: ignore[index]

    # An entered Runtime is converted exactly and checked against the current.
    await flow.async_step_maintenance_record(
        {CONF_SCHEDULE_UUIDS: [], CONF_EVENT_TITLE: "Check"}
    )
    too_high = await flow.async_step_maintenance_event_earlier(
        {CONF_PERFORMED_DATE: "2026-09-02", CONF_EVENT_RUNTIME_HOURS: 11}
    )
    assert too_high["errors"] == {"base": "maintenance_runtime_above_current"}
    assert _suggested(too_high, CONF_EVENT_RUNTIME_HOURS) == 11
    await flow.async_step_maintenance_event_earlier(
        {CONF_PERFORMED_DATE: "2026-09-02", CONF_EVENT_RUNTIME_HOURS: 2.2}
    )
    runtimes = {
        event["title"]: event["runtime_seconds"]
        for event in manager._data["maintenance_events"].values()
    }
    assert runtimes == {"Oil": None, "Check": "7920.0"}


async def test_the_first_event_shows_the_lock_before_saving(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data,
        _schedule(CAL, "Filter", anchor_date="2026-04-01"),
        _schedule(RUN, "Belt", calendar=False, runtime="3600"),
    )
    flow, _entry, manager = await _flow(hass, freezer, data)
    sent: list[Any] = []
    real_mutate = manager.async_mutate_maintenance

    async def _spy(request: Any) -> Any:
        sent.append(request)
        return await real_mutate(request)

    manager.async_mutate_maintenance = _spy  # type: ignore[method-assign]
    await flow.async_step_maintenance_record(
        {CONF_SCHEDULE_UUIDS: [CAL, RUN], CONF_EVENT_TITLE: "Both"}
    )
    notice = await flow.async_step_maintenance_event_earlier(
        {CONF_PERFORMED_DATE: "2026-09-10"}
    )

    # Only the Schedule with a starting point is named; nothing is saved yet.
    assert notice["step_id"] == "maintenance_event_lock"
    assert notice["description_placeholders"]["schedules"] == "Filter"
    assert sent == []
    unconfirmed = await flow.async_step_maintenance_event_lock(
        {CONF_CONFIRM_BASELINE_LOCK: False}
    )
    assert unconfirmed["errors"] == {
        CONF_CONFIRM_BASELINE_LOCK: "maintenance_confirmation_required"
    }
    menu = await flow.async_step_maintenance_event_lock(
        {CONF_CONFIRM_BASELINE_LOCK: True}
    )

    assert menu["step_id"] == "maintenance_menu"
    (request,) = sent
    assert isinstance(request, RecordEventRequest)
    assert request.confirmed_baseline_locks == frozenset({CAL})
    assert manager.maintenance_baseline_locked(CAL)

    # Locked: starting point editing is gone, and voiding does not unlock it.
    await _open(flow, CAL)
    event_uuid = request.event_uuid
    await flow.async_step_maintenance_history()
    await flow.async_step_maintenance_void_event()
    await flow.async_step_maintenance_history_filter({})
    await flow.async_step_maintenance_select_event({CONF_EVENT_UUID: event_uuid})
    await flow.async_step_maintenance_void_event_confirm({CONF_CONFIRM_VOID: True})
    view = await _open(flow, CAL)
    assert "maintenance_starting_point" not in view["menu_options"]
    assert manager.maintenance_baseline_locked(CAL)


async def test_a_lock_appearing_after_the_notice_is_shown_again(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data,
        _schedule(CAL, "Filter"),
        _schedule(RUN, "Belt", calendar=False, runtime="3600"),
    )
    flow, _entry, manager = await _flow(hass, freezer, data)
    await flow.async_step_maintenance_record(
        {CONF_SCHEDULE_UUIDS: [CAL], CONF_EVENT_TITLE: "Filter"}
    )
    # A starting point set elsewhere after the preflight saw none: the
    # manager refuses, and the flow shows the notice for the current state.
    real_guards = manager.maintenance_event_guards
    calls = 0

    def _stale_then_real(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            manager._data["maintenance_schedules"][CAL]["initial_anchor"] = {
                "date": "2026-04-01",
                "runtime_seconds": None,
            }
            return type(real_guards(*args, **kwargs))()
        return real_guards(*args, **kwargs)

    manager.maintenance_event_guards = _stale_then_real  # type: ignore[method-assign]
    notice = await flow.async_step_maintenance_event_earlier(
        {CONF_PERFORMED_DATE: "2026-09-10"}
    )
    assert notice["step_id"] == "maintenance_event_lock"
    assert manager._data["maintenance_events"] == {}


async def test_a_same_day_runtime_record_warns_but_saves(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data,
        _schedule(RUN, "Oil", calendar=False, runtime="3600"),
        events=(_event(EVENT, "2026-09-20", schedules=[RUN], runtime="30000"),),
    )
    flow, _entry, manager = await _flow(hass, freezer, data)
    await flow.async_step_maintenance_record(
        {CONF_SCHEDULE_UUIDS: [RUN], CONF_EVENT_TITLE: "Oil again"}
    )
    warning = await flow.async_step_maintenance_event_earlier(
        {CONF_PERFORMED_DATE: "2026-09-20", CONF_EVENT_RUNTIME_HOURS: 9}
    )
    assert warning["step_id"] == "maintenance_event_ambiguity"
    assert warning["description_placeholders"]["schedules"] == "Oil"
    assert len(manager._data["maintenance_events"]) == 1

    await flow.async_step_maintenance_event_ambiguity({})

    events = manager._data["maintenance_events"]
    assert len(events) == 2
    assert all(event["voided_at"] is None for event in events.values())
    projection = manager.maintenance_projection(RUN)
    assert projection is not None
    assert projection.anchor.runtime is None
    assert projection.combined_state == "unknown"


# --- History -----------------------------------------------------------------------


def _history(count: int, schedule: str = CAL) -> tuple[dict[str, Any], ...]:
    return tuple(
        _event(
            f"e0000000-0000-4000-8000-{index:012d}",
            f"2026-08-{index:02d}",
            schedules=[schedule],
            title=f"Service {index}",
        )
        for index in range(1, count + 1)
    )


async def test_the_summary_limit_never_limits_what_can_be_chosen(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    count = MAINTENANCE_HISTORY_SUMMARY_LIMIT + 3
    data = _data(asset_store_data, _schedule(CAL, "Filter"), events=_history(count))
    flow, _entry, manager = await _flow(hass, freezer, data)

    history = await flow.async_step_maintenance_history()
    lines = history["description_placeholders"]["events"].splitlines()
    assert len(lines) == MAINTENANCE_HISTORY_SUMMARY_LIMIT
    assert lines[0].startswith(f"• {count} Aug 2026 · Service {count}")
    assert history["description_placeholders"]["count"] == str(count)
    assert history["menu_options"] == [
        "maintenance_correct_event",
        "maintenance_void_event",
        "maintenance_menu",
    ]

    await flow.async_step_maintenance_void_event()
    filter_form = await flow.async_step_maintenance_history_filter()
    assert _keys(filter_form) == {CONF_DATE_FROM, CONF_DATE_TO}
    backwards = await flow.async_step_maintenance_history_filter(
        {CONF_DATE_FROM: "2026-08-05", CONF_DATE_TO: "2026-08-01"}
    )
    assert backwards["errors"] == {"base": "maintenance_filter_range"}
    selection = await flow.async_step_maintenance_history_filter({})
    values = _options(selection, CONF_EVENT_UUID)
    assert values[0] == NOT_SELECTED
    assert values[1:] == [
        f"e0000000-0000-4000-8000-{index:012d}" for index in range(count, 0, -1)
    ]
    assert _default(selection, CONF_EVENT_UUID) == NOT_SELECTED
    required = await flow.async_step_maintenance_select_event(
        {CONF_EVENT_UUID: NOT_SELECTED}
    )
    assert required["errors"] == {CONF_EVENT_UUID: "maintenance_event_required"}

    oldest = "e0000000-0000-4000-8000-000000000001"
    confirm = await flow.async_step_maintenance_select_event({CONF_EVENT_UUID: oldest})
    assert confirm["step_id"] == "maintenance_void_event_confirm"
    done = await flow.async_step_maintenance_void_event_confirm(
        {CONF_CONFIRM_VOID: True, CONF_VOID_REASON: "Duplicate"}
    )
    assert done["step_id"] == "maintenance_history"
    assert done["description_placeholders"]["result"] == (
        "Record voided. It stays in the history."
    )
    assert manager._data["maintenance_events"][oldest]["void_reason"] == "Duplicate"

    # Another old one, narrowed by date, corrected.
    second = "e0000000-0000-4000-8000-000000000002"
    await flow.async_step_maintenance_correct_event()
    narrowed = await flow.async_step_maintenance_history_filter(
        {CONF_DATE_FROM: "2026-08-01", CONF_DATE_TO: "2026-08-02"}
    )
    assert _options(narrowed, CONF_EVENT_UUID) == [NOT_SELECTED, second, oldest]
    field = next(iter(narrowed["data_schema"].schema.values()))
    assert field.config["options"][2]["label"] == "1 Aug 2026 · Service 1 · Voided"
    stale = await flow.async_step_maintenance_select_event({CONF_EVENT_UUID: oldest})
    assert stale["errors"] == {"base": "maintenance_event_not_active"}
    form = await flow.async_step_maintenance_select_event({CONF_EVENT_UUID: second})
    assert form["step_id"] == "maintenance_correct_event_form"
    assert _suggested(form, CONF_PERFORMED_DATE) == "2026-08-02"
    corrected = await flow.async_step_maintenance_correct_event_form(
        {
            CONF_SCHEDULE_UUIDS: [CAL],
            CONF_EVENT_TITLE: "Service 2 (fixed)",
            CONF_PERFORMED_DATE: "2026-08-03",
            CONF_VOID_REASON: "Wrong date",
        }
    )
    assert corrected["step_id"] == "maintenance_history"
    events = manager._data["maintenance_events"]
    assert events[second]["voided_at"] is not None
    assert events[second]["void_reason"] == "Wrong date"
    (replacement,) = [
        event for event in events.values() if event["corrects_event_uuid"] == second
    ]
    assert replacement["title"] == "Service 2 (fixed)"
    assert replacement["performed_date"] == "2026-08-03"
    assert len(events) == count + 1
    # Both stay visible, with their state.
    await flow.async_step_maintenance_void_event()
    everything = await flow.async_step_maintenance_history_filter({})
    field = next(iter(everything["data_schema"].schema.values()))
    labels = [option["label"] for option in field.config["options"]]
    assert "2 Aug 2026 · Service 2 · Corrected" in labels
    assert "3 Aug 2026 · Service 2 (fixed) · Active" in labels


async def test_an_empty_range_and_a_stale_correction_are_reported(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(asset_store_data, _schedule(CAL, "Filter"), events=_history(2))
    flow, _entry, manager = await _flow(hass, freezer, data)
    await flow.async_step_maintenance_correct_event()
    empty = await flow.async_step_maintenance_history_filter(
        {CONF_DATE_FROM: "2025-01-01", CONF_DATE_TO: "2025-01-31"}
    )
    assert empty["errors"] == {"base": "maintenance_no_matching_events"}
    await flow.async_step_maintenance_history_filter({})
    target = "e0000000-0000-4000-8000-000000000001"
    await flow.async_step_maintenance_select_event({CONF_EVENT_UUID: target})
    # Voided elsewhere while the correction form was open.
    manager._data["maintenance_events"][target]["voided_at"] = (
        "2026-09-19T00:00:00+00:00"
    )
    before = _events(manager)
    stale = await flow.async_step_maintenance_correct_event_form(
        {
            CONF_SCHEDULE_UUIDS: [CAL],
            CONF_EVENT_TITLE: "Fix",
            CONF_PERFORMED_DATE: "2026-08-01",
        }
    )
    assert stale["errors"] == {"base": "maintenance_event_not_active"}
    assert _events(manager) == before


async def test_an_unchanged_runtime_is_sent_as_stored(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    """The form shows 0.277778 h for 1000 s; leaving it as shown keeps 1000."""
    stored = _event(EVENT, "2026-09-01", schedules=[RUN], runtime="1000")
    data = _data(
        asset_store_data,
        _schedule(RUN, "Oil", calendar=False, runtime="3600"),
        events=(stored,),
    )
    flow, _entry, manager = await _flow(hass, freezer, data)
    await flow.async_step_maintenance_correct_event()
    await flow.async_step_maintenance_history_filter({})
    form = await flow.async_step_maintenance_select_event({CONF_EVENT_UUID: EVENT})
    shown = _suggested(form, CONF_EVENT_RUNTIME_HOURS)
    assert shown == 0.277778

    await flow.async_step_maintenance_correct_event_form(
        {
            CONF_SCHEDULE_UUIDS: [RUN],
            CONF_EVENT_TITLE: "Oil, correct title",
            CONF_PERFORMED_DATE: "2026-09-01",
            CONF_EVENT_RUNTIME_HOURS: shown,
        }
    )
    (corrected,) = [
        event
        for event in manager._data["maintenance_events"].values()
        if event["corrects_event_uuid"] == EVENT
    ]
    assert corrected["runtime_seconds"] == "1000"

    # A changed value is converted; a cleared one becomes unknown.
    await flow.async_step_maintenance_correct_event()
    await flow.async_step_maintenance_history_filter({})
    await flow.async_step_maintenance_select_event(
        {CONF_EVENT_UUID: corrected["event_uuid"]}
    )
    await flow.async_step_maintenance_correct_event_form(
        {
            CONF_SCHEDULE_UUIDS: [RUN],
            CONF_EVENT_TITLE: "Oil",
            CONF_PERFORMED_DATE: "2026-09-01",
        }
    )
    latest = [
        event
        for event in manager._data["maintenance_events"].values()
        if event["voided_at"] is None
    ]
    assert [event["runtime_seconds"] for event in latest] == [None]


# --- Archive ------------------------------------------------------------------------


async def test_an_archived_asset_offers_only_history_correct_and_void(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data, _schedule(CAL, "Filter"), events=_history(2), archived=True
    )
    flow, entry, manager = await _flow(hass, freezer, data)
    view = await flow.async_step_archived_assets({CONF_ASSET_UUID: ASSET_UUID})
    assert "maintenance_history" in view["menu_options"]

    # Current management is never reachable for it.
    for step in (
        flow.async_step_maintenance_menu,
        flow.async_step_maintenance_add_schedule,
        flow.async_step_maintenance_record,
        flow.async_step_maintenance_open_schedule,
    ):
        assert (await step())["step_id"] == "archived_asset"
    flow._maintenance_schedule_uuid = CAL
    for step in (
        flow.async_step_maintenance_schedule,
        flow.async_step_maintenance_mark_done,
        flow.async_step_maintenance_edit_schedule,
        flow.async_step_maintenance_intervals,
        flow.async_step_maintenance_disable_schedule,
        flow.async_step_maintenance_delete_schedule,
    ):
        assert (await step())["step_id"] == "archived_asset"

    history = await flow.async_step_maintenance_history()
    assert history["menu_options"] == [
        "maintenance_correct_event",
        "maintenance_void_event",
        "archived_asset",
    ]
    first = "e0000000-0000-4000-8000-000000000001"
    second = "e0000000-0000-4000-8000-000000000002"
    with capture_reloads(hass) as reloads:
        await flow.async_step_maintenance_void_event()
        await flow.async_step_maintenance_history_filter({})
        await flow.async_step_maintenance_select_event({CONF_EVENT_UUID: first})
        voided = await flow.async_step_maintenance_void_event_confirm(
            {CONF_CONFIRM_VOID: True}
        )
        await flow.async_step_maintenance_correct_event()
        await flow.async_step_maintenance_history_filter({})
        await flow.async_step_maintenance_select_event({CONF_EVENT_UUID: second})
        corrected = await flow.async_step_maintenance_correct_event_form(
            {
                CONF_SCHEDULE_UUIDS: [CAL],
                CONF_EVENT_TITLE: "Service 2",
                CONF_PERFORMED_DATE: "2026-08-12",
            }
        )

    reloads.assert_not_called()
    assert entry.runtime_data is manager
    assert voided["step_id"] == corrected["step_id"] == "maintenance_history"
    assert corrected["menu_options"][-1] == "archived_asset"
    events = manager._data["maintenance_events"]
    assert events[first]["voided_at"] and events[second]["voided_at"]
    assert len(events) == 3
    assert manager.asset_archived(ASSET_UUID)
    back = await flow.async_step_archived_asset()
    assert back["step_id"] == "archived_asset"


async def test_a_stale_management_form_after_archive_lands_in_the_archived_view(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    flow, entry, manager = await _flow(hass, freezer, _data(asset_store_data))
    await flow.async_step_maintenance_add_schedule(
        {CONF_SCHEDULE_NAME: "Oil", CONF_RUNTIME_HOURS: 10}
    )
    assert await manager.async_archive_asset(entry, ASSET_UUID)
    before = deepcopy(manager._data)

    with capture_reloads(hass) as reloads:
        result = await flow.async_step_maintenance_start_unknown()

    reloads.assert_not_called()
    assert result["step_id"] == "archived_asset"
    assert "archived meanwhile" in result["description_placeholders"]["result"]
    assert manager._data == before


async def test_a_record_rejected_by_archive_is_not_resurrected(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(asset_store_data, _schedule(CAL, "Filter"))
    flow, _entry, manager = await _flow(hass, freezer, data)
    await flow.async_step_maintenance_record(
        {CONF_SCHEDULE_UUIDS: [CAL], CONF_EVENT_TITLE: "Filter"}
    )
    # Archived after the guard preview: the manager decides.
    real_guards = manager.maintenance_event_guards

    def _archive_after_preview(*args: Any, **kwargs: Any) -> Any:
        result = real_guards(*args, **kwargs)
        manager._data["assets"][ASSET_UUID]["archived_at"] = "2026-09-20T00:00:00+00:00"
        return result

    manager.maintenance_event_guards = _archive_after_preview  # type: ignore[method-assign]
    result = await flow.async_step_maintenance_event_earlier(
        {CONF_PERFORMED_DATE: "2026-09-10"}
    )
    assert result["step_id"] == "archived_asset"
    assert manager._data["maintenance_events"] == {}
    assert flow._event_draft is None
    assert "maintenance_record" not in result["menu_options"]


async def test_history_opened_while_archived_follows_a_restore(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data, _schedule(CAL, "Filter"), events=_history(1), archived=True
    )
    flow, _entry, manager = await _flow(hass, freezer, data)
    await flow.async_step_archived_assets({CONF_ASSET_UUID: ASSET_UUID})
    await flow.async_step_maintenance_history()
    await flow.async_step_maintenance_void_event()
    await flow.async_step_maintenance_history_filter({})
    target = "e0000000-0000-4000-8000-000000000001"
    await flow.async_step_maintenance_select_event({CONF_EVENT_UUID: target})
    assert await manager.async_restore_asset(ASSET_UUID)

    done = await flow.async_step_maintenance_void_event_confirm(
        {CONF_CONFIRM_VOID: True}
    )
    assert manager._data["maintenance_events"][target]["voided_at"]
    # Active again: History leads back to current management.
    assert done["menu_options"][-1] == "maintenance_menu"


# --- Outcomes and persistence ---------------------------------------------------------


async def test_a_lost_acknowledgement_is_retried_as_a_replay(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(asset_store_data, _schedule(CAL, "Filter"))
    flow, _entry, manager = await _flow(hass, freezer, data)
    outcomes: list[MutationOutcome] = []
    real_mutate = manager.async_mutate_maintenance
    lost = True

    async def _lost_once(request: Any) -> Any:
        nonlocal lost
        result = await real_mutate(request)
        outcomes.append(result.outcome)
        if lost:
            lost = False
            raise AssetStorePersistenceError("acknowledgement lost")
        return result

    manager.async_mutate_maintenance = _lost_once  # type: ignore[method-assign]
    await flow.async_step_maintenance_record(
        {CONF_SCHEDULE_UUIDS: [CAL], CONF_EVENT_TITLE: "Filter"}
    )
    failed = await flow.async_step_maintenance_event_earlier(
        {CONF_PERFORMED_DATE: "2026-09-10"}
    )
    assert failed["step_id"] == "maintenance_event_earlier"
    assert failed["errors"] == {"base": "persistence_error"}

    retried = await flow.async_step_maintenance_event_earlier(
        {CONF_PERFORMED_DATE: "2026-09-10"}
    )

    assert outcomes == [MutationOutcome.CHANGED, MutationOutcome.REPLAY]
    assert retried["step_id"] == "maintenance_menu"
    assert retried["description_placeholders"]["result"] == "Maintenance recorded."
    assert len(manager._data["maintenance_events"]) == 1


async def test_a_changed_retry_of_a_landed_record_is_refused_not_duplicated(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(asset_store_data, _schedule(CAL, "Filter"))
    flow, _entry, manager = await _flow(hass, freezer, data)
    await flow.async_step_maintenance_record(
        {CONF_SCHEDULE_UUIDS: [CAL], CONF_EVENT_TITLE: "Filter"}
    )
    event_uuid = flow._event_draft["event_uuid"]
    await manager.async_mutate_maintenance(
        RecordEventRequest(
            event_uuid=event_uuid,
            asset_uuid=ASSET_UUID,
            schedule_uuids=[CAL],
            title="Filter",
            performed_date="2026-09-09",
        )
    )
    conflict = await flow.async_step_maintenance_event_earlier(
        {CONF_PERFORMED_DATE: "2026-09-10"}
    )
    assert conflict["errors"] == {"base": "maintenance_replay_conflict"}
    assert len(manager._data["maintenance_events"]) == 1


# --- Through Home Assistant ----------------------------------------------------------


async def test_real_flow_updates_maintenance_entities_without_a_reload(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    freezer: Any,
) -> None:
    freezer.move_to(TODAY)
    entry = await _load(hass, hass_storage, _data(asset_store_data))
    manager = entry.runtime_data
    registry = er.async_get(hass)
    options = hass.config_entries.options

    def _entity(domain: str, unique_id: str) -> str | None:
        return registry.async_get_entity_id(domain, DOMAIN, unique_id)

    with _verified_store_readback(hass_storage), capture_reloads(hass) as reloads:
        flow = await options.async_init(entry.entry_id)
        flow_id = flow["flow_id"]
        await options.async_configure(flow_id, {"next_step_id": "manage_asset"})
        await options.async_configure(flow_id, {CONF_ASSET_UUID: ASSET_UUID})
        await options.async_configure(flow_id, {"next_step_id": "maintenance_menu"})
        await options.async_configure(
            flow_id, {"next_step_id": "maintenance_add_schedule"}
        )
        await options.async_configure(
            flow_id,
            {
                CONF_SCHEDULE_NAME: "Filter",
                CONF_CALENDAR_VALUE: 1,
                CONF_CALENDAR_UNIT: "months",
            },
        )
        await options.async_configure(flow_id, {})
        await options.async_configure(
            flow_id, {"next_step_id": "maintenance_start_known"}
        )
        view = await options.async_configure(
            flow_id, {CONF_STARTING_DATE: "2026-08-01"}
        )
        await hass.async_block_till_done()
        assert view["step_id"] == "maintenance_schedule"
        (schedule_uuid,) = manager._data["maintenance_schedules"]
        status = _entity("sensor", maintenance_status_unique_id(schedule_uuid))
        due = _entity("sensor", maintenance_due_date_unique_id(schedule_uuid))
        assert status is not None and due is not None
        assert hass.states.get(status).state == "overdue"  # type: ignore[union-attr]
        assert hass.states.get(due).state == "2026-09-01"  # type: ignore[union-attr]
        preparation_id = maintenance_preparation_unique_id(schedule_uuid)
        assert _entity("binary_sensor", preparation_id) is None

        # Add a reminder: the preparation entity appears.
        await options.async_configure(
            flow_id, {"next_step_id": "maintenance_edit_schedule"}
        )
        await options.async_configure(
            flow_id,
            {
                CONF_SCHEDULE_NAME: "Filter",
                CONF_CALENDAR_VALUE: 1,
                CONF_CALENDAR_UNIT: "months",
            },
        )
        await options.async_configure(flow_id, {CONF_LEAD_DAYS: 3})
        await hass.async_block_till_done()
        assert _entity("binary_sensor", preparation_id) is not None

        # Record maintenance: the status follows at once.
        await options.async_configure(
            flow_id, {"next_step_id": "maintenance_mark_done"}
        )
        await options.async_configure(
            flow_id, {CONF_SCHEDULE_UUIDS: [schedule_uuid], CONF_EVENT_TITLE: "Filter"}
        )
        await options.async_configure(
            flow_id, {"next_step_id": "maintenance_event_earlier"}
        )
        lock = await options.async_configure(
            flow_id, {CONF_PERFORMED_DATE: "2026-09-15"}
        )
        assert lock["step_id"] == "maintenance_event_lock"
        await options.async_configure(flow_id, {CONF_CONFIRM_BASELINE_LOCK: True})
        await hass.async_block_till_done()
        assert hass.states.get(status).state == "ok"  # type: ignore[union-attr]
        assert hass.states.get(due).state == "2026-10-15"  # type: ignore[union-attr]

        # Remove the reminder: its entity goes.
        await options.async_configure(
            flow_id, {"next_step_id": "maintenance_edit_schedule"}
        )
        await options.async_configure(
            flow_id,
            {
                CONF_SCHEDULE_NAME: "Filter",
                CONF_CALENDAR_VALUE: 1,
                CONF_CALENDAR_UNIT: "months",
            },
        )
        await options.async_configure(flow_id, {})
        await hass.async_block_till_done()
        assert _entity("binary_sensor", preparation_id) is None

        # A second, unused Schedule is deleted: its entities go.
        await options.async_configure(flow_id, {"next_step_id": "maintenance_menu"})
        await options.async_configure(
            flow_id, {"next_step_id": "maintenance_add_schedule"}
        )
        await options.async_configure(
            flow_id, {CONF_SCHEDULE_NAME: "Spare", CONF_RUNTIME_HOURS: 5}
        )
        await options.async_configure(
            flow_id, {"next_step_id": "maintenance_start_unknown"}
        )
        await hass.async_block_till_done()
        spare = next(
            uuid
            for uuid in manager._data["maintenance_schedules"]
            if uuid != schedule_uuid
        )
        spare_status = maintenance_status_unique_id(spare)
        assert _entity("sensor", spare_status) is not None
        await options.async_configure(
            flow_id, {"next_step_id": "maintenance_delete_schedule"}
        )
        menu = await options.async_configure(
            flow_id, {CONF_CONFIRM_DELETE_SCHEDULE: True}
        )
        await hass.async_block_till_done()
        assert menu["step_id"] == "maintenance_menu"
        assert _entity("sensor", spare_status) is None

    reloads.assert_not_called()
    assert entry.runtime_data is manager
    stored = hass_storage["device_lifecycle.assets"]
    assert (stored["version"], stored["minor_version"]) == (4, 1)
    assert list(stored["data"]["maintenance_schedules"]) == [schedule_uuid]


async def test_just_now_records_the_checkpointed_runtime_of_a_live_writer(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data, _schedule(RUN, "Oil", calendar=False, runtime="3600")
    )
    freezer.move_to(TODAY)
    manager = _manager(hass, data)
    clock = _Clock(0)
    writer = _sensor(hass, manager, clock)
    await _add(hass, writer)
    writer._active_since = 0.0
    clock.value = 1800  # half an hour observed, not yet committed
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(36000)
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: ASSET_UUID})
    await flow.async_step_maintenance_record(
        {CONF_SCHEDULE_UUIDS: [RUN], CONF_EVENT_TITLE: "Oil"}
    )

    shown = await flow.async_step_maintenance_event_just_now()

    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(37800)
    assert shown["description_placeholders"]["runtime"] == "10.50 h"
    await flow.async_step_maintenance_event_just_now({})
    (event,) = manager._data["maintenance_events"].values()
    assert Decimal(event["runtime_seconds"]) == Decimal(37800)

    # Runtime goes on; the recorded maintenance keeps its value.
    clock.value = 7200
    await manager.async_checkpoint_runtime(ASSET_UUID)
    assert manager.runtime_total_seconds(ASSET_UUID) > Decimal(37800)
    assert Decimal(
        manager._data["maintenance_events"][event["event_uuid"]]["runtime_seconds"]
    ) == Decimal(37800)


# --- Edges: every step leaves stale or missing state cleanly ---------------------------


async def test_steps_without_their_draft_or_target_return_to_a_valid_view(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(asset_store_data, _schedule(CAL, "Filter"))
    flow, _entry, manager = await _flow(hass, freezer, data)
    before = deepcopy(manager._data)
    for step in (
        flow.async_step_maintenance_schedule_reminder,
        flow.async_step_maintenance_start_choice,
        flow.async_step_maintenance_start_known,
        flow.async_step_maintenance_start_unknown,
        flow.async_step_maintenance_event_details,
        flow.async_step_maintenance_event_timing,
        flow.async_step_maintenance_event_just_now,
        flow.async_step_maintenance_event_earlier,
        flow.async_step_maintenance_event_lock,
        flow.async_step_maintenance_event_ambiguity,
    ):
        assert (await step())["step_id"] == "maintenance_menu", step
    assert (await flow.async_step_maintenance_correct_event_form())["step_id"] == (
        "maintenance_history"
    )
    await _open(flow, CAL)
    for step in (
        flow.async_step_maintenance_remove_interval,
        flow.async_step_maintenance_confirm_destroy_baseline,
    ):
        assert (await step())["step_id"] == "maintenance_intervals", step
    # A Schedule deleted elsewhere: back to the Maintenance view, said once.
    del manager._data["maintenance_schedules"][CAL]
    gone = await flow.async_step_maintenance_edit_schedule()
    assert gone["step_id"] == "maintenance_menu"
    assert (
        gone["description_placeholders"]["result"] == "The schedule no longer exists."
    )
    assert manager._data == {**before, "maintenance_schedules": {}}


async def test_every_management_step_sends_an_archived_asset_to_its_view(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(asset_store_data, _schedule(CAL, "Filter"), archived=True)
    flow, _entry, _unused = await _flow(hass, freezer, data)
    flow._maintenance_schedule_uuid = CAL
    flow._event_draft = {"form": None}
    for step in (
        flow.async_step_maintenance_schedule_reminder,
        flow.async_step_maintenance_start_choice,
        flow.async_step_maintenance_start_known,
        flow.async_step_maintenance_event_details,
        flow.async_step_maintenance_event_timing,
        flow.async_step_maintenance_event_just_now,
        flow.async_step_maintenance_event_earlier,
        flow.async_step_maintenance_starting_point,
        flow.async_step_maintenance_add_calendar,
        flow.async_step_maintenance_add_runtime,
        flow.async_step_maintenance_remove_calendar,
        flow.async_step_maintenance_confirm_destroy_baseline,
        flow.async_step_maintenance_enable_schedule,
    ):
        result = await step()
        assert result["step_id"] == "archived_asset", step
    assert flow._event_draft is None
    # An unknown Asset goes back to the Asset picker.
    flow._selected_asset_uuid = "99999999-9999-4999-8999-999999999999"
    for step in (
        flow.async_step_maintenance_menu,
        flow.async_step_maintenance_history,
        flow.async_step_maintenance_history_filter,
        flow.async_step_maintenance_select_event,
        flow.async_step_maintenance_void_event_confirm,
    ):
        assert (await step())["errors"] == {"base": "asset_missing"}, step


async def test_a_manager_archive_refusal_redirects_every_writer(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    """Archived between the guard and the write: the manager's refusal wins."""
    data = _data(
        asset_store_data,
        _schedule(BOTH, "Pump", runtime="3600", anchor_date="2026-04-01"),
        events=_history(1, BOTH),
    )
    flow, _entry, manager = await _flow(hass, freezer, data)

    async def _archived(_request: Any) -> Any:
        manager._data["assets"][ASSET_UUID]["archived_at"] = "2026-09-20T00:00:00+00:00"
        raise AssetStoreError("The Asset is archived", code="asset_archived")

    def _active() -> None:
        manager._data["assets"][ASSET_UUID]["archived_at"] = None

    manager.async_mutate_maintenance = _archived  # type: ignore[method-assign]
    await _open(flow, BOTH)
    writes = [
        lambda: flow.async_step_maintenance_disable_schedule(),
        lambda: flow.async_step_maintenance_delete_schedule(
            {CONF_CONFIRM_DELETE_SCHEDULE: True}
        ),
        lambda: flow.async_step_maintenance_start_unknown(),
        lambda: flow._async_remove_interval(
            manager.asset(ASSET_UUID), IntervalDimension.RUNTIME, confirm_destroy=False
        ),
    ]
    for write in writes:
        _active()
        flow._anchor_context = {"mode": "set", "schedule_uuid": BOTH}
        result = await write()
        assert result["step_id"] == "archived_asset"
    flow._schedule_draft = {
        "schedule_uuid": BOTH,
        "name": "Pump",
        "enabled": True,
        "calendar": None,
        "runtime_seconds": "3600",
        "reminder": None,
    }
    _active()
    edit = await flow._async_save_schedule_edit(manager.asset(ASSET_UUID))
    assert edit["step_id"] == "archived_asset"
    _active()
    flow._history_event_uuid = "e0000000-0000-4000-8000-000000000001"
    void = await flow.async_step_maintenance_void_event_confirm(
        {CONF_CONFIRM_VOID: True}
    )
    assert void["step_id"] == "archived_asset"


async def test_failed_writes_stay_where_the_person_is(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data,
        _schedule(BOTH, "Pump", runtime="3600"),
        events=_history(1, BOTH),
    )
    flow, _entry, manager = await _flow(hass, freezer, data)
    await _open(flow, BOTH)
    manager._store.async_save = AsyncMock(side_effect=OSError("disk"))

    toggled = await flow.async_step_maintenance_disable_schedule()
    assert toggled["step_id"] == "maintenance_schedule"
    assert "could not be saved" in toggled["description_placeholders"]["result"]
    await flow.async_step_maintenance_edit_schedule(
        {
            CONF_SCHEDULE_NAME: "Renamed",
            CONF_CALENDAR_VALUE: 6,
            CONF_CALENDAR_UNIT: "months",
            CONF_RUNTIME_HOURS: 1,
        }
    )
    edit = await flow.async_step_maintenance_schedule_reminder({})
    assert edit["step_id"] == "maintenance_edit_schedule"
    assert edit["errors"]["base"]
    await flow.async_step_maintenance_remove_calendar()
    removal = await flow.async_step_maintenance_remove_interval(
        {CONF_CONFIRM_REMOVE_INTERVAL: True}
    )
    assert removal["step_id"] == "maintenance_intervals"
    assert "could not be saved" in removal["description_placeholders"]["result"]
    event_uuid = "e0000000-0000-4000-8000-000000000001"
    await flow.async_step_maintenance_void_event()
    await flow.async_step_maintenance_history_filter({})
    await flow.async_step_maintenance_select_event({CONF_EVENT_UUID: event_uuid})
    void = await flow.async_step_maintenance_void_event_confirm(
        {CONF_CONFIRM_VOID: True}
    )
    assert void["step_id"] == "maintenance_void_event_confirm"
    assert void["errors"]["base"]
    await flow.async_step_maintenance_correct_event()
    await flow.async_step_maintenance_history_filter({})
    await flow.async_step_maintenance_select_event({CONF_EVENT_UUID: event_uuid})
    correct = await flow.async_step_maintenance_correct_event_form(
        {
            CONF_SCHEDULE_UUIDS: [BOTH],
            CONF_EVENT_TITLE: "Service",
            CONF_PERFORMED_DATE: "2026-08-01",
            CONF_NOTES: "Checked",
            CONF_VOID_REASON: "Typo",
        }
    )
    assert correct["step_id"] == "maintenance_correct_event_form"
    assert _suggested(correct, CONF_NOTES) == "Checked"
    assert _suggested(correct, CONF_VOID_REASON) == "Typo"
    assert manager._data["maintenance_events"][event_uuid]["voided_at"] is None


async def test_forms_render_again_and_reject_invalid_input(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data,
        _schedule(BOTH, "Pump", runtime="3600"),
        _schedule(CAL, "Filter", anchor_date="2026-04-01"),
        events=_history(1, BOTH),
    )
    flow, _entry, manager = await _flow(hass, freezer, data)
    before = deepcopy(manager._data)
    await _open(flow, BOTH)

    bad_edit = await flow.async_step_maintenance_edit_schedule(
        {
            CONF_SCHEDULE_NAME: "Pump",
            CONF_CALENDAR_VALUE: 0.5,
            CONF_CALENDAR_UNIT: "months",
            CONF_RUNTIME_HOURS: 1,
        }
    )
    assert bad_edit["errors"] == {"base": "maintenance_invalid_number"}
    await flow.async_step_maintenance_edit_schedule(
        {
            CONF_SCHEDULE_NAME: "Pump",
            CONF_CALENDAR_VALUE: 6,
            CONF_CALENDAR_UNIT: "months",
            CONF_RUNTIME_HOURS: 1,
        }
    )
    again = await flow.async_step_maintenance_schedule_reminder()
    assert again["step_id"] == "maintenance_schedule_reminder"

    await _open(flow, CAL)
    empty = await flow.async_step_maintenance_add_runtime({})
    assert empty["errors"] == {"base": "maintenance_interval_required"}
    calendar_form = await flow.async_step_maintenance_add_calendar()
    assert _keys(calendar_form) == {CONF_CALENDAR_VALUE, CONF_CALENDAR_UNIT}
    await flow.async_step_maintenance_add_runtime({CONF_RUNTIME_HOURS: 5})
    bad_start = await flow.async_step_maintenance_start_known(
        {CONF_STARTING_RUNTIME_HOURS: -2}
    )
    assert bad_start["errors"] == {"base": "maintenance_invalid_number"}
    back = await flow.async_step_maintenance_start_back()
    assert back["step_id"] == "maintenance_intervals"
    await flow.async_step_maintenance_starting_point()
    back = await flow.async_step_maintenance_start_back()
    assert back["step_id"] == "maintenance_schedule"

    foreign = await flow.async_step_maintenance_record(
        {CONF_SCHEDULE_UUIDS: [OTHER_ASSET_SCHEDULE], CONF_EVENT_TITLE: "X"}
    )
    assert foreign["errors"] == {"base": "maintenance_schedule_not_found"}
    await flow.async_step_maintenance_record(
        {CONF_SCHEDULE_UUIDS: [BOTH], CONF_EVENT_TITLE: "Pump"}
    )
    bad_hours = await flow.async_step_maintenance_event_earlier(
        {CONF_PERFORMED_DATE: "2026-09-01", CONF_EVENT_RUNTIME_HOURS: -1}
    )
    assert bad_hours["errors"] == {"base": "maintenance_invalid_number"}
    manager.async_checkpoint_runtime = AsyncMock(  # type: ignore[method-assign]
        return_value=None
    )
    captured = await flow.async_step_maintenance_event_just_now()
    assert captured["description_placeholders"]["runtime"] == "not known"
    assert (await flow.async_step_maintenance_event_just_now())["step_id"] == (
        "maintenance_event_just_now"
    )
    # Choosing an earlier date after a capture starts a fresh draft.
    captured_uuid = flow._event_draft["event_uuid"]
    await flow.async_step_maintenance_event_earlier()
    assert flow._event_draft["event_uuid"] != captured_uuid
    assert flow._event_draft["captured"] is False
    assert flow._event_draft["schedule_uuids"] == [BOTH]

    # A Schedule deleted after it was chosen: the preview says so.
    await flow.async_step_maintenance_record(
        {CONF_SCHEDULE_UUIDS: [CAL], CONF_EVENT_TITLE: "Filter"}
    )
    del manager._data["maintenance_schedules"][CAL]
    missing = await flow.async_step_maintenance_event_earlier(
        {CONF_PERFORMED_DATE: "2026-09-01"}
    )
    assert missing["errors"] == {"base": "maintenance_schedule_not_found"}
    assert flow._schedule_names([CAL, BOTH]) == "Pump"
    manager._data["maintenance_schedules"][CAL] = before["maintenance_schedules"][CAL]

    # The guard steps render again when opened directly.
    flow._pending_locks = (CAL,)
    assert (await flow.async_step_maintenance_event_lock())["step_id"] == (
        "maintenance_event_lock"
    )
    flow._pending_ambiguity = (BOTH,)
    assert (await flow.async_step_maintenance_event_ambiguity())["step_id"] == (
        "maintenance_event_ambiguity"
    )
    assert manager._data["maintenance_events"] == before["maintenance_events"]


async def test_history_selection_and_correction_edges(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    other_event = _event(EVENT, "2026-08-05", schedules=[])
    other_event["asset_uuid"] = "99999999-9999-4999-8999-999999999999"
    data = _data(
        asset_store_data,
        _schedule(BOTH, "Pump", runtime="3600"),
        events=_history(1, BOTH),
    )
    flow, _entry, manager = await _flow(hass, freezer, data)
    target = "e0000000-0000-4000-8000-000000000001"

    no_history = _data(asset_store_data)
    empty_flow, _e, _m = await _flow(hass, freezer, no_history)
    empty = await empty_flow.async_step_maintenance_history()
    assert empty["menu_options"] == ["maintenance_menu"]
    assert "No maintenance recorded yet." in empty["description_placeholders"]["events"]

    await flow.async_step_maintenance_void_event()
    await flow.async_step_maintenance_history_filter({})
    again = await flow.async_step_maintenance_select_event()
    assert again["step_id"] == "maintenance_select_event"
    unknown = await flow.async_step_maintenance_select_event(
        {CONF_EVENT_UUID: "e0000000-0000-4000-8000-00000000ffff"}
    )
    assert unknown["errors"] == {"base": "maintenance_event_not_found"}
    await flow.async_step_maintenance_select_event({CONF_EVENT_UUID: target})
    shown = await flow.async_step_maintenance_void_event_confirm()
    assert shown["step_id"] == "maintenance_void_event_confirm"
    unconfirmed = await flow.async_step_maintenance_void_event_confirm(
        {CONF_CONFIRM_VOID: False}
    )
    assert unconfirmed["errors"] == {
        CONF_CONFIRM_VOID: "maintenance_confirmation_required"
    }
    # Voided elsewhere with another reason: a conflict, nothing changes.
    manager._data["maintenance_events"][target].update(
        {"voided_at": "2026-09-19T00:00:00+00:00", "void_reason": "Other"}
    )
    conflict = await flow.async_step_maintenance_void_event_confirm(
        {CONF_CONFIRM_VOID: True}
    )
    assert conflict["errors"] == {"base": "maintenance_replay_conflict"}
    flow._history_event_uuid = None
    gone = await flow.async_step_maintenance_void_event_confirm(
        {CONF_CONFIRM_VOID: True}
    )
    assert gone["errors"] == {"base": "maintenance_event_not_found"}

    manager._data["maintenance_events"][target].update(
        {"voided_at": None, "void_reason": None}
    )
    await flow.async_step_maintenance_correct_event()
    await flow.async_step_maintenance_history_filter({})
    await flow.async_step_maintenance_select_event({CONF_EVENT_UUID: target})
    assert (await flow.async_step_maintenance_correct_event_form())["step_id"] == (
        "maintenance_correct_event_form"
    )
    foreign = await flow.async_step_maintenance_correct_event_form(
        {
            CONF_SCHEDULE_UUIDS: [OTHER_ASSET_SCHEDULE],
            CONF_EVENT_TITLE: "X",
            CONF_PERFORMED_DATE: "2026-08-01",
        }
    )
    assert foreign["errors"] == {"base": "maintenance_schedule_not_found"}
    bad_hours = await flow.async_step_maintenance_correct_event_form(
        {
            CONF_SCHEDULE_UUIDS: [BOTH],
            CONF_EVENT_TITLE: "X",
            CONF_PERFORMED_DATE: "2026-08-01",
            CONF_EVENT_RUNTIME_HOURS: -3,
        }
    )
    assert bad_hours["errors"] == {"base": "maintenance_invalid_number"}
    del manager._data["maintenance_events"][target]
    vanished = await flow.async_step_maintenance_correct_event_form(
        {
            CONF_SCHEDULE_UUIDS: [BOTH],
            CONF_EVENT_TITLE: "X",
            CONF_PERFORMED_DATE: "2026-08-01",
        }
    )
    assert vanished["errors"] == {"base": "maintenance_event_not_found"}


async def test_a_schedule_deleted_mid_edit_is_reported_once(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data,
        _schedule(CAL, "Filter"),
        _schedule(RUN, "Oil", calendar=False, runtime="3600"),
        events=(_event(EVENT, "2026-09-01", schedules=[RUN], runtime="1000"),),
    )
    flow, _entry, manager = await _flow(hass, freezer, data)
    assert manager.maintenance_schedule(None) is None
    assert manager.maintenance_event(None) is None
    await _open(flow, CAL)
    await flow.async_step_maintenance_edit_schedule(
        {
            CONF_SCHEDULE_NAME: "Filter",
            CONF_CALENDAR_VALUE: 3,
            CONF_CALENDAR_UNIT: "months",
        }
    )
    del manager._data["maintenance_schedules"][CAL]
    result = await flow.async_step_maintenance_schedule_reminder({})
    assert result["step_id"] == "maintenance_menu"
    assert result["description_placeholders"]["result"] == (
        "The schedule no longer exists."
    )

    # Setting a starting point for a Schedule that is gone asks for nothing.
    flow._anchor_context = {"mode": "set", "schedule_uuid": CAL}
    empty = await flow.async_step_maintenance_start_known()
    assert _keys(empty) == set()

    # A changed Runtime in a correction is converted exactly.
    await flow.async_step_maintenance_correct_event()
    await flow.async_step_maintenance_history_filter({})
    await flow.async_step_maintenance_select_event({CONF_EVENT_UUID: EVENT})
    await flow.async_step_maintenance_correct_event_form(
        {
            CONF_SCHEDULE_UUIDS: [RUN],
            CONF_EVENT_TITLE: "Service",
            CONF_PERFORMED_DATE: "2026-09-01",
            CONF_EVENT_RUNTIME_HOURS: 0.5,
        }
    )
    (corrected,) = [
        event
        for event in manager._data["maintenance_events"].values()
        if event["corrects_event_uuid"] == EVENT
    ]
    assert corrected["runtime_seconds"] == "1800.0"


# --- Selector labels stay distinct ---------------------------------------------------

TWIN = "c0000000-0000-4000-8000-000000000004"


def _labels(form: dict[str, Any], key: str) -> dict[str, str]:
    for marker, field in form["data_schema"].schema.items():
        if marker == key:
            return {
                option["value"]: option["label"]
                for option in field.config["options"]
                if option["value"] != NOT_SELECTED
            }
    raise AssertionError(key)


def _assert_distinct(labels: dict[str, str]) -> None:
    assert len({label.casefold() for label in labels.values()}) == len(labels)


async def test_schedule_labels_use_names_then_intervals_without_identities(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data,
        _schedule(CAL, "Filter"),
        _schedule(RUN, "Belt", calendar=False, runtime="3600"),
        _schedule(BOTH, "Pump", runtime="3600"),
        _schedule(TWIN, "pump", calendar=False, runtime="3600"),
    )
    flow, _entry, _unused = await _flow(hass, freezer, data)

    labels = _labels(
        await flow.async_step_maintenance_open_schedule(), CONF_SCHEDULE_UUID
    )

    assert labels == {
        RUN: "Belt",
        CAL: "Filter",
        BOTH: "Pump (every 6 month(s) or every 1.00 h of Runtime)",
        TWIN: "pump (every 1.00 h of Runtime)",
    }
    assert not any(uuid in label for uuid in labels for label in labels.values())


async def test_identical_schedules_get_a_stable_identity_only_when_needed(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    data = _data(
        asset_store_data,
        _schedule(TWIN, "Filter"),
        _schedule(CAL, "Filter"),
        _schedule(RUN, "Belt", calendar=False, runtime="3600"),
    )
    flow, _entry, manager = await _flow(hass, freezer, data)

    first = _labels(
        await flow.async_step_maintenance_open_schedule(), CONF_SCHEDULE_UUID
    )
    assert first == {
        RUN: "Belt",
        CAL: f"Filter (every 6 month(s)) ({CAL})",
        TWIN: f"Filter (every 6 month(s)) ({TWIN})",
    }
    _assert_distinct(first)
    # The same data renders the same labels, whatever the stored order.
    manager._data["maintenance_schedules"] = dict(
        reversed(list(manager._data["maintenance_schedules"].items()))
    )
    again = _labels(
        await flow.async_step_maintenance_open_schedule(), CONF_SCHEDULE_UUID
    )
    assert again == first
    # Each label opens its own Schedule.
    view = await _open(flow, TWIN)
    assert flow._maintenance_schedule_uuid == TWIN
    assert view["step_id"] == "maintenance_schedule"

    # The same labels in Record maintenance.
    record = await flow.async_step_maintenance_record()
    assert _labels(record, CONF_SCHEDULE_UUIDS) == first
    await flow.async_step_maintenance_record(
        {CONF_SCHEDULE_UUIDS: [TWIN], CONF_EVENT_TITLE: "Filter"}
    )
    await flow.async_step_maintenance_event_earlier({CONF_PERFORMED_DATE: "2026-09-10"})
    (event,) = manager._data["maintenance_events"].values()
    assert event["schedule_uuids"] == [TWIN]

    # And in Correct.
    await flow.async_step_maintenance_correct_event()
    await flow.async_step_maintenance_history_filter({})
    correct = await flow.async_step_maintenance_select_event(
        {CONF_EVENT_UUID: event["event_uuid"]}
    )
    assert _labels(correct, CONF_SCHEDULE_UUIDS) == first
    assert _default(correct, CONF_SCHEDULE_UUIDS) == [TWIN]


async def test_identical_event_labels_get_a_stable_identity_only_when_needed(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    alike = [
        _event(
            f"e0000000-0000-4000-8000-00000000000{index}", "2026-08-10", schedules=[]
        )
        for index in (2, 1)
    ]
    single = _event(
        "e0000000-0000-4000-8000-000000000003",
        "2026-08-09",
        schedules=[],
        title="Other",
    )
    voided = _event(
        "e0000000-0000-4000-8000-000000000004",
        "2026-08-10",
        schedules=[],
        voided=True,
    )
    data = _data(asset_store_data, events=(*alike, single, voided))
    flow, _entry, manager = await _flow(hass, freezer, data)
    await flow.async_step_maintenance_void_event()

    form = await flow.async_step_maintenance_history_filter({})
    labels = _labels(form, CONF_EVENT_UUID)
    first, second = (
        event["event_uuid"] for event in sorted(alike, key=lambda e: e["event_uuid"])
    )
    assert labels == {
        first: f"10 Aug 2026 · Service · Active ({first})",
        second: f"10 Aug 2026 · Service · Active ({second})",
        # Different state, different label: no identity added.
        voided["event_uuid"]: "10 Aug 2026 · Service · Voided",
        single["event_uuid"]: "9 Aug 2026 · Other · Active",
    }
    _assert_distinct(labels)
    # History order is unchanged: newest date first, then title and identity.
    assert _options(form, CONF_EVENT_UUID) == [
        NOT_SELECTED,
        first,
        second,
        voided["event_uuid"],
        single["event_uuid"],
    ]
    assert _labels(
        await flow.async_step_maintenance_history_filter({}), CONF_EVENT_UUID
    ) == (labels)
    # The summary is not a selector and stays plain.
    history = await flow.async_step_maintenance_history()
    assert first not in history["description_placeholders"]["events"]

    await flow.async_step_maintenance_history_filter({})
    await flow.async_step_maintenance_select_event({CONF_EVENT_UUID: second})
    await flow.async_step_maintenance_void_event_confirm({CONF_CONFIRM_VOID: True})
    events = manager._data["maintenance_events"]
    assert events[second]["voided_at"] is not None
    assert events[first]["voided_at"] is None


# --- Final labels are unique even when a suffix meets user text ---------------------


def _unique(labels: dict[str, str]) -> bool:
    return len({label.casefold() for label in labels.values()}) == len(labels)


def test_a_suffix_that_meets_user_text_is_separated_again() -> None:
    """The reviewer's example: C is named exactly like A's first fallback."""
    interval = "every 6 month(s)"
    ordinary = {
        CAL: "Filter",
        TWIN: "Filter",
        RUN: f"Filter ({interval}) ({CAL})",
    }

    labels = _unique_labels(ordinary, lambda _uuid: interval)

    assert _unique(labels)
    assert labels == {
        CAL: f"Filter ({interval}) ({CAL}) ({CAL})",
        TWIN: f"Filter ({interval}) ({TWIN})",
        RUN: f"Filter ({interval}) ({CAL}) ({RUN})",
    }
    # The same result from any order of the same data.
    assert (
        _unique_labels(dict(reversed(ordinary.items())), lambda _: interval) == labels
    )


def test_chained_forgeries_and_case_still_end_unique() -> None:
    """Each forged label matches the next fallback; case is ignored."""
    ordinary = {
        CAL: "Filter",
        TWIN: "FILTER",
        RUN: f"filter ({CAL})",
        BOTH: f"Filter ({CAL}) ({CAL})",
        OTHER_ASSET_SCHEDULE: "Belt",
    }

    labels = _unique_labels(ordinary)

    assert _unique(labels)
    assert labels[OTHER_ASSET_SCHEDULE] == "Belt"
    assert labels[TWIN] == f"FILTER ({TWIN})"
    for order in (reversed(ordinary.items()), sorted(ordinary.items())):
        assert _unique_labels(dict(order)) == labels


def test_event_shaped_labels_forged_after_a_fallback_end_unique() -> None:
    first = "e0000000-0000-4000-8000-000000000001"
    second = "e0000000-0000-4000-8000-000000000002"
    third = "e0000000-0000-4000-8000-000000000003"
    plain = "10 Aug 2026 · Service · Active"
    ordinary = {first: plain, second: plain, third: f"{plain} ({first})"}

    labels = _unique_labels(ordinary)

    assert _unique(labels)
    assert labels[second] == f"{plain} ({second})"
    assert labels[first] != labels[third]
    assert _unique_labels({third: "9 Aug 2026 · Other · Active"}) == {
        third: "9 Aug 2026 · Other · Active"
    }


async def test_a_schedule_named_like_a_fallback_label_stays_distinct_in_the_flow(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    """Names are normally cut to fit a row; without the cut, the reviewer's
    example is reachable through the flow, and the labels stay distinct."""
    forged = f"Filter (every 6 month(s)) ({CAL})"
    data = _data(
        asset_store_data,
        _schedule(CAL, "Filter"),
        _schedule(TWIN, "Filter"),
        _schedule(RUN, forged),
    )
    flow, _entry, manager = await _flow(hass, freezer, data)
    with patch(
        "custom_components.device_lifecycle.config_flow._short_name",
        side_effect=lambda text, *_args: str(text),
    ):
        labels = _labels(
            await flow.async_step_maintenance_open_schedule(), CONF_SCHEDULE_UUID
        )
        record = _labels(
            await flow.async_step_maintenance_record(), CONF_SCHEDULE_UUIDS
        )
        manager._data["maintenance_schedules"] = dict(
            reversed(list(manager._data["maintenance_schedules"].items()))
        )
        again = _labels(
            await flow.async_step_maintenance_open_schedule(), CONF_SCHEDULE_UUID
        )

    assert _unique(labels)
    assert labels[RUN] != labels[CAL]
    assert labels[TWIN] == f"Filter (every 6 month(s)) ({TWIN})"
    assert record == labels == again
    view = await _open(flow, RUN)
    assert view["description_placeholders"]["schedule"] == _short_name(forged)


async def test_an_event_titled_like_a_fallback_label_stays_distinct(
    hass: HomeAssistant, asset_store_data: AssetStoreData, freezer: Any
) -> None:
    first = "e0000000-0000-4000-8000-000000000001"
    second = "e0000000-0000-4000-8000-000000000002"
    crafted = _event(
        "e0000000-0000-4000-8000-000000000003",
        "2026-08-10",
        schedules=[],
        title=f"Service · Active ({first})",
    )
    data = _data(
        asset_store_data,
        events=(
            _event(first, "2026-08-10", schedules=[]),
            _event(second, "2026-08-10", schedules=[]),
            crafted,
        ),
    )
    flow, _entry, _unused = await _flow(hass, freezer, data)
    await flow.async_step_maintenance_void_event()
    with patch(
        "custom_components.device_lifecycle.config_flow._short_name",
        side_effect=lambda text, *_args: str(text),
    ):
        labels = _labels(
            await flow.async_step_maintenance_history_filter({}), CONF_EVENT_UUID
        )

    assert _unique(labels)
    assert labels[first] == f"10 Aug 2026 · Service · Active ({first})"
    assert labels[second] == f"10 Aug 2026 · Service · Active ({second})"
    # Its ordinary label is unique, so it is unchanged.
    assert labels[crafted["event_uuid"]] == (
        f"10 Aug 2026 · Service · Active ({first}) · Active"
    )
