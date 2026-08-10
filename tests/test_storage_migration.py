"""Tests for Device Lifecycle Asset Store 1.1/1.2/2.1 to 3.1 migration."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_DEPLOYMENT_STATE,
    CONF_DEVICE_ID,
    CONF_HA_AREA_ID,
    CONFIG_ENTRY_VERSION,
    DEPLOYMENT_STATE_UNKNOWN,
    DOMAIN,
    SUBENTRY_TYPE_RUNTIME,
)
from custom_components.device_lifecycle.migration import (
    async_migrate_entity_registry,
    runtime_unique_id,
)
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
    assert migrated_asset["lifecycle"] == {
        "status": "unknown",
        "current_event_uuid": None,
    }
    assert migrated["lifecycle_events"] == {}
    assert migrated["replacement_records"] == {}

    assert runtime_subentry_data == runtime_before
    assert runtime_subentry_data["asset_uuid"] == ASSET_UUID
    assert runtime_unique_id(ASSET_UUID) == f"{ASSET_UUID}_runtime_hours"
    _validate_store_data(migrated)


async def test_store_2_1_to_3_1_preserves_all_existing_canonical_data(
    hass: HomeAssistant,
    asset_store_data,
) -> None:
    """The 3.1 boundary adds only lifecycle/replacement structures."""
    source = deepcopy(asset_store_data)
    for asset in source["assets"].values():
        del asset["lifecycle"]
        asset["runtime"]["total_seconds"] = "1234.500"
    del source["lifecycle_events"]
    del source["replacement_records"]
    before = deepcopy(source)

    migrated = await DeviceLifecycleStore(hass)._async_migrate_func(2, 1, source)

    assert source == before
    comparable = deepcopy(migrated)
    for asset in comparable["assets"].values():
        assert asset.pop("lifecycle") == {
            "status": "unknown",
            "current_event_uuid": None,
        }
    assert comparable.pop("lifecycle_events") == {}
    assert comparable.pop("replacement_records") == {}
    assert comparable == before
    _validate_store_data(migrated)


async def test_valid_store_3_1_load_is_exact(
    hass: HomeAssistant,
    asset_store_data,
) -> None:
    source = deepcopy(asset_store_data)

    loaded = await DeviceLifecycleStore(hass)._async_migrate_func(3, 1, source)

    assert loaded == source
    assert loaded is not source


@pytest.mark.parametrize(("major", "minor"), [(3, 2), (4, 1), (2, 2)])
async def test_unsupported_store_versions_fail_closed(
    hass: HomeAssistant,
    asset_store_data,
    major: int,
    minor: int,
) -> None:
    with pytest.raises(AssetStoreError, match="Unsupported Asset Core Store version"):
        await DeviceLifecycleStore(hass)._async_migrate_func(
            major,
            minor,
            deepcopy(asset_store_data),
        )


async def test_corrupt_store_3_1_is_rejected(
    hass: HomeAssistant,
    asset_store_data,
) -> None:
    asset_store_data["assets"][ASSET_UUID]["lifecycle"] = {
        "status": "active",
        "current_event_uuid": None,
    }

    with pytest.raises(AssetStoreError) as raised:
        await DeviceLifecycleStore(hass)._async_migrate_func(
            3,
            1,
            asset_store_data,
        )

    assert raised.value.code == "lifecycle_chain_invalid"


def test_store_serializes_detached_snapshot_off_event_loop(
    hass: HomeAssistant,
) -> None:
    """3.1 opts into executor serialization under the manager's detached lock."""
    store = DeviceLifecycleStore(hass)
    assert store._serialize_in_event_loop is False


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
    current_schema = await store._async_migrate_func(3, 1, deepcopy(migrated_once))

    assert migrated_twice == migrated_once
    assert current_schema == migrated_once


async def test_store_1_2_related_refs_survive_v2_migration(
    hass: HomeAssistant,
    asset_store_data,
) -> None:
    """Existing Store 1.2 related references survive the v2 boundary."""
    source = deepcopy(asset_store_data)
    source["assets"][ASSET_UUID]["ha_device_refs"].extend(
        [
            {"device_id": "related-one", "role": "related"},
            {"device_id": "related-two", "role": "related"},
        ]
    )
    store = DeviceLifecycleStore(hass)

    loaded = await store._async_migrate_func(1, 2, source)

    assert loaded == source
    assert loaded["assets"][ASSET_UUID]["ha_device_refs"][-2:] == [
        {"device_id": "related-one", "role": "related"},
        {"device_id": "related-two", "role": "related"},
    ]
    _validate_store_data(loaded)


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


def test_schema_and_config_entry_versions_for_0_7_0() -> None:
    """0.7.0 uses Store 3.1 without changing the ConfigEntry schema."""
    assert STORAGE_VERSION == 3
    assert STORAGE_MINOR_VERSION == 1
    assert CONFIG_ENTRY_VERSION == 4


async def test_runtime_entity_migration_resolves_primary_asset(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Runtime entity migration ignores a related-only stored Asset UUID."""
    external_entry = MockConfigEntry(
        domain="hue",
        title="External owner",
        data={},
    )
    external_entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=external_entry.entry_id,
        identifiers={("hue", "migration-primary-only")},
        name="Migration device",
    )

    manager = AssetStoreManager(hass)
    manager._store.async_save = AsyncMock()
    related_asset = await manager.async_create_manual_asset(name="Related Asset")
    primary_asset = await manager.async_create_manual_asset(name="Primary Asset")
    await manager.async_add_related_device(related_asset["asset_uuid"], device.id)
    await manager.async_link_asset_device(primary_asset["asset_uuid"], device.id)

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="device_lifecycle_main",
        version=CONFIG_ENTRY_VERSION,
        data={},
        subentries_data=(
            {
                "data": {
                    CONF_ASSET_UUID: related_asset["asset_uuid"],
                    CONF_DEVICE_ID: device.id,
                },
                "subentry_type": SUBENTRY_TYPE_RUNTIME,
                "title": "Runtime migration",
                "unique_id": None,
            },
        ),
    )
    entry.add_to_hass(hass)
    runtime_subentry = next(iter(entry.subentries.values()))
    legacy_unique_id = (
        f"{runtime_subentry.subentry_id}_{device.id}_runtime_hours"
    )
    registry_entry = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        legacy_unique_id,
        suggested_object_id="migration_runtime_hours",
        config_entry=entry,
        config_subentry_id=runtime_subentry.subentry_id,
        device_id=device.id,
    )
    original_entity_id = registry_entry.entity_id

    await async_migrate_entity_registry(hass, entry, manager)

    migrated = entity_registry.async_get(original_entity_id)
    assert migrated is not None
    assert migrated.entity_id == original_entity_id
    assert migrated.unique_id == runtime_unique_id(primary_asset["asset_uuid"])
    assert migrated.config_subentry_id == runtime_subentry.subentry_id
    assert migrated.device_id == device.id
    assert entity_registry.async_get_entity_id(
        Platform.SENSOR,
        DOMAIN,
        runtime_unique_id(related_asset["asset_uuid"]),
    ) is None


async def test_existing_device_purchase_creation_sets_purchase_provenance(
    hass: HomeAssistant,
    purchase_subentry_data,
) -> None:
    """Existing HA-device Purchase creation remains valid under schema 1.2."""
    manager_data = {
        "next_asset_number": 1,
        "purchases": {},
        "assets": {},
        "lifecycle_events": {},
        "replacement_records": {},
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
