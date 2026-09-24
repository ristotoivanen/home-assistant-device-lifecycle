"""Hub rows 1-3 open a read-only section menu before any editor.

Details & warranty, Installation & location and Lifecycle & replacement
each show the Asset's current state in a few short lines, offer their
existing operations as rows, and go back to the hub with a real menu row.
A section that groups several facts keeps them apart: Details & warranty
shows the Asset's details, its linked Purchase and its own warranty, and
Lifecycle & replacement shows the Lifecycle state beside the replacement
relationships in both directions. Each keeps its own operations.
Somebody who only wants to look never has to open a form or use its Submit
button as a way back.

These tests own that navigation contract, the copy budget of each section,
and that the editors themselves are exactly the forms they were before.
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

from custom_components.device_lifecycle.config_flow import (
    NOT_SELECTED,
    SUMMARY_NAME_MAX_LENGTH,
)
from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_DEPLOYMENT_STATE,
    CONF_EFFECTIVE_DATE,
    CONF_HA_AREA_ID,
    CONF_LIFECYCLE_STATUS,
    CONF_PURCHASE_UUID,
    CONF_REPLACEMENT_REASON,
    CONF_REPLACEMENT_TARGET_ASSET_UUID,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DEPLOYMENT_STATE_UNKNOWN,
    WARRANTY_MANUAL,
    WARRANTY_NONE,
    WARRANTY_ONE_YEAR,
    WARRANTY_TWO_YEARS,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import ASSET_UUID, DEVICE_ID, PURCHASE_UUID, capture_reloads
from .test_explicit_selection_safety import _flow_manager_on_asset
from .test_options_flow import _identical_metadata_input, _manager, _options_flow
from .test_purchase_reconciliation import (
    SECOND_PURCHASE_UUID,
    _data_with_second_purchase,
)

HUB_STEP = "manage_asset_menu"
BACK = "manage_asset_menu"
DETAILS_WARRANTY = "asset_details_warranty_menu"
LIFECYCLE_REPLACEMENT = "asset_lifecycle_replacement_menu"
REPLACEMENT_SUBMENU = "asset_replacement"
# Section menu -> the operations it opens, in hub row order.
SECTIONS = {
    DETAILS_WARRANTY: ["edit_asset_metadata", "change_asset_purchase"],
    "asset_installation_menu": ["asset_deployment"],
    LIFECYCLE_REPLACEMENT: ["asset_lifecycle", REPLACEMENT_SUBMENU],
}
EDITORS = [(section, editor) for section, editors in SECTIONS.items() for editor in editors]
# Replacement keeps its own submenu; every other operation is a form.
SUBMENUS = {REPLACEMENT_SUBMENU}
SUMMARY_MAX_LENGTH = 60
# Asset details keep technical values nearly whole: the longest label
# ("Software version: ", "Ohjelmistoversio: ") plus a 64-character value.
DETAIL_VALUE_MAX_LENGTH = 64
# Section menu -> its fact groups, in description order, each with the most
# lines it may grow to and the longest line it may show.
GROUPS = {
    DETAILS_WARRANTY: {
        "details": (8, len("Ohjelmistoversio: ") + DETAIL_VALUE_MAX_LENGTH),
        "purchase": (3, SUMMARY_MAX_LENGTH),
        "warranty": (2, SUMMARY_MAX_LENGTH),
    },
    "asset_installation_menu": {"facts": (3, SUMMARY_MAX_LENGTH)},
    LIFECYCLE_REPLACEMENT: {
        "lifecycle": (2, SUMMARY_MAX_LENGTH),
        # One direction per line: the longest lead-in, then "Name · DLxxxx".
        "replacement": (
            2,
            len("This Asset was replaced by: ")
            + SUMMARY_NAME_MAX_LENGTH
            + len(" · DL0007"),
        ),
    },
}
# Detail line label -> the editor field it describes.
DETAIL_FIELDS = {
    "manufacturer": ("Manufacturer", "Valmistaja"),
    "model": ("Model", "Malli"),
    "model_id": ("Model ID", "Mallitunnus"),
    "serial_number": ("Serial number", "Sarjanumero"),
    "sw_version": ("Software version", "Ohjelmistoversio"),
    "hw_version": ("Hardware version", "Laitteistoversio"),
    "category": ("Category", "Luokka"),
    "notes": ("Notes", "Muistiinpanot"),
}
TRANSLATIONS = (
    Path(__file__).parents[1]
    / "custom_components"
    / "device_lifecycle"
    / "translations"
)
LONG = "Very long user supplied text that keeps going well past any summary"
# The fixture warranty runs until 2028-01-15; the tests look at it from here.
TODAY = "2026-09-24 12:00:00+00:00"


def _translations(language: str) -> dict[str, Any]:
    """Return one language's OptionsFlow steps."""
    return json.loads(
        (TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8")
    )["options"]["step"]


async def _hub_flow(
    hass: HomeAssistant,
    manager: AssetStoreManager,
    asset_uuid: str = ASSET_UUID,
):
    """Return a flow sitting on one Asset's hub, and the hub."""
    flow, _entry = _options_flow(hass, manager)
    hub = await flow.async_step_manage_asset({CONF_ASSET_UUID: asset_uuid})
    assert hub["step_id"] == HUB_STEP
    return flow, hub


async def _section(flow, section: str) -> dict[str, Any]:
    """Render one section menu directly."""
    return await getattr(flow, f"async_step_{section}")()


def _lines(result: dict[str, Any], group: str = "facts") -> list[str]:
    """Return one fact group's lines, as the description shows them."""
    return result["description_placeholders"][group].split("\n")


def _shown(result: dict[str, Any]) -> str:
    """Return every placeholder value a section puts in front of people."""
    return "\n".join(result["description_placeholders"].values())


async def _rich_asset(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    asset_store_data: AssetStoreData,
) -> tuple[AssetStoreManager, ar.AreaEntry]:
    """Return the fixture Asset installed in an Area with a dated Lifecycle."""
    manager = _manager(hass, asset_store_data)
    workshop = area_registry.async_create("Workshop")
    await manager.async_set_asset_deployment_reporting(
        ASSET_UUID,
        deployment_state=DEPLOYMENT_STATE_DEPLOYED,
        ha_area_id=workshop.id,
    )
    await manager.async_set_asset_lifecycle_reporting(
        ASSET_UUID,
        "active",
        effective_date="2026-01-02",
        notes="Lifecycle note that must stay hidden",
    )
    return manager, workshop


async def _chain(
    manager: AssetStoreManager,
    *,
    predecessor_name: str = "Smoke test 0.7.1",
    successor_name: str = "Uusi lämmitin",
    replaces: bool = True,
    replaced_by: bool = True,
):
    """Put the fixture Asset in the middle of an active replacement chain."""
    predecessor = await manager.async_create_manual_asset(name=predecessor_name)
    successor = await manager.async_create_manual_asset(name=successor_name)
    records = []
    if replaces:
        records.append(
            await manager.async_create_asset_replacement(
                predecessor["asset_uuid"],
                ASSET_UUID,
                reason="failure",
                effective_date="2026-08-11",
                notes="Replacement note that must stay hidden",
            )
        )
    if replaced_by:
        records.append(
            await manager.async_create_asset_replacement(
                ASSET_UUID,
                successor["asset_uuid"],
                reason="upgrade",
                effective_date="2026-09-12",
                notes="Replacement note that must stay hidden",
            )
        )
    return predecessor, successor, records


@pytest.mark.parametrize(("section", "editors"), SECTIONS.items())
async def test_each_hub_row_opens_its_section_menu(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    section: str,
    editors: list[str],
) -> None:
    """A section is a menu: its editors, then Back, and nothing else."""
    manager = _manager(hass, asset_store_data)
    flow, hub = await _hub_flow(hass, manager)

    result = await _section(flow, section)

    assert list(SECTIONS).index(section) == hub["menu_options"].index(section)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == section
    assert result["menu_options"] == [*editors, BACK]
    assert set(result["description_placeholders"]) == {"asset", *GROUPS[section]}
    assert result["description_placeholders"]["asset"] == "Workshop device · DL0007"


async def test_details_and_purchase_no_longer_have_sections_of_their_own(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """One row replaces Asset details and Purchase & warranty on the hub."""
    manager = _manager(hass, asset_store_data)
    flow, hub = await _hub_flow(hass, manager)

    assert hub["menu_options"][0] == DETAILS_WARRANTY
    for retired in ("asset_details_menu", "asset_purchase_menu"):
        assert retired not in hub["menu_options"]
        assert not hasattr(flow, f"async_step_{retired}")
        for language in ("en", "fi"):
            assert retired not in _translations(language)


async def test_lifecycle_and_replacement_share_one_hub_row(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """One row replaces Lifecycle and Replacement; the submenu stays."""
    manager = _manager(hass, asset_store_data)
    flow, hub = await _hub_flow(hass, manager)

    assert hub["menu_options"][2] == LIFECYCLE_REPLACEMENT
    for retired in ("asset_lifecycle_menu", REPLACEMENT_SUBMENU):
        assert retired not in hub["menu_options"]
    assert not hasattr(flow, "async_step_asset_lifecycle_menu")
    for language in ("en", "fi"):
        assert "asset_lifecycle_menu" not in _translations(language)
    # Replacement operations are only reached through their own submenu.
    for operation in (
        "replacement_replaces",
        "replacement_replaced_by",
        "manage_asset_replacement",
    ):
        assert operation not in hub["menu_options"]
        assert operation not in SECTIONS[LIFECYCLE_REPLACEMENT]


@pytest.mark.parametrize(
    ("language", "headings", "actions", "title"),
    [
        (
            "en",
            ["**Lifecycle**", "**Replacement**"],
            {
                "asset_lifecycle": "Record a lifecycle change",
                REPLACEMENT_SUBMENU: "Manage replacement",
                BACK: "← Back to asset management",
            },
            "Lifecycle & replacement",
        ),
        (
            "fi",
            ["**Elinkaari**", "**Korvaaminen**"],
            {
                "asset_lifecycle": "Kirjaa elinkaaren muutos",
                REPLACEMENT_SUBMENU: "Hallitse korvaamista",
                BACK: "← Takaisin laitteen hallintaan",
            },
            "Elinkaari ja korvaaminen",
        ),
    ],
)
def test_lifecycle_and_replacement_labels_each_group_and_action(
    language: str,
    headings: list[str],
    actions: dict[str, str],
    title: str,
) -> None:
    """Two headed groups in a fixed order, then the two operations and Back."""
    step = _translations(language)[LIFECYCLE_REPLACEMENT]
    description = step["description"]
    positions = [
        description.index(f"{heading}\n{{{group}}}")
        for heading, group in zip(
            headings, GROUPS[LIFECYCLE_REPLACEMENT], strict=True
        )
    ]

    assert description.startswith("{asset}\n\n")
    assert positions == sorted(positions)
    assert step["menu_options"] == actions
    assert step["title"] == title
    assert _translations(language)["manage_asset_menu"]["menu_options"][
        LIFECYCLE_REPLACEMENT
    ] == title


@pytest.mark.parametrize(
    ("language", "headings", "actions"),
    [
        (
            "en",
            ["**Asset details**", "**Purchase**", "**Warranty**"],
            {
                "edit_asset_metadata": "Edit asset details",
                "change_asset_purchase": "Change linked purchase",
                BACK: "← Back to asset management",
            },
        ),
        (
            "fi",
            ["**Perustiedot**", "**Osto**", "**Takuu**"],
            {
                "edit_asset_metadata": "Muokkaa perustietoja",
                "change_asset_purchase": "Vaihda ostoslinkitystä",
                BACK: "← Takaisin laitteen hallintaan",
            },
        ),
    ],
)
def test_details_and_warranty_labels_each_group_and_action(
    language: str,
    headings: list[str],
    actions: dict[str, str],
) -> None:
    """Three headed groups in a fixed order, then two edit rows and Back."""
    step = _translations(language)[DETAILS_WARRANTY]
    description = step["description"]
    positions = [
        description.index(f"{heading}\n{{{group}}}")
        for heading, group in zip(headings, GROUPS[DETAILS_WARRANTY], strict=True)
    ]

    assert description.startswith("{asset}\n\n")
    assert positions == sorted(positions)
    assert step["menu_options"] == actions
    assert step["title"] == (
        "Details & warranty" if language == "en" else "Tiedot ja takuu"
    )


@pytest.mark.parametrize(("section", "editor"), EDITORS)
async def test_every_action_row_opens_the_existing_editor(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    section: str,
    editor: str,
) -> None:
    """End to end: hub row, then the action row, then the same step as before."""
    manager = _manager(hass, asset_store_data)
    flow_id = await _flow_manager_on_asset(hass, manager, ASSET_UUID)

    opened = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": section}
    )
    form = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": editor}
    )

    assert opened["type"] is FlowResultType.MENU
    assert opened["step_id"] == section
    assert form["type"] is (
        FlowResultType.MENU if editor in SUBMENUS else FlowResultType.FORM
    )
    assert form["step_id"] == editor


@pytest.mark.parametrize("section", SECTIONS)
async def test_back_returns_to_the_same_hub_without_touching_anything(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    section: str,
) -> None:
    """End to end: Back is menu navigation, with no write, reload or result."""
    manager = _manager(hass, asset_store_data)
    flow_id = await _flow_manager_on_asset(hass, manager, ASSET_UUID)
    await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": section}
    )
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        hub = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": BACK}
        )

    assert hub["type"] is FlowResultType.MENU
    assert hub["step_id"] == HUB_STEP
    assert hub["description_placeholders"]["asset"] == "Workshop device · DL0007"
    assert hub["description_placeholders"]["result"] == ""
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()
    # No Asset, Lifecycle event, replacement record or Purchase changed.
    assert manager._data == before


@pytest.mark.parametrize("section", SECTIONS)
async def test_back_keeps_the_selected_asset_and_rereads_it(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    section: str,
) -> None:
    """Back keeps the same Asset, and the hub reads its data afresh."""
    manager = _manager(hass, asset_store_data)
    other = await manager.async_create_manual_asset(name="Another unit")
    flow, _hub = await _hub_flow(hass, manager)
    await _section(flow, section)
    await manager.async_update_asset_metadata(ASSET_UUID, name="Renamed meanwhile")

    hub = await flow.async_step_manage_asset_menu()

    assert flow._selected_asset_uuid == ASSET_UUID != other["asset_uuid"]
    assert hub["description_placeholders"]["asset"] == "Renamed meanwhile · DL0007"
    assert hub["description_placeholders"]["result"] == ""


@pytest.mark.parametrize("section", SECTIONS)
async def test_rendering_a_section_writes_nothing(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    section: str,
) -> None:
    """Looking at a section is not a change, however often it is shown."""
    manager = _manager(hass, asset_store_data)
    flow, _hub = await _hub_flow(hass, manager)
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        first = await _section(flow, section)
        second = await _section(flow, section)

    assert first["type"] is second["type"] is FlowResultType.MENU
    assert first["description_placeholders"] == second["description_placeholders"]
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()
    assert manager._data == before
    assert flow._last_result is None


async def test_a_section_rereads_the_asset_on_every_render(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    freezer,
) -> None:
    """Nothing about a section is kept in the flow between renders."""
    freezer.move_to(TODAY)
    manager = _manager(hass, asset_store_data)
    flow, _hub = await _hub_flow(hass, manager)
    first = await _section(flow, DETAILS_WARRANTY)

    await manager.async_update_asset_metadata(ASSET_UUID, serial_number="NEW-SERIAL")
    manager._data["assets"][ASSET_UUID]["purchase_uuid"] = None
    manager._data["assets"][ASSET_UUID]["warranty"] = {
        "type": WARRANTY_MANUAL,
        "until": "2027-01-19",
    }
    second = await _section(flow, DETAILS_WARRANTY)

    assert "Serial number: SERIAL-1" in _lines(first, "details")
    assert "Serial number: NEW-SERIAL" in _lines(second, "details")
    assert _lines(first, "purchase")[0] == "Purchase: Workshop equipment"
    assert _lines(second, "purchase") == ["Purchase: No Purchase"]
    assert _lines(first, "warranty") == ["Valid until 15 Jan 2028", "Type: 2 years"]
    assert _lines(second, "warranty") == ["Valid until 19 Jan 2027", "Type: Manual"]


@pytest.mark.parametrize("section", SECTIONS)
async def test_a_vanished_asset_falls_back_to_the_picker(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    section: str,
) -> None:
    """A deleted Asset returns to the selector the way every step does."""
    manager = _manager(hass, asset_store_data)
    flow, _hub = await _hub_flow(hass, manager)
    manager._data["assets"].pop(ASSET_UUID)

    result = await _section(flow, section)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manage_asset"
    assert result["errors"] == {"base": "asset_missing"}


async def test_details_purchase_and_warranty_are_three_separate_groups(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    freezer,
) -> None:
    """Every detail, then the named Purchase, then the Asset's own warranty."""
    freezer.move_to(TODAY)
    manager = _manager(hass, asset_store_data)
    flow, _hub = await _hub_flow(hass, manager)

    result = await _section(flow, DETAILS_WARRANTY)

    assert _lines(result, "details") == [
        "Manufacturer: Example manufacturer",
        "Model: Example model",
        "Model ID: MODEL-1",
        "Serial number: SERIAL-1",
        "Software version: 1.2.3",
        "Hardware version: A",
        "Category: Tool",
        "Notes: yes",
    ]
    assert _lines(result, "purchase") == [
        "Purchase: Workshop equipment",
        "Purchase date: 15 Jan 2026",
        "Seller: Example seller",
    ]
    assert _lines(result, "warranty") == ["Valid until 15 Jan 2028", "Type: 2 years"]
    # The warranty is never worded as part of the Purchase.
    assert "Warranty" not in result["description_placeholders"]["purchase"]
    for hidden in (
        "Existing asset notes",
        "ORDER-123",
        "example.invalid",
        "Existing purchase notes",
    ):
        assert hidden not in _shown(result)


async def test_unrecorded_details_are_left_out_consistently(
    hass: HomeAssistant,
) -> None:
    """Missing values are omitted, never dashed; the notes line always says."""
    manager = _manager(hass)
    bare = await manager.async_create_manual_asset(name="Bare unit")
    partial = await manager.async_create_manual_asset(
        name="Partial unit", manufacturer="Signify", sw_version="1.116.3"
    )
    flow, _hub = await _hub_flow(hass, manager, bare["asset_uuid"])
    bare_result = await _section(flow, DETAILS_WARRANTY)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: partial["asset_uuid"]})
    partial_result = await _section(flow, DETAILS_WARRANTY)

    assert _lines(bare_result, "details") == ["No other details recorded", "Notes: no"]
    assert _lines(partial_result, "details") == [
        "Manufacturer: Signify",
        "Software version: 1.116.3",
        "Notes: no",
    ]
    # A new manual Asset has neither a Purchase nor a warranty yet.
    for result in (bare_result, partial_result):
        assert _lines(result, "purchase") == ["Purchase: No Purchase"]
        assert _lines(result, "warranty") == ["Not specified"]
        assert "—" not in _shown(result)
        assert "None" not in _shown(result)


async def test_notes_text_never_reaches_asset_details(
    hass: HomeAssistant,
) -> None:
    """However long or multi-line, notes only ever read as yes."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(
        name="Noted unit",
        notes="First private line\nSecond private line " + LONG,
    )
    flow, _hub = await _hub_flow(hass, manager, asset["asset_uuid"])

    result = await _section(flow, DETAILS_WARRANTY)

    assert _lines(result, "details")[-1] == "Notes: yes"
    for hidden in ("First private line", "Second private line", LONG):
        assert hidden not in _shown(result)


async def test_technical_details_stay_useful_when_long(
    hass: HomeAssistant,
) -> None:
    """Model ID, serial and versions keep 64 characters; names still shorten."""
    manager = _manager(hass)
    serial = "SN-" + "0123456789" * 6 + "X"  # exactly 64 characters
    model_id = "MODEL-" + "ABCDEFGHIJ" * 8  # 86 characters
    asset = await manager.async_create_manual_asset(
        name="Technical unit",
        manufacturer="Manufacturer with a rather long legal name Oy",
        model="Hue White and Color Ambiance A19 E26/E27 Smart Bulb",
        model_id=model_id,
        serial_number=serial,
        sw_version="  1.122.8\n(build 2026-08-01)  ",
        hw_version="Rev C",
    )
    flow, _hub = await _hub_flow(hass, manager, asset["asset_uuid"])

    lines = _lines(await _section(flow, DETAILS_WARRANTY), "details")

    assert len(serial) == DETAIL_VALUE_MAX_LENGTH
    assert f"Serial number: {serial}" in lines
    assert f"Model ID: {model_id[:63]}…" in lines
    assert "Model: Hue White and Color Ambiance A19 E26/E27 Smart Bulb" in lines
    # A value with line breaks stays one fact on one line.
    assert "Software version: 1.122.8 (build 2026-08-01)" in lines
    assert "Hardware version: Rev C" in lines
    # Long names still follow the summary rule.
    assert "Manufacturer: Manufacturer with a rat…" in lines
    _most, longest = GROUPS[DETAILS_WARRANTY]["details"]
    assert all(len(line) <= longest for line in lines)


@pytest.mark.parametrize("language", ["en", "fi"])
def test_detail_labels_match_the_editor_fields(language: str) -> None:
    """The view and the editor call each detail by the same name."""
    fields = _translations(language)["edit_asset_metadata"]["data"]

    for field, (english, finnish) in DETAIL_FIELDS.items():
        assert fields[field] == (english if language == "en" else finnish), field


@pytest.mark.parametrize("editor", ["edit_asset_metadata", "change_asset_purchase"])
@pytest.mark.parametrize(("language", "label"), [("en", "Save"), ("fi", "Tallenna")])
def test_both_editors_buttons_save(language: str, label: str, editor: str) -> None:
    """Reached through an explicit edit row, each editor's button says Save."""
    assert _translations(language)[editor]["submit"] == label


@pytest.mark.parametrize(
    ("language", "title"), [("en", "Linked purchase"), ("fi", "Ostoslinkitys")]
)
def test_the_purchase_editor_is_named_for_the_link_it_changes(
    language: str, title: str
) -> None:
    """The form changes the Purchase link only, and its title says so."""
    step = _translations(language)["change_asset_purchase"]

    assert step["title"] == title
    assert "takuu" not in step["title"].casefold()
    assert "warranty" not in step["title"].casefold()


async def test_an_unnamed_purchase_does_not_repeat_its_seller(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Without a name the Purchase is already labelled by seller and date."""
    manager = _manager(hass, asset_store_data)
    manager._data["purchases"][PURCHASE_UUID]["name"] = None
    flow, _hub = await _hub_flow(hass, manager)

    result = await _section(flow, DETAILS_WARRANTY)

    assert _lines(result, "purchase") == [
        "Purchase: Example seller",
        "Purchase date: 15 Jan 2026",
    ]


async def test_a_purchase_with_nothing_to_name_it_by_is_not_named_by_uuid(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """No name, seller or date: a word, not the Purchase's identifier."""
    manager = _manager(hass, asset_store_data)
    purchase = manager._data["purchases"][PURCHASE_UUID]
    purchase.update({"name": None, "seller": None, "purchase_date": None})
    flow, _hub = await _hub_flow(hass, manager)

    result = await _section(flow, DETAILS_WARRANTY)

    assert _lines(result, "purchase") == ["Purchase: Unnamed Purchase"]
    assert PURCHASE_UUID not in _shown(result)


async def test_purchase_states_without_a_current_purchase_stay_readable(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    freezer,
) -> None:
    """No Purchase, a historical one, and an unavailable one, all in words.

    The warranty is the Asset's own, so it reads the same whichever of these
    the Purchase is in.
    """
    freezer.move_to(TODAY)
    manager = _manager(hass, asset_store_data)
    flow, _hub = await _hub_flow(hass, manager)
    purchase = manager._data["purchases"][PURCHASE_UUID]
    asset = manager._data["assets"][ASSET_UUID]

    purchase["configured"] = False
    historical = await _section(flow, DETAILS_WARRANTY)
    asset["purchase_uuid"] = SECOND_PURCHASE_UUID
    unavailable = await _section(flow, DETAILS_WARRANTY)
    asset["purchase_uuid"] = None
    none = await _section(flow, DETAILS_WARRANTY)

    assert _lines(historical, "purchase") == [
        "Purchase: Historical — Workshop equipment",
        "Purchase date: 15 Jan 2026",
        "Seller: Example seller",
    ]
    assert _lines(unavailable, "purchase") == ["Purchase: Unavailable Purchase"]
    assert _lines(none, "purchase") == ["Purchase: No Purchase"]
    for result in (historical, unavailable, none):
        assert _lines(result, "warranty") == [
            "Valid until 15 Jan 2028",
            "Type: 2 years",
        ]
        assert PURCHASE_UUID not in _shown(result)
        assert SECOND_PURCHASE_UUID not in _shown(result)


@pytest.mark.parametrize(
    ("warranty", "english", "finnish"),
    [
        pytest.param(
            {"type": WARRANTY_TWO_YEARS, "until": "2028-01-15"},
            ["Valid until 15 Jan 2028", "Type: 2 years"],
            ["Voimassa 15.1.2028 asti", "Tyyppi: 2 vuotta"],
            id="valid",
        ),
        pytest.param(
            {"type": WARRANTY_MANUAL, "until": "2026-09-24"},
            ["Valid until 24 Sep 2026", "Type: Manual"],
            ["Voimassa 24.9.2026 asti", "Tyyppi: Manuaalinen"],
            id="valid-through-today",
        ),
        pytest.param(
            {"type": WARRANTY_ONE_YEAR, "until": "2026-09-23"},
            ["Expired on 23 Sep 2026", "Type: 1 year"],
            ["Päättynyt 23.9.2026", "Tyyppi: 1 vuosi"],
            id="expired",
        ),
        pytest.param(
            {"type": WARRANTY_ONE_YEAR, "until": None},
            ["End date unknown", "Type: 1 year"],
            ["Päättymispäivä ei tiedossa", "Tyyppi: 1 vuosi"],
            id="end-unknown",
        ),
        pytest.param(
            {"type": WARRANTY_NONE, "until": None},
            ["Not specified"],
            ["Ei määritetty"],
            id="not-specified",
        ),
        pytest.param(
            {},
            ["Not specified"],
            ["Ei määritetty"],
            id="missing",
        ),
    ],
)
async def test_warranty_states_follow_the_warranty_sensor(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    freezer,
    warranty: dict[str, Any],
    english: list[str],
    finnish: list[str],
) -> None:
    """Valid through its end date, expired after it, and never guessed."""
    freezer.move_to(TODAY)
    manager = _manager(hass, asset_store_data)
    manager._data["assets"][ASSET_UUID]["warranty"] = warranty
    flow, _hub = await _hub_flow(hass, manager)

    shown_english = _lines(await _section(flow, DETAILS_WARRANTY), "warranty")
    hass.config.language = "fi"
    shown_finnish = _lines(await _section(flow, DETAILS_WARRANTY), "warranty")

    assert shown_english == english
    assert shown_finnish == finnish


async def test_changing_the_purchase_from_the_section_leaves_the_warranty_alone(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    freezer,
) -> None:
    """End to end: relinking through the merged section keeps the warranty."""
    freezer.move_to(TODAY)
    data = _data_with_second_purchase(asset_store_data)
    # A different date, so a warranty recomputed from it would show.
    data["purchases"][SECOND_PURCHASE_UUID]["purchase_date"] = "2025-03-01"
    manager = _manager(hass, data)
    flow_id = await _flow_manager_on_asset(hass, manager, ASSET_UUID)
    warranty = deepcopy(manager.asset(ASSET_UUID)["warranty"])
    source = manager.asset(ASSET_UUID)["field_sources"]["warranty"]

    before = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": DETAILS_WARRANTY}
    )
    await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "change_asset_purchase"}
    )
    with capture_reloads(hass):
        hub = await hass.config_entries.options.async_configure(
            flow_id, {CONF_PURCHASE_UUID: SECOND_PURCHASE_UUID}
        )
    after = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": DETAILS_WARRANTY}
    )

    assert hub["step_id"] == HUB_STEP
    assert hub["description_placeholders"]["result"] == "Linked purchase updated."
    asset = manager.asset(ASSET_UUID)
    assert asset["purchase_uuid"] == SECOND_PURCHASE_UUID
    assert asset["warranty"] == warranty
    assert asset["field_sources"]["warranty"] == source
    assert _lines(after, "purchase")[0] == "Purchase: Second purchase"
    assert _lines(after, "warranty") == _lines(before, "warranty") == [
        "Valid until 15 Jan 2028",
        "Type: 2 years",
    ]


async def test_installation_shows_state_date_and_area_by_name(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """Installed, since when, and where, with the Area named for people."""
    manager, workshop = await _rich_asset(hass, area_registry, asset_store_data)
    flow, _hub = await _hub_flow(hass, manager)

    result = await _section(flow, "asset_installation_menu")

    assert _lines(result) == [
        "Installation: Installed",
        "Installation date: 20 Jan 2026",
        "Location: Workshop",
    ]
    assert workshop.id not in _shown(result)


async def test_installation_edge_states_are_readable_and_untouched(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """No Area, a vanished Area, and a leftover Area, none of them repaired."""
    manager = _manager(hass, asset_store_data)
    flow, _hub = await _hub_flow(hass, manager)
    asset = manager._data["assets"][ASSET_UUID]

    unknown = await _section(flow, "asset_installation_menu")
    asset[CONF_DEPLOYMENT_STATE] = DEPLOYMENT_STATE_DEPLOYED
    asset[CONF_HA_AREA_ID] = "vanished-area-id"
    vanished = await _section(flow, "asset_installation_menu")
    asset[CONF_DEPLOYMENT_STATE] = DEPLOYMENT_STATE_NOT_DEPLOYED
    leftover = await _section(flow, "asset_installation_menu")
    after = deepcopy(manager._data)

    assert _lines(unknown) == [
        "Installation: Unknown",
        "Installation date: 20 Jan 2026",
        "Location: No Area",
    ]
    assert _lines(vanished)[-1] == "Location: Unavailable Home Assistant Area"
    assert _lines(leftover) == [
        "Installation: Not installed · inconsistent location data",
        "Installation date: 20 Jan 2026",
        "Location: Unavailable Home Assistant Area",
    ]
    for result in (vanished, leftover):
        assert "vanished-area-id" not in _shown(result)
    # Showing a stale reference never repairs it.
    assert after["assets"][ASSET_UUID][CONF_HA_AREA_ID] == "vanished-area-id"
    assert asset[CONF_DEPLOYMENT_STATE] == DEPLOYMENT_STATE_NOT_DEPLOYED


async def test_installation_without_a_date_leaves_the_date_out(
    hass: HomeAssistant,
) -> None:
    """A new manual Asset has no installation date, so no line for it."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="New spare")
    flow, _hub = await _hub_flow(hass, manager, asset["asset_uuid"])

    result = await _section(flow, "asset_installation_menu")

    assert _lines(result) == ["Installation: Not installed", "Location: No Area"]


NO_RELATIONSHIP = "No active relationship"


async def _lifecycle_state(manager: AssetStoreManager, state: str) -> None:
    """Record the fixture Asset's Lifecycle through the canonical operation."""
    status, date = {
        "active": ("active", "2026-08-11"),
        "retired": ("retired", "2026-09-12"),
    }[state]
    await manager.async_set_asset_lifecycle(
        ASSET_UUID, status, effective_date=date, notes="Lifecycle note hidden"
    )


@pytest.mark.parametrize(
    ("state", "replaces", "replaced_by", "stale", "lifecycle", "replacement"),
    [
        pytest.param(
            "active", False, False, False,
            ["Status: Active", "Effective from: 11 Aug 2026"],
            [NO_RELATIONSHIP, NO_RELATIONSHIP],
            id="A-active-no-replacement",
        ),
        pytest.param(
            "active", True, False, False,
            ["Status: Active", "Effective from: 11 Aug 2026"],
            ["Smoke test 0.7.1 · DL0008", NO_RELATIONSHIP],
            id="B-active-replaces-predecessor",
        ),
        pytest.param(
            "active", False, True, False,
            ["Status: Active", "Effective from: 11 Aug 2026"],
            [NO_RELATIONSHIP, "Uusi lämmitin · DL0009"],
            id="C-active-replaced-by-successor",
        ),
        pytest.param(
            "active", True, True, False,
            ["Status: Active", "Effective from: 11 Aug 2026"],
            ["Smoke test 0.7.1 · DL0008", "Uusi lämmitin · DL0009"],
            id="D-active-both-directions",
        ),
        pytest.param(
            "retired", False, False, False,
            ["Status: Retired", "Effective from: 12 Sep 2026"],
            [NO_RELATIONSHIP, NO_RELATIONSHIP],
            id="E-retired-no-replacement",
        ),
        pytest.param(
            "retired", False, True, False,
            ["Status: Retired", "Effective from: 12 Sep 2026"],
            [NO_RELATIONSHIP, "Uusi lämmitin · DL0009"],
            id="F-retired-replaced-by-successor",
        ),
        pytest.param(
            "active", True, True, True,
            ["Status: Active", "Effective from: 11 Aug 2026"],
            ["Unavailable Asset", "Unavailable Asset"],
            id="G-stale-references",
        ),
    ],
)
async def test_lifecycle_and_replacement_states_read_in_words(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    state: str,
    replaces: bool,
    replaced_by: bool,
    stale: bool,
    lifecycle: list[str],
    replacement: list[str],
) -> None:
    """The state and both directions, each named for people, never by UUID."""
    manager = _manager(hass, asset_store_data)
    await _lifecycle_state(manager, state)
    predecessor, successor, records = await _chain(
        manager, replaces=replaces, replaced_by=replaced_by
    )
    if stale:
        # A validated Store cannot hold this; the view must still not leak.
        for partner in (predecessor, successor):
            manager._data["assets"].pop(partner["asset_uuid"])
    flow, _hub = await _hub_flow(hass, manager)

    result = await _section(flow, LIFECYCLE_REPLACEMENT)
    shown = _shown(result)

    assert _lines(result, "lifecycle") == lifecycle
    assert _lines(result, "replacement") == [
        f"This Asset replaces: {replacement[0]}",
        f"This Asset was replaced by: {replacement[1]}",
    ]
    for hidden in (
        ASSET_UUID,
        predecessor["asset_uuid"],
        successor["asset_uuid"],
        *(record["replacement_uuid"] for record in records),
        "→",
        "Lifecycle note",
        "Replacement note",
    ):
        assert hidden not in shown, hidden


async def test_a_voided_relationship_is_history_not_a_current_fact(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Only active records show, the same way the submenu reads them."""
    manager = _manager(hass, asset_store_data)
    _predecessor, _successor, records = await _chain(manager, replaces=False)
    flow, _hub = await _hub_flow(hass, manager)
    before = await _section(flow, LIFECYCLE_REPLACEMENT)

    await manager.async_void_asset_replacement(
        records[0]["replacement_uuid"], void_reason="Recorded in error"
    )
    after = await _section(flow, LIFECYCLE_REPLACEMENT)

    assert _lines(before, "replacement")[1] == (
        "This Asset was replaced by: Uusi lämmitin · DL0009"
    )
    assert _lines(after, "replacement") == [
        f"This Asset replaces: {NO_RELATIONSHIP}",
        f"This Asset was replaced by: {NO_RELATIONSHIP}",
    ]
    assert _lines(after, "lifecycle") == _lines(before, "lifecycle")


async def test_lifecycle_and_replacement_read_in_finnish(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Both directions are whole Finnish sentences, as the submenu words them."""
    hass.config.language = "fi"
    manager = _manager(hass, asset_store_data)
    await _lifecycle_state(manager, "retired")
    predecessor, _successor, _records = await _chain(manager)
    flow, _hub = await _hub_flow(hass, manager)

    both = await _section(flow, LIFECYCLE_REPLACEMENT)
    manager._data["assets"].pop(predecessor["asset_uuid"])
    stale = await _section(flow, LIFECYCLE_REPLACEMENT)

    assert _lines(both, "lifecycle") == [
        "Tila: Käytöstä poistettu",
        "Voimassa alkaen: 12.9.2026",
    ]
    assert _lines(both, "replacement") == [
        "Tämä laite korvaa: Smoke test 0.7.1 · DL0008",
        "Tämän laitteen korvasi: Uusi lämmitin · DL0009",
    ]
    assert _lines(stale, "replacement")[0] == (
        "Tämä laite korvaa: Laite ei ole enää käytettävissä"
    )


async def test_manage_replacement_opens_the_unchanged_submenu(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """End to end: the same operations, and its own Back to the hub."""
    manager = _manager(hass, asset_store_data)
    await _chain(manager, replaces=False)
    flow_id = await _flow_manager_on_asset(hass, manager, ASSET_UUID)
    await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": LIFECYCLE_REPLACEMENT}
    )
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        submenu = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": REPLACEMENT_SUBMENU}
        )
        hub = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": BACK}
        )

    assert submenu["type"] is FlowResultType.MENU
    assert submenu["step_id"] == REPLACEMENT_SUBMENU
    assert submenu["menu_options"] == [
        "replacement_replaces",
        "replacement_replaced_by",
        "manage_asset_replacement",
        BACK,
    ]
    assert submenu["description_placeholders"]["result"] == ""
    assert hub["step_id"] == HUB_STEP
    assert hub["description_placeholders"]["result"] == ""
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()
    assert manager._data == before


async def test_recording_a_replacement_leaves_the_lifecycle_alone(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """End to end: a relationship recorded from the section changes only itself."""
    manager, _workshop = await _rich_asset(hass, area_registry, asset_store_data)
    await manager.async_add_related_device(ASSET_UUID, "related-device-id")
    predecessor = await manager.async_create_manual_asset(name="Smoke test 0.7.1")
    flow_id = await _flow_manager_on_asset(hass, manager, ASSET_UUID)
    before_asset = deepcopy(manager.asset(ASSET_UUID))
    before_events = manager.lifecycle_events_for_asset(ASSET_UUID)
    before_predecessor = deepcopy(manager.asset(predecessor["asset_uuid"]))

    section = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": LIFECYCLE_REPLACEMENT}
    )
    await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": REPLACEMENT_SUBMENU}
    )
    form = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "replacement_replaces"}
    )
    with capture_reloads(hass):
        submenu = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_REPLACEMENT_TARGET_ASSET_UUID: predecessor["asset_uuid"],
                CONF_REPLACEMENT_REASON: "failure",
                CONF_EFFECTIVE_DATE: "2026-08-11",
            },
        )
    await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": BACK}
    )
    after = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": LIFECYCLE_REPLACEMENT}
    )

    # Phase 1: the form still starts on the placeholder, not on an Asset.
    assert form["step_id"] == "replacement_replaces"
    target = next(
        marker
        for marker in form["data_schema"].schema
        if marker == CONF_REPLACEMENT_TARGET_ASSET_UUID
    )
    assert target.default() == NOT_SELECTED
    # Recording a relationship keeps the result in the replacement submenu.
    assert submenu["step_id"] == REPLACEMENT_SUBMENU
    assert submenu["description_placeholders"]["result"] == "Replacement updated."
    # Neither Asset's Lifecycle, installation, Area, Purchase, warranty or
    # Home Assistant devices moved.
    assert manager.asset(ASSET_UUID) == before_asset
    assert manager.asset(predecessor["asset_uuid"]) == before_predecessor
    assert manager.lifecycle_events_for_asset(ASSET_UUID) == before_events
    assert _lines(after, "lifecycle") == _lines(section, "lifecycle")
    assert _lines(after, "replacement")[0] == (
        "This Asset replaces: Smoke test 0.7.1 · DL0008"
    )


async def test_recording_a_lifecycle_change_leaves_replacement_alone(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """End to end: retiring from the section keeps every relationship."""
    manager = _manager(hass, asset_store_data)
    await _chain(manager)
    records = deepcopy(manager._data["replacement_records"])
    flow_id = await _flow_manager_on_asset(hass, manager, ASSET_UUID)

    section = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": LIFECYCLE_REPLACEMENT}
    )
    await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "asset_lifecycle"}
    )
    with capture_reloads(hass):
        hub = await hass.config_entries.options.async_configure(
            flow_id,
            {CONF_LIFECYCLE_STATUS: "retired", CONF_EFFECTIVE_DATE: "2026-09-12"},
        )
    after = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": LIFECYCLE_REPLACEMENT}
    )

    assert hub["step_id"] == HUB_STEP
    assert hub["description_placeholders"]["result"] == "Lifecycle updated."
    assert manager._data["replacement_records"] == records
    assert _lines(after, "lifecycle") == [
        "Status: Retired",
        "Effective from: 12 Sep 2026",
    ]
    assert _lines(after, "replacement") == _lines(section, "replacement")


async def test_lifecycle_shows_the_current_state_not_its_history(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """Status and effective date of the current event, no notes, no history."""
    manager, _workshop = await _rich_asset(hass, area_registry, asset_store_data)
    flow, _hub = await _hub_flow(hass, manager)
    event_uuid = manager.asset(ASSET_UUID)["lifecycle"]["current_event_uuid"]

    result = await _section(flow, LIFECYCLE_REPLACEMENT)

    assert _lines(result, "lifecycle") == [
        "Status: Active",
        "Effective from: 2 Jan 2026",
    ]
    assert "Lifecycle note" not in _shown(result)
    assert event_uuid not in _shown(result)


async def test_lifecycle_without_an_effective_date_is_one_line(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A migrated unknown Lifecycle has no event, so no date to show."""
    manager = _manager(hass, asset_store_data)
    flow, _hub = await _hub_flow(hass, manager)

    result = await _section(flow, LIFECYCLE_REPLACEMENT)

    assert _lines(result, "lifecycle") == ["Status: Unknown"]


async def test_sections_read_in_finnish(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    asset_store_data: AssetStoreData,
    freezer,
) -> None:
    """Every section uses the management vocabulary and Finnish dates."""
    freezer.move_to(TODAY)
    hass.config.language = "fi"
    manager, _workshop = await _rich_asset(hass, area_registry, asset_store_data)
    flow, _hub = await _hub_flow(hass, manager)

    details_warranty = await _section(flow, DETAILS_WARRANTY)
    installation = await _section(flow, "asset_installation_menu")
    lifecycle_replacement = await _section(flow, LIFECYCLE_REPLACEMENT)

    assert _lines(details_warranty, "details") == [
        "Valmistaja: Example manufacturer",
        "Malli: Example model",
        "Mallitunnus: MODEL-1",
        "Sarjanumero: SERIAL-1",
        "Ohjelmistoversio: 1.2.3",
        "Laitteistoversio: A",
        "Luokka: Tool",
        "Muistiinpanot: on",
    ]
    assert _lines(details_warranty, "purchase") == [
        "Ostos: Workshop equipment",
        "Ostopäivä: 15.1.2026",
        "Myyjä: Example seller",
    ]
    assert _lines(details_warranty, "warranty") == [
        "Voimassa 15.1.2028 asti",
        "Tyyppi: 2 vuotta",
    ]
    assert _lines(installation) == [
        "Asennustila: Asennettu",
        "Asennuspäivä: 20.1.2026",
        "Sijainti: Workshop",
    ]
    assert _lines(lifecycle_replacement, "lifecycle") == [
        "Tila: Aktiivinen",
        "Voimassa alkaen: 2.1.2026",
    ]
    assert _lines(lifecycle_replacement, "replacement") == [
        "Tämä laite korvaa: Ei aktiivista suhdetta",
        "Tämän laitteen korvasi: Ei aktiivista suhdetta",
    ]


async def test_finnish_edge_states_are_named_in_finnish(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """The none, historical, unavailable and stale wordings exist in Finnish."""
    hass.config.language = "fi"
    manager = _manager(hass, asset_store_data)
    flow, _hub = await _hub_flow(hass, manager)
    asset = manager._data["assets"][ASSET_UUID]
    manager._data["purchases"][PURCHASE_UUID]["configured"] = False
    asset[CONF_HA_AREA_ID] = "vanished-area-id"
    asset[CONF_DEPLOYMENT_STATE] = DEPLOYMENT_STATE_UNKNOWN

    historical = await _section(flow, DETAILS_WARRANTY)
    stale_area = await _section(flow, "asset_installation_menu")
    asset[CONF_DEPLOYMENT_STATE] = DEPLOYMENT_STATE_NOT_DEPLOYED
    leftover = await _section(flow, "asset_installation_menu")
    asset["purchase_uuid"] = SECOND_PURCHASE_UUID
    unavailable = await _section(flow, DETAILS_WARRANTY)
    asset["purchase_uuid"] = None
    no_purchase = await _section(flow, DETAILS_WARRANTY)

    assert _lines(historical, "purchase")[0] == (
        "Ostos: Historiallinen — Workshop equipment"
    )
    assert _lines(stale_area) == [
        "Asennustila: Ei tiedossa",
        "Asennuspäivä: 20.1.2026",
        "Sijainti: Alue ei ole enää käytettävissä",
    ]
    assert _lines(leftover)[0] == (
        "Asennustila: Ei asennettu · sijaintitieto epäkonsistentti"
    )
    assert _lines(unavailable, "purchase") == ["Ostos: Osto ei saatavilla"]
    assert _lines(no_purchase, "purchase") == ["Ostos: Ei ostosta"]


@pytest.mark.parametrize("section", SECTIONS)
async def test_no_section_shows_a_technical_identifier(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    asset_store_data: AssetStoreData,
    section: str,
) -> None:
    """UUIDs, registry IDs, receipts and notes never reach a section."""
    manager, workshop = await _rich_asset(hass, area_registry, asset_store_data)
    await manager.async_add_related_device(ASSET_UUID, "related-device-id")
    predecessor, successor, records = await _chain(manager)
    flow, _hub = await _hub_flow(hass, manager)
    asset = manager.asset(ASSET_UUID)

    result = await _section(flow, section)
    shown = _shown(result)

    for hidden in (
        ASSET_UUID,
        predecessor["asset_uuid"],
        successor["asset_uuid"],
        *(record["replacement_uuid"] for record in records),
        "Replacement note",
        PURCHASE_UUID,
        DEVICE_ID,
        "related-device-id",
        workshop.id,
        asset["lifecycle"]["current_event_uuid"],
        "purchase-subentry-id",
        "ORDER-123",
        "https://",
        "Existing asset notes",
        "Existing purchase notes",
        "Lifecycle note",
    ):
        assert hidden not in shown, hidden


@pytest.mark.parametrize("language", ["en", "fi"])
@pytest.mark.parametrize("section", SECTIONS)
async def test_long_values_stay_within_the_summary_budget(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    asset_store_data: AssetStoreData,
    freezer,
    section: str,
    language: str,
) -> None:
    """Long names shorten, every line fits a menu row, and sections stay short."""
    freezer.move_to(TODAY)
    hass.config.language = language
    data = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    for field in (
        "name",
        "category",
        "manufacturer",
        "model",
        "model_id",
        "serial_number",
        "sw_version",
        "hw_version",
    ):
        asset[field] = f"{field} {LONG}"
    asset["notes"] = LONG * 3
    purchase = data["purchases"][PURCHASE_UUID]
    purchase["name"] = f"Purchase {LONG}"
    purchase["seller"] = f"Seller {LONG}"
    manager = _manager(hass, data)
    area = area_registry.async_create(f"Area {LONG}")
    await manager.async_set_asset_deployment_reporting(
        ASSET_UUID,
        deployment_state=DEPLOYMENT_STATE_DEPLOYED,
        ha_area_id=area.id,
    )
    await _chain(
        manager,
        predecessor_name=f"Predecessor {LONG}",
        successor_name=f"Successor {LONG}",
    )
    flow, _hub = await _hub_flow(hass, manager)

    result = await _section(flow, section)

    assert result["description_placeholders"]["asset"] == (
        "name Very long user sup… · DL0007"
    )
    for group, (most, longest) in GROUPS[section].items():
        lines = _lines(result, group)
        assert 1 <= len(lines) <= most, (group, lines)
        assert all(len(line) <= longest for line in lines), (group, lines)
    assert LONG not in _shown(result)


async def test_saving_from_a_section_still_lands_on_the_hub(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """End to end: the editors keep their 0.7.4 destination after a save."""
    manager = _manager(hass, asset_store_data)
    flow_id = await _flow_manager_on_asset(hass, manager, ASSET_UUID)
    submissions = [
        (
            DETAILS_WARRANTY,
            "edit_asset_metadata",
            _identical_metadata_input(manager.asset(ASSET_UUID)),
        ),
        (
            DETAILS_WARRANTY,
            "change_asset_purchase",
            {CONF_PURCHASE_UUID: PURCHASE_UUID},
        ),
        (
            "asset_installation_menu",
            "asset_deployment",
            {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_UNKNOWN},
        ),
        (
            LIFECYCLE_REPLACEMENT,
            "asset_lifecycle",
            {CONF_LIFECYCLE_STATUS: "unknown"},
        ),
    ]

    for section, editor, submission in submissions:
        await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": section}
        )
        await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": editor}
        )
        landed = await hass.config_entries.options.async_configure(
            flow_id, submission
        )

        assert landed["type"] is FlowResultType.MENU, editor
        assert landed["step_id"] == HUB_STEP, editor
        assert landed["description_placeholders"]["result"], editor
