"""OptionsFlow tests for existing Home Assistant device relationships."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock

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
    form = await flow.async_step_ha_relationship()

    assert "ha_relationship" in menu["menu_options"]
    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "ha_relationship"
    assert isinstance(_schema_validator(form, CONF_DEVICE_ID), selector.DeviceSelector)
    assert form["description_placeholders"]["current_device"] == "No HA device"


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

    result = await flow.async_step_ha_relationship({CONF_DEVICE_ID: device.id})

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

    await flow.async_step_ha_relationship({CONF_DEVICE_ID: device.id})

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

    result = await flow.async_step_ha_relationship(
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

    result = await flow.async_step_ha_relationship({CONF_DEVICE_ID: device.id})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "device_already_linked"}
    assert result["description_placeholders"]["owner_asset_id"] == owner["asset_id"]
    assert manager.asset(candidate["asset_uuid"])["ha_device_refs"] == []
    assert manager.asset_for_device_id(device.id)["asset_uuid"] == owner["asset_uuid"]
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
        result = await flow.async_step_ha_relationship(
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

    form = await flow.async_step_ha_relationship()

    assert form["type"] is FlowResultType.FORM
    assert "Unavailable" in form["description_placeholders"]["current_device"]
    assert before["ha_device_refs"][0]["device_id"] in form[
        "description_placeholders"
    ]["current_device"]
    assert manager.asset(ASSET_UUID)["ha_device_refs"] == before["ha_device_refs"]

    result = await flow.async_step_ha_relationship(
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

    result = await flow.async_step_ha_relationship(
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

    unlink = await flow.async_step_ha_relationship(
        {CONF_HA_RELATIONSHIP_ACTION: HA_RELATIONSHIP_ACTION_UNLINK}
    )
    replace = await flow.async_step_ha_relationship(
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

    result = await flow.async_step_ha_relationship(
        {
            CONF_HA_RELATIONSHIP_ACTION: HA_RELATIONSHIP_ACTION_REPLACE,
            CONF_DEVICE_ID: new.id,
        }
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "asset_store_error"}
    assert manager._data == before
    assert manager.asset_for_device_id(old.id)["asset_uuid"] == asset["asset_uuid"]
    assert manager.asset_for_device_id(new.id) is None


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
        {"device_id": device.id, "role": "primary"}
    ]
    if subentry_type == SUBENTRY_TYPE_PURCHASE:
        purchase = manager.purchases()[0]
        assert purchase["asset_uuids"] == [asset["asset_uuid"]]
    else:
        runtime_subentry = next(iter(parent_entry.subentries.values()))
        assert runtime_subentry.data[CONF_ASSET_UUID] == asset["asset_uuid"]
