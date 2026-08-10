"""Sensor parsing and canonical Runtime projection quality gates."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
import pytest

from custom_components.device_lifecycle.sensor import (
    DeviceLifecycleSensor,
    DeviceRuntimeHoursSensor,
    _format_warranty_date,
    _power_value_watts,
    _warranty_type_label,
)
from custom_components.device_lifecycle.storage import AssetStoreError


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (State("sensor.power", "bad", {"unit_of_measurement": "W"}), None),
        (State("sensor.power", "nan", {"unit_of_measurement": "W"}), None),
        (State("sensor.power", "10", {}), None),
        (State("sensor.power", "10", {"unit_of_measurement": "widgets"}), None),
        (
            State(
                "sensor.power",
                "1.5",
                {"unit_of_measurement": UnitOfPower.KILO_WATT},
            ),
            1500.0,
        ),
    ],
)
def test_power_state_conversion_rejects_unsafe_values_and_normalizes_units(
    state: State,
    expected: float | None,
) -> None:
    """Runtime never treats malformed/unknown units as physical watts."""
    assert _power_value_watts(state) == expected


def test_finnish_warranty_labels_and_dates_remain_compatible() -> None:
    """The existing warranty Lifecycle sensor retains its Finnish presentation."""
    assert _warranty_type_label("manual", "fi-FI") == "Manuaalinen"
    assert _warranty_type_label("custom", "fi") == "custom"
    assert _format_warranty_date(date(2026, 8, 10), "fi") == "10.8.2026"


def test_runtime_sensor_requires_canonical_initialized_total(asset_store_data) -> None:
    """An unresolved Runtime migration cannot expose a fabricated zero entity."""
    asset = deepcopy(next(iter(asset_store_data["assets"].values())))
    asset["runtime"]["total_seconds"] = None

    with pytest.raises(AssetStoreError, match="not initialized"):
        DeviceRuntimeHoursSensor(
            data={
                "source_entity_id": "switch.machine",
                "runtime_mode": "on",
            },
            asset=asset,
            device_entry=SimpleNamespace(id="asset-device"),
            unique_id=f"{asset['asset_uuid']}_runtime_hours",
            manager=Mock(),
        )


def test_warranty_lifecycle_sensor_active_expired_and_sparse_attributes(
    hass: HomeAssistant,
    asset_store_data,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The existing warranty sensor retains active/expired English/Finnish behavior."""
    asset = deepcopy(next(iter(asset_store_data["assets"].values())))
    purchase = deepcopy(next(iter(asset_store_data["purchases"].values())))
    purchase["name"] = None
    purchase["seller"] = None
    purchase["notes"] = None
    sensor = DeviceLifecycleSensor(
        asset=asset,
        purchase=purchase,
        device_entry=SimpleNamespace(id="asset-device"),
        unique_id=f"{asset['asset_uuid']}_lifecycle",
        purchase_title=None,
    )
    sensor.hass = hass

    yesterday = dt_util.now().date() - timedelta(days=1)
    asset["warranty"]["until"] = yesterday.isoformat()
    monkeypatch.setattr(hass.config, "language", "en")
    assert sensor.icon == "mdi:calendar-alert"
    assert sensor.native_value.startswith("Expired · 1 day ago")
    assert "ostos" not in sensor.extra_state_attributes

    tomorrow = dt_util.now().date() + timedelta(days=1)
    asset["warranty"]["until"] = tomorrow.isoformat()
    monkeypatch.setattr(hass.config, "language", "fi")
    assert sensor.native_value.startswith("Voimassa · 1 pv")
    asset["warranty"]["until"] = yesterday.isoformat()
    assert sensor.native_value.startswith("Päättynyt · 1 pv sitten")


async def test_runtime_early_exit_and_power_hysteresis_boundaries(
    hass: HomeAssistant,
    asset_store_data,
) -> None:
    """Removal guards, inactive sealing, invalid power, and zero stop threshold are explicit."""
    asset = deepcopy(next(iter(asset_store_data["assets"].values())))
    asset["runtime"]["total_seconds"] = "0"
    sensor = DeviceRuntimeHoursSensor(
        data={
            "source_entity_id": "sensor.power",
            "runtime_mode": "power",
            "power_threshold": 2,
            "power_hysteresis": 3,
        },
        asset=asset,
        device_entry=SimpleNamespace(id="asset-device"),
        unique_id=f"{asset['asset_uuid']}_runtime_hours",
        manager=SimpleNamespace(async_commit_runtime_delta=AsyncMock()),
    )
    sensor.hass = hass
    sensor._removing = True
    await sensor._handle_source_change(None)  # type: ignore[arg-type]
    await sensor._handle_periodic_checkpoint(datetime.now(UTC))

    sensor._removing = False
    sensor._seal_active(10, continue_active=False)
    invalid = State(
        "sensor.power", "bad", {"unit_of_measurement": UnitOfPower.WATT}
    )
    assert not sensor._is_active(invalid, currently_active=True)
    positive = State(
        "sensor.power", "1", {"unit_of_measurement": UnitOfPower.WATT}
    )
    assert sensor._is_active(positive, currently_active=True)
    sensor._runtime_mode = "unknown"
    assert not sensor._is_active(positive, currently_active=False)
