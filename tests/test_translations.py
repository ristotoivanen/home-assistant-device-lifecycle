"""Translation structure and Asset-management UI terminology tests."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
import textwrap
from typing import Any

import pytest

from custom_components.device_lifecycle.config_flow import (
    DeviceLifecycleOptionsFlow,
)
from custom_components.device_lifecycle.const import (
    DEPLOYMENT_STATES,
    HA_RELATIONSHIP_ACTIONS,
)

TRANSLATION_DIRECTORY = (
    Path(__file__).parents[1]
    / "custom_components"
    / "device_lifecycle"
    / "translations"
)
STRINGS_FILE = TRANSLATION_DIRECTORY.parent / "strings.json"
LANGUAGES = ("en", "fi")


def _translation(language: str) -> dict[str, Any]:
    """Load one translation file as strict JSON."""
    with (TRANSLATION_DIRECTORY / f"{language}.json").open(
        encoding="utf-8"
    ) as translation_file:
        return json.load(translation_file)


def _key_shape(value: Any) -> Any:
    """Return a recursive mapping shape while ignoring translated values."""
    if isinstance(value, dict):
        return {key: _key_shape(child) for key, child in value.items()}
    return None


def _string_values(value: Any) -> list[str]:
    """Flatten only user-visible string values from a translation subtree."""
    if isinstance(value, dict):
        return [text for child in value.values() for text in _string_values(child)]
    return [value] if isinstance(value, str) else []


def _options_flow_tree() -> ast.ClassDef:
    """Return the parsed OptionsFlow class for emitted-key inspection."""
    source = textwrap.dedent(inspect.getsource(DeviceLifecycleOptionsFlow))
    tree = ast.parse(source)
    class_node = tree.body[0]
    assert isinstance(class_node, ast.ClassDef)
    return class_node


def _emitted_options_errors() -> set[str]:
    """Collect literal OptionsFlow error keys, including helper return values."""
    error_keys: set[str] = set()
    for node in ast.walk(_options_flow_tree()):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "base"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    error_keys.add(value.value)
        elif isinstance(node, ast.Assign):
            if not isinstance(node.value, ast.Constant) or not isinstance(
                node.value.value, str
            ):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "errors"
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "base"
                ):
                    error_keys.add(node.value.value)
        elif isinstance(node, ast.Return):
            if isinstance(node.value, ast.Constant) and isinstance(
                node.value.value, str
            ):
                error_keys.add(node.value.value)
            elif (
                isinstance(node.value, ast.Tuple)
                and len(node.value.elts) == 2
                and isinstance(node.value.elts[1], ast.Constant)
                and isinstance(node.value.elts[1].value, str)
            ):
                error_keys.add(node.value.elts[1].value)
    return error_keys


def _completion_keys() -> set[str]:
    """Collect result-message keys passed to the common completion helper."""
    keys: set[str] = set()
    for node in ast.walk(_options_flow_tree()):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "_finish_asset_action" or len(node.args) < 2:
            continue
        description = node.args[1]
        if isinstance(description, ast.Constant) and isinstance(description.value, str):
            keys.add(description.value)
    return keys


def test_options_translation_structures_match_and_are_valid_json() -> None:
    """English and Finnish expose the same complete OptionsFlow structure."""
    english = _translation("en")
    finnish = _translation("fi")

    assert _key_shape(english["options"]) == _key_shape(finnish["options"])
    assert _key_shape(english["config_subentries"]["purchase"]) == _key_shape(
        finnish["config_subentries"]["purchase"]
    )
    assert _key_shape(english["entity"]) == _key_shape(finnish["entity"])


def test_strings_source_matches_english_translation() -> None:
    """Legacy strings remain unchanged while runtime translations add 0.6.1."""
    with STRINGS_FILE.open(encoding="utf-8") as strings_file:
        strings = json.load(strings_file)

    english = _translation("en")
    installed_date = english["entity"]["sensor"].pop("installed_date")
    assert installed_date == {"name": "Installation date"}
    assert "installed_date" not in strings["entity"]["sensor"]
    assert strings == english


def test_exposure_entity_names_and_enum_states_are_translated() -> None:
    """Both supported languages expose the new translated entity contract."""
    english = _translation("en")["entity"]["sensor"]
    finnish = _translation("fi")["entity"]["sensor"]

    assert english["asset_id"]["name"] == "Asset ID"
    assert finnish["asset_id"]["name"] == "Elinkaaritunnus"
    assert english["installed_date"]["name"] == "Installation date"
    assert finnish["installed_date"]["name"] == "Käyttöönottopäivä"
    assert set(english["deployment"]["state"]) == {
        "unknown",
        "not_deployed",
        "deployed",
    }
    assert set(english["relationships"]["state"]) == {
        "none",
        "present",
        "missing",
    }
    assert _key_shape(english) == _key_shape(finnish)


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_emitted_options_error_has_translation(language: str) -> None:
    """Every literal error emitted by OptionsFlow has localized UI text."""
    translated_errors = set(_translation(language)["options"]["error"])

    assert _emitted_options_errors() <= translated_errors


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_menu_action_and_step_has_translation(language: str) -> None:
    """Both menus and every dispatched action have localized labels and steps."""
    steps = _translation(language)["options"]["step"]

    assert set(steps["init"]["menu_options"]) == {
        "create_manual_asset",
        "manage_asset",
    }
    assert set(steps["manage_asset_menu"]["menu_options"]) == {
        "asset_deployment",
        "change_asset_purchase",
        "edit_asset_metadata",
        "ha_relationship",
    }
    assert set(steps["ha_relationship"]["menu_options"]) == {
        "add_related_device",
        "manage_primary_device",
        "remove_related_device",
    }
    menu_actions = {
        *steps["init"]["menu_options"],
        *steps["manage_asset_menu"]["menu_options"],
        *steps["ha_relationship"]["menu_options"],
    }
    assert menu_actions <= set(steps)


@pytest.mark.parametrize("language", LANGUAGES)
def test_deployment_relationship_confirmation_and_results_exist(
    language: str,
) -> None:
    """All selectors, relationship copy, and result messages exist."""
    translation = _translation(language)

    assert set(translation["selector"]["deployment_state"]["options"]) == set(
        DEPLOYMENT_STATES
    )
    assert set(translation["selector"]["ha_relationship_action"]["options"]) == set(
        HA_RELATIONSHIP_ACTIONS
    )
    assert translation["options"]["step"]["confirm_not_deployed"]["title"]
    assert translation["options"]["step"]["confirm_not_deployed"]["description"]
    assert _completion_keys() <= set(translation["options"]["create_entry"])


@pytest.mark.parametrize("language", LANGUAGES)
def test_options_ui_excludes_internal_and_out_of_scope_terminology(
    language: str,
) -> None:
    """Ordinary UI does not expose internals, archive, or owned-device concepts."""
    options_text = " ".join(
        _string_values(_translation(language)["options"])
    ).casefold()
    forbidden = {
        "archive",
        "restore",
        "asset store",
        "asset uuid",
        "config subentry",
        "ha_device_refs",
        "owned device",
        "provenance",
        "purchase_uuid",
        "storage schema",
        "uuid",
        "arkistoi",
        "omistama laite",
        "palauta arkistosta",
        "tallennusskeema",
    }

    assert not {term for term in forbidden if term in options_text}


def test_purchase_creation_copy_explicitly_allows_zero_devices() -> None:
    """Purchase-first UI cannot imply that a Home Assistant device is required."""
    english = _translation("en")["config_subentries"]["purchase"]["step"]["user"]
    finnish = _translation("fi")["config_subentries"]["purchase"]["step"]["user"]
    english_text = " ".join(_string_values(english)).casefold()
    finnish_text = " ".join(_string_values(finnish)).casefold()

    assert "leave empty" in english_text
    assert "no devices" in english_text
    assert "select at least one" not in english_text
    assert "jätä tyhjäksi" in finnish_text
    assert "vaikka laitteita ei" in finnish_text
    assert "valitse vähintään yksi" not in finnish_text


def test_purchase_reconfigure_copy_separates_assets_from_ha_devices() -> None:
    """Both languages explain the read-only Asset and HA registry scopes."""
    english = _translation("en")["config_subentries"]["purchase"]["step"]["reconfigure"]
    finnish = _translation("fi")["config_subentries"]["purchase"]["step"]["reconfigure"]

    assert "{linked_assets}" in english["description"]
    assert "read-only" in english["description"]
    assert "Device Registry devices only" in english["data_description"]["device_ids"]
    assert "{linked_assets}" in finnish["description"]
    assert "vain tiedoksi" in finnish["description"]
    assert (
        "vain Home Assistantin laiterekisterin"
        in finnish["data_description"]["device_ids"]
    )


def test_finalized_english_and_finnish_lifecycle_terms() -> None:
    """Key fields use the approved user-facing terminology consistently."""
    english = _translation("en")
    finnish = _translation("fi")

    assert english["options"]["step"]["asset_deployment"]["data"] == {
        "clear_ha_area": "Clear current Area",
        "clear_installed_date": "Clear installation date",
        "deployment_state": "Deployment status",
        "ha_area_id": "Home Assistant Area",
        "installed_date": "Installation date",
    }
    assert finnish["options"]["step"]["asset_deployment"]["data"] == {
        "clear_ha_area": "Poista nykyinen alue",
        "clear_installed_date": "Poista käyttöönottopäivä",
        "deployment_state": "Käyttötila",
        "ha_area_id": "Alue",
        "installed_date": "Käyttöönottopäivä",
    }
    assert finnish["selector"]["ha_relationship_action"]["options"] == {
        "replace": "Vaihda linkitetty laite",
        "unlink": "Poista linkitys",
    }
