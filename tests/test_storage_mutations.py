"""Transactional mutation tests for the Device Lifecycle Asset Store."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, patch
from uuid import UUID

from homeassistant.core import HomeAssistant
import pytest

from custom_components.device_lifecycle.const import (
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
)
from custom_components.device_lifecycle.models import AssetStoreData, PurchaseData
from custom_components.device_lifecycle.storage import (
    AssetStoreError,
    AssetStoreManager,
)

from .conftest import ASSET_UUID, PURCHASE_UUID

MANUAL_ASSET_UUID = "33333333-3333-4333-8333-333333333333"
SECOND_PURCHASE_UUID = "44444444-4444-4444-8444-444444444444"


def _manager(
    hass: HomeAssistant,
    data: AssetStoreData | None = None,
) -> AssetStoreManager:
    """Return a manager with an isolated in-memory snapshot and mocked save."""
    manager = AssetStoreManager(hass)
    if data is not None:
        manager._data = deepcopy(data)
    manager._store.async_save = AsyncMock()
    return manager


def _configured_purchase(purchase_uuid: str) -> PurchaseData:
    """Return a valid configured Purchase with no Assets."""
    return {
        "purchase_uuid": purchase_uuid,
        "config_subentry_id": f"subentry-{purchase_uuid}",
        "configured": True,
        "name": "Second purchase",
        "purchase_date": "2026-08-01",
        "seller": "Example seller",
        "total_price": "99.9",
        "currency": "EUR",
        "receipt_reference": None,
        "receipt_url": None,
        "notes": None,
        "asset_uuids": [],
    }


async def test_create_manual_asset_without_purchase_or_ha_device(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A manual Asset gets stable identity without external relationships."""
    manager = _manager(hass)
    monkeypatch.setattr(
        "custom_components.device_lifecycle.storage.uuid4",
        lambda: UUID(MANUAL_ASSET_UUID),
    )

    asset = await manager.async_create_manual_asset(
        name="Shelf bulb",
        manufacturer="Philips",
        model="Hue White",
        notes="Spare",
    )

    assert asset["asset_uuid"] == MANUAL_ASSET_UUID
    assert asset["asset_id"] == "DL0001"
    assert asset["name"] == "Shelf bulb"
    assert asset["purchase_uuid"] is None
    assert asset["deployment_state"] == DEPLOYMENT_STATE_NOT_DEPLOYED
    assert asset["installed_date"] is None
    assert asset["ha_area_id"] is None
    assert asset["ha_device_refs"] == []
    assert asset["field_sources"] == {
        "name": "user",
        "manufacturer": "user",
        "model": "user",
        "notes": "user",
    }
    assert manager._data["next_asset_number"] == 2
    manager._store.async_save.assert_awaited_once()


async def test_manual_asset_uuid_is_canonical(hass: HomeAssistant) -> None:
    """Manual creation allocates a canonical immutable UUID."""
    manager = _manager(hass)

    asset = await manager.async_create_manual_asset(name="Router")

    assert str(UUID(asset["asset_uuid"])) == asset["asset_uuid"]
    assert manager.asset(asset["asset_uuid"])["asset_uuid"] == asset["asset_uuid"]


async def test_manual_asset_allocation_is_sequential(hass: HomeAssistant) -> None:
    """Manual Assets use the existing monotonic DL allocator."""
    manager = _manager(hass)

    first = await manager.async_create_manual_asset(name="First")
    second = await manager.async_create_manual_asset(name="Second")

    assert [first["asset_id"], second["asset_id"]] == ["DL0001", "DL0002"]
    assert manager._data["next_asset_number"] == 3


async def test_concurrent_creations_are_serialized(hass: HomeAssistant) -> None:
    """Concurrent callers cannot allocate the same DL ID or overlap saves."""
    manager = _manager(hass)
    active_saves = 0
    maximum_active_saves = 0

    async def _save(_data: AssetStoreData) -> None:
        nonlocal active_saves, maximum_active_saves
        active_saves += 1
        maximum_active_saves = max(maximum_active_saves, active_saves)
        await asyncio.sleep(0)
        active_saves -= 1

    manager._store.async_save = AsyncMock(side_effect=_save)

    first, second = await asyncio.gather(
        manager.async_create_manual_asset(name="Concurrent one"),
        manager.async_create_manual_asset(name="Concurrent two"),
    )

    assert {first["asset_id"], second["asset_id"]} == {"DL0001", "DL0002"}
    assert manager._data["next_asset_number"] == 3
    assert maximum_active_saves == 1
    assert manager._store.async_save.await_count == 2


async def test_failed_mutator_leaves_store_unchanged(hass: HomeAssistant) -> None:
    """An exception during mutation cannot publish a partial copy."""
    manager = _manager(hass)
    before = deepcopy(manager._data)

    def _fail(data: AssetStoreData) -> None:
        data["next_asset_number"] = 2
        raise RuntimeError("injected mutation failure")

    with pytest.raises(RuntimeError, match="injected mutation failure"):
        await manager._async_mutate(_fail)

    assert manager._data == before
    manager._store.async_save.assert_not_awaited()


async def test_failed_validation_leaves_store_unchanged(
    hass: HomeAssistant,
) -> None:
    """Complete-store validation runs before save and publish."""
    manager = _manager(hass)
    before = deepcopy(manager._data)

    def _invalidate(data: AssetStoreData) -> None:
        data["next_asset_number"] = 0

    with pytest.raises(AssetStoreError, match="next_asset_number"):
        await manager._async_mutate(_invalidate)

    assert manager._data == before
    manager._store.async_save.assert_not_awaited()


async def test_failed_creation_save_does_not_consume_identity(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed save consumes neither the UUID nor the monotonic DL number."""
    manager = _manager(hass)
    monkeypatch.setattr(
        "custom_components.device_lifecycle.storage.uuid4",
        lambda: UUID(MANUAL_ASSET_UUID),
    )
    manager._store.async_save = AsyncMock(
        side_effect=OSError("injected atomic save failure")
    )
    before = deepcopy(manager._data)

    with pytest.raises(OSError, match="injected atomic save failure"):
        await manager.async_create_manual_asset(name="Failed Asset")

    assert manager._data == before
    assert manager._data["next_asset_number"] == 1
    assert manager.asset(MANUAL_ASSET_UUID) is None

    manager._store.async_save = AsyncMock()
    created = await manager.async_create_manual_asset(name="Retry Asset")
    assert created["asset_uuid"] == MANUAL_ASSET_UUID
    assert created["asset_id"] == "DL0001"
    assert manager._data["next_asset_number"] == 2


async def test_duplicate_generated_uuid_does_not_consume_asset_id(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a UUID collision fails before advancing the monotonic allocator."""
    manager = _manager(hass, asset_store_data)
    monkeypatch.setattr(
        "custom_components.device_lifecycle.storage.uuid4",
        lambda: UUID(ASSET_UUID),
    )
    before = deepcopy(manager._data)

    with pytest.raises(AssetStoreError, match="UUID is already in use"):
        await manager.async_create_manual_asset(name="Collision")

    assert manager._data == before
    assert manager._data["next_asset_number"] == 8
    manager._store.async_save.assert_not_awaited()


async def test_mutation_results_are_detached_snapshots(hass: HomeAssistant) -> None:
    """Changing a returned mutation result cannot change manager state."""
    manager = _manager(hass)

    created = await manager.async_create_manual_asset(name="Detached")
    asset_uuid = created["asset_uuid"]
    created["name"] = "Changed outside manager"
    created["ha_device_refs"].append({"device_id": "external", "role": "primary"})

    stored = manager.asset(asset_uuid)
    assert stored["name"] == "Detached"
    assert stored["ha_device_refs"] == []


async def test_update_asset_metadata_preserves_identity(
    hass: HomeAssistant,
) -> None:
    """User metadata edits cannot replace UUID or DL identity."""
    manager = _manager(hass)
    created = await manager.async_create_manual_asset(
        name="Original",
        serial_number="SERIAL",
    )

    updated = await manager.async_update_asset_metadata(
        created["asset_uuid"],
        name="Updated",
        model="Model 2",
        serial_number=None,
    )

    assert updated["asset_uuid"] == created["asset_uuid"]
    assert updated["asset_id"] == created["asset_id"]
    assert updated["name"] == "Updated"
    assert updated["model"] == "Model 2"
    assert updated["serial_number"] is None
    assert updated["field_sources"]["name"] == "user"
    assert updated["field_sources"]["model"] == "user"
    assert updated["field_sources"]["serial_number"] == "user"


async def test_purchase_assignment_and_clearing_are_atomic(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Purchase assignment updates both relationship directions together."""
    asset_store_data["purchases"][SECOND_PURCHASE_UUID] = _configured_purchase(
        SECOND_PURCHASE_UUID
    )
    manager = _manager(hass, asset_store_data)

    assigned = await manager.async_set_asset_purchase(
        ASSET_UUID,
        SECOND_PURCHASE_UUID,
    )

    assert assigned["purchase_uuid"] == SECOND_PURCHASE_UUID
    assert assigned["field_sources"]["purchase_uuid"] == "user"
    assert manager.purchase(PURCHASE_UUID)["asset_uuids"] == []
    assert manager.purchase(SECOND_PURCHASE_UUID)["asset_uuids"] == [ASSET_UUID]

    cleared = await manager.async_set_asset_purchase(ASSET_UUID, None)

    assert cleared["purchase_uuid"] is None
    assert cleared["field_sources"]["purchase_uuid"] == "user"
    assert manager.purchase(PURCHASE_UUID)["asset_uuids"] == []
    assert manager.purchase(SECOND_PURCHASE_UUID)["asset_uuids"] == []


async def test_failed_purchase_save_leaves_both_sides_unchanged(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A save failure cannot leave half of a Purchase relationship update."""
    asset_store_data["purchases"][SECOND_PURCHASE_UUID] = _configured_purchase(
        SECOND_PURCHASE_UUID
    )
    manager = _manager(hass, asset_store_data)
    manager._store.async_save = AsyncMock(side_effect=OSError("save failed"))
    before = deepcopy(manager._data)

    with pytest.raises(OSError, match="save failed"):
        await manager.async_set_asset_purchase(ASSET_UUID, SECOND_PURCHASE_UUID)

    assert manager._data == before
    assert manager.asset(ASSET_UUID)["purchase_uuid"] == PURCHASE_UUID
    assert manager.purchase(PURCHASE_UUID)["asset_uuids"] == [ASSET_UUID]
    assert manager.purchase(SECOND_PURCHASE_UUID)["asset_uuids"] == []


async def test_new_assignment_rejects_unconfigured_purchase(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """New user-managed relationships target configured Purchases only."""
    historical = _configured_purchase(SECOND_PURCHASE_UUID)
    historical["configured"] = False
    asset_store_data["purchases"][SECOND_PURCHASE_UUID] = historical
    manager = _manager(hass, asset_store_data)
    before = deepcopy(manager._data)

    with pytest.raises(AssetStoreError, match="not currently configured"):
        await manager.async_set_asset_purchase(ASSET_UUID, SECOND_PURCHASE_UUID)

    assert manager._data == before
    manager._store.async_save.assert_not_awaited()


async def test_deployment_mutation_sets_changes_and_clears_fields(
    hass: HomeAssistant,
) -> None:
    """Deployment fields are explicit and independent from Asset identity."""
    manager = _manager(hass)
    created = await manager.async_create_manual_asset(name="Access point")

    deployed = await manager.async_set_asset_deployment(
        created["asset_uuid"],
        deployment_state=DEPLOYMENT_STATE_DEPLOYED,
        installed_date="2026-08-10",
        ha_area_id="office",
    )

    assert deployed["deployment_state"] == DEPLOYMENT_STATE_DEPLOYED
    assert deployed["installed_date"] == "2026-08-10"
    assert deployed["ha_area_id"] == "office"
    assert deployed["field_sources"]["deployment_state"] == "user"
    assert deployed["field_sources"]["installed_date"] == "user"
    assert deployed["field_sources"]["ha_area_id"] == "user"

    not_deployed = await manager.async_set_asset_deployment(
        created["asset_uuid"],
        deployment_state=DEPLOYMENT_STATE_NOT_DEPLOYED,
        ha_area_id=None,
    )

    assert not_deployed["deployment_state"] == DEPLOYMENT_STATE_NOT_DEPLOYED
    assert not_deployed["ha_area_id"] is None
    assert not_deployed["installed_date"] == "2026-08-10"
    assert not_deployed["asset_uuid"] == created["asset_uuid"]
    assert not_deployed["asset_id"] == created["asset_id"]


async def test_invalid_deployment_mutation_is_not_published(
    hass: HomeAssistant,
) -> None:
    """Invalid explicit deployment data fails complete-store validation."""
    manager = _manager(hass)
    created = await manager.async_create_manual_asset(name="Switch")
    manager._store.async_save.reset_mock()
    before = deepcopy(manager._data)

    with pytest.raises(AssetStoreError, match="invalid deployment state"):
        await manager.async_set_asset_deployment(
            created["asset_uuid"],
            deployment_state="invented",
        )

    assert manager._data == before
    manager._store.async_save.assert_not_awaited()


async def test_link_asset_device_changes_only_stored_relationship(
    hass: HomeAssistant,
) -> None:
    """Stage 3 linking never reads or mutates the HA Device Registry."""
    manager = _manager(hass)
    created = await manager.async_create_manual_asset(name="Manual device")
    manager._store.async_save.reset_mock()

    with patch(
        "custom_components.device_lifecycle.storage.dr.async_get"
    ) as registry_get:
        linked = await manager.async_link_asset_device(
            created["asset_uuid"],
            "existing-ha-device",
        )

    registry_get.assert_not_called()
    assert linked["ha_device_refs"] == [
        {"device_id": "existing-ha-device", "role": "primary"}
    ]
    assert manager.asset_for_primary_device_id("existing-ha-device")["asset_uuid"] == created[
        "asset_uuid"
    ]
    manager._store.async_save.assert_awaited_once()


async def test_reconciliation_save_runs_inside_mutation_lock(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Legacy reconciliation uses the same serialized transaction boundary."""
    manager = _manager(hass, asset_store_data)

    async def _save(_data: AssetStoreData) -> None:
        assert manager._mutation_lock.locked()

    manager._store.async_save = AsyncMock(side_effect=_save)
    entry = type(
        "Entry",
        (),
        {"entry_id": "entry", "subentries": {}},
    )()

    await manager.async_reconcile_entry(entry)

    assert manager.purchase(PURCHASE_UUID)["configured"] is False
    manager._store.async_save.assert_awaited_once()
