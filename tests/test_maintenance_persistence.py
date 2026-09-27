"""Maintenance through the Store manager's verified persistence (WP11).

The pure Maintenance layer owns every rule; these tests prove the transport:
the request runs on the locked mutation candidate, CHANGED writes only the
two Maintenance collections through the verified Store 4.1 save, NO_OP and
REPLAY write nothing, and an ambiguous save is settled from the persisted
Store by replaying the same request. Unverified state is never published.
"""

from __future__ import annotations

import ast
import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.device_lifecycle import storage
from custom_components.device_lifecycle.const import DEPLOYMENT_STATE_NOT_DEPLOYED
from custom_components.device_lifecycle.maintenance_mutations import (
    AddIntervalRequest,
    CalendarIntervalInput,
    CorrectEventRequest,
    CreateScheduleRequest,
    DeleteScheduleRequest,
    EditScheduleRequest,
    InitialAnchorInput,
    IntervalDimension,
    MaintenanceArchivedAssetError,
    MaintenanceConfirmationRequiredError,
    MutationOutcome,
    RecordEventRequest,
    RemoveIntervalRequest,
    SetEnabledRequest,
    SetInitialAnchorRequest,
    VoidEventRequest,
)
from custom_components.device_lifecycle.maintenance_projection import (
    DueState,
    InactiveReason,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    STORAGE_KEY,
    AssetStoreError,
    AssetStoreManager,
    AssetStorePersistenceError,
    DeviceLifecycleStore,
    _validate_store_data,
)

from .conftest import ASSET_UUID

SCHEDULE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
SCHEDULE_2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
EVENT = "cccccccc-cccc-4ccc-8ccc-ccccccccccc1"
EVENT_2 = "cccccccc-cccc-4ccc-8ccc-ccccccccccc2"
MISSING = "dddddddd-dddd-4ddd-8ddd-ddddddddddd9"
ARCHIVED_AT = "2026-09-26T12:34:56.123456+00:00"
RUNTIME = "5000"
# Well in the past, whatever the test clock says.
PERFORMED = "2024-03-01"
NON_MAINTENANCE_KEYS = (
    "next_asset_number",
    "purchases",
    "assets",
    "lifecycle_events",
    "replacement_records",
)


# --- Helpers -----------------------------------------------------------------


def _create(**values: Any) -> CreateScheduleRequest:
    request: dict[str, Any] = {
        "schedule_uuid": SCHEDULE,
        "asset_uuid": ASSET_UUID,
        "name": "Filter change",
        "calendar_interval": CalendarIntervalInput(6, "months"),
    }
    request.update(values)
    return CreateScheduleRequest(**request)


def _record(**values: Any) -> RecordEventRequest:
    request: dict[str, Any] = {
        "event_uuid": EVENT,
        "asset_uuid": ASSET_UUID,
        "schedule_uuids": [SCHEDULE],
        "title": "Filter change",
        "performed_date": PERFORMED,
    }
    request.update(values)
    return RecordEventRequest(**request)


def _correct(**values: Any) -> CorrectEventRequest:
    request: dict[str, Any] = {
        "new_event_uuid": EVENT_2,
        "target_event_uuid": EVENT,
        "schedule_uuids": [SCHEDULE],
        "title": "Filter change",
        "performed_date": "2024-02-01",
    }
    request.update(values)
    return CorrectEventRequest(**request)


def _base(asset_store_data: AssetStoreData) -> dict[str, Any]:
    data: dict[str, Any] = deepcopy(asset_store_data)
    data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = RUNTIME
    _validate_store_data(data)
    return data


def _with_schedule_and_event(asset_store_data: AssetStoreData) -> dict[str, Any]:
    data = _base(asset_store_data)
    data["maintenance_schedules"][SCHEDULE] = {
        "schedule_uuid": SCHEDULE,
        "asset_uuid": ASSET_UUID,
        "name": "Filter change",
        "enabled": True,
        "calendar_interval": {"value": 6, "unit": "months"},
        "runtime_interval_seconds": None,
        "initial_anchor": None,
        "preparation_reminder": None,
    }
    data["maintenance_events"][EVENT] = {
        "event_uuid": EVENT,
        "asset_uuid": ASSET_UUID,
        "schedule_uuids": [SCHEDULE],
        "title": "Filter change",
        "performed_date": PERFORMED,
        "runtime_seconds": None,
        "recorded_at": "2024-03-01T10:00:00+00:00",
        "notes": None,
        "voided_at": None,
        "void_reason": None,
        "corrects_event_uuid": None,
    }
    _validate_store_data(data)
    return data


def _archive(data: dict[str, Any]) -> None:
    asset = data["assets"][ASSET_UUID]
    asset["deployment_state"] = DEPLOYMENT_STATE_NOT_DEPLOYED
    asset["ha_area_id"] = None
    asset["archived_at"] = ARCHIVED_AT


def _envelope(data: Any) -> dict[str, Any]:
    return {"version": 4, "minor_version": 1, "key": STORAGE_KEY, "data": data}


def _bytes(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


@contextmanager
def _readback(hass_storage: dict[str, Any]) -> Iterator[None]:
    with patch.object(
        storage.json_util,
        "load_json",
        side_effect=lambda _path: deepcopy(hass_storage[STORAGE_KEY]),
    ):
        yield


@contextmanager
def _save_spy() -> Iterator[Any]:
    with patch.object(
        DeviceLifecycleStore,
        "async_save",
        autospec=True,
        side_effect=DeviceLifecycleStore.async_save,
    ) as save:
        yield save


async def _manager(
    hass: HomeAssistant, hass_storage: dict[str, Any], data: dict[str, Any]
) -> AssetStoreManager:
    """A manager set up from a persisted, valid Store 4.1 file."""
    hass_storage[STORAGE_KEY] = _envelope(deepcopy(data))
    manager = AssetStoreManager(hass)
    with _readback(hass_storage):
        await manager.async_setup()
    return manager


def _non_maintenance(data: Any) -> dict[str, Any]:
    return {key: deepcopy(data[key]) for key in NON_MAINTENANCE_KEYS}


# --- CHANGED, NO_OP, REPLAY -----------------------------------------------------


async def test_changed_persists_only_maintenance_through_verified_save(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    manager = await _manager(hass, hass_storage, _base(asset_store_data))
    before = deepcopy(manager._data)
    published_during_save: list[bool] = []
    original_save = DeviceLifecycleStore.async_save

    async def _observed_save(store: DeviceLifecycleStore, data: Any) -> None:
        # Nothing is published before the write is verified.
        published_during_save.append(SCHEDULE in manager._data["maintenance_schedules"])
        await original_save(store, data)

    with (
        _readback(hass_storage),
        patch.object(
            DeviceLifecycleStore,
            "async_save",
            autospec=True,
            side_effect=_observed_save,
        ) as save,
    ):
        result = await manager.async_mutate_maintenance(_create())

    assert result.outcome is MutationOutcome.CHANGED
    save.assert_called_once()
    assert published_during_save == [False]
    persisted = hass_storage[STORAGE_KEY]
    assert (persisted["version"], persisted["minor_version"]) == (4, 1)
    _validate_store_data(persisted["data"])
    assert persisted["data"] == manager._data
    assert set(manager._data["maintenance_schedules"]) == {SCHEDULE}
    assert manager._data["maintenance_events"] == {}
    assert (
        result.snapshot.schedules[SCHEDULE]
        == (persisted["data"]["maintenance_schedules"][SCHEDULE])
    )
    assert _non_maintenance(manager._data) == _non_maintenance(before)
    assert manager._persistence_uncertain is False


async def test_no_op_writes_nothing(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    manager = await _manager(
        hass, hass_storage, _with_schedule_and_event(asset_store_data)
    )
    persisted = _bytes(hass_storage[STORAGE_KEY])
    before = deepcopy(manager._data)

    with _readback(hass_storage), _save_spy() as save:
        result = await manager.async_mutate_maintenance(
            SetEnabledRequest(schedule_uuid=SCHEDULE, enabled=True)
        )

    assert result.outcome is MutationOutcome.NO_OP
    save.assert_not_called()
    assert _bytes(hass_storage[STORAGE_KEY]) == persisted
    assert manager._data == before


async def test_replay_writes_nothing_and_reuses_the_identity(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    manager = await _manager(hass, hass_storage, _base(asset_store_data))
    with _readback(hass_storage):
        await manager.async_mutate_maintenance(_create())
        await manager.async_mutate_maintenance(_record())
    persisted = _bytes(hass_storage[STORAGE_KEY])

    with _readback(hass_storage), _save_spy() as save:
        schedule = await manager.async_mutate_maintenance(_create())
        event = await manager.async_mutate_maintenance(_record())

    assert schedule.outcome is MutationOutcome.REPLAY
    assert event.outcome is MutationOutcome.REPLAY
    save.assert_not_called()
    assert _bytes(hass_storage[STORAGE_KEY]) == persisted
    assert set(manager._data["maintenance_schedules"]) == {SCHEDULE}
    assert set(manager._data["maintenance_events"]) == {EVENT}


# --- Persistence failures --------------------------------------------------------


async def test_definite_save_failure_publishes_nothing(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    manager = await _manager(hass, hass_storage, _base(asset_store_data))
    persisted = _bytes(hass_storage[STORAGE_KEY])
    before = deepcopy(manager._data)

    with (
        patch.object(
            DeviceLifecycleStore,
            "async_save",
            side_effect=AssetStorePersistenceError("write failed"),
        ),
        patch.object(storage.json_util, "load_json") as load,
        pytest.raises(AssetStorePersistenceError) as raised,
    ):
        await manager.async_mutate_maintenance(_create())

    assert raised.value.ambiguous is False
    load.assert_not_called()
    assert manager._data == before
    assert manager._persistence_uncertain is False
    assert _bytes(hass_storage[STORAGE_KEY]) == persisted


async def test_mismatching_readback_is_a_definite_failure(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    """The write is known not to match; the resolver never turns it into
    success."""
    manager = await _manager(hass, hass_storage, _base(asset_store_data))
    old_envelope = deepcopy(hass_storage[STORAGE_KEY])
    before = deepcopy(manager._data)

    with (
        patch.object(
            storage.json_util,
            "load_json",
            side_effect=lambda _p: deepcopy(old_envelope),
        ) as load,
        pytest.raises(AssetStorePersistenceError) as raised,
    ):
        await manager.async_mutate_maintenance(_create())

    assert raised.value.ambiguous is False
    load.assert_called_once()  # the readback in async_save only
    assert manager._data == before
    assert manager._persistence_uncertain is False


async def test_ambiguous_write_that_landed_resolves_as_success(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    """The atomic write landed but its readback was unreadable."""
    manager = await _manager(hass, hass_storage, _base(asset_store_data))
    reads: list[int] = []

    def _load(_path: str) -> Any:
        reads.append(1)
        if len(reads) == 1:
            raise HomeAssistantError("readback failed")
        return deepcopy(hass_storage[STORAGE_KEY])

    with patch.object(storage.json_util, "load_json", side_effect=_load):
        result = await manager.async_mutate_maintenance(_create())

    assert len(reads) == 2
    assert result.outcome in (MutationOutcome.REPLAY, MutationOutcome.NO_OP)
    assert manager._persistence_uncertain is False
    assert manager._data == hass_storage[STORAGE_KEY]["data"]
    assert set(manager._data["maintenance_schedules"]) == {SCHEDULE}
    assert result.snapshot.schedules[SCHEDULE]["schedule_uuid"] == SCHEDULE

    # The user's retry is a plain replay: no second object, no write.
    with _readback(hass_storage), _save_spy() as save:
        retry = await manager.async_mutate_maintenance(_create())
    assert retry.outcome is MutationOutcome.REPLAY
    save.assert_not_called()


async def test_ambiguous_event_write_that_landed_resolves_as_success(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    data = _with_schedule_and_event(asset_store_data)
    del data["maintenance_events"][EVENT]
    manager = await _manager(hass, hass_storage, data)
    reads: list[int] = []

    def _load(_path: str) -> Any:
        reads.append(1)
        if len(reads) == 1:
            raise HomeAssistantError("readback failed")
        return deepcopy(hass_storage[STORAGE_KEY])

    with patch.object(storage.json_util, "load_json", side_effect=_load):
        result = await manager.async_mutate_maintenance(_record())

    assert result.outcome is MutationOutcome.REPLAY
    assert list(manager._data["maintenance_events"]) == [EVENT]
    assert manager._persistence_uncertain is False


async def test_ambiguous_write_that_did_not_land_becomes_a_definite_failure(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    data = _base(asset_store_data)
    manager = await _manager(hass, hass_storage, data)

    with (
        _readback(hass_storage),
        patch.object(
            DeviceLifecycleStore,
            "async_save",
            side_effect=AssetStorePersistenceError("unknown", ambiguous=True),
        ),
        pytest.raises(AssetStorePersistenceError) as raised,
    ):
        await manager.async_mutate_maintenance(_create())

    assert raised.value.ambiguous is False
    assert isinstance(raised.value.__cause__, AssetStorePersistenceError)
    assert raised.value.__cause__.ambiguous is True
    assert manager._persistence_uncertain is False
    assert manager._data == hass_storage[STORAGE_KEY]["data"] == data
    assert manager._data["maintenance_schedules"] == {}


async def test_ambiguous_write_with_an_unreadable_store_stays_uncertain(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    manager = await _manager(hass, hass_storage, _base(asset_store_data))
    before = deepcopy(manager._data)
    original = AssetStorePersistenceError("unknown", ambiguous=True)

    with (
        patch.object(DeviceLifecycleStore, "async_save", side_effect=original),
        patch.object(
            storage.json_util,
            "load_json",
            side_effect=HomeAssistantError("unreadable"),
        ),
        pytest.raises(AssetStorePersistenceError) as raised,
    ):
        await manager.async_mutate_maintenance(_create())

    assert raised.value is original
    assert manager._persistence_uncertain is True
    assert manager._data == before


async def test_ambiguous_write_with_a_corrupt_store_stays_uncertain(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    manager = await _manager(hass, hass_storage, _base(asset_store_data))
    before = deepcopy(manager._data)
    corrupt = deepcopy(hass_storage[STORAGE_KEY])
    corrupt["data"]["maintenance_schedules"][SCHEDULE] = {"schedule_uuid": SCHEDULE}
    hass_storage[STORAGE_KEY] = corrupt
    original = AssetStorePersistenceError("unknown", ambiguous=True)

    with (
        _readback(hass_storage),
        patch.object(DeviceLifecycleStore, "async_save", side_effect=original),
        pytest.raises(AssetStorePersistenceError) as raised,
    ):
        await manager.async_mutate_maintenance(_create())

    assert raised.value is original
    assert manager._persistence_uncertain is True
    assert manager._data == before

    # A later mutation recovers first, and the corrupt file fails it closed.
    with (
        _readback(hass_storage),
        _save_spy() as save,
        pytest.raises(AssetStoreError),
    ):
        await manager.async_mutate_maintenance(_create())
    save.assert_not_called()
    assert manager._data == before
    assert manager._persistence_uncertain is True


async def test_uncertain_manager_recovers_before_evaluating_the_request(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    """An earlier ambiguous write had landed; the recovered state replays."""
    manager = await _manager(hass, hass_storage, _base(asset_store_data))
    landed = _with_schedule_and_event(asset_store_data)
    hass_storage[STORAGE_KEY] = _envelope(deepcopy(landed))
    manager._persistence_uncertain = True

    with _readback(hass_storage), _save_spy() as save:
        result = await manager.async_mutate_maintenance(_create())

    assert result.outcome is MutationOutcome.REPLAY
    save.assert_not_called()
    assert manager._persistence_uncertain is False
    assert manager._data == landed


async def test_callers_without_a_resolver_keep_the_old_semantics(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    """An ambiguous save of an existing mutation is not resolved in place."""
    manager = await _manager(hass, hass_storage, _base(asset_store_data))
    before = deepcopy(manager._data)
    original = AssetStorePersistenceError("unknown", ambiguous=True)

    with (
        patch.object(DeviceLifecycleStore, "async_save", side_effect=original),
        patch.object(storage.json_util, "load_json") as load,
        pytest.raises(AssetStorePersistenceError) as raised,
    ):
        await manager.async_update_asset_metadata(ASSET_UUID, name="Renamed")

    assert raised.value is original
    load.assert_not_called()
    assert manager._persistence_uncertain is True
    assert manager._data == before


# --- Archived Asset ----------------------------------------------------------------


BLOCKED_ON_ARCHIVED = {
    "create_schedule": _create(schedule_uuid=SCHEDULE_2),
    "edit_schedule": EditScheduleRequest(
        schedule_uuid=SCHEDULE,
        name="Renamed",
        enabled=True,
        calendar_interval=CalendarIntervalInput(6, "months"),
        runtime_interval_seconds=None,
        preparation_reminder=None,
    ),
    "set_initial_anchor": SetInitialAnchorRequest(
        schedule_uuid=SCHEDULE, initial_anchor=InitialAnchorInput(date="2024-01-01")
    ),
    "add_interval": AddIntervalRequest(
        schedule_uuid=SCHEDULE,
        dimension=IntervalDimension.RUNTIME,
        runtime_interval_seconds="3600",
    ),
    "remove_interval": RemoveIntervalRequest(
        schedule_uuid=SCHEDULE, dimension=IntervalDimension.CALENDAR
    ),
    "set_enabled": SetEnabledRequest(schedule_uuid=SCHEDULE, enabled=False),
    "delete_schedule": DeleteScheduleRequest(schedule_uuid=SCHEDULE),
    "record_event": _record(event_uuid=EVENT_2, performed_date="2024-04-01"),
}


@pytest.mark.parametrize("operation", BLOCKED_ON_ARCHIVED)
async def test_archived_asset_blocks_current_maintenance(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
    operation: str,
) -> None:
    data = _with_schedule_and_event(asset_store_data)
    _archive(data)
    manager = await _manager(hass, hass_storage, data)
    persisted = _bytes(hass_storage[STORAGE_KEY])

    with (
        _readback(hass_storage),
        _save_spy() as save,
        pytest.raises(AssetStoreError) as raised,
    ):
        await manager.async_mutate_maintenance(BLOCKED_ON_ARCHIVED[operation])

    assert raised.value.code == "asset_archived"
    assert isinstance(raised.value.__cause__, MaintenanceArchivedAssetError)
    save.assert_not_called()
    assert _bytes(hass_storage[STORAGE_KEY]) == persisted
    assert manager._data == data


async def test_archived_asset_still_replays_what_already_landed(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    """Replay is recognized before the Archive refusal."""
    data = _with_schedule_and_event(asset_store_data)
    manager = await _manager(hass, hass_storage, data)
    with _readback(hass_storage):
        created = await manager.async_mutate_maintenance(
            _create(schedule_uuid=SCHEDULE_2, name="Belt")
        )
    assert created.outcome is MutationOutcome.CHANGED
    archived = deepcopy(manager._data)
    _archive(archived)  # type: ignore[arg-type]
    manager = await _manager(hass, hass_storage, archived)

    with _readback(hass_storage), _save_spy() as save:
        replayed = await manager.async_mutate_maintenance(
            _create(schedule_uuid=SCHEDULE_2, name="Belt")
        )
    assert replayed.outcome is MutationOutcome.REPLAY
    save.assert_not_called()


@pytest.mark.parametrize(
    "request_",
    [VoidEventRequest(event_uuid=EVENT, void_reason="Wrong"), _correct()],
    ids=["void", "correct"],
)
async def test_archived_asset_history_can_still_be_corrected(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
    request_: Any,
) -> None:
    data = _with_schedule_and_event(asset_store_data)
    _archive(data)
    manager = await _manager(hass, hass_storage, data)

    with _readback(hass_storage), _save_spy() as save:
        result = await manager.async_mutate_maintenance(request_)

    assert result.outcome is MutationOutcome.CHANGED
    save.assert_called_once()
    assert manager._data["maintenance_events"][EVENT]["voided_at"] is not None
    assert manager._data["assets"] == data["assets"]
    assert hass_storage[STORAGE_KEY]["data"] == manager._data


# --- Error mapping -----------------------------------------------------------------


async def test_pure_errors_keep_their_stable_codes(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    manager = await _manager(
        hass, hass_storage, _with_schedule_and_event(asset_store_data)
    )
    with _readback(hass_storage), _save_spy() as save:
        with pytest.raises(AssetStoreError) as missing:
            await manager.async_mutate_maintenance(
                SetEnabledRequest(schedule_uuid=MISSING, enabled=False)
            )
        with pytest.raises(AssetStoreError) as conflict:
            await manager.async_mutate_maintenance(_create(name="Different"))
        with pytest.raises(AssetStoreError) as confirm:
            await manager.async_mutate_maintenance(
                RemoveIntervalRequest(
                    schedule_uuid=SCHEDULE, dimension=IntervalDimension.CALENDAR
                )
            )
    save.assert_not_called()
    assert missing.value.code == "maintenance_schedule_not_found"
    assert conflict.value.code == "maintenance_replay_conflict"
    # The last interval cannot be removed; the pure layer names that rule.
    assert confirm.value.code == "maintenance_last_interval"
    assert "Different" not in str(conflict.value)


async def test_confirmation_details_travel_with_the_error(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    data = _base(asset_store_data)
    manager = await _manager(hass, hass_storage, data)
    with _readback(hass_storage):
        await manager.async_mutate_maintenance(
            _create(initial_anchor=InitialAnchorInput(date="2024-01-01"))
        )
        with pytest.raises(AssetStoreError) as raised:
            await manager.async_mutate_maintenance(_record())

    assert raised.value.code == "maintenance_confirm_baseline_lock"
    cause = raised.value.__cause__
    assert isinstance(cause, MaintenanceConfirmationRequiredError)
    assert cause.schedule_uuids == (SCHEDULE,)

    with _readback(hass_storage):
        confirmed = await manager.async_mutate_maintenance(
            _record(confirmed_baseline_locks=frozenset({SCHEDULE}))
        )
    assert confirmed.outcome is MutationOutcome.CHANGED


# --- Runtime and locking -------------------------------------------------------------


async def test_current_runtime_comes_from_the_locked_candidate(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    """A Runtime commit queued behind the Maintenance mutation is not seen."""
    manager = await _manager(hass, hass_storage, _base(asset_store_data))
    seen: list[str | None] = []
    real_mutate = storage.mutate_maintenance

    def _capture(snapshot: Any, request: Any, context: Any) -> Any:
        seen.append(context.current_runtime)
        return real_mutate(snapshot, request, context)

    save_entered = asyncio.Event()
    release_save = asyncio.Event()
    original_save = DeviceLifecycleStore.async_save
    saves: list[Any] = []

    async def _blocking_save(store: DeviceLifecycleStore, data: Any) -> None:
        saves.append(deepcopy(data))
        if len(saves) == 1:
            save_entered.set()
            await release_save.wait()
        await original_save(store, data)

    with (
        _readback(hass_storage),
        patch.object(storage, "mutate_maintenance", side_effect=_capture),
        patch.object(
            DeviceLifecycleStore,
            "async_save",
            autospec=True,
            side_effect=_blocking_save,
        ),
    ):
        maintenance = asyncio.create_task(
            manager.async_mutate_maintenance(
                _create(
                    runtime_interval_seconds="3600",
                    initial_anchor=InitialAnchorInput(
                        date="2024-01-01", runtime_seconds=RUNTIME
                    ),
                )
            )
        )
        await save_entered.wait()
        assert manager._mutation_lock.locked()
        commit = asyncio.create_task(
            manager.async_commit_runtime_delta(
                ASSET_UUID, expected_total=Decimal(RUNTIME), delta=Decimal(5)
            )
        )
        await asyncio.sleep(0)
        assert not commit.done()
        release_save.set()
        result = await maintenance
        total = await commit

    assert seen == [RUNTIME]
    assert result.outcome is MutationOutcome.CHANGED
    # The Maintenance write carried the candidate's Runtime unchanged ...
    assert saves[0]["assets"][ASSET_UUID]["runtime"]["total_seconds"] == RUNTIME
    # ... and the queued commit then ran normally on top of it.
    assert total == Decimal(5005)
    persisted = hass_storage[STORAGE_KEY]["data"]
    assert persisted["assets"][ASSET_UUID]["runtime"]["total_seconds"] == "5005"
    assert SCHEDULE in persisted["maintenance_schedules"]


async def test_runtime_checkpoint_runs_before_and_outside_the_mutation_lock(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    """ "Just now": checkpoint first (its own locks), then Maintenance."""
    manager = await _manager(
        hass, hass_storage, _with_schedule_and_event(asset_store_data)
    )
    lock_held_during_checkpoint: list[bool] = []

    async def _checkpoint() -> None:
        lock_held_during_checkpoint.append(manager._mutation_lock.locked())
        await manager.async_commit_runtime_delta(
            ASSET_UUID, expected_total=Decimal(RUNTIME), delta=Decimal(100)
        )

    async def _prepare_unload() -> None:
        raise AssertionError("not used")

    manager.register_runtime_checkpoint(
        ASSET_UUID, _checkpoint, prepare_unload=_prepare_unload
    )
    with _readback(hass_storage):
        observed = await manager.async_checkpoint_runtime(ASSET_UUID)
        result = await manager.async_mutate_maintenance(
            _record(
                event_uuid=EVENT_2,
                performed_date="2024-05-01",
                runtime_seconds=format(observed, "f"),
            )
        )

    assert lock_held_during_checkpoint == [False]
    assert observed == Decimal(5100)
    assert result.outcome is MutationOutcome.CHANGED
    event = manager._data["maintenance_events"][EVENT_2]
    assert event["runtime_seconds"] == "5100"
    assert manager._data["assets"][ASSET_UUID]["runtime"]["total_seconds"] == "5100"


def test_the_maintenance_path_never_touches_runtime_machinery() -> None:
    """No Runtime lock, writer, or checkpoint inside the Maintenance path."""
    tree = ast.parse(Path(storage.__file__).read_text(encoding="utf-8"))
    manager = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AssetStoreManager"
    )
    maintenance_methods = {
        "_maintenance_request_asset",
        "_run_maintenance",
        "async_mutate_maintenance",
    }
    found = set()
    for method in manager.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if method.name not in maintenance_methods:
            continue
        found.add(method.name)
        names = {n.attr for n in ast.walk(method) if isinstance(n, ast.Attribute)}
        assert not names & {
            "async_checkpoint_runtime",
            "_runtime_writers",
            "checkpoint",
            "async_commit_runtime_delta",
            "async_initialize_new_runtime",
            "async_import_legacy_runtime",
        }, method.name
    assert found == maintenance_methods


# --- Isolation ------------------------------------------------------------------------


async def test_every_operation_changes_only_the_maintenance_collections(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    manager = await _manager(hass, hass_storage, _base(asset_store_data))
    before = _non_maintenance(manager._data)
    requests = [
        _create(),
        EditScheduleRequest(
            schedule_uuid=SCHEDULE,
            name="Renamed",
            enabled=True,
            calendar_interval=CalendarIntervalInput(3, "months"),
            runtime_interval_seconds=None,
            preparation_reminder=None,
        ),
        SetInitialAnchorRequest(
            schedule_uuid=SCHEDULE, initial_anchor=InitialAnchorInput(date="2024-01-01")
        ),
        AddIntervalRequest(
            schedule_uuid=SCHEDULE,
            dimension=IntervalDimension.RUNTIME,
            runtime_interval_seconds="3600",
            anchor_component="4000",
        ),
        SetEnabledRequest(schedule_uuid=SCHEDULE, enabled=False),
        SetEnabledRequest(schedule_uuid=SCHEDULE, enabled=True),
        _record(confirmed_baseline_locks=frozenset({SCHEDULE})),
        _correct(),
        VoidEventRequest(event_uuid=EVENT_2, void_reason="Wrong"),
        _create(schedule_uuid=SCHEDULE_2, name="Unused"),
        DeleteScheduleRequest(schedule_uuid=SCHEDULE_2),
    ]
    with _readback(hass_storage):
        for request in requests:
            result = await manager.async_mutate_maintenance(request)
            assert result.outcome is MutationOutcome.CHANGED, request
            assert _non_maintenance(manager._data) == before, request
            assert hass_storage[STORAGE_KEY]["data"] == manager._data
    assert manager._data["assets"][ASSET_UUID]["runtime"]["total_seconds"] == RUNTIME
    assert set(manager._data["maintenance_schedules"]) == {SCHEDULE}
    assert set(manager._data["maintenance_events"]) == {EVENT, EVENT_2}


# --- Read helpers and projection --------------------------------------------------------


async def test_read_helpers_return_detached_complete_history(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    manager = await _manager(
        hass, hass_storage, _with_schedule_and_event(asset_store_data)
    )
    with _readback(hass_storage):
        await manager.async_mutate_maintenance(
            _create(schedule_uuid=SCHEDULE_2, name="Belt", enabled=False)
        )
        await manager.async_mutate_maintenance(_correct())
    archived = deepcopy(manager._data)
    _archive(archived)  # type: ignore[arg-type]
    manager = await _manager(hass, hass_storage, archived)  # type: ignore[arg-type]

    schedules = manager.maintenance_schedules_for_asset(ASSET_UUID)
    events = manager.maintenance_events_for_asset(ASSET_UUID)

    assert [item["schedule_uuid"] for item in schedules] == [SCHEDULE_2, SCHEDULE]
    assert schedules[0]["enabled"] is False
    # The voided original and its correction, ordered by performed date.
    assert [item["event_uuid"] for item in events] == [EVENT_2, EVENT]
    assert events[1]["voided_at"] is not None
    assert events[0]["corrects_event_uuid"] == EVENT
    assert manager.maintenance_schedules_for_asset(MISSING) == []
    assert manager.maintenance_events_for_asset(MISSING) == []

    schedules[0]["name"] = "mutated"
    events[0]["title"] = "mutated"
    assert manager._data["maintenance_schedules"][SCHEDULE_2]["name"] == "Belt"
    assert manager._data["maintenance_events"][EVENT_2]["title"] == "Filter change"


async def test_projection_uses_the_pure_rules_and_canonical_state(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    data = _base(asset_store_data)
    manager = await _manager(hass, hass_storage, data)
    calendar_only = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee1"
    runtime_only = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee2"
    combined = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee3"
    disabled = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeee4"
    with _readback(hass_storage):
        for request in (
            _create(
                schedule_uuid=calendar_only,
                initial_anchor=InitialAnchorInput(date="2024-01-01"),
            ),
            _create(
                schedule_uuid=runtime_only,
                calendar_interval=None,
                runtime_interval_seconds="3600",
                initial_anchor=InitialAnchorInput(runtime_seconds="4000"),
            ),
            _create(
                schedule_uuid=combined,
                runtime_interval_seconds="100000",
                initial_anchor=InitialAnchorInput(
                    date="2024-01-01", runtime_seconds="4000"
                ),
            ),
            _create(schedule_uuid=disabled, enabled=False),
        ):
            await manager.async_mutate_maintenance(request)

    calendar = manager.maintenance_projection(calendar_only)
    assert calendar is not None and calendar.active
    assert calendar.calendar is not None and calendar.runtime is None
    assert calendar.calendar.state is DueState.OVERDUE

    runtime = manager.maintenance_projection(runtime_only)
    assert runtime is not None and runtime.active
    assert runtime.calendar is None and runtime.runtime is not None
    assert runtime.runtime.due == Decimal(7600)
    assert runtime.runtime.state is DueState.OK

    both = manager.maintenance_projection(combined)
    assert both is not None and both.calendar and both.runtime
    assert both.combined_state is DueState.OVERDUE

    off = manager.maintenance_projection(disabled)
    assert off is not None and not off.active
    assert off.inactive_reason is InactiveReason.DISABLED
    assert manager.maintenance_projection(MISSING) is None

    # Unknown Runtime: the Runtime condition cannot be decided.
    unknown = deepcopy(manager._data)
    unknown["assets"][ASSET_UUID]["runtime"]["total_seconds"] = None
    manager = await _manager(hass, hass_storage, unknown)  # type: ignore[arg-type]
    runtime = manager.maintenance_projection(runtime_only)
    assert runtime is not None and runtime.runtime is not None
    assert runtime.runtime.state is DueState.UNKNOWN

    # Archived: suppressed, persisted data untouched.
    archived = deepcopy(manager._data)
    _archive(archived)  # type: ignore[arg-type]
    manager = await _manager(hass, hass_storage, archived)  # type: ignore[arg-type]
    before = deepcopy(manager._data)
    for schedule_uuid in (calendar_only, runtime_only, combined, disabled):
        projection = manager.maintenance_projection(schedule_uuid)
        assert projection is not None and not projection.active
        assert projection.inactive_reason is InactiveReason.ASSET_ARCHIVED
    assert manager._data == before


def test_no_maintenance_ui_entities_or_archive_api() -> None:
    package = Path(storage.__file__).parent
    for name in ("config_flow.py", "sensor.py", "exposure.py", "__init__.py"):
        source = (package / name).read_text(encoding="utf-8")
        assert "async_mutate_maintenance" not in source, name
        assert "maintenance_projection" not in source, name
    source = (package / "storage.py").read_text(encoding="utf-8")
    for symbol in (
        "apply_archive_request",
        "async_archive_asset",
        "async_restore_asset",
    ):
        assert symbol not in source
    assert (storage.STORAGE_VERSION, storage.STORAGE_MINOR_VERSION) == (4, 1)


async def test_ambiguous_write_with_a_missing_store_file_stays_uncertain(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    manager = await _manager(hass, hass_storage, _base(asset_store_data))
    before = deepcopy(manager._data)
    original = AssetStorePersistenceError("unknown", ambiguous=True)

    with (
        patch.object(DeviceLifecycleStore, "async_save", side_effect=original),
        patch.object(
            DeviceLifecycleStore, "async_load_persisted_snapshot", return_value=None
        ),
        pytest.raises(AssetStorePersistenceError) as raised,
    ):
        await manager.async_mutate_maintenance(_create())

    assert raised.value is original
    assert manager._persistence_uncertain is True
    assert manager._data == before


async def test_a_refused_replay_proves_the_write_did_not_land(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    """The persisted Store refuses the request (the Asset is archived there),
    so it cannot hold the write: a definite failure on the known state."""
    data = _base(asset_store_data)
    manager = await _manager(hass, hass_storage, data)
    archived = deepcopy(data)
    _archive(archived)
    hass_storage[STORAGE_KEY] = _envelope(deepcopy(archived))

    with (
        _readback(hass_storage),
        patch.object(
            DeviceLifecycleStore,
            "async_save",
            side_effect=AssetStorePersistenceError("unknown", ambiguous=True),
        ),
        pytest.raises(AssetStorePersistenceError) as raised,
    ):
        await manager.async_mutate_maintenance(_create())

    assert raised.value.ambiguous is False
    assert manager._persistence_uncertain is False
    assert manager._data == archived


async def test_a_request_without_a_usable_identity_is_refused_by_the_pure_rules(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    manager = await _manager(hass, hass_storage, _base(asset_store_data))
    with (
        _readback(hass_storage),
        _save_spy() as save,
        pytest.raises(AssetStoreError) as raised,
    ):
        await manager.async_mutate_maintenance(
            SetEnabledRequest(schedule_uuid=123, enabled=True)  # type: ignore[arg-type]
        )
    assert raised.value.code == "maintenance_invalid_request"
    save.assert_not_called()
