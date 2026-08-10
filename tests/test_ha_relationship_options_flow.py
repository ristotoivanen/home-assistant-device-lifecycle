"""OptionsFlow tests for existing Home Assistant device relationships."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, PropertyMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle.config_flow import (
    DeviceLifecycleOptionsFlow,
)
from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_CURRENCY,
    CONF_DEPLOYMENT_STATE,
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
    CONF_HA_AREA_ID,
    CONF_HA_RELATIONSHIP_ACTION,
    CONF_INSTALLED_DATE,
    CONF_RUNTIME_MODE,
    CONF_SOURCE_ENTITY_ID,
    CONF_WARRANTY_TYPE,
    DEPLOYMENT_STATE_DEPLOYED,
    DOMAIN,
    HA_RELATIONSHIP_ACTION_REPLACE,
    HA_RELATIONSHIP_ACTION_UNLINK,
    RUNTIME_MODE_ON,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
    WARRANTY_NONE,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import ASSET_UUID
from .test_options_flow import _manager, _options_flow, _store_with_purchase


def _external_device(
    hass: HomeAssistant,
    registry: dr.DeviceRegistry,
    *,
    key: str,
    domain: str = "hue",
    area_id: str | None = None,
    entry_type: dr.DeviceEntryType | None = None,
    name: str | None = None,
    manufacturer: str | None = "External manufacturer",
    model: str | None = "External model",
    model_id: str | None = "EXT-1",
    serial_number: str | None = "EXT-SERIAL",
    sw_version: str | None = "9.1",
    hw_version: str | None = "B",
) -> tuple[MockConfigEntry, dr.DeviceEntry]:
    """Create one external registry device with inspectable ownership data."""
    entry = MockConfigEntry(
        domain=domain,
        title=f"{domain} owner {key}",
        data={"stable": key},
    )
    entry.add_to_hass(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(domain, key)},
        connections={("test_connection", key)},
        entry_type=entry_type,
        name=name or f"External device {key}",
        manufacturer=manufacturer,
        model=model,
        model_id=model_id,
        serial_number=serial_number,
        sw_version=sw_version,
        hw_version=hw_version,
    )
    if area_id is not None:
        updated = registry.async_update_device(device.id, area_id=area_id)
        assert updated is not None
        device = updated
    return entry, device


def _device_snapshot(device: dr.DeviceEntry) -> dict:
    """Return registry fields Device Lifecycle must never mutate."""
    return {
        "config_entry_id": device.config_entry_id,
        "identifiers": set(device.identifiers),
        "connections": set(device.connections),
        "area_id": device.area_id,
        "name": device.name,
        "name_by_user": device.name_by_user,
    }


async def _select_asset(
    flow: DeviceLifecycleOptionsFlow,
    asset_uuid: str,
) -> dict:
    """Position one OptionsFlow on an Asset's management menu."""
    return await flow.async_step_manage_asset({CONF_ASSET_UUID: asset_uuid})


def _schema_validator(result: dict, field: str):
    """Return the selector validator for a form field."""
    for marker, validator in result["data_schema"].schema.items():
        if getattr(marker, "schema", marker) == field:
            return validator
    raise AssertionError(f"Missing field {field}")


def _subentry_data(subentry_type: str, device_id: str) -> dict:
    """Return one active dependency/reconciliation config subentry."""
    if subentry_type == SUBENTRY_TYPE_PURCHASE:
        data = {
            CONF_DEVICE_IDS: [device_id],
            CONF_CURRENCY: "EUR",
            CONF_WARRANTY_TYPE: WARRANTY_NONE,
        }
    else:
        data = {
            CONF_DEVICE_ID: device_id,
            CONF_RUNTIME_MODE: RUNTIME_MODE_ON,
            CONF_SOURCE_ENTITY_ID: "switch.runtime_source",
        }
    return {
        "data": data,
        "subentry_type": subentry_type,
        "title": f"Active {subentry_type}",
        "unique_id": None,
    }


async def test_ha_relationship_action_is_visible_and_uses_device_selector(
    hass: HomeAssistant,
) -> None:
    """Manage Asset exposes the relationship inspector and device selector."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Shelf bulb")
    flow, _entry = _options_flow(hass, manager)

    menu = await _select_asset(flow, asset["asset_uuid"])
    overview = await flow.async_step_ha_relationship()
    form = await flow.async_step_manage_primary_device()

    assert "ha_relationship" in menu["menu_options"]
    assert overview["type"] is FlowResultType.MENU
    assert overview["step_id"] == "ha_relationship"
    assert overview["menu_options"] == [
        "manage_primary_device",
        "add_related_device",
    ]
    assert overview["description_placeholders"]["current_primary"] == (
        "No primary Home Assistant device"
    )
    assert overview["description_placeholders"]["related_devices"] == (
        "No related Home Assistant devices"
    )
    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "manage_primary_device"
    assert isinstance(_schema_validator(form, CONF_DEVICE_ID), selector.DeviceSelector)
    assert form["description_placeholders"]["current_device"] == (
        "No primary Home Assistant device"
    )


async def test_manual_asset_links_without_identity_or_registry_mutation(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """A manual Asset links by relationship while all external state is stable."""
    device_area = area_registry.async_create("External device Area")
    asset_area = area_registry.async_create("Asset deployment Area")
    external_entry, device = _external_device(
        hass,
        device_registry,
        key="manual-link",
        area_id=device_area.id,
    )
    manager = _manager(hass, _store_with_purchase())
    asset = await manager.async_create_manual_asset(
        name="User shelf bulb",
        manufacturer="User manufacturer",
        model="User model",
    )
    await manager.async_update_asset_metadata(
        asset["asset_uuid"],
        serial_number=None,
        notes="User notes",
    )
    await manager.async_set_asset_purchase(
        asset["asset_uuid"],
        next(iter(manager._data["purchases"])),
    )
    await manager.async_set_asset_deployment(
        asset["asset_uuid"],
        deployment_state=DEPLOYMENT_STATE_DEPLOYED,
        installed_date="2026-08-10",
        ha_area_id=asset_area.id,
    )
    before = manager.asset(asset["asset_uuid"])
    registry_before = _device_snapshot(device)
    entry_data_before = dict(external_entry.data)
    manager._store.async_save.reset_mock()
    flow, parent_entry = _options_flow(hass, manager)
    await _select_asset(flow, asset["asset_uuid"])

    result = await flow.async_step_manage_primary_device({CONF_DEVICE_ID: device.id})

    updated = manager.asset(asset["asset_uuid"])
    registry_after = device_registry.async_get(device.id)
    assert registry_after is not None
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert updated["ha_device_refs"] == [
        {"device_id": device.id, "role": "primary"}
    ]
    assert updated["asset_uuid"] == before["asset_uuid"]
    assert updated["asset_id"] == before["asset_id"]
    assert updated["purchase_uuid"] == before["purchase_uuid"]
    assert updated[CONF_DEPLOYMENT_STATE] == before[CONF_DEPLOYMENT_STATE]
    assert updated[CONF_INSTALLED_DATE] == before[CONF_INSTALLED_DATE]
    assert updated[CONF_HA_AREA_ID] == before[CONF_HA_AREA_ID]
    assert updated["name"] == before["name"] == "User shelf bulb"
    assert updated["manufacturer"] == before["manufacturer"]
    assert updated["model"] == before["model"]
    assert updated["serial_number"] is None
    assert updated["notes"] == "User notes"
    assert updated["field_sources"]["serial_number"] == "user"
    assert _device_snapshot(registry_after) == registry_before
    assert external_entry.data == entry_data_before
    assert registry_after.config_entry_id == external_entry.entry_id
    assert registry_after.config_entry_id != parent_entry.entry_id
    manager._store.async_save.assert_awaited_once()


async def test_non_user_metadata_refreshes_but_user_fields_remain_owned(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Link refresh follows existing provenance rules, including cleared fields."""
    _entry, device = _external_device(
        hass,
        device_registry,
        key="metadata-refresh",
        name="HA refreshed name",
        manufacturer="HA refreshed manufacturer",
        model="HA replacement model",
        serial_number="HA-SERIAL",
    )
    data = deepcopy(asset_store_data)
    stored = data["assets"][ASSET_UUID]
    stored["ha_device_refs"] = []
    stored["name"] = "Old HA name"
    stored["manufacturer"] = "Old HA manufacturer"
    stored["model"] = "User model"
    stored["serial_number"] = None
    stored["field_sources"].update(
        {
            "name": "home_assistant",
            "manufacturer": "home_assistant",
            "model": "user",
            "serial_number": "user",
        }
    )
    manager = _manager(hass, data)
    flow, _parent = _options_flow(hass, manager)
    await _select_asset(flow, ASSET_UUID)

    await flow.async_step_manage_primary_device({CONF_DEVICE_ID: device.id})

    updated = manager.asset(ASSET_UUID)
    assert updated["name"] == "HA refreshed name"
    assert updated["manufacturer"] == "HA refreshed manufacturer"
    assert updated["field_sources"]["name"] == "home_assistant"
    assert updated["field_sources"]["manufacturer"] == "home_assistant"
    assert updated["model"] == "User model"
    assert updated["serial_number"] is None
    assert updated["field_sources"]["model"] == "user"
    assert updated["field_sources"]["serial_number"] == "user"


async def test_repeated_same_device_link_is_idempotent(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Selecting the same primary device changes no identity and consumes no save."""
    _entry, device = _external_device(
        hass,
        device_registry,
        key="idempotent",
    )
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Idempotent Asset")
    await manager.async_link_asset_device(
        asset["asset_uuid"],
        device.id,
        device=device,
    )
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()
    flow, _parent = _options_flow(hass, manager)
    await _select_asset(flow, asset["asset_uuid"])

    result = await flow.async_step_manage_primary_device(
        {
            CONF_HA_RELATIONSHIP_ACTION: HA_RELATIONSHIP_ACTION_REPLACE,
            CONF_DEVICE_ID: device.id,
        }
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert manager._data == before
    assert manager.asset_count == 1
    manager._store.async_save.assert_not_awaited()


async def test_device_primary_for_another_asset_is_rejected_with_dl_id(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """One operational device cannot be primary for two physical Assets."""
    _entry, device = _external_device(
        hass,
        device_registry,
        key="already-owned",
    )
    manager = _manager(hass)
    owner = await manager.async_create_manual_asset(name="Owner")
    candidate = await manager.async_create_manual_asset(name="Candidate")
    await manager.async_link_asset_device(owner["asset_uuid"], device.id)
    manager._store.async_save.reset_mock()
    flow, _parent = _options_flow(hass, manager)
    await _select_asset(flow, candidate["asset_uuid"])

    result = await flow.async_step_manage_primary_device({CONF_DEVICE_ID: device.id})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "device_already_linked"}
    assert result["description_placeholders"]["owner_asset_id"] == owner["asset_id"]
    assert manager.asset(candidate["asset_uuid"])["ha_device_refs"] == []
    assert manager.asset_for_primary_device_id(device.id)["asset_uuid"] == owner["asset_uuid"]
    manager._store.async_save.assert_not_awaited()


async def test_missing_service_excluded_and_own_devices_are_rejected(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Server validation enforces the conservative physical-device boundary."""
    _service_entry, service = _external_device(
        hass,
        device_registry,
        key="service",
        entry_type=dr.DeviceEntryType.SERVICE,
    )
    _excluded_entry, excluded = _external_device(
        hass,
        device_registry,
        key="excluded",
        domain="hassio",
    )
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Validation Asset")
    flow, parent_entry = _options_flow(hass, manager)
    own_device = device_registry.async_get_or_create(
        config_entry_id=parent_entry.entry_id,
        identifiers={(DOMAIN, "owned-device")},
        name="Device Lifecycle owned",
    )
    await _select_asset(flow, asset["asset_uuid"])

    for device_id, expected_error in (
        ("missing-device-id", "device_missing"),
        (service.id, "service_device_not_allowed"),
        (excluded.id, "non_physical_device_not_allowed"),
        (own_device.id, "device_lifecycle_device_not_allowed"),
    ):
        result = await flow.async_step_manage_primary_device(
            {CONF_DEVICE_ID: device_id}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": expected_error}
        assert manager.asset(asset["asset_uuid"])["ha_device_refs"] == []


async def test_missing_stored_device_is_displayed_and_can_be_unlinked(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A stale device ID is preserved for inspection until explicit unlink."""
    manager = _manager(hass, asset_store_data)
    before = manager.asset(ASSET_UUID)
    flow, _parent = _options_flow(hass, manager)
    await _select_asset(flow, ASSET_UUID)

    form = await flow.async_step_manage_primary_device()

    assert form["type"] is FlowResultType.FORM
    assert "Unavailable" in form["description_placeholders"]["current_device"]
    assert before["ha_device_refs"][0]["device_id"] in form[
        "description_placeholders"
    ]["current_device"]
    assert manager.asset(ASSET_UUID)["ha_device_refs"] == before["ha_device_refs"]

    result = await flow.async_step_manage_primary_device(
        {CONF_HA_RELATIONSHIP_ACTION: HA_RELATIONSHIP_ACTION_UNLINK}
    )

    updated = manager.asset(ASSET_UUID)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert updated["ha_device_refs"] == []
    assert updated["asset_uuid"] == before["asset_uuid"]
    assert updated["asset_id"] == before["asset_id"]
    assert updated["purchase_uuid"] == before["purchase_uuid"]


async def test_dependency_free_replacement_is_one_atomic_save(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Replacement removes the old primary and adds the new one in one save."""
    first_area = area_registry.async_create("First device Area")
    second_area = area_registry.async_create("Second device Area")
    _first_entry, first = _external_device(
        hass,
        device_registry,
        key="replace-first",
        area_id=first_area.id,
    )
    _second_entry, second = _external_device(
        hass,
        device_registry,
        key="replace-second",
        area_id=second_area.id,
    )
    first_before = _device_snapshot(first)
    second_before = _device_snapshot(second)
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Replace Asset")
    await manager.async_link_asset_device(asset["asset_uuid"], first.id)
    before = manager.asset(asset["asset_uuid"])
    manager._store.async_save.reset_mock()
    flow, _parent = _options_flow(hass, manager)
    await _select_asset(flow, asset["asset_uuid"])

    result = await flow.async_step_manage_primary_device(
        {
            CONF_HA_RELATIONSHIP_ACTION: HA_RELATIONSHIP_ACTION_REPLACE,
            CONF_DEVICE_ID: second.id,
        }
    )

    updated = manager.asset(asset["asset_uuid"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert updated["ha_device_refs"] == [
        {"device_id": second.id, "role": "primary"}
    ]
    assert updated["asset_uuid"] == before["asset_uuid"]
    assert updated["asset_id"] == before["asset_id"]
    assert updated["purchase_uuid"] == before["purchase_uuid"]
    manager._store.async_save.assert_awaited_once()
    assert _device_snapshot(device_registry.async_get(first.id)) == first_before
    assert _device_snapshot(device_registry.async_get(second.id)) == second_before


@pytest.mark.parametrize(
    ("subentry_type", "expected_error"),
    [
        (SUBENTRY_TYPE_PURCHASE, "ha_device_purchase_dependency"),
        (SUBENTRY_TYPE_RUNTIME, "ha_device_runtime_dependency"),
    ],
)
async def test_active_dependency_blocks_unlink_and_replacement(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    subentry_type: str,
    expected_error: str,
) -> None:
    """Active Purchase and Runtime references are never rewritten implicitly."""
    _old_entry, old = _external_device(
        hass,
        device_registry,
        key=f"dependency-old-{subentry_type}",
    )
    _new_entry, new = _external_device(
        hass,
        device_registry,
        key=f"dependency-new-{subentry_type}",
    )
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Dependent Asset")
    await manager.async_link_asset_device(asset["asset_uuid"], old.id)
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()
    flow, _parent = _options_flow(
        hass,
        manager,
        subentries_data=(_subentry_data(subentry_type, old.id),),
    )
    await _select_asset(flow, asset["asset_uuid"])

    unlink = await flow.async_step_manage_primary_device(
        {CONF_HA_RELATIONSHIP_ACTION: HA_RELATIONSHIP_ACTION_UNLINK}
    )
    replace = await flow.async_step_manage_primary_device(
        {
            CONF_HA_RELATIONSHIP_ACTION: HA_RELATIONSHIP_ACTION_REPLACE,
            CONF_DEVICE_ID: new.id,
        }
    )

    assert unlink["errors"] == {"base": expected_error}
    assert replace["errors"] == {"base": expected_error}
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()


async def test_failed_replacement_keeps_old_relationship(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """An atomic save failure cannot publish a half-replaced relationship."""
    _old_entry, old = _external_device(
        hass,
        device_registry,
        key="failed-old",
    )
    _new_entry, new = _external_device(
        hass,
        device_registry,
        key="failed-new",
    )
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Failure Asset")
    await manager.async_link_asset_device(asset["asset_uuid"], old.id)
    before = deepcopy(manager._data)
    manager._store.async_save = AsyncMock(side_effect=OSError("disk unavailable"))
    flow, _parent = _options_flow(hass, manager)
    await _select_asset(flow, asset["asset_uuid"])

    result = await flow.async_step_manage_primary_device(
        {
            CONF_HA_RELATIONSHIP_ACTION: HA_RELATIONSHIP_ACTION_REPLACE,
            CONF_DEVICE_ID: new.id,
        }
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "asset_store_error"}
    assert manager._data == before
    assert manager.asset_for_primary_device_id(old.id)["asset_uuid"] == asset["asset_uuid"]
    assert manager.asset_for_primary_device_id(new.id) is None


@pytest.mark.parametrize(
    "subentry_type",
    [SUBENTRY_TYPE_PURCHASE, SUBENTRY_TYPE_RUNTIME],
)
async def test_later_reconciliation_resolves_same_linked_asset(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    subentry_type: str,
) -> None:
    """Purchase and Runtime reconciliation reuse the UI-linked Asset identity."""
    _external_entry, device = _external_device(
        hass,
        device_registry,
        key=f"reconcile-{subentry_type}",
    )
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Reconcile Asset")
    await manager.async_link_asset_device(asset["asset_uuid"], device.id)
    related_device_id = f"related-{subentry_type}"
    await manager.async_add_related_device(
        asset["asset_uuid"],
        related_device_id,
    )
    counter_before = manager._data["next_asset_number"]
    flow, parent_entry = _options_flow(
        hass,
        manager,
        subentries_data=(_subentry_data(subentry_type, device.id),),
    )
    del flow

    await manager.async_reconcile_entry(parent_entry)

    updated = manager.asset(asset["asset_uuid"])
    assert manager.asset_count == 1
    assert manager._data["next_asset_number"] == counter_before
    assert updated["asset_uuid"] == asset["asset_uuid"]
    assert updated["asset_id"] == asset["asset_id"]
    assert updated["ha_device_refs"] == [
        {"device_id": device.id, "role": "primary"},
        {"device_id": related_device_id, "role": "related"},
    ]
    if subentry_type == SUBENTRY_TYPE_PURCHASE:
        purchase = manager.purchases()[0]
        assert purchase["asset_uuids"] == [asset["asset_uuid"]]
    else:
        runtime_subentry = next(iter(parent_entry.subentries.values()))
        assert runtime_subentry.data[CONF_ASSET_UUID] == asset["asset_uuid"]


async def test_relationship_overview_displays_primary_related_and_stale_refs(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Overview names current devices and never hides stale stored IDs."""
    _primary_entry, primary = _external_device(
        hass,
        device_registry,
        key="overview-primary",
    )
    _related_entry, related = _external_device(
        hass,
        device_registry,
        key="overview-related",
    )
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Overview")
    await manager.async_link_asset_device(asset["asset_uuid"], primary.id)
    await manager.async_add_related_device(asset["asset_uuid"], related.id)
    await manager.async_add_related_device(asset["asset_uuid"], "stale-related")
    flow, _parent = _options_flow(hass, manager)
    await _select_asset(flow, asset["asset_uuid"])

    overview = await flow.async_step_ha_relationship()

    assert overview["type"] is FlowResultType.MENU
    assert overview["menu_options"] == [
        "manage_primary_device",
        "add_related_device",
        "remove_related_device",
    ]
    placeholders = overview["description_placeholders"]
    assert primary.id in placeholders["current_primary"]
    assert primary.name in placeholders["current_primary"]
    assert related.id in placeholders["related_devices"]
    assert related.name in placeholders["related_devices"]
    assert "stale-related" in placeholders["related_devices"]
    assert "Unavailable" in placeholders["related_devices"]


async def test_add_related_uses_device_selector_without_metadata_or_registry_writes(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Related add is validated but has no operational or ownership effects."""
    external_entry, device = _external_device(
        hass,
        device_registry,
        key="add-related",
    )
    registry_before = _device_snapshot(device)
    entry_data_before = dict(external_entry.data)
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(
        name="User Asset",
        manufacturer="User manufacturer",
        model="User model",
    )
    before = manager.asset(asset["asset_uuid"])
    manager._store.async_save.reset_mock()
    flow, _parent = _options_flow(
        hass,
        manager,
        subentries_data=(
            _subentry_data(SUBENTRY_TYPE_PURCHASE, device.id),
            _subentry_data(SUBENTRY_TYPE_RUNTIME, device.id),
        ),
    )
    await _select_asset(flow, asset["asset_uuid"])

    form = await flow.async_step_add_related_device()
    result = await flow.async_step_add_related_device(
        {CONF_DEVICE_ID: device.id}
    )

    updated = manager.asset(asset["asset_uuid"])
    current_device = device_registry.async_get(device.id)
    assert form["type"] is FlowResultType.FORM
    assert isinstance(_schema_validator(form, CONF_DEVICE_ID), selector.DeviceSelector)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert updated["ha_device_refs"] == [
        {"device_id": device.id, "role": "related"}
    ]
    for field in (
        "asset_uuid",
        "asset_id",
        "name",
        "manufacturer",
        "model",
        "purchase_uuid",
        CONF_DEPLOYMENT_STATE,
        CONF_INSTALLED_DATE,
        CONF_HA_AREA_ID,
        "field_sources",
    ):
        assert updated[field] == before[field]
    assert _device_snapshot(current_device) == registry_before
    assert external_entry.data == entry_data_before
    manager._store.async_save.assert_awaited_once()


async def test_related_add_allows_primary_owner_elsewhere_but_not_same_asset(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Related is non-exclusive, while one Asset cannot duplicate its primary."""
    _entry, device = _external_device(
        hass,
        device_registry,
        key="related-primary-owner",
    )
    manager = _manager(hass)
    related_asset = await manager.async_create_manual_asset(name="Related Asset")
    primary_asset = await manager.async_create_manual_asset(name="Primary Asset")
    await manager.async_link_asset_device(primary_asset["asset_uuid"], device.id)
    flow, _parent = _options_flow(hass, manager)
    await _select_asset(flow, related_asset["asset_uuid"])

    allowed = await flow.async_step_add_related_device(
        {CONF_DEVICE_ID: device.id}
    )

    assert allowed["type"] is FlowResultType.CREATE_ENTRY
    assert manager.asset(related_asset["asset_uuid"])["ha_device_refs"] == [
        {"device_id": device.id, "role": "related"}
    ]

    await _select_asset(flow, primary_asset["asset_uuid"])
    rejected = await flow.async_step_add_related_device(
        {CONF_DEVICE_ID: device.id}
    )

    assert rejected["type"] is FlowResultType.FORM
    assert rejected["errors"] == {"base": "related_device_is_primary"}
    assert manager.asset(primary_asset["asset_uuid"])["ha_device_refs"] == [
        {"device_id": device.id, "role": "primary"}
    ]


async def test_remove_related_uses_stored_options_and_removes_stale_ref(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Removal uses stored references so missing devices remain repairable."""
    _entry, current = _external_device(
        hass,
        device_registry,
        key="remove-current-related",
    )
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Remove stale")
    await manager.async_add_related_device(asset["asset_uuid"], current.id)
    await manager.async_add_related_device(asset["asset_uuid"], "missing-related")
    flow, _parent = _options_flow(hass, manager)
    await _select_asset(flow, asset["asset_uuid"])

    form = await flow.async_step_remove_related_device()
    validator = _schema_validator(form, CONF_DEVICE_ID)
    options = list(validator.config["options"])
    result = await flow.async_step_remove_related_device(
        {CONF_DEVICE_ID: "missing-related"}
    )

    assert isinstance(validator, selector.SelectSelector)
    assert {option["value"] for option in options} == {
        current.id,
        "missing-related",
    }
    stale_option = next(
        option for option in options if option["value"] == "missing-related"
    )
    assert "missing-related" in stale_option["label"]
    assert "Unavailable" in stale_option["label"]
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert manager.asset(asset["asset_uuid"])["ha_device_refs"] == [
        {"device_id": current.id, "role": "related"}
    ]


async def test_related_device_can_be_promoted_in_primary_flow(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Primary replacement promotes the target without demoting the old primary."""
    _old_entry, old = _external_device(
        hass,
        device_registry,
        key="promote-old",
    )
    _new_entry, new = _external_device(
        hass,
        device_registry,
        key="promote-new",
    )
    _keep_entry, keep = _external_device(
        hass,
        device_registry,
        key="promote-keep",
    )
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Promote UI")
    await manager.async_link_asset_device(asset["asset_uuid"], old.id)
    await manager.async_add_related_device(asset["asset_uuid"], new.id)
    await manager.async_add_related_device(asset["asset_uuid"], keep.id)
    manager._store.async_save.reset_mock()
    flow, _parent = _options_flow(hass, manager)
    await _select_asset(flow, asset["asset_uuid"])

    result = await flow.async_step_manage_primary_device(
        {
            CONF_HA_RELATIONSHIP_ACTION: HA_RELATIONSHIP_ACTION_REPLACE,
            CONF_DEVICE_ID: new.id,
        }
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert manager.asset(asset["asset_uuid"])["ha_device_refs"] == [
        {"device_id": new.id, "role": "primary"},
        {"device_id": keep.id, "role": "related"},
    ]
    manager._store.async_save.assert_awaited_once()


async def test_related_target_validation_uses_single_config_entry_owner(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """HA 2026.8 validation does not read deprecated multi-owner properties."""
    _entry, device = _external_device(
        hass,
        device_registry,
        key="single-owner-api",
    )
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Single owner")
    flow, _parent = _options_flow(hass, manager)
    await _select_asset(flow, asset["asset_uuid"])

    with patch.object(
        dr.DeviceEntry,
        "config_entries",
        new_callable=PropertyMock,
        side_effect=AssertionError("deprecated config_entries was read"),
    ):
        result = await flow.async_step_add_related_device(
            {CONF_DEVICE_ID: device.id}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_related_add_enforces_all_primary_target_safety_checks(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """DeviceSelector filtering never replaces server-side validation."""
    _service_entry, service = _external_device(
        hass,
        device_registry,
        key="related-service",
        entry_type=dr.DeviceEntryType.SERVICE,
    )
    _excluded_entry, excluded = _external_device(
        hass,
        device_registry,
        key="related-excluded",
        domain="hassio",
    )
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Related validation")
    flow, parent_entry = _options_flow(hass, manager)
    own_device = device_registry.async_get_or_create(
        config_entry_id=parent_entry.entry_id,
        identifiers={(DOMAIN, "related-owned")},
        name="Device Lifecycle owned",
    )
    await _select_asset(flow, asset["asset_uuid"])

    for device_id, expected_error in (
        ("missing-related-id", "device_missing"),
        (service.id, "service_device_not_allowed"),
        (excluded.id, "non_physical_device_not_allowed"),
        (own_device.id, "device_lifecycle_device_not_allowed"),
    ):
        result = await flow.async_step_add_related_device(
            {CONF_DEVICE_ID: device_id}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": expected_error}

    assert manager.asset(asset["asset_uuid"])["ha_device_refs"] == []


@pytest.mark.parametrize(
    "subentry_type",
    [SUBENTRY_TYPE_PURCHASE, SUBENTRY_TYPE_RUNTIME],
)
async def test_reconciliation_uses_primary_only_and_preserves_related_identity(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    subentry_type: str,
) -> None:
    """A related reference is neither identity nor a reconciliation fallback."""
    _external_entry, device = _external_device(
        hass,
        device_registry,
        key=f"related-not-identity-{subentry_type}",
    )
    manager = _manager(hass)
    related_asset = await manager.async_create_manual_asset(
        name="Reference-only Asset"
    )
    await manager.async_add_related_device(
        related_asset["asset_uuid"],
        device.id,
    )
    flow, parent_entry = _options_flow(
        hass,
        manager,
        subentries_data=(_subentry_data(subentry_type, device.id),),
    )
    del flow

    await manager.async_reconcile_entry(parent_entry)
    first_snapshot = deepcopy(manager._data)
    await manager.async_reconcile_entry(parent_entry)

    operational_asset = manager.asset_for_primary_device_id(device.id)
    assert operational_asset is not None
    assert operational_asset["asset_uuid"] != related_asset["asset_uuid"]
    assert operational_asset["ha_device_refs"] == [
        {"device_id": device.id, "role": "primary"}
    ]
    assert manager.asset(related_asset["asset_uuid"])["ha_device_refs"] == [
        {"device_id": device.id, "role": "related"}
    ]
    assert manager.asset_count == 2
    assert manager._data == first_snapshot

    if subentry_type == SUBENTRY_TYPE_PURCHASE:
        assert manager.purchases()[0]["asset_uuids"] == [
            operational_asset["asset_uuid"]
        ]
    else:
        runtime_subentry = next(iter(parent_entry.subentries.values()))
        assert runtime_subentry.data[CONF_ASSET_UUID] == operational_asset[
            "asset_uuid"
        ]
