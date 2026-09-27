"""Archive and Runtime binding are one protocol (WP13).

Archive and the Runtime binding reservation both run under the Store
manager's ``_mutation_lock``, so they are totally ordered, and the
reservation lives in ``hass.data`` until Home Assistant has added the
Runtime subentry. These deterministic interleavings prove that no path ends
with an archived Asset and a Runtime subentry, an observing writer, or
Runtime that is pending or unresolved.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER, ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr

from custom_components.device_lifecycle import _async_update_listener
from custom_components.device_lifecycle.archive import ArchiveOutcome
from custom_components.device_lifecycle.config_flow import RuntimeSubentryFlow
from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_DEVICE_ID,
    CONF_RUNTIME_MODE,
    CONF_SOURCE_ENTITY_ID,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    RUNTIME_MODE_ON,
    SUBENTRY_TYPE_RUNTIME,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    RUNTIME_BINDING_RESERVATIONS,
    STORAGE_KEY,
    AssetStoreError,
    AssetStoreManager,
    RuntimeArchiveEligibility,
)

from .conftest import ASSET_UUID, capture_reloads, loaded_entry
from .test_archive_manager import _data, _entry, _runtime_subentry
from .test_exposure_options_reload import _verified_store_readback
from .test_runtime import _Clock, _manager, _sensor
from .test_runtime_checkpoint import _add
from .test_stale_device_registry_references import _device, _load, _refs, _reload

SWITCH = "switch.workshop_tool"


# --- Store-level interleavings ------------------------------------------------


def _blocking_save(manager: AssetStoreManager) -> tuple[asyncio.Event, asyncio.Event]:
    entered, release = asyncio.Event(), asyncio.Event()

    async def _save(_data: Any) -> None:
        entered.set()
        await release.wait()

    manager._store.async_save = AsyncMock(side_effect=_save)  # type: ignore[method-assign]
    return entered, release


async def test_archive_in_flight_makes_a_waiting_reservation_fail(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    entry = _entry(manager)
    entered, release = _blocking_save(manager)

    archive = asyncio.create_task(manager.async_archive_asset(entry, ASSET_UUID))
    await entered.wait()
    reserve = asyncio.create_task(
        manager.async_reserve_runtime_binding(entry, ASSET_UUID)
    )
    await asyncio.sleep(0)
    assert not reserve.done()  # waiting for the same lock

    release.set()
    assert await archive is ArchiveOutcome.CHANGED
    with pytest.raises(AssetStoreError) as raised:
        await reserve
    assert raised.value.code == "runtime_asset_archived"
    assert hass.data.get(RUNTIME_BINDING_RESERVATIONS, {}) == {}


async def test_a_held_reservation_makes_archive_fail(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    entry = _entry(manager)
    await manager.async_reserve_runtime_binding(entry, ASSET_UUID)

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_archive_asset(entry, ASSET_UUID)
    assert raised.value.code == "archive_runtime_binding_in_progress"
    manager._store.async_save.assert_not_awaited()
    assert manager._data["assets"][ASSET_UUID]["archived_at"] is None


async def test_a_delta_queued_behind_archive_keeps_it_undurable(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """Archive holds the lock while a delta commit waits: still pending."""
    clock = _Clock(0)
    manager = _manager(hass, _data(asset_store_data))
    sensor = _sensor(hass, manager, clock)
    await _add(hass, sensor)
    sensor._active_since = 0.0
    clock.value = 10
    manager._store.async_save = AsyncMock(side_effect=OSError("disk"))
    with pytest.raises(AssetStoreError):
        await sensor.async_retire_runtime()
    entry = _entry(manager)

    # Another mutation holds the lock; Archive queues first, the commit second.
    entered, release = _blocking_save(manager)
    holder = asyncio.create_task(
        manager.async_update_asset_metadata(ASSET_UUID, notes="held")
    )
    await entered.wait()
    archive = asyncio.create_task(manager.async_archive_asset(entry, ASSET_UUID))
    await asyncio.sleep(0)
    finalize = asyncio.create_task(manager.async_finalize_runtime(ASSET_UUID))
    for _ in range(5):
        await asyncio.sleep(0)
    assert sensor.runtime_durability().pending is True

    manager._store.async_save = AsyncMock()  # type: ignore[method-assign]
    release.set()
    await holder
    with pytest.raises(AssetStoreError) as raised:
        await archive
    assert raised.value.code == "archive_runtime_undurable"
    assert await finalize == Decimal(1010)
    assert manager._data["assets"][ASSET_UUID]["archived_at"] is None

    # Now durable: the same Archive succeeds.
    assert await manager.async_archive_asset(entry, ASSET_UUID) is (
        ArchiveOutcome.CHANGED
    )
    assert sensor.runtime_durability().pending is False


async def test_archive_never_takes_a_runtime_lock_and_finalize_never_runs_locked(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    clock = _Clock(0)
    manager = _manager(hass, _data(asset_store_data))
    sensor = _sensor(hass, manager, clock)
    await _add(hass, sensor)
    acquisitions: list[bool] = []
    real_lock = sensor._runtime_lock

    class _Instrumented:
        async def __aenter__(self) -> None:
            acquisitions.append(manager._mutation_lock.locked())
            await real_lock.acquire()

        async def __aexit__(self, *_exc: object) -> None:
            real_lock.release()

    sensor._runtime_lock = _Instrumented()  # type: ignore[assignment]
    entry = _entry(manager)

    with pytest.raises(AssetStoreError):
        await manager.async_archive_asset(entry, ASSET_UUID)  # observing writer
    assert acquisitions == []

    await manager.async_retire_orphaned_runtime_writers(entry)
    await manager.async_finalize_runtime(ASSET_UUID)
    assert acquisitions == [False, False]
    assert await manager.async_archive_asset(entry, ASSET_UUID) is (
        ArchiveOutcome.CHANGED
    )
    assert acquisitions == [False, False]


async def test_failed_retirement_timeline_counts_no_time_after_quiesce(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """1000 s canonical; on from t=50; removed and retired at t=60 with the
    commit failing; finalized at t=100 with the source still on: 1010."""
    clock = _Clock(0)
    manager = _manager(hass, _data(asset_store_data))
    sensor = _sensor(hass, manager, clock)
    await _add(hass, sensor)
    clock.value = 50
    sensor._active_since = 50.0
    hass.states.async_set(sensor._source_entity_id, "on")

    clock.value = 60
    manager._store.async_save = AsyncMock(side_effect=OSError("disk"))
    without_subentry = _entry(manager)
    await manager.async_retire_orphaned_runtime_writers(without_subentry)

    durability = sensor.runtime_durability()
    assert durability.observing is False
    assert durability.pending is True
    assert [item.delta for item in sensor._pending] == [Decimal(10)]
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1000)
    with pytest.raises(AssetStoreError) as refused:
        await manager.async_archive_asset(without_subentry, ASSET_UUID)
    assert refused.value.code == "archive_runtime_undurable"
    with pytest.raises(AssetStoreError):
        await manager.async_prepare_runtime_unload()

    clock.value = 100
    manager._store.async_save = AsyncMock()
    with capture_reloads(hass) as reloads:
        assert await manager.async_finalize_runtime(ASSET_UUID) == Decimal(1010)
        assert sensor._pending == []
        assert ASSET_UUID in manager._runtime_writers
        assert manager._runtime_archive_eligibility(manager._data, ASSET_UUID) is (
            RuntimeArchiveEligibility.QUIESCED_DURABLE
        )
        assert await manager.async_archive_asset(without_subentry, ASSET_UUID) is (
            ArchiveOutcome.CHANGED
        )
    reloads.assert_not_called()
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1010)


async def test_update_listener_retires_then_always_schedules_the_reload(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    clock = _Clock(0)
    manager = _manager(hass, _data(asset_store_data))
    sensor = _sensor(hass, manager, clock)
    await _add(hass, sensor)
    sensor._active_since = 0.0
    clock.value = 5
    manager._store.async_save = AsyncMock(side_effect=OSError("disk"))
    entry = _entry(manager)

    with patch.object(hass.config_entries, "async_schedule_reload") as schedule:
        await _async_update_listener(hass, entry)  # type: ignore[arg-type]

    schedule.assert_called_once_with(entry.entry_id)
    assert sensor.runtime_durability().observing is False
    assert sensor.runtime_durability().pending is True


# --- Runtime subentry flow -------------------------------------------------------


async def _installation(
    hass: HomeAssistant,
    hass_storage: dict,
    registry: dr.DeviceRegistry,
    asset_store_data: AssetStoreData,
    *,
    archived: bool = False,
    runtime: dict[str, Any] | None = None,
) -> tuple[Any, dr.DeviceEntry]:
    data: dict[str, Any] = deepcopy(asset_store_data)
    device = _device(hass, registry, "runtime-race", data["assets"][ASSET_UUID])
    asset = data["assets"][ASSET_UUID]
    asset["ha_device_refs"] = _refs(device.id)
    asset["deployment_state"] = DEPLOYMENT_STATE_NOT_DEPLOYED
    asset["runtime"]["total_seconds"] = "1000"
    if archived:
        asset["archived_at"] = "2026-09-26T12:34:56.123456+00:00"
    hass.states.async_set(SWITCH, "off")
    subentries = []
    if runtime is not None:
        subentries.append(
            {
                "data": {**runtime, CONF_DEVICE_ID: device.id},
                "subentry_type": SUBENTRY_TYPE_RUNTIME,
                "title": "Workshop device",
                "unique_id": None,
            }
        )
    entry = await _load(hass, hass_storage, data, *subentries)  # type: ignore[arg-type]
    return entry, device


async def _create_runtime(hass: HomeAssistant, entry: Any, device_id: str) -> Any:
    initial = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_RUNTIME), context={"source": SOURCE_USER}
    )
    source = await hass.config_entries.subentries.async_configure(
        initial["flow_id"],
        {CONF_DEVICE_ID: device_id, CONF_RUNTIME_MODE: RUNTIME_MODE_ON},
    )
    if (
        source["type"] is not FlowResultType.FORM
        or source["step_id"] != "runtime_source"
    ):
        return source
    return await hass.config_entries.subentries.async_configure(
        source["flow_id"], {CONF_SOURCE_ENTITY_ID: SWITCH}
    )


@pytest.mark.real_reload
async def test_reservation_is_held_until_home_assistant_adds_the_subentry(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Home Assistant adds the subentry, then removes the flow; the
    reservation spans the add, then Archive sees the subentry itself."""
    entry, device = await _installation(
        hass, hass_storage, device_registry, asset_store_data
    )
    order: list[tuple[str, bool]] = []
    key = (entry.entry_id, ASSET_UUID)
    add_subentry = ConfigEntries.async_add_subentry
    remove_flow = RuntimeSubentryFlow.async_remove

    def _add(self: ConfigEntries, *args: Any, **kwargs: Any) -> Any:
        order.append(("add", bool(hass.data[RUNTIME_BINDING_RESERVATIONS].get(key))))
        return add_subentry(self, *args, **kwargs)

    def _remove(self: RuntimeSubentryFlow) -> None:
        added = any(
            item.subentry_type == SUBENTRY_TYPE_RUNTIME
            for item in entry.subentries.values()
        )
        order.append(("remove", added))
        remove_flow(self)

    with (
        _verified_store_readback(hass_storage),
        patch.object(ConfigEntries, "async_add_subentry", _add),
        patch.object(RuntimeSubentryFlow, "async_remove", _remove),
    ):
        result = await _create_runtime(hass, entry, device.id)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert order == [("add", True), ("remove", True)]
    assert hass.data[RUNTIME_BINDING_RESERVATIONS] == {}
    manager: AssetStoreManager = entry.runtime_data
    with pytest.raises(AssetStoreError) as raised:
        await manager.async_archive_asset(entry, ASSET_UUID)
    assert raised.value.code == "archive_runtime_configured"


@pytest.mark.real_reload
async def test_a_reservation_survives_a_reload_before_the_add(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    entry, _device_entry = await _installation(
        hass, hass_storage, device_registry, asset_store_data
    )
    first: AssetStoreManager = entry.runtime_data
    release = await first.async_reserve_runtime_binding(entry, ASSET_UUID)

    await _reload(hass, hass_storage, entry)
    replaced: AssetStoreManager = entry.runtime_data
    assert replaced is not first

    with (
        _verified_store_readback(hass_storage),
        pytest.raises(AssetStoreError) as raised,
    ):
        await replaced.async_archive_asset(entry, ASSET_UUID)
    assert raised.value.code == "archive_runtime_binding_in_progress"
    # The replaced manager can no longer reserve for this entry.
    with pytest.raises(AssetStoreError) as stale:
        await first.async_reserve_runtime_binding(entry, ASSET_UUID)
    assert stale.value.code == "entry_not_loaded"

    release()
    with _verified_store_readback(hass_storage):
        assert await replaced.async_archive_asset(entry, ASSET_UUID) is (
            ArchiveOutcome.CHANGED
        )


@pytest.mark.real_reload
async def test_an_abandoned_flow_leaves_no_reservation(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    entry, device = await _installation(
        hass, hass_storage, device_registry, asset_store_data
    )
    initial = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_RUNTIME), context={"source": SOURCE_USER}
    )
    source = await hass.config_entries.subentries.async_configure(
        initial["flow_id"],
        {CONF_DEVICE_ID: device.id, CONF_RUNTIME_MODE: RUNTIME_MODE_ON},
    )
    assert source["step_id"] == "runtime_source"
    hass.config_entries.subentries.async_abort(source["flow_id"])
    await hass.async_block_till_done()

    assert hass.data.get(RUNTIME_BINDING_RESERVATIONS, {}) == {}
    with _verified_store_readback(hass_storage):
        assert await entry.runtime_data.async_archive_asset(entry, ASSET_UUID) is (
            ArchiveOutcome.CHANGED
        )


async def test_flow_removal_releases_a_held_reservation(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    entry = loaded_entry(hass, runtime_data=manager)
    flow = RuntimeSubentryFlow()
    flow.hass = hass
    flow._runtime_binding_release = await manager.async_reserve_runtime_binding(
        entry, ASSET_UUID
    )
    assert hass.data[RUNTIME_BINDING_RESERVATIONS]
    flow.async_remove()
    flow.async_remove()
    assert hass.data[RUNTIME_BINDING_RESERVATIONS] == {}


@pytest.mark.real_reload
async def test_runtime_for_an_archived_owner_is_refused_early_and_at_reservation(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    """The device of an archived owner is never free for Runtime."""
    entry, device = await _installation(
        hass, hass_storage, device_registry, asset_store_data, archived=True
    )
    with _verified_store_readback(hass_storage):
        result = await _create_runtime(hass, entry, device.id)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "runtime_asset_archived"}
    assert not [
        item
        for item in entry.subentries.values()
        if item.subentry_type == SUBENTRY_TYPE_RUNTIME
    ]

    # Even past the early check, the reservation refuses the archived target.
    flow = RuntimeSubentryFlow()
    flow.hass = hass
    flow._get_entry = lambda: entry  # type: ignore[method-assign]
    flow._set_runtime_context(device_id=device.id, runtime_mode=RUNTIME_MODE_ON)
    with _verified_store_readback(hass_storage):
        refused = await flow.async_step_runtime_source({CONF_SOURCE_ENTITY_ID: SWITCH})
    assert refused["errors"] == {"base": "runtime_asset_archived"}
    assert flow._runtime_binding_release is None


@pytest.mark.real_reload
async def test_a_quarantined_subentry_cannot_be_reconfigured(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    entry, device = await _installation(
        hass,
        hass_storage,
        device_registry,
        asset_store_data,
        archived=True,
        runtime=runtime_subentry_data,
    )
    subentry_id = next(iter(entry.subentries))
    runtime = {**runtime_subentry_data, CONF_DEVICE_ID: device.id}
    assert entry.runtime_data.runtime_quarantine == {subentry_id}

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_RUNTIME),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "runtime_asset_archived"
    assert dict(entry.subentries[subentry_id].data) == runtime


async def test_runtime_flows_need_a_loaded_parent(hass: HomeAssistant) -> None:
    entry = loaded_entry(hass, state=None)
    flow = RuntimeSubentryFlow()
    flow.hass = hass
    flow._get_entry = lambda: entry  # type: ignore[method-assign]
    with patch(
        "custom_components.device_lifecycle.config_flow.dr.async_get",
        return_value=SimpleNamespace(
            async_get=lambda _id: SimpleNamespace(entry_type=None)
        ),
    ):
        result = await flow.async_step_user(
            {CONF_DEVICE_ID: "device", CONF_RUNTIME_MODE: RUNTIME_MODE_ON}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"

    flow._set_runtime_context(device_id="device", runtime_mode=RUNTIME_MODE_ON)
    with patch(
        "custom_components.device_lifecycle.config_flow._runtime_source_error",
        return_value=None,
    ):
        source = await flow.async_step_runtime_source({CONF_SOURCE_ENTITY_ID: SWITCH})
    assert source["reason"] == "entry_not_loaded"

    flow._get_reconfigure_subentry = lambda: SimpleNamespace(  # type: ignore[method-assign]
        data={CONF_DEVICE_ID: "device", CONF_ASSET_UUID: ASSET_UUID}
    )
    reconfigure = await flow.async_step_reconfigure()
    assert reconfigure["reason"] == "entry_not_loaded"


@pytest.mark.real_reload
async def test_after_restore_new_runtime_resumes_from_the_canonical_total(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    entry, device = await _installation(
        hass, hass_storage, device_registry, asset_store_data, archived=True
    )
    with _verified_store_readback(hass_storage):
        assert await entry.runtime_data.async_restore_asset(ASSET_UUID) is (
            ArchiveOutcome.CHANGED
        )
        result = await _create_runtime(hass, entry, device.id)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    manager: AssetStoreManager = entry.runtime_data
    assert ASSET_UUID in manager._runtime_writers
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1000)
    durability = manager._runtime_writers[ASSET_UUID].durability
    assert durability is not None
    assert durability().committed_seconds == Decimal(1000)
    assert (
        hass_storage[STORAGE_KEY]["data"]["assets"][ASSET_UUID]["archived_at"] is None
    )


async def test_a_subentry_added_after_a_released_reservation_still_blocks_archive(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    entry = _entry(manager)
    release = await manager.async_reserve_runtime_binding(entry, ASSET_UUID)
    entry.subentries["runtime"] = _runtime_subentry()
    release()
    with pytest.raises(AssetStoreError) as raised:
        await manager.async_archive_asset(entry, ASSET_UUID)
    assert raised.value.code == "archive_runtime_configured"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("entry_not_loaded", ("abort", "entry_not_loaded")),
        ("runtime_asset_archived", ("error", "runtime_asset_archived")),
        ("asset_missing", ("error", "device_missing")),
    ],
)
async def test_a_refused_reservation_never_creates_the_subentry(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    code: str,
    expected: tuple[str, str],
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    entry = loaded_entry(hass, runtime_data=manager)
    flow = RuntimeSubentryFlow()
    flow.hass = hass
    flow._get_entry = lambda: entry  # type: ignore[method-assign]
    flow._set_runtime_context(device_id="device", runtime_mode=RUNTIME_MODE_ON)
    with (
        patch.object(
            manager, "runtime_binding_target", return_value=(ASSET_UUID, False)
        ),
        patch.object(
            manager,
            "async_reserve_runtime_binding",
            AsyncMock(side_effect=AssetStoreError("refused", code=code)),
        ),
        patch(
            "custom_components.device_lifecycle.config_flow._runtime_source_error",
            return_value=None,
        ),
        patch.object(flow, "async_create_entry") as create,
    ):
        result = await flow.async_step_runtime_source({CONF_SOURCE_ENTITY_ID: SWITCH})
    create.assert_not_called()
    kind, value = expected
    if kind == "abort":
        assert result["reason"] == value
    else:
        assert result["errors"] == {"base": value}
    assert flow._runtime_binding_release is None


async def test_reconfigure_source_refuses_an_archived_target(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data, archived=True))
    entry = loaded_entry(hass, runtime_data=manager)
    flow = RuntimeSubentryFlow()
    flow.hass = hass
    flow._get_entry = lambda: entry  # type: ignore[method-assign]
    subentry = _runtime_subentry(legacy=True)
    flow._get_reconfigure_subentry = lambda: subentry  # type: ignore[method-assign]
    flow._set_runtime_context(device_id="device", runtime_mode=RUNTIME_MODE_ON)
    manager._data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": subentry.data[CONF_DEVICE_ID], "role": "primary"}
    ]
    result = await flow.async_step_reconfigure_source({CONF_SOURCE_ENTITY_ID: SWITCH})
    assert result["reason"] == "runtime_asset_archived"
