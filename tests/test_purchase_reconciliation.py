"""Purchase provenance and purchase-first lifecycle tests."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from homeassistant.core import HomeAssistant
import pytest

from custom_components.device_lifecycle.const import (
    CONF_CURRENCY,
    CONF_DEVICE_IDS,
    CONF_NOTES,
    CONF_PURCHASE_DATE,
    CONF_PURCHASE_NAME,
    CONF_PURCHASE_PRICE,
    CONF_PURCHASE_UUID,
    CONF_RECEIPT_REFERENCE,
    CONF_RECEIPT_URL,
    CONF_SELLER,
    SUBENTRY_TYPE_PURCHASE,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    AssetStoreError,
    AssetStoreManager,
)

from .conftest import (
    ASSET_UUID,
    DEVICE_ID,
    PURCHASE_SUBENTRY_ID,
    PURCHASE_UUID,
)

MANUAL_ASSET_UUID = "33333333-3333-4333-8333-333333333333"
SECOND_PURCHASE_UUID = "44444444-4444-4444-8444-444444444444"
SECOND_PURCHASE_SUBENTRY_ID = "purchase-subentry-second"


def _manager(hass: HomeAssistant, data: AssetStoreData) -> AssetStoreManager:
    """Return a manager with detached data and a mocked persistent save."""
    manager = AssetStoreManager(hass)
    manager._data = deepcopy(data)
    manager._store.async_save = AsyncMock()
    return manager


def _purchase_subentry(
    data: dict,
    *,
    subentry_id: str = PURCHASE_SUBENTRY_ID,
) -> SimpleNamespace:
    """Return one lightweight Purchase config subentry."""
    return SimpleNamespace(
        subentry_id=subentry_id,
        subentry_type=SUBENTRY_TYPE_PURCHASE,
        title=str(data.get(CONF_PURCHASE_NAME) or "Purchase"),
        data=deepcopy(data),
    )


def _entry(*subentries: SimpleNamespace) -> SimpleNamespace:
    """Return one lightweight parent config entry."""
    return SimpleNamespace(
        entry_id="device-lifecycle-entry-id",
        subentries={subentry.subentry_id: subentry for subentry in subentries},
    )


def _manual_purchase_data(asset_store_data: AssetStoreData) -> AssetStoreData:
    """Remove the legacy Asset while preserving its configured Purchase."""
    data = deepcopy(asset_store_data)
    data["assets"] = {}
    data["purchases"][PURCHASE_UUID]["asset_uuids"] = []
    return data


async def test_legacy_purchase_membership_removes_and_restores_normally(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict,
) -> None:
    """Device-selected membership retains the existing 0.5.3 behavior."""
    manager = _manager(hass, asset_store_data)
    raw = deepcopy(purchase_subentry_data)
    raw[CONF_DEVICE_IDS] = []
    subentry = _purchase_subentry(raw)
    entry = _entry(subentry)

    await manager.async_reconcile_entry(entry)

    removed = manager.asset(ASSET_UUID)
    assert removed["purchase_uuid"] is None
    assert removed["field_sources"]["purchase_uuid"] == "purchase"
    assert manager.purchase(PURCHASE_UUID)["asset_uuids"] == []
    assert manager._data["next_asset_number"] == 8

    subentry.data[CONF_DEVICE_IDS] = [DEVICE_ID]
    await manager.async_reconcile_entry(entry)

    restored = manager.asset(ASSET_UUID)
    assert restored["asset_id"] == "DL0007"
    assert restored["purchase_uuid"] == PURCHASE_UUID
    assert restored["field_sources"]["purchase_uuid"] == "purchase"
    assert manager.purchase(PURCHASE_UUID)["asset_uuids"] == [ASSET_UUID]
    assert manager._data["next_asset_number"] == 8


async def test_user_managed_no_device_asset_survives_idempotent_reconciliation(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user-owned Purchase link survives reload with no HA relationship."""
    manager = _manager(hass, _manual_purchase_data(asset_store_data))
    monkeypatch.setattr(
        "custom_components.device_lifecycle.storage.uuid4",
        lambda: UUID(MANUAL_ASSET_UUID),
    )
    manual = await manager.async_create_manual_asset(name="Shelf spare")
    await manager.async_set_asset_purchase(manual["asset_uuid"], PURCHASE_UUID)
    raw = deepcopy(purchase_subentry_data)
    raw[CONF_DEVICE_IDS] = []
    entry = _entry(_purchase_subentry(raw))
    manager._store.async_save.reset_mock()

    await manager.async_reconcile_entry(entry)
    first_snapshot = deepcopy(manager._data)
    await manager.async_reconcile_entry(entry)

    asset = manager.asset(MANUAL_ASSET_UUID)
    assert asset["purchase_uuid"] == PURCHASE_UUID
    assert asset["field_sources"]["purchase_uuid"] == "user"
    assert asset["ha_device_refs"] == []
    assert manager.purchase(PURCHASE_UUID)["asset_uuids"] == [MANUAL_ASSET_UUID]
    assert manager._data == first_snapshot
    manager._store.async_save.assert_not_awaited()


async def test_user_managed_asset_survives_reconciliation_after_ha_link(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adding an HA relationship does not transfer Purchase ownership."""
    manager = _manager(hass, _manual_purchase_data(asset_store_data))
    monkeypatch.setattr(
        "custom_components.device_lifecycle.storage.uuid4",
        lambda: UUID(MANUAL_ASSET_UUID),
    )
    manual = await manager.async_create_manual_asset(name="Later linked")
    await manager.async_set_asset_purchase(manual["asset_uuid"], PURCHASE_UUID)
    await manager.async_link_asset_device(manual["asset_uuid"], DEVICE_ID)
    raw = deepcopy(purchase_subentry_data)
    raw[CONF_DEVICE_IDS] = [DEVICE_ID]

    await manager.async_reconcile_entry(_entry(_purchase_subentry(raw)))

    asset = manager.asset(MANUAL_ASSET_UUID)
    assert asset["purchase_uuid"] == PURCHASE_UUID
    assert asset["field_sources"]["purchase_uuid"] == "user"
    assert asset["ha_device_refs"] == [
        {"device_id": DEVICE_ID, "role": "primary"}
    ]
    assert manager.purchase(PURCHASE_UUID)["asset_uuids"] == [MANUAL_ASSET_UUID]


async def test_cleared_user_purchase_relationship_stays_bidirectionally_clear(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit clearing updates both sides and is not undone on reload."""
    manager = _manager(hass, _manual_purchase_data(asset_store_data))
    monkeypatch.setattr(
        "custom_components.device_lifecycle.storage.uuid4",
        lambda: UUID(MANUAL_ASSET_UUID),
    )
    manual = await manager.async_create_manual_asset(name="Unassigned")
    await manager.async_set_asset_purchase(manual["asset_uuid"], PURCHASE_UUID)
    await manager.async_set_asset_purchase(manual["asset_uuid"], None)
    raw = deepcopy(purchase_subentry_data)
    raw[CONF_DEVICE_IDS] = []

    await manager.async_reconcile_entry(_entry(_purchase_subentry(raw)))

    asset = manager.asset(MANUAL_ASSET_UUID)
    assert asset["purchase_uuid"] is None
    assert asset["field_sources"]["purchase_uuid"] == "user"
    assert manager.purchase(PURCHASE_UUID)["asset_uuids"] == []


async def test_conflicting_user_and_legacy_purchase_assignment_is_rejected(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict,
) -> None:
    """Legacy device reconciliation cannot move a user-owned relationship."""
    data = deepcopy(asset_store_data)
    data["assets"][ASSET_UUID]["field_sources"]["purchase_uuid"] = "user"
    second_purchase = deepcopy(data["purchases"][PURCHASE_UUID])
    second_purchase.update(
        {
            "purchase_uuid": SECOND_PURCHASE_UUID,
            "config_subentry_id": SECOND_PURCHASE_SUBENTRY_ID,
            "name": "Conflicting purchase",
            "asset_uuids": [],
        }
    )
    data["purchases"][SECOND_PURCHASE_UUID] = second_purchase
    manager = _manager(hass, data)
    raw = deepcopy(purchase_subentry_data)
    raw[CONF_PURCHASE_UUID] = SECOND_PURCHASE_UUID
    raw[CONF_PURCHASE_NAME] = "Conflicting purchase"
    subentry = _purchase_subentry(
        raw,
        subentry_id=SECOND_PURCHASE_SUBENTRY_ID,
    )
    before = deepcopy(manager._data)

    with pytest.raises(AssetStoreError, match="user-managed relationship"):
        await manager.async_reconcile_entry(_entry(subentry))

    assert manager._data == before
    manager._store.async_save.assert_not_awaited()


async def test_historical_user_managed_purchase_relationship_is_preserved(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Removing a Purchase subentry retains its user-owned Asset history."""
    data = deepcopy(asset_store_data)
    data["assets"][ASSET_UUID]["field_sources"]["purchase_uuid"] = "user"
    manager = _manager(hass, data)

    await manager.async_reconcile_entry(_entry())

    asset = manager.asset(ASSET_UUID)
    purchase = manager.purchase(PURCHASE_UUID)
    assert asset["purchase_uuid"] == PURCHASE_UUID
    assert asset["field_sources"]["purchase_uuid"] == "user"
    assert purchase["configured"] is False
    assert purchase["asset_uuids"] == [ASSET_UUID]


async def test_empty_purchase_allocates_only_purchase_until_device_is_added(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Purchase-first storage defers Asset UUID and DL allocation until receipt."""
    manager = _manager(
        hass,
        {"next_asset_number": 1, "purchases": {}, "assets": {}},
    )
    uuid_factory = Mock(
        side_effect=[UUID(PURCHASE_UUID), UUID(ASSET_UUID)]
    )
    monkeypatch.setattr(
        "custom_components.device_lifecycle.storage.uuid4",
        uuid_factory,
    )
    raw = {
        CONF_PURCHASE_NAME: "Purchase first",
        CONF_PURCHASE_DATE: "2026-08-10",
        CONF_SELLER: "Example seller",
        CONF_PURCHASE_PRICE: 149.95,
        CONF_CURRENCY: "EUR",
        CONF_RECEIPT_REFERENCE: "ORDER-FIRST",
        CONF_RECEIPT_URL: "https://example.invalid/order/first",
        CONF_NOTES: "Assets arrive later",
    }
    subentry = _purchase_subentry(raw)
    entry = _entry(subentry)

    with patch.object(
        hass.config_entries,
        "async_update_subentry",
    ) as update_subentry:
        await manager.async_reconcile_entry(entry)

    purchase = manager.purchase(PURCHASE_UUID)
    assert uuid_factory.call_count == 1
    assert manager._data["assets"] == {}
    assert manager._data["next_asset_number"] == 1
    assert purchase["purchase_uuid"] == PURCHASE_UUID
    assert purchase["asset_uuids"] == []
    assert purchase["name"] == "Purchase first"
    assert purchase["purchase_date"] == "2026-08-10"
    assert purchase["seller"] == "Example seller"
    assert purchase["total_price"] == "149.95"
    assert purchase["currency"] == "EUR"
    assert purchase["receipt_reference"] == "ORDER-FIRST"
    assert purchase["receipt_url"] == "https://example.invalid/order/first"
    assert purchase["notes"] == "Assets arrive later"
    updated_data = update_subentry.call_args.kwargs["data"]
    assert updated_data[CONF_PURCHASE_UUID] == PURCHASE_UUID

    subentry.data = {
        **raw,
        CONF_PURCHASE_UUID: PURCHASE_UUID,
        CONF_DEVICE_IDS: [DEVICE_ID],
    }
    manager._store.async_save.reset_mock()

    await manager.async_reconcile_entry(entry)

    asset = manager.asset(ASSET_UUID)
    assert uuid_factory.call_count == 2
    assert asset["asset_id"] == "DL0001"
    assert asset["purchase_uuid"] == PURCHASE_UUID
    assert asset["field_sources"]["purchase_uuid"] == "purchase"
    assert asset["installed_date"] is None
    assert manager._data["next_asset_number"] == 2
    assert manager.purchase(PURCHASE_UUID)["asset_uuids"] == [ASSET_UUID]
