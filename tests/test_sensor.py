"""Regression tests for Device Lifecycle 0.5.3 sensor identity and restore data."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.sensor import RestoreSensor
from homeassistant.core import HomeAssistant

from custom_components.device_lifecycle.migration import (
    lifecycle_unique_id,
    runtime_unique_id,
)
from custom_components.device_lifecycle.sensor import DeviceRuntimeHoursSensor

from .conftest import ASSET_UUID, DEVICE_ID, SOURCE_ENTITY_ID


def test_asset_owned_unique_ids_are_stable() -> None:
    """Protect the 0.5.3 entity-registry identity contract."""
    assert lifecycle_unique_id(ASSET_UUID) == f"{ASSET_UUID}_lifecycle"
    assert runtime_unique_id(ASSET_UUID) == f"{ASSET_UUID}_runtime_hours"


async def test_runtime_sensor_restores_existing_total(
    hass: HomeAssistant,
    asset_store_data,
    runtime_subentry_data,
) -> None:
    """Protect restored Runtime totals and their Asset-owned unique ID."""
    asset = asset_store_data["assets"][ASSET_UUID]
    sensor = DeviceRuntimeHoursSensor(
        data=runtime_subentry_data,
        asset=asset,
        device_entry=SimpleNamespace(id=DEVICE_ID),
        unique_id=runtime_unique_id(ASSET_UUID),
    )
    sensor.hass = hass
    restored = SimpleNamespace(native_value="1284.53")

    with (
        patch.object(
            RestoreSensor,
            "async_added_to_hass",
            new=AsyncMock(),
        ),
        patch.object(
            sensor,
            "async_get_last_sensor_data",
            new=AsyncMock(return_value=restored),
        ),
        patch.object(sensor, "async_on_remove", new=Mock()),
        patch.object(sensor, "async_write_ha_state", new=Mock()),
        patch(
            "custom_components.device_lifecycle.sensor.async_track_state_change_event",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.device_lifecycle.sensor.async_track_time_interval",
            return_value=lambda: None,
        ),
    ):
        await sensor.async_added_to_hass()

    assert sensor.unique_id == f"{ASSET_UUID}_runtime_hours"
    assert sensor.native_value == Decimal("1284.530000")
    assert sensor.extra_state_attributes["asset_uuid"] == ASSET_UUID
    assert sensor.extra_state_attributes["asset_id"] == "DL0007"
    assert sensor.extra_state_attributes["lahde_entiteetti"] == SOURCE_ENTITY_ID
