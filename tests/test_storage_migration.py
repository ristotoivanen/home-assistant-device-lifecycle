"""Tests for the Device Lifecycle Asset Store 1.1 to 1.2 migration."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant

from custom_components.device_lifecycle.const import (
    CONFIG_ENTRY_VERSION,
    CONF_DEPLOYMENT_STATE,
    CONF_HA_AREA_ID,
    DEPLOYMENT_STATE_UNKNOWN,
)
from custom_components.device_lifecycle.migration import runtime_unique_id
from custom_components.device_lifecycle.storage import (
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    AssetStoreError,
    AssetStoreManager,
    DeviceLifecycleStore,
    _validate_store_data,
)

from .conftest import (
    ASSET_UUID,
    DEVICE_ID,
    PURCHASE_SUBENTRY_ID,
    PURCHASE_UUID,
)


async def test_complete_store_migration_preserves_identity_and_relationships(
    hass: HomeAssistant,
    asset_store_data_v1_1,
    runtime_subentry_data,
) -> None:
    """Migrate a representative 0.5.3 Store without rewriting stable data."""
    store = DeviceLifecycleStore(hass)
    source = deepcopy(asset_store_data_v1_1)
    source_before = deepcopy(source)
    runtime_before = deepcopy(runtime_subentry_data)

    migrated = await store._async_migrate_func(1, 1, source)

    assert source == source_before
    assert migrated["next_asset_number"] == source_before["next_asset_number"] == 8
    assert list(migrated["purchases"]) == list(source_before["purchases"])
    assert migrated["purchases"][PURCHASE_UUID] == source_before["purchases"][
        PURCHASE_UUID
    ]

    migrated_asset = migrated["assets"][ASSET_UUID]
    source_asset = source_before["assets"][ASSET_UUID]
    assert migrated_asset["asset_uuid"] == source_asset["asset_uuid"] == ASSET_UUID
    assert migrated_asset["asset_id"] == source_asset["asset_id"] == "DL0007"
    assert migrated_asset["purchase_uuid"] == PURCHASE_UUID
    assert migrated_asset["ha_device_refs"] == source_asset["ha_device_refs"] == [
        {"device_id": DEVICE_ID, "role": "primary"}
    ]
    assert migrated["purchases"][PURCHASE_UUID]["config_subentry_id"] == (
        PURCHASE_SUBENTRY_ID
    )
    assert migrated["purchases"][PURCHASE_UUID]["asset_uuids"] == [ASSET_UUID]

    assert migrated_asset[CONF_DEPLOYMENT_STATE] == DEPLOYMENT_STATE_UNKNOWN
    assert migrated_asset[CONF_HA_AREA_ID] is None
    assert migrated_asset["field_sources"]["purchase_uuid"] == "purchase"

    assert runtime_subentry_data == runtime_before
    assert runtime_subentry_data["asset_uuid"] == ASSET_UUID
    assert runtime_unique_id(ASSET_UUID) == f"{ASSET_UUID}_runtime_hours"
    _validate_store_data(migrated)


async def test_store_migration_is_idempotent(
    hass: HomeAssistant,
    asset_store_data_v1_1,
) -> None:
    """Repeated migration does not rewrite or duplicate schema fields."""
    store = DeviceLifecycleStore(hass)

    migrated_once = await store._async_migrate_func(
        1,
        1,
        deepcopy(asset_store_data_v1_1),
    )
    migrated_twice = await store._async_migrate_func(
        1,
        1,
        deepcopy(migrated_once),
    )
    current_schema = await store._async_migrate_func(
        1,
        2,
        deepcopy(migrated_once),
    )

    assert migrated_twice == migrated_once
    assert current_schema == migrated_once


async def test_migration_preserves_purchase_with_zero_assets(
    hass: HomeAssistant,
    asset_store_data_v1_1,
) -> None:
    """A zero-Asset Purchase survives migration without allocating identity."""
    asset_store_data_v1_1["assets"] = {}
    asset_store_data_v1_1["purchases"][PURCHASE_UUID]["asset_uuids"] = []
    counter_before = asset_store_data_v1_1["next_asset_number"]
    store = DeviceLifecycleStore(hass)

    migrated = await store._async_migrate_func(1, 1, asset_store_data_v1_1)

    assert migrated["assets"] == {}
    assert migrated["purchases"][PURCHASE_UUID]["asset_uuids"] == []
    assert migrated["next_asset_number"] == counter_before
    _validate_store_data(migrated)


async def test_migration_preserves_existing_user_purchase_provenance(
    hass: HomeAssistant,
    asset_store_data_v1_1,
) -> None:
    """Migration fills only missing Purchase provenance."""
    asset_store_data_v1_1["assets"][ASSET_UUID]["field_sources"][
        "purchase_uuid"
    ] = "user"
    store = DeviceLifecycleStore(hass)

    migrated = await store._async_migrate_func(1, 1, asset_store_data_v1_1)

    assert migrated["assets"][ASSET_UUID]["field_sources"]["purchase_uuid"] == (
        "user"
    )


def test_invalid_deployment_state_is_rejected(asset_store_data) -> None:
    """Schema 1.2 accepts only the three approved deployment states."""
    asset_store_data["assets"][ASSET_UUID][CONF_DEPLOYMENT_STATE] = "stored"

    with pytest.raises(AssetStoreError, match="invalid deployment state"):
        _validate_store_data(asset_store_data)


@pytest.mark.parametrize("invalid_area", [123, True, ""])
def test_invalid_ha_area_type_is_rejected(asset_store_data, invalid_area) -> None:
    """Area relationships must be a non-empty registry ID or null."""
    asset_store_data["assets"][ASSET_UUID][CONF_HA_AREA_ID] = invalid_area

    with pytest.raises(AssetStoreError, match="invalid HA Area ID"):
        _validate_store_data(asset_store_data)


def test_attached_purchase_requires_relationship_provenance(asset_store_data) -> None:
    """Every non-null Purchase relationship declares who manages it."""
    del asset_store_data["assets"][ASSET_UUID]["field_sources"]["purchase_uuid"]

    with pytest.raises(AssetStoreError, match="no Purchase relationship provenance"):
        _validate_store_data(asset_store_data)


def test_purchase_relationship_rejects_home_assistant_provenance(
    asset_store_data,
) -> None:
    """Purchase membership can be managed only by Purchase or user input."""
    asset_store_data["assets"][ASSET_UUID]["field_sources"][
        "purchase_uuid"
    ] = "home_assistant"

    with pytest.raises(
        AssetStoreError,
        match="invalid Purchase relationship provenance",
    ):
        _validate_store_data(asset_store_data)


def test_stage_2_versions_are_scoped_without_manifest_release_bump() -> None:
    """Only the Store minor version changes during Stage 2."""
    manifest_path = (
        Path(__file__).parents[1]
        / "custom_components"
        / "device_lifecycle"
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert STORAGE_VERSION == 1
    assert STORAGE_MINOR_VERSION == 2
    assert CONFIG_ENTRY_VERSION == 4
    assert manifest["version"] == "0.5.3"


async def test_existing_device_purchase_creation_sets_purchase_provenance(
    hass: HomeAssistant,
    purchase_subentry_data,
) -> None:
    """Existing HA-device Purchase creation remains valid under schema 1.2."""
    manager_data = {
        "next_asset_number": 1,
        "purchases": {},
        "assets": {},
    }
    purchase = SimpleNamespace(
        subentry_id=PURCHASE_SUBENTRY_ID,
        subentry_type="purchase",
        title="Workshop equipment",
        data=deepcopy(purchase_subentry_data),
    )
    entry = SimpleNamespace(
        entry_id="device-lifecycle-entry-id",
        subentries={PURCHASE_SUBENTRY_ID: purchase},
    )
    manager = AssetStoreManager(hass)
    manager._data = manager_data
    manager._store.async_save = AsyncMock()

    await manager.async_reconcile_entry(entry)

    assert manager.asset_count == 1
    asset = next(iter(manager._data["assets"].values()))
    assert asset["asset_id"] == "DL0001"
    assert asset["purchase_uuid"] == PURCHASE_UUID
    assert asset["field_sources"]["purchase_uuid"] == "purchase"
    assert asset[CONF_DEPLOYMENT_STATE] == DEPLOYMENT_STATE_UNKNOWN
    assert asset[CONF_HA_AREA_ID] is None
    _validate_store_data(manager._data)
