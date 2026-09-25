"""Every Device Lifecycle form says what its button does.

Home Assistant labels a form's button with its generic Submit / Lähetä
unless the step's translation names the action. Asset management and the
Purchase and Runtime tracking forms all name it, so no Device Lifecycle form
falls back to the generic label.

The label follows what the step really does:

- a step that only leads to the next form continues: Continue / Jatka
- a step that creates a Purchase or Runtime tracking repeats the action the
  person started from: Add purchase / Lisää ostos, Add runtime tracking /
  Lisää käyttötuntiseuranta
- a step that saves changes to an existing one: Save changes / Tallenna
  muutokset

Adding and saving end the flow on Home Assistant's own confirmation, which
the person closes, so these labels do not promise an immediate return.
"""

from __future__ import annotations

import ast
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle.const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
    CONF_PURCHASE_DATE,
    CONF_PURCHASE_NAME,
    CONF_RUNTIME_MODE,
    CONF_SOURCE_ENTITY_ID,
    CONF_WARRANTY_TYPE,
    RUNTIME_MODE_ON,
    RUNTIME_MODE_POWER,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
    WARRANTY_NONE,
)
from custom_components.device_lifecycle.models import AssetStoreData

from .conftest import SOURCE_ENTITY_ID, capture_reloads
from .test_exposure_options_reload import (
    _power_source_input,
    _setup_runtime_entry,
    _verified_store_readback,
)
from .test_stale_device_registry_references import _recorded_saves

pytestmark = pytest.mark.real_reload

INTEGRATION = Path(__file__).parents[1] / "custom_components" / "device_lifecycle"
GENERIC = {"submit", "lähetä"}
ROUTE = {"en": "Continue", "fi": "Jatka"}
SAVE = {"en": "Save changes", "fi": "Tallenna muutokset"}

# What each Purchase and Runtime tracking form's button says.
SUBENTRY_LABELS = {
    "en": {
        ("purchase", "user"): "Add purchase",
        ("purchase", "reconfigure"): "Save changes",
        ("runtime", "user"): "Continue",
        ("runtime", "runtime_source"): "Add runtime tracking",
        ("runtime", "reconfigure"): "Continue",
        ("runtime", "reconfigure_source"): "Save changes",
    },
    "fi": {
        ("purchase", "user"): "Lisää ostos",
        ("purchase", "reconfigure"): "Tallenna muutokset",
        ("runtime", "user"): "Jatka",
        ("runtime", "runtime_source"): "Lisää käyttötuntiseuranta",
        ("runtime", "reconfigure"): "Jatka",
        ("runtime", "reconfigure_source"): "Tallenna muutokset",
    },
}

# Asset management labels, unchanged by the subentry work.
OPTIONS_LABELS = {
    "en": {
        "add_related_device": "Add and return",
        "asset_deployment": "Save and return",
        "asset_lifecycle": "Save and return",
        "change_asset_purchase": "Save and return",
        "confirm_disposed": "Save and return",
        "confirm_not_deployed": "Save and return",
        "confirm_void_replacement": "Void and return",
        "correct_asset_replacement": "Save and return",
        "edit_asset_metadata": "Save and return",
        "manage_asset": "Open",
        "manage_asset_replacement": "Continue",
        "manage_primary_device": "Save and return",
        "quick_add_confirm": "Add device",
        "quick_add_details": "Continue",
        "quick_add_from_ha": "Continue",
        "quick_add_replacement": "Continue",
        "remove_related_device": "Remove and return",
        "replacement_replaces": "Save and return",
    },
    "fi": {
        "add_related_device": "Lisää ja palaa",
        "asset_deployment": "Tallenna ja palaa",
        "asset_lifecycle": "Tallenna ja palaa",
        "change_asset_purchase": "Tallenna ja palaa",
        "confirm_disposed": "Tallenna ja palaa",
        "confirm_not_deployed": "Tallenna ja palaa",
        "confirm_void_replacement": "Mitätöi ja palaa",
        "correct_asset_replacement": "Tallenna ja palaa",
        "edit_asset_metadata": "Tallenna ja palaa",
        "manage_asset": "Avaa",
        "manage_asset_replacement": "Jatka",
        "manage_primary_device": "Tallenna ja palaa",
        "quick_add_confirm": "Lisää laite",
        "quick_add_details": "Jatka",
        "quick_add_from_ha": "Jatka",
        "quick_add_replacement": "Jatka",
        "remove_related_device": "Poista ja palaa",
        "replacement_replaces": "Tallenna ja palaa",
    },
}


def _translations(language: str) -> dict[str, Any]:
    """Return one language's translation file."""
    return json.loads(
        (INTEGRATION / "translations" / f"{language}.json").read_text(
            encoding="utf-8"
        )
    )


def _form_labels(language: str) -> dict[tuple[str, str], str | None]:
    """Return every form step's button label, keyed by (flow, step)."""
    data = _translations(language)
    labels: dict[tuple[str, str], str | None] = {
        ("options", step): copy.get("submit")
        for step, copy in data["options"]["step"].items()
        if "menu_options" not in copy
    }
    for kind, subentry in data["config_subentries"].items():
        for step, copy in subentry["step"].items():
            labels[(kind, step)] = copy.get("submit")
    for step, copy in data["config"]["step"].items():
        labels[("config", step)] = copy.get("submit")
    return labels


def _steps_shown_by_code() -> dict[str, set[str]]:
    """Return the step IDs each flow class can show as a form or a menu.

    Literal `step_id` keywords are collected from every call in the class,
    so a form shown through a helper is found as well as a direct one.
    """
    tree = ast.parse((INTEGRATION / "config_flow.py").read_text(encoding="utf-8"))
    flows = {
        "DeviceLifecycleConfigFlow": "config",
        "DeviceLifecycleOptionsFlow": "options",
        "PurchaseSubentryFlow": "purchase",
        "RuntimeSubentryFlow": "runtime",
    }
    shown: dict[str, set[str]] = {flow: set() for flow in flows.values()}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name not in flows:
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            for keyword in call.keywords:
                if (
                    keyword.arg == "step_id"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    shown[flows[node.name]].add(keyword.value.value)
    return shown


# --- Every form names its action -------------------------------------------


@pytest.mark.parametrize("language", ["en", "fi"])
def test_no_form_falls_back_to_the_generic_submit(language: str) -> None:
    """Asset management, Purchase and Runtime tracking forms all say what."""
    labels = _form_labels(language)

    missing = sorted(step for step, label in labels.items() if not label)
    generic = sorted(
        step
        for step, label in labels.items()
        if label and label.strip().casefold() in GENERIC
    )
    assert missing == []
    assert generic == []
    assert len(labels) == len(OPTIONS_LABELS[language]) + len(
        SUBENTRY_LABELS[language]
    )


def test_every_form_the_code_can_show_is_labelled() -> None:
    """A new form step cannot appear without its own button label."""
    shown = _steps_shown_by_code()
    for language in ("en", "fi"):
        labelled = {step for step, label in _form_labels(language).items() if label}
        menus = {
            step
            for step, copy in _translations(language)["options"]["step"].items()
            if "menu_options" in copy
        }
        for flow, steps in shown.items():
            forms = {(flow, step) for step in steps if step not in menus}
            assert forms <= labelled, (language, sorted(forms - labelled))

    # First setup shows no form: it creates the entry and moves on.
    assert shown["config"] == set()


@pytest.mark.parametrize("language", ["en", "fi"])
def test_purchase_and_runtime_forms_have_these_labels(language: str) -> None:
    """The whole matrix, read from the translation keys themselves."""
    labels = _form_labels(language)

    assert {
        step: label for step, label in labels.items() if step[0] != "options"
    } == SUBENTRY_LABELS[language]


@pytest.mark.parametrize("language", ["en", "fi"])
def test_asset_management_labels_are_unchanged(language: str) -> None:
    """The subentry work leaves every Asset management button as it was."""
    labels = _form_labels(language)

    assert {
        step: label for (flow, step), label in labels.items() if flow == "options"
    } == OPTIONS_LABELS[language]


def test_both_languages_label_the_same_forms() -> None:
    """Finnish and English cover exactly the same form steps."""
    assert set(_form_labels("en")) == set(_form_labels("fi"))
    for language in ("en", "fi"):
        subentries = _translations(language)["config_subentries"]
        for kind in ("purchase", "runtime"):
            assert set(subentries[kind]["step"]) == {
                step for flow, step in SUBENTRY_LABELS[language] if flow == kind
            }


@pytest.mark.parametrize("language", ["en", "fi"])
def test_a_saved_edit_is_confirmed_in_words(language: str) -> None:
    """Saving ends on Home Assistant's confirmation, never on a raw key."""
    subentries = _translations(language)["config_subentries"]
    expected = {
        "en": {"purchase": "Purchase updated.", "runtime": "Runtime tracking updated."},
        "fi": {
            "purchase": "Ostos päivitettiin.",
            "runtime": "Käyttötuntiseuranta päivitettiin.",
        },
    }[language]

    for kind, text in expected.items():
        assert subentries[kind]["abort"] == {"reconfigure_successful": text}


# --- The label matches what the step does ------------------------------------


async def _runtime_installation(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
):
    """Return an entry with one tracked device, and an untracked one."""
    entry, runtime_subentry_id = await _setup_runtime_entry(
        hass,
        hass_storage,
        asset_store_data,
        runtime_subentry_data,
        device_registry,
        source_state=(
            SOURCE_ENTITY_ID,
            {"device_class": SensorDeviceClass.POWER, "unit_of_measurement": "W"},
        ),
    )
    owner = MockConfigEntry(domain="hue", title="Second owner", data={})
    owner.add_to_hass(hass)
    untracked = device_registry.async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("hue", "label-compressor")},
        name="Compressor",
    )
    hass.states.async_set("input_boolean.compressor_running", "off")
    return entry, runtime_subentry_id, untracked


def _kind_of(result: dict[str, Any]) -> str:
    """Classify what submitting a step did."""
    if result["type"] is FlowResultType.FORM:
        return "route"
    if result["type"] is FlowResultType.CREATE_ENTRY:
        return "create"
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    return "edit"


async def test_each_button_says_what_its_step_does(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    """Every step is submitted for real; its outcome decides its label.

    Only a step that leads to another form continues. A step that creates
    repeats the Add action the person started from, and a step that saves
    an existing one says Save changes. The outcomes are the destinations
    these forms have always had.
    """
    entry, runtime_subentry_id, untracked = await _runtime_installation(
        hass, hass_storage, asset_store_data, runtime_subentry_data, device_registry
    )
    subentries = hass.config_entries.subentries
    outcomes: dict[tuple[str, str], str] = {}

    with _verified_store_readback(hass_storage):
        form = await subentries.async_init(
            (entry.entry_id, SUBENTRY_TYPE_PURCHASE),
            context={"source": SOURCE_USER},
        )
        purchase = {
            CONF_DEVICE_IDS: [],
            CONF_PURCHASE_NAME: "Label purchase",
            CONF_PURCHASE_DATE: "2026-08-01",
            CONF_WARRANTY_TYPE: WARRANTY_NONE,
        }
        created = await subentries.async_configure(form["flow_id"], purchase)
        outcomes[("purchase", form["step_id"])] = _kind_of(created)
        await hass.async_block_till_done()

        purchase_subentry = next(
            item
            for item in entry.subentries.values()
            if item.subentry_type == SUBENTRY_TYPE_PURCHASE
        )
        form = await entry.start_subentry_reconfigure_flow(
            hass, purchase_subentry.subentry_id
        )
        saved = await subentries.async_configure(form["flow_id"], purchase)
        outcomes[("purchase", form["step_id"])] = _kind_of(saved)
        await hass.async_block_till_done()

        form = await entry.start_subentry_reconfigure_flow(hass, runtime_subentry_id)
        source = await subentries.async_configure(
            form["flow_id"], {CONF_RUNTIME_MODE: RUNTIME_MODE_POWER}
        )
        outcomes[("runtime", form["step_id"])] = _kind_of(source)
        assert source["step_id"] == "reconfigure_source"
        saved = await subentries.async_configure(
            source["flow_id"],
            _power_source_input(runtime_subentry_data, SOURCE_ENTITY_ID),
        )
        outcomes[("runtime", source["step_id"])] = _kind_of(saved)
        await hass.async_block_till_done()

        form = await subentries.async_init(
            (entry.entry_id, SUBENTRY_TYPE_RUNTIME),
            context={"source": SOURCE_USER},
        )
        source = await subentries.async_configure(
            form["flow_id"],
            {CONF_DEVICE_ID: untracked.id, CONF_RUNTIME_MODE: RUNTIME_MODE_ON},
        )
        outcomes[("runtime", form["step_id"])] = _kind_of(source)
        assert source["step_id"] == "runtime_source"
        created = await subentries.async_configure(
            source["flow_id"],
            {CONF_SOURCE_ENTITY_ID: "input_boolean.compressor_running"},
        )
        outcomes[("runtime", source["step_id"])] = _kind_of(created)
        await hass.async_block_till_done()

    # The destinations these forms have always had.
    assert outcomes == {
        ("purchase", "user"): "create",
        ("purchase", "reconfigure"): "edit",
        ("runtime", "reconfigure"): "route",
        ("runtime", "reconfigure_source"): "edit",
        ("runtime", "user"): "route",
        ("runtime", "runtime_source"): "create",
    }
    for language in ("en", "fi"):
        data = _translations(language)["config_subentries"]
        for (kind, step), outcome in outcomes.items():
            label = data[kind]["step"][step]["submit"]
            expected = {
                "route": ROUTE[language],
                "create": data[kind]["initiate_flow"]["user"],
                "edit": SAVE[language],
            }[outcome]
            assert label == expected, (language, kind, step, outcome)


async def test_opening_purchase_and_runtime_forms_changes_nothing(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    """Showing a form, or moving to its next form, writes and reloads nothing."""
    entry, runtime_subentry_id, untracked = await _runtime_installation(
        hass, hass_storage, asset_store_data, runtime_subentry_data, device_registry
    )
    subentries = hass.config_entries.subentries
    before = {key: deepcopy(dict(item.data)) for key, item in entry.subentries.items()}
    stored = deepcopy(hass_storage)

    with _recorded_saves() as saves, capture_reloads(hass) as reload:
        opened = [
            await subentries.async_init(
                (entry.entry_id, SUBENTRY_TYPE_PURCHASE),
                context={"source": SOURCE_USER},
            ),
            await subentries.async_init(
                (entry.entry_id, SUBENTRY_TYPE_RUNTIME),
                context={"source": SOURCE_USER},
            ),
            await entry.start_subentry_reconfigure_flow(hass, runtime_subentry_id),
        ]
        routed = [
            await subentries.async_configure(
                opened[1]["flow_id"],
                {CONF_DEVICE_ID: untracked.id, CONF_RUNTIME_MODE: RUNTIME_MODE_ON},
            ),
            await subentries.async_configure(
                opened[2]["flow_id"], {CONF_RUNTIME_MODE: RUNTIME_MODE_POWER}
            ),
        ]
        await hass.async_block_till_done()

    assert [form["type"] for form in opened + routed] == [FlowResultType.FORM] * 5
    assert [form["step_id"] for form in routed] == [
        "runtime_source",
        "reconfigure_source",
    ]
    assert saves == []
    reload.assert_not_called()
    assert {key: dict(item.data) for key, item in entry.subentries.items()} == before
    assert hass_storage == stored
    for form in opened:
        subentries.async_abort(form["flow_id"])
    assert subentries.async_progress() == []
