"""BL-03 lifecycle fencing, using real HA reloads and isolated atomic files."""

from __future__ import annotations

import asyncio
import json
import threading
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import storage as ha_storage
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle import async_unload_entry, storage
from custom_components.device_lifecycle.const import CONFIG_ENTRY_VERSION, DOMAIN
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    STORAGE_KEY,
    AssetStoreError,
    AssetStoreManager,
    AssetStorePersistenceError,
    DeviceLifecycleStore,
    _validate_store_data,
    json_util,
)

from .conftest import ASSET_UUID
from .test_persistence_safety import _mutate
from .test_quick_add_storage import QUICK_UUID, SECOND_QUICK_UUID, _request

_REAL_WRITE = DeviceLifecycleStore.__dict__.get(
    "_async_write_data", Store._async_write_data
)
_REAL_LOAD = Store._async_load


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Use a test-owned directory, never a production Store."""
    return hass_tmp_config_dir


@pytest.fixture(autouse=True)
def real_files():
    """Restore disk I/O intercepted by the HA pytest plugin."""
    with (
        patch.object(DeviceLifecycleStore, "_async_write_data", _REAL_WRITE),
        patch.object(DeviceLifecycleStore, "_async_load", _REAL_LOAD),
    ):
        yield


async def _seed(hass: HomeAssistant, data: AssetStoreData) -> AssetStoreManager:
    manager = AssetStoreManager(hass)
    data = deepcopy(data)
    data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "1000"
    data["assets"][ASSET_UUID]["notes"] = "Keep my customization"
    data["assets"][ASSET_UUID]["field_sources"]["notes"] = "user"
    _validate_store_data(data)
    await manager._store.async_save(data)
    await manager.async_setup()
    return manager


class _Gate:
    """Block a real executor write; always release in the test's finally."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.started = asyncio.Event()
        self.release = threading.Event()
        self.original = ha_storage.write_utf8_file_atomic
        self.writes = 0

    def write(self, *args: Any, **kwargs: Any) -> None:
        self.writes += 1
        if self.writes == 1:
            self.hass.loop.call_soon_threadsafe(self.started.set)
            assert self.release.wait(10), "test failed to release executor"
        self.original(*args, **kwargs)


async def _blocked(task: asyncio.Task[Any]) -> None:
    done, _ = await asyncio.wait({task}, timeout=0.03)
    assert not done


def _disk(manager: AssetStoreManager) -> AssetStoreData:
    return json_util.load_json(manager._store.path)["data"]


@pytest.mark.parametrize("operation", ["quick", "runtime", "metadata"])
async def test_replacement_setup_drains_prior_write_and_fences_old_manager(
    hass: HomeAssistant, asset_store_data: AssetStoreData, operation: str
) -> None:
    """CE1a: replacement cannot load before the old acknowledgement settles."""
    old = await _seed(hass, asset_store_data)
    gate = _Gate(hass)
    new = AssetStoreManager(hass)
    with patch.object(ha_storage, "write_utf8_file_atomic", side_effect=gate.write):
        first = asyncio.create_task(_mutate(old, operation))
        setup = None
        try:
            await asyncio.wait_for(gate.started.wait(), 5)
            setup = asyncio.create_task(new.async_setup())
            await _blocked(setup)
        finally:
            gate.release.set()
            await first
            if setup is not None:
                await setup
    acknowledged = deepcopy(old._data)
    assert new._data == acknowledged == _disk(new)
    with pytest.raises(AssetStoreError):
        await old.async_update_asset_metadata(ASSET_UUID, name="Retired edit")
    result = await new.async_quick_create_asset(_request(asset_uuid=SECOND_QUICK_UUID))
    expected = deepcopy(acknowledged)
    expected["assets"][SECOND_QUICK_UUID] = result.asset
    expected["lifecycle_events"].update(new._data["lifecycle_events"])
    expected["next_asset_number"] += 1
    assert _disk(new) == new._data == expected
    assert result.asset["asset_id"] == ("DL0009" if operation == "quick" else "DL0008")
    if operation == "quick":
        assert new.asset(QUICK_UUID)["asset_id"] == "DL0008"
    assert new.asset(ASSET_UUID)["notes"] == "Keep my customization"


async def _loaded_entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Device Lifecycle",
        unique_id="device_lifecycle_main",
        version=CONFIG_ENTRY_VERSION,
        data={},
        options={},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


@pytest.mark.real_reload
@pytest.mark.parametrize("operation", ["quick", "runtime", "metadata"])
async def test_real_reload_drains_write_and_preserves_all_acknowledged_state(
    hass: HomeAssistant, asset_store_data: AssetStoreData, operation: str
) -> None:
    """CE1b, including Quick Create IDs and Runtime delta through real reload."""
    await _seed(hass, asset_store_data)
    entry = await _loaded_entry(hass)
    old = entry.runtime_data
    gate = _Gate(hass)
    with patch.object(ha_storage, "write_utf8_file_atomic", side_effect=gate.write):
        first = asyncio.create_task(_mutate(old, operation))
        reload = None
        try:
            await asyncio.wait_for(gate.started.wait(), 5)
            reload = asyncio.create_task(
                hass.config_entries.async_reload(entry.entry_id)
            )
            await _blocked(reload)
        finally:
            gate.release.set()
            await first
            if reload is not None:
                assert await reload
    await hass.async_block_till_done()
    new = entry.runtime_data
    assert entry.state is ConfigEntryState.LOADED and new is not old
    expected = deepcopy(old._data)
    assert new._data == _disk(new) == expected
    with pytest.raises(AssetStoreError):
        await old.async_quick_create_asset(_request(asset_uuid=SECOND_QUICK_UUID))
    await new.async_update_asset_metadata(ASSET_UUID, category="After reload")
    expected["assets"][ASSET_UUID]["category"] = "After reload"
    expected["assets"][ASSET_UUID]["field_sources"]["category"] = "user"
    assert new._data == _disk(new) == expected
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_concurrent_mutations_are_ordered_around_replacement(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    old = await _seed(hass, asset_store_data)
    gate = _Gate(hass)
    new = AssetStoreManager(hass)
    with patch.object(ha_storage, "write_utf8_file_atomic", side_effect=gate.write):
        first = asyncio.create_task(old.async_quick_create_asset(_request()))
        tasks = []
        try:
            await asyncio.wait_for(gate.started.wait(), 5)
            tasks.append(
                asyncio.create_task(
                    old.async_update_asset_metadata(ASSET_UUID, name="Queued edit")
                )
            )
            await asyncio.sleep(0)
            tasks.append(asyncio.create_task(new.async_setup()))
            await asyncio.sleep(0)
            tasks.append(
                asyncio.create_task(
                    old.async_update_asset_metadata(ASSET_UUID, category="Too late")
                )
            )
            tasks.append(
                asyncio.create_task(
                    new.async_quick_create_asset(_request(asset_uuid=SECOND_QUICK_UUID))
                )
            )
            await _blocked(tasks[1])
        finally:
            gate.release.set()
            await first
            results = await asyncio.gather(*tasks, return_exceptions=True)
    assert not isinstance(results[0], BaseException)
    assert results[1] is None
    assert isinstance(results[2], AssetStoreError)
    assert results[3].asset["asset_id"] == "DL0009"
    assert new.asset(QUICK_UUID)["asset_id"] == "DL0008"
    assert new.asset(ASSET_UUID)["name"] == "Queued edit"
    assert new.asset(ASSET_UUID)["notes"] == "Keep my customization"
    assert _disk(new) == new._data and new._data["next_asset_number"] == 10


@pytest.mark.parametrize("failure", ["platform-false", "platform-error", "checkpoint"])
async def test_failed_unload_keeps_manager_writable_until_successful_retry(
    hass: HomeAssistant, asset_store_data: AssetStoreData, failure: str
) -> None:
    manager = await _seed(hass, asset_store_data)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.runtime_data = manager
    platforms = AsyncMock(return_value=False)
    prepare = AsyncMock(side_effect=AssetStoreError("checkpoint failed"))
    if failure == "platform-error":
        platforms.side_effect = RuntimeError("platform unload failed")
    with patch.object(hass.config_entries, "async_unload_platforms", platforms):
        if failure == "checkpoint":
            with patch.object(manager, "async_prepare_runtime_unload", prepare):
                assert not await async_unload_entry(hass, entry)
            platforms.assert_not_awaited()
        elif failure == "platform-error":
            with pytest.raises(RuntimeError):
                await async_unload_entry(hass, entry)
        else:
            assert not await async_unload_entry(hass, entry)
    await manager.async_update_asset_metadata(ASSET_UUID, name="After failed unload")
    with patch.object(
        hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
    ):
        assert await async_unload_entry(hass, entry)
    before = Path(manager._store.path).read_bytes()
    with pytest.raises(AssetStoreError):
        await manager.async_update_asset_metadata(ASSET_UUID, name="After retirement")
    assert Path(manager._store.path).read_bytes() == before


@pytest.mark.parametrize(
    "operation", ["metadata", "quick", "runtime", "maintenance", "archive"]
)
@pytest.mark.parametrize("stopping", [False, True])
async def test_cancelled_verified_write_publishes_before_cancellation_returns(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    operation: str,
    stopping: bool,
) -> None:
    """CE2: all transports publish exact verified state and still cancel."""
    manager = await _seed(hass, asset_store_data)
    listener = Mock()
    manager.async_add_publish_listener(listener)
    gate = _Gate(hass)
    previous = hass.state
    if stopping:
        hass.set_state(CoreState.stopping)
    try:
        with patch.object(ha_storage, "write_utf8_file_atomic", side_effect=gate.write):
            task = asyncio.create_task(_mutate(manager, operation))
            try:
                await asyncio.wait_for(gate.started.wait(), 5)
                task.cancel()
                await asyncio.sleep(0)
            finally:
                gate.release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert manager._data == _disk(manager)
        assert not manager._persistence_uncertain
        listener.assert_called_once()
        assert gate.writes == 1
    finally:
        hass.set_state(previous)


async def test_stalled_io_logs_once_and_blocks_cancelled_unload_and_setup(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CE3: diagnostics never release a fence while an executor can still write."""
    manager = await _seed(hass, asset_store_data)
    gate = _Gate(hass)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.runtime_data = manager
    new = AssetStoreManager(hass)
    with (
        patch.object(storage, "PERSISTENCE_WARNING_SECONDS", 0.02, create=True),
        patch.object(
            hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)
        ),
        patch.object(ha_storage, "write_utf8_file_atomic", side_effect=gate.write),
    ):
        task = asyncio.create_task(
            manager.async_update_asset_metadata(ASSET_UUID, name="Stalled edit")
        )
        unload = setup = None
        try:
            await asyncio.wait_for(gate.started.wait(), 5)
            unload = asyncio.create_task(async_unload_entry(hass, entry))
            await asyncio.sleep(0)
            setup = asyncio.create_task(new.async_setup())
            for _ in range(5):
                task.cancel()
                await asyncio.sleep(0.01)
            await _blocked(task)
            await _blocked(unload)
            await _blocked(setup)
            warnings = [r for r in caplog.records if "still pending" in r.message]
            assert 1 <= len(warnings) <= 2
            await asyncio.sleep(0.05)
            assert len(
                [r for r in caplog.records if "still pending" in r.message]
            ) == len(warnings)
        finally:
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            if unload is not None:
                assert await unload
            if setup is not None:
                await setup
    assert new._data == _disk(new)
    assert new.asset(ASSET_UUID)["name"] == "Stalled edit"


@pytest.mark.parametrize(
    "operation", ["metadata", "quick", "maintenance", "archive", "runtime"]
)
async def test_unexpected_recovery_error_stays_classified_and_fail_closed(
    hass: HomeAssistant, asset_store_data: AssetStoreData, operation: str
) -> None:
    """CE4: neither immediate resolution nor next recovery leaks RuntimeError."""
    manager = await _seed(hass, asset_store_data)
    before = deepcopy(manager._data)
    read = json_util.load_json
    calls = 0
    failure = RuntimeError("cannot schedule new futures after shutdown")

    def _read(path: str) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            envelope = read(path)
            envelope["data"]["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "2000"
            Path(path).write_text(json.dumps(envelope))
            return envelope
        raise failure

    with patch.object(json_util, "load_json", side_effect=_read):
        with pytest.raises(AssetStoreError):
            await _mutate(manager, operation)
        durable = Path(manager._store.path).read_bytes()
        for _ in range(2):
            with pytest.raises(AssetStorePersistenceError) as error:
                await manager.async_update_asset_metadata(
                    ASSET_UUID, category="Blocked"
                )
            assert error.value.__cause__ is failure
            assert manager._persistence_uncertain and manager._data == before
            assert Path(manager._store.path).read_bytes() == durable
    if operation == "archive":
        await manager.async_restore_asset(ASSET_UUID)
    else:
        await manager.async_update_asset_metadata(ASSET_UUID, category="Recovered")
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(2000)
    assert manager._data == _disk(manager)


async def test_recovery_cancellation_is_not_converted_or_published(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = await _seed(hass, asset_store_data)
    before = deepcopy(manager._data)
    manager._persistence_uncertain = True
    with (
        patch.object(json_util, "load_json", side_effect=asyncio.CancelledError),
        pytest.raises(asyncio.CancelledError),
    ):
        await manager.async_update_asset_metadata(ASSET_UUID, name="Unverified")
    assert manager._persistence_uncertain and manager._data == before


@pytest.mark.parametrize("cancel_setup", [False, True])
async def test_migration_drains_before_retry_and_concurrent_mutation(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData, cancel_setup: bool
) -> None:
    """HA async_load's migration save has the same fence as normal commits."""
    first = AssetStoreManager(hass)
    path = Path(first._store.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 3,
                "minor_version": 1,
                "key": STORAGE_KEY,
                "data": asset_store_data_v3_1,
            }
        )
    )
    gate = _Gate(hass)
    new = AssetStoreManager(hass)
    with patch.object(ha_storage, "write_utf8_file_atomic", side_effect=gate.write):
        setup = asyncio.create_task(first.async_setup())
        retry = edit = None
        try:
            await asyncio.wait_for(gate.started.wait(), 5)
            if cancel_setup:
                setup.cancel()
            await asyncio.sleep(0)
            retry = asyncio.create_task(new.async_setup())
            await asyncio.sleep(0)
            edit = asyncio.create_task(
                new.async_update_asset_metadata(ASSET_UUID, category="After migration")
            )
            await _blocked(setup)
            await _blocked(retry)
            await _blocked(edit)
        finally:
            gate.release.set()
            if cancel_setup:
                with pytest.raises(asyncio.CancelledError):
                    await setup
            else:
                await setup
            if retry is not None:
                await retry
            if edit is not None:
                await edit
    expected = storage._migrate_v3_1_to_v4_1(deepcopy(asset_store_data_v3_1))
    expected["assets"][ASSET_UUID]["category"] = "After migration"
    expected["assets"][ASSET_UUID]["field_sources"]["category"] = "user"
    assert new._data == _disk(new) == expected
    assert gate.writes == 2


@pytest.mark.real_reload
async def test_real_reload_waits_for_live_runtime_writer_checkpoint(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """Exercise Runtime-lock -> Store-lock ordering, not only a direct delta."""
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
    gate = _Gate(hass)
    with patch.object(ha_storage, "write_utf8_file_atomic", side_effect=gate.write):
        checkpoint = asyncio.create_task(old.async_checkpoint_runtime(ASSET_UUID))
        reload = None
        try:
            await asyncio.wait_for(gate.started.wait(), 5)
            reload = asyncio.create_task(
                hass.config_entries.async_reload(entry.entry_id)
            )
            await _blocked(reload)
        finally:
            gate.release.set()
            assert await checkpoint == Decimal(1010)
            if reload is not None:
                assert await reload
    await hass.async_block_till_done()
    assert sensor._removing and sensor._pending == []
    assert entry.runtime_data.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
    assert entry.runtime_data._data == _disk(entry.runtime_data)
    await sensor.async_will_remove_from_hass()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_cancelled_retirement_drain_does_not_close_failed_unload(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = await _seed(hass, asset_store_data)
    entry = MockConfigEntry(domain=DOMAIN)
    entry.runtime_data = manager
    gate = _Gate(hass)
    unloaded = asyncio.Event()

    async def _platforms(_entry, _platforms):
        unloaded.set()
        return True

    with (
        patch.object(ha_storage, "write_utf8_file_atomic", side_effect=gate.write),
        patch.object(
            hass.config_entries, "async_unload_platforms", side_effect=_platforms
        ),
    ):
        mutation = asyncio.create_task(manager.async_quick_create_asset(_request()))
        try:
            await asyncio.wait_for(gate.started.wait(), 5)
            unload = asyncio.create_task(async_unload_entry(hass, entry))
            await asyncio.wait_for(unloaded.wait(), 5)
            await _blocked(unload)
            unload.cancel()
            with pytest.raises(asyncio.CancelledError):
                await unload
        finally:
            gate.release.set()
            await mutation
        await manager.async_update_asset_metadata(
            ASSET_UUID, name="After cancelled unload"
        )
        assert await async_unload_entry(hass, entry)
    with pytest.raises(AssetStoreError):
        await manager.async_update_asset_metadata(
            ASSET_UUID, category="After retirement"
        )
    assert _disk(manager) == manager._data


async def test_independently_cancelled_save_stays_uncertain_until_recovery(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """F7: a child cancelled before writing cannot publish the candidate."""
    manager = await _seed(hass, asset_store_data)
    before = deepcopy(manager._data)
    with (
        patch.object(
            manager._store, "async_save", AsyncMock(side_effect=asyncio.CancelledError)
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await manager.async_update_asset_metadata(ASSET_UUID, name="Not written")
    assert manager._persistence_uncertain and manager._data == before == _disk(manager)
    await manager.async_update_asset_metadata(ASSET_UUID, category="Recovered")
    assert not manager._persistence_uncertain


async def test_cancelling_save_task_itself_cannot_release_a_live_executor_write(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """Shutdown can cancel child tasks as well as the waiting mutation caller."""
    manager = await _seed(hass, asset_store_data)
    gate = _Gate(hass)
    save_task = None
    original = manager._store.async_save

    async def _save(data):
        nonlocal save_task
        save_task = asyncio.current_task()
        return await original(data)

    with (
        patch.object(manager._store, "async_save", side_effect=_save),
        patch.object(ha_storage, "write_utf8_file_atomic", side_effect=gate.write),
    ):
        mutation = asyncio.create_task(manager.async_quick_create_asset(_request()))
        followup = None
        try:
            await asyncio.wait_for(gate.started.wait(), 5)
            for _ in range(3):
                save_task.cancel()
                mutation.cancel()
                await asyncio.sleep(0)
            followup = asyncio.create_task(
                manager.async_update_asset_metadata(
                    ASSET_UUID, category="After child cancellation"
                )
            )
            await _blocked(mutation)
            await _blocked(followup)
            assert manager._mutation_lock.locked()
        finally:
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await mutation
            if followup is not None:
                await followup
    assert manager.asset(QUICK_UUID)["asset_id"] == "DL0008"
    assert manager._data["next_asset_number"] == 9
    assert manager._data == _disk(manager)


async def test_caller_timeout_waits_for_io_then_publishes_and_raises_timeout(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = await _seed(hass, asset_store_data)
    gate = _Gate(hass)

    async def _bounded():
        async with asyncio.timeout(0.02):
            await manager.async_commit_runtime_delta(
                ASSET_UUID, expected_total=Decimal(1000), delta=Decimal(50)
            )

    with patch.object(ha_storage, "write_utf8_file_atomic", side_effect=gate.write):
        task = asyncio.create_task(_bounded())
        try:
            await asyncio.wait_for(gate.started.wait(), 5)
            await asyncio.sleep(0.04)
            await _blocked(task)
        finally:
            gate.release.set()
            with pytest.raises(TimeoutError):
                await task
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1050)
    assert not manager._persistence_uncertain and manager._data == _disk(manager)


async def test_failed_existence_check_is_an_uncertain_recovery_error(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = await _seed(hass, asset_store_data)
    before = deepcopy(manager._data)
    manager._persistence_uncertain = True
    failure = RuntimeError("executor closed")
    with (
        patch.object(json_util, "load_json", return_value={}),
        patch.object(storage.os.path, "exists", side_effect=failure),
        pytest.raises(AssetStorePersistenceError) as error,
    ):
        await manager.async_update_asset_metadata(ASSET_UUID, name="Unverified")
    assert error.value.__cause__ is failure
    assert manager._persistence_uncertain and manager._data == before


async def test_failed_setup_cannot_mutate_and_can_retry_without_reusing_ids(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    old = await _seed(hass, asset_store_data)
    manager = AssetStoreManager(hass)
    with (
        patch.object(
            manager._store, "async_load", side_effect=AssetStoreError("failed setup")
        ),
        pytest.raises(AssetStoreError),
    ):
        await manager.async_setup()
    with pytest.raises(AssetStoreError):
        await manager.async_quick_create_asset(_request())
    await manager.async_setup()
    assert manager._data == old._data
    with pytest.raises(AssetStoreError):
        await old.async_setup()
    result = await manager.async_quick_create_asset(_request())
    assert result.asset["asset_id"] == "DL0008"


@pytest.mark.real_reload
async def test_publish_and_subentry_update_can_checkpoint_then_reload_without_deadlock(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """Callbacks schedule work outside the fence; Runtime locks precede it."""
    from homeassistant.config_entries import ConfigSubentry

    from .test_runtime import _Clock, _sensor
    from .test_runtime_checkpoint import _add

    await _seed(hass, asset_store_data)
    entry = await _loaded_entry(hass)
    manager = entry.runtime_data
    sensor = _sensor(hass, manager, _Clock(10))
    await _add(hass, sensor)
    subentry = ConfigSubentry(
        data={}, subentry_type="runtime", title="Removed Runtime", unique_id=None
    )
    tasks = []

    def _published(_changed):
        tasks.append(asyncio.create_task(manager.async_checkpoint_runtime(ASSET_UUID)))

    remove = manager.async_add_publish_listener(_published)
    try:
        await asyncio.wait_for(
            manager.async_commit_runtime_delta(
                ASSET_UUID, expected_total=Decimal(1000), delta=Decimal(10)
            ),
            5,
        )
        await asyncio.gather(*tasks)
        # Removal update retires the registered orphan writer, then schedules
        # the actual HA reload. It never holds the Store fence around Runtime.
        hass.config_entries.async_add_subentry(entry, subentry)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data is not manager
        assert entry.runtime_data.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
        assert _disk(entry.runtime_data) == entry.runtime_data._data
        hass.config_entries.async_update_subentry(
            entry, subentry, title="Updated Runtime"
        )
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
    finally:
        remove()
        await sensor.async_will_remove_from_hass()
        await hass.config_entries.async_unload(entry.entry_id)
