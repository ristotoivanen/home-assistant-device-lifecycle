"""Every OptionsFlow form's button says what pressing it does.

A form that only chooses where to go next continues; a form that changes
something saves, adds, removes or voids, and then returns; the Asset
picker opens; the last Quick Add step adds the device. No form falls back
to Home Assistant's generic Submit.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_REPLACEMENT_ACTION,
    CONF_REPLACEMENT_UUID,
    REPLACEMENT_ACTION_CORRECT,
)

from .conftest import capture_reloads
from .test_options_flow import _manager, _options_flow

INTEGRATION = Path(__file__).parents[1] / "custom_components" / "device_lifecycle"
CONTINUE = ("Continue", "Jatka")
SAVE_AND_RETURN = ("Save and return", "Tallenna ja palaa")
# Form step -> (English, Finnish) button text, by what the step does.
SUBMIT = {
    # Opens the chosen Asset's hub.
    "manage_asset": ("Open", "Avaa"),
    # Change one thing, then return.
    "edit_asset_metadata": SAVE_AND_RETURN,
    "change_asset_purchase": SAVE_AND_RETURN,
    "asset_deployment": SAVE_AND_RETURN,
    "confirm_not_deployed": SAVE_AND_RETURN,
    "asset_lifecycle": SAVE_AND_RETURN,
    "confirm_disposed": SAVE_AND_RETURN,
    "replacement_replaces": SAVE_AND_RETURN,
    "correct_asset_replacement": SAVE_AND_RETURN,
    "manage_primary_device": SAVE_AND_RETURN,
    "add_related_device": ("Add and return", "Lisää ja palaa"),
    "remove_related_device": ("Remove and return", "Poista ja palaa"),
    "confirm_void_replacement": ("Void and return", "Mitätöi ja palaa"),
    # Only choose where to go next.
    "manage_asset_replacement": CONTINUE,
    "quick_add_from_ha": CONTINUE,
    "quick_add_details": CONTINUE,
    "quick_add_replacement": CONTINUE,
    # Creates the Asset and ends the flow.
    "quick_add_confirm": ("Add device", "Lisää laite"),
}
GENERIC = {"submit", "lähetä", "ok", "save", "tallenna"}


def _steps(language: str) -> dict[str, Any]:
    """Return one language's OptionsFlow steps."""
    return json.loads(
        (INTEGRATION / "translations" / f"{language}.json").read_text(encoding="utf-8")
    )["options"]["step"]


def _options_flow_form_steps() -> set[str]:
    """Return every form step the OptionsFlow can show, read from its code."""
    source = (INTEGRATION / "config_flow.py").read_text(encoding="utf-8")
    body = source[
        source.index("class DeviceLifecycleOptionsFlow")
        : source.index("class PurchaseSubentryFlow")
    ]
    literal = set(
        re.findall(r'async_show_form\(\s*step_id="(\w+)"', body)
    )
    # The replacement creation form passes its step id through a helper.
    dynamic = set(re.findall(r'step_id="(replacement_\w+)",\n\s+user_input', body))
    return literal | dynamic


def test_the_matrix_covers_every_form_the_flow_can_show() -> None:
    """A new form cannot appear without deciding what its button says."""
    assert _options_flow_form_steps() == set(SUBMIT)


@pytest.mark.parametrize("language", ["en", "fi"])
def test_every_form_names_its_action(language: str) -> None:
    """The button reads as the action, never as a generic Submit or Save."""
    steps = _steps(language)
    forms = {step for step, copy in steps.items() if "menu_options" not in copy}

    assert forms == set(SUBMIT)
    for step, (english, finnish) in SUBMIT.items():
        label = steps[step]["submit"]
        assert label == (english if language == "en" else finnish), step
        assert label.casefold() not in GENERIC, step


@pytest.mark.parametrize("language", ["en", "fi"])
def test_menus_have_no_button_to_name(language: str) -> None:
    """Menus navigate by their rows, so a submit label would be dead copy."""
    for step, copy in _steps(language).items():
        if "menu_options" in copy:
            assert "submit" not in copy, step


async def test_continue_writes_nothing(hass: HomeAssistant) -> None:
    """Choosing a relationship and an action only routes to the next form."""
    manager = _manager(hass)
    old = await manager.async_create_manual_asset(name="Old heater")
    new = await manager.async_create_manual_asset(name="New heater")
    record = await manager.async_create_asset_replacement(
        old["asset_uuid"], new["asset_uuid"],
        reason="failure", effective_date=None, notes=None,
    )
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: new["asset_uuid"]})
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        routed = await flow.async_step_manage_asset_replacement(
            {
                CONF_REPLACEMENT_UUID: record["replacement_uuid"],
                CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_CORRECT,
            }
        )

    assert routed["type"] is FlowResultType.FORM
    assert routed["step_id"] == "correct_asset_replacement"
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()
