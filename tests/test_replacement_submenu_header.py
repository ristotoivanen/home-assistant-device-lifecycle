"""The Replacement submenu reads like the Lifecycle & replacement section.

Its header names the Asset on its own line, then a Replacement heading with
one line per direction, then the one instruction a person needs here: a
relationship is recorded from the new Asset. The direction lines are the
section's own, so both views always agree. The operations below the header
are unchanged, and opening the submenu is only a read.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_REPLACEMENT_REASON,
    CONF_REPLACEMENT_TARGET_ASSET_UUID,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import ASSET_UUID, capture_reloads
from .test_explicit_selection_safety import _flow_manager_on_asset
from .test_options_flow import _manager, _options_flow

SUBMENU = "asset_replacement"
SECTION = "asset_lifecycle_replacement_menu"
# Back returns to the section the submenu was opened from.
BACK = SECTION
TRANSLATIONS = (
    Path(__file__).parents[1]
    / "custom_components"
    / "device_lifecycle"
    / "translations"
)
COPY = {
    "en": {
        "heading": "**Replacement**",
        "replaces": "This Asset replaces: ",
        "replaced_by": "This Asset was replaced by: ",
        "none": "No active relationship",
        "unavailable": "Unavailable Asset",
        "instruction": (
            "A replacement relationship is always recorded from the new Asset."
        ),
        # The domain-separation paragraph this header no longer repeats.
        "retired": ("does not transfer", "Purchases", "Runtime", "physical Assets only"),
        "actions": {
            "replacement_replaces": "This Asset replaces…",
            "manage_asset_replacement": "Manage existing replacement",
            BACK: "← Back to lifecycle & replacement",
        },
    },
    "fi": {
        "heading": "**Korvaaminen**",
        "replaces": "Tämä laite korvaa: ",
        "replaced_by": "Tämän laitteen korvasi: ",
        "none": "Ei aktiivista suhdetta",
        "unavailable": "Laite ei ole enää käytettävissä",
        "instruction": "Korvaussuhde kirjataan aina uudesta laitteesta.",
        "retired": ("eikä siirrä", "ostoksia", "käyttötunteja", "vain fyysiset"),
        "actions": {
            "replacement_replaces": "Tämä laite korvaa…",
            "manage_asset_replacement": "Hallitse nykyistä korvaussuhdetta",
            BACK: "← Takaisin elinkaareen ja korvaamiseen",
        },
    },
}


def _step(language: str) -> dict[str, Any]:
    """Return the translated Replacement submenu step."""
    return json.loads(
        (TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8")
    )["options"]["step"][SUBMENU]


def _rendered(language: str, placeholders: dict[str, str]) -> list[str]:
    """Render the header the way the frontend substitutes placeholders.

    Blank lines separate Markdown paragraphs, so they are dropped here: what
    is left is what a person reads, line by line.
    """
    text = _step(language)["description"].format_map(placeholders)
    return [line for line in text.split("\n") if line.strip()]


def _label(asset: dict[str, Any]) -> str:
    """Name an Asset the way the header does."""
    return f"{asset['name']} · {asset['asset_id']}"


async def _on(hass: HomeAssistant, manager: AssetStoreManager, asset_uuid: str):
    """Return a flow on one Asset's hub."""
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
        notes="Replacement note that must stay hidden",
    )


@pytest.mark.parametrize("language", ["en", "fi"])
def test_the_header_is_short_lines_not_a_paragraph(language: str) -> None:
    """Asset, result, heading, the directions, then one instruction."""
    copy = COPY[language]
    description = _step(language)["description"]

    assert description == (
        "{asset}\n\n{result}\n\n"
        f"{copy['heading']}\n{{replacement}}\n\n"
        f"{copy['instruction']}"
    )
    for phrase in copy["retired"]:
        assert phrase not in description, phrase


@pytest.mark.parametrize("language", ["en", "fi"])
def test_the_actions_keep_their_labels(language: str) -> None:
    """The operations keep their labels; Back names the section it returns to."""
    assert _step(language)["menu_options"] == COPY[language]["actions"]


@pytest.mark.parametrize("language", ["en", "fi"])
@pytest.mark.parametrize(
    "state",
    ["none", "replaces", "replaced_by", "middle", "voided", "unavailable"],
)
async def test_each_direction_reads_on_its_own_line(
    hass: HomeAssistant,
    language: str,
    state: str,
) -> None:
    """Every relationship state renders as the same short lines."""
    hass.config.language = language
    copy = COPY[language]
    manager = _manager(hass)
    old = await manager.async_create_manual_asset(name="Old heater")
    current = await manager.async_create_manual_asset(name="Current heater")
    new = await manager.async_create_manual_asset(name="New heater")
    records = []
    if state in ("replaces", "middle", "unavailable"):
        records.append(await _replace(manager, old, current))
    if state in ("replaced_by", "middle", "voided"):
        records.append(await _replace(manager, current, new))
    if state == "voided":
        await manager.async_void_asset_replacement(
            records[-1]["replacement_uuid"], void_reason="Recorded in error"
        )
    if state == "unavailable":
        # A validated Store cannot hold this; the header must still not leak.
        manager._data["assets"].pop(old["asset_uuid"])
    replaces, replaced_by = {
        "none": (copy["none"], copy["none"]),
        "replaces": (_label(old), copy["none"]),
        "replaced_by": (copy["none"], _label(new)),
        "middle": (_label(old), _label(new)),
        "voided": (copy["none"], copy["none"]),
        "unavailable": (copy["unavailable"], copy["none"]),
    }[state]
    flow = await _on(hass, manager, current["asset_uuid"])

    submenu = await flow.async_step_asset_replacement()
    lines = _rendered(language, submenu["description_placeholders"])

    assert lines == [
        "Current heater · DL0002",
        copy["heading"],
        f"{copy['replaces']}{replaces}",
        f"{copy['replaced_by']}{replaced_by}",
        copy["instruction"],
    ]
    shown = "\n".join(lines)
    for hidden in (
        old["asset_uuid"],
        current["asset_uuid"],
        new["asset_uuid"],
        *(record["replacement_uuid"] for record in records),
        "→",
        "Replacement note",
    ):
        assert hidden not in shown, hidden


async def test_the_header_and_the_section_say_the_same_thing(
    hass: HomeAssistant,
) -> None:
    """Both views render the relationships from one helper."""
    manager = _manager(hass)
    old = await manager.async_create_manual_asset(name="Old heater")
    current = await manager.async_create_manual_asset(name="Current heater")
    new = await manager.async_create_manual_asset(name="New heater")
    await _replace(manager, old, current)
    await _replace(manager, current, new)
    flow = await _on(hass, manager, current["asset_uuid"])

    submenu = await flow.async_step_asset_replacement()
    section = await flow.async_step_asset_lifecycle_replacement_menu()

    for key in ("asset", "replacement"):
        assert submenu["description_placeholders"][key] == (
            section["description_placeholders"][key]
        )


async def test_a_long_name_keeps_the_asset_line_short(
    hass: HomeAssistant,
) -> None:
    """The Asset line shortens the name like every other read-only view."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(
        name="Very long heater name that keeps going past the row"
    )
    flow = await _on(hass, manager, asset["asset_uuid"])

    submenu = await flow.async_step_asset_replacement()

    assert submenu["description_placeholders"]["asset"] == (
        "Very long heater name t… · DL0001"
    )


async def test_opening_the_submenu_writes_nothing(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """End to end: the header is a read, with no write, reload or result."""
    manager = _manager(hass, asset_store_data)
    successor = await manager.async_create_manual_asset(name="Successor unit")
    await _replace(manager, manager.asset(ASSET_UUID), successor)
    flow_id = await _flow_manager_on_asset(hass, manager, ASSET_UUID)
    await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": SECTION}
    )
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        submenu = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": SUBMENU}
        )

    assert submenu["type"] is FlowResultType.MENU
    assert submenu["menu_options"] == [
        "replacement_replaces",
        "manage_asset_replacement",
        BACK,
    ]
    assert submenu["description_placeholders"]["result"] == ""
    assert _rendered("en", submenu["description_placeholders"])[:2] == [
        "Workshop device · DL0007",
        "**Replacement**",
    ]
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()
    assert manager._data == before


async def test_a_result_sits_between_the_asset_and_the_facts(
    hass: HomeAssistant,
) -> None:
    """After recording, the result reads once, and the facts are fresh."""
    manager = _manager(hass)
    old = await manager.async_create_manual_asset(name="Old heater")
    new = await manager.async_create_manual_asset(name="New heater")
    flow = await _on(hass, manager, new["asset_uuid"])

    with capture_reloads(hass):
        recorded = await flow.async_step_replacement_replaces(
            {
                CONF_REPLACEMENT_TARGET_ASSET_UUID: old["asset_uuid"],
                CONF_REPLACEMENT_REASON: "failure",
            }
        )

    assert recorded["step_id"] == SUBMENU
    assert _rendered("en", recorded["description_placeholders"]) == [
        _label(new),
        "Replacement updated.",
        "**Replacement**",
        f"This Asset replaces: {_label(old)}",
        "This Asset was replaced by: No active relationship",
        COPY["en"]["instruction"],
    ]
