"""Regression tests for Device Lifecycle 0.5.3 sensor identity and restore data."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.components.sensor import RestoreSensor, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_DEVICE_ID,
    CONF_RUNTIME_MODE,
    CONF_SOURCE_ENTITY_ID,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    RUNTIME_MODE_ON,
    SUBENTRY_TYPE_RUNTIME,
)
from custom_components.device_lifecycle.migration import (
    lifecycle_unique_id,
    runtime_unique_id,
)
from custom_components.device_lifecycle.sensor import (
    DeviceRuntimeHoursSensor,
    async_setup_entry,
)

from .conftest import ASSET_UUID, DEVICE_ID, SOURCE_ENTITY_ID
from .test_options_flow import _manager


def test_asset_owned_unique_ids_are_stable() -> None:
    """Protect the 0.5.3 entity-registry identity contract."""
    assert lifecycle_unique_id(ASSET_UUID) == f"{ASSET_UUID}_lifecycle"
    assert runtime_unique_id(ASSET_UUID) == f"{ASSET_UUID}_runtime_hours"


async def test_runtime_sensor_projects_canonical_total(
    hass: HomeAssistant,
    asset_store_data,
    runtime_subentry_data,
) -> None:
    """Canonical Asset Runtime replaces RestoreSensor ownership."""
    asset = asset_store_data["assets"][ASSET_UUID]
    asset["runtime"]["total_seconds"] = "4624308.00"
    sensor = DeviceRuntimeHoursSensor(
        data=runtime_subentry_data,
        asset=asset,
        device_entry=SimpleNamespace(id=DEVICE_ID),
        unique_id=runtime_unique_id(ASSET_UUID),
        manager=Mock(),
        monotonic=lambda: 0.0,
    )
    sensor.hass = hass

    assert sensor.unique_id == f"{ASSET_UUID}_runtime_hours"
    assert sensor.native_value == Decimal("1284.530000")
    assert isinstance(sensor, SensorEntity)
    assert not isinstance(sensor, RestoreSensor)
    assert sensor.extra_state_attributes["asset_uuid"] == ASSET_UUID
    assert sensor.extra_state_attributes["asset_id"] == "DL0007"
    assert sensor.extra_state_attributes["lahde_entiteetti"] == SOURCE_ENTITY_ID


async def test_related_device_never_becomes_runtime_entity_target(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Entity setup requires the Runtime device to be the Asset primary."""
    external_entry = MockConfigEntry(
        domain="hue",
        title="External owner",
        data={},
    )
    external_entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=external_entry.entry_id,
        identifiers={("hue", "related-runtime")},
        name="Related runtime device",
    )
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Related only")
    await manager.async_add_related_device(asset["asset_uuid"], device.id)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="device_lifecycle_main",
        version=CONFIG_ENTRY_VERSION,
        data={},
        subentries_data=(
            {
                "data": {
                    CONF_ASSET_UUID: asset["asset_uuid"],
                    CONF_DEVICE_ID: device.id,
                    CONF_RUNTIME_MODE: RUNTIME_MODE_ON,
                    CONF_SOURCE_ENTITY_ID: "switch.related_runtime_source",
                },
                "subentry_type": SUBENTRY_TYPE_RUNTIME,
                "title": "Related runtime",
                "unique_id": None,
            },
        ),
    )
    entry.runtime_data = manager
    entry.add_to_hass(hass)
    async_add_entities = Mock()

    await async_setup_entry(hass, entry, async_add_entities)

    async_add_entities.assert_not_called()
