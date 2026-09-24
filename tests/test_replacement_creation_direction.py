"""A replacement relationship is created only from the new Asset.

The new Asset names the Asset it replaces, "This Asset replaces…", so the
Asset being managed is always the successor and the chosen one always its
predecessor. The old Asset shows what replaced it, read only, once the new
Asset has recorded it. There is no second, inverse way to create the same
relationship.

The Store model is unchanged: a record is a predecessor and a successor,
whichever Asset recorded it, so relationships created before this change
read and manage exactly as before.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import InvalidData
from homeassistant.helpers import area_registry as ar

from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_CONFIRM_VOID,
    CONF_EFFECTIVE_DATE,
    CONF_PREDECESSOR_ASSET_UUID,
    CONF_REPLACEMENT_ACTION,
    CONF_REPLACEMENT_REASON,
    CONF_REPLACEMENT_TARGET_ASSET_UUID,
    CONF_REPLACEMENT_UUID,
    CONF_SUCCESSOR_ASSET_UUID,
    CONF_VOID_REASON,
    DEPLOYMENT_STATE_DEPLOYED,
    REPLACEMENT_ACTION_CORRECT,
    REPLACEMENT_ACTION_VOID,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    AssetStoreManager,
    _validate_store_data,
)

from .conftest import ASSET_UUID, DEVICE_ID, capture_reloads
from .test_explicit_selection_safety import _flow_manager_on_asset
from .test_options_flow import _manager, _options_flow

HUB = "manage_asset_menu"
SECTION = "asset_lifecycle_replacement_menu"
SUBMENU = "asset_replacement"
CREATE = "replacement_replaces"
INVERSE = "replacement_replaced_by"
MANAGE = "manage_asset_replacement"
BACK = "manage_asset_menu"
TRANSLATIONS = (
    Path(__file__).parents[1]
    / "custom_components"
    / "device_lifecycle"
    / "translations"
)
NO_RELATIONSHIP = "No active relationship"


def _translations(language: str) -> dict[str, Any]:
    """Return one language's translation file."""
    return json.loads(
        (TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8")
    )


def _label(asset: dict[str, Any]) -> str:
    """Name an Asset the way the section does."""
    return f"{asset['name']} · {asset['asset_id']}"


async def _configure(hass: HomeAssistant, flow_id: str, user_input: dict) -> dict:
    """Send one step through Home Assistant's own flow manager."""
    return await hass.config_entries.options.async_configure(flow_id, user_input)


async def _switch_to(hass: HomeAssistant, flow_id: str, asset_uuid: str) -> dict:
    """From the hub, choose another Asset and land on its hub."""
    await _configure(hass, flow_id, {"next_step_id": "manage_asset"})
    hub = await _configure(hass, flow_id, {CONF_ASSET_UUID: asset_uuid})
    assert hub["step_id"] == HUB
    return hub


async def _record_replaces(
    hass: HomeAssistant,
    flow_id: str,
    predecessor_uuid: str,
) -> dict:
    """From the hub, record that the current Asset replaces another one.

    The only creation path the UI offers: Lifecycle & replacement, Manage
    replacement, This Asset replaces…, then back to the hub.
    """
    await _configure(hass, flow_id, {"next_step_id": SECTION})
    submenu = await _configure(hass, flow_id, {"next_step_id": SUBMENU})
    assert INVERSE not in submenu["menu_options"]
    form = await _configure(hass, flow_id, {"next_step_id": CREATE})
    assert form["step_id"] == CREATE
    with capture_reloads(hass):
        recorded = await _configure(
            hass,
            flow_id,
            {
                CONF_REPLACEMENT_TARGET_ASSET_UUID: predecessor_uuid,
                CONF_REPLACEMENT_REASON: "failure",
                CONF_EFFECTIVE_DATE: "2026-09-12",
            },
        )
    assert recorded["step_id"] == SUBMENU
    assert recorded["description_placeholders"]["result"] == "Replacement updated."
    await _configure(hass, flow_id, {"next_step_id": BACK})
    return recorded


async def _section_lines(hass: HomeAssistant, flow_id: str) -> list[str]:
    """Open Lifecycle & replacement from the hub, read it, and go back."""
    section = await _configure(hass, flow_id, {"next_step_id": SECTION})
    await _configure(hass, flow_id, {"next_step_id": BACK})
    return section["description_placeholders"]["replacement"].split("\n")


async def _submenu_options(hass: HomeAssistant, flow_id: str) -> list[str]:
    """Open the replacement submenu from the hub, read it, and go back."""
    await _configure(hass, flow_id, {"next_step_id": SECTION})
    submenu = await _configure(hass, flow_id, {"next_step_id": SUBMENU})
    await _configure(hass, flow_id, {"next_step_id": BACK})
    return submenu["menu_options"]


async def _chain(hass: HomeAssistant, manager: AssetStoreManager):
    """Build Old -> Middle -> New through the UI, each from the newer Asset."""
    old = await manager.async_create_manual_asset(name="Old heater")
    middle = await manager.async_create_manual_asset(name="Middle heater")
    new = await manager.async_create_manual_asset(name="New heater")
    flow_id = await _flow_manager_on_asset(hass, manager, middle["asset_uuid"])
    await _record_replaces(hass, flow_id, old["asset_uuid"])
    await _switch_to(hass, flow_id, new["asset_uuid"])
    await _record_replaces(hass, flow_id, middle["asset_uuid"])
    return flow_id, old, middle, new


async def _on(hass: HomeAssistant, manager: AssetStoreManager, asset_uuid: str):
    """Return a flow on one Asset, driven directly."""
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset_uuid})
    return flow


@pytest.mark.parametrize(
    ("state", "manage"),
    [
        pytest.param("none", False, id="no-relationship"),
        pytest.param("replaces", True, id="replaces-only"),
        pytest.param("replaced_by", True, id="replaced-by-only"),
        pytest.param("middle", True, id="middle-of-chain"),
    ],
)
async def test_the_submenu_creates_only_from_the_new_asset(
    hass: HomeAssistant,
    state: str,
    manage: bool,
) -> None:
    """One create row in every state; management only once there is a record."""
    manager = _manager(hass)
    old = await manager.async_create_manual_asset(name="Old heater")
    current = await manager.async_create_manual_asset(name="Current heater")
    new = await manager.async_create_manual_asset(name="New heater")
    if state in ("replaces", "middle"):
        await manager.async_create_asset_replacement(
            old["asset_uuid"], current["asset_uuid"],
            reason="failure", effective_date=None, notes=None,
        )
    if state in ("replaced_by", "middle"):
        await manager.async_create_asset_replacement(
            current["asset_uuid"], new["asset_uuid"],
            reason="upgrade", effective_date=None, notes=None,
        )
    flow = await _on(hass, manager, current["asset_uuid"])

    submenu = await flow.async_step_asset_replacement()

    assert submenu["menu_options"] == (
        [CREATE, MANAGE, BACK] if manage else [CREATE, BACK]
    )
    assert INVERSE not in submenu["menu_options"]


async def test_the_inverse_creation_step_no_longer_exists(
    hass: HomeAssistant,
) -> None:
    """No handler, no copy, and Home Assistant refuses it as a menu choice."""
    manager = _manager(hass)
    current = await manager.async_create_manual_asset(name="Current heater")
    flow_id = await _flow_manager_on_asset(hass, manager, current["asset_uuid"])
    await _configure(hass, flow_id, {"next_step_id": SECTION})
    await _configure(hass, flow_id, {"next_step_id": SUBMENU})
    before = deepcopy(manager._data)

    with pytest.raises(InvalidData):
        await _configure(hass, flow_id, {"next_step_id": INVERSE})

    flow = await _on(hass, manager, current["asset_uuid"])
    assert not hasattr(flow, f"async_step_{INVERSE}")
    for language in ("en", "fi"):
        steps = _translations(language)["options"]["step"]
        assert INVERSE not in steps
        assert INVERSE not in steps[SUBMENU]["menu_options"]
    assert manager._data == before


@pytest.mark.parametrize(
    ("language", "create_label", "fact", "state", "hint"),
    [
        (
            "en",
            "This Asset replaces…",
            "This Asset was replaced by",
            "Replaced by",
            "A replacement is always recorded from the new Asset.",
        ),
        (
            "fi",
            "Tämä laite korvaa…",
            "Tämän laitteen korvasi",
            "Korvattu laitteella",
            "Korvaussuhde kirjataan aina uudesta laitteesta",
        ),
    ],
)
def test_the_inverse_stays_as_read_only_wording(
    language: str,
    create_label: str,
    fact: str,
    state: str,
    hint: str,
) -> None:
    """The action is gone; the fact and the sensor state keep their words."""
    data = _translations(language)
    submenu = data["options"]["step"][SUBMENU]["menu_options"]

    assert submenu[CREATE] == create_label
    # The old Asset's submenu says where its relationship is recorded.
    assert hint in data["options"]["step"][SUBMENU]["description"]
    assert not any(label.startswith(fact) for label in submenu.values())
    assert data["entity"]["sensor"]["replacement"]["state"]["replaced_by"] == state


async def test_the_current_asset_is_always_the_successor(
    hass: HomeAssistant,
) -> None:
    """End to end: the chosen Asset is stored as the predecessor, old -> new."""
    manager = _manager(hass)
    old = await manager.async_create_manual_asset(name="Old heater")
    new = await manager.async_create_manual_asset(name="New heater")
    flow_id = await _flow_manager_on_asset(hass, manager, new["asset_uuid"])

    await _record_replaces(hass, flow_id, old["asset_uuid"])

    records = manager.replacement_records_for_asset(new["asset_uuid"])
    assert len(records) == 1
    assert records[0]["predecessor_asset_uuid"] == old["asset_uuid"]
    assert records[0]["successor_asset_uuid"] == new["asset_uuid"]
    assert records[0]["reason"] == "failure"
    assert records[0]["effective_date"] == "2026-09-12"
    assert await _section_lines(hass, flow_id) == [
        f"This Asset replaces: {_label(old)}",
        f"This Asset was replaced by: {NO_RELATIONSHIP}",
    ]


async def test_the_old_asset_shows_what_replaced_it_read_only(
    hass: HomeAssistant,
) -> None:
    """End to end: the replaced Asset reads its successor, with no way to invert."""
    manager = _manager(hass)
    old = await manager.async_create_manual_asset(name="Old heater")
    new = await manager.async_create_manual_asset(name="New heater")
    flow_id = await _flow_manager_on_asset(hass, manager, new["asset_uuid"])
    await _record_replaces(hass, flow_id, old["asset_uuid"])

    await _switch_to(hass, flow_id, old["asset_uuid"])

    assert await _section_lines(hass, flow_id) == [
        f"This Asset replaces: {NO_RELATIONSHIP}",
        f"This Asset was replaced by: {_label(new)}",
    ]
    assert await _submenu_options(hass, flow_id) == [CREATE, MANAGE, BACK]


async def test_a_middle_asset_reads_both_directions_and_creates_one(
    hass: HomeAssistant,
) -> None:
    """End to end: Old -> Middle -> New, recorded each time from the newer Asset."""
    manager = _manager(hass)
    flow_id, old, middle, new = await _chain(hass, manager)

    await _switch_to(hass, flow_id, middle["asset_uuid"])
    lines = await _section_lines(hass, flow_id)
    options = await _submenu_options(hass, flow_id)

    assert lines == [
        f"This Asset replaces: {_label(old)}",
        f"This Asset was replaced by: {_label(new)}",
    ]
    assert options == [CREATE, MANAGE, BACK]
    assert manager.active_replacement_predecessor(middle["asset_uuid"])[
        "asset_uuid"
    ] == old["asset_uuid"]
    assert manager.active_replacement_successor(middle["asset_uuid"])[
        "asset_uuid"
    ] == new["asset_uuid"]


async def test_a_middle_asset_reads_both_directions_in_finnish(
    hass: HomeAssistant,
) -> None:
    """The two facts are whole Finnish sentences, one per direction."""
    manager = _manager(hass)
    _flow_id, old, middle, new = await _chain(hass, manager)
    hass.config.language = "fi"
    flow = await _on(hass, manager, middle["asset_uuid"])

    section = await flow.async_step_asset_lifecycle_replacement_menu()

    assert section["description_placeholders"]["replacement"].split("\n") == [
        f"Tämä laite korvaa: {_label(old)}",
        f"Tämän laitteen korvasi: {_label(new)}",
    ]


async def test_recording_a_replacement_changes_nothing_else(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """End to end: no Lifecycle, installation, Area, Purchase, warranty,
    Home Assistant device or identity moves on either Asset."""
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
        old["asset_uuid"], "active", effective_date="2025-01-02", notes=None
    )
    await manager.async_set_asset_deployment_reporting(
        old["asset_uuid"],
        deployment_state=DEPLOYMENT_STATE_DEPLOYED,
        ha_area_id=workshop.id,
    )
    flow_id = await _flow_manager_on_asset(hass, manager, ASSET_UUID)
    before = deepcopy(manager._data)

    await _record_replaces(hass, flow_id, old["asset_uuid"])

    after = manager._data
    # Both Assets, with their Lifecycle, installation, Area, Purchase,
    # warranty and Home Assistant devices, are exactly as they were.
    assert after["assets"] == before["assets"]
    assert after["lifecycle_events"] == before["lifecycle_events"]
    assert after["purchases"] == before["purchases"]
    assert after["next_asset_number"] == before["next_asset_number"]
    new_asset = after["assets"][ASSET_UUID]
    assert new_asset["ha_device_refs"][0]["device_id"] == DEVICE_ID
    assert new_asset["lifecycle"]["status"] == "active"
    assert after["assets"][old["asset_uuid"]]["lifecycle"]["status"] == "active"
    # The only change is the one relationship.
    added = set(after["replacement_records"]) - set(before["replacement_records"])
    assert len(added) == 1
    record = after["replacement_records"][added.pop()]
    assert (record["predecessor_asset_uuid"], record["successor_asset_uuid"]) == (
        old["asset_uuid"],
        ASSET_UUID,
    )


async def test_a_middle_asset_manages_either_relationship(
    hass: HomeAssistant,
) -> None:
    """Management still lists both records, voids one and corrects the other."""
    manager = _manager(hass)
    _flow_id, _old, middle, new = await _chain(hass, manager)
    other = await manager.async_create_manual_asset(name="Other heater")
    flow = await _on(hass, manager, middle["asset_uuid"])
    records = {
        record["successor_asset_uuid"]: record
        for record in manager.replacement_records_for_asset(middle["asset_uuid"])
    }
    to_new = records[new["asset_uuid"]]
    from_old = records[middle["asset_uuid"]]

    manage = await flow.async_step_manage_asset_replacement()
    choices = next(
        validator
        for marker, validator in manage["data_schema"].schema.items()
        if marker == CONF_REPLACEMENT_UUID
    ).config["options"]
    assert {choice["value"] for choice in choices} == {
        to_new["replacement_uuid"],
        from_old["replacement_uuid"],
    }

    await flow.async_step_manage_asset_replacement(
        {
            CONF_REPLACEMENT_UUID: to_new["replacement_uuid"],
            CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_VOID,
        }
    )
    with capture_reloads(hass):
        voided = await flow.async_step_confirm_void_replacement(
            {CONF_VOID_REASON: "Recorded on the wrong unit", CONF_CONFIRM_VOID: True}
        )
    await flow.async_step_manage_asset_replacement(
        {
            CONF_REPLACEMENT_UUID: from_old["replacement_uuid"],
            CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_CORRECT,
        }
    )
    with capture_reloads(hass):
        corrected = await flow.async_step_correct_asset_replacement(
            {
                CONF_PREDECESSOR_ASSET_UUID: other["asset_uuid"],
                CONF_SUCCESSOR_ASSET_UUID: middle["asset_uuid"],
                CONF_REPLACEMENT_REASON: "upgrade",
                CONF_VOID_REASON: "Wrong predecessor",
            }
        )
    section = await flow.async_step_asset_lifecycle_replacement_menu()

    assert voided["step_id"] == corrected["step_id"] == SUBMENU
    assert manager.active_replacement_successor(middle["asset_uuid"]) is None
    assert manager.active_replacement_predecessor(middle["asset_uuid"])[
        "asset_uuid"
    ] == other["asset_uuid"]
    # Voided records are history: management no longer offers them.
    remaining = manager.replacement_records_for_asset(middle["asset_uuid"])
    assert len(remaining) == 1
    assert section["description_placeholders"]["replacement"].split("\n") == [
        f"This Asset replaces: {_label(other)}",
        f"This Asset was replaced by: {NO_RELATIONSHIP}",
    ]


async def test_existing_records_need_no_migration(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A record once created from the old Asset is just old -> new in the Store.

    The removed inverse form made exactly this manager call, with the managed
    Asset as predecessor; the record it left behind validates, reads and
    manages like any other.
    """
    manager = _manager(hass, asset_store_data)
    successor = await manager.async_create_manual_asset(name="Successor unit")
    record = await manager.async_create_asset_replacement(
        ASSET_UUID,
        successor["asset_uuid"],
        reason="failure",
        effective_date="2026-01-02",
        notes="Recorded from the old Asset in 0.7.4",
    )
    _validate_store_data(deepcopy(manager._data))

    old_flow = await _on(hass, manager, ASSET_UUID)
    old_section = await old_flow.async_step_asset_lifecycle_replacement_menu()
    old_submenu = await old_flow.async_step_asset_replacement()
    new_flow = await _on(hass, manager, successor["asset_uuid"])
    new_section = await new_flow.async_step_asset_lifecycle_replacement_menu()
    manage = await old_flow.async_step_manage_asset_replacement(
        {
            CONF_REPLACEMENT_UUID: record["replacement_uuid"],
            CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_VOID,
        }
    )

    assert old_section["description_placeholders"]["replacement"].split("\n")[1] == (
        f"This Asset was replaced by: {_label(successor)}"
    )
    assert new_section["description_placeholders"]["replacement"].split("\n")[0] == (
        "This Asset replaces: Workshop device · DL0007"
    )
    assert old_submenu["menu_options"] == [CREATE, MANAGE, BACK]
    assert manage["step_id"] == "confirm_void_replacement"
