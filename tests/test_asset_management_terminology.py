"""The words Asset management uses, in both languages.

Device Lifecycle stores canonical states like `not_deployed`; people read
"Ei asennettu". These tests own that boundary: what the management flow
calls things, that the old vocabulary does not creep back, and that no
canonical value ever reaches the screen.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant

from custom_components.device_lifecycle.const import (
    CONF_ASSET_NAME,
    CONF_ASSET_UUID,
    CONF_DEPLOYMENT_STATE,
    CONF_LIFECYCLE_STATUS,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DEPLOYMENT_STATES,
    LIFECYCLE_STATUS_ACTIVE,
    LIFECYCLE_STATUSES,
)
from custom_components.device_lifecycle.models import AssetStoreData

from .conftest import ASSET_UUID
from .test_options_flow import (
    _identical_metadata_input,
    _manager,
    _options_flow,
)

TRANSLATIONS = (
    Path(__file__).parents[1]
    / "custom_components"
    / "device_lifecycle"
    / "translations"
)
BASELINE_REVISION = "01dc4cb"
# The published 0.7.4 release (tag v0.7.4).
RELEASED_0_7_4_REVISION = "aed576a"

# Canonical values are storage, never copy. The underscored ones can only
# ever be a leak; the single words are ordinary English too ("an active
# Purchase"), so those are checked against rendered state labels instead.
RAW_STATES = set(DEPLOYMENT_STATES) | set(LIFECYCLE_STATUSES)
MACHINE_STATES = {state for state in RAW_STATES if "_" in state}

# Management-flow steps whose copy Phase 8 owns. Quick Add keeps its own
# structure but shares the state vocabulary.
MANAGEMENT_STEPS = (
    "manage_asset",
    "manage_asset_menu",
    "edit_asset_metadata",
    "change_asset_purchase",
    "asset_deployment",
    "confirm_not_deployed",
    "asset_lifecycle",
    "confirm_disposed",
    "asset_replacement",
    "manage_asset_replacement",
    "correct_asset_replacement",
    "confirm_void_replacement",
    "replacement_replaces",
    "replacement_replaced_by",
    "ha_relationship",
    "manage_primary_device",
    "add_related_device",
    "remove_related_device",
)

# Phrases, not bare words: "deployed" alone appears in ordinary sentences,
# but "deployment status" was a label this phase replaced.
RETIRED_TERMS = {
    "fi": (
        "käyttötila",
        "käyttötilan",
        "käyttöönottopäivä",
        "laitteen korvaus",
        "laitelinkki",
        "laitelinkit",
        "laitelinkkejä",
        "liitä ostokseen",
        "ei käytössä",
        "poistettu käytöstä",
    ),
    "en": (
        "deployment status",
        "deployment state",
        "deployment default",
        "asset replacement",
        "home assistant relationships",
        "change purchase",
        "edit metadata",
        "not deployed",
    ),
}


def _translation(language: str) -> dict:
    """Load one runtime translation file."""
    return json.loads(
        (TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8")
    )


def _strings(node) -> list[str]:
    """Flatten every user-visible string in a translation subtree."""
    if isinstance(node, dict):
        return [text for child in node.values() for text in _strings(child)]
    return [node] if isinstance(node, str) else []


def _management_copy(language: str) -> str:
    """Return every user-visible string the management flow can show."""
    options = _translation(language)["options"]
    parts = _strings(options.get("error", {}))
    parts += _strings(options.get("create_entry", {}))
    steps = options["step"]
    for step_id in MANAGEMENT_STEPS:
        parts += _strings(steps[step_id])
    return "\n".join(parts)


async def _flow(hass: HomeAssistant, manager, language: str = "en"):
    """Return a flow on the fixture Asset, rendering in one language."""
    hass.config.language = language
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: ASSET_UUID})
    return flow


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            "en",
            {
                "title": "Installation & location",
                "deployment_state": "Installation status",
                "installed_date": "Installation date",
                "ha_area_id": "Location",
            },
        ),
        (
            "fi",
            {
                "title": "Asennus ja sijainti",
                "deployment_state": "Asennustila",
                "installed_date": "Asennuspäivä",
                "ha_area_id": "Sijainti",
            },
        ),
    ],
)
def test_installation_form_uses_the_frozen_words(
    language: str,
    expected: dict,
) -> None:
    """The installation editor is named for installation, not deployment."""
    step = _translation(language)["options"]["step"]["asset_deployment"]

    assert step["title"] == expected["title"]
    assert step["data"]["deployment_state"] == expected["deployment_state"]
    assert step["data"]["installed_date"] == expected["installed_date"]
    assert step["data"]["ha_area_id"] == expected["ha_area_id"]


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("en", {"deployed": "Installed", "not_deployed": "Not installed"}),
        ("fi", {"deployed": "Asennettu", "not_deployed": "Ei asennettu"}),
    ],
)
def test_installation_states_are_named_for_people(
    language: str,
    expected: dict,
) -> None:
    """Every canonical installation state has a word, in both languages."""
    options = _translation(language)["selector"]["deployment_state"]["options"]

    assert set(options) == set(DEPLOYMENT_STATES)
    for state, label in expected.items():
        assert options[state] == label


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            "en",
            {
                "active": "Active",
                "retired": "Retired",
                "disposed": "Disposed",
                "lost": "Lost",
                "unknown": "Unknown",
            },
        ),
        (
            "fi",
            {
                "active": "Aktiivinen",
                "retired": "Käytöstä poistettu",
                "disposed": "Hävitetty",
                "lost": "Kadonnut",
                "unknown": "Ei tiedossa",
            },
        ),
    ],
)
def test_lifecycle_states_are_named_for_people(
    language: str,
    expected: dict,
) -> None:
    """Every canonical lifecycle status has a word, in both languages."""
    options = _translation(language)["selector"]["lifecycle_status"]["options"]

    assert set(options) == set(LIFECYCLE_STATUSES)
    assert options == expected


@pytest.mark.parametrize("language", ["en", "fi"])
def test_the_two_domains_point_at_each_other(language: str) -> None:
    """Each form explains when the other one is the right place to go."""
    steps = _translation(language)["options"]["step"]
    installation = steps["asset_deployment"]["description"]
    lifecycle = steps["asset_lifecycle"]["description"]

    if language == "en":
        assert "Not installed" in installation
        assert "Lifecycle" in installation
        assert "Retired" in lifecycle
        assert "Installation & location" in lifecycle
        assert "Not installed" in lifecycle
    else:
        assert "Ei asennettu" in installation
        assert "Elinkaari" in installation
        assert "Käytöstä poistettu" in lifecycle
        assert "Asennus ja sijainti" in lifecycle
        assert "Ei asennettu" in lifecycle


@pytest.mark.parametrize("language", ["en", "fi"])
def test_lifecycle_copy_does_not_claim_a_change_is_irreversible(
    language: str,
) -> None:
    """History is append-only, so a later correction is always possible."""
    steps = _translation(language)["options"]["step"]
    copy = " ".join(
        _strings(steps["asset_lifecycle"]) + _strings(steps["confirm_disposed"])
    ).casefold()

    for claim in (
        "cannot be undone",
        "can never",
        "permanently deletes",
        "ei voi koskaan",
        "ei voi perua",
        "peruuttamat",
    ):
        assert claim not in copy


@pytest.mark.parametrize("language", ["en", "fi"])
def test_purchase_copy_keeps_warranty_a_separate_fact(language: str) -> None:
    """Nothing in the Purchase editor says the warranty came from it."""
    description = _translation(language)["options"]["step"][
        "change_asset_purchase"
    ]["description"]
    folded = description.casefold()

    if language == "en":
        assert "the warranty is the asset's own" in folded
        assert "does not change the warranty" in folded
        for implication in (
            "warranty from this purchase",
            "warranty belongs to this purchase",
            "warranty is derived",
        ):
            assert implication not in folded
    else:
        assert "takuu on laitteen oma tieto" in folded
        assert "ei muuta takuuta" in folded


@pytest.mark.parametrize("language", ["en", "fi"])
def test_home_assistant_devices_are_always_named_in_full(
    language: str,
) -> None:
    """The Asset and the Home Assistant device never share one word."""
    steps = _translation(language)["options"]["step"]
    submenu = steps["ha_relationship"]

    if language == "en":
        assert submenu["title"] == "Home Assistant devices"
        assert submenu["menu_options"] == {
            "add_related_device": "Add related Home Assistant device",
            "manage_asset_menu": "← Back to asset management",
            "manage_primary_device": "Manage primary Home Assistant device",
            "remove_related_device": "Remove related Home Assistant device",
        }
        assert "primary Home Assistant device is this Asset's main" in (
            submenu["description"]
        )
    else:
        assert submenu["title"] == "Home Assistant -laitteet"
        assert submenu["menu_options"] == {
            "add_related_device": "Lisää liittyvä Home Assistant -laite",
            "manage_asset_menu": "← Takaisin laitteen hallintaan",
            "manage_primary_device": (
                "Hallitse ensisijaista Home Assistant -laitetta"
            ),
            "remove_related_device": "Poista liittyvä Home Assistant -laite",
        }
        assert "Ensisijainen Home Assistant -laite on tämän laitteen" in (
            submenu["description"]
        )


@pytest.mark.parametrize("language", ["en", "fi"])
def test_the_hub_rows_carry_the_frozen_names(language: str) -> None:
    """The seven hub rows are the frozen vocabulary, in order."""
    rows = _translation(language)["options"]["step"]["manage_asset_menu"][
        "menu_options"
    ]

    if language == "en":
        assert rows == {
            "edit_asset_metadata": "Asset details",
            "change_asset_purchase": "Purchase & warranty",
            "asset_deployment": "Installation & location",
            "asset_lifecycle": "Lifecycle",
            "asset_replacement": "Replacement",
            "ha_relationship": "Home Assistant devices",
            "manage_asset": "Choose another device",
        }
    else:
        assert rows == {
            "edit_asset_metadata": "Perustiedot",
            "change_asset_purchase": "Osto ja takuu",
            "asset_deployment": "Asennus ja sijainti",
            "asset_lifecycle": "Elinkaari",
            "asset_replacement": "Korvaaminen",
            "ha_relationship": "Home Assistant -laitteet",
            "manage_asset": "Valitse toinen laite",
        }


@pytest.mark.parametrize("language", ["en", "fi"])
def test_no_machine_state_reaches_the_screen(language: str) -> None:
    """A value like `not_deployed` can only ever be a leak."""
    copy = _management_copy(language)

    for raw in MACHINE_STATES:
        assert raw not in copy, raw


@pytest.mark.parametrize("language", ["en", "fi"])
async def test_rendered_state_labels_are_never_the_canonical_value(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    language: str,
) -> None:
    """Every state the flow renders comes back as a word, not as its value.

    Checked against what a step actually produces, since that is the only
    place a canonical value could reach somebody.
    """
    manager = _manager(hass, asset_store_data)
    flow = await _flow(hass, manager, language)

    for selector_key, canonical_values in (
        ("deployment_state", DEPLOYMENT_STATES),
        ("lifecycle_status", LIFECYCLE_STATUSES),
    ):
        labels = [
            await flow._quick_selector_label(selector_key, value)
            for value in canonical_values
        ]

        # Distinct within one selector; `unknown` legitimately reads the
        # same in both, because it means the same thing in both.
        assert len(set(labels)) == len(labels), selector_key
        for label, canonical in zip(labels, canonical_values, strict=True):
            assert label != canonical
            assert "_" not in label


@pytest.mark.parametrize("language", ["en", "fi"])
async def test_no_machine_state_reaches_a_rendered_placeholder(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    language: str,
) -> None:
    """The hub and its submenus render words, whatever the Asset's state is."""
    manager = _manager(hass, asset_store_data)
    flow = await _flow(hass, manager, language)
    await flow.async_step_asset_deployment(
        {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED}
    )

    rendered = []
    for step in ("manage_asset_menu", "asset_replacement", "ha_relationship"):
        result = await getattr(flow, f"async_step_{step}")()
        rendered.extend((result.get("description_placeholders") or {}).values())

    joined = " ".join(rendered)
    for raw in MACHINE_STATES:
        assert raw not in joined, raw


@pytest.mark.parametrize("language", ["en", "fi"])
def test_the_retired_vocabulary_does_not_come_back(language: str) -> None:
    """The words Phase 8 replaced stay replaced in management copy."""
    copy = _management_copy(language).casefold()

    for term in RETIRED_TERMS[language]:
        assert term not in copy, term


async def test_every_operation_reports_itself_in_words(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A finished operation reads as a sentence, never as its internal key."""
    manager = _manager(hass, asset_store_data)
    flow = await _flow(hass, manager)
    edit = _identical_metadata_input(manager.asset(ASSET_UUID))
    edit[CONF_ASSET_NAME] = "Reported in words"

    saved = await flow.async_step_edit_asset_metadata(edit)

    assert saved["description_placeholders"]["result"] == (
        "Asset details updated."
    )
    assert "asset_updated" not in saved["description_placeholders"]["result"]


async def test_the_same_lifecycle_status_says_so_in_the_readers_language(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Resubmitting the current status is explained, not silently ignored."""
    manager = _manager(hass, asset_store_data)
    english = await _flow(hass, manager, "en")
    await english.async_step_asset_lifecycle(
        {CONF_LIFECYCLE_STATUS: LIFECYCLE_STATUS_ACTIVE}
    )

    repeated = await english.async_step_asset_lifecycle(
        {CONF_LIFECYCLE_STATUS: LIFECYCLE_STATUS_ACTIVE}
    )

    assert repeated["description_placeholders"]["result"] == (
        "Lifecycle status is already Active. Nothing was changed."
    )

    finnish = await _flow(hass, manager, "fi")
    repeated_fi = await finnish.async_step_asset_lifecycle(
        {CONF_LIFECYCLE_STATUS: LIFECYCLE_STATUS_ACTIVE}
    )

    assert repeated_fi["description_placeholders"]["result"] == (
        "Elinkaaritila on jo Aktiivinen. Mitään ei muutettu."
    )


async def test_the_result_is_reported_once_and_never_follows_the_reader(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """One render spends the message; switching Assets discards it."""
    manager = _manager(hass, asset_store_data)
    other = await manager.async_create_manual_asset(name="Somewhere else")
    flow = await _flow(hass, manager)

    saved = await flow.async_step_asset_deployment(
        {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED}
    )
    assert saved["description_placeholders"]["result"] == (
        "Installation & location updated."
    )

    reopened = await flow.async_step_manage_asset_menu()
    assert reopened["description_placeholders"]["result"] == ""

    await flow.async_step_asset_deployment(
        {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED}
    )
    switched = await flow.async_step_manage_asset(
        {CONF_ASSET_UUID: other["asset_uuid"]}
    )

    assert switched["description_placeholders"]["result"] == ""


@pytest.mark.parametrize("language", ["en", "fi"])
def test_quick_add_shares_the_management_vocabulary(language: str) -> None:
    """Quick Add keeps its own shape but not its own words for a state."""
    steps = _translation(language)["options"]["step"]
    section = steps["quick_add_details"]["sections"]["lifecycle"]
    management = steps["asset_deployment"]["data"]

    assert section["data"]["deployment_state"] == management["deployment_state"]
    assert section["data"]["installed_date"] == management["installed_date"]
    # Structure untouched: the same three Quick Add steps, in the same shape.
    for step_id in ("quick_add", "quick_add_details", "quick_add_confirm"):
        assert step_id in steps


def test_entity_translations_are_untouched_since_the_release_baseline() -> None:
    """Entity names and states belong to Home Assistant history, not to copy."""
    for language in ("en", "fi"):
        path = (
            f"custom_components/device_lifecycle/translations/{language}.json"
        )
        baseline = json.loads(
            subprocess.run(
                ["git", "show", f"{BASELINE_REVISION}:{path}"],
                capture_output=True,
                text=True,
                check=True,
                cwd=Path(__file__).parents[1],
            ).stdout
        )
        current = _translation(language)

        assert current["entity"] == baseline["entity"], language


def test_entity_translations_are_untouched_since_the_0_7_4_release() -> None:
    """0.7.5 changes flow copy only; entity names and states stay as released."""
    for language in ("en", "fi"):
        path = (
            f"custom_components/device_lifecycle/translations/{language}.json"
        )
        released = json.loads(
            subprocess.run(
                ["git", "show", f"{RELEASED_0_7_4_REVISION}:{path}"],
                capture_output=True,
                text=True,
                check=True,
                cwd=Path(__file__).parents[1],
            ).stdout
        )

        assert _translation(language)["entity"] == released["entity"], language
