"""Preserve durable snapshots when HA stops during an executor write."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage as ha_storage

from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    AssetStoreError,
    AssetStoreManager,
)

from . import test_persistence_lifecycle as lifecycle
from .conftest import ASSET_UUID
from .test_quick_add_storage import QUICK_UUID, SECOND_QUICK_UUID, _request


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Keep real atomic files within an isolated test configuration directory."""
    return hass_tmp_config_dir


@pytest.fixture(autouse=True)
def real_files():
    """Restore real HA file loading and the integration's executor writer."""
    with (
        patch.object(
            lifecycle.DeviceLifecycleStore, "_async_write_data", lifecycle._REAL_WRITE
        ),
        patch.object(
            lifecycle.DeviceLifecycleStore, "_async_load", lifecycle._REAL_LOAD
        ),
    ):
        yield


@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("operation", ["quick", "runtime", "metadata"])
async def test_real_ha_stop_keeps_executor_fenced_and_preserves_acknowledged_snapshot(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    operation: str,
    replacement: bool,
) -> None:
    """Real async_stop cannot allow a late writer to overwrite a STOP callback."""
    old = await lifecycle._seed(hass, asset_store_data)
    gate = lifecycle._Gate(hass)
    writer_finished = threading.Event()
    callback_started = asyncio.Event()
    acknowledged: list[AssetStoreData] = []
    failures: list[BaseException] = []
    managers: list[AssetStoreManager] = []
    first: asyncio.Task[Any] | None = None
    stop: asyncio.Task[Any] | None = None

    def _write(*args: Any, **kwargs: Any) -> None:
        first_write = gate.writes == 0
        try:
            gate.write(*args, **kwargs)
        finally:
            if first_write:
                writer_finished.set()

    async def _on_stop(_event: Any) -> None:
        callback_started.set()
        try:
            # A safe writer may still be running here. A cancelled caller must
            # finish draining it before a subsequent operation can use its state.
            try:
                assert first is not None
                await asyncio.shield(first)
            except asyncio.CancelledError:
                pass
            manager = old
            if replacement:
                manager = AssetStoreManager(hass)
                await manager.async_setup()
            managers.append(manager)
            if operation == "quick":
                await manager.async_quick_create_asset(
                    _request(asset_uuid=SECOND_QUICK_UUID)
                )
            elif operation == "runtime":
                total = manager.runtime_total_seconds(ASSET_UUID)
                assert total is not None
                await manager.async_commit_runtime_delta(
                    ASSET_UUID, expected_total=total, delta=Decimal(100)
                )
            else:
                await manager.async_update_asset_metadata(
                    ASSET_UUID, category="Acknowledged on stop"
                )
            acknowledged.append(deepcopy(manager._data))
        except (AssetStoreError, asyncio.CancelledError) as err:
            failures.append(err)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)
    with patch.object(ha_storage, "write_utf8_file_atomic", side_effect=_write):
        try:
            if operation == "runtime":
                first = hass.async_create_task(
                    old.async_commit_runtime_delta(
                        ASSET_UUID, expected_total=Decimal(1000), delta=Decimal(50)
                    )
                )
            else:
                first = hass.async_create_task(old.async_quick_create_asset(_request()))
            await asyncio.wait_for(gate.started.wait(), 5)
            # HA cancels background Futures itself; shielding a task which owns
            # one of those Futures cannot protect the running executor thread.
            stop = asyncio.create_task(hass.async_stop(force=True))
            await asyncio.wait_for(callback_started.wait(), 5)
            await asyncio.sleep(0.05)
            premature_acknowledgement = bool(acknowledged)
            gate.release.set()
            await asyncio.wait_for(stop, 5)
            assert await asyncio.to_thread(writer_finished.wait, 5)

            assert not failures
            assert len(acknowledged) == 1
            manager = managers[0]
            # Compare every collection and counter, not only the edited field.
            assert lifecycle._disk(manager) == manager._data == acknowledged[0]
            assert not premature_acknowledgement
            assert manager.asset(ASSET_UUID)["notes"] == "Keep my customization"
            if operation == "quick":
                assert manager.asset(QUICK_UUID)["asset_id"] == "DL0008"
                assert manager.asset(SECOND_QUICK_UUID)["asset_id"] == "DL0009"
                assert manager._data["next_asset_number"] == 10
            elif operation == "runtime":
                assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1150)
            else:
                assert manager.asset(QUICK_UUID)["asset_id"] == "DL0008"
                assert manager.asset(ASSET_UUID)["category"] == "Acknowledged on stop"
            if replacement:
                with pytest.raises(AssetStoreError):
                    await old.async_update_asset_metadata(
                        ASSET_UUID, name="Retained old callback"
                    )
        finally:
            gate.release.set()
            if first is not None:
                await asyncio.gather(first, return_exceptions=True)
            if stop is not None:
                await asyncio.gather(stop, return_exceptions=True)
            await asyncio.to_thread(writer_finished.wait, 5)


async def test_queued_executor_write_drains_repeated_caller_and_child_cancellation(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """Cancellation before a queued writer starts must preserve its fence."""
    manager = await lifecycle._seed(hass, asset_store_data)
    release = threading.Event()
    worker_started = threading.Event()
    submitted = asyncio.Event()
    saves: list[asyncio.Task[Any]] = []
    original_executor = hass.loop.run_in_executor
    original_save = manager._store.async_save
    replacement = AssetStoreManager(hass)
    tasks: list[asyncio.Task[Any]] = []

    def _block_worker() -> None:
        worker_started.set()
        assert release.wait(5)

    async def _save(data: AssetStoreData) -> None:
        task = asyncio.current_task()
        assert task is not None
        saves.append(task)
        await original_save(data)

    with ThreadPoolExecutor(max_workers=1) as executor:
        blocker = executor.submit(_block_worker)
        assert await asyncio.to_thread(worker_started.wait, 5)

        def _run_in_executor(pool: Any, target: Any, *args: Any) -> Any:
            if target == manager._store._write_data:
                future = original_executor(executor, target, *args)
                submitted.set()
                return future
            return original_executor(pool, target, *args)

        with (
            patch.object(hass.loop, "run_in_executor", side_effect=_run_in_executor),
            patch.object(manager._store, "async_save", side_effect=_save),
        ):
            mutation = asyncio.create_task(manager.async_quick_create_asset(_request()))
            tasks.append(mutation)
            try:
                await asyncio.wait_for(submitted.wait(), 5)
                for _ in range(3):
                    mutation.cancel()
                    saves[0].cancel()
                    await asyncio.sleep(0)
                setup = asyncio.create_task(replacement.async_setup())
                tasks.append(setup)
                await lifecycle._blocked(mutation)
                await lifecycle._blocked(setup)
                assert manager._mutation_lock.locked()
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
                blocker.result(timeout=5)
    assert mutation.cancelled()
    assert replacement._data == lifecycle._disk(replacement)
    assert replacement.asset(QUICK_UUID)["asset_id"] == "DL0008"
    assert replacement._data["next_asset_number"] == 9
    assert replacement.asset(ASSET_UUID)["notes"] == "Keep my customization"
