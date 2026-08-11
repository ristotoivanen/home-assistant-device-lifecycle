"""Canonical Asset Runtime migration, checkpoint, and durability tests."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from decimal import Decimal
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components.sensor import SensorExtraStoredData
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store, UnsupportedStorageVersionError
from homeassistant.util.file import WriteError
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_DEVICE_ID,
    CONF_POWER_HYSTERESIS,
    CONF_POWER_THRESHOLD,
    CONF_RUNTIME_DATA_VERSION,
    CONF_RUNTIME_MODE,
    CONF_SOURCE_ENTITY_ID,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    RUNTIME_DATA_VERSION,
    RUNTIME_MODE_ON,
    RUNTIME_MODE_POWER,
    SUBENTRY_TYPE_RUNTIME,
)
from custom_components.device_lifecycle.exposure import asset_device_identifier
from custom_components.device_lifecycle.migration import runtime_unique_id
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.sensor import (
    DeviceRuntimeHoursSensor,
    _legacy_restore_runtime_seconds,
    async_setup_entry,
)
from custom_components.device_lifecycle.storage import (
    STORAGE_KEY,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    AssetStoreError,
    AssetStoreManager,
    AssetStorePersistenceError,
    DeviceLifecycleStore,
    _validate_store_data,
)

from .conftest import ASSET_UUID, DEVICE_ID, SOURCE_ENTITY_ID

_ORIGINAL_ASYNC_WRITE_DATA = Store._async_write_data


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Use an isolated real config path for Runtime durability tests."""
    return hass_tmp_config_dir


class _Clock:
    """Controllable monotonic clock."""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _manager(
    hass: HomeAssistant,
    data: AssetStoreData,
) -> AssetStoreManager:
    """Return a manager with detached data and an acknowledged mock save."""
    manager = AssetStoreManager(hass)
    manager._data = deepcopy(data)
    manager._store.async_save = AsyncMock()
    return manager


def _runtime_entry(
    *,
    device_id: str = DEVICE_ID,
    marker: bool = False,
    duplicate: bool = False,
) -> MockConfigEntry:
    """Return a parent entry with one or two Runtime subentries."""
    data: dict[str, Any] = {
        CONF_DEVICE_ID: device_id,
        CONF_ASSET_UUID: ASSET_UUID,
        CONF_RUNTIME_MODE: RUNTIME_MODE_ON,
        CONF_SOURCE_ENTITY_ID: SOURCE_ENTITY_ID,
    }
    if marker:
        data[CONF_RUNTIME_DATA_VERSION] = RUNTIME_DATA_VERSION
    subentries: list[dict[str, Any]] = [
        {
            "data": data,
            "subentry_type": SUBENTRY_TYPE_RUNTIME,
            "title": "Workshop Runtime",
            "unique_id": None,
        }
    ]
    if duplicate:
        subentries.append(
            {
                "data": dict(data),
                "subentry_type": SUBENTRY_TYPE_RUNTIME,
                "title": "Duplicate Runtime",
                "unique_id": None,
            }
        )
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="device_lifecycle_main",
        version=CONFIG_ENTRY_VERSION,
        data={},
        subentries_data=tuple(subentries),
    )


def _register_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> dr.DeviceEntry:
    """Register the external target device without Device Lifecycle ownership."""
    external_entry = MockConfigEntry(domain="hue", title="External", data={})
    external_entry.add_to_hass(hass)
    return device_registry.async_get_or_create(
        config_entry_id=external_entry.entry_id,
        identifiers={("hue", "runtime-target")},
        name="Workshop device",
    )


def _register_asset_device(
    entry: ConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> dr.DeviceEntry:
    """Register the projection guaranteed before the sensor platform setup."""
    return device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=None,
        identifiers={asset_device_identifier(ASSET_UUID)},
        name="Workshop Asset",
    )


def _register_runtime_entity(
    entry: ConfigEntry,
    entity_registry: er.EntityRegistry,
    device: dr.DeviceEntry,
) -> str:
    """Create the existing stable Runtime registry identity."""
    subentry = next(iter(entry.subentries.values()))
    registry_entry = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        runtime_unique_id(ASSET_UUID),
        suggested_object_id="workshop_runtime_hours",
        config_entry=entry,
        config_subentry_id=subentry.subentry_id,
        device_id=device.id,
    )
    return registry_entry.entity_id


def _mock_native_restore(
    hass: HomeAssistant,
    entity_id: str,
    value: Decimal | int | str,
    unit: str | None = UnitOfTime.HOURS,
) -> None:
    """Install native RestoreSensor data for one existing entity ID."""
    state = State(
        entity_id,
        str(value),
        {"unit_of_measurement": unit} if unit is not None else {},
    )
    extra = SensorExtraStoredData(value, unit).as_dict()
    mock_restore_cache_with_extra_data(hass, [(state, extra)])


def _sensor(
    hass: HomeAssistant,
    manager: AssetStoreManager,
    clock: _Clock,
    *,
    mode: str = RUNTIME_MODE_ON,
    threshold: float = 10.0,
    hysteresis: float = 2.0,
) -> DeviceRuntimeHoursSensor:
    """Return a Runtime entity projected from manager canonical data."""
    asset = manager.asset(ASSET_UUID)
    assert asset is not None
    data: dict[str, Any] = {
        CONF_DEVICE_ID: DEVICE_ID,
        CONF_ASSET_UUID: ASSET_UUID,
        CONF_RUNTIME_MODE: mode,
        CONF_SOURCE_ENTITY_ID: SOURCE_ENTITY_ID,
        CONF_RUNTIME_DATA_VERSION: RUNTIME_DATA_VERSION,
    }
    if mode == RUNTIME_MODE_POWER:
        data[CONF_POWER_THRESHOLD] = threshold
        data[CONF_POWER_HYSTERESIS] = hysteresis
    sensor = DeviceRuntimeHoursSensor(
        data=data,
        asset=asset,
        device_entry=SimpleNamespace(id=DEVICE_ID),
        unique_id=runtime_unique_id(ASSET_UUID),
        manager=manager,
        monotonic=clock,
    )
    sensor.hass = hass
    sensor.async_write_ha_state = Mock()
    return sensor


async def test_legacy_restore_import_is_exact_and_once(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """0.5.6 native hours import once without changing entity identity."""
    manager = _manager(hass, asset_store_data)
    device = _register_device(hass, device_registry)
    entry = _runtime_entry(device_id=device.id)
    entry.runtime_data = manager
    entry.add_to_hass(hass)
    _register_asset_device(entry, device_registry)
    # Runtime fixtures refer to a stable test device ID; mirror that relation.
    manager._data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": device.id, "role": "primary"}
    ]
    original_entity_id = _register_runtime_entity(
        entry,
        entity_registry,
        device,
    )
    _mock_native_restore(hass, original_entity_id, Decimal("1284.53"))
    add_entities = Mock()

    await async_setup_entry(hass, entry, add_entities)

    assert manager.asset(ASSET_UUID)["runtime"]["total_seconds"] == "4624308.00"
    assert entity_registry.async_get(original_entity_id).entity_id == original_entity_id
    assert entity_registry.async_get(original_entity_id).unique_id == runtime_unique_id(
        ASSET_UUID
    )
    entity = add_entities.call_args.args[0][0]
    assert entity.unique_id == runtime_unique_id(ASSET_UUID)

    manager._store.async_save.reset_mock()
    _mock_native_restore(hass, original_entity_id, Decimal(9999))
    await async_setup_entry(hass, entry, Mock())

    assert manager.asset(ASSET_UUID)["runtime"]["total_seconds"] == "4624308.00"
    manager._store.async_save.assert_not_awaited()


async def test_legacy_restore_import_survives_missing_primary_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """A stale primary device cannot erase or reset legacy Runtime."""
    manager = _manager(hass, asset_store_data)
    device = _register_device(hass, device_registry)
    manager._data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": device.id, "role": "primary"}
    ]
    original_refs = deepcopy(
        manager._data["assets"][ASSET_UUID]["ha_device_refs"]
    )
    entry = _runtime_entry(device_id=device.id)
    entry.runtime_data = manager
    entry.add_to_hass(hass)
    asset_device = _register_asset_device(entry, device_registry)
    entity_id = _register_runtime_entity(entry, entity_registry, device)
    _mock_native_restore(hass, entity_id, Decimal("1284.53"))
    initialize_runtime = AsyncMock(
        wraps=manager.async_initialize_new_runtime,
    )
    manager.async_initialize_new_runtime = initialize_runtime
    missing_device_add = Mock()

    with patch.object(device_registry, "async_get", return_value=None):
        await async_setup_entry(hass, entry, missing_device_add)

    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal("4624308.00")
    initialize_runtime.assert_not_awaited()
    assert missing_device_add.call_count == 2
    registry_entry = entity_registry.async_get(entity_id)
    assert registry_entry is not None
    assert registry_entry.entity_id == entity_id
    assert registry_entry.unique_id == runtime_unique_id(ASSET_UUID)
    assert registry_entry.device_id == device.id
    assert manager.asset(ASSET_UUID)["ha_device_refs"] == original_refs
    manager._store.async_save.assert_awaited_once()

    manager._store.async_save.reset_mock()
    _mock_native_restore(hass, entity_id, Decimal(9999))
    available_device_add = Mock()
    await async_setup_entry(hass, entry, available_device_add)

    manager._store.async_save.assert_not_awaited()
    assert available_device_add.call_count == 2
    projected = available_device_add.call_args.args[0][0]
    assert projected.native_value == Decimal("1284.530000")
    assert projected.device_entry.id == asset_device.id
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal("4624308.00")
    assert entity_registry.async_get(entity_id).entity_id == entity_id
    assert manager.asset(ASSET_UUID)["ha_device_refs"] == original_refs


@pytest.mark.parametrize("restore_value", [None, "not-a-number"])
async def test_missing_primary_device_preserves_unresolved_legacy_runtime(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
    restore_value: str | None,
) -> None:
    """Missing or invalid restore data stays null without losing identity."""
    manager = _manager(hass, asset_store_data)
    device = _register_device(hass, device_registry)
    manager._data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": device.id, "role": "primary"}
    ]
    original_refs = deepcopy(
        manager._data["assets"][ASSET_UUID]["ha_device_refs"]
    )
    entry = _runtime_entry(device_id=device.id)
    entry.runtime_data = manager
    entry.add_to_hass(hass)
    _register_asset_device(entry, device_registry)
    entity_id = _register_runtime_entity(entry, entity_registry, device)
    if restore_value is not None:
        _mock_native_restore(hass, entity_id, restore_value)
    initialize_runtime = AsyncMock(
        wraps=manager.async_initialize_new_runtime,
    )
    manager.async_initialize_new_runtime = initialize_runtime
    add_entities = Mock()

    with patch.object(device_registry, "async_get", return_value=None):
        await async_setup_entry(hass, entry, add_entities)

    assert manager.runtime_total_seconds(ASSET_UUID) is None
    initialize_runtime.assert_not_awaited()
    add_entities.assert_called_once()
    assert not any(
        isinstance(entity, DeviceRuntimeHoursSensor)
        for entity in add_entities.call_args.args[0]
    )
    registry_entry = entity_registry.async_get(entity_id)
    assert registry_entry is not None
    assert registry_entry.entity_id == entity_id
    assert registry_entry.unique_id == runtime_unique_id(ASSET_UUID)
    assert registry_entry.device_id == device.id
    assert manager.asset(ASSET_UUID)["ha_device_refs"] == original_refs
    manager._store.async_save.assert_not_awaited()


async def test_marked_runtime_initializes_with_missing_primary_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """A marked Runtime projects through the Asset despite a stale external device."""
    manager = _manager(hass, asset_store_data)
    device = _register_device(hass, device_registry)
    manager._data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": device.id, "role": "primary"}
    ]
    entry = _runtime_entry(device_id=device.id, marker=True)
    entry.runtime_data = manager
    entry.add_to_hass(hass)
    _register_asset_device(entry, device_registry)
    entity_id = _register_runtime_entity(entry, entity_registry, device)
    add_entities = Mock()

    with patch.object(device_registry, "async_get", return_value=None):
        await async_setup_entry(hass, entry, add_entities)

    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(0)
    manager._store.async_save.assert_awaited_once()
    assert add_entities.call_count == 2
    assert isinstance(add_entities.call_args.args[0][0], DeviceRuntimeHoursSensor)
    assert entity_registry.async_get(entity_id).entity_id == entity_id
    assert manager.asset(ASSET_UUID)["ha_device_refs"] == [
        {"device_id": device.id, "role": "primary"}
    ]


async def test_canonical_total_always_wins_over_restore(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A non-null canonical total cannot be overwritten by legacy input."""
    asset_store_data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "12.5"
    manager = _manager(hass, asset_store_data)

    result = await manager.async_import_legacy_runtime(
        ASSET_UUID,
        Decimal(9000),
    )

    assert result == Decimal("12.5")
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal("12.5")
    manager._store.async_save.assert_not_awaited()


@pytest.mark.parametrize(
    ("value", "unit"),
    [
        ("not-a-number", UnitOfTime.HOURS),
        ("-1", UnitOfTime.HOURS),
        ("NaN", UnitOfTime.HOURS),
        ("Infinity", UnitOfTime.HOURS),
        ("1", UnitOfTime.SECONDS),
        ("1", None),
    ],
)
def test_invalid_legacy_native_restore_is_rejected(
    hass: HomeAssistant,
    value: str,
    unit: str | None,
) -> None:
    """Malformed, negative, non-finite, and unsafe-unit imports fail closed."""
    entity_id = "sensor.workshop_runtime_hours"
    _mock_native_restore(hass, entity_id, value, unit)

    with pytest.raises(AssetStoreError):
        _legacy_restore_runtime_seconds(hass, entity_id)


def test_missing_legacy_restore_is_rejected(hass: HomeAssistant) -> None:
    """No native restore data never implies a zero Runtime total."""
    with pytest.raises(AssetStoreError, match="missing"):
        _legacy_restore_runtime_seconds(hass, "sensor.missing_runtime")


async def test_missing_restore_keeps_canonical_null_and_registry_identity(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """A pending legacy migration adds no zero entity and removes no identity."""
    manager = _manager(hass, asset_store_data)
    device = _register_device(hass, device_registry)
    manager._data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": device.id, "role": "primary"}
    ]
    entry = _runtime_entry(device_id=device.id)
    entry.runtime_data = manager
    entry.add_to_hass(hass)
    _register_asset_device(entry, device_registry)
    entity_id = _register_runtime_entity(entry, entity_registry, device)
    add_entities = Mock()

    await async_setup_entry(hass, entry, add_entities)

    assert manager.runtime_total_seconds(ASSET_UUID) is None
    add_entities.assert_called_once()
    assert not any(
        isinstance(entity, DeviceRuntimeHoursSensor)
        for entity in add_entities.call_args.args[0]
    )
    assert entity_registry.async_get(entity_id) is not None
    manager._store.async_save.assert_not_awaited()


async def test_failed_import_is_retryable_without_zero_or_entity_replacement(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """A failed canonical write leaves null and retries from the same entity ID."""
    manager = _manager(hass, asset_store_data)
    manager._store.async_save = AsyncMock(side_effect=OSError("disk unavailable"))
    device = _register_device(hass, device_registry)
    entry = _runtime_entry(device_id=device.id)
    entry.runtime_data = manager
    entry.add_to_hass(hass)
    _register_asset_device(entry, device_registry)
    manager._data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": device.id, "role": "primary"}
    ]
    entity_id = _register_runtime_entity(entry, entity_registry, device)
    _mock_native_restore(hass, entity_id, "2.5")
    first_add = Mock()

    await async_setup_entry(hass, entry, first_add)

    assert manager.runtime_total_seconds(ASSET_UUID) is None
    first_add.assert_called_once()
    assert entity_registry.async_get(entity_id) is not None

    manager._store.async_save = AsyncMock()
    second_add = Mock()
    await async_setup_entry(hass, entry, second_add)

    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal("9000.0")
    assert second_add.call_count == 2
    assert entity_registry.async_get(entity_id).entity_id == entity_id


async def test_new_runtime_marker_initializes_zero_and_recreation_continues(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Only a marked new Runtime may initialize null to zero."""
    manager = _manager(hass, asset_store_data)

    initialized = await manager.async_initialize_new_runtime(ASSET_UUID)
    await manager.async_commit_runtime_delta(
        ASSET_UUID,
        expected_total=initialized,
        delta=Decimal(60),
    )
    recreated = await manager.async_initialize_new_runtime(ASSET_UUID)

    assert initialized == Decimal(0)
    assert recreated == Decimal(60)
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(60)


async def test_marked_runtime_setup_initializes_zero_and_removal_keeps_total(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """New setup may create zero, while subentry removal never erases history."""
    manager = _manager(hass, asset_store_data)
    device = _register_device(hass, device_registry)
    manager._data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": device.id, "role": "primary"}
    ]
    entry = _runtime_entry(device_id=device.id, marker=True)
    entry.runtime_data = manager
    entry.add_to_hass(hass)
    _register_asset_device(entry, device_registry)
    add_entities = Mock()

    await async_setup_entry(hass, entry, add_entities)

    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(0)
    assert add_entities.call_count == 2
    await manager.async_commit_runtime_delta(
        ASSET_UUID,
        expected_total=Decimal(0),
        delta=Decimal(77),
    )

    removed_entry = SimpleNamespace(
        entry_id=entry.entry_id,
        subentries={},
        runtime_data=manager,
    )
    await async_setup_entry(hass, removed_entry, Mock())

    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(77)


async def test_runtime_delta_commit_is_idempotent_and_never_decreases(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Expected-plus-delta retries succeed, while any other total fails."""
    asset_store_data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "100"
    manager = _manager(hass, asset_store_data)

    first = await manager.async_commit_runtime_delta(
        ASSET_UUID,
        expected_total=Decimal(100),
        delta=Decimal(25),
    )
    repeated = await manager.async_commit_runtime_delta(
        ASSET_UUID,
        expected_total=Decimal(100),
        delta=Decimal(25),
    )

    assert first == repeated == Decimal(125)
    with pytest.raises(AssetStoreError, match="changed unexpectedly"):
        await manager.async_commit_runtime_delta(
            ASSET_UUID,
            expected_total=Decimal(90),
            delta=Decimal(1),
        )
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(125)


async def test_on_start_stop_and_periodic_checkpoint_count_once(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """ON transitions and the five-minute checkpoint persist exact elapsed once."""
    asset_store_data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "0"
    manager = _manager(hass, asset_store_data)
    clock = _Clock()
    sensor = _sensor(hass, manager, clock)

    await sensor._handle_source_change(
        SimpleNamespace(
            data={
                "old_state": State(SOURCE_ENTITY_ID, STATE_OFF),
                "new_state": State(SOURCE_ENTITY_ID, STATE_ON),
            }
        )
    )
    clock.advance(300)
    await sensor._handle_periodic_checkpoint(None)
    clock.advance(20)
    await sensor._handle_source_change(
        SimpleNamespace(
            data={
                "old_state": State(SOURCE_ENTITY_ID, STATE_ON),
                "new_state": State(SOURCE_ENTITY_ID, STATE_OFF),
            }
        )
    )

    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal("320.0")
    assert sensor.native_value == Decimal("0.088889")
    assert sensor._pending == []


@pytest.mark.parametrize("state_value", [STATE_UNKNOWN, STATE_UNAVAILABLE])
async def test_unknown_and_unavailable_seal_active_elapsed(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    state_value: str,
) -> None:
    """Existing unavailable/unknown semantics stop and checkpoint Runtime."""
    asset_store_data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "0"
    manager = _manager(hass, asset_store_data)
    clock = _Clock()
    sensor = _sensor(hass, manager, clock)
    sensor._active_since = clock()
    clock.advance(15)

    await sensor._handle_source_change(
        SimpleNamespace(
            data={
                "old_state": State(SOURCE_ENTITY_ID, STATE_ON),
                "new_state": State(SOURCE_ENTITY_ID, state_value),
            }
        )
    )

    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal("15.0")
    assert sensor._active_since is None


def test_power_threshold_hysteresis_semantics_are_unchanged(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """POWER start remains strict-above and active stop remains hysteretic."""
    asset_store_data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "0"
    sensor = _sensor(
        hass,
        _manager(hass, asset_store_data),
        _Clock(),
        mode=RUNTIME_MODE_POWER,
    )

    def power(value: str) -> State:
        return State(
            SOURCE_ENTITY_ID,
            value,
            {"unit_of_measurement": UnitOfPower.WATT},
        )

    assert not sensor._is_active(power("10"), currently_active=False)
    assert sensor._is_active(power("10.1"), currently_active=False)
    assert sensor._is_active(power("8"), currently_active=True)
    assert not sensor._is_active(power("7.9"), currently_active=True)


async def test_save_failure_keeps_sealed_pending_and_retry_commits_once(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Stopping seals elapsed even when persistence fails, then inactive retries."""
    asset_store_data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "0"
    manager = _manager(hass, asset_store_data)
    clock = _Clock()
    sensor = _sensor(hass, manager, clock)
    sensor._active_since = clock()
    clock.advance(30)
    manager._store.async_save = AsyncMock(side_effect=OSError("disk unavailable"))

    await sensor._handle_source_change(
        SimpleNamespace(
            data={
                "old_state": State(SOURCE_ENTITY_ID, STATE_ON),
                "new_state": State(SOURCE_ENTITY_ID, STATE_OFF),
            }
        )
    )

    assert sensor._active_since is None
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(0)
    assert sum(item.delta for item in sensor._pending) == Decimal("30.0")
    assert sensor.native_value == Decimal("0.008333")

    manager._store.async_save = AsyncMock()
    await sensor._handle_periodic_checkpoint(None)
    await sensor._handle_periodic_checkpoint(None)

    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal("30.0")
    assert sensor._pending == []


async def test_periodic_and_stop_race_and_concurrent_checkpoints_do_not_duplicate(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """The entity lock serializes periodic/transition and duplicate callbacks."""
    asset_store_data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "0"
    manager = _manager(hass, asset_store_data)
    clock = _Clock()
    sensor = _sensor(hass, manager, clock)
    sensor._active_since = clock()
    clock.advance(300)
    stop_event = SimpleNamespace(
        data={
            "old_state": State(SOURCE_ENTITY_ID, STATE_ON),
            "new_state": State(SOURCE_ENTITY_ID, STATE_OFF),
        }
    )

    await asyncio.gather(
        sensor._handle_periodic_checkpoint(None),
        sensor._handle_source_change(stop_event),
        sensor._handle_periodic_checkpoint(None),
    )

    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal("300.0")
    assert sensor._pending == []


async def test_ambiguous_acknowledgement_recovers_idempotently(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A write that landed before acknowledgement cannot be added twice."""
    asset_store_data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "0"
    manager = _manager(hass, asset_store_data)
    persisted = deepcopy(manager._data)
    persisted["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "10"
    manager._store.async_save = AsyncMock(
        side_effect=AssetStorePersistenceError(
            "acknowledgement lost",
            ambiguous=True,
        )
    )
    manager._store.async_load_persisted_snapshot = AsyncMock(
        return_value=persisted
    )

    with pytest.raises(AssetStorePersistenceError):
        await manager.async_commit_runtime_delta(
            ASSET_UUID,
            expected_total=Decimal(0),
            delta=Decimal(10),
        )

    manager._store.async_save = AsyncMock()
    recovered = await manager.async_commit_runtime_delta(
        ASSET_UUID,
        expected_total=Decimal(0),
        delta=Decimal(10),
    )

    assert recovered == Decimal(10)
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(10)
    manager._store.async_save.assert_not_awaited()


async def test_restart_downtime_not_counted_and_active_starts_new_interval(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Restart projects canonical data and starts active timing only at startup."""
    asset_store_data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "3600"
    manager = _manager(hass, asset_store_data)
    clock = _Clock(10_000)
    hass.states.async_set(SOURCE_ENTITY_ID, STATE_ON)
    sensor = _sensor(hass, manager, clock)

    with (
        patch(
            "custom_components.device_lifecycle.sensor.async_track_state_change_event",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.device_lifecycle.sensor.async_track_time_interval",
            return_value=lambda: None,
        ),
            patch.object(
                type(hass.bus),
                "async_listen_once",
                return_value=lambda: None,
        ),
    ):
        await sensor.async_added_to_hass()

    assert sensor.native_value == Decimal("1.000000")
    clock.advance(10)
    assert sensor.native_value == Decimal("1.002778")


async def test_normal_unload_checkpoints_before_listener_cleanup(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Entity removal seals and persists active elapsed before unsubscribing."""
    asset_store_data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "0"
    manager = _manager(hass, asset_store_data)
    clock = _Clock()
    sensor = _sensor(hass, manager, clock)
    cleanup_order: list[str] = []
    sensor._unsub_source = lambda: cleanup_order.append("source")
    sensor._unsub_interval = lambda: cleanup_order.append("interval")
    sensor._unsub_shutdown = lambda: cleanup_order.append("shutdown")
    sensor._active_since = clock()
    clock.advance(42)

    await sensor.async_will_remove_from_hass()

    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal("42.0")
    assert cleanup_order == ["source", "interval", "shutdown"]


async def test_normal_shutdown_checkpoints_active_elapsed(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """The normal stop listener uses the same serialized final checkpoint."""
    asset_store_data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "5"
    manager = _manager(hass, asset_store_data)
    clock = _Clock()
    sensor = _sensor(hass, manager, clock)
    sensor._active_since = clock()
    clock.advance(25)

    await sensor._handle_shutdown(SimpleNamespace())

    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal("30.0")
    assert sensor._active_since is None


async def test_hard_crash_loses_only_post_checkpoint_window_when_healthy(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A healthy checkpoint bounds an abrupt crash to the next interval window."""
    asset_store_data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "0"
    manager = _manager(hass, asset_store_data)
    clock = _Clock()
    sensor = _sensor(hass, manager, clock)
    sensor._active_since = clock()
    clock.advance(300)
    await sensor._handle_periodic_checkpoint(None)
    clock.advance(299)

    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal("300.0")
    assert sensor.native_value == Decimal("0.166389")
    restarted = _sensor(hass, manager, _Clock(50_000))
    assert restarted.native_value == Decimal("0.083333")


async def test_duplicate_runtime_writers_are_prevented(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """Only one entity writer is added for duplicate Asset Runtime subentries."""
    asset_store_data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "0"
    manager = _manager(hass, asset_store_data)
    device = _register_device(hass, device_registry)
    entry = _runtime_entry(
        device_id=device.id,
        marker=True,
        duplicate=True,
    )
    entry.runtime_data = manager
    entry.add_to_hass(hass)
    _register_asset_device(entry, device_registry)
    manager._data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": device.id, "role": "primary"}
    ]
    add_entities = Mock()

    await async_setup_entry(hass, entry, add_entities)

    assert add_entities.call_count == 2
    assert len(add_entities.call_args.args[0]) == 1


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"total_seconds": 1},
        {"total_seconds": "bad"},
        {"total_seconds": "-1"},
        {"total_seconds": "NaN"},
        {"total_seconds": "Infinity"},
        {"total_seconds": None, "extra": None},
    ],
)
def test_runtime_store_validation_rejects_corruption(
    asset_store_data: AssetStoreData,
    invalid: dict[str, Any],
) -> None:
    """Every v2 Asset requires one null or finite non-negative decimal string."""
    asset_store_data["assets"][ASSET_UUID]["runtime"] = invalid

    with pytest.raises(AssetStoreError, match="Runtime"):
        _validate_store_data(asset_store_data)


async def test_store_1_1_and_1_2_migrate_to_detached_v2_1(
    hass: HomeAssistant,
    asset_store_data_v1_1: AssetStoreData,
) -> None:
    """Both supported v1 schemas preserve identity and add null Runtime."""
    store = DeviceLifecycleStore(hass)
    source_1_1 = deepcopy(asset_store_data_v1_1)
    before_1_1 = deepcopy(source_1_1)
    migrated_1_1 = await store._async_migrate_func(1, 1, source_1_1)

    source_1_2 = deepcopy(migrated_1_1)
    del source_1_2["assets"][ASSET_UUID]["runtime"]
    before_1_2 = deepcopy(source_1_2)
    migrated_1_2 = await store._async_migrate_func(1, 2, source_1_2)

    assert source_1_1 == before_1_1
    assert source_1_2 == before_1_2
    for migrated, original in (
        (migrated_1_1, before_1_1),
        (migrated_1_2, before_1_2),
    ):
        assert migrated["next_asset_number"] == original["next_asset_number"]
        assert migrated["purchases"] == original["purchases"]
        assert migrated["assets"][ASSET_UUID]["asset_uuid"] == ASSET_UUID
        assert migrated["assets"][ASSET_UUID]["asset_id"] == "DL0007"
        assert migrated["assets"][ASSET_UUID]["ha_device_refs"] == original[
            "assets"
        ][ASSET_UUID]["ha_device_refs"]
        assert migrated["assets"][ASSET_UUID]["runtime"] == {
            "total_seconds": None
        }


async def test_real_store_writeerror_is_detected_and_not_published(
    hass: HomeAssistant,
) -> None:
    """Catch HA Store's logged/swallowed atomic file WriteError by read-back."""
    manager = AssetStoreManager(hass)
    before = deepcopy(manager._data)

    manager._store._async_write_data = MethodType(
        _ORIGINAL_ASYNC_WRITE_DATA,
        manager._store,
    )

    def _fail_write(*_args: Any, **_kwargs: Any) -> None:
        raise WriteError("injected underlying write failure")

    with (
        patch(
            "homeassistant.helpers.storage.write_utf8_file_atomic",
            side_effect=_fail_write,
        ),
        pytest.raises(AssetStorePersistenceError),
    ):
        await manager.async_create_manual_asset(name="Must not publish")

    assert manager._data == before
    assert manager.asset_count == 0

    created = await manager.async_create_manual_asset(name="Verified retry")
    assert created["asset_id"] == "DL0001"
    assert manager.asset_count == 1


async def test_v2_downgrade_is_rejected_without_rewrite(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A v1 reader rejects v2 and returning to v2 retains exact Runtime."""
    monkeypatch.setattr(
        "custom_components.device_lifecycle.storage.json_util.load_json",
        lambda _path: deepcopy(hass_storage.get(STORAGE_KEY, {})),
    )
    manager = AssetStoreManager(hass)
    asset = await manager.async_create_manual_asset(name="Downgrade test")
    await manager.async_initialize_new_runtime(asset["asset_uuid"])
    await manager.async_commit_runtime_delta(
        asset["asset_uuid"],
        expected_total=Decimal(0),
        delta=Decimal("1234.567"),
    )
    before = deepcopy(hass_storage[STORAGE_KEY])

    old_reader = Store[AssetStoreData](
        hass,
        1,
        STORAGE_KEY,
        private=True,
        atomic_writes=True,
        minor_version=2,
    )
    with pytest.raises(UnsupportedStorageVersionError):
        await old_reader.async_load()

    after = deepcopy(hass_storage[STORAGE_KEY])
    assert after == before

    returning = AssetStoreManager(hass)
    await returning.async_setup()
    assert returning.runtime_total_seconds(asset["asset_uuid"]) == Decimal(
        "1234.567"
    )
    assert STORAGE_VERSION == 3
    assert STORAGE_MINOR_VERSION == 1


async def test_future_v2_minor_is_rejected_without_rewrite(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    asset_store_data: AssetStoreData,
) -> None:
    """A future same-major Store fails closed instead of HA fallback."""
    hass_storage[STORAGE_KEY] = {
        "version": 2,
        "minor_version": 2,
        "key": STORAGE_KEY,
        "data": {
            **deepcopy(asset_store_data),
            "future_minor_only": {"opaque": True},
        },
    }
    before = deepcopy(hass_storage[STORAGE_KEY])
    monkeypatch.setattr(
        "custom_components.device_lifecycle.storage.json_util.load_json",
        lambda _path: deepcopy(hass_storage.get(STORAGE_KEY, {})),
    )
    store = DeviceLifecycleStore(hass)
    store.async_save = AsyncMock(wraps=store.async_save)

    with pytest.raises(
        AssetStoreError,
        match=r"Unsupported Asset Core Store version 2\.2; expected one of",
    ):
        await store.async_load()

    store.async_save.assert_not_awaited()
    assert hass_storage[STORAGE_KEY] == before
