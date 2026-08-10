"""Shared fixtures for Device Lifecycle tests."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

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


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading integrations from custom_components."""


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
def asset_store_data(asset_store_data_v1_1: AssetStoreData) -> AssetStoreData:
    """Return the representative payload after migration to schema 2.1."""
    data = deepcopy(asset_store_data_v1_1)
    asset = data["assets"][ASSET_UUID]
    asset[CONF_DEPLOYMENT_STATE] = DEPLOYMENT_STATE_UNKNOWN
    asset[CONF_HA_AREA_ID] = None
    asset["runtime"] = {"total_seconds": None}
    asset["field_sources"]["purchase_uuid"] = "purchase"
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
