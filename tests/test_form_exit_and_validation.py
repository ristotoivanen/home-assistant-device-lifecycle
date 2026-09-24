"""Every edit or action form says how to leave it, and names what is missing.

A form is a modal whose top-left X closes it without saving; a menu has its
own Back row. So every edit, mutation or action form ends its description
with one line saying how to close it without saving, and no menu, read-only
view or the Asset picker carries that line.

When a required choice, reason or confirmation is missing, the form stays
open, names the missing field, and changes nothing. No field is filled in on
the person's behalf.

Where Home Assistant's own frontend owns the check, the integration cannot
word it: a required text field left empty is stopped in the browser with
the frontend's generic message before the flow sees it. Those fields keep
their required marker; only what reaches the flow is worded here.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr

from custom_components.device_lifecycle.config_flow import NOT_SELECTED
from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_CONFIRM_VOID,
    CONF_DEPLOYMENT_STATE,
    CONF_DEVICE_ID,
    CONF_HA_RELATIONSHIP_ACTION,
    CONF_LIFECYCLE_STATUS,
    CONF_PREDECESSOR_ASSET_UUID,
    CONF_REPLACEMENT_ACTION,
    CONF_REPLACEMENT_REASON,
    CONF_REPLACEMENT_TARGET_ASSET_UUID,
    CONF_REPLACEMENT_UUID,
    CONF_SUCCESSOR_ASSET_UUID,
    CONF_VOID_REASON,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    HA_RELATIONSHIP_ACTION_REPLACE,
    REPLACEMENT_ACTION_CORRECT,
    REPLACEMENT_ACTION_VOID,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import ASSET_UUID, capture_reloads
from .test_explicit_selection_safety import _flow_manager_on_asset
from .test_ha_relationship_options_flow import _external_device
from .test_options_flow import _manager, _options_flow

TRANSLATIONS = (
    Path(__file__).parents[1]
    / "custom_components"
    / "device_lifecycle"
    / "translations"
)
EXIT = {
    "en": "Close the window without saving by using the X button in the upper-left corner.",
    "fi": "Sulje ikkuna tallentamatta painamalla vasemman yläkulman X-painiketta.",
}
# Every edit, mutation or action form: its X closes it without saving.
EDIT_FORMS = {
    "edit_asset_metadata",
    "change_asset_purchase",
    "asset_deployment",
    "confirm_not_deployed",
    "asset_lifecycle",
    "confirm_disposed",
    "replacement_replaces",
    "manage_asset_replacement",
    "correct_asset_replacement",
    "confirm_void_replacement",
    "manage_primary_device",
    "add_related_device",
    "remove_related_device",
    "quick_add_from_ha",
    "quick_add_details",
    "quick_add_replacement",
    "quick_add_confirm",
}
# The Asset picker only opens an Asset: there is nothing to save or discard.
NOT_EDIT_FORMS = {"manage_asset"}
# Purchase and runtime forms open from the integration page: every one of
# them is a form whose X closes it without saving.
SUBENTRY_FORMS = {
    "purchase": {"user", "reconfigure"},
    "runtime": {"user", "runtime_source", "reconfigure", "reconfigure_source"},
}
# What each changed field says when it is missing.
FIELD_ERRORS = {
    "en": {
        "correction_reason_required": (
            "Enter the reason for correcting the previous relationship."
        ),
        "replacement_void_reason_required": (
            "Enter the reason for voiding the relationship."
        ),
        "void_confirmation_required": "Confirm that the relationship should be voided.",
        "primary_device_required": "Select the primary Home Assistant device.",
        "related_device_to_add_required": "Select the Home Assistant device to add.",
    },
    "fi": {
        "correction_reason_required": "Anna vanhan suhteen korjaamisen syy.",
        "replacement_void_reason_required": "Anna mitätöinnin syy.",
        "void_confirmation_required": "Vahvista suhteen mitätöinti.",
        "primary_device_required": "Valitse ensisijainen Home Assistant -laite.",
        "related_device_to_add_required": "Valitse lisättävä Home Assistant -laite.",
    },
}


def _options(language: str) -> dict[str, Any]:
    """Return one language's OptionsFlow translations."""
    return json.loads(
        (TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8")
    )["options"]


def _marker(form: dict[str, Any], field: str) -> vol.Marker:
    """Return the schema marker for one field."""
    return next(marker for marker in form["data_schema"].schema if marker == field)


async def _on(hass: HomeAssistant, manager: AssetStoreManager, asset_uuid: str):
    """Return a flow on one Asset, driven directly."""
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset_uuid})
    return flow


# --- Where the exit line appears ------------------------------------------


@pytest.mark.parametrize("language", ["en", "fi"])
def test_every_form_is_either_an_edit_form_or_the_picker(language: str) -> None:
    """A new form cannot appear without deciding whether it gets the line."""
    steps = _options(language)["step"]
    forms = {step for step, copy in steps.items() if "menu_options" not in copy}

    assert forms == EDIT_FORMS | NOT_EDIT_FORMS


@pytest.mark.parametrize("language", ["en", "fi"])
@pytest.mark.parametrize("step", sorted(EDIT_FORMS))
def test_each_edit_form_ends_with_the_exit_line(language: str, step: str) -> None:
    """Once, last, as its own paragraph, and nowhere in an error message."""
    options = _options(language)
    description = options["step"][step]["description"]

    assert description.count(EXIT[language]) == 1
    assert description.endswith(f"\n\n{EXIT[language]}")
    for message in options["error"].values():
        assert "X" not in message.split() and EXIT[language] not in message


@pytest.mark.parametrize("language", ["en", "fi"])
def test_purchase_and_runtime_forms_end_with_it_too(language: str) -> None:
    """Every subentry step is a form, so every one of them says it."""
    subentries = json.loads(
        (TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8")
    )["config_subentries"]

    assert {kind: set(copy["step"]) for kind, copy in subentries.items()} == (
        SUBENTRY_FORMS
    )
    for kind, steps in SUBENTRY_FORMS.items():
        for step in steps:
            description = subentries[kind]["step"][step]["description"]
            assert description.count(EXIT[language]) == 1, (kind, step)
            assert description.endswith(f"\n\n{EXIT[language]}"), (kind, step)
        for message in subentries[kind]["error"].values():
            assert EXIT[language] not in message


@pytest.mark.parametrize("language", ["en", "fi"])
def test_menus_views_and_the_picker_never_carry_it(language: str) -> None:
    """The hub, sections, submenus and read-only views have their own Back."""
    steps = _options(language)["step"]
    others = [
        step
        for step, copy in steps.items()
        if "menu_options" in copy or step in NOT_EDIT_FORMS
    ]

    assert {"manage_asset_menu", "asset_replacement", "ha_relationship"} <= set(others)
    for step in others:
        assert EXIT[language] not in (steps[step].get("description") or ""), step
        # Nor a variant of it: X guidance belongs to forms only.
        assert "X-painik" not in (steps[step].get("description") or ""), step
        assert "X button" not in (steps[step].get("description") or ""), step


@pytest.mark.parametrize("language", ["en", "fi"])
def test_each_missing_field_has_its_own_words(language: str) -> None:
    """Specific sentences, in both languages, never the generic one."""
    errors = _options(language)["error"]

    for key, message in FIELD_ERRORS[language].items():
        assert errors[key] == message, key


# --- Required choices are never made for the person -------------------------


async def test_device_choices_start_empty_and_are_named_when_missing(
    hass: HomeAssistant,
) -> None:
    """No Home Assistant device is preselected; an empty submit is named."""
    manager = _manager(hass)
    # Real devices to choose from, so a default would have one to take.
    device_registry = dr.async_get(hass)
    for key in ("exit-first", "exit-second"):
        _external_device(hass, device_registry, key=key, name=f"Device {key}")
    asset = await manager.async_create_manual_asset(name="Unlinked unit")
    flow = await _on(hass, manager, asset["asset_uuid"])
    manager._store.async_save.reset_mock()

    primary = await flow.async_step_manage_primary_device()
    add = await flow.async_step_add_related_device()
    with capture_reloads(hass) as reload:
        missing_primary = await flow.async_step_manage_primary_device({})
        missing_related = await flow.async_step_add_related_device(
            {CONF_DEVICE_ID: ""}
        )

    for form in (primary, add):
        marker = _marker(form, CONF_DEVICE_ID)
        # Optional in the schema only so the flow, not the frontend, can say
        # what is missing; the device picker shows no required marker either
        # way, and nothing is filled in.
        assert isinstance(marker, vol.Optional)
        assert marker.default is vol.UNDEFINED
        assert not (marker.description or {}).get("suggested_value")
        # An untouched form submits no device at all.
        assert form["data_schema"]({}) == {}
    assert missing_primary["step_id"] == "manage_primary_device"
    assert missing_primary["errors"] == {CONF_DEVICE_ID: "primary_device_required"}
    assert missing_related["step_id"] == "add_related_device"
    assert missing_related["errors"] == {
        CONF_DEVICE_ID: "related_device_to_add_required"
    }
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()
    assert flow._selected_asset_uuid == asset["asset_uuid"]
    assert manager.asset(asset["asset_uuid"])["ha_device_refs"] == []


async def test_replacing_a_primary_device_needs_the_new_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Replace with nothing chosen is named on the device field, not a base error."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Linked unit")
    _owner, device = _external_device(
        hass, device_registry, key="exit-primary", name="Kitchen socket"
    )
    await manager.async_link_asset_device(
        asset["asset_uuid"], device.id, device=device
    )
    flow = await _on(hass, manager, asset["asset_uuid"])
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        result = await flow.async_step_manage_primary_device(
            {CONF_HA_RELATIONSHIP_ACTION: HA_RELATIONSHIP_ACTION_REPLACE}
        )

    assert result["step_id"] == "manage_primary_device"
    assert result["errors"] == {CONF_DEVICE_ID: "primary_device_required"}
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()
    assert manager._data == before


async def test_removal_and_replacement_keep_their_explicit_placeholder(
    hass: HomeAssistant,
) -> None:
    """Both start on "Select a device…" and name a missing choice."""
    manager = _manager(hass)
    old = await manager.async_create_manual_asset(name="Old unit")
    new = await manager.async_create_manual_asset(name="New unit")
    await manager.async_add_related_device(new["asset_uuid"], "related-device-id")
    flow = await _on(hass, manager, new["asset_uuid"])
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    replaces = await flow.async_step_replacement_replaces()
    remove = await flow.async_step_remove_related_device()
    with capture_reloads(hass) as reload:
        no_target = await flow.async_step_replacement_replaces(
            {
                CONF_REPLACEMENT_TARGET_ASSET_UUID: NOT_SELECTED,
                CONF_REPLACEMENT_REASON: "failure",
            }
        )
        no_device = await flow.async_step_remove_related_device(
            {CONF_DEVICE_ID: NOT_SELECTED}
        )

    assert _marker(replaces, CONF_REPLACEMENT_TARGET_ASSET_UUID).default() == (
        NOT_SELECTED
    )
    assert _marker(remove, CONF_DEVICE_ID).default() == NOT_SELECTED
    assert no_target["errors"] == {
        CONF_REPLACEMENT_TARGET_ASSET_UUID: "replacement_target_required"
    }
    assert no_device["errors"] == {CONF_DEVICE_ID: "related_device_required"}
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()
    assert manager._data == before
    assert old["asset_uuid"] not in json.dumps(manager._data["replacement_records"])


# --- Reasons and confirmation ---------------------------------------------


async def _managed_record(hass: HomeAssistant, manager: AssetStoreManager):
    """Return a flow on the new Asset of one relationship, and the record."""
    old = await manager.async_create_manual_asset(name="Old unit")
    new = await manager.async_create_manual_asset(name="New unit")
    record = await manager.async_create_asset_replacement(
        old["asset_uuid"], new["asset_uuid"],
        reason="failure", effective_date=None, notes=None,
    )
    flow = await _on(hass, manager, new["asset_uuid"])
    return flow, old, new, record


async def test_a_correction_without_a_reason_is_named_and_changes_nothing(
    hass: HomeAssistant,
) -> None:
    """The reason field says what is missing; every other choice is kept."""
    manager = _manager(hass)
    flow, old, new, record = await _managed_record(hass, manager)
    other = await manager.async_create_manual_asset(name="Other unit")
    await flow.async_step_manage_asset_replacement(
        {
            CONF_REPLACEMENT_UUID: record["replacement_uuid"],
            CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_CORRECT,
        }
    )
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()
    submission = {
        CONF_PREDECESSOR_ASSET_UUID: other["asset_uuid"],
        CONF_SUCCESSOR_ASSET_UUID: new["asset_uuid"],
        CONF_REPLACEMENT_REASON: "upgrade",
        CONF_VOID_REASON: "   ",
    }

    with capture_reloads(hass) as reload:
        result = await flow.async_step_correct_asset_replacement(submission)

    assert result["step_id"] == "correct_asset_replacement"
    assert result["errors"] == {CONF_VOID_REASON: "correction_reason_required"}
    # The person's choices survive the correction of one field.
    assert _marker(result, CONF_PREDECESSOR_ASSET_UUID).description[
        "suggested_value"
    ] == other["asset_uuid"]
    assert _marker(result, CONF_REPLACEMENT_REASON).description[
        "suggested_value"
    ] == "upgrade"
    # The reason stays required, with its marker, in the schema.
    assert isinstance(_marker(result, CONF_VOID_REASON), vol.Required)
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()
    assert manager._data == before
    assert manager.active_replacement_predecessor(new["asset_uuid"])[
        "asset_uuid"
    ] == old["asset_uuid"]


@pytest.mark.parametrize(
    ("submission", "errors"),
    [
        pytest.param(
            {CONF_VOID_REASON: "   ", CONF_CONFIRM_VOID: True},
            {CONF_VOID_REASON: "replacement_void_reason_required"},
            id="no-reason",
        ),
        pytest.param(
            {CONF_VOID_REASON: "Recorded in error", CONF_CONFIRM_VOID: False},
            {CONF_CONFIRM_VOID: "void_confirmation_required"},
            id="not-confirmed",
        ),
        pytest.param(
            {CONF_VOID_REASON: "", CONF_CONFIRM_VOID: False},
            {
                CONF_VOID_REASON: "replacement_void_reason_required",
                CONF_CONFIRM_VOID: "void_confirmation_required",
            },
            id="neither",
        ),
    ],
)
async def test_a_void_needs_both_its_reason_and_its_confirmation(
    hass: HomeAssistant,
    submission: dict[str, Any],
    errors: dict[str, str],
) -> None:
    """Each missing part is named; the form stays open and nothing is voided."""
    manager = _manager(hass)
    flow, _old, new, record = await _managed_record(hass, manager)
    await flow.async_step_manage_asset_replacement(
        {
            CONF_REPLACEMENT_UUID: record["replacement_uuid"],
            CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_VOID,
        }
    )
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        result = await flow.async_step_confirm_void_replacement(submission)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "confirm_void_replacement"
    assert result["errors"] == errors
    assert isinstance(_marker(result, CONF_VOID_REASON), vol.Required)
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()
    assert manager._data == before
    assert flow._selected_asset_uuid == new["asset_uuid"]
    assert manager.replacement_record(record["replacement_uuid"])["voided_at"] is None


# --- Closing with X -------------------------------------------------------


async def _open(hass: HomeAssistant, flow_id: str, *steps: Any) -> dict:
    """Follow menu rows or submissions through the flow manager."""
    result: dict = {}
    for step in steps:
        user_input = {"next_step_id": step} if isinstance(step, str) else step
        result = await hass.config_entries.options.async_configure(
            flow_id, user_input
        )
    return result


PATHS: dict[str, tuple[Any, ...]] = {
    "edit_asset_metadata": ("asset_details_warranty_menu", "edit_asset_metadata"),
    "change_asset_purchase": ("asset_details_warranty_menu", "change_asset_purchase"),
    "asset_deployment": ("asset_installation_menu", "asset_deployment"),
    "confirm_not_deployed": (
        "asset_installation_menu",
        "asset_deployment",
        {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED},
    ),
    "asset_lifecycle": ("asset_lifecycle_replacement_menu", "asset_lifecycle"),
    "confirm_disposed": (
        "asset_lifecycle_replacement_menu",
        "asset_lifecycle",
        {CONF_LIFECYCLE_STATUS: "disposed"},
    ),
    "replacement_replaces": (
        "asset_lifecycle_replacement_menu",
        "asset_replacement",
        "replacement_replaces",
    ),
    "manage_asset_replacement": (
        "asset_lifecycle_replacement_menu",
        "asset_replacement",
        "manage_asset_replacement",
    ),
    "manage_primary_device": ("ha_relationship", "manage_primary_device"),
    "add_related_device": ("ha_relationship", "add_related_device"),
    "remove_related_device": ("ha_relationship", "remove_related_device"),
}


@pytest.mark.parametrize("form", sorted(PATHS) + ["correct", "void"])
async def test_closing_a_form_with_x_saves_nothing(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    asset_store_data: AssetStoreData,
    form: str,
) -> None:
    """End to end: the X aborts the flow; no write, reload or change of any kind.

    The frontend's X removes the flow through Home Assistant's flow manager;
    the integration keeps nothing pending that a removal would have to save.
    """
    manager = _manager(hass, asset_store_data)
    office = area_registry.async_create("Office")
    await manager.async_set_asset_deployment(
        ASSET_UUID, deployment_state=DEPLOYMENT_STATE_DEPLOYED, ha_area_id=office.id
    )
    await manager.async_add_related_device(ASSET_UUID, "related-device-id")
    old = await manager.async_create_manual_asset(name="Old unit")
    record = await manager.async_create_asset_replacement(
        old["asset_uuid"], ASSET_UUID,
        reason="failure", effective_date=None, notes=None,
    )
    flow_id = await _flow_manager_on_asset(hass, manager, ASSET_UUID)
    if form in ("correct", "void"):
        action = REPLACEMENT_ACTION_CORRECT if form == "correct" else (
            REPLACEMENT_ACTION_VOID
        )
        opened = await _open(
            hass,
            flow_id,
            *PATHS["manage_asset_replacement"],
            {
                CONF_REPLACEMENT_UUID: record["replacement_uuid"],
                CONF_REPLACEMENT_ACTION: action,
            },
        )
    else:
        opened = await _open(hass, flow_id, *PATHS[form])
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        hass.config_entries.options.async_abort(flow_id)
        await hass.async_block_till_done()

    assert opened["type"] is FlowResultType.FORM
    assert hass.config_entries.options.async_progress() == []
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()
    assert manager._data == before
