"""The two submenus reached from the Asset hub, and where their work lands.

Replacement and Home Assistant devices are places somebody stays in for a
while: record a replacement, then correct it; set a primary device, then
add a related one. These tests own that each operation returns to the
submenu it was started from, and that the Back row is the only way out.
"""

from __future__ import annotations

from copy import deepcopy

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr

from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_CONFIRM_VOID,
    CONF_DEVICE_ID,
    CONF_EFFECTIVE_DATE,
    CONF_HA_RELATIONSHIP_ACTION,
    CONF_NOTES,
    CONF_PREDECESSOR_ASSET_UUID,
    CONF_REPLACEMENT_ACTION,
    CONF_REPLACEMENT_REASON,
    CONF_REPLACEMENT_TARGET_ASSET_UUID,
    CONF_REPLACEMENT_UUID,
    CONF_SUCCESSOR_ASSET_UUID,
    CONF_VOID_REASON,
    HA_RELATIONSHIP_ACTION_REPLACE,
    REPLACEMENT_ACTION_CORRECT,
    REPLACEMENT_ACTION_VOID,
)
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import capture_reloads
from .test_ha_relationship_options_flow import _external_device
from .test_options_flow import _manager, _options_flow

BACK_ROW = "manage_asset_menu"
HUB_STEP = "manage_asset_menu"
REPLACEMENT_STEP = "asset_replacement"
HA_STEP = "ha_relationship"


async def _on_asset(
    hass: HomeAssistant,
    manager: AssetStoreManager,
    asset_uuid: str,
):
    """Return a flow sitting on one Asset's hub."""
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset_uuid})
    return flow


async def _replacement_pair(manager: AssetStoreManager):
    """Return two Assets and an active replacement record between them."""
    old = await manager.async_create_manual_asset(name="Old unit")
    new = await manager.async_create_manual_asset(name="New unit")
    record = await manager.async_create_asset_replacement(
        old["asset_uuid"],
        new["asset_uuid"],
        reason="failure",
        effective_date=None,
        notes=None,
    )
    return old, new, record


async def test_replacement_submenu_lists_its_operations_then_back(
    hass: HomeAssistant,
) -> None:
    """Back is the last row, after whatever operations currently apply."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Fresh unit")
    flow = await _on_asset(hass, manager, asset["asset_uuid"])

    empty = await flow.async_step_asset_replacement()

    assert empty["type"] is FlowResultType.MENU
    assert empty["menu_options"] == [
        "replacement_replaces",
        "replacement_replaced_by",
        BACK_ROW,
    ]

    old, _new, _record = await _replacement_pair(manager)
    populated_flow = await _on_asset(hass, manager, old["asset_uuid"])
    populated = await populated_flow.async_step_asset_replacement()

    assert populated["menu_options"] == [
        "replacement_replaces",
        "replacement_replaced_by",
        "manage_asset_replacement",
        BACK_ROW,
    ]


async def test_ha_submenu_lists_its_operations_then_back(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Back is the last row here too, after the related-device row appears."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Linked unit")
    flow = await _on_asset(hass, manager, asset["asset_uuid"])

    bare = await flow.async_step_ha_relationship()

    assert bare["type"] is FlowResultType.MENU
    assert bare["menu_options"] == [
        "manage_primary_device",
        "add_related_device",
        BACK_ROW,
    ]

    _owner, related = _external_device(
        hass,
        device_registry,
        key="submenu-related",
        name="Bench meter",
    )
    await manager.async_add_related_device(asset["asset_uuid"], related.id)
    populated = await flow.async_step_ha_relationship()

    assert populated["menu_options"] == [
        "manage_primary_device",
        "add_related_device",
        "remove_related_device",
        BACK_ROW,
    ]


@pytest.mark.parametrize("submenu", [REPLACEMENT_STEP, HA_STEP])
async def test_rendering_a_submenu_writes_nothing(
    hass: HomeAssistant,
    submenu: str,
) -> None:
    """Opening a submenu is navigation, not a change."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Untouched unit")
    flow = await _on_asset(hass, manager, asset["asset_uuid"])
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        first = await getattr(flow, f"async_step_{submenu}")()
        second = await getattr(flow, f"async_step_{submenu}")()

    assert first["type"] is second["type"] is FlowResultType.MENU
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


@pytest.mark.parametrize("submenu", [REPLACEMENT_STEP, HA_STEP])
async def test_back_returns_to_the_same_asset_hub_without_touching_anything(
    hass: HomeAssistant,
    submenu: str,
) -> None:
    """Back is real menu navigation: no write, no reload, same Asset."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Returning unit")
    flow = await _on_asset(hass, manager, asset["asset_uuid"])
    await getattr(flow, f"async_step_{submenu}")()
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        hub = await flow.async_step_manage_asset_menu()

    assert hub["type"] is FlowResultType.MENU
    assert hub["step_id"] == HUB_STEP
    assert flow._selected_asset_uuid == asset["asset_uuid"]
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


async def test_recording_a_replacement_returns_to_the_replacement_submenu(
    hass: HomeAssistant,
) -> None:
    """Both directions of recording stay in the Replacement submenu."""
    manager = _manager(hass)
    old = await manager.async_create_manual_asset(name="Old unit")
    new = await manager.async_create_manual_asset(name="New unit")
    other = await manager.async_create_manual_asset(name="Other unit")
    flow = await _on_asset(hass, manager, new["asset_uuid"])

    replaces = await flow.async_step_replacement_replaces(
        {
            CONF_REPLACEMENT_TARGET_ASSET_UUID: old["asset_uuid"],
            CONF_REPLACEMENT_REASON: "failure",
            CONF_EFFECTIVE_DATE: None,
            CONF_NOTES: "Swapped in",
        }
    )

    assert replaces["step_id"] == REPLACEMENT_STEP
    assert replaces["description_placeholders"]["result"] == (
        "asset_replacement_updated"
    )
    assert flow._selected_asset_uuid == new["asset_uuid"]
    assert manager.active_replacement_predecessor(new["asset_uuid"])[
        "asset_uuid"
    ] == old["asset_uuid"]

    other_flow = await _on_asset(hass, manager, other["asset_uuid"])
    replaced_by = await other_flow.async_step_replacement_replaced_by(
        {
            CONF_REPLACEMENT_TARGET_ASSET_UUID: old["asset_uuid"],
            CONF_REPLACEMENT_REASON: "upgrade",
        }
    )

    assert replaced_by["step_id"] == REPLACEMENT_STEP
    assert other_flow._selected_asset_uuid == other["asset_uuid"]


async def test_correcting_a_replacement_returns_to_the_replacement_submenu(
    hass: HomeAssistant,
) -> None:
    """A correction lands back where the record list is."""
    manager = _manager(hass)
    old, _new, record = await _replacement_pair(manager)
    correct = await manager.async_create_manual_asset(name="Correct unit")
    flow = await _on_asset(hass, manager, old["asset_uuid"])
    form = await flow.async_step_manage_asset_replacement(
        {
            CONF_REPLACEMENT_UUID: record["replacement_uuid"],
            CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_CORRECT,
        }
    )

    with capture_reloads(hass) as reload:
        corrected = await flow.async_step_correct_asset_replacement(
            {
                CONF_PREDECESSOR_ASSET_UUID: old["asset_uuid"],
                CONF_SUCCESSOR_ASSET_UUID: correct["asset_uuid"],
                CONF_REPLACEMENT_REASON: "warranty_rma",
                CONF_VOID_REASON: "Wrong successor",
            }
        )

    assert form["step_id"] == "correct_asset_replacement"
    assert corrected["step_id"] == REPLACEMENT_STEP
    assert flow._selected_asset_uuid == old["asset_uuid"]
    assert manager.active_replacement_successor(old["asset_uuid"])[
        "asset_uuid"
    ] == correct["asset_uuid"]
    reload.assert_called_once()


async def test_confirmed_void_returns_to_the_replacement_submenu(
    hass: HomeAssistant,
) -> None:
    """Voiding through its confirmation also stays in the submenu."""
    manager = _manager(hass)
    old, _new, record = await _replacement_pair(manager)
    flow = await _on_asset(hass, manager, old["asset_uuid"])
    confirmation = await flow.async_step_manage_asset_replacement(
        {
            CONF_REPLACEMENT_UUID: record["replacement_uuid"],
            CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_VOID,
        }
    )

    with capture_reloads(hass) as reload:
        voided = await flow.async_step_confirm_void_replacement(
            {
                CONF_VOID_REASON: "Recorded against the wrong unit",
                CONF_CONFIRM_VOID: True,
            }
        )

    assert confirmation["step_id"] == "confirm_void_replacement"
    assert voided["step_id"] == REPLACEMENT_STEP
    assert flow._selected_asset_uuid == old["asset_uuid"]
    assert manager.replacement_record(record["replacement_uuid"])["voided_at"]
    assert manager.active_replacement_successor(old["asset_uuid"]) is None
    reload.assert_called_once()


async def test_primary_device_change_returns_to_the_ha_submenu(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Setting the primary device stays in the Home Assistant devices submenu."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Primary unit")
    _owner, device = _external_device(
        hass,
        device_registry,
        key="submenu-primary",
        name="Bench controller",
    )
    flow = await _on_asset(hass, manager, asset["asset_uuid"])

    with capture_reloads(hass) as reload:
        linked = await flow.async_step_manage_primary_device(
            {
                CONF_HA_RELATIONSHIP_ACTION: HA_RELATIONSHIP_ACTION_REPLACE,
                CONF_DEVICE_ID: device.id,
            }
        )

    assert linked["step_id"] == HA_STEP
    assert linked["description_placeholders"]["result"] == (
        "asset_ha_relationship_updated"
    )
    assert flow._selected_asset_uuid == asset["asset_uuid"]
    assert manager.asset(asset["asset_uuid"])["ha_device_refs"] == [
        {"device_id": device.id, "role": "primary"}
    ]
    reload.assert_called_once()


async def test_related_device_add_and_remove_return_to_the_ha_submenu(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Adding and removing a related device both stay in the submenu."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Related unit")
    _owner, related = _external_device(
        hass,
        device_registry,
        key="submenu-add-remove",
        name="Bench meter",
    )
    flow = await _on_asset(hass, manager, asset["asset_uuid"])

    added = await flow.async_step_add_related_device({CONF_DEVICE_ID: related.id})

    assert added["step_id"] == HA_STEP
    assert added["description_placeholders"]["result"] == (
        "asset_related_device_added"
    )
    assert manager.asset(asset["asset_uuid"])["ha_device_refs"] == [
        {"device_id": related.id, "role": "related"}
    ]

    removed = await flow.async_step_remove_related_device(
        {CONF_DEVICE_ID: related.id}
    )

    assert removed["step_id"] == HA_STEP
    assert removed["description_placeholders"]["result"] == (
        "asset_related_device_removed"
    )
    assert flow._selected_asset_uuid == asset["asset_uuid"]
    assert manager.asset(asset["asset_uuid"])["ha_device_refs"] == []


async def test_replacement_result_is_reported_once_and_does_not_follow_back(
    hass: HomeAssistant,
) -> None:
    """A submenu result is spent on the submenu, never on the hub after Back."""
    manager = _manager(hass)
    old = await manager.async_create_manual_asset(name="Old unit")
    new = await manager.async_create_manual_asset(name="New unit")
    flow = await _on_asset(hass, manager, new["asset_uuid"])

    saved = await flow.async_step_replacement_replaces(
        {
            CONF_REPLACEMENT_TARGET_ASSET_UUID: old["asset_uuid"],
            CONF_REPLACEMENT_REASON: "failure",
        }
    )
    assert saved["description_placeholders"]["result"] == (
        "asset_replacement_updated"
    )

    reopened = await flow.async_step_asset_replacement()
    assert reopened["description_placeholders"]["result"] == ""

    hub = await flow.async_step_manage_asset_menu()
    assert hub["description_placeholders"]["result"] == ""


async def test_ha_result_is_reported_once_and_does_not_follow_back(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """The same holds for the Home Assistant devices submenu."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Result unit")
    _owner, device = _external_device(
        hass,
        device_registry,
        key="submenu-result",
        name="Bench controller",
    )
    flow = await _on_asset(hass, manager, asset["asset_uuid"])

    saved = await flow.async_step_add_related_device({CONF_DEVICE_ID: device.id})
    assert saved["description_placeholders"]["result"] == (
        "asset_related_device_added"
    )

    reopened = await flow.async_step_ha_relationship()
    assert reopened["description_placeholders"]["result"] == ""

    hub = await flow.async_step_manage_asset_menu()
    assert hub["description_placeholders"]["result"] == ""


async def test_submenus_show_context_without_technical_identifiers(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Both submenus can describe the current state by name alone."""
    manager = _manager(hass)
    old, new, _record = await _replacement_pair(manager)
    _owner, device = _external_device(
        hass,
        device_registry,
        key="submenu-context",
        name="Bench controller",
    )
    await manager.async_link_asset_device(old["asset_uuid"], device.id, device=device)
    flow = await _on_asset(hass, manager, old["asset_uuid"])

    replacement = await flow.async_step_asset_replacement()
    ha_devices = await flow.async_step_ha_relationship()

    assert replacement["description_placeholders"]["replacement"] == (
        f"New unit · {new['asset_id']}"
    )
    assert old["asset_uuid"] not in replacement["description_placeholders"][
        "replacement"
    ]
    assert ha_devices["description_placeholders"]["ha_devices"] == (
        "Bench controller"
    )
    assert device.id not in ha_devices["description_placeholders"]["ha_devices"]
