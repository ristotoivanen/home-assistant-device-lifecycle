"""Entities and Repairs follow Store commits without a reload (WP14).

After a verified Store commit the manager publishes the canonical snapshot
and tells its publish listeners which Assets changed. Asset entities
re-read their canonical snapshots and write their state; stale-reference
Repairs are re-derived. The commit is authoritative: a failing listener is
logged and never undoes it, and nothing is announced for a true no-op.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.device_lifecycle import sensor, storage
from custom_components.device_lifecycle.archive import ArchiveOutcome
from custom_components.device_lifecycle.config_flow import DeviceLifecycleConfigFlow
from custom_components.device_lifecycle.const import (
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DOMAIN,
)
from custom_components.device_lifecycle.exposure import (
    asset_device_entry,
    asset_id_unique_id,
    deployment_unique_id,
    installed_date_unique_id,
    lifecycle_status_unique_id,
    relationships_unique_id,
    replacement_unique_id,
)
from custom_components.device_lifecycle.maintenance_mutations import (
    CalendarIntervalInput,
    CreateScheduleRequest,
    DeleteScheduleRequest,
    MutationOutcome,
)
from custom_components.device_lifecycle.migration import (
    lifecycle_unique_id,
    runtime_unique_id,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.stale_references import (
    is_owned_stale_reference_issue,
    stale_reference_issue_id,
)
from custom_components.device_lifecycle.storage import (
    STORAGE_KEY,
    AssetStoreManager,
    AssetStorePersistenceError,
    DeviceLifecycleStore,
    _changed_asset_uuids,
)

from .conftest import ASSET_UUID, capture_reloads
from .test_exposure_options_reload import _verified_store_readback
from .test_runtime import _Clock, _manager, _sensor
from .test_runtime_checkpoint import _add
from .test_stale_device_registry_references import (
    SECOND_ASSET_UUID,
    _device,
    _load,
    _refs,
    _runtime,
    _with_second_asset,
)

SCHEDULE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
GONE_DEVICE = "device-that-left-home-assistant"
ASSET_UNIQUE_IDS = {
    "lifecycle": lifecycle_unique_id,
    "deployment": deployment_unique_id,
    "installed_date": installed_date_unique_id,
    "asset_id": asset_id_unique_id,
    "lifecycle_status": lifecycle_status_unique_id,
    "replacement": replacement_unique_id,
    "relationships": relationships_unique_id,
}
SNAPSHOT_CLASSES = (
    sensor.DeviceLifecycleSensor,
    sensor.DeviceDeploymentSensor,
    sensor.DeviceInstallationDateSensor,
    sensor.DeviceAssetIdSensor,
    sensor.DeviceLifecycleStatusSensor,
    sensor.DeviceReplacementSensor,
    sensor.DeviceRelationshipsSensor,
    sensor.DeviceRuntimeHoursSensor,
)


def _data(asset_store_data: AssetStoreData) -> dict[str, Any]:
    data: dict[str, Any] = deepcopy(asset_store_data)
    data["assets"][ASSET_UUID]["deployment_state"] = DEPLOYMENT_STATE_NOT_DEPLOYED
    data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "1000"
    return data


def _create(**values: Any) -> CreateScheduleRequest:
    request: dict[str, Any] = {
        "schedule_uuid": SCHEDULE,
        "asset_uuid": ASSET_UUID,
        "name": "Filter change",
        "calendar_interval": CalendarIntervalInput(6, "months"),
    }
    request.update(values)
    return CreateScheduleRequest(**request)


def _two_assets(asset_store_data: AssetStoreData) -> dict[str, Any]:
    data = _with_second_asset(_data(asset_store_data))  # type: ignore[arg-type]
    data["assets"][SECOND_ASSET_UUID]["ha_device_refs"] = []
    return data  # type: ignore[return-value]


def _recording(manager: AssetStoreManager) -> list[frozenset[str]]:
    calls: list[frozenset[str]] = []
    manager.async_add_publish_listener(calls.append)
    return calls


# --- The publish listener -------------------------------------------------------


async def test_listeners_run_in_order_after_publication_and_are_isolated(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    caplog: pytest.LogCaptureFixture,
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    saved: list[Any] = []
    manager._store.async_save = AsyncMock(side_effect=saved.append)
    seen: list[tuple[str, frozenset[str], bool]] = []

    def _listener(name: str, *, fail: bool = False) -> Any:
        def _call(changed: frozenset[str]) -> None:
            seen.append((name, changed, manager._data == saved[-1]))
            if fail:
                raise RuntimeError("refresh failed")

        return _call

    manager.async_add_publish_listener(_listener("a"))
    manager.async_add_publish_listener(_listener("b", fail=True))
    manager.async_add_publish_listener(_listener("c"))

    updated = await manager.async_update_asset_metadata(ASSET_UUID, name="Renamed")

    assert updated["name"] == "Renamed"
    assert [(name, changed, published) for name, changed, published in seen] == [
        ("a", frozenset({ASSET_UUID}), True),
        ("b", frozenset({ASSET_UUID}), True),
        ("c", frozenset({ASSET_UUID}), True),
    ]
    assert manager._data["assets"][ASSET_UUID]["name"] == "Renamed"
    assert manager._persistence_uncertain is False
    assert "refresh after a Store commit failed" in caplog.text


async def test_a_no_op_announces_nothing_and_unsubscribe_is_final(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data = _data(asset_store_data)
    manager = _manager(hass, data)
    calls: list[frozenset[str]] = []
    remove = manager.async_add_publish_listener(calls.append)

    await manager.async_restore_asset(ASSET_UUID)  # already active: NO_OP
    await manager.async_unlink_asset_device(ASSET_UUID)
    calls.clear()
    manager._store.async_save.reset_mock()
    await manager.async_unlink_asset_device(ASSET_UUID)  # already unlinked
    assert calls == []
    manager._store.async_save.assert_not_awaited()

    await manager.async_update_asset_metadata(ASSET_UUID, notes="Changed")
    assert calls == [frozenset({ASSET_UUID})]
    remove()
    remove()
    await manager.async_update_asset_metadata(ASSET_UUID, notes="Again")
    assert calls == [frozenset({ASSET_UUID})]


async def test_maintenance_and_runtime_commits_name_the_owning_asset(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data = _two_assets(asset_store_data)
    manager = _manager(hass, data)
    calls = _recording(manager)

    created = await manager.async_mutate_maintenance(_create())
    assert created.outcome is MutationOutcome.CHANGED
    assert manager._data["assets"] == data["assets"]  # Asset records unchanged
    assert calls == [frozenset({ASSET_UUID})]

    replay = await manager.async_mutate_maintenance(_create())
    assert replay.outcome is MutationOutcome.REPLAY
    assert calls == [frozenset({ASSET_UUID})]

    await manager.async_mutate_maintenance(
        DeleteScheduleRequest(schedule_uuid=SCHEDULE)
    )
    assert calls[-1] == frozenset({ASSET_UUID})  # owner read from the old side

    await manager.async_commit_runtime_delta(
        ASSET_UUID, expected_total=Decimal(1000), delta=Decimal(5)
    )
    assert calls[-1] == frozenset({ASSET_UUID})
    assert SECOND_ASSET_UUID not in set().union(*calls)


def test_changed_assets_come_from_both_snapshots(
    asset_store_data: AssetStoreData,
) -> None:
    old = _with_second_asset(_data(asset_store_data))  # type: ignore[arg-type]
    assert _changed_asset_uuids(old, deepcopy(old)) == frozenset()

    new = deepcopy(old)
    new["replacement_records"]["r"] = {
        "predecessor_asset_uuid": ASSET_UUID,
        "successor_asset_uuid": SECOND_ASSET_UUID,
    }
    assert _changed_asset_uuids(old, new) == {ASSET_UUID, SECOND_ASSET_UUID}

    new = deepcopy(old)
    new["purchases"][next(iter(new["purchases"]))]["seller"] = "Changed"
    assert _changed_asset_uuids(old, new) == {ASSET_UUID, SECOND_ASSET_UUID}

    with_event = deepcopy(old)
    with_event["maintenance_events"]["e"] = {"asset_uuid": SECOND_ASSET_UUID}
    assert _changed_asset_uuids(with_event, old) == {SECOND_ASSET_UUID}


# --- Persistence outcomes ----------------------------------------------------------


def _envelope(data: Any) -> dict[str, Any]:
    return {"version": 4, "minor_version": 1, "key": STORAGE_KEY, "data": data}


@contextmanager
def _readback(hass_storage: dict[str, Any]) -> Iterator[None]:
    with patch.object(
        storage.json_util,
        "load_json",
        side_effect=lambda _path: deepcopy(hass_storage[STORAGE_KEY]),
    ):
        yield


async def _persisted(
    hass: HomeAssistant, hass_storage: dict[str, Any], data: dict[str, Any]
) -> AssetStoreManager:
    hass_storage[STORAGE_KEY] = _envelope(deepcopy(data))
    manager = AssetStoreManager(hass)
    with _readback(hass_storage):
        await manager.async_setup()
    return manager


async def test_an_ambiguous_write_that_landed_is_announced_once_published(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    manager = await _persisted(hass, hass_storage, _data(asset_store_data))
    seen: list[tuple[frozenset[str], bool]] = []

    def _listener(changed: frozenset[str]) -> None:
        seen.append((changed, manager._data == hass_storage[STORAGE_KEY]["data"]))
        raise RuntimeError("refresh failed")

    manager.async_add_publish_listener(_listener)
    reads: list[int] = []

    def _load(_path: str) -> Any:
        reads.append(1)
        if len(reads) == 1:
            raise HomeAssistantError("readback failed")
        return deepcopy(hass_storage[STORAGE_KEY])

    with patch.object(storage.json_util, "load_json", side_effect=_load):
        result = await manager.async_mutate_maintenance(_create())

    assert result.outcome in (MutationOutcome.REPLAY, MutationOutcome.NO_OP)
    assert seen == [(frozenset({ASSET_UUID}), True)]
    assert manager._persistence_uncertain is False


async def test_an_ambiguous_write_that_did_not_land_announces_nothing(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    manager = await _persisted(hass, hass_storage, _data(asset_store_data))
    calls = _recording(manager)
    with (
        _readback(hass_storage),
        patch.object(
            DeviceLifecycleStore,
            "async_save",
            side_effect=AssetStorePersistenceError("unknown", ambiguous=True),
        ),
        pytest.raises(AssetStorePersistenceError),
    ):
        await manager.async_mutate_maintenance(_create())
    assert calls == []


async def test_recovery_announces_the_actual_difference_only(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    data = _data(asset_store_data)
    manager = await _persisted(hass, hass_storage, data)
    calls = _recording(manager)

    manager._persistence_uncertain = True
    with _readback(hass_storage):
        await manager.async_restore_asset(ASSET_UUID)  # recovery finds no change
    assert calls == []

    landed = deepcopy(data)
    landed["assets"][ASSET_UUID]["notes"] = "Written before the ambiguity"
    hass_storage[STORAGE_KEY] = _envelope(deepcopy(landed))
    manager._persistence_uncertain = True
    with _readback(hass_storage):
        await manager.async_restore_asset(ASSET_UUID)
    assert calls == [frozenset({ASSET_UUID})]
    assert manager._data == landed


# --- Runtime entity -----------------------------------------------------------------


async def test_runtime_entity_refresh_leaves_runtime_accounting_alone(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    clock = _Clock(0)
    manager = _manager(hass, _data(asset_store_data))
    runtime = _sensor(hass, manager, clock)
    runtime._bind_snapshot(manager, ASSET_UUID)
    await _add(hass, runtime)
    runtime._active_since = 0.0
    clock.value = 10
    manager._store.async_save = AsyncMock(side_effect=OSError("disk"))
    await runtime._handle_periodic_checkpoint(None)  # 10 s pending
    pending = list(runtime._pending)
    manager._store.async_save = AsyncMock()

    with patch.object(runtime, "async_write_ha_state") as write:
        await manager.async_update_asset_metadata(ASSET_UUID, notes="Changed")

    write.assert_called_once()
    assert runtime._pending == pending
    assert runtime._committed_seconds == Decimal(1000)
    assert runtime._active_since == 10
    await runtime.async_will_remove_from_hass()
    assert manager._publish_listeners == []


# --- A loaded entry: Archive and Restore ------------------------------------------------


def _registry_snapshot(hass: HomeAssistant, entry: Any) -> dict[str, tuple]:
    registry = er.async_get(hass)
    return {
        item.unique_id: (
            item.id,
            item.entity_id,
            item.config_entry_id,
            item.config_subentry_id,
            item.disabled_by,
            item.device_id,
        )
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }


def _device_snapshot(hass: HomeAssistant, entry: Any) -> tuple:
    device = asset_device_entry(
        dr.async_get(hass), config_entry_id=entry.entry_id, asset_uuid=ASSET_UUID
    )
    assert device is not None
    return (
        device.id,
        device.identifiers,
        device.config_entries,
        device.config_entries_subentries,
        device.name_by_user,
        device.name,
        device.disabled_by,
    )


def _asset_id_state(hass: HomeAssistant) -> Any:
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, asset_id_unique_id(ASSET_UUID)
    )
    assert entity_id is not None
    return hass.states.get(entity_id)


def _stale_issues(hass: HomeAssistant) -> set[str]:
    return {
        issue_id
        for domain, issue_id in ir.async_get(hass).issues
        if is_owned_stale_reference_issue(domain, issue_id)
    }


@contextmanager
def _refreshes() -> Iterator[list[str]]:
    """Record every Asset entity snapshot refresh by class."""
    refreshed: list[str] = []
    patches = []
    for cls in SNAPSHOT_CLASSES:
        original = cls._refresh_snapshot

        def _wrapped(
            self: Any, manager: Any, asset: Any, _original: Any = original
        ) -> None:
            refreshed.append(type(self).__name__)
            _original(self, manager, asset)

        patches.append(patch.object(cls, "_refresh_snapshot", _wrapped))
    for item in patches:
        item.start()
    try:
        yield refreshed
    finally:
        for item in patches:
            item.stop()


@pytest.mark.real_reload
async def test_archive_and_restore_refresh_in_place_without_a_reload(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    data = _data(asset_store_data)
    device = _device(hass, device_registry, "publish", data["assets"][ASSET_UUID])
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(device.id, GONE_DEVICE)
    entry = await _load(hass, hass_storage, data)  # type: ignore[arg-type]
    manager: AssetStoreManager = entry.runtime_data
    stale_id = stale_reference_issue_id(ASSET_UUID, "related", GONE_DEVICE)
    assert _stale_issues(hass) == {stale_id}
    assert _asset_id_state(hass).attributes["archived"] is False

    # A user-disabled Asset entity stays exactly as the person left it.
    registry = er.async_get(hass)
    disabled_id = registry.async_get_entity_id(
        "sensor", DOMAIN, installed_date_unique_id(ASSET_UUID)
    )
    assert disabled_id is not None
    registry.async_update_entity(disabled_id, disabled_by=er.RegistryEntryDisabler.USER)
    entities = _registry_snapshot(hass, entry)
    asset_device = _device_snapshot(hass, entry)
    asset_id_entity = _asset_id_state(hass).entity_id
    flow = DeviceLifecycleConfigFlow.async_get_options_flow(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    flow.flow_id = "publish-refresh-options-flow"
    flow.context = {}
    assert ASSET_UUID in {option["value"] for option in flow._asset_choices()}

    with (
        _verified_store_readback(hass_storage),
        capture_reloads(hass) as reloads,
        _refreshes() as refreshed,
    ):
        assert await manager.async_archive_asset(entry, ASSET_UUID) is (
            ArchiveOutcome.CHANGED
        )
        await hass.async_block_till_done()
        archived_state = _asset_id_state(hass)
        archived_issues = _stale_issues(hass)
        archived_choices = {option["value"] for option in flow._asset_choices()}
        archived_refreshes = sorted(refreshed)
        refreshed.clear()

        assert await manager.async_restore_asset(ASSET_UUID) is ArchiveOutcome.CHANGED
        await hass.async_block_till_done()
        restored_refreshes = sorted(refreshed)

    reloads.assert_not_called()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is manager
    # Every added Asset entity re-read its snapshot. Not added here: the one
    # the person disabled, the Replacement sensor (disabled by default while
    # the Asset has no Replacement), and Runtime (not tracked).
    expected = sorted(
        cls.__name__
        for cls in SNAPSHOT_CLASSES
        if cls
        not in (
            sensor.DeviceInstallationDateSensor,
            sensor.DeviceReplacementSensor,
            sensor.DeviceRuntimeHoursSensor,
        )
    )
    assert archived_refreshes == expected
    assert restored_refreshes == expected

    assert archived_state.attributes["archived"] is True
    assert archived_state.state == "DL0007"
    assert archived_state.entity_id == asset_id_entity
    assert archived_issues == set()
    assert ASSET_UUID not in archived_choices

    restored = _asset_id_state(hass)
    assert restored.attributes["archived"] is False
    assert restored.state == "DL0007"
    assert restored.entity_id == asset_id_entity
    assert _stale_issues(hass) == {stale_id}
    assert ASSET_UUID in {option["value"] for option in flow._asset_choices()}

    assert _registry_snapshot(hass, entry) == entities
    assert entities[installed_date_unique_id(ASSET_UUID)][4] is (
        er.RegistryEntryDisabler.USER
    )
    assert _device_snapshot(hass, entry) == asset_device
    for unique_id in ASSET_UNIQUE_IDS.values():
        assert unique_id(ASSET_UUID) in entities


@pytest.mark.real_reload
async def test_a_failing_repairs_refresh_never_fails_the_archive(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    data = _data(asset_store_data)
    device = _device(hass, device_registry, "failing", data["assets"][ASSET_UUID])
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(device.id)
    entry = await _load(hass, hass_storage, data)  # type: ignore[arg-type]
    manager: AssetStoreManager = entry.runtime_data

    with (
        _verified_store_readback(hass_storage),
        patch(
            "custom_components.device_lifecycle.async_sync_stale_reference_issues",
            side_effect=RuntimeError("repairs failed"),
        ),
    ):
        assert await manager.async_archive_asset(entry, ASSET_UUID) is (
            ArchiveOutcome.CHANGED
        )
    assert hass_storage[STORAGE_KEY]["data"]["assets"][ASSET_UUID]["archived_at"]
    assert manager._persistence_uncertain is False
    assert _asset_id_state(hass).attributes["archived"] is True


@pytest.mark.real_reload
async def test_a_runtime_entity_follows_commits_without_a_reload(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    data = _data(asset_store_data)
    device = _device(hass, device_registry, "runtime", data["assets"][ASSET_UUID])
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(device.id)
    entry = await _load(
        hass,
        hass_storage,
        data,  # type: ignore[arg-type]
        _runtime(runtime_subentry_data, device.id),
    )
    manager: AssetStoreManager = entry.runtime_data
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, runtime_unique_id(ASSET_UUID)
    )
    assert entity_id is not None

    with (
        _verified_store_readback(hass_storage),
        capture_reloads(hass) as reloads,
        _refreshes() as refreshed,
    ):
        await manager.async_update_asset_metadata(ASSET_UUID, notes="Changed")
        await hass.async_block_till_done()
    reloads.assert_not_called()
    assert "DeviceRuntimeHoursSensor" in refreshed
    assert hass.states.get(entity_id) is not None
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1000)


async def test_each_asset_entity_re_reads_what_it_renders(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _two_assets(asset_store_data))
    asset = manager.asset(ASSET_UUID)
    assert asset is not None
    device_entry = SimpleNamespace(id="asset-device")
    purchase = manager.purchase(asset["purchase_uuid"])
    entities = {
        "lifecycle": sensor.DeviceLifecycleSensor(
            asset=asset,
            purchase=purchase,
            device_entry=device_entry,
            unique_id="lifecycle",
            purchase_title="Original title",
            manager=manager,
        ),
        "deployment": sensor.DeviceDeploymentSensor(
            asset=asset, device_entry=device_entry, manager=manager
        ),
        "installed": sensor.DeviceInstallationDateSensor(
            asset=asset, device_entry=device_entry, manager=manager
        ),
        "asset_id": sensor.DeviceAssetIdSensor(
            asset=asset, device_entry=device_entry, manager=manager
        ),
        "status": sensor.DeviceLifecycleStatusSensor(
            asset=asset, current_event=None, device_entry=device_entry, manager=manager
        ),
        "replacement": sensor.DeviceReplacementSensor(
            asset=asset,
            predecessor=None,
            successor=None,
            device_entry=device_entry,
            manager=manager,
        ),
        "relationships": sensor.DeviceRelationshipsSensor(
            asset=asset,
            device_entry=device_entry,
            device_registry=dr.async_get(hass),
            manager=manager,
        ),
    }
    writes: list[str] = []
    for name, entity in entities.items():
        entity.async_write_ha_state = (  # type: ignore[method-assign]
            lambda name=name: writes.append(name)
        )
        manager.async_add_publish_listener(entity._handle_store_publish)

    await manager.async_set_asset_deployment(ASSET_UUID, installed_date="2026-03-01")
    await manager.async_set_asset_lifecycle(
        ASSET_UUID, "active", effective_date="2026-03-02", notes=None
    )
    await manager.async_create_asset_replacement(
        ASSET_UUID,
        SECOND_ASSET_UUID,
        reason="upgrade",
        effective_date=None,
        notes=None,
    )
    await manager.async_link_asset_device(ASSET_UUID, "new-device", replace=True)
    await manager.async_set_asset_purchase(ASSET_UUID, None)

    assert set(writes) == set(entities)
    assert entities["deployment"].extra_state_attributes["installed_date"] == (
        "2026-03-01"
    )
    assert str(entities["installed"].native_value) == "2026-03-01"
    assert entities["status"].native_value == "active"
    assert entities["status"].extra_state_attributes == {"effective_date": "2026-03-02"}
    assert entities["replacement"].native_value == "replaced_by"
    assert entities["relationships"].referenced_device_ids == {"new-device"}
    assert entities["lifecycle"]._purchase is None
    assert entities["lifecycle"]._purchase_title is None
    assert entities["asset_id"].extra_state_attributes == {"archived": False}

    # A Purchase that is newly assigned is named from the canonical snapshot.
    await manager.async_set_asset_purchase(ASSET_UUID, purchase["purchase_uuid"])  # type: ignore[index]
    assert entities["lifecycle"]._purchase_title == purchase["name"]  # type: ignore[index]

    # Another Asset's change is not this entity's refresh.
    writes.clear()
    await manager.async_update_asset_metadata(SECOND_ASSET_UUID, notes="Other")
    assert writes == []


async def test_snapshot_entity_edges(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    asset = manager.asset(ASSET_UUID)
    assert asset is not None
    device_entry = SimpleNamespace(id="asset-device")

    # Without a manager the entity is a static projection that subscribes to
    # nothing.
    static = sensor.DeviceAssetIdSensor(asset=asset, device_entry=device_entry)
    static.hass = hass
    await static.async_added_to_hass()
    assert static._unsub_publish is None
    assert manager._publish_listeners == []

    # An Asset that vanished from the snapshot is left as it was rendered.
    bound = sensor.DeviceAssetIdSensor(
        asset=asset, device_entry=device_entry, manager=manager
    )
    bound.async_write_ha_state = lambda: pytest.fail("no write")  # type: ignore[method-assign]
    manager._data["assets"].pop(ASSET_UUID)
    bound._handle_store_publish(frozenset({ASSET_UUID}))
    assert bound.native_value == asset["asset_id"]

    class _Bare(sensor._AssetSnapshotEntity):
        pass

    with pytest.raises(NotImplementedError):
        _Bare()._refresh_snapshot(manager, asset)


async def test_recovery_of_a_missing_file_with_an_empty_store_announces_nothing(
    hass: HomeAssistant,
) -> None:
    manager = AssetStoreManager(hass)
    manager._store.async_save = AsyncMock()  # type: ignore[method-assign]
    calls = _recording(manager)
    manager._persistence_uncertain = True
    with patch.object(
        manager._store, "async_load_persisted_snapshot", AsyncMock(return_value=None)
    ):
        await manager.async_create_manual_asset(name="First")
    assert manager._persistence_uncertain is False
    assert len(calls) == 1
