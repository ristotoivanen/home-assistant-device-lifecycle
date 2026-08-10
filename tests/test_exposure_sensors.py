"""Deployment, Relationships, Asset ID, and Lifecycle exposure tests."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, Mock

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle.const import (
    CONFIG_ENTRY_VERSION,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DEPLOYMENT_STATE_UNKNOWN,
    DOMAIN,
)
from custom_components.device_lifecycle.exposure import (
    asset_device_identifier,
    asset_id_unique_id,
    deployment_unique_id,
    relationships_unique_id,
)
from custom_components.device_lifecycle.migration import lifecycle_unique_id
from custom_components.device_lifecycle.models import AssetData
from custom_components.device_lifecycle.sensor import (
    DeviceAssetIdSensor,
    DeviceDeploymentSensor,
    DeviceLifecycleSensor,
    DeviceRelationshipsSensor,
    _DeploymentAreaRegistryCoordinator,
    _RelationshipsRegistryCoordinator,
    async_setup_entry,
)
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import ASSET_UUID


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    """Register the parent Device Lifecycle config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="device_lifecycle_main",
        version=CONFIG_ENTRY_VERSION,
        data={},
    )
    entry.add_to_hass(hass)
    return entry


def _asset_device(
    registry: dr.DeviceRegistry,
    entry: MockConfigEntry,
    asset_uuid: str = ASSET_UUID,
) -> dr.DeviceEntry:
    """Register the exact owned Asset Device required before platform setup."""
    return registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=None,
        identifiers={asset_device_identifier(asset_uuid)},
        name="Asset projection",
    )


def _manager(
    hass: HomeAssistant,
    asset_store_data,
) -> AssetStoreManager:
    """Return one manager with no persistence side effects."""
    manager = AssetStoreManager(hass)
    manager._data = deepcopy(asset_store_data)
    manager._store.async_save = AsyncMock()
    return manager


def _relationship_asset(
    asset_store_data,
    references: list[dict[str, str]],
) -> AssetData:
    """Return an Asset snapshot with the requested exact relationships."""
    asset = deepcopy(asset_store_data["assets"][ASSET_UUID])
    asset["ha_device_refs"] = references
    return asset


def _external_device(
    hass: HomeAssistant,
    registry: dr.DeviceRegistry,
    *,
    identifier: str,
    name: str,
) -> dr.DeviceEntry:
    """Register an independently owned external device."""
    entry = MockConfigEntry(domain="hue", title=f"Owner {identifier}", data={})
    entry.add_to_hass(hass)
    return registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("hue", identifier)},
        name=name,
    )


async def test_every_asset_gets_parent_owned_lifecycle_without_purchase(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    asset_store_data,
) -> None:
    """A relationship-free, Purchase-free Asset still exposes Lifecycle."""
    asset = asset_store_data["assets"][ASSET_UUID]
    asset["purchase_uuid"] = None
    asset["field_sources"].pop("purchase_uuid", None)
    asset["ha_device_refs"] = []
    asset["warranty"] = {"type": "none", "until": None}
    manager = _manager(hass, asset_store_data)
    entry = _entry(hass)
    asset_device = _asset_device(device_registry, entry)
    entry.runtime_data = manager
    add_entities = Mock()

    await async_setup_entry(hass, entry, add_entities)

    add_entities.assert_called_once()
    entities = add_entities.call_args.args[0]
    lifecycle = next(
        entity for entity in entities if isinstance(entity, DeviceLifecycleSensor)
    )
    assert lifecycle.unique_id == lifecycle_unique_id(ASSET_UUID)
    assert lifecycle.device_entry.id == asset_device.id
    lifecycle.hass = hass
    assert lifecycle.native_value == "Warranty not specified"
    assert "purchase_uuid" not in lifecycle.extra_state_attributes
    assert len(entities) == 4


async def test_deployment_states_and_area_resolution_are_exact(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    area_registry: ar.AreaRegistry,
    asset_store_data,
) -> None:
    """Deployment exposes unknown/not-deployed/deployed and no name repair."""
    entry = _entry(hass)
    device = _asset_device(device_registry, entry)
    area = area_registry.async_create("Workshop")
    asset = deepcopy(asset_store_data["assets"][ASSET_UUID])
    sensor = DeviceDeploymentSensor(asset=asset, device_entry=device)
    sensor.hass = hass

    assert sensor.unique_id == deployment_unique_id(ASSET_UUID)
    assert sensor.device_class == SensorDeviceClass.ENUM
    assert sensor.options == ["unknown", "not_deployed", "deployed"]
    for state in (
        DEPLOYMENT_STATE_UNKNOWN,
        DEPLOYMENT_STATE_NOT_DEPLOYED,
        DEPLOYMENT_STATE_DEPLOYED,
    ):
        asset["deployment_state"] = state
        assert sensor.native_value == state

    asset["ha_area_id"] = None
    assert sensor.extra_state_attributes == {
        "installed_date": asset["installed_date"],
        "asset_area_id": None,
        "asset_area_name": None,
        "asset_area_state": "not_set",
    }
    asset["ha_area_id"] = area.id
    assert sensor.extra_state_attributes["asset_area_state"] == "present"
    assert sensor.extra_state_attributes["asset_area_name"] == "Workshop"
    asset["ha_area_id"] = "stale-workshop-id"
    stale = sensor.extra_state_attributes
    assert stale["asset_area_state"] == "missing"
    assert stale["asset_area_id"] == "stale-workshop-id"
    assert stale["asset_area_name"] is None
    assert asset["ha_area_id"] == "stale-workshop-id"


async def test_area_events_refresh_only_exact_deployment_projection(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    area_registry: ar.AreaRegistry,
    asset_store_data,
) -> None:
    """Rename and removal refresh the exact stored ID without remapping it."""
    entry = _entry(hass)
    device = _asset_device(device_registry, entry)
    area = area_registry.async_create("Workshop")
    unrelated = area_registry.async_create("Office")
    asset = deepcopy(asset_store_data["assets"][ASSET_UUID])
    asset["ha_area_id"] = area.id
    stored_area_id = asset["ha_area_id"]
    sensor = DeviceDeploymentSensor(asset=asset, device_entry=device)
    sensor.hass = hass
    sensor.async_write_ha_state = Mock()
    coordinator = _DeploymentAreaRegistryCoordinator(hass, [sensor])
    unsubscribe = coordinator.async_start()

    area_registry.async_update(unrelated.id, name="Unrelated rename")
    await hass.async_block_till_done()
    sensor.async_write_ha_state.assert_not_called()

    area_registry.async_update(area.id, name="Renamed workshop")
    await hass.async_block_till_done()
    sensor.async_write_ha_state.assert_called_once_with()
    assert sensor.extra_state_attributes["asset_area_name"] == "Renamed workshop"
    assert sensor.extra_state_attributes["asset_area_state"] == "present"
    assert asset["ha_area_id"] == stored_area_id == area.id

    sensor.async_write_ha_state.reset_mock()
    area_registry.async_delete(area.id)
    await hass.async_block_till_done()
    sensor.async_write_ha_state.assert_called_once_with()
    assert sensor.extra_state_attributes["asset_area_name"] is None
    assert sensor.extra_state_attributes["asset_area_state"] == "missing"
    assert asset["ha_area_id"] == stored_area_id
    unsubscribe()


async def test_area_listener_is_removed_with_config_entry_unload(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    area_registry: ar.AreaRegistry,
    asset_store_data,
) -> None:
    """The setup-scoped Area listener is unregistered during entry unload."""
    area = area_registry.async_create("Workshop")
    asset_store_data["assets"][ASSET_UUID]["ha_area_id"] = area.id
    manager = _manager(hass, asset_store_data)
    entry = _entry(hass)
    _asset_device(device_registry, entry)
    entry.runtime_data = manager
    add_entities = Mock()

    await async_setup_entry(hass, entry, add_entities)

    deployment = next(
        entity
        for entity in add_entities.call_args.args[0]
        if isinstance(entity, DeviceDeploymentSensor)
    )
    deployment.hass = hass
    deployment.async_write_ha_state = Mock()
    await entry._async_process_on_unload(hass)

    area_registry.async_update(area.id, name="After unload")
    await hass.async_block_till_done()
    deployment.async_write_ha_state.assert_not_called()


async def test_relationship_states_names_and_counts_are_deterministic(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    asset_store_data,
) -> None:
    """Exact primary/related IDs expose none, present, missing, and mixed state."""
    entry = _entry(hass)
    asset_device = _asset_device(device_registry, entry)
    primary = _external_device(
        hass,
        device_registry,
        identifier="primary",
        name="Primary name",
    )
    related = _external_device(
        hass,
        device_registry,
        identifier="related",
        name="Related name",
    )

    none_sensor = DeviceRelationshipsSensor(
        asset=_relationship_asset(asset_store_data, []),
        device_entry=asset_device,
        device_registry=device_registry,
    )
    assert none_sensor.native_value == "none"
    assert none_sensor.extra_state_attributes["primary_state"] == "not_linked"

    present_sensor = DeviceRelationshipsSensor(
        asset=_relationship_asset(
            asset_store_data,
            [
                {"device_id": primary.id, "role": "primary"},
                {"device_id": related.id, "role": "related"},
            ],
        ),
        device_entry=asset_device,
        device_registry=device_registry,
    )
    assert present_sensor.native_value == "present"
    present = present_sensor.extra_state_attributes
    assert present["primary_state"] == "present"
    assert present["primary_device_id"] == primary.id
    assert present["primary_device_name"] == "Primary name"
    assert present["related_devices"] == [
        {
            "device_id": related.id,
            "name": "Related name",
            "state": "present",
        }
    ]
    assert present["related_device_count"] == 1
    assert present["missing_related_device_count"] == 0

    mixed_sensor = DeviceRelationshipsSensor(
        asset=_relationship_asset(
            asset_store_data,
            [
                {"device_id": "missing-primary", "role": "primary"},
                {"device_id": related.id, "role": "related"},
                {"device_id": "missing-related", "role": "related"},
            ],
        ),
        device_entry=asset_device,
        device_registry=device_registry,
    )
    assert mixed_sensor.native_value == "missing"
    mixed = mixed_sensor.extra_state_attributes
    assert mixed["primary_state"] == "missing"
    assert mixed["primary_device_id"] == "missing-primary"
    assert mixed["related_device_count"] == 2
    assert mixed["missing_related_device_count"] == 1
    assert [item["state"] for item in mixed["related_devices"]] == [
        "present",
        "missing",
    ]


async def test_external_rename_and_removal_update_projection_only(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    asset_store_data,
) -> None:
    """Registry events refresh display projection without rewriting references."""
    entry = _entry(hass)
    asset_device = _asset_device(device_registry, entry)
    external = _external_device(
        hass,
        device_registry,
        identifier="rename-remove",
        name="Original external name",
    )
    asset = _relationship_asset(
        asset_store_data,
        [{"device_id": external.id, "role": "related"}],
    )
    stored_before = deepcopy(asset["ha_device_refs"])
    sensor = DeviceRelationshipsSensor(
        asset=asset,
        device_entry=asset_device,
        device_registry=device_registry,
    )
    sensor.hass = hass
    sensor.async_write_ha_state = Mock()
    coordinator = _RelationshipsRegistryCoordinator(hass, [sensor])
    unsubscribe = coordinator.async_start()

    device_registry.async_update_device(external.id, name="Renamed externally")
    assert sensor.extra_state_attributes["related_devices"][0]["name"] == (
        "Renamed externally"
    )
    assert sensor.async_write_ha_state.called
    device_registry.async_remove_device(external.id)
    assert sensor.native_value == "missing"
    assert sensor.extra_state_attributes["related_devices"][0] == {
        "device_id": external.id,
        "name": None,
        "state": "missing",
    }
    assert asset["ha_device_refs"] == stored_before
    unsubscribe()


def test_asset_id_sensor_is_stable_enabled_diagnostic(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    asset_store_data,
) -> None:
    """Asset ID exposes DLxxxx through its dedicated diagnostic entity."""
    entry = _entry(hass)
    device = _asset_device(device_registry, entry)
    asset = asset_store_data["assets"][ASSET_UUID]
    sensor = DeviceAssetIdSensor(asset=asset, device_entry=device)

    assert sensor.unique_id == asset_id_unique_id(ASSET_UUID)
    assert sensor.native_value == "DL0007"
    assert sensor.entity_category == EntityCategory.DIAGNOSTIC
    assert sensor.entity_registry_enabled_default is True
    assert sensor.has_entity_name is True
    assert sensor.translation_key == "asset_id"


def test_relationship_sensor_is_enabled_diagnostic_with_stable_id(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    asset_store_data,
) -> None:
    """Relationships is a normal enabled diagnostic projection."""
    entry = _entry(hass)
    device = _asset_device(device_registry, entry)
    sensor = DeviceRelationshipsSensor(
        asset=asset_store_data["assets"][ASSET_UUID],
        device_entry=device,
        device_registry=device_registry,
    )

    assert sensor.unique_id == relationships_unique_id(ASSET_UUID)
    assert sensor.entity_category == EntityCategory.DIAGNOSTIC
    assert sensor.entity_registry_enabled_default is True
    assert sensor.has_entity_name is True
    assert sensor.translation_key == "relationships"
