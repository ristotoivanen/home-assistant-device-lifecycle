"""On-demand durable Runtime checkpoint owned by the Runtime domain."""

from __future__ import annotations

import ast
import asyncio
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, State

from custom_components.device_lifecycle import sensor as sensor_module
from custom_components.device_lifecycle import storage as storage_module
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.sensor import DeviceRuntimeHoursSensor
from custom_components.device_lifecycle.storage import (
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    STORE_TOP_LEVEL_KEYS,
    AssetStoreError,
    AssetStoreManager,
)

from .conftest import ASSET_UUID, SOURCE_ENTITY_ID
from .test_runtime import _Clock, _manager, _sensor

MISSING_ASSET = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa9"


def _runtime_manager(
    hass: HomeAssistant,
    data: AssetStoreData,
    total: str | None = "1000",
) -> AssetStoreManager:
    data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = total
    return _manager(hass, data)


def _listener_patches() -> Any:
    """Patch Home Assistant listener registration for direct entity lifecycle."""
    return (
        patch.object(
            sensor_module, "async_track_state_change_event", return_value=lambda: None
        ),
        patch.object(
            sensor_module, "async_track_time_interval", return_value=lambda: None
        ),
    )


async def _add(hass: HomeAssistant, sensor: DeviceRuntimeHoursSensor) -> None:
    state_patch, interval_patch = _listener_patches()
    with (
        state_patch,
        interval_patch,
        patch.object(type(hass.bus), "async_listen_once", return_value=lambda: None),
    ):
        await sensor.async_added_to_hass()


def _writers(manager: AssetStoreManager) -> dict[str, DeviceRuntimeHoursSensor]:
    """Map each registered Asset to the entity whose methods are registered."""
    writers = {}
    for asset_uuid, writer in manager._runtime_writers.items():
        entity = writer.checkpoint.__self__
        assert writer.checkpoint == entity.async_checkpoint_runtime
        assert writer.prepare_unload == entity.async_prepare_runtime_unload
        writers[asset_uuid] = entity
    return writers


def _record_commits(manager: AssetStoreManager) -> list[tuple[Decimal, Decimal]]:
    """Record every delta commit in order while keeping the real pipeline."""
    commits: list[tuple[Decimal, Decimal]] = []
    original = manager.async_commit_runtime_delta

    async def _commit(
        asset_uuid: str, *, expected_total: Decimal, delta: Decimal
    ) -> Decimal:
        commits.append((expected_total, delta))
        return await original(asset_uuid, expected_total=expected_total, delta=delta)

    manager.async_commit_runtime_delta = _commit  # type: ignore[method-assign]
    return commits


# Registration


async def test_entity_lifecycle_registers_and_unregisters_checkpoint(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _runtime_manager(hass, asset_store_data)
    sensor = _sensor(hass, manager, _Clock())
    assert manager._runtime_writers == {}

    await _add(hass, sensor)
    assert _writers(manager) == {ASSET_UUID: sensor}

    await sensor.async_will_remove_from_hass()
    assert manager._runtime_writers == {}
    assert sensor._unsub_checkpoint is None


async def test_duplicate_writer_registration_fails_closed(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """A second writer is refused before it registers any listener."""
    manager = _runtime_manager(hass, asset_store_data)
    first = _sensor(hass, manager, _Clock())
    await _add(hass, first)
    second = _sensor(hass, manager, _Clock())

    state_patch, interval_patch = _listener_patches()
    with (
        state_patch as track_state,
        interval_patch as track_interval,
        pytest.raises(AssetStoreError) as info,
    ):
        await second.async_added_to_hass()
    assert info.value.code == "runtime_checkpoint_writer_exists"
    track_state.assert_not_called()
    track_interval.assert_not_called()
    assert _writers(manager) == {ASSET_UUID: first}


async def test_stale_unregister_never_removes_a_newer_writer(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _runtime_manager(hass, asset_store_data)
    first = AsyncMock()
    second = AsyncMock()
    unregister_first = manager.register_runtime_checkpoint(
        ASSET_UUID, first, prepare_unload=first
    )
    unregister_first()
    unregister_second = manager.register_runtime_checkpoint(
        ASSET_UUID, second, prepare_unload=second
    )
    unregister_first()
    assert manager._runtime_writers[ASSET_UUID].checkpoint is second
    unregister_second()
    unregister_second()
    assert manager._runtime_writers == {}


async def test_reload_leaves_no_stale_writer(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """After removal a recreated entity registers again; the old one is gone."""
    manager = _runtime_manager(hass, asset_store_data)
    old = _sensor(hass, manager, _Clock())
    await _add(hass, old)
    await old.async_will_remove_from_hass()

    new = _sensor(hass, manager, _Clock())
    await _add(hass, new)
    assert _writers(manager) == {ASSET_UUID: new}
    # A fresh manager, as created by an entry reload, starts empty.
    assert AssetStoreManager(hass)._runtime_writers == {}


# No active writer


@pytest.mark.parametrize(
    ("total", "expected"), [(None, None), ("1000.5", Decimal("1000.5"))]
)
async def test_no_writer_returns_canonical_total_without_writing(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    total: str | None,
    expected: Decimal | None,
) -> None:
    """No writer means no uncommitted Runtime; nothing is fabricated or written."""
    manager = _runtime_manager(hass, asset_store_data, total)
    assert await manager.async_checkpoint_runtime(ASSET_UUID) == expected
    manager._store.async_save.assert_not_awaited()
    assert manager.runtime_total_seconds(ASSET_UUID) == expected


async def test_checkpoint_of_missing_asset_fails(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _runtime_manager(hass, asset_store_data)
    with pytest.raises(AssetStoreError, match="does not exist"):
        await manager.async_checkpoint_runtime(MISSING_ASSET)


# Active checkpoint


async def test_active_checkpoints_seal_persist_and_keep_tracking(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """Canonical 1000, active since 50: checkpoints at 60 and 65 give 1010, 1015."""
    manager = _runtime_manager(hass, asset_store_data)
    clock = _Clock(50)
    sensor = _sensor(hass, manager, clock)
    await _add(hass, sensor)
    sensor._active_since = 50.0
    commits = _record_commits(manager)

    clock.value = 60
    first = await manager.async_checkpoint_runtime(ASSET_UUID)
    assert first == Decimal(1010)
    assert sensor._active_since == 60
    clock.value = 65
    second = await manager.async_checkpoint_runtime(ASSET_UUID)

    assert second == Decimal(1015)
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1015)
    assert manager._data["assets"][ASSET_UUID]["runtime"] == {"total_seconds": "1015.0"}
    assert commits == [
        (Decimal(1000), Decimal("10.0")),
        (Decimal("1010.0"), Decimal("5.0")),
    ]
    assert sensor._active_since == 65
    assert sensor._pending == []
    clock.value = 65 + 3600
    assert sensor.native_value == Decimal("1.281944")


async def test_inactive_checkpoint_writes_nothing(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _runtime_manager(hass, asset_store_data)
    sensor = _sensor(hass, manager, _Clock(10))
    await _add(hass, sensor)
    sensor.async_write_ha_state.reset_mock()

    assert await manager.async_checkpoint_runtime(ASSET_UUID) == Decimal(1000)
    manager._store.async_save.assert_not_awaited()
    sensor.async_write_ha_state.assert_called_once()


async def test_existing_pending_flushes_before_new_sealed_delta(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """A retained +10 is committed first, then the newly sealed +5."""
    manager = _runtime_manager(hass, asset_store_data)
    clock = _Clock(0)
    sensor = _sensor(hass, manager, clock)
    await _add(hass, sensor)
    sensor._active_since = 0.0
    manager._store.async_save = AsyncMock(side_effect=OSError("disk unavailable"))
    clock.value = 10
    await sensor._handle_periodic_checkpoint(None)
    assert [(item.expected_total, item.delta) for item in sensor._pending] == [
        (Decimal(1000), Decimal("10.0"))
    ]

    manager._store.async_save = AsyncMock()
    commits = _record_commits(manager)
    clock.value = 15
    assert await manager.async_checkpoint_runtime(ASSET_UUID) == Decimal(1015)
    assert commits == [
        (Decimal(1000), Decimal("10.0")),
        (Decimal("1010.0"), Decimal("5.0")),
    ]


# Strict failure and retry


async def test_explicit_failure_raises_and_retains_every_pending_delta(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _runtime_manager(hass, asset_store_data)
    clock = _Clock(0)
    sensor = _sensor(hass, manager, clock)
    await _add(hass, sensor)
    sensor._active_since = 0.0
    manager._store.async_save = AsyncMock(side_effect=OSError("disk unavailable"))
    clock.value = 10
    await sensor._handle_periodic_checkpoint(None)
    clock.value = 15

    with pytest.raises(AssetStoreError) as info:
        await manager.async_checkpoint_runtime(ASSET_UUID)
    assert info.value.code == "runtime_checkpoint_failed"
    assert isinstance(info.value.__cause__, OSError)
    assert [(item.expected_total, item.delta) for item in sensor._pending] == [
        (Decimal(1000), Decimal("10.0")),
        (Decimal("1010.0"), Decimal("5.0")),
    ]
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1000)
    assert sensor._active_since == 15
    # The display still includes the retained Runtime.
    assert sensor.native_value == Decimal("0.281944")

    manager._store.async_save = AsyncMock()
    clock.value = 20
    assert await manager.async_checkpoint_runtime(ASSET_UUID) == Decimal(1020)
    assert sensor._pending == []


async def test_explicit_unexpected_total_raises(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _runtime_manager(hass, asset_store_data)
    clock = _Clock(0)
    sensor = _sensor(hass, manager, clock)
    await _add(hass, sensor)
    sensor._active_since = 0.0
    clock.value = 10
    manager.async_commit_runtime_delta = AsyncMock(return_value=Decimal(999))  # type: ignore[method-assign]

    with pytest.raises(AssetStoreError) as info:
        await sensor.async_checkpoint_runtime()
    assert info.value.code == "runtime_checkpoint_failed"
    assert len(sensor._pending) == 1


async def test_background_unexpected_total_stays_best_effort(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _runtime_manager(hass, asset_store_data)
    clock = _Clock(0)
    sensor = _sensor(hass, manager, clock)
    sensor._active_since = 0.0
    clock.value = 10
    manager.async_commit_runtime_delta = AsyncMock(return_value=Decimal(999))  # type: ignore[method-assign]

    await sensor._handle_periodic_checkpoint(None)
    assert len(sensor._pending) == 1


async def test_background_failure_stays_best_effort(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """Periodic, transition, and shutdown checkpoints still log and retry."""
    manager = _runtime_manager(hass, asset_store_data)
    clock = _Clock(0)
    sensor = _sensor(hass, manager, clock)
    sensor._active_since = 0.0
    manager._store.async_save = AsyncMock(side_effect=OSError("disk unavailable"))
    clock.value = 10
    await sensor._handle_periodic_checkpoint(None)
    clock.value = 12
    await sensor._handle_source_change(
        SimpleNamespace(
            data={
                "old_state": State(SOURCE_ENTITY_ID, STATE_ON),
                "new_state": State(SOURCE_ENTITY_ID, STATE_OFF),
            }
        )
    )
    await sensor._handle_shutdown(SimpleNamespace())
    assert sum(item.delta for item in sensor._pending) == Decimal("12.0")

    manager._store.async_save = AsyncMock()
    await sensor._handle_periodic_checkpoint(None)
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal("1012.0")


# Concurrency and removal


async def test_concurrent_explicit_checkpoints_serialize(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """The second request seals only time that elapsed after the first."""
    manager = _runtime_manager(hass, asset_store_data)
    clock = _Clock(0)
    sensor = _sensor(hass, manager, clock)
    await _add(hass, sensor)
    sensor._active_since = 0.0
    commits = _record_commits(manager)
    original_save = manager._store.async_save

    async def _slow_save(data: AssetStoreData) -> None:
        await asyncio.sleep(0)
        clock.advance(3)
        await original_save(data)

    manager._store.async_save = AsyncMock(side_effect=_slow_save)
    clock.value = 10

    first, second = await asyncio.gather(
        manager.async_checkpoint_runtime(ASSET_UUID),
        manager.async_checkpoint_runtime(ASSET_UUID),
    )

    assert commits == [
        (Decimal(1000), Decimal("10.0")),
        (Decimal("1010.0"), Decimal("3.0")),
    ]
    assert first == Decimal(1010)
    assert second == Decimal(1013)
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1013)


async def test_removal_racing_a_checkpoint_counts_once_and_leaves_no_writer(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _runtime_manager(hass, asset_store_data)
    clock = _Clock(0)
    sensor = _sensor(hass, manager, clock)
    await _add(hass, sensor)
    sensor._active_since = 0.0
    clock.value = 7
    original_save = manager._store.async_save

    async def _yielding_save(data: AssetStoreData) -> None:
        await asyncio.sleep(0)
        await original_save(data)

    manager._store.async_save = AsyncMock(side_effect=_yielding_save)

    results = await asyncio.gather(
        manager.async_checkpoint_runtime(ASSET_UUID),
        sensor.async_will_remove_from_hass(),
        manager.async_checkpoint_runtime(ASSET_UUID),
        return_exceptions=True,
    )

    assert results[0] == Decimal(1007)
    # A request queued behind the removal is refused, not answered with a
    # total the removed writer no longer observes.
    assert isinstance(results[2], AssetStoreError)
    assert results[2].code == "runtime_writer_quiesced"
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1007)
    assert manager._runtime_writers == {}
    assert sensor._active_since is None
    clock.value = 100
    sensor.async_checkpoint_runtime = AsyncMock()  # type: ignore[method-assign]
    assert await manager.async_checkpoint_runtime(ASSET_UUID) == Decimal(1007)
    sensor.async_checkpoint_runtime.assert_not_awaited()


# Lock ordering


async def test_manager_checkpoint_never_holds_mutation_lock_around_callback(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """Runtime lock first, Store mutation lock only inside each commit."""
    manager = _runtime_manager(hass, asset_store_data)
    observed: list[bool] = []

    async def _checkpoint() -> None:
        observed.append(manager._mutation_lock.locked())

    manager.register_runtime_checkpoint(
        ASSET_UUID, _checkpoint, prepare_unload=_checkpoint
    )
    await manager.async_checkpoint_runtime(ASSET_UUID)
    await manager.async_prepare_runtime_unload()
    assert observed == [False, False]

    source = ast.parse(Path(storage_module.__file__).read_text(encoding="utf-8"))
    for name in ("async_checkpoint_runtime", "async_prepare_runtime_unload"):
        method = next(
            node
            for node in ast.walk(source)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name
        )
        body = ast.Module(body=method.body, type_ignores=[])
        attributes = {
            node.attr for node in ast.walk(body) if isinstance(node, ast.Attribute)
        }
        assert "_mutation_lock" not in attributes, name


async def test_sensor_checkpoint_takes_runtime_lock_before_store_lock(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _runtime_manager(hass, asset_store_data)
    clock = _Clock(0)
    sensor = _sensor(hass, manager, clock)
    await _add(hass, sensor)
    sensor._active_since = 0.0
    clock.value = 5
    order: list[tuple[bool, bool]] = []
    original_save = manager._store.async_save

    async def _save(data: AssetStoreData) -> None:
        order.append((sensor._runtime_lock.locked(), manager._mutation_lock.locked()))
        await original_save(data)

    manager._store.async_save = AsyncMock(side_effect=_save)
    await manager.async_checkpoint_runtime(ASSET_UUID)
    assert order == [(True, True)]


# Boundaries


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
    return names


def test_runtime_domain_has_no_maintenance_imports() -> None:
    for module in (sensor_module, storage_module):
        imports = _imports(Path(module.__file__))
        assert not any("maintenance" in name for name in imports), module.__name__


def test_store_3_1_shape_and_version_unchanged(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    assert (STORAGE_VERSION, STORAGE_MINOR_VERSION) == (3, 1)
    assert (
        frozenset(
            {
                "next_asset_number",
                "purchases",
                "assets",
                "lifecycle_events",
                "replacement_records",
            }
        )
        == STORE_TOP_LEVEL_KEYS
    )
    manager = _runtime_manager(hass, asset_store_data)
    assert set(manager._data) == STORE_TOP_LEVEL_KEYS
    assert set(manager._data["assets"][ASSET_UUID]["runtime"]) == {"total_seconds"}
