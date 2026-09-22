"""Shared fixtures for Device Lifecycle tests."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

import pytest
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_CURRENCY,
    CONF_DEPLOYMENT_STATE,
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
    CONF_HA_AREA_ID,
    CONF_INSTALLED_DATE,
    CONF_NOTES,
    CONF_POWER_HYSTERESIS,
    CONF_POWER_THRESHOLD,
    CONF_PURCHASE_DATE,
    CONF_PURCHASE_NAME,
    CONF_PURCHASE_PRICE,
    CONF_PURCHASE_UUID,
    CONF_RECEIPT_REFERENCE,
    CONF_RECEIPT_URL,
    CONF_RUNTIME_MODE,
    CONF_SELLER,
    CONF_SOURCE_ENTITY_ID,
    CONF_WARRANTY_TYPE,
    CONF_WARRANTY_UNTIL,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DEPLOYMENT_STATE_UNKNOWN,
    RUNTIME_MODE_POWER,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
    WARRANTY_TWO_YEARS,
)
from custom_components.device_lifecycle.models import AssetStoreData

pytest_plugins = "pytest_homeassistant_custom_component"

ASSET_UUID = "11111111-1111-4111-8111-111111111111"
PURCHASE_UUID = "22222222-2222-4222-8222-222222222222"
DEVICE_ID = "existing-ha-device-id"
PURCHASE_SUBENTRY_ID = "purchase-subentry-id"
RUNTIME_SUBENTRY_ID = "runtime-subentry-id"
SOURCE_ENTITY_ID = "sensor.workshop_power"
STORE_V1_2_SECOND_ASSET_UUID = "33333333-3333-4333-8333-333333333333"


def device_registry_entries(registry: dr.DeviceRegistry) -> list[dr.DeviceEntry]:
    """Return every Device Registry entry on Home Assistant 2026.8 and 2026.9+.

    `registry.devices` is a device-ID mapping on 2026.8 and a collection of
    `DeviceEntry` values on 2026.9+. Tests use this to count devices
    independently of the production lookup helper under test.
    """
    devices = registry.devices
    if isinstance(devices, Mapping):
        return list(devices.values())
    return list(devices)


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading integrations from custom_components."""


@pytest.fixture(autouse=True)
def schedule_reload(request: pytest.FixtureRequest) -> Iterator[Mock | None]:
    """Record ConfigEntry reload requests without executing a real reload.

    OptionsFlow tests drive a flow directly against a MockConfigEntry whose
    `runtime_data` is a test-owned manager. A real reload sets the
    integration up again and replaces `entry.runtime_data` with a manager
    loaded from the (empty) test Store, racing any later step of the same
    test (0.7.2 CI: `asset_missing` instead of `related_device_is_primary`).

    Both reload mechanisms are intercepted into one recorder: the
    fire-and-forget `async_schedule_reload` and the awaited `async_reload`
    that asset mutations use so the flow can continue afterwards (0.7.4
    WP-G1). Which mechanism a mutation uses is an implementation detail, so
    DS-6 tests assert only that a reload was or was not requested, e.g.
    `schedule_reload.assert_called_once_with(entry_id)` or
    `schedule_reload.assert_not_called()`.

    The intercept keeps the real entry-id contract (`UnknownEntry` for an
    unknown entry), and the awaited form reports the entry as usable without
    running an unload/setup cycle. A test-local `patch.object` on either
    `hass.config_entries` method shadows it, which is how the persistent
    reload-failure path is exercised.

    Tests that intentionally exercise a real integration reload opt out with
    `@pytest.mark.real_reload` (or a module-level `pytestmark`); the fixture
    then yields `None` and Home Assistant's own implementation runs.
    """
    if request.node.get_closest_marker("real_reload") is not None:
        yield None
        return

    recorder = Mock(name="config_entry_reload")

    def _intercepted_schedule_reload(self: ConfigEntries, entry_id: str) -> None:
        self.async_get_known_entry(entry_id)
        recorder(entry_id)

    async def _intercepted_reload(self: ConfigEntries, entry_id: str) -> bool:
        self.async_get_known_entry(entry_id)
        recorder(entry_id)
        return True

    with (
        patch.object(
            ConfigEntries,
            "async_schedule_reload",
            _intercepted_schedule_reload,
        ),
        patch.object(
            ConfigEntries,
            "async_reload",
            _intercepted_reload,
        ),
    ):
        yield recorder


@contextmanager
def capture_reloads(hass: HomeAssistant) -> Iterator[Mock]:
    """Record every ConfigEntry reload request inside one assertion window.

    The shared `schedule_reload` fixture records for the whole test; this is
    the scoped form used where a test asserts what one specific step did.

    Both mechanisms land in the same recorder on purpose: DS-6 is a contract
    about whether a mutation needs a reload, not about which Home Assistant
    call delivers it. Patching only one of them would make an
    `assert_not_called()` pass for a mutation that reloaded through the other.
    """
    recorder = Mock(name="config_entry_reload")

    async def _awaited_reload(entry_id: str) -> bool:
        recorder(entry_id)
        return True

    with (
        patch.object(hass.config_entries, "async_schedule_reload", recorder),
        patch.object(hass.config_entries, "async_reload", _awaited_reload),
    ):
        yield recorder


@pytest.fixture
def asset_store_data_v1_1() -> AssetStoreData:
    """Return a representative valid Device Lifecycle 0.5.3 store payload."""
    return {
        "next_asset_number": 8,
        "purchases": {
            PURCHASE_UUID: {
                "purchase_uuid": PURCHASE_UUID,
                "config_subentry_id": PURCHASE_SUBENTRY_ID,
                "configured": True,
                "name": "Workshop equipment",
                "purchase_date": "2026-01-15",
                "seller": "Example seller",
                "total_price": "349.9",
                "currency": "EUR",
                "receipt_reference": "ORDER-123",
                "receipt_url": "https://example.invalid/receipt/ORDER-123",
                "notes": "Existing purchase notes",
                "asset_uuids": [ASSET_UUID],
            }
        },
        "assets": {
            ASSET_UUID: {
                "asset_uuid": ASSET_UUID,
                "asset_id": "DL0007",
                "name": "Workshop device",
                "category": "Tool",
                "purchase_uuid": PURCHASE_UUID,
                "installed_date": "2026-01-20",
                "warranty": {
                    "type": WARRANTY_TWO_YEARS,
                    "until": "2028-01-15",
                },
                "manufacturer": "Example manufacturer",
                "model": "Example model",
                "model_id": "MODEL-1",
                "serial_number": "SERIAL-1",
                "sw_version": "1.2.3",
                "hw_version": "A",
                "notes": "Existing asset notes",
                "field_sources": {
                    "name": "home_assistant",
                    "manufacturer": "home_assistant",
                    "model": "home_assistant",
                    "model_id": "home_assistant",
                    "serial_number": "home_assistant",
                    "sw_version": "home_assistant",
                    "hw_version": "home_assistant",
                    "installed_date": "purchase",
                    "warranty": "purchase",
                },
                "ha_device_refs": [
                    {
                        "device_id": DEVICE_ID,
                        "role": "primary",
                    }
                ],
            }
        },
    }


@pytest.fixture
def asset_store_data_v1_2(
    asset_store_data_v1_1: AssetStoreData,
) -> AssetStoreData:
    """Return a representative Device Lifecycle 0.5.6 Store 1.2 payload."""
    data = deepcopy(asset_store_data_v1_1)
    asset = data["assets"][ASSET_UUID]
    asset[CONF_DEPLOYMENT_STATE] = DEPLOYMENT_STATE_DEPLOYED
    asset[CONF_HA_AREA_ID] = "workshop-area"
    asset["field_sources"]["purchase_uuid"] = "purchase"
    asset["ha_device_refs"] = [
        {"device_id": DEVICE_ID, "role": "primary"},
        {"device_id": "related-one", "role": "related"},
        {"device_id": "related-two", "role": "related"},
    ]

    second_asset = deepcopy(asset)
    second_asset.update(
        {
            "asset_uuid": STORE_V1_2_SECOND_ASSET_UUID,
            "asset_id": "DL0006",
            "name": "Workshop controller",
            "category": "Controller",
            CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED,
            CONF_HA_AREA_ID: None,
            "installed_date": None,
            "manufacturer": "Second manufacturer",
            "model": "Second model",
            "model_id": "SECOND-1",
            "serial_number": "SECOND-SERIAL",
            "sw_version": "5.6.0",
            "hw_version": "B",
            "notes": "Second historical Asset",
            "ha_device_refs": [
                {"device_id": "second-primary", "role": "primary"},
                {"device_id": "second-related", "role": "related"},
            ],
        }
    )
    data["assets"][STORE_V1_2_SECOND_ASSET_UUID] = second_asset
    data["purchases"][PURCHASE_UUID]["asset_uuids"] = [
        STORE_V1_2_SECOND_ASSET_UUID,
        ASSET_UUID,
    ]

    assert "runtime" not in asset
    assert "lifecycle" not in asset
    assert "lifecycle_events" not in data
    assert "replacement_records" not in data
    return data


@pytest.fixture
def asset_store_data(asset_store_data_v1_1: AssetStoreData) -> AssetStoreData:
    """Return the representative payload after migration to schema 3.1."""
    data = deepcopy(asset_store_data_v1_1)
    asset = data["assets"][ASSET_UUID]
    asset[CONF_DEPLOYMENT_STATE] = DEPLOYMENT_STATE_UNKNOWN
    asset[CONF_HA_AREA_ID] = None
    asset["runtime"] = {"total_seconds": None}
    asset["lifecycle"] = {
        "status": "unknown",
        "current_event_uuid": None,
    }
    asset["field_sources"]["purchase_uuid"] = "purchase"
    data["lifecycle_events"] = {}
    data["replacement_records"] = {}
    return data


@pytest.fixture
def purchase_subentry_data() -> dict[str, Any]:
    """Return the config-subentry projection for the existing Purchase."""
    return {
        CONF_DEVICE_IDS: [DEVICE_ID],
        CONF_PURCHASE_NAME: "Workshop equipment",
        CONF_PURCHASE_DATE: "2026-01-15",
        CONF_INSTALLED_DATE: "2026-01-20",
        CONF_WARRANTY_TYPE: WARRANTY_TWO_YEARS,
        CONF_WARRANTY_UNTIL: "2028-01-15",
        CONF_SELLER: "Example seller",
        CONF_PURCHASE_PRICE: 349.90,
        CONF_CURRENCY: "EUR",
        CONF_RECEIPT_REFERENCE: "ORDER-123",
        CONF_RECEIPT_URL: "https://example.invalid/receipt/ORDER-123",
        CONF_NOTES: "Existing purchase notes",
        CONF_PURCHASE_UUID: PURCHASE_UUID,
    }


@pytest.fixture
def runtime_subentry_data() -> dict[str, Any]:
    """Return an existing Runtime config-subentry projection."""
    return {
        CONF_DEVICE_ID: DEVICE_ID,
        CONF_ASSET_UUID: ASSET_UUID,
        CONF_RUNTIME_MODE: RUNTIME_MODE_POWER,
        CONF_SOURCE_ENTITY_ID: SOURCE_ENTITY_ID,
        CONF_POWER_THRESHOLD: 10.0,
        CONF_POWER_HYSTERESIS: 2.0,
    }


@pytest.fixture
def existing_entry(
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
) -> SimpleNamespace:
    """Return a lightweight parent entry with existing Purchase and Runtime data."""
    purchase = SimpleNamespace(
        subentry_id=PURCHASE_SUBENTRY_ID,
        subentry_type=SUBENTRY_TYPE_PURCHASE,
        title="Workshop equipment",
        data=deepcopy(purchase_subentry_data),
    )
    runtime = SimpleNamespace(
        subentry_id=RUNTIME_SUBENTRY_ID,
        subentry_type=SUBENTRY_TYPE_RUNTIME,
        title="Workshop device",
        data=deepcopy(runtime_subentry_data),
    )
    return SimpleNamespace(
        entry_id="device-lifecycle-entry-id",
        subentries={
            purchase.subentry_id: purchase,
            runtime.subentry_id: runtime,
        },
    )
