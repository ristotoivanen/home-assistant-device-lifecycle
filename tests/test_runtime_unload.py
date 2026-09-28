"""Config-entry unload is gated on a durable Runtime checkpoint."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, State

from custom_components.device_lifecycle import PLATFORMS, async_unload_entry
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.sensor import DeviceRuntimeHoursSensor
from custom_components.device_lifecycle.storage import (
    AssetStoreError,
    AssetStoreManager,
)

from .conftest import ASSET_UUID, SOURCE_ENTITY_ID
from .test_runtime import _Clock, _sensor
from .test_runtime_checkpoint import _add, _runtime_manager

ASSET_BEFORE = "00000000-0000-4000-8000-000000000001"
ASSET_AFTER = "ffffffff-ffff-4fff-8fff-fffffffffff1"


def _event(old: str, new: str) -> SimpleNamespace:
    return SimpleNamespace(
        data={
            "old_state": State(SOURCE_ENTITY_ID, old),
            "new_state": State(SOURCE_ENTITY_ID, new),
        }
    )


async def _active_sensor(
    hass: HomeAssistant,
    manager: AssetStoreManager,
    clock: _Clock,
    since: float,
) -> DeviceRuntimeHoursSensor:
    sensor = _sensor(hass, manager, clock)
    await _add(hass, sensor)
    sensor._active_since = since
    return sensor


def _pending(sensor: DeviceRuntimeHoursSensor) -> list[tuple[Decimal, Decimal]]:
    return [(item.expected_total, item.delta) for item in sensor._pending]


async def _unload(
    hass: HomeAssistant,
    manager: AssetStoreManager,
    *,
    sensors: tuple[DeviceRuntimeHoursSensor, ...] = (),
    platforms_unloaded: bool = True,
) -> tuple[bool, Mock]:
    """Run async_unload_entry; platform unload removes the given entities."""

    async def _unload_platforms(_entry: Any, _platforms: Any) -> bool:
        if platforms_unloaded:
            for sensor in sensors:
                await sensor.async_will_remove_from_hass()
        return platforms_unloaded

    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        side_effect=_unload_platforms,
    ) as unload_platforms:
        result = await async_unload_entry(hass, SimpleNamespace(runtime_data=manager))
    return result, unload_platforms


def _failing_save(manager: AssetStoreManager) -> None:
    manager._store.async_save = AsyncMock(side_effect=OSError("disk unavailable"))


def _working_save(manager: AssetStoreManager) -> None:
    manager._store.async_save = AsyncMock()


# The reproduced gap: a failed final flush during unload


async def test_failed_final_checkpoint_blocks_unload(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Before the gate, unload proceeded, the entity took +10 s with it, and a
    later checkpoint reported the old 1000. Now nothing is unloaded."""
    manager = _runtime_manager(hass, asset_store_data)
    clock = _Clock(50)
    sensor = await _active_sensor(hass, manager, clock, 50.0)
    clock.value = 60
    _failing_save(manager)

    with caplog.at_level(logging.ERROR):
        result, unload_platforms = await _unload(hass, manager, sensors=(sensor,))

    assert result is False
    unload_platforms.assert_not_called()
    assert "was not unloaded because Runtime could not be durably" in caplog.text
    assert set(manager._runtime_writers) == {ASSET_UUID}
    assert _pending(sensor) == [(Decimal(1000), Decimal("10.0"))]
    assert sensor._removing is False
    assert sensor._active_since == 60
    with pytest.raises(AssetStoreError) as info:
        await manager.async_checkpoint_runtime(ASSET_UUID)
    assert info.value.code == "runtime_checkpoint_failed"


async def test_active_failure_timeline_resumes_without_double_counting(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """Canonical 1000, active 50, prepare 60 fails, checkpoint 65 gives 1015."""
    manager = _runtime_manager(hass, asset_store_data)
    clock = _Clock(50)
    sensor = await _active_sensor(hass, manager, clock, 50.0)
    clock.value = 60
    _failing_save(manager)
    with pytest.raises(AssetStoreError) as info:
        await manager.async_prepare_runtime_unload()
    assert info.value.code == "runtime_unload_checkpoint_failed"
    assert ASSET_UUID in str(info.value)
    assert _pending(sensor) == [(Decimal(1000), Decimal("10.0"))]

    _working_save(manager)
    clock.value = 65
    assert await manager.async_checkpoint_runtime(ASSET_UUID) == Decimal(1015)
    assert sensor._pending == []
    assert sensor._active_since == 65


# Successful unload


async def test_successful_unload_is_durable_and_leaves_nothing_behind(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _runtime_manager(hass, asset_store_data)
    clock = _Clock(50)
    sensor = await _active_sensor(hass, manager, clock, 50.0)
    listeners = {name: Mock() for name in ("source", "interval", "shutdown")}
    sensor._unsub_source = listeners["source"]
    sensor._unsub_interval = listeners["interval"]
    sensor._unsub_shutdown = listeners["shutdown"]
    clock.value = 60
    saves_before_removal: list[int] = []
    original = sensor.async_will_remove_from_hass

    async def _remove() -> None:
        saves_before_removal.append(manager._store.async_save.await_count)
        await original()

    sensor.async_will_remove_from_hass = _remove  # type: ignore[method-assign]

    result, unload_platforms = await _unload(hass, manager, sensors=(sensor,))

    assert result is True
    unload_platforms.assert_awaited_once()
    assert unload_platforms.call_args.args[1] == PLATFORMS
    # The seal was persisted before platform unload; removal wrote nothing more.
    assert saves_before_removal == [1]
    assert manager._store.async_save.await_count == 1
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
    assert sensor._pending == []
    assert sensor._active_since is None
    assert manager._runtime_writers == {}
    assert manager._runtime_unresolved == set()
    for unsubscribe in listeners.values():
        unsubscribe.assert_called_once()
    assert sensor._unsub_source is None
    assert sensor._unsub_checkpoint is None


async def test_unload_without_runtime_writers_proceeds(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _runtime_manager(hass, asset_store_data)
    result, unload_platforms = await _unload(hass, manager)
    assert result is True
    unload_platforms.assert_awaited_once()
    manager._store.async_save.assert_not_awaited()


async def test_inactive_writer_prepares_without_writing(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _runtime_manager(hass, asset_store_data)
    sensor = _sensor(hass, manager, _Clock(10))
    await _add(hass, sensor)
    await manager.async_prepare_runtime_unload()
    manager._store.async_save.assert_not_awaited()
    assert sensor._removing is True


async def test_retry_after_failed_preparation_counts_once(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _runtime_manager(hass, asset_store_data)
    clock = _Clock(0)
    sensor = await _active_sensor(hass, manager, clock, 0.0)
    clock.value = 10
    _failing_save(manager)
    first, first_platforms = await _unload(hass, manager, sensors=(sensor,))
    assert first is False
    first_platforms.assert_not_called()

    _working_save(manager)
    clock.value = 12
    second, second_platforms = await _unload(hass, manager, sensors=(sensor,))

    assert second is True
    second_platforms.assert_awaited_once()
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1012)
    assert manager._runtime_writers == {}


# Races with queued callbacks


async def _queue_behind_prepare(
    manager: AssetStoreManager,
    sensor: DeviceRuntimeHoursSensor,
    callback: Any,
) -> None:
    """Queue a callback that passed its outer check behind a successful prepare."""
    await sensor._runtime_lock.acquire()
    prepare = asyncio.create_task(manager.async_prepare_runtime_unload())
    await asyncio.sleep(0)
    queued = asyncio.create_task(callback)
    await asyncio.sleep(0)
    assert sensor._removing is False
    sensor._runtime_lock.release()
    await prepare
    await queued


async def test_queued_source_change_cannot_create_runtime_after_quiesce(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _runtime_manager(hass, asset_store_data)
    clock = _Clock(0)
    sensor = _sensor(hass, manager, clock)
    await _add(hass, sensor)
    clock.value = 5

    await _queue_behind_prepare(
        manager, sensor, sensor._handle_source_change(_event(STATE_OFF, STATE_ON))
    )

    assert sensor._removing is True
    assert sensor._active_since is None
    clock.value = 500
    await sensor.async_will_remove_from_hass()
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1000)
    manager._store.async_save.assert_not_awaited()


async def test_queued_periodic_checkpoint_cannot_seal_after_quiesce(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _runtime_manager(hass, asset_store_data)
    clock = _Clock(0)
    sensor = await _active_sensor(hass, manager, clock, 0.0)
    clock.value = 10

    await _queue_behind_prepare(
        manager, sensor, sensor._handle_periodic_checkpoint(None)
    )

    assert sensor._pending == []
    clock.value = 99
    await sensor._handle_periodic_checkpoint(None)
    await sensor._handle_source_change(_event(STATE_ON, STATE_OFF))
    await sensor._handle_shutdown(SimpleNamespace())
    assert sensor._pending == []
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
    assert manager._store.async_save.await_count == 1


# Several Runtime writers


async def test_all_writers_prepared_in_asset_order(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _runtime_manager(hass, asset_store_data)
    order: list[str] = []
    for asset_uuid in (ASSET_AFTER, ASSET_BEFORE):

        async def _prepare(asset_uuid: str = asset_uuid) -> None:
            order.append(asset_uuid)

        manager.register_runtime_checkpoint(
            asset_uuid, AsyncMock(), prepare_unload=_prepare
        )
    result, unload_platforms = await _unload(hass, manager)
    assert result is True
    assert order == [ASSET_BEFORE, ASSET_AFTER]
    unload_platforms.assert_awaited_once()


async def test_later_writer_failure_blocks_unload_and_prepared_writer_stays_quiesced(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """Fail closed: the prepared writer is not resumed with guessed Runtime."""
    manager = _runtime_manager(hass, asset_store_data)
    clock = _Clock(0)
    sensor = await _active_sensor(hass, manager, clock, 0.0)
    failing = AsyncMock(
        side_effect=AssetStoreError("disk", code="runtime_checkpoint_failed")
    )
    manager.register_runtime_checkpoint(
        ASSET_AFTER, AsyncMock(), prepare_unload=failing
    )
    clock.value = 10

    result, unload_platforms = await _unload(hass, manager, sensors=(sensor,))

    assert result is False
    unload_platforms.assert_not_called()
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
    assert sensor._removing is True
    with pytest.raises(AssetStoreError) as info:
        await manager.async_checkpoint_runtime(ASSET_UUID)
    assert info.value.code == "runtime_writer_quiesced"

    # A retry prepares the quiesced writer again as a no-op and continues.
    failing.side_effect = None
    clock.value = 50
    retried, retried_platforms = await _unload(hass, manager, sensors=(sensor,))
    assert retried is True
    retried_platforms.assert_awaited_once()
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
    assert failing.await_count == 2


async def test_first_writer_failure_stops_before_later_writers(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _runtime_manager(hass, asset_store_data)
    first = AsyncMock(side_effect=AssetStoreError("disk"))
    later = AsyncMock()
    manager.register_runtime_checkpoint(ASSET_BEFORE, AsyncMock(), prepare_unload=first)
    manager.register_runtime_checkpoint(ASSET_AFTER, AsyncMock(), prepare_unload=later)
    with pytest.raises(AssetStoreError, match=ASSET_BEFORE):
        await manager.async_prepare_runtime_unload()
    later.assert_not_awaited()


# Platform unload failure after preparation


async def test_platform_unload_failure_keeps_writers_quiesced(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed unload is reported; Runtime continuity is not fabricated."""
    manager = _runtime_manager(hass, asset_store_data)
    clock = _Clock(0)
    sensor = await _active_sensor(hass, manager, clock, 0.0)
    clock.value = 10

    with caplog.at_level(logging.ERROR):
        result, _platforms = await _unload(
            hass, manager, sensors=(sensor,), platforms_unloaded=False
        )

    assert result is False
    assert "Runtime tracking is stopped until the entry is reloaded" in caplog.text
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
    clock.value = 100
    await sensor._handle_source_change(_event(STATE_OFF, STATE_ON))
    await sensor._handle_periodic_checkpoint(None)
    assert sensor._active_since is None
    assert sensor._pending == []
    with pytest.raises(AssetStoreError) as info:
        await manager.async_checkpoint_runtime(ASSET_UUID)
    assert info.value.code == "runtime_writer_quiesced"


# Direct removal outside the unload gate


async def test_direct_removal_with_failed_flush_is_unresolved(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """The old total is never presented as a fresh checkpoint afterwards."""
    manager = _runtime_manager(hass, asset_store_data)
    clock = _Clock(0)
    sensor = await _active_sensor(hass, manager, clock, 0.0)
    clock.value = 10
    _failing_save(manager)

    await sensor.async_will_remove_from_hass()

    assert manager._runtime_writers == {}
    assert manager._runtime_unresolved == {ASSET_UUID}
    _working_save(manager)
    with pytest.raises(AssetStoreError) as info:
        await manager.async_checkpoint_runtime(ASSET_UUID)
    assert info.value.code == "runtime_checkpoint_unresolved"

    # Re-adding a writer in the same manager lifetime cannot recover the loss.
    replacement = _sensor(hass, manager, clock)
    await _add(hass, replacement)
    with pytest.raises(AssetStoreError):
        await manager.async_checkpoint_runtime(ASSET_UUID)
    # Unload is not blocked by the already-lost Runtime.
    result, _platforms = await _unload(hass, manager, sensors=(replacement,))
    assert result is True


async def test_direct_removal_with_successful_flush_is_resolved(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _runtime_manager(hass, asset_store_data)
    clock = _Clock(0)
    sensor = await _active_sensor(hass, manager, clock, 0.0)
    clock.value = 10
    await sensor.async_will_remove_from_hass()
    assert manager._runtime_unresolved == set()
    assert await manager.async_checkpoint_runtime(ASSET_UUID) == Decimal(1010)
