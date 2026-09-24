"""Hub rows 1-4 open a read-only section menu before any editor.

Asset details, Purchase & warranty, Installation & location and Lifecycle
each show the Asset's current state in a few short lines, offer their
existing editor as one row, and go back to the hub with a real menu row.
Somebody who only wants to look never has to open a form or use its Submit
button as a way back.

These tests own that navigation contract, the copy budget of each section,
and that the editors themselves are exactly the forms they were before.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar

from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_DEPLOYMENT_STATE,
    CONF_HA_AREA_ID,
    CONF_LIFECYCLE_STATUS,
    CONF_PURCHASE_UUID,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DEPLOYMENT_STATE_UNKNOWN,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import ASSET_UUID, DEVICE_ID, PURCHASE_UUID, capture_reloads
from .test_explicit_selection_safety import _flow_manager_on_asset
from .test_options_flow import _identical_metadata_input, _manager, _options_flow

HUB_STEP = "manage_asset_menu"
BACK = "manage_asset_menu"
# Section menu -> the editor it opens, in hub row order.
SECTIONS = {
    "asset_details_menu": "edit_asset_metadata",
    "asset_purchase_menu": "change_asset_purchase",
    "asset_installation_menu": "asset_deployment",
    "asset_lifecycle_menu": "asset_lifecycle",
}
# The longest a section may grow: one line naming the Asset, then its facts.
MAX_FACT_LINES = {
    "asset_details_menu": 4,
    "asset_purchase_menu": 4,
    "asset_installation_menu": 3,
    "asset_lifecycle_menu": 2,
}
SUMMARY_MAX_LENGTH = 60
LONG = "Very long user supplied text that keeps going well past any summary"


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


def _lines(result: dict[str, Any]) -> list[str]:
    """Return the section's fact lines, as the description shows them."""
    return result["description_placeholders"]["facts"].split("\n")


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


@pytest.mark.parametrize(("section", "editor"), SECTIONS.items())
async def test_each_hub_row_opens_its_section_menu(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    section: str,
    editor: str,
) -> None:
    """A section is a menu: its editor, then Back, and nothing else."""
    manager = _manager(hass, asset_store_data)
    flow, hub = await _hub_flow(hass, manager)

    result = await _section(flow, section)

    assert list(SECTIONS).index(section) == hub["menu_options"].index(section)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == section
    assert result["menu_options"] == [editor, BACK]
    assert set(result["description_placeholders"]) == {"asset", "facts"}
    assert result["description_placeholders"]["asset"] == "Workshop device · DL0007"


@pytest.mark.parametrize(("section", "editor"), SECTIONS.items())
async def test_the_action_row_opens_the_existing_editor(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    section: str,
    editor: str,
) -> None:
    """End to end: hub row, then the action row, then the same form as before."""
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
    assert form["type"] is FlowResultType.FORM
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
) -> None:
    """Nothing about a section is kept in the flow between renders."""
    manager = _manager(hass, asset_store_data)
    flow, _hub = await _hub_flow(hass, manager)
    first = await _section(flow, "asset_details_menu")

    await manager.async_update_asset_metadata(ASSET_UUID, serial_number="NEW-SERIAL")
    second = await _section(flow, "asset_details_menu")

    assert "Serial number: SERIAL-1" in _lines(first)
    assert "Serial number: NEW-SERIAL" in _lines(second)


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


async def test_asset_details_show_what_it_is_but_not_its_notes(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Identity, category and serial number; notes only as a yes."""
    manager = _manager(hass, asset_store_data)
    flow, _hub = await _hub_flow(hass, manager)

    result = await _section(flow, "asset_details_menu")

    assert _lines(result) == [
        "Example manufacturer · Example model",
        "Category: Tool",
        "Serial number: SERIAL-1",
        "Notes: yes",
    ]
    assert "Existing asset notes" not in _shown(result)


async def test_asset_details_with_little_recorded_stay_short(
    hass: HomeAssistant,
) -> None:
    """Missing facts are left out, and a category never appears twice."""
    manager = _manager(hass)
    bare = await manager.async_create_manual_asset(name="Bare unit")
    categorized = await manager.async_create_manual_asset(
        name="Categorized unit", category="Heater"
    )
    flow, _hub = await _hub_flow(hass, manager, bare["asset_uuid"])
    bare_result = await _section(flow, "asset_details_menu")
    await flow.async_step_manage_asset({CONF_ASSET_UUID: categorized["asset_uuid"]})
    categorized_result = await _section(flow, "asset_details_menu")

    assert _lines(bare_result) == ["No manufacturer or model recorded"]
    assert _lines(categorized_result) == ["Heater"]


async def test_purchase_and_warranty_are_separate_facts(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A named Purchase with its date and seller, then the Asset's warranty."""
    manager = _manager(hass, asset_store_data)
    flow, _hub = await _hub_flow(hass, manager)

    result = await _section(flow, "asset_purchase_menu")

    assert _lines(result) == [
        "Purchase: Workshop equipment",
        "Purchase date: 15 Jan 2026",
        "Seller: Example seller",
        "Warranty: until 15 Jan 2028",
    ]
    for hidden in ("ORDER-123", "example.invalid", "Existing purchase notes"):
        assert hidden not in _shown(result)


async def test_an_unnamed_purchase_does_not_repeat_its_seller(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Without a name the Purchase is already labelled by seller and date."""
    manager = _manager(hass, asset_store_data)
    manager._data["purchases"][PURCHASE_UUID]["name"] = None
    flow, _hub = await _hub_flow(hass, manager)

    result = await _section(flow, "asset_purchase_menu")

    assert _lines(result) == [
        "Purchase: Example seller",
        "Purchase date: 15 Jan 2026",
        "Warranty: until 15 Jan 2028",
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

    result = await _section(flow, "asset_purchase_menu")

    assert _lines(result) == [
        "Purchase: Unnamed Purchase",
        "Warranty: until 15 Jan 2028",
    ]
    assert PURCHASE_UUID not in _shown(result)


async def test_purchase_states_without_a_current_purchase_stay_readable(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """No Purchase, a historical one, and an unavailable one, all in words."""
    manager = _manager(hass, asset_store_data)
    flow, _hub = await _hub_flow(hass, manager)
    purchase = manager._data["purchases"][PURCHASE_UUID]
    asset = manager._data["assets"][ASSET_UUID]

    purchase["configured"] = False
    historical = await _section(flow, "asset_purchase_menu")
    asset["purchase_uuid"] = "44444444-4444-4444-8444-444444444444"
    unavailable = await _section(flow, "asset_purchase_menu")
    asset["purchase_uuid"] = None
    asset["warranty"] = {"type": "none", "until": None}
    none = await _section(flow, "asset_purchase_menu")

    assert _lines(historical)[0] == "Purchase: Historical — Workshop equipment"
    assert _lines(unavailable) == [
        "Purchase: Unavailable Purchase",
        "Warranty: until 15 Jan 2028",
    ]
    assert _lines(none) == ["Purchase: No Purchase", "Warranty: None"]
    for result in (historical, unavailable, none):
        assert PURCHASE_UUID not in _shown(result)
        assert "44444444" not in _shown(result)


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


async def test_lifecycle_shows_the_current_state_not_its_history(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """Status and effective date of the current event, no notes, no history."""
    manager, _workshop = await _rich_asset(hass, area_registry, asset_store_data)
    flow, _hub = await _hub_flow(hass, manager)
    event_uuid = manager.asset(ASSET_UUID)["lifecycle"]["current_event_uuid"]

    result = await _section(flow, "asset_lifecycle_menu")

    assert _lines(result) == ["Status: Active", "Effective from: 2 Jan 2026"]
    assert "Lifecycle note" not in _shown(result)
    assert event_uuid not in _shown(result)


async def test_lifecycle_without_an_effective_date_is_one_line(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A migrated unknown Lifecycle has no event, so no date to show."""
    manager = _manager(hass, asset_store_data)
    flow, _hub = await _hub_flow(hass, manager)

    result = await _section(flow, "asset_lifecycle_menu")

    assert _lines(result) == ["Status: Unknown"]


async def test_sections_read_in_finnish(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """Every section uses the management vocabulary and Finnish dates."""
    hass.config.language = "fi"
    manager, _workshop = await _rich_asset(hass, area_registry, asset_store_data)
    flow, _hub = await _hub_flow(hass, manager)

    details = await _section(flow, "asset_details_menu")
    purchase = await _section(flow, "asset_purchase_menu")
    installation = await _section(flow, "asset_installation_menu")
    lifecycle = await _section(flow, "asset_lifecycle_menu")

    assert _lines(details) == [
        "Example manufacturer · Example model",
        "Luokka: Tool",
        "Sarjanumero: SERIAL-1",
        "Muistiinpanot: on",
    ]
    assert _lines(purchase) == [
        "Ostos: Workshop equipment",
        "Ostopäivä: 15.1.2026",
        "Myyjä: Example seller",
        "Takuu: 15.1.2028 asti",
    ]
    assert _lines(installation) == [
        "Asennustila: Asennettu",
        "Asennuspäivä: 20.1.2026",
        "Sijainti: Workshop",
    ]
    assert _lines(lifecycle) == ["Tila: Aktiivinen", "Voimassa alkaen: 2.1.2026"]


async def test_finnish_edge_states_are_named_in_finnish(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """The none, historical and stale wordings exist in Finnish too."""
    hass.config.language = "fi"
    manager = _manager(hass, asset_store_data)
    flow, _hub = await _hub_flow(hass, manager)
    asset = manager._data["assets"][ASSET_UUID]
    manager._data["purchases"][PURCHASE_UUID]["configured"] = False
    asset[CONF_HA_AREA_ID] = "vanished-area-id"
    asset[CONF_DEPLOYMENT_STATE] = DEPLOYMENT_STATE_UNKNOWN

    historical = await _section(flow, "asset_purchase_menu")
    stale_area = await _section(flow, "asset_installation_menu")
    asset[CONF_DEPLOYMENT_STATE] = DEPLOYMENT_STATE_NOT_DEPLOYED
    leftover = await _section(flow, "asset_installation_menu")
    asset["purchase_uuid"] = None
    no_purchase = await _section(flow, "asset_purchase_menu")

    assert _lines(historical)[0] == "Ostos: Historiallinen — Workshop equipment"
    assert _lines(stale_area) == [
        "Asennustila: Ei tiedossa",
        "Asennuspäivä: 20.1.2026",
        "Sijainti: Alue ei ole enää käytettävissä",
    ]
    assert _lines(leftover)[0] == (
        "Asennustila: Ei asennettu · sijaintitieto epäkonsistentti"
    )
    assert _lines(no_purchase)[0] == "Ostos: Ei ostosta"


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
    flow, _hub = await _hub_flow(hass, manager)
    asset = manager.asset(ASSET_UUID)

    result = await _section(flow, section)
    shown = _shown(result)

    for hidden in (
        ASSET_UUID,
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
    section: str,
    language: str,
) -> None:
    """Long names shorten, every line fits a menu row, and sections stay short."""
    hass.config.language = language
    data = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    for field in ("name", "category", "manufacturer", "model", "serial_number"):
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
    flow, _hub = await _hub_flow(hass, manager)

    result = await _section(flow, section)
    lines = _lines(result)

    assert result["description_placeholders"]["asset"] == (
        "name Very long user sup… · DL0007"
    )
    assert 1 <= len(lines) <= MAX_FACT_LINES[section]
    assert all(len(line) <= SUMMARY_MAX_LENGTH for line in lines), lines
    assert LONG not in _shown(result)


async def test_saving_from_a_section_still_lands_on_the_hub(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """End to end: the editors keep their 0.7.4 destination after a save."""
    manager = _manager(hass, asset_store_data)
    flow_id = await _flow_manager_on_asset(hass, manager, ASSET_UUID)
    submissions = {
        "asset_details_menu": (
            "edit_asset_metadata",
            _identical_metadata_input(manager.asset(ASSET_UUID)),
        ),
        "asset_purchase_menu": (
            "change_asset_purchase",
            {CONF_PURCHASE_UUID: PURCHASE_UUID},
        ),
        "asset_installation_menu": (
            "asset_deployment",
            {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_UNKNOWN},
        ),
        "asset_lifecycle_menu": (
            "asset_lifecycle",
            {CONF_LIFECYCLE_STATUS: "unknown"},
        ),
    }

    for section, (editor, submission) in submissions.items():
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
