"""Managing an existing replacement relationship reads in names and verbs.

A relationship is offered as "Name · DLxxxx → Name · DLxxxx", predecessor
first, and each button says what pressing it does: the selection step only
continues, the correction saves and returns, and the void confirmation voids
and returns. Nothing about which record is chosen, how a correction or a
void is stored, or where the flow lands afterwards changes.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar

from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_CONFIRM_VOID,
    CONF_PREDECESSOR_ASSET_UUID,
    CONF_REPLACEMENT_ACTION,
    CONF_REPLACEMENT_REASON,
    CONF_REPLACEMENT_UUID,
    CONF_SUCCESSOR_ASSET_UUID,
    CONF_VOID_REASON,
    DEPLOYMENT_STATE_DEPLOYED,
    REPLACEMENT_ACTION_CORRECT,
    REPLACEMENT_ACTION_VOID,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import ASSET_UUID, DEVICE_ID, capture_reloads
from .test_explicit_selection_safety import _flow_manager_on_asset
from .test_options_flow import _manager, _options_flow

SECTION = "asset_lifecycle_replacement_menu"
SUBMENU = "asset_replacement"
MANAGE = "manage_asset_replacement"
CORRECT = "correct_asset_replacement"
VOID = "confirm_void_replacement"
TRANSLATIONS = (
    Path(__file__).parents[1]
    / "custom_components"
    / "device_lifecycle"
    / "translations"
)
# Step -> the button text in each language. Only these steps change.
SUBMIT = {
    MANAGE: {"en": "Continue", "fi": "Jatka"},
    CORRECT: {"en": "Save and return", "fi": "Tallenna ja palaa"},
    VOID: {"en": "Void and return", "fi": "Mitätöi ja palaa"},
}
# Words that would claim the selection step saves something.
SAVING_WORDS = ("save", "tallenna", "palaa", "return")


def _steps(language: str) -> dict[str, Any]:
    """Return one language's OptionsFlow steps."""
    return json.loads(
        (TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8")
    )["options"]["step"]


def _label(asset: dict[str, Any]) -> str:
    """Name an Asset the way the management labels do."""
    return f"{asset['name']} · {asset['asset_id']}"


def _record_options(form: dict[str, Any]) -> list[dict[str, str]]:
    """Return the relationship selector's options."""
    return next(
        validator
        for marker, validator in form["data_schema"].schema.items()
        if marker == CONF_REPLACEMENT_UUID
    ).config["options"]


async def _on(hass: HomeAssistant, manager: AssetStoreManager, asset_uuid: str):
    """Return a flow on one Asset, driven directly."""
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset_uuid})
    return flow


async def _replace(manager: AssetStoreManager, old: dict, new: dict) -> dict:
    """Record old -> new directly in the manager."""
    return await manager.async_create_asset_replacement(
        old["asset_uuid"],
        new["asset_uuid"],
        reason="failure",
        effective_date=None,
        notes=None,
    )


async def _chain(manager: AssetStoreManager):
    """Return Old -> Middle -> New and both records."""
    old = await manager.async_create_manual_asset(name="Testilaite 0.5.4")
    middle = await manager.async_create_manual_asset(name="Testilaite 0.7.0")
    new = await manager.async_create_manual_asset(name="Testilaite 0.7.5")
    first = await _replace(manager, old, middle)
    second = await _replace(manager, middle, new)
    return old, middle, new, first, second


async def _to_manage(hass: HomeAssistant, flow_id: str) -> dict:
    """From the hub: Lifecycle & replacement, Manage replacement, management."""
    await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": SECTION}
    )
    await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": SUBMENU}
    )
    return await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": MANAGE}
    )


async def test_a_relationship_is_named_by_both_assets_predecessor_first(
    hass: HomeAssistant,
) -> None:
    """Names and IDs on both sides, the arrow from old to new, no UUID."""
    manager = _manager(hass)
    old = await manager.async_create_manual_asset(name="Testilaite 0.5.4")
    new = await manager.async_create_manual_asset(name="Testilaite 0.7.0")
    record = await _replace(manager, old, new)
    flow = await _on(hass, manager, new["asset_uuid"])

    form = await flow.async_step_manage_asset_replacement()
    options = _record_options(form)

    assert options == [
        {
            # The selector still returns the same record.
            "value": record["replacement_uuid"],
            "label": "Testilaite 0.5.4 · DL0001 → Testilaite 0.7.0 · DL0002",
        }
    ]
    label = options[0]["label"]
    assert label.index(old["asset_id"]) < label.index("→") < label.index(
        new["asset_id"]
    )
    for hidden in (old["asset_uuid"], new["asset_uuid"], record["replacement_uuid"]):
        assert hidden not in label


async def test_a_middle_asset_tells_its_two_relationships_apart(
    hass: HomeAssistant,
) -> None:
    """Old -> Middle and Middle -> New are two distinct, named choices."""
    manager = _manager(hass)
    old, middle, new, first, second = await _chain(manager)
    flow = await _on(hass, manager, middle["asset_uuid"])

    options = _record_options(await flow.async_step_manage_asset_replacement())

    assert {option["value"]: option["label"] for option in options} == {
        first["replacement_uuid"]: f"{_label(old)} → {_label(middle)}",
        second["replacement_uuid"]: f"{_label(middle)} → {_label(new)}",
    }


@pytest.mark.parametrize(
    ("language", "unavailable"),
    [("en", "Unavailable Asset"), ("fi", "Laite ei ole enää käytettävissä")],
)
async def test_a_missing_asset_is_named_in_words(
    hass: HomeAssistant,
    language: str,
    unavailable: str,
) -> None:
    """A validated Store cannot hold this; the label must still not leak."""
    hass.config.language = language
    manager = _manager(hass)
    old = await manager.async_create_manual_asset(name="Testilaite 0.5.4")
    new = await manager.async_create_manual_asset(name="Testilaite 0.7.0")
    await _replace(manager, old, new)
    manager._data["assets"].pop(old["asset_uuid"])
    flow = await _on(hass, manager, new["asset_uuid"])

    options = _record_options(await flow.async_step_manage_asset_replacement())

    assert options[0]["label"] == f"{unavailable} → {_label(new)}"
    assert old["asset_uuid"] not in options[0]["label"]


async def test_long_names_shorten_like_every_other_view(
    hass: HomeAssistant,
) -> None:
    """Each side keeps its Asset ID whole; only the name is shortened."""
    manager = _manager(hass)
    old = await manager.async_create_manual_asset(
        name="An old heater with a very long descriptive name"
    )
    new = await manager.async_create_manual_asset(name="New heater")
    await _replace(manager, old, new)
    flow = await _on(hass, manager, new["asset_uuid"])

    options = _record_options(await flow.async_step_manage_asset_replacement())

    assert options[0]["label"] == (
        "An old heater with a ve… · DL0001 → New heater · DL0002"
    )


async def test_correction_and_void_describe_the_relationship_by_name(
    hass: HomeAssistant,
) -> None:
    """Both follow-up forms name the chosen relationship the same way."""
    manager = _manager(hass)
    old, middle, _new, first, _second = await _chain(manager)
    flow = await _on(hass, manager, middle["asset_uuid"])

    correct = await flow.async_step_manage_asset_replacement(
        {
            CONF_REPLACEMENT_UUID: first["replacement_uuid"],
            CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_CORRECT,
        }
    )
    void = await flow.async_step_manage_asset_replacement(
        {
            CONF_REPLACEMENT_UUID: first["replacement_uuid"],
            CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_VOID,
        }
    )

    expected = f"{_label(old)} → {_label(middle)}"
    assert correct["step_id"] == CORRECT
    assert void["step_id"] == VOID
    assert correct["description_placeholders"]["relationship"] == expected
    assert void["description_placeholders"]["relationship"] == expected


@pytest.mark.parametrize("language", ["en", "fi"])
def test_each_management_button_says_what_it_does(language: str) -> None:
    """Continue to choose, save and return to correct, void and return to void."""
    steps = _steps(language)

    for step_id, labels in SUBMIT.items():
        assert steps[step_id]["submit"] == labels[language], step_id
    selection = steps[MANAGE]["submit"].casefold()
    for word in SAVING_WORDS:
        assert word not in selection, word
    # Recording a relationship saves it and returns to the submenu.
    assert steps["replacement_replaces"]["submit"] == SUBMIT[CORRECT][language]


async def test_the_selection_step_only_routes(
    hass: HomeAssistant,
) -> None:
    """End to end: choosing a record and an action writes and reloads nothing."""
    manager = _manager(hass)
    _old, middle, _new, first, second = await _chain(manager)
    flow_id = await _flow_manager_on_asset(hass, manager, middle["asset_uuid"])
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        manage = await _to_manage(hass, flow_id)
        correct = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_REPLACEMENT_UUID: second["replacement_uuid"],
                CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_CORRECT,
            },
        )

    assert manage["type"] is FlowResultType.FORM
    assert manage["step_id"] == MANAGE
    assert correct["type"] is FlowResultType.FORM
    assert correct["step_id"] == CORRECT
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()
    assert manager._data == before
    assert first["replacement_uuid"] in {
        option["value"] for option in _record_options(manage)
    }


async def _rich_pair(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    asset_store_data: AssetStoreData,
):
    """Return a manager where the fixture Asset replaced a deployed Asset."""
    manager = _manager(hass, asset_store_data)
    workshop = area_registry.async_create("Workshop")
    await manager.async_set_asset_deployment_reporting(
        ASSET_UUID, deployment_state=DEPLOYMENT_STATE_DEPLOYED, ha_area_id=workshop.id
    )
    await manager.async_set_asset_lifecycle_reporting(
        ASSET_UUID, "active", effective_date="2026-01-02", notes=None
    )
    await manager.async_add_related_device(ASSET_UUID, "related-device-id")
    old = await manager.async_create_manual_asset(name="Old heater")
    await manager.async_set_asset_lifecycle_reporting(
        old["asset_uuid"], "retired", effective_date="2026-01-02", notes=None
    )
    other = await manager.async_create_manual_asset(name="Other heater")
    record = await _replace(manager, old, manager.asset(ASSET_UUID))
    return manager, old, other, record


async def test_correction_saves_once_and_returns_to_the_replacement_view(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """End to end: one save, the same Asset, and no other domain moves."""
    manager, old, other, record = await _rich_pair(
        hass, area_registry, asset_store_data
    )
    flow_id = await _flow_manager_on_asset(hass, manager, ASSET_UUID)
    await _to_manage(hass, flow_id)
    await hass.config_entries.options.async_configure(
        flow_id,
        {
            CONF_REPLACEMENT_UUID: record["replacement_uuid"],
            CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_CORRECT,
        },
    )
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        landed = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_PREDECESSOR_ASSET_UUID: other["asset_uuid"],
                CONF_SUCCESSOR_ASSET_UUID: ASSET_UUID,
                CONF_REPLACEMENT_REASON: "upgrade",
                CONF_VOID_REASON: "Wrong predecessor",
            },
        )

    assert landed["type"] is FlowResultType.MENU
    assert landed["step_id"] == SUBMENU
    assert landed["description_placeholders"]["result"] == "Replacement updated."
    assert landed["description_placeholders"]["asset"] == "Workshop device · DL0007"
    manager._store.async_save.assert_awaited_once()
    reload.assert_called_once()
    assert manager.active_replacement_predecessor(ASSET_UUID)["asset_uuid"] == (
        other["asset_uuid"]
    )
    # Lifecycle, installation, Area, Purchase, warranty, Home Assistant
    # devices and identity of every Asset are exactly as they were.
    assert manager._data["assets"] == before["assets"]
    assert manager._data["lifecycle_events"] == before["lifecycle_events"]
    assert manager._data["purchases"] == before["purchases"]
    assert manager.asset(ASSET_UUID)["ha_device_refs"][0]["device_id"] == DEVICE_ID
    assert manager.asset(old["asset_uuid"])["lifecycle"]["status"] == "retired"


async def test_void_needs_confirmation_and_a_reason_then_returns(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """End to end: unconfirmed and unexplained voids change nothing."""
    manager, _old, _other, record = await _rich_pair(
        hass, area_registry, asset_store_data
    )
    flow_id = await _flow_manager_on_asset(hass, manager, ASSET_UUID)
    await _to_manage(hass, flow_id)
    choose_void = {
        CONF_REPLACEMENT_UUID: record["replacement_uuid"],
        CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_VOID,
    }
    confirm = await hass.config_entries.options.async_configure(
        flow_id, choose_void
    )
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with (
        capture_reloads(hass) as reload,
        patch.object(manager, "async_correct_asset_replacement") as correct_call,
        patch.object(manager, "async_create_asset_replacement") as create_call,
    ):
        unconfirmed = await hass.config_entries.options.async_configure(
            flow_id, {CONF_VOID_REASON: "Recorded in error", CONF_CONFIRM_VOID: False}
        )
        await hass.config_entries.options.async_configure(flow_id, choose_void)
        unexplained = await hass.config_entries.options.async_configure(
            flow_id, {CONF_VOID_REASON: "  ", CONF_CONFIRM_VOID: True}
        )
        assert manager._data == before
        manager._store.async_save.assert_not_awaited()
        reload.assert_not_called()

        voided = await hass.config_entries.options.async_configure(
            flow_id, {CONF_VOID_REASON: "Recorded in error", CONF_CONFIRM_VOID: True}
        )

    assert confirm["step_id"] == VOID
    # Declining returns to the choice it came from, with nothing voided.
    assert unconfirmed["step_id"] == MANAGE
    assert unexplained["step_id"] == VOID
    assert unexplained["errors"] == {"base": "replacement_void_reason_required"}
    assert voided["type"] is FlowResultType.MENU
    assert voided["step_id"] == SUBMENU
    assert voided["description_placeholders"]["result"] == "Replacement updated."
    manager._store.async_save.assert_awaited_once()
    reload.assert_called_once()
    correct_call.assert_not_called()
    create_call.assert_not_called()
    assert manager.replacement_records_for_asset(ASSET_UUID) == []
    stored = manager.replacement_record(record["replacement_uuid"])
    assert stored["voided_at"] is not None
    assert stored["void_reason"] == "Recorded in error"
    # Voiding moves no Asset, Lifecycle, installation, Area or Purchase.
    assert manager._data["assets"] == before["assets"]
    assert manager._data["lifecycle_events"] == before["lifecycle_events"]
    assert manager._data["purchases"] == before["purchases"]
