"""Regression tests for the Device Lifecycle 0.5.3 Asset Store."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant

from custom_components.device_lifecycle.storage import (
    AssetStoreManager,
    _validate_store_data,
)

from .conftest import ASSET_UUID, DEVICE_ID, PURCHASE_SUBENTRY_ID, PURCHASE_UUID


def test_existing_store_payload_is_valid_and_unchanged(asset_store_data) -> None:
    """Protect the migrated 0.5.3 payload from reconciliation rewrites."""
    original = deepcopy(asset_store_data)

    _validate_store_data(asset_store_data)

    assert asset_store_data == original
    assert asset_store_data["next_asset_number"] == 8
    assert asset_store_data["assets"][ASSET_UUID]["asset_uuid"] == ASSET_UUID
    assert asset_store_data["assets"][ASSET_UUID]["asset_id"] == "DL0007"
    assert asset_store_data["assets"][ASSET_UUID]["ha_device_refs"] == [
        {"device_id": DEVICE_ID, "role": "primary"}
    ]
    assert asset_store_data["purchases"][PURCHASE_UUID]["asset_uuids"] == [
        ASSET_UUID
    ]


def test_existing_store_validation_allows_purchase_with_zero_assets(
    asset_store_data,
) -> None:
    """Record that the 0.5.3 Store model already accepts an empty Purchase."""
    asset_store_data["assets"] = {}
    asset_store_data["purchases"][PURCHASE_UUID]["asset_uuids"] = []

    _validate_store_data(asset_store_data)

    assert asset_store_data["purchases"][PURCHASE_UUID]["asset_uuids"] == []
    assert asset_store_data["next_asset_number"] == 8


async def test_reconcile_existing_subentries_preserves_asset_core_identity(
    hass: HomeAssistant,
    asset_store_data,
    existing_entry,
) -> None:
    """Protect idempotent setup of existing Purchase and Runtime subentries."""
    manager = AssetStoreManager(hass)
    manager._data = deepcopy(asset_store_data)
    manager._store.async_save = AsyncMock()
    original = deepcopy(manager._data)

    await manager.async_reconcile_entry(existing_entry)

    assert manager._data == original
    assert manager.asset(ASSET_UUID) == original["assets"][ASSET_UUID]
    assert manager.asset_for_device_id(DEVICE_ID) == original["assets"][ASSET_UUID]
    assert manager.purchase(PURCHASE_UUID) == original["purchases"][PURCHASE_UUID]
    assert manager.purchase_for_subentry(PURCHASE_SUBENTRY_ID) == original[
        "purchases"
    ][PURCHASE_UUID]
    manager._store.async_save.assert_not_awaited()


async def test_removed_purchase_subentry_retains_purchase_and_asset_records(
    hass: HomeAssistant,
    asset_store_data,
) -> None:
    """Protect 0.5.3 retention when the active Purchase subentry is removed."""
    manager = AssetStoreManager(hass)
    manager._data = deepcopy(asset_store_data)
    manager._store.async_save = AsyncMock()
    entry = SimpleNamespace(entry_id="device-lifecycle-entry-id", subentries={})

    await manager.async_reconcile_entry(entry)

    asset = manager.asset(ASSET_UUID)
    purchase = manager.purchase(PURCHASE_UUID)
    assert asset is not None
    assert purchase is not None
    assert asset["asset_uuid"] == ASSET_UUID
    assert asset["asset_id"] == "DL0007"
    assert asset["purchase_uuid"] == PURCHASE_UUID
    assert asset["ha_device_refs"] == [
        {"device_id": DEVICE_ID, "role": "primary"}
    ]
    assert purchase["configured"] is False
    assert purchase["asset_uuids"] == [ASSET_UUID]
    assert manager._data["next_asset_number"] == 8
    manager._store.async_save.assert_awaited_once()


def test_store_accessors_return_detached_snapshots(
    hass: HomeAssistant,
    asset_store_data,
) -> None:
    """Protect manager state from mutation through returned Asset snapshots."""
    manager = AssetStoreManager(hass)
    manager._data = deepcopy(asset_store_data)

    asset = manager.asset(ASSET_UUID)
    purchase = manager.purchase(PURCHASE_UUID)
    assert asset is not None
    assert purchase is not None

    asset["name"] = "Changed outside manager"
    purchase["name"] = "Changed outside manager"

    assert manager.asset(ASSET_UUID)["name"] == "Workshop device"
    assert manager.purchase(PURCHASE_UUID)["name"] == "Workshop equipment"
