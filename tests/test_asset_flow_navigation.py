"""Every Asset-management step returns to its immediate logical parent.

The hub opens sections; a section opens its editors and, for replacement,
its submenu; the Home Assistant devices view opens its forms. A successful
save returns to the view the form was opened from, and shows its result
there once. Back goes one level up, never skipping to the hub, and is only
navigation: no write, no reload, no result, the same Asset.

    Asset hub
      Details & warranty ........ Asset details, Linked purchase
      Installation & location ... installation form (+ clear location)
      Lifecycle & replacement ... lifecycle form (+ disposed)
        Replacement ............. create, manage (correct, void)
      Home Assistant devices .... primary, add related, remove related
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr

from custom_components.device_lifecycle.const import (
    CONF_CONFIRM_AREA_CLEAR,
    CONF_CONFIRM_DISPOSED,
    CONF_CONFIRM_VOID,
    CONF_DEPLOYMENT_STATE,
    CONF_DEVICE_ID,
    CONF_LIFECYCLE_STATUS,
    CONF_PREDECESSOR_ASSET_UUID,
    CONF_PURCHASE_UUID,
    CONF_REPLACEMENT_ACTION,
    CONF_REPLACEMENT_REASON,
    CONF_REPLACEMENT_TARGET_ASSET_UUID,
    CONF_REPLACEMENT_UUID,
    CONF_SUCCESSOR_ASSET_UUID,
    CONF_VOID_REASON,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    REPLACEMENT_ACTION_CORRECT,
    REPLACEMENT_ACTION_VOID,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import ASSET_UUID, PURCHASE_UUID, capture_reloads
from .test_explicit_selection_safety import _flow_manager_on_asset
from .test_ha_relationship_options_flow import _external_device
from .test_options_flow import _identical_metadata_input, _manager

HUB = "manage_asset_menu"
DETAILS = "asset_details_warranty_menu"
INSTALLATION = "asset_installation_menu"
LIFECYCLE = "asset_lifecycle_replacement_menu"
REPLACEMENT = "asset_replacement"
HA_DEVICES = "ha_relationship"
# Menu -> the parent its Back row returns to.
BACK_TO = {
    DETAILS: HUB,
    INSTALLATION: HUB,
    LIFECYCLE: HUB,
    REPLACEMENT: LIFECYCLE,
    HA_DEVICES: HUB,
}
# Menu -> how to reach it from the hub.
PATH = {
    DETAILS: [DETAILS],
    INSTALLATION: [INSTALLATION],
    LIFECYCLE: [LIFECYCLE],
    REPLACEMENT: [LIFECYCLE, REPLACEMENT],
    HA_DEVICES: [HA_DEVICES],
}
BACK_LABELS = {
    "en": {
        HUB: "← Back to asset management",
        LIFECYCLE: "← Back to lifecycle & replacement",
    },
    "fi": {
        HUB: "← Takaisin laitteen hallintaan",
        LIFECYCLE: "← Takaisin elinkaareen ja korvaamiseen",
    },
}
TRANSLATIONS = (
    Path(__file__).parents[1]
    / "custom_components"
    / "device_lifecycle"
    / "translations"
)


async def _go(hass: HomeAssistant, flow_id: str, user_input: dict) -> dict:
    """Send one step through Home Assistant's own flow manager."""
    return await hass.config_entries.options.async_configure(flow_id, user_input)


async def _open(hass: HomeAssistant, flow_id: str, steps: list[str]) -> dict:
    """Follow menu rows from the hub, returning the last view."""
    result: dict = {}
    for step in steps:
        result = await _go(hass, flow_id, {"next_step_id": step})
    return result


@pytest.mark.parametrize(("menu", "parent"), BACK_TO.items())
async def test_back_goes_one_level_up_and_changes_nothing(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    menu: str,
    parent: str,
) -> None:
    """End to end: Back is the immediate parent, with no write or reload."""
    manager = _manager(hass, asset_store_data)
    flow_id = await _flow_manager_on_asset(hass, manager, ASSET_UUID)
    opened = await _open(hass, flow_id, PATH[menu])
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        back = await _go(hass, flow_id, {"next_step_id": parent})

    assert opened["step_id"] == menu
    assert opened["menu_options"][-1] == parent
    assert back["type"] is FlowResultType.MENU
    assert back["step_id"] == parent
    assert back["description_placeholders"]["result"] == ""
    assert "Workshop device" in back["description_placeholders"]["asset"]
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()
    assert manager._data == before


@pytest.mark.parametrize("language", ["en", "fi"])
def test_each_back_row_names_where_it_goes(language: str) -> None:
    """The label is the parent's name, so Back never surprises."""
    steps = json.loads(
        (TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8")
    )["options"]["step"]

    for menu, parent in BACK_TO.items():
        assert steps[menu]["menu_options"][parent] == BACK_LABELS[language][parent]


async def _details_save(hass, manager, flow_id) -> dict:
    await _open(hass, flow_id, [DETAILS, "edit_asset_metadata"])
    edit = _identical_metadata_input(manager.asset(ASSET_UUID))
    edit["name"] = "Renamed from its section"
    return await _go(hass, flow_id, edit)


async def _purchase_save(hass, manager, flow_id) -> dict:
    await _open(hass, flow_id, [DETAILS, "change_asset_purchase"])
    return await _go(hass, flow_id, {CONF_PURCHASE_UUID: PURCHASE_UUID})


async def _installation_save(hass, manager, flow_id) -> dict:
    await _open(hass, flow_id, [INSTALLATION, "asset_deployment"])
    return await _go(
        hass, flow_id, {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED}
    )


async def _clear_location_save(hass, manager, flow_id) -> dict:
    await _open(hass, flow_id, [INSTALLATION, "asset_deployment"])
    confirm = await _go(
        hass, flow_id, {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED}
    )
    assert confirm["step_id"] == "confirm_not_deployed"
    return await _go(hass, flow_id, {CONF_CONFIRM_AREA_CLEAR: True})


async def _lifecycle_save(hass, manager, flow_id) -> dict:
    await _open(hass, flow_id, [LIFECYCLE, "asset_lifecycle"])
    return await _go(hass, flow_id, {CONF_LIFECYCLE_STATUS: "retired"})


async def _disposed_save(hass, manager, flow_id) -> dict:
    await _open(hass, flow_id, [LIFECYCLE, "asset_lifecycle"])
    confirm = await _go(hass, flow_id, {CONF_LIFECYCLE_STATUS: "disposed"})
    assert confirm["step_id"] == "confirm_disposed"
    return await _go(hass, flow_id, {CONF_CONFIRM_DISPOSED: True})


async def _replacement_create(hass, manager, flow_id) -> dict:
    old = await manager.async_create_manual_asset(name="Old unit")
    await _open(hass, flow_id, [LIFECYCLE, REPLACEMENT, "replacement_replaces"])
    return await _go(
        hass,
        flow_id,
        {
            CONF_REPLACEMENT_TARGET_ASSET_UUID: old["asset_uuid"],
            CONF_REPLACEMENT_REASON: "failure",
        },
    )


async def _replacement_record(manager: AssetStoreManager) -> dict:
    old = await manager.async_create_manual_asset(name="Old unit")
    return await manager.async_create_asset_replacement(
        old["asset_uuid"], ASSET_UUID,
        reason="failure", effective_date=None, notes=None,
    )


async def _replacement_correct(hass, manager, flow_id) -> dict:
    record = await _replacement_record(manager)
    other = await manager.async_create_manual_asset(name="Other unit")
    await _open(hass, flow_id, [LIFECYCLE, REPLACEMENT, "manage_asset_replacement"])
    await _go(
        hass,
        flow_id,
        {
            CONF_REPLACEMENT_UUID: record["replacement_uuid"],
            CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_CORRECT,
        },
    )
    return await _go(
        hass,
        flow_id,
        {
            CONF_PREDECESSOR_ASSET_UUID: other["asset_uuid"],
            CONF_SUCCESSOR_ASSET_UUID: ASSET_UUID,
            CONF_REPLACEMENT_REASON: "upgrade",
            CONF_VOID_REASON: "Wrong predecessor",
        },
    )


async def _replacement_void(hass, manager, flow_id) -> dict:
    record = await _replacement_record(manager)
    await _open(hass, flow_id, [LIFECYCLE, REPLACEMENT, "manage_asset_replacement"])
    await _go(
        hass,
        flow_id,
        {
            CONF_REPLACEMENT_UUID: record["replacement_uuid"],
            CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_VOID,
        },
    )
    return await _go(
        hass,
        flow_id,
        {CONF_VOID_REASON: "Recorded in error", CONF_CONFIRM_VOID: True},
    )


SAVES = {
    "details": (_details_save, DETAILS, "Asset details updated."),
    "purchase": (_purchase_save, DETAILS, "Linked purchase updated."),
    "installation": (_installation_save, INSTALLATION, "Installation & location updated."),
    "clear-location": (_clear_location_save, INSTALLATION, "Installation & location updated."),
    "lifecycle": (_lifecycle_save, LIFECYCLE, "Lifecycle updated."),
    "disposed": (_disposed_save, LIFECYCLE, "Lifecycle updated."),
    "replacement-create": (_replacement_create, REPLACEMENT, "Replacement updated."),
    "replacement-correct": (_replacement_correct, REPLACEMENT, "Replacement updated."),
    "replacement-void": (_replacement_void, REPLACEMENT, "Replacement updated."),
}


@pytest.mark.parametrize("save", SAVES)
async def test_a_save_returns_to_the_view_it_was_opened_from(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    asset_store_data: AssetStoreData,
    save: str,
) -> None:
    """End to end: the parent, the same Asset, and the result exactly once."""
    manager = _manager(hass, asset_store_data)
    office = area_registry.async_create("Office")
    await manager.async_set_asset_deployment(
        ASSET_UUID, deployment_state=DEPLOYMENT_STATE_DEPLOYED, ha_area_id=office.id
    )
    flow_id = await _flow_manager_on_asset(hass, manager, ASSET_UUID)
    run, parent, result = SAVES[save]

    with capture_reloads(hass):
        landed = await run(hass, manager, flow_id)
    again = await _go(hass, flow_id, {"next_step_id": BACK_TO[parent]})

    assert landed["type"] is FlowResultType.MENU, save
    assert landed["step_id"] == parent, save
    assert landed["description_placeholders"]["result"] == result, save
    assert "DL0007" in landed["description_placeholders"]["asset"], save
    # One level up, the result is not repeated.
    assert again["step_id"] == BACK_TO[parent], save
    assert again["description_placeholders"]["result"] == "", save


async def test_home_assistant_device_forms_return_to_their_view(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """End to end: link, add and remove all land back on Home Assistant devices."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Linked unit")
    _owner, primary = _external_device(
        hass, device_registry, key="nav-primary", name="Kitchen socket"
    )
    _owner, related = _external_device(
        hass, device_registry, key="nav-related", name="Bench meter"
    )
    flow_id = await _flow_manager_on_asset(hass, manager, asset["asset_uuid"])
    landed: dict[str, dict[str, Any]] = {}

    with capture_reloads(hass):
        await _open(hass, flow_id, [HA_DEVICES, "manage_primary_device"])
        landed["primary"] = await _go(hass, flow_id, {CONF_DEVICE_ID: primary.id})
        await _go(hass, flow_id, {"next_step_id": "add_related_device"})
        landed["add"] = await _go(hass, flow_id, {CONF_DEVICE_ID: related.id})
        await _go(hass, flow_id, {"next_step_id": "remove_related_device"})
        landed["remove"] = await _go(hass, flow_id, {CONF_DEVICE_ID: related.id})

    for name, result in (
        ("primary", "Home Assistant devices updated."),
        ("add", "Related Home Assistant device added."),
        ("remove", "Related Home Assistant device removed."),
    ):
        assert landed[name]["step_id"] == HA_DEVICES, name
        assert landed[name]["description_placeholders"]["result"] == result, name
        assert landed[name]["description_placeholders"]["asset"] == (
            "Linked unit · DL0001"
        ), name
