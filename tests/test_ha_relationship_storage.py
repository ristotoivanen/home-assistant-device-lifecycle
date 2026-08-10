"""Store-level tests for primary and related Home Assistant relationships."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
import pytest

from custom_components.device_lifecycle.storage import (
    AssetStoreError,
    _validate_store_data,
)

from .test_options_flow import _manager


async def test_relationship_storage_invariants(hass: HomeAssistant) -> None:
    """Store 1.2 enforces primary exclusivity but keeps related non-exclusive."""
    manager = _manager(hass)
    first = await manager.async_create_manual_asset(name="First")
    second = await manager.async_create_manual_asset(name="Second")
    valid = deepcopy(manager._data)
    valid["assets"][first["asset_uuid"]]["ha_device_refs"] = [
        {"device_id": "primary-first", "role": "primary"},
        {"device_id": "shared-related", "role": "related"},
        {"device_id": "related-first", "role": "related"},
    ]
    valid["assets"][second["asset_uuid"]]["ha_device_refs"] = [
        {"device_id": "primary-first", "role": "related"},
        {"device_id": "shared-related", "role": "related"},
    ]

    _validate_store_data(valid)

    too_many_primary = deepcopy(valid)
    too_many_primary["assets"][first["asset_uuid"]]["ha_device_refs"].append(
        {"device_id": "second-primary", "role": "primary"}
    )
    with pytest.raises(AssetStoreError, match="more than one primary"):
        _validate_store_data(too_many_primary)

    duplicate_local = deepcopy(valid)
    duplicate_local["assets"][first["asset_uuid"]]["ha_device_refs"].append(
        {"device_id": "primary-first", "role": "related"}
    )
    with pytest.raises(AssetStoreError, match="more than once"):
        _validate_store_data(duplicate_local)

    duplicate_related = deepcopy(valid)
    duplicate_related["assets"][first["asset_uuid"]]["ha_device_refs"].append(
        {"device_id": "shared-related", "role": "related"}
    )
    with pytest.raises(AssetStoreError, match="more than once"):
        _validate_store_data(duplicate_related)

    duplicate_primary = deepcopy(valid)
    duplicate_primary["assets"][second["asset_uuid"]]["ha_device_refs"] = [
        {"device_id": "primary-first", "role": "primary"}
    ]
    with pytest.raises(AssetStoreError, match="primary for more than one"):
        _validate_store_data(duplicate_primary)

    unknown_role = deepcopy(valid)
    unknown_role["assets"][second["asset_uuid"]]["ha_device_refs"] = [
        {"device_id": "unknown-role", "role": "controller"}
    ]
    with pytest.raises(AssetStoreError, match="invalid HA reference"):
        _validate_store_data(unknown_role)


async def test_add_related_is_atomic_idempotent_and_does_not_refresh_metadata(
    hass: HomeAssistant,
) -> None:
    """Related additions preserve all identity and metadata fields."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(
        name="Related Asset",
        manufacturer="User manufacturer",
        serial_number="SERIAL",
    )
    await manager.async_link_asset_device(asset["asset_uuid"], "primary")
    before = manager.asset(asset["asset_uuid"])
    manager._store.async_save.reset_mock()

    with patch.object(
        manager,
        "_refresh_home_assistant_metadata",
        side_effect=AssertionError("related add refreshed metadata"),
    ):
        updated = await manager.async_add_related_device(
            asset["asset_uuid"],
            "related",
        )

    expected = deepcopy(before)
    expected["ha_device_refs"].append(
        {"device_id": "related", "role": "related"}
    )
    assert updated == expected
    assert manager.asset(asset["asset_uuid"]) == expected
    manager._store.async_save.assert_awaited_once()

    manager._store.async_save.reset_mock()
    repeated = await manager.async_add_related_device(
        asset["asset_uuid"],
        "related",
    )

    assert repeated == expected
    manager._store.async_save.assert_not_awaited()


async def test_related_is_many_to_many_and_may_be_primary_elsewhere(
    hass: HomeAssistant,
) -> None:
    """Related references have no global uniqueness or identity semantics."""
    manager = _manager(hass)
    first = await manager.async_create_manual_asset(name="First")
    second = await manager.async_create_manual_asset(name="Second")
    await manager.async_link_asset_device(second["asset_uuid"], "shared-device")

    await manager.async_add_related_device(first["asset_uuid"], "shared-device")
    await manager.async_add_related_device(first["asset_uuid"], "many-to-many")
    await manager.async_add_related_device(second["asset_uuid"], "many-to-many")

    assert manager.asset(first["asset_uuid"])["ha_device_refs"] == [
        {"device_id": "shared-device", "role": "related"},
        {"device_id": "many-to-many", "role": "related"},
    ]
    assert manager.asset(second["asset_uuid"])["ha_device_refs"] == [
        {"device_id": "shared-device", "role": "primary"},
        {"device_id": "many-to-many", "role": "related"},
    ]


async def test_add_related_rejects_same_assets_primary(hass: HomeAssistant) -> None:
    """One Asset cannot hold the same device as both primary and related."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Primary")
    await manager.async_link_asset_device(asset["asset_uuid"], "device")
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with pytest.raises(AssetStoreError, match="already primary"):
        await manager.async_add_related_device(asset["asset_uuid"], "device")

    assert manager._data == before
    manager._store.async_save.assert_not_awaited()


async def test_remove_related_preserves_primary_and_other_related_refs(
    hass: HomeAssistant,
) -> None:
    """Exact related removal works even for a stale registry-independent ID."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Remove related")
    await manager.async_link_asset_device(asset["asset_uuid"], "primary")
    await manager.async_add_related_device(asset["asset_uuid"], "stale-related")
    await manager.async_add_related_device(asset["asset_uuid"], "keep-related")
    manager._store.async_save.reset_mock()

    updated = await manager.async_remove_related_device(
        asset["asset_uuid"],
        "stale-related",
    )

    assert updated["ha_device_refs"] == [
        {"device_id": "primary", "role": "primary"},
        {"device_id": "keep-related", "role": "related"},
    ]
    manager._store.async_save.assert_awaited_once()

    manager._store.async_save.reset_mock()
    repeated = await manager.async_remove_related_device(
        asset["asset_uuid"],
        "stale-related",
    )
    assert repeated == updated
    manager._store.async_save.assert_not_awaited()


async def test_failed_related_save_keeps_published_snapshot(
    hass: HomeAssistant,
) -> None:
    """A failed related write cannot publish a partial relationship list."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Save failure")
    before = deepcopy(manager._data)
    manager._store.async_save = AsyncMock(side_effect=OSError("save failed"))

    with pytest.raises(OSError, match="save failed"):
        await manager.async_add_related_device(asset["asset_uuid"], "related")

    assert manager._data == before
    assert manager.asset(asset["asset_uuid"])["ha_device_refs"] == []


async def test_promote_related_to_primary_is_one_atomic_replacement(
    hass: HomeAssistant,
) -> None:
    """Promotion removes the related copy and does not demote the old primary."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Promote")
    await manager.async_link_asset_device(asset["asset_uuid"], "old-primary")
    await manager.async_add_related_device(asset["asset_uuid"], "new-primary")
    await manager.async_add_related_device(asset["asset_uuid"], "keep-related")
    manager._store.async_save.reset_mock()

    updated = await manager.async_link_asset_device(
        asset["asset_uuid"],
        "new-primary",
        replace=True,
        expected_current_device_id="old-primary",
    )

    assert updated["ha_device_refs"] == [
        {"device_id": "new-primary", "role": "primary"},
        {"device_id": "keep-related", "role": "related"},
    ]
    assert not any(
        reference["device_id"] == "old-primary"
        for reference in updated["ha_device_refs"]
    )
    manager._store.async_save.assert_awaited_once()

    unlinked = await manager.async_unlink_asset_device(asset["asset_uuid"])
    assert unlinked["ha_device_refs"] == [
        {"device_id": "keep-related", "role": "related"}
    ]


async def test_primary_lookup_excludes_related_devices(hass: HomeAssistant) -> None:
    """The explicit operational lookup never resolves a related reference."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Lookup")
    await manager.async_link_asset_device(asset["asset_uuid"], "primary")
    await manager.async_add_related_device(asset["asset_uuid"], "related")

    assert manager.asset_for_primary_device_id("primary")["asset_uuid"] == asset[
        "asset_uuid"
    ]
    assert manager.asset_for_primary_device_id("related") is None


@pytest.mark.parametrize("device_id", ["", "   ", None])
async def test_related_mutations_require_a_device_id(
    hass: HomeAssistant,
    device_id: str | None,
) -> None:
    """Manager relationship mutations reject empty registry identifiers."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Validation")

    with pytest.raises(AssetStoreError, match="device ID is required"):
        await manager.async_add_related_device(asset["asset_uuid"], device_id)
    with pytest.raises(AssetStoreError, match="device ID is required"):
        await manager.async_remove_related_device(asset["asset_uuid"], device_id)
