"""Supported HA setup ownership and recovery must protect durable identity."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState, OperationNotAllowed
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle import storage
from custom_components.device_lifecycle.const import CONFIG_ENTRY_VERSION, DOMAIN
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    _STORE_LIFECYCLES,
    AssetStoreError,
    AssetStoreManager,
    DeviceLifecycleStore,
)

from .conftest import ASSET_UUID
from .test_persistence_lifecycle import (
    _REAL_LOAD,
    _REAL_WRITE,
    _blocked,
    _disk,
    _loaded_entry,
    _seed,
)
from .test_quick_add_storage import QUICK_UUID, _request

pytestmark = [pytest.mark.real_reload, pytest.mark.usefixtures("real_files")]


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Use a test-owned directory, never a production Store."""
    return hass_tmp_config_dir


@pytest.fixture(autouse=True)
def real_files() -> Iterator[None]:
    """Use the production Store writer and reader with isolated file I/O."""
    with (
        patch.object(DeviceLifecycleStore, "_async_write_data", _REAL_WRITE),
        patch.object(DeviceLifecycleStore, "_async_load", _REAL_LOAD),
    ):
        yield


async def _reject_old(manager: AssetStoreManager) -> None:
    with pytest.raises(AssetStoreError) as error:
        await manager.async_update_asset_metadata(ASSET_UUID, name="Retired write")
    assert error.value.code == "entry_not_loaded"


async def test_loaded_entry_rejects_second_supported_setup(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """A second setup cannot replace a live manager through ConfigEntries."""
    await _seed(hass, asset_store_data)
    entry = await _loaded_entry(hass)
    manager = entry.runtime_data
    before = deepcopy(manager._data)
    with (
        patch.object(DeviceLifecycleStore, "async_load") as load,
        pytest.raises(OperationNotAllowed),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
    load.assert_not_called()
    assert entry.runtime_data is manager and not manager._retired
    assert _disk(manager) == before
    await manager.async_update_asset_metadata(ASSET_UUID, category="Still active")
    assert manager._data == _disk(manager)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("restart_owner_state", [False, True])
async def test_initial_corrupt_load_and_retry_require_backup_before_quick_create(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    restart_owner_state: bool,
) -> None:
    """The sidecar evidence must survive loss of in-memory lifecycle objects."""
    old = await _seed(hass, asset_store_data)
    path = Path(old._store.path)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Device Lifecycle",
        unique_id="device_lifecycle_main",
        version=CONFIG_ENTRY_VERSION,
        data={},
        options={},
    )
    entry.add_to_hass(hass)
    # Normalize against this empty entry before taking the recovery baseline.
    # The fixture's historical Purchase is retained but no longer configured.
    await old.async_reconcile_entry(entry)
    original = path.read_bytes()
    path.write_text('{"version":4,"data":', encoding="utf-8")
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR and not path.exists()
    if restart_owner_state:
        # Isolate the persistent guard from any in-memory failure latch. This
        # simulates lost integration ownership state, not a full HA restart.
        hass.data.pop(_STORE_LIFECYCLES)
    assert not await hass.config_entries.async_reload(entry.entry_id), (
        "a missing Store with preserved corruption must require backup recovery"
    )
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert not path.exists()
    lifecycle = AssetStoreManager(hass)._lifecycle
    failed = lifecycle.owner
    assert not failed._setup_ready
    with pytest.raises(AssetStoreError) as error:
        await failed.async_quick_create_asset(_request())
    assert error.value.code == "entry_not_loaded" and not path.exists()
    path.write_bytes(original)
    assert await hass.config_entries.async_reload(entry.entry_id)
    new = entry.runtime_data
    assert new._data == _disk(new) == old._data
    result = await new.async_quick_create_asset(_request())
    assert result.asset["asset_id"] == "DL0008"
    assert new.asset(ASSET_UUID)["notes"] == "Keep my customization"
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("failure", ["load", "unsupported", "invalid-payload"])
async def test_failed_real_reload_has_no_active_predecessor_and_retry_recovers(
    hass: HomeAssistant, asset_store_data: AssetStoreData, failure: str
) -> None:
    """Successful unload retires the old owner before replacement is attempted."""
    await _seed(hass, asset_store_data)
    entry = await _loaded_entry(hass)
    old = entry.runtime_data
    before = deepcopy(old._data)
    path = Path(old._store.path)
    original = path.read_bytes()
    if failure != "load":
        envelope = json.loads(original)
        if failure == "unsupported":
            envelope["version"] = 999
        else:
            envelope["data"]["assets"][ASSET_UUID]["asset_id"] = "invalid ID"
        path.write_text(json.dumps(envelope), encoding="utf-8")

    actual_load = DeviceLifecycleStore.async_load

    async def load(store):
        assert old._retired, "supported reload must retire before replacement load"
        if failure == "load":
            raise AssetStoreError("injected setup load failure")
        return await actual_load(store)

    with patch.object(DeviceLifecycleStore, "async_load", load):
        assert not await hass.config_entries.async_reload(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    failed = old._lifecycle.owner
    assert failed is not old and not failed._setup_ready
    await _reject_old(old)
    await _reject_old(failed)
    path.write_bytes(original)
    assert await hass.config_entries.async_reload(entry.entry_id)
    new = entry.runtime_data
    assert new is not old and new is not failed
    assert new._data == _disk(new) == before
    await _reject_old(old)
    await _reject_old(failed)
    result = await new.async_quick_create_asset(_request())
    assert result.asset["asset_id"] == "DL0008"
    assert new.asset(QUICK_UUID)["asset_uuid"] == QUICK_UUID
    assert new.asset(ASSET_UUID)["notes"] == "Keep my customization"
    assert new._data == _disk(new)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_cancelled_real_reload_drains_loading_before_retry(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """Cancelled setup never revives an owner retired by successful unload."""
    await _seed(hass, asset_store_data)
    entry = await _loaded_entry(hass)
    old = entry.runtime_data
    before = deepcopy(old._data)
    started = asyncio.Event()
    release = asyncio.Event()
    actual_load = DeviceLifecycleStore.async_load

    async def load(store):
        started.set()
        await release.wait()
        return await actual_load(store)

    with patch.object(DeviceLifecycleStore, "async_load", load):
        reload = asyncio.create_task(hass.config_entries.async_reload(entry.entry_id))
        retry = None
        try:
            await asyncio.wait_for(started.wait(), 5)
            assert old._retired
            reload.cancel()
            await _blocked(reload)
            retry = asyncio.create_task(
                hass.config_entries.async_reload(entry.entry_id)
            )
            await _blocked(retry)
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await reload
            if retry is not None:
                assert await retry
    new = entry.runtime_data
    assert new._data == _disk(new) == before
    await _reject_old(old)
    await new.async_update_asset_metadata(ASSET_UUID, name="Retried after cancellation")
    assert new._data == _disk(new)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_corrupt_store_retry_cannot_start_empty_or_reuse_permanent_ids(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """HA's corrupt-file rename must not turn a retry into a first install."""
    await _seed(hass, asset_store_data)
    entry = await _loaded_entry(hass)
    old = entry.runtime_data
    path = Path(old._store.path)
    original = path.read_bytes()
    path.write_text('{"version":4,"data":', encoding="utf-8")
    assert not await hass.config_entries.async_reload(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert not path.exists()
    preserved = list(path.parent.glob(f"{path.name}.corrupt.*"))
    assert len(preserved) == 1
    await _reject_old(old)
    assert not await hass.config_entries.async_reload(entry.entry_id), (
        "retry must require recovery while the canonical Store is missing and "
        "HA has preserved its corrupt predecessor"
    )
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert not path.exists() and preserved[0].exists()
    failed = old._lifecycle.owner
    assert not failed._setup_ready, "retry must not authorize an empty owner"
    await _reject_old(failed)
    path.write_bytes(original)
    assert await hass.config_entries.async_reload(entry.entry_id)
    new = entry.runtime_data
    assert new._data == old._data == _disk(new)
    result = await new.async_quick_create_asset(_request())
    assert result.asset["asset_id"] == "DL0008"
    assert new.runtime_total_seconds(ASSET_UUID) == Decimal(1000)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("unrelated_sidecar", [False, True])
async def test_fresh_store_can_initialize_without_resetting_an_existing_key(
    hass: HomeAssistant, unrelated_sidecar: bool
) -> None:
    """An absent directory or another integration's sidecar is a normal install."""
    manager = AssetStoreManager(hass)
    path = Path(manager._store.path)
    if unrelated_sidecar:
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.parent / "other_integration.assets.corrupt.timestamp").write_text(
            "preserved other file", encoding="utf-8"
        )
    await manager.async_setup()
    result = await manager.async_quick_create_asset(_request())
    assert result.asset["asset_id"] == "DL0001"
    assert manager._data == _disk(manager)


async def test_unreadable_corruption_evidence_directory_fails_closed_and_can_retry(
    hass: HomeAssistant,
) -> None:
    """A failed sidecar inspection cannot authorize first-install allocation."""
    manager = AssetStoreManager(hass)
    path = Path(manager._store.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    failure = PermissionError("cannot inspect recovery evidence")
    with (
        patch.object(storage.os, "scandir", side_effect=failure),
        pytest.raises(PermissionError) as error,
    ):
        await manager.async_setup()
    assert error.value is failure and not manager._setup_ready
    with pytest.raises(AssetStoreError):
        await manager.async_quick_create_asset(_request())
    assert not path.exists()
    await manager.async_setup()
    result = await manager.async_quick_create_asset(_request())
    assert result.asset["asset_id"] == "DL0001"


async def test_overlapping_supported_reloads_serialize_failed_setup_then_retry(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """The entry lock prevents two simultaneous setup attempts and owner revival."""
    await _seed(hass, asset_store_data)
    entry = await _loaded_entry(hass)
    old = entry.runtime_data
    before = deepcopy(old._data)
    started, release = asyncio.Event(), asyncio.Event()
    actual_load = DeviceLifecycleStore.async_load
    loads = 0
    owners = []

    async def load(store):
        nonlocal loads
        loads += 1
        owners.append(old._lifecycle.owner)
        if loads == 1:
            started.set()
            await release.wait()
            raise AssetStoreError("first overlapping setup fails")
        return await actual_load(store)

    with patch.object(DeviceLifecycleStore, "async_load", load):
        first = asyncio.create_task(hass.config_entries.async_reload(entry.entry_id))
        second = None
        try:
            await asyncio.wait_for(started.wait(), 5)
            assert old._retired
            second = asyncio.create_task(
                hass.config_entries.async_reload(entry.entry_id)
            )
            await _blocked(second)
            assert loads == 1
        finally:
            release.set()
            assert not await first
            if second is not None:
                assert await second
    new = entry.runtime_data
    assert loads == 2 and owners[0] is not owners[1] is new
    assert owners[0]._retired and not new._retired
    assert new._data == _disk(new) == before
    await _reject_old(old)
    await _reject_old(owners[0])
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_replacement_setup_fences_retained_old_callbacks_queued_during_loading(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """Queued old references cannot write after the successful ownership transfer."""
    await _seed(hass, asset_store_data)
    entry = await _loaded_entry(hass)
    old = entry.runtime_data
    before = deepcopy(old._data)
    started, release = asyncio.Event(), asyncio.Event()
    actual_load = DeviceLifecycleStore.async_load

    async def load(store):
        started.set()
        await release.wait()
        return await actual_load(store)

    with patch.object(DeviceLifecycleStore, "async_load", load):
        reload = asyncio.create_task(hass.config_entries.async_reload(entry.entry_id))
        queued = []
        try:
            await asyncio.wait_for(started.wait(), 5)
            queued = [
                asyncio.create_task(
                    old.async_update_asset_metadata(ASSET_UUID, name="Stale callback")
                ),
                asyncio.create_task(old.async_quick_create_asset(_request())),
                asyncio.create_task(
                    old.async_commit_runtime_delta(
                        ASSET_UUID, expected_total=Decimal(1000), delta=Decimal(10)
                    )
                ),
            ]
            for task in queued:
                await _blocked(task)
        finally:
            release.set()
            assert await reload
            results = await asyncio.gather(*queued, return_exceptions=True)
    assert all(
        isinstance(result, AssetStoreError) and result.code == "entry_not_loaded"
        for result in results
    )
    new = entry.runtime_data
    assert new._data == _disk(new) == before
    result = await new.async_quick_create_asset(_request())
    assert result.asset["asset_id"] == "DL0008"
    assert new.runtime_total_seconds(ASSET_UUID) == Decimal(1000)
    assert new.asset(ASSET_UUID)["notes"] == "Keep my customization"
    assert new._data == _disk(new)
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_reload_setup_failure_after_runtime_preparation_preserves_total(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """A failed replacement cannot revive the quiesced writer or lose its delta."""
    from .test_runtime import _Clock, _sensor
    from .test_runtime_checkpoint import _add

    await _seed(hass, asset_store_data)
    entry = await _loaded_entry(hass)
    old = entry.runtime_data
    clock = _Clock(0)
    sensor = _sensor(hass, old, clock)
    await _add(hass, sensor)
    sensor._active_since = 0.0
    clock.value = 10
    try:
        with patch.object(
            DeviceLifecycleStore,
            "async_load",
            side_effect=AssetStoreError("replacement setup fails after Runtime flush"),
        ):
            assert not await hass.config_entries.async_reload(entry.entry_id)
        assert old._retired and sensor._removing and sensor._pending == []
        assert _disk(old) == old._data
        assert old.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
        await _reject_old(old)
        assert await hass.config_entries.async_reload(entry.entry_id)
        new = entry.runtime_data
        assert new._data == _disk(new) == old._data
        assert new.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
        assert new.asset(ASSET_UUID)["notes"] == "Keep my customization"
    finally:
        await sensor.async_will_remove_from_hass()
    assert await hass.config_entries.async_unload(entry.entry_id)
