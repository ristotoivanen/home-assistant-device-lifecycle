"""Every Asset-management screen opens the same way.

The Asset's name and ID stand on their own line, then the current state or
purpose in a line or two, then only the guidance that prevents a likely
mistake: Lifecycle is not temporary removal, the warranty belongs to the
Asset, a replacement is recorded from the new Asset. No screen opens with
one dense paragraph, and no screen names anything by a technical ID.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar

from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_DEPLOYMENT_STATE,
    CONF_LIFECYCLE_STATUS,
    CONF_REPLACEMENT_ACTION,
    CONF_REPLACEMENT_UUID,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    REPLACEMENT_ACTION_CORRECT,
    REPLACEMENT_ACTION_VOID,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import ASSET_UUID, DEVICE_ID, PURCHASE_UUID
from .test_options_flow import _manager, _options_flow

# The last line of every edit or action form: how to leave without saving.
EXIT = {
    "en": "Close the window without saving by using the X button in the upper-left corner.",
    "fi": "Sulje ikkuna tallentamatta painamalla vasemman yläkulman X-painiketta.",
}
TRANSLATIONS = (
    Path(__file__).parents[1]
    / "custom_components"
    / "device_lifecycle"
    / "translations"
)
# Every Asset-management screen that describes itself.
SCREENS = (
    "manage_asset_menu",
    "asset_details_warranty_menu",
    "edit_asset_metadata",
    "change_asset_purchase",
    "asset_installation_menu",
    "asset_deployment",
    "confirm_not_deployed",
    "asset_lifecycle_replacement_menu",
    "asset_lifecycle",
    "confirm_disposed",
    "asset_replacement",
    "replacement_replaces",
    "manage_asset_replacement",
    "correct_asset_replacement",
    "confirm_void_replacement",
    "ha_relationship",
    "manage_primary_device",
    "add_related_device",
    "remove_related_device",
)
# The hub names the selection; every other screen starts with the Asset.
LEADS_WITH_THE_ASSET = tuple(step for step in SCREENS if step != "manage_asset_menu")
# Openings of the dense paragraphs these screens replaced.
DENSE_OPENINGS = {
    "en": ("Asset: {asset}.", "Edit the details for {asset}", "Correct {relationship}"),
    "fi": ("Laite: {asset}.", "Muokkaa laitteen {asset}", "Korjaa laitteen {asset}"),
}
# Phrases the rewritten copy no longer repeats.
RETIRED_PROSE = {
    "en": (
        "The current status is preselected",
        "Lifecycle and installation status are independent",
        "Installation status is independent of any linked Home Assistant device",
        "A previous unavailable Purchase is shown only",
        "Select a configured Purchase or No Purchase",
        "managed separately",
        "one atomic save",
    ),
    "fi": (
        "Nykyinen tila on esivalittu",
        "Elinkaari ja asennustila ovat toisistaan riippumattomia",
        "Asennustila ei riipu linkitetystä Home Assistant -laitteesta",
        "Aiempi ostos, joka ei ole enää käytettävissä",
        "Valitse käytettävissä oleva ostos tai Ei ostosta",
        "hallitaan erikseen",
        "atomisella tallennuksella",
    ),
}


def _steps(language: str) -> dict[str, Any]:
    """Return one language's OptionsFlow steps."""
    return json.loads(
        (TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8")
    )["options"]["step"]


def _rendered(language: str, result: dict[str, Any]) -> list[str]:
    """Render a screen's description as the lines a person reads."""
    text = _steps(language)[result["step_id"]]["description"].format_map(
        result["description_placeholders"]
    )
    return [line for line in text.split("\n") if line.strip()]


async def _on(hass: HomeAssistant, manager: AssetStoreManager, asset_uuid: str):
    """Return a flow on one Asset."""
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset_uuid})
    return flow


@pytest.mark.parametrize("language", ["en", "fi"])
@pytest.mark.parametrize("step_id", LEADS_WITH_THE_ASSET)
def test_the_asset_stands_on_its_own_line(language: str, step_id: str) -> None:
    """Name and ID first, separated from whatever follows."""
    description = _steps(language)[step_id]["description"]

    assert description.startswith("{asset}\n\n"), step_id
    for opening in DENSE_OPENINGS[language]:
        assert opening not in description, (step_id, opening)


@pytest.mark.parametrize("language", ["en", "fi"])
def test_retired_prose_is_gone(language: str) -> None:
    """Documentation that did not prevent a mistake is no longer repeated."""
    copy = "\n".join(_steps(language)[step]["description"] for step in SCREENS)

    for phrase in RETIRED_PROSE[language]:
        assert phrase not in copy, phrase


@pytest.mark.parametrize(
    ("language", "status", "retired", "temporary"),
    [
        (
            "en",
            "Current status: Active",
            "Retired means permanent or deliberate removal from use.",
            ("Temporary disconnection or storage belongs under Installation & "
            "location → Not installed."),
        ),
        (
            "fi",
            "Nykyinen tila: Aktiivinen",
            ("Käytöstä poistettu tarkoittaa pysyvää tai tarkoituksellista "
            "käytöstä poistamista."),
            ("Tilapäinen irrotus tai varastointi kuuluu kohtaan Asennus ja "
            "sijainti → Ei asennettu."),
        ),
    ],
)
async def test_the_lifecycle_form_says_what_retired_is_not(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    language: str,
    status: str,
    retired: str,
    temporary: str,
) -> None:
    """Current status, then retired versus temporary removal, each alone."""
    hass.config.language = language
    manager = _manager(hass, asset_store_data)
    await manager.async_set_asset_lifecycle(
        ASSET_UUID, "active", effective_date="2026-01-02", notes=None
    )
    flow = await _on(hass, manager, ASSET_UUID)

    form = await flow.async_step_asset_lifecycle()

    assert _rendered(language, form) == [
        "Workshop device · DL0007",
        status,
        retired,
        temporary,
        EXIT[language],
    ]


@pytest.mark.parametrize(
    ("language", "facts", "guidance"),
    [
        (
            "en",
            [
                "Installation: Installed",
                "Installation date: 20 Jan 2026",
                "Location: Workshop",
            ],
            ("Not installed means the Asset is disconnected or in storage but "
            "still part of your inventory. Permanent removal, disposal or loss "
            "is recorded under Lifecycle."),
        ),
        (
            "fi",
            [
                "Asennustila: Asennettu",
                "Asennuspäivä: 20.1.2026",
                "Sijainti: Workshop",
            ],
            ("Ei asennettu tarkoittaa, että laite on irrotettu tai varastossa mutta "
            "kuuluu edelleen laitekantaasi. Pysyvä käytöstä poisto, hävittäminen "
            "tai katoaminen kirjataan kohtaan Elinkaari."),
        ),
    ],
)
async def test_the_installation_form_shows_the_current_state_first(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    asset_store_data: AssetStoreData,
    language: str,
    facts: list[str],
    guidance: str,
) -> None:
    """The section's own fact lines, then when Lifecycle is the right place."""
    hass.config.language = language
    manager = _manager(hass, asset_store_data)
    workshop = area_registry.async_create("Workshop")
    await manager.async_set_asset_deployment(
        ASSET_UUID, deployment_state=DEPLOYMENT_STATE_DEPLOYED, ha_area_id=workshop.id
    )
    flow = await _on(hass, manager, ASSET_UUID)

    form = await flow.async_step_asset_deployment()
    lines = _rendered(language, form)

    assert lines == ["Workshop device · DL0007", *facts, guidance, EXIT[language]]
    assert workshop.id not in "\n".join(lines)


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        (
            "en",
            [
                "Workshop device · DL0007",
                "Current Purchase: Workshop equipment",
                ("This form changes the Purchase link only. The warranty is the "
                "Asset's own detail: changing the Purchase link does not change "
                "the warranty."),
            ],
        ),
        (
            "fi",
            [
                "Workshop device · DL0007",
                "Nykyinen ostos: Workshop equipment",
                ("Tällä lomakkeella muutetaan vain ostoslinkitystä. Takuu on "
                "laitteen oma tieto: ostoslinkityksen muuttaminen ei muuta takuuta."),
            ],
        ),
    ],
)
async def test_the_purchase_form_keeps_the_warranty_apart(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    language: str,
    expected: list[str],
) -> None:
    """Current Purchase on its own line, then the one rule that matters."""
    hass.config.language = language
    manager = _manager(hass, asset_store_data)
    flow = await _on(hass, manager, ASSET_UUID)

    lines = _rendered(language, await flow.async_step_change_asset_purchase())

    assert lines == [*expected, EXIT[language]]
    assert PURCHASE_UUID not in "\n".join(lines)


@pytest.mark.parametrize(
    ("language", "details", "replaces"),
    [
        (
            "en",
            "Edit the Asset's details.",
            "Select the old Asset that this Asset replaces.",
        ),
        (
            "fi",
            "Muokkaa laitteen perustietoja.",
            "Valitse vanha laite, jonka tämä laite korvaa.",
        ),
    ],
)
async def test_short_forms_say_only_what_they_are_for(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    language: str,
    details: str,
    replaces: str,
) -> None:
    """The details and replacement forms need one sentence each."""
    hass.config.language = language
    manager = _manager(hass, asset_store_data)
    flow = await _on(hass, manager, ASSET_UUID)

    assert _rendered(language, await flow.async_step_edit_asset_metadata()) == [
        "Workshop device · DL0007",
        details,
        EXIT[language],
    ]
    assert _rendered(language, await flow.async_step_replacement_replaces()) == [
        "Workshop device · DL0007",
        replaces,
        EXIT[language],
    ]


async def test_replacement_management_names_the_relationship_on_its_own_line(
    hass: HomeAssistant,
) -> None:
    """The chosen relationship reads as a labelled line, then one sentence."""
    manager = _manager(hass)
    old = await manager.async_create_manual_asset(name="Old heater")
    new = await manager.async_create_manual_asset(name="New heater")
    record = await manager.async_create_asset_replacement(
        old["asset_uuid"], new["asset_uuid"],
        reason="failure", effective_date=None, notes=None,
    )
    flow = await _on(hass, manager, new["asset_uuid"])
    choose = {CONF_REPLACEMENT_UUID: record["replacement_uuid"]}

    correct = await flow.async_step_manage_asset_replacement(
        {**choose, CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_CORRECT}
    )
    void = await flow.async_step_manage_asset_replacement(
        {**choose, CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_VOID}
    )

    relationship = "Old heater · DL0001 → New heater · DL0002"
    assert _rendered("en", correct) == [
        "New heater · DL0002",
        f"Relationship to correct: {relationship}",
        ("The old relationship is voided and the corrected one saved in the same "
        "step."),
        EXIT["en"],
    ]
    assert _rendered("en", void) == [
        "New heater · DL0002",
        f"Relationship to void: {relationship}",
        "The relationship stops being current; its history is kept.",
        EXIT["en"],
    ]


async def test_confirmations_ask_one_question_after_the_asset(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """Disposal and clearing a location each ask plainly, then explain."""
    manager = _manager(hass, asset_store_data)
    office = area_registry.async_create("Office")
    await manager.async_set_asset_deployment(
        ASSET_UUID, deployment_state=DEPLOYMENT_STATE_DEPLOYED, ha_area_id=office.id
    )
    flow = await _on(hass, manager, ASSET_UUID)

    disposed = await flow.async_step_asset_lifecycle(
        {CONF_LIFECYCLE_STATUS: "disposed"}
    )
    not_deployed = await flow.async_step_asset_deployment(
        {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED}
    )

    assert _rendered("en", disposed)[:2] == [
        "Workshop device · DL0007",
        "Record this Asset as disposed?",
    ]
    assert _rendered("en", not_deployed)[:2] == [
        "Workshop device · DL0007",
        "Location to clear: Office",
    ]
    assert office.id not in "\n".join(_rendered("en", not_deployed))


@pytest.mark.parametrize(
    ("language", "heading", "primary", "related", "guidance"),
    [
        (
            "en",
            "**Home Assistant devices**",
            "Primary: {name}",
            "Related: No related Home Assistant devices",
            ("The primary Home Assistant device is this Asset's main counterpart "
            "in Home Assistant. Related Home Assistant devices are references "
            "only."),
        ),
        (
            "fi",
            "**Home Assistant -laitteet**",
            "Ensisijainen: {name}",
            "Liittyvät: Ei liittyviä Home Assistant -laitteita",
            ("Ensisijainen Home Assistant -laite on tämän laitteen vastine Home "
            "Assistantissa. Liittyvät Home Assistant -laitteet ovat vain "
            "viitteitä."),
        ),
    ],
)
async def test_home_assistant_devices_read_like_a_section(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    language: str,
    heading: str,
    primary: str,
    related: str,
    guidance: str,
) -> None:
    """Heading, primary and related on their own lines, never a device ID."""
    hass.config.language = language
    manager = _manager(hass, asset_store_data)
    flow = await _on(hass, manager, ASSET_UUID)

    submenu = await flow.async_step_ha_relationship()
    lines = _rendered(language, submenu)
    name = submenu["description_placeholders"]["current_primary"]

    assert lines == [
        "Workshop device · DL0007",
        heading,
        primary.format(name=name),
        related,
        guidance,
    ]
    assert DEVICE_ID not in "\n".join(lines)
