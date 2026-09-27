"""Maintenance entities: one current projection per Schedule (WP16).

Every Schedule has a status and a next-maintenance sensor, and a Schedule
with a preparation reminder a preparation binary_sensor. They sit on the
Asset Device, are parent-owned, and follow Store publishes and the local
civil date. Archive makes them unavailable (never ``off`` or ``disabled``);
Restore recomputes at once. A disabled Schedule reports ``disabled``,
distinct from ``unknown``. The projection is the manager's; nothing is
persisted.
"""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_platform
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.device_lifecycle import PLATFORMS
from custom_components.device_lifecycle.archive import ArchiveOutcome
from custom_components.device_lifecycle.binary_sensor import (
    MaintenancePreparationBinarySensor,
)
from custom_components.device_lifecycle.const import (
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DOMAIN,
)
from custom_components.device_lifecycle.exposure import (
    asset_device_entry,
    build_exposure_migration_plan,
)
from custom_components.device_lifecycle.maintenance_entities import (
    MaintenanceEntityCoordinator,
    maintenance_due_date_unique_id,
    maintenance_preparation_unique_id,
    maintenance_status_unique_id,
)
from custom_components.device_lifecycle.maintenance_mutations import (
    CalendarIntervalInput,
    CreateScheduleRequest,
    DeleteScheduleRequest,
    EditScheduleRequest,
    PreparationReminderInput,
    RecordEventRequest,
    SetEnabledRequest,
)
from custom_components.device_lifecycle.maintenance_projection import (
    InactiveReason,
    MaintenanceProjection,
    PreparationState,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.sensor import (
    MaintenanceDueDateSensor,
    MaintenanceStatusSensor,
)
from custom_components.device_lifecycle.storage import (
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    AssetStoreManager,
)

from .conftest import ASSET_UUID
from .test_exposure_options_reload import _verified_store_readback
from .test_stale_device_registry_references import (
    SECOND_ASSET_UUID,
    _load,
    _reload,
    _with_second_asset,
)

pytestmark = pytest.mark.real_reload

# Home Assistant's test time zone is US/Pacific: 12:00 local is 19:00 UTC.
TODAY = "2026-09-20 12:00:00-07:00"

OK = "a0000000-0000-4000-8000-000000000001"
DUE = "a0000000-0000-4000-8000-000000000002"
OVERDUE = "a0000000-0000-4000-8000-000000000003"
UNKNOWN = "a0000000-0000-4000-8000-000000000004"
DISABLED = "a0000000-0000-4000-8000-000000000005"
RUNTIME = "a0000000-0000-4000-8000-000000000006"
PREPARING = "a0000000-0000-4000-8000-000000000007"
DELETED = "a0000000-0000-4000-8000-0000000000dd"
NEW = "a0000000-0000-4000-8000-0000000000ee"
EVENT = "e0000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _readback(hass_storage: dict) -> Iterator[None]:
    """Every Store write in these tests is read back and verified."""
    with _verified_store_readback(hass_storage):
        yield


def _schedule(
    schedule_uuid: str,
    name: str,
    *,
    anchor: str | None,
    enabled: bool = True,
    lead_days: int | None = None,
    runtime: str | None = None,
    runtime_anchor: str | None = None,
    calendar: bool = True,
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
            if anchor is None and runtime_anchor is None
            else {"date": anchor, "runtime_seconds": runtime_anchor}
        ),
        "preparation_reminder": (
            None if lead_days is None else {"lead_days": lead_days, "message": None}
        ),
    }


def _data(
    asset_store_data: AssetStoreData, *schedules: dict[str, Any]
) -> AssetStoreData:
    data: dict[str, Any] = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    asset["deployment_state"] = DEPLOYMENT_STATE_NOT_DEPLOYED
    asset["runtime"]["total_seconds"] = "1000"
    data["maintenance_schedules"] = {
        schedule["schedule_uuid"]: schedule for schedule in schedules
    }
    return data  # type: ignore[return-value]


def _all_schedules() -> tuple[dict[str, Any], ...]:
    """One Schedule per projection outcome on 2026-09-20."""
    return (
        # Due 2026-10-01; its 7-day window opens on 2026-09-24.
        _schedule(OK, "Filter change", anchor="2026-04-01", lead_days=7),
        _schedule(DUE, "Descaling", anchor="2026-03-20", lead_days=7),
        _schedule(OVERDUE, "Belt check", anchor="2026-03-01"),
        # No baseline: the calendar due date is not known.
        _schedule(UNKNOWN, "Gasket", anchor=None, lead_days=7),
        _schedule(DISABLED, "Paused", anchor="2026-03-01", enabled=False, lead_days=7),
        _schedule(
            RUNTIME,
            "Oil",
            anchor=None,
            calendar=False,
            runtime="3600",
            runtime_anchor="0",
        ),
        # Due 2026-09-25: inside its 7-day window.
        _schedule(PREPARING, "Inspection", anchor="2026-03-25", lead_days=7),
    )


def _entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)


def _state(hass: HomeAssistant, domain: str, unique_id: str) -> Any:
    entity_id = _entity_id(hass, domain, unique_id)
    assert entity_id is not None, unique_id
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return state


def _status(hass: HomeAssistant, schedule_uuid: str) -> str:
    return _state(hass, "sensor", maintenance_status_unique_id(schedule_uuid)).state


def _due(hass: HomeAssistant, schedule_uuid: str) -> str:
    return _state(hass, "sensor", maintenance_due_date_unique_id(schedule_uuid)).state


def _preparation(hass: HomeAssistant, schedule_uuid: str) -> str:
    return _state(
        hass, "binary_sensor", maintenance_preparation_unique_id(schedule_uuid)
    ).state


def _entity(hass: HomeAssistant, domain: str, unique_id: str) -> Any:
    entity_id = _entity_id(hass, domain, unique_id)
    for platform in entity_platform.async_get_platforms(hass, DOMAIN):
        if platform.domain == domain and entity_id in platform.entities:
            return platform.entities[entity_id]
    raise AssertionError(unique_id)


def _maintenance_registry(hass: HomeAssistant, entry: Any) -> dict[str, tuple]:
    return {
        item.unique_id: (
            item.id,
            item.entity_id,
            item.domain,
            item.config_entry_id,
            item.config_subentry_id,
            item.device_id,
            item.disabled_by,
        )
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
        if "_maintenance_" in item.unique_id
    }


async def _setup(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    freezer: Any,
    *schedules: dict[str, Any],
) -> Any:
    freezer.move_to(TODAY)
    return await _load(hass, hass_storage, _data(asset_store_data, *schedules))


# --- Identity and placement ------------------------------------------------------


async def test_entities_have_schedule_identities_on_the_asset_device(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    freezer: Any,
) -> None:
    entry = await _setup(
        hass, hass_storage, asset_store_data, freezer, *_all_schedules()
    )
    assert PLATFORMS == ["binary_sensor", "sensor"]
    device = asset_device_entry(
        dr.async_get(hass), config_entry_id=entry.entry_id, asset_uuid=ASSET_UUID
    )
    assert device is not None
    registry = _maintenance_registry(hass, entry)

    with_reminder = {OK, DUE, UNKNOWN, DISABLED, PREPARING}
    expected = {
        *(
            f"{uuid}_{kind}"
            for uuid in (OK, DUE, OVERDUE, UNKNOWN, DISABLED, RUNTIME, PREPARING)
            for kind in ("maintenance_status", "maintenance_due_date")
        ),
        *(f"{uuid}_maintenance_preparation" for uuid in with_reminder),
    }
    assert set(registry) == expected
    for unique_id, (
        _id,
        _eid,
        domain,
        entry_id,
        subentry,
        device_id,
        disabled,
    ) in registry.items():
        assert domain == (
            "binary_sensor" if unique_id.endswith("_preparation") else "sensor"
        )
        assert entry_id == entry.entry_id
        assert subentry is None
        assert device_id == device.id
        assert disabled is None
        # Only the Schedule UUID: no name, Asset ID, or Asset UUID.
        assert unique_id.split("_", 1)[0] in {
            OK,
            DUE,
            OVERDUE,
            UNKNOWN,
            DISABLED,
            RUNTIME,
            PREPARING,
        }
        assert "DL0007" not in unique_id
        assert ASSET_UUID not in unique_id

    status = _state(hass, "sensor", maintenance_status_unique_id(OK))
    assert status.attributes["device_class"] == "enum"
    assert status.attributes["options"] == [
        "ok",
        "unknown",
        "due",
        "overdue",
        "disabled",
    ]
    assert status.attributes["friendly_name"].endswith("Maintenance Filter change")
    due = _state(hass, "sensor", maintenance_due_date_unique_id(OK))
    assert due.attributes["device_class"] == "date"
    preparation = _state(hass, "binary_sensor", maintenance_preparation_unique_id(OK))
    assert "device_class" not in preparation.attributes

    # No history, no Store records, no Runtime internals: presentation only.
    for state in (status, due, preparation):
        assert set(state.attributes) <= {
            "device_class",
            "friendly_name",
            "icon",
            "options",
        }


async def test_reload_keeps_every_identity(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    freezer: Any,
) -> None:
    entry = await _setup(
        hass, hass_storage, asset_store_data, freezer, *_all_schedules()
    )
    before = _maintenance_registry(hass, entry)

    await _reload(hass, hass_storage, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert _maintenance_registry(hass, entry) == before
    assert _status(hass, OVERDUE) == "overdue"


# --- Projection mapping ----------------------------------------------------------


async def test_every_projection_outcome_is_presented(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    freezer: Any,
) -> None:
    await _setup(hass, hass_storage, asset_store_data, freezer, *_all_schedules())

    assert _status(hass, OK) == "ok"
    assert _status(hass, DUE) == "due"
    assert _status(hass, OVERDUE) == "overdue"
    assert _status(hass, UNKNOWN) == "unknown"
    assert _status(hass, DISABLED) == "disabled"
    assert _status(hass, RUNTIME) == "ok"

    assert _due(hass, OK) == "2026-10-01"
    assert _due(hass, DUE) == "2026-09-20"
    assert _due(hass, OVERDUE) == "2026-09-01"
    assert _due(hass, UNKNOWN) == STATE_UNKNOWN
    assert _due(hass, DISABLED) == STATE_UNKNOWN
    # Runtime-only: no date is ever made up from Runtime.
    assert _due(hass, RUNTIME) == STATE_UNKNOWN

    assert _preparation(hass, PREPARING) == STATE_ON
    assert _preparation(hass, OK) == STATE_OFF
    # Once due, the reminder no longer suggests that time remains.
    assert _preparation(hass, DUE) == STATE_OFF
    assert _preparation(hass, UNKNOWN) == STATE_UNKNOWN
    assert (
        _entity(hass, "binary_sensor", maintenance_preparation_unique_id(UNKNOWN)).is_on
        is None
    )
    # A disabled Schedule has no active projection, so nothing is prepared.
    assert _preparation(hass, DISABLED) == STATE_OFF


async def test_disabled_unknown_and_archived_stay_distinct(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    freezer: Any,
) -> None:
    entry = await _setup(
        hass, hass_storage, asset_store_data, freezer, *_all_schedules()
    )
    manager: AssetStoreManager = entry.runtime_data

    disabled = _entity(hass, "sensor", maintenance_status_unique_id(DISABLED))
    unknown = _entity(hass, "sensor", maintenance_status_unique_id(UNKNOWN))
    assert (disabled.native_value, disabled.available) == ("disabled", True)
    assert (unknown.native_value, unknown.available) == ("unknown", True)

    await manager.async_archive_asset(entry, ASSET_UUID)
    await hass.async_block_till_done()

    for schedule_uuid in (DISABLED, UNKNOWN, OK):
        entity = _entity(hass, "sensor", maintenance_status_unique_id(schedule_uuid))
        assert entity.available is False
        assert _status(hass, schedule_uuid) == STATE_UNAVAILABLE


# --- Archive and Restore -------------------------------------------------------


async def test_archive_makes_entities_unavailable_and_restore_recomputes(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    freezer: Any,
) -> None:
    entry = await _setup(
        hass, hass_storage, asset_store_data, freezer, *_all_schedules()
    )
    manager: AssetStoreManager = entry.runtime_data
    # The person's own choice is never overwritten.
    registry = er.async_get(hass)
    user_disabled = _entity_id(hass, "sensor", maintenance_due_date_unique_id(DUE))
    assert user_disabled is not None
    registry.async_update_entity(
        user_disabled, disabled_by=er.RegistryEntryDisabler.USER
    )
    before = _maintenance_registry(hass, entry)
    reloads: list[str] = []
    entry.async_on_unload(lambda: reloads.append("unloaded"))

    assert await manager.async_archive_asset(entry, ASSET_UUID) is (
        ArchiveOutcome.CHANGED
    )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is manager
    assert reloads == []
    assert _maintenance_registry(hass, entry) == before
    for schedule_uuid in (OK, DUE, OVERDUE, UNKNOWN, DISABLED, RUNTIME, PREPARING):
        assert _status(hass, schedule_uuid) == STATE_UNAVAILABLE
        if schedule_uuid != DUE:
            assert _due(hass, schedule_uuid) == STATE_UNAVAILABLE
    for schedule_uuid in (OK, DUE, UNKNOWN, DISABLED, PREPARING):
        # Archived is not off.
        assert _preparation(hass, schedule_uuid) == STATE_UNAVAILABLE

    # Time passes while archived; Restore shows the result at once.
    freezer.move_to("2026-10-05 12:00:00-07:00")
    assert await manager.async_restore_asset(ASSET_UUID) is ArchiveOutcome.CHANGED
    await hass.async_block_till_done()

    assert reloads == []
    assert _maintenance_registry(hass, entry) == before
    assert before[maintenance_due_date_unique_id(DUE)][6] is (
        er.RegistryEntryDisabler.USER
    )
    assert _status(hass, OK) == "overdue"
    assert _status(hass, DISABLED) == "disabled"
    assert _status(hass, UNKNOWN) == "unknown"
    assert _due(hass, OK) == "2026-10-01"
    assert _preparation(hass, PREPARING) == STATE_OFF
    assert _preparation(hass, UNKNOWN) == STATE_UNKNOWN


# --- Refresh ---------------------------------------------------------------------


async def test_schedule_and_event_commits_refresh_without_a_reload(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    freezer: Any,
) -> None:
    entry = await _setup(
        hass,
        hass_storage,
        asset_store_data,
        freezer,
        _schedule(OVERDUE, "Belt check", anchor="2026-03-01"),
    )
    manager: AssetStoreManager = entry.runtime_data
    before = _maintenance_registry(hass, entry)
    status_id = _entity_id(hass, "sensor", maintenance_status_unique_id(OVERDUE))

    await manager.async_mutate_maintenance(SetEnabledRequest(OVERDUE, False))
    await hass.async_block_till_done()
    assert _status(hass, OVERDUE) == "disabled"

    await manager.async_mutate_maintenance(SetEnabledRequest(OVERDUE, True))
    await manager.async_mutate_maintenance(
        EditScheduleRequest(
            schedule_uuid=OVERDUE,
            name="Belt replacement",
            enabled=True,
            calendar_interval=CalendarIntervalInput(1, "years"),
            runtime_interval_seconds=None,
            preparation_reminder=None,
        )
    )
    await hass.async_block_till_done()
    state = _state(hass, "sensor", maintenance_status_unique_id(OVERDUE))
    assert state.entity_id == status_id
    assert state.state == "ok"
    assert state.attributes["friendly_name"].endswith("Maintenance Belt replacement")
    assert _due(hass, OVERDUE) == "2027-03-01"

    await manager.async_mutate_maintenance(
        EditScheduleRequest(
            schedule_uuid=OVERDUE,
            name="Belt replacement",
            enabled=True,
            calendar_interval=CalendarIntervalInput(3, "months"),
            runtime_interval_seconds=None,
            preparation_reminder=None,
        )
    )
    await hass.async_block_till_done()
    assert _status(hass, OVERDUE) == "overdue"

    await manager.async_mutate_maintenance(
        RecordEventRequest(
            event_uuid=EVENT,
            asset_uuid=ASSET_UUID,
            schedule_uuids=[OVERDUE],
            title="Belt replaced",
            performed_date="2026-09-20",
            confirmed_baseline_locks=frozenset({OVERDUE}),
        )
    )
    await hass.async_block_till_done()
    assert _status(hass, OVERDUE) == "ok"
    assert _due(hass, OVERDUE) == "2026-12-20"
    assert _maintenance_registry(hass, entry) == before
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is manager


async def test_runtime_commit_refreshes_the_projection_at_once(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    freezer: Any,
) -> None:
    entry = await _setup(
        hass,
        hass_storage,
        asset_store_data,
        freezer,
        _schedule(
            RUNTIME,
            "Oil",
            anchor=None,
            calendar=False,
            runtime="3600",
            runtime_anchor="0",
        ),
    )
    manager: AssetStoreManager = entry.runtime_data
    assert _status(hass, RUNTIME) == "ok"

    await manager.async_commit_runtime_delta(
        ASSET_UUID, expected_total=Decimal(1000), delta=Decimal(2600)
    )
    await hass.async_block_till_done()
    assert _status(hass, RUNTIME) == "due"

    await manager.async_commit_runtime_delta(
        ASSET_UUID, expected_total=Decimal(3600), delta=Decimal(1)
    )
    await hass.async_block_till_done()
    assert _status(hass, RUNTIME) == "overdue"
    assert _due(hass, RUNTIME) == STATE_UNKNOWN
    # Nothing is persisted about the projection.
    stored = hass_storage["device_lifecycle.assets"]["data"]
    assert stored["maintenance_schedules"][RUNTIME] == _schedule(
        RUNTIME, "Oil", anchor=None, calendar=False, runtime="3600", runtime_anchor="0"
    )
    assert (
        hass_storage["device_lifecycle.assets"]["version"],
        hass_storage["device_lifecycle.assets"]["minor_version"],
    ) == (STORAGE_VERSION, STORAGE_MINOR_VERSION)


async def test_local_midnight_refreshes_by_the_local_civil_date(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    freezer: Any,
) -> None:
    """Due 2026-10-01, window from 2026-09-24; US/Pacific is UTC-7."""
    entry = await _setup(
        hass,
        hass_storage,
        asset_store_data,
        freezer,
        _schedule(OK, "Filter change", anchor="2026-04-01", lead_days=7),
    )
    manager: AssetStoreManager = entry.runtime_data
    saves: list[Any] = []
    original_save = manager._store.async_save

    async def _spy(data: Any) -> None:
        saves.append(data)
        await original_save(data)

    manager._store.async_save = _spy  # type: ignore[method-assign]

    # 23:59:59 local on 09-23 is already 09-24 in UTC: still not preparing.
    freezer.move_to("2026-09-24 06:59:59+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _preparation(hass, OK) == STATE_OFF
    assert _status(hass, OK) == "ok"

    freezer.move_to("2026-09-24 07:00:00+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _preparation(hass, OK) == STATE_ON

    # 23:59:59 local on 09-30 is 10-01 in UTC: not due yet.
    freezer.move_to("2026-10-01 06:59:59+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _status(hass, OK) == "ok"
    assert _preparation(hass, OK) == STATE_ON

    freezer.move_to("2026-10-01 07:00:00+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _status(hass, OK) == "due"
    assert _preparation(hass, OK) == STATE_OFF

    freezer.move_to("2026-10-02 07:00:00+00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _status(hass, OK) == "overdue"
    assert saves == []


# --- Entity set changes and cleanup ------------------------------------------------


async def test_new_and_deleted_schedules_follow_without_a_reload(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    freezer: Any,
) -> None:
    entry = await _setup(
        hass,
        hass_storage,
        asset_store_data,
        freezer,
        _schedule(OK, "Filter change", anchor="2026-04-01"),
    )
    manager: AssetStoreManager = entry.runtime_data
    kept = _maintenance_registry(hass, entry)

    await manager.async_mutate_maintenance(
        CreateScheduleRequest(
            schedule_uuid=NEW,
            asset_uuid=ASSET_UUID,
            name="Descaling",
            calendar_interval=CalendarIntervalInput(1, "months"),
            preparation_reminder=PreparationReminderInput(3),
        )
    )
    await hass.async_block_till_done()
    assert _status(hass, NEW) == "unknown"
    assert _preparation(hass, NEW) == STATE_UNKNOWN
    created = _maintenance_registry(hass, entry)
    assert set(created) - set(kept) == {
        maintenance_status_unique_id(NEW),
        maintenance_due_date_unique_id(NEW),
        maintenance_preparation_unique_id(NEW),
    }

    await manager.async_mutate_maintenance(DeleteScheduleRequest(NEW))
    await hass.async_block_till_done()
    assert _maintenance_registry(hass, entry) == kept
    for unique_id, domain in (
        (maintenance_status_unique_id(NEW), "sensor"),
        (maintenance_preparation_unique_id(NEW), "binary_sensor"),
    ):
        assert hass.states.get(created[unique_id][1]) is None, domain
    assert entry.state is ConfigEntryState.LOADED


async def test_removing_and_re_adding_a_reminder_reuses_the_identity(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    freezer: Any,
) -> None:
    entry = await _setup(
        hass,
        hass_storage,
        asset_store_data,
        freezer,
        _schedule(PREPARING, "Inspection", anchor="2026-03-25", lead_days=7),
    )
    manager: AssetStoreManager = entry.runtime_data
    before = _maintenance_registry(hass, entry)
    unique_id = maintenance_preparation_unique_id(PREPARING)
    entity_id = before[unique_id][1]

    def _edit(reminder: PreparationReminderInput | None) -> EditScheduleRequest:
        return EditScheduleRequest(
            schedule_uuid=PREPARING,
            name="Inspection",
            enabled=True,
            calendar_interval=CalendarIntervalInput(6, "months"),
            runtime_interval_seconds=None,
            preparation_reminder=reminder,
        )

    await manager.async_mutate_maintenance(_edit(None))
    await hass.async_block_till_done()
    removed = _maintenance_registry(hass, entry)
    assert unique_id not in removed
    assert hass.states.get(entity_id) is None
    # Status and date keep their identity through the edit.
    assert removed == {key: value for key, value in before.items() if key != unique_id}

    await manager.async_mutate_maintenance(_edit(PreparationReminderInput(7)))
    await hass.async_block_till_done()
    readded = _maintenance_registry(hass, entry)
    assert readded[unique_id][1] == entity_id
    assert not readded[unique_id][1].endswith("_2")
    assert _preparation(hass, PREPARING) == STATE_ON

    # Setup agrees with the incremental result.
    await _reload(hass, hass_storage, entry)
    assert _maintenance_registry(hass, entry) == readded


async def test_setup_removes_only_orphaned_maintenance_entities(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    freezer: Any,
) -> None:
    """A hard-deleted Schedule's entities and a removed reminder's entity go;
    everything else stays: current Maintenance, Asset and Runtime entities,
    and other integrations' entries with the same suffixes."""
    entry = await _setup(
        hass,
        hass_storage,
        asset_store_data,
        freezer,
        _schedule(OK, "Filter change", anchor="2026-04-01"),
    )
    registry = er.async_get(hass)
    orphans = {
        registry.async_get_or_create(
            domain, DOMAIN, unique_id, config_entry=entry
        ).entity_id
        for domain, unique_id in (
            ("sensor", maintenance_status_unique_id(DELETED)),
            ("sensor", maintenance_due_date_unique_id(DELETED)),
            ("binary_sensor", maintenance_preparation_unique_id(DELETED)),
            # The Schedule exists but has no reminder.
            ("binary_sensor", maintenance_preparation_unique_id(OK)),
        )
    }
    foreign = registry.async_get_or_create(
        "sensor", "other_integration", maintenance_status_unique_id(DELETED)
    ).entity_id
    kept = {
        item.entity_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.entity_id not in orphans
    }

    # A publish only follows the entities it knows; setup reconciles.
    await entry.runtime_data.async_mutate_maintenance(SetEnabledRequest(OK, False))
    await hass.async_block_till_done()
    assert all(registry.async_get(entity_id) for entity_id in orphans)

    await _reload(hass, hass_storage, entry)

    remaining = {
        item.entity_id
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    assert remaining == kept
    assert not orphans & remaining
    assert registry.async_get(foreign) is not None
    assert _status(hass, OK) == "disabled"


async def test_exposure_preflight_ignores_the_maintenance_suffixes(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    freezer: Any,
) -> None:
    entry = await _setup(
        hass, hass_storage, asset_store_data, freezer, *_all_schedules()
    )
    manager: AssetStoreManager = entry.runtime_data
    for unique_id in _maintenance_registry(hass, entry):
        assert not unique_id.endswith(("_lifecycle", "_runtime_hours"))

    plan = build_exposure_migration_plan(
        entry=entry,
        assets=manager.assets(),
        device_registry=dr.async_get(hass),
        entity_registry=er.async_get(hass),
    )

    planned = {update.unique_id for update in plan.entity_updates}
    assert not {item for item in planned if "_maintenance_" in item}
    # And the real preflight runs again on reload without complaint.
    await _reload(hass, hass_storage, entry)
    assert entry.state is ConfigEntryState.LOADED


# --- Entity edges ----------------------------------------------------------------


class _Manager:
    """Only what an entity reads: the projection and the Archive state."""

    def __init__(self, projection: MaintenanceProjection | None) -> None:
        self.projection = projection
        self.archived = False

    def maintenance_projection(self, _schedule_uuid: str) -> Any:
        return self.projection

    def asset_archived(self, _asset_uuid: str) -> bool:
        return self.archived


def _projection(
    *, active: bool = True, preparation: PreparationState | None = None
) -> MaintenanceProjection:
    return MaintenanceProjection(
        active=active,
        anchor=None,  # type: ignore[arg-type]
        calendar=None,
        runtime=None,
        combined_state=None,
        preparation=preparation,
        inactive_reason=None if active else InactiveReason.ASSET_ARCHIVED,
    )


@pytest.mark.parametrize(
    ("preparation", "expected"),
    [
        (PreparationState.ACTIVE, True),
        (PreparationState.INACTIVE, False),
        (PreparationState.UNKNOWN, None),
    ],
)
def test_preparation_maps_each_state_exactly(
    preparation: PreparationState, expected: bool | None
) -> None:
    manager = _Manager(_projection(preparation=preparation))
    entity = MaintenancePreparationBinarySensor(
        manager=manager,  # type: ignore[arg-type]
        schedule=_schedule(OK, "Filter change", anchor=None, lead_days=7),  # type: ignore[arg-type]
        device_entry=None,  # type: ignore[arg-type]
        unique_id=maintenance_preparation_unique_id(OK),
    )
    assert entity.is_on is expected
    assert entity.available is True
    manager.archived = True
    assert entity.available is False


def test_a_vanished_schedule_renders_nothing() -> None:
    schedule = _schedule(OK, "Filter change", anchor=None)
    for cls in (
        MaintenanceStatusSensor,
        MaintenanceDueDateSensor,
        MaintenancePreparationBinarySensor,
    ):
        entity = cls(
            manager=_Manager(None),  # type: ignore[arg-type]
            schedule=schedule,  # type: ignore[arg-type]
            device_entry=None,  # type: ignore[arg-type]
            unique_id="x",
        )
        assert entity.available is False
        value = (
            entity.is_on
            if isinstance(entity, MaintenancePreparationBinarySensor)
            else entity.native_value
        )
        assert value is None
    # An archived projection presents nothing; availability says why.
    status = MaintenanceStatusSensor(
        manager=_Manager(_projection(active=False)),  # type: ignore[arg-type]
        schedule=schedule,  # type: ignore[arg-type]
        device_entry=None,  # type: ignore[arg-type]
        unique_id="x",
    )
    assert status.native_value is None


async def test_a_publish_touches_only_the_changed_assets_entities(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    freezer: Any,
) -> None:
    data: dict[str, Any] = _with_second_asset(  # type: ignore[assignment]
        _data(asset_store_data, _schedule(OK, "Filter change", anchor="2026-04-01"))
    )
    data["assets"][SECOND_ASSET_UUID]["ha_device_refs"] = []
    other = _schedule(DUE, "Descaling", anchor="2026-03-20")
    other["asset_uuid"] = SECOND_ASSET_UUID
    data["maintenance_schedules"][DUE] = other
    freezer.move_to(TODAY)
    entry = await _load(hass, hass_storage, data)  # type: ignore[arg-type]
    manager: AssetStoreManager = entry.runtime_data
    registry = _maintenance_registry(hass, entry)
    assert (
        registry[maintenance_status_unique_id(DUE)][5]
        != (registry[maintenance_status_unique_id(OK)][5])
    )
    other_state = _state(hass, "sensor", maintenance_status_unique_id(DUE))

    await manager.async_mutate_maintenance(SetEnabledRequest(OK, False))
    await hass.async_block_till_done()

    assert _status(hass, OK) == "disabled"
    assert _state(hass, "sensor", maintenance_status_unique_id(DUE)) is other_state
    assert _status(hass, DUE) == "due"


async def test_coordinator_edges(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    freezer: Any,
) -> None:
    entry = await _setup(hass, hass_storage, asset_store_data, freezer)
    manager: AssetStoreManager = entry.runtime_data
    added: list[Any] = []
    schedule = _schedule(NEW, "Descaling", anchor=None)
    device = asset_device_entry(
        dr.async_get(hass), config_entry_id=entry.entry_id, asset_uuid=ASSET_UUID
    )

    def _twice(*_args: Any) -> list[Any]:
        return [
            MaintenanceStatusSensor(
                manager=manager,
                schedule=schedule,  # type: ignore[arg-type]
                device_entry=device,  # type: ignore[arg-type]
                unique_id=maintenance_status_unique_id(NEW),
            )
            for _ in range(2)
        ]

    coordinator = MaintenanceEntityCoordinator(
        hass,
        entry,
        manager,
        domain="sensor",
        suffixes=("_maintenance_status",),
        expected_unique_ids=lambda item: {
            maintenance_status_unique_id(item["schedule_uuid"])
        },
        factory=_twice,
        async_add_entities=added.extend,  # type: ignore[arg-type]
    )
    # A new Asset without its Asset Device yet gets its entities at setup.
    assert coordinator._build("not-an-asset-uuid", [schedule]) == []  # type: ignore[list-item]
    # One entity per unique ID, whatever the factory returns.
    assert len(coordinator._build(ASSET_UUID, [schedule])) == 1  # type: ignore[list-item]
    assert coordinator._build(ASSET_UUID, [schedule]) == []  # type: ignore[list-item]

    # Midnight leaves an entity whose Schedule is gone alone.
    gone = MagicMock(schedule_uuid=DELETED, asset_uuid=ASSET_UUID)
    coordinator._entities["gone"] = gone
    coordinator._handle_midnight(None)
    gone.refresh_projection.assert_not_called()
    assert added == []
