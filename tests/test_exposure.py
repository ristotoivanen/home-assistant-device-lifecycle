"""Asset Device and exposure registry migration tests through 0.6.1."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, patch

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
    CONF_RUNTIME_MODE,
    CONF_SOURCE_ENTITY_ID,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    RUNTIME_MODE_ON,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
)
from custom_components.device_lifecycle.exposure import (
    asset_device_entry,
    asset_device_identifier,
    async_reconcile_exposure_registry,
    build_exposure_migration_plan,
    deployment_unique_id,
    installed_date_unique_id,
    lifecycle_status_unique_id,
    replacement_unique_id,
)
from custom_components.device_lifecycle.migration import (
    async_migrate_entity_registry,
    lifecycle_unique_id,
    runtime_unique_id,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    STORAGE_KEY,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    AssetStoreError,
    AssetStoreManager,
)

from .conftest import ASSET_UUID


def _manager(
    hass: HomeAssistant,
    data: AssetStoreData,
) -> AssetStoreManager:
    """Return a detached manager whose Store writes are observable."""
    manager = AssetStoreManager(hass)
    manager._data = deepcopy(data)
    manager._store.async_save = AsyncMock()
    return manager


def _external_device(
    hass: HomeAssistant,
    registry: dr.DeviceRegistry,
    *,
    identifier: str = "asset-exposure-external",
) -> dr.DeviceEntry:
    """Create one independently owned external registry device."""
    owner = MockConfigEntry(domain="hue", title="External owner", data={})
    owner.add_to_hass(hass)
    return registry.async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("hue", identifier)},
        connections={(dr.CONNECTION_NETWORK_MAC, "00:11:22:33:44:55")},
        name="External device",
        manufacturer="External manufacturer",
        model="External model",
    )


def _entry(
    hass: HomeAssistant,
    *,
    device_id: str | None = None,
) -> MockConfigEntry:
    """Create the parent entry with optional Purchase and Runtime subentries."""
    subentries: list[dict[str, object]] = []
    if device_id is not None:
        subentries.extend(
            (
                {
                    "data": {CONF_DEVICE_IDS: [device_id]},
                    "subentry_type": SUBENTRY_TYPE_PURCHASE,
                    "title": "Purchase",
                    "unique_id": None,
                },
                {
                    "data": {
                        CONF_ASSET_UUID: ASSET_UUID,
                        CONF_DEVICE_ID: device_id,
                        CONF_RUNTIME_MODE: RUNTIME_MODE_ON,
                        CONF_SOURCE_ENTITY_ID: "switch.runtime_source",
                    },
                    "subentry_type": SUBENTRY_TYPE_RUNTIME,
                    "title": "Runtime",
                    "unique_id": None,
                },
            )
        )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="device_lifecycle_main",
        version=CONFIG_ENTRY_VERSION,
        data={},
        subentries_data=tuple(subentries),
    )
    entry.add_to_hass(hass)
    return entry


def _purchase_only_entry(
    hass: HomeAssistant,
    device_ids: list[str],
) -> MockConfigEntry:
    """Create a parent entry with one legacy Purchase ownership scope."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="device_lifecycle_main",
        version=CONFIG_ENTRY_VERSION,
        data={},
        subentries_data=(
            {
                "data": {CONF_DEVICE_IDS: device_ids},
                "subentry_type": SUBENTRY_TYPE_PURCHASE,
                "title": "Purchase",
                "unique_id": None,
            },
        ),
    )
    entry.add_to_hass(hass)
    return entry


def _external_snapshot(device: dr.DeviceEntry) -> dict[str, object]:
    """Return every external field Device Lifecycle must leave unchanged."""
    return {
        "identifiers": device.identifiers,
        "connections": device.connections,
        "config_entry_id": device.config_entry_id,
        "config_subentry_id": device.config_subentry_id,
        "name": device.name,
        "name_by_user": device.name_by_user,
        "area_id": device.area_id,
        "manufacturer": device.manufacturer,
        "model": device.model,
    }


async def test_one_asset_device_per_asset_with_exact_uuid_identity(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """Every Asset, including a manual relationship-free Asset, gets one device."""
    manager = _manager(hass, asset_store_data)
    manual = await manager.async_create_manual_asset(name="Manual Asset")
    entry = _entry(hass)
    store_before = deepcopy(manager._data)

    await async_reconcile_exposure_registry(hass, entry, manager)

    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert len(devices) == 2
    assert {next(iter(device.identifiers)) for device in devices} == {
        asset_device_identifier(ASSET_UUID),
        asset_device_identifier(manual["asset_uuid"]),
    }
    assert all(len(device.identifiers) == 1 for device in devices)
    assert all(not device.connections for device in devices)
    assert all(device.config_subentry_id is None for device in devices)
    assert all(device.area_id is None for device in devices)
    assert manager._data == store_before
    manager._store.async_save.assert_awaited_once()  # manual Asset creation only


async def test_asset_device_projects_only_canonical_store_metadata(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """Supported physical metadata is projected without fabricated values."""
    manager = _manager(hass, asset_store_data)
    entry = _entry(hass)

    await async_reconcile_exposure_registry(hass, entry, manager)

    device = asset_device_entry(
        device_registry,
        config_entry_id=entry.entry_id,
        asset_uuid=ASSET_UUID,
    )
    assert device is not None
    asset = asset_store_data["assets"][ASSET_UUID]
    assert device.name == asset["name"]
    assert device.manufacturer == asset["manufacturer"]
    assert device.model == asset["model"]
    assert device.model_id == asset["model_id"]
    assert device.serial_number == asset["serial_number"]
    assert device.sw_version == asset["sw_version"]
    assert device.hw_version == asset["hw_version"]
    assert device.name != asset["asset_id"]


async def test_rename_and_primary_change_keep_asset_device_identity(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """Mutable metadata and external relationships never redefine projection ID."""
    manager = _manager(hass, asset_store_data)
    entry = _entry(hass)
    await async_reconcile_exposure_registry(hass, entry, manager)
    original = asset_device_entry(
        device_registry,
        config_entry_id=entry.entry_id,
        asset_uuid=ASSET_UUID,
    )
    assert original is not None

    manager._data["assets"][ASSET_UUID]["name"] = "Renamed canonical Asset"
    manager._data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": "new-external-primary", "role": "primary"}
    ]
    await async_reconcile_exposure_registry(hass, entry, manager)

    current = asset_device_entry(
        device_registry,
        config_entry_id=entry.entry_id,
        asset_uuid=ASSET_UUID,
    )
    assert current is not None
    assert current.id == original.id
    assert current.identifiers == {asset_device_identifier(ASSET_UUID)}
    assert current.name == "Renamed canonical Asset"


async def test_existing_device_reused_and_missing_projection_recreated(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """The deterministic projection is reusable and safely recreatable."""
    manager = _manager(hass, asset_store_data)
    entry = _entry(hass)
    existing = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=None,
        identifiers={asset_device_identifier(ASSET_UUID)},
        name="Old projection name",
    )

    await async_reconcile_exposure_registry(hass, entry, manager)
    reused = asset_device_entry(
        device_registry,
        config_entry_id=entry.entry_id,
        asset_uuid=ASSET_UUID,
    )
    assert reused is not None
    assert reused.id == existing.id

    device_registry.async_remove_device(reused.id)
    await async_reconcile_exposure_registry(hass, entry, manager)
    recreated = asset_device_entry(
        device_registry,
        config_entry_id=entry.entry_id,
        asset_uuid=ASSET_UUID,
    )
    assert recreated is not None
    assert recreated.identifiers == {asset_device_identifier(ASSET_UUID)}
    assert manager.asset(ASSET_UUID)["asset_uuid"] == ASSET_UUID
    assert manager.asset(ASSET_UUID)["asset_id"] == "DL0007"


async def test_incomplete_owned_device_subentry_is_repaired_to_parent(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """An exact owned projection is repairable without changing Asset identity."""
    manager = _manager(hass, asset_store_data)
    entry = _purchase_only_entry(hass, [])
    purchase_subentry = next(iter(entry.subentries.values()))
    misplaced = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=purchase_subentry.subentry_id,
        identifiers={asset_device_identifier(ASSET_UUID)},
        name="Misplaced owned projection",
    )

    await async_reconcile_exposure_registry(hass, entry, manager)

    repaired = asset_device_entry(
        device_registry,
        config_entry_id=entry.entry_id,
        asset_uuid=ASSET_UUID,
    )
    assert repaired is not None
    assert repaired.id == misplaced.id
    assert repaired.config_subentry_id is None
    assert manager.asset(ASSET_UUID)["asset_uuid"] == ASSET_UUID
    assert manager.asset(ASSET_UUID)["asset_id"] == "DL0007"


async def test_ambiguous_asset_device_preflight_fails_before_mutation(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """Multiple exact identifier holders are never selected by metadata."""
    manager = _manager(hass, asset_store_data)
    entry = _entry(hass)
    identifier = {asset_device_identifier(ASSET_UUID)}
    first = dr.DeviceEntry(
        config_entry_id=entry.entry_id,
        identifiers=identifier,
        name="First",
    )
    second = dr.DeviceEntry(
        config_entry_id=entry.entry_id,
        identifiers=identifier,
        name="Second",
    )
    device_registry.devices[first.id] = first
    device_registry.devices[second.id] = second
    before_ids = set(device_registry.devices)

    with pytest.raises(AssetStoreError, match="2 Device Registry devices"):
        build_exposure_migration_plan(
            entry=entry,
            assets=manager.assets(),
            device_registry=device_registry,
            entity_registry=entity_registry,
        )

    assert set(device_registry.devices) == before_ids


async def test_external_device_and_asset_area_are_never_mutated(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    area_registry,
    asset_store_data: AssetStoreData,
) -> None:
    """External ownership and deployment Area remain separate registry scopes."""
    external = _external_device(hass, device_registry)
    external_area = area_registry.async_create("External Area")
    asset_area = area_registry.async_create("Asset Area")
    device_registry.async_update_device(external.id, area_id=external_area.id)
    external = device_registry.async_get(external.id)
    before = _external_snapshot(external)
    asset_store_data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": external.id, "role": "primary"}
    ]
    asset_store_data["assets"][ASSET_UUID]["ha_area_id"] = asset_area.id
    manager = _manager(hass, asset_store_data)
    entry = _entry(hass)

    await async_reconcile_exposure_registry(hass, entry, manager)

    projected = asset_device_entry(
        device_registry,
        config_entry_id=entry.entry_id,
        asset_uuid=ASSET_UUID,
    )
    assert projected is not None
    assert projected.area_id is None
    assert _external_snapshot(device_registry.async_get(external.id)) == before


async def test_lifecycle_and_runtime_registry_ownership_migrates_in_place(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """Existing recorder identities move while Runtime subentry ownership stays."""
    external = _external_device(hass, device_registry)
    asset_store_data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": external.id, "role": "primary"}
    ]
    asset_store_data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "1234"
    manager = _manager(hass, asset_store_data)
    entry = _entry(hass, device_id=external.id)
    purchase_subentry = next(
        item
        for item in entry.subentries.values()
        if item.subentry_type == SUBENTRY_TYPE_PURCHASE
    )
    runtime_subentry = next(
        item
        for item in entry.subentries.values()
        if item.subentry_type == SUBENTRY_TYPE_RUNTIME
    )
    lifecycle = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        lifecycle_unique_id(ASSET_UUID),
        suggested_object_id="preserved_lifecycle",
        config_entry=entry,
        config_subentry_id=purchase_subentry.subentry_id,
        device_id=external.id,
    )
    runtime = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        runtime_unique_id(ASSET_UUID),
        suggested_object_id="preserved_runtime",
        config_entry=entry,
        config_subentry_id=runtime_subentry.subentry_id,
        device_id=external.id,
    )
    entity_registry.async_update_entity(
        lifecycle.entity_id,
        name="User lifecycle name",
    )
    lifecycle_entity_id = lifecycle.entity_id
    runtime_entity_id = runtime.entity_id
    store_before = deepcopy(manager._data)

    await async_reconcile_exposure_registry(hass, entry, manager)

    asset_device = asset_device_entry(
        device_registry,
        config_entry_id=entry.entry_id,
        asset_uuid=ASSET_UUID,
    )
    migrated_lifecycle = entity_registry.async_get(lifecycle_entity_id)
    migrated_runtime = entity_registry.async_get(runtime_entity_id)
    assert asset_device is not None
    assert migrated_lifecycle.entity_id == lifecycle_entity_id
    assert migrated_lifecycle.unique_id == lifecycle_unique_id(ASSET_UUID)
    assert migrated_lifecycle.config_subentry_id is None
    assert migrated_lifecycle.device_id == asset_device.id
    assert migrated_lifecycle.name == "User lifecycle name"
    assert migrated_runtime.entity_id == runtime_entity_id
    assert migrated_runtime.unique_id == runtime_unique_id(ASSET_UUID)
    assert migrated_runtime.config_subentry_id == runtime_subentry.subentry_id
    assert migrated_runtime.device_id == asset_device.id
    assert manager.runtime_total_seconds(ASSET_UUID) == 1234
    assert manager._data == store_before


async def test_deployment_entity_collision_fails_closed_without_device_creation(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """A foreign Deployment unique ID is not deleted or taken over."""
    manager = _manager(hass, asset_store_data)
    entry = _entry(hass)
    foreign = MockConfigEntry(domain=DOMAIN, title="Foreign", data={})
    foreign.add_to_hass(hass)
    collision = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        deployment_unique_id(ASSET_UUID),
        suggested_object_id="collision",
        config_entry=foreign,
    )

    with pytest.raises(AssetStoreError, match="not unambiguously owned"):
        await async_reconcile_exposure_registry(hass, entry, manager)

    assert entity_registry.async_get(collision.entity_id) == collision
    assert dr.async_entries_for_config_entry(device_registry, entry.entry_id) == []


async def test_installation_date_collision_fails_closed_without_device_creation(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """A foreign Installation Date unique ID is not deleted or taken over."""
    manager = _manager(hass, asset_store_data)
    entry = _entry(hass)
    foreign = MockConfigEntry(domain=DOMAIN, title="Foreign", data={})
    foreign.add_to_hass(hass)
    collision = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        installed_date_unique_id(ASSET_UUID),
        suggested_object_id="installation_date_collision",
        config_entry=foreign,
    )

    with pytest.raises(AssetStoreError, match="not unambiguously owned"):
        await async_reconcile_exposure_registry(hass, entry, manager)

    assert entity_registry.async_get(collision.entity_id) == collision
    assert dr.async_entries_for_config_entry(device_registry, entry.entry_id) == []


async def test_compatible_installation_date_entity_is_reused_idempotently(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """A correctly owned existing identity converges onto the Asset Device."""
    manager = _manager(hass, asset_store_data)
    entry = _entry(hass)
    unique_id = installed_date_unique_id(ASSET_UUID)
    existing = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        unique_id,
        suggested_object_id="installation_date",
        config_entry=entry,
    )

    await async_reconcile_exposure_registry(hass, entry, manager)
    first = entity_registry.async_get(existing.entity_id)
    asset_device = asset_device_entry(
        device_registry,
        config_entry_id=entry.entry_id,
        asset_uuid=ASSET_UUID,
    )
    assert first is not None
    assert asset_device is not None
    assert first.entity_id == existing.entity_id
    assert first.unique_id == unique_id
    assert first.config_subentry_id is None
    assert first.device_id == asset_device.id

    await async_reconcile_exposure_registry(hass, entry, manager)
    second = entity_registry.async_get(existing.entity_id)
    assert second is not None
    assert second.entity_id == first.entity_id
    assert second.unique_id == first.unique_id
    assert second.device_id == first.device_id


@pytest.mark.parametrize("fail_after_creations", [0, 1, 2])
async def test_device_creation_failure_rolls_back_only_attempt_devices(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    asset_store_data: AssetStoreData,
    fail_after_creations: int,
) -> None:
    """Failures before/after one or multiple creations preserve Asset Core."""
    manager = _manager(hass, asset_store_data)
    await manager.async_create_manual_asset(name="Second")
    await manager.async_create_manual_asset(name="Third")
    entry = _entry(hass)
    store_before = deepcopy(manager._data)
    original_get_or_create = device_registry.async_get_or_create
    successful = 0

    def _fail_creation(**kwargs):
        nonlocal successful
        if successful == fail_after_creations:
            raise RuntimeError("injected device creation failure")
        successful += 1
        return original_get_or_create(**kwargs)

    with (
        patch.object(
            device_registry,
            "async_get_or_create",
            side_effect=_fail_creation,
        ),
        pytest.raises(AssetStoreError, match="rolled back"),
    ):
        await async_reconcile_exposure_registry(hass, entry, manager)

    assert dr.async_entries_for_config_entry(device_registry, entry.entry_id) == []
    assert manager._data == store_before


async def test_entity_update_failure_rolls_back_entities_and_created_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """A Runtime move failure reverses the completed Lifecycle move."""
    external = _external_device(hass, device_registry)
    asset_store_data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": external.id, "role": "primary"}
    ]
    manager = _manager(hass, asset_store_data)
    entry = _entry(hass, device_id=external.id)
    purchase_subentry = next(
        item
        for item in entry.subentries.values()
        if item.subentry_type == SUBENTRY_TYPE_PURCHASE
    )
    runtime_subentry = next(
        item
        for item in entry.subentries.values()
        if item.subentry_type == SUBENTRY_TYPE_RUNTIME
    )
    lifecycle = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        lifecycle_unique_id(ASSET_UUID),
        config_entry=entry,
        config_subentry_id=purchase_subentry.subentry_id,
        device_id=external.id,
    )
    runtime = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        runtime_unique_id(ASSET_UUID),
        config_entry=entry,
        config_subentry_id=runtime_subentry.subentry_id,
        device_id=external.id,
    )
    store_before = deepcopy(manager._data)
    original_update = entity_registry.async_update_entity
    runtime_updates = 0

    def _fail_runtime(entity_id: str, **kwargs):
        nonlocal runtime_updates
        if entity_id == runtime.entity_id:
            runtime_updates += 1
            if runtime_updates == 1:
                raise RuntimeError("injected Runtime update failure")
        return original_update(entity_id, **kwargs)

    with (
        patch.object(
            entity_registry,
            "async_update_entity",
            side_effect=_fail_runtime,
        ),
        pytest.raises(AssetStoreError, match="rolled back"),
    ):
        await async_reconcile_exposure_registry(hass, entry, manager)

    restored_lifecycle = entity_registry.async_get(lifecycle.entity_id)
    restored_runtime = entity_registry.async_get(runtime.entity_id)
    assert restored_lifecycle.device_id == external.id
    assert restored_lifecycle.config_subentry_id == purchase_subentry.subentry_id
    assert restored_runtime.device_id == external.id
    assert restored_runtime.config_subentry_id == runtime_subentry.subentry_id
    assert dr.async_entries_for_config_entry(device_registry, entry.entry_id) == []
    assert manager._data == store_before


@pytest.mark.parametrize("successful_lifecycle_moves", [0, 1])
async def test_lifecycle_update_failures_before_first_and_during_updates(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
    successful_lifecycle_moves: int,
) -> None:
    """Lifecycle migration failure restores every attempted existing entity."""
    first_external = _external_device(
        hass,
        device_registry,
        identifier="lifecycle-first",
    )
    second_external = _external_device(
        hass,
        device_registry,
        identifier="lifecycle-second",
    )
    asset_store_data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": first_external.id, "role": "primary"}
    ]
    manager = _manager(hass, asset_store_data)
    second_asset = await manager.async_create_manual_asset(name="Second Asset")
    await manager.async_link_asset_device(
        second_asset["asset_uuid"],
        second_external.id,
    )
    entry = _purchase_only_entry(
        hass,
        [first_external.id, second_external.id],
    )
    purchase_subentry = next(iter(entry.subentries.values()))
    lifecycle_entries = []
    for asset_uuid, external in sorted(
        (
            (ASSET_UUID, first_external),
            (second_asset["asset_uuid"], second_external),
        )
    ):
        lifecycle_entries.append(
            entity_registry.async_get_or_create(
                Platform.SENSOR,
                DOMAIN,
                lifecycle_unique_id(asset_uuid),
                config_entry=entry,
                config_subentry_id=purchase_subentry.subentry_id,
                device_id=external.id,
            )
        )
    store_before = deepcopy(manager._data)
    original_update = entity_registry.async_update_entity
    forward_calls = 0
    failed = False

    def _fail_selected(entity_id: str, **kwargs):
        nonlocal forward_calls, failed
        if not failed and kwargs.get("config_subentry_id") is None:
            if forward_calls == successful_lifecycle_moves:
                failed = True
                raise RuntimeError("injected Lifecycle update failure")
            forward_calls += 1
        return original_update(entity_id, **kwargs)

    with (
        patch.object(
            entity_registry,
            "async_update_entity",
            side_effect=_fail_selected,
        ),
        pytest.raises(AssetStoreError, match="rolled back"),
    ):
        await async_reconcile_exposure_registry(hass, entry, manager)

    assert {
        entity_registry.async_get(item.entity_id).device_id
        for item in lifecycle_entries
    } == {first_external.id, second_external.id}
    assert all(
        entity_registry.async_get(item.entity_id).config_subentry_id
        == purchase_subentry.subentry_id
        for item in lifecycle_entries
    )
    assert dr.async_entries_for_config_entry(device_registry, entry.entry_id) == []
    assert manager._data == store_before


async def test_created_device_cleanup_failure_is_recoverable(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """Cleanup failure leaves only deterministic projections for the next retry."""
    manager = _manager(hass, asset_store_data)
    await manager.async_create_manual_asset(name="Second Asset")
    entry = _entry(hass)
    store_before = deepcopy(manager._data)
    original_get_or_create = device_registry.async_get_or_create
    calls = 0

    def _fail_second_create(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected second creation failure")
        return original_get_or_create(**kwargs)

    with (
        patch.object(
            device_registry,
            "async_get_or_create",
            side_effect=_fail_second_create,
        ),
        patch.object(
            device_registry,
            "async_remove_device",
            side_effect=RuntimeError("injected cleanup failure"),
        ),
        pytest.raises(AssetStoreError, match="rollback was incomplete"),
    ):
        await async_reconcile_exposure_registry(hass, entry, manager)

    assert len(dr.async_entries_for_config_entry(device_registry, entry.entry_id)) == 1
    assert manager._data == store_before

    await async_reconcile_exposure_registry(hass, entry, manager)

    assert len(dr.async_entries_for_config_entry(device_registry, entry.entry_id)) == 2
    for asset in manager.assets():
        matches = [
            device
            for device in device_registry.devices.values()
            if asset_device_identifier(asset["asset_uuid"]) in device.identifiers
        ]
        assert len(matches) == 1
    assert manager._data == store_before


async def test_partial_migration_after_rollback_failure_recovers_on_reload(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """A partial derived projection converges on the next setup attempt."""
    external = _external_device(hass, device_registry)
    asset_store_data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": external.id, "role": "primary"}
    ]
    asset_store_data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "900"
    manager = _manager(hass, asset_store_data)
    entry = _entry(hass, device_id=external.id)
    purchase_subentry = next(
        item
        for item in entry.subentries.values()
        if item.subentry_type == SUBENTRY_TYPE_PURCHASE
    )
    runtime_subentry = next(
        item
        for item in entry.subentries.values()
        if item.subentry_type == SUBENTRY_TYPE_RUNTIME
    )
    lifecycle = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        lifecycle_unique_id(ASSET_UUID),
        config_entry=entry,
        config_subentry_id=purchase_subentry.subentry_id,
        device_id=external.id,
    )
    runtime = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        runtime_unique_id(ASSET_UUID),
        config_entry=entry,
        config_subentry_id=runtime_subentry.subentry_id,
        device_id=external.id,
    )
    store_before = deepcopy(manager._data)
    original_update = entity_registry.async_update_entity
    lifecycle_updates = 0

    def _fail_forward_and_rollback(entity_id: str, **kwargs):
        nonlocal lifecycle_updates
        if entity_id == runtime.entity_id:
            raise RuntimeError("injected Runtime update failure")
        if entity_id == lifecycle.entity_id:
            lifecycle_updates += 1
            if lifecycle_updates == 2:
                raise RuntimeError("injected rollback failure")
        return original_update(entity_id, **kwargs)

    with (
        patch.object(
            entity_registry,
            "async_update_entity",
            side_effect=_fail_forward_and_rollback,
        ),
        pytest.raises(AssetStoreError, match="rollback was incomplete"),
    ):
        await async_reconcile_exposure_registry(hass, entry, manager)

    partial_device = asset_device_entry(
        device_registry,
        config_entry_id=entry.entry_id,
        asset_uuid=ASSET_UUID,
    )
    assert partial_device is not None
    assert entity_registry.async_get(lifecycle.entity_id).device_id == partial_device.id
    assert entity_registry.async_get(runtime.entity_id).device_id == external.id
    assert manager._data == store_before

    await async_reconcile_exposure_registry(hass, entry, manager)

    assert entity_registry.async_get(lifecycle.entity_id).device_id == partial_device.id
    assert entity_registry.async_get(lifecycle.entity_id).config_subentry_id is None
    assert entity_registry.async_get(runtime.entity_id).device_id == partial_device.id
    assert (
        entity_registry.async_get(runtime.entity_id).config_subentry_id
        == runtime_subentry.subentry_id
    )
    assert manager._data == store_before
    assert manager.runtime_total_seconds(ASSET_UUID) == 900
    assert (
        len(
            [
                device
                for device in device_registry.devices.values()
                if asset_device_identifier(ASSET_UUID) in device.identifiers
            ]
        )
        == 1
    )


def test_0_7_0_changes_only_the_store_schema() -> None:
    """Lifecycle/replacement use Store 3.1 without a ConfigEntry migration."""
    assert STORAGE_VERSION == 3
    assert STORAGE_MINOR_VERSION == 1
    assert CONFIG_ENTRY_VERSION == 4


@pytest.mark.parametrize(
    "unique_id_factory",
    [lifecycle_status_unique_id, replacement_unique_id],
)
def test_new_entity_foreign_collision_fails_exposure_preflight(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
    unique_id_factory,
) -> None:
    """New deterministic identities are preflighted before registry mutation."""
    entry = _entry(hass)
    foreign = MockConfigEntry(domain="test", title="Foreign", data={})
    foreign.add_to_hass(hass)
    entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        unique_id_factory(ASSET_UUID),
        config_entry=foreign,
        suggested_object_id="foreign_collision",
    )

    with pytest.raises(AssetStoreError, match="not unambiguously owned"):
        build_exposure_migration_plan(
            entry=entry,
            assets=list(asset_store_data["assets"].values()),
            device_registry=device_registry,
            entity_registry=entity_registry,
        )


@pytest.mark.parametrize(
    ("disabled_by", "expected"),
    [
        (er.RegistryEntryDisabler.INTEGRATION, None),
        (
            er.RegistryEntryDisabler.USER,
            er.RegistryEntryDisabler.USER,
        ),
        (
            er.RegistryEntryDisabler.CONFIG_ENTRY,
            er.RegistryEntryDisabler.CONFIG_ENTRY,
        ),
    ],
)
async def test_active_replacement_enables_only_integration_disabled_entry(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
    disabled_by: er.RegistryEntryDisabler,
    expected: er.RegistryEntryDisabler | None,
) -> None:
    """Canonical exposure respects user/config-entry disable decisions."""
    manager = _manager(hass, asset_store_data)
    successor = await manager.async_create_manual_asset(name="Replacement")
    await manager.async_create_asset_replacement(
        ASSET_UUID,
        successor["asset_uuid"],
        reason="failure",
        effective_date=None,
        notes=None,
    )
    entry = _entry(hass)
    registry_entry = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        replacement_unique_id(ASSET_UUID),
        config_entry=entry,
        suggested_object_id="replacement_visibility",
        disabled_by=disabled_by,
    )

    await async_reconcile_exposure_registry(hass, entry, manager)

    updated = entity_registry.async_get(registry_entry.entity_id)
    assert updated is not None
    assert updated.disabled_by is expected


async def test_voided_replacement_does_not_automatically_disable_entry(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """An enabled relationship projection remains enabled after later voiding."""
    manager = _manager(hass, asset_store_data)
    successor = await manager.async_create_manual_asset(name="Replacement")
    record = await manager.async_create_asset_replacement(
        ASSET_UUID,
        successor["asset_uuid"],
        reason="failure",
        effective_date=None,
        notes=None,
    )
    entry = _entry(hass)
    registry_entry = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        replacement_unique_id(ASSET_UUID),
        config_entry=entry,
        suggested_object_id="replacement_stays_enabled",
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    await async_reconcile_exposure_registry(hass, entry, manager)
    await manager.async_void_asset_replacement(
        record["replacement_uuid"],
        void_reason="Incorrect link",
    )

    await async_reconcile_exposure_registry(hass, entry, manager)

    updated = entity_registry.async_get(registry_entry.entity_id)
    assert updated is not None
    assert updated.disabled_by is None


async def test_full_setup_creates_parent_owned_entities_after_registry_gate(
    hass: HomeAssistant,
    hass_storage,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """Platform forwarding happens only after the deterministic device exists."""
    asset_store_data["assets"][ASSET_UUID]["purchase_uuid"] = None
    asset_store_data["assets"][ASSET_UUID]["field_sources"].pop(
        "purchase_uuid",
        None,
    )
    asset_store_data["purchases"] = {}
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "minor_version": STORAGE_MINOR_VERSION,
        "key": STORAGE_KEY,
        "data": deepcopy(asset_store_data),
    }
    entry = _entry(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    asset_device = asset_device_entry(
        device_registry,
        config_entry_id=entry.entry_id,
        asset_uuid=ASSET_UUID,
    )
    assert asset_device is not None
    expected_unique_ids = {
        lifecycle_unique_id(ASSET_UUID),
        deployment_unique_id(ASSET_UUID),
        installed_date_unique_id(ASSET_UUID),
        f"{ASSET_UUID}_relationships",
        f"{ASSET_UUID}_asset_id",
    }
    entries = [
        item
        for item in er.async_entries_for_config_entry(
            entity_registry,
            entry.entry_id,
        )
        if item.unique_id in expected_unique_ids
    ]
    assert {item.unique_id for item in entries} == expected_unique_ids
    assert all(item.config_subentry_id is None for item in entries)
    assert all(item.device_id == asset_device.id for item in entries)


async def test_direct_legacy_upgrade_runs_identity_then_exposure_migration(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """A supported 0.4.x registry identity converges without new Asset identity."""
    external = _external_device(hass, device_registry)
    asset_store_data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": external.id, "role": "primary"}
    ]
    manager = _manager(hass, asset_store_data)
    entry = _entry(hass, device_id=external.id)
    purchase_subentry = next(
        item
        for item in entry.subentries.values()
        if item.subentry_type == SUBENTRY_TYPE_PURCHASE
    )
    runtime_subentry = next(
        item
        for item in entry.subentries.values()
        if item.subentry_type == SUBENTRY_TYPE_RUNTIME
    )
    legacy_lifecycle = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        f"{purchase_subentry.subentry_id}_{external.id}_lifecycle",
        suggested_object_id="legacy_lifecycle",
        config_entry=entry,
        config_subentry_id=purchase_subentry.subentry_id,
        device_id=external.id,
    )
    legacy_runtime = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        f"{runtime_subentry.subentry_id}_{external.id}_runtime_hours",
        suggested_object_id="legacy_runtime",
        config_entry=entry,
        config_subentry_id=runtime_subentry.subentry_id,
        device_id=external.id,
    )
    lifecycle_entity_id = legacy_lifecycle.entity_id
    runtime_entity_id = legacy_runtime.entity_id
    store_before = deepcopy(manager._data)

    await async_migrate_entity_registry(hass, entry, manager)
    assert entity_registry.async_get(lifecycle_entity_id).unique_id == (
        lifecycle_unique_id(ASSET_UUID)
    )
    assert entity_registry.async_get(runtime_entity_id).unique_id == (
        runtime_unique_id(ASSET_UUID)
    )

    await async_reconcile_exposure_registry(hass, entry, manager)

    asset_device = asset_device_entry(
        device_registry,
        config_entry_id=entry.entry_id,
        asset_uuid=ASSET_UUID,
    )
    assert asset_device is not None
    lifecycle = entity_registry.async_get(lifecycle_entity_id)
    runtime = entity_registry.async_get(runtime_entity_id)
    assert lifecycle.entity_id == lifecycle_entity_id
    assert lifecycle.device_id == asset_device.id
    assert lifecycle.config_subentry_id is None
    assert runtime.entity_id == runtime_entity_id
    assert runtime.device_id == asset_device.id
    assert runtime.config_subentry_id == runtime_subentry.subentry_id
    assert manager._data == store_before
