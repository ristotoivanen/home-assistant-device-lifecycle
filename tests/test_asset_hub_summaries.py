"""What each Asset hub row says about the Asset underneath it.

One short line per area, in the reader's language, built from canonical
data and nothing else. These tests cover the states people actually reach,
including the awkward ones: a missing model, a Purchase that was never
linked, an Area that has since been deleted, a replacement that was voided.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr

from custom_components.device_lifecycle.config_flow import (
    SUMMARY_MAX_LENGTH,
    SUMMARY_NAME_MAX_LENGTH,
    _short_name,
    _summary_date,
    _summary_line,
)
from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_CATEGORY,
    CONF_HA_AREA_ID,
    CONF_INSTALLED_DATE,
    CONF_MANUFACTURER,
    CONF_MODEL,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DEPLOYMENT_STATE_UNKNOWN,
    LIFECYCLE_STATUS_ACTIVE,
    LIFECYCLE_STATUS_DISPOSED,
    LIFECYCLE_STATUS_LOST,
    LIFECYCLE_STATUS_RETIRED,
    WARRANTY_NONE,
    WARRANTY_TWO_YEARS,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import ASSET_UUID, capture_reloads
from .test_ha_relationship_options_flow import _external_device
from .test_options_flow import _manager, _options_flow

RAW_CANONICAL_VALUES = (
    "not_deployed",
    "deployed",
    "unknown",
    "retired",
    "disposed",
    "lost",
    "active",
)


async def _flow(hass: HomeAssistant, manager: AssetStoreManager, language: str = "en"):
    """Return a flow positioned on the fixture Asset, in one language."""
    hass.config.language = language
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: ASSET_UUID})
    return flow


def _asset_with(asset_store_data: AssetStoreData, **fields) -> AssetStoreData:
    """Return the fixture store with the selected Asset's fields replaced."""
    data = deepcopy(asset_store_data)
    data["assets"][ASSET_UUID].update(fields)
    return data


def test_summary_line_joins_only_what_has_something_to_say() -> None:
    """Empty parts disappear rather than leaving stray separators."""
    assert _summary_line("Installed", "", "15 Aug 2026") == (
        "Installed · 15 Aug 2026"
    )
    assert _summary_line("", "  ", None) == ""
    assert _summary_line("Alone") == "Alone"


def test_summary_line_truncates_without_a_dangling_separator() -> None:
    """A cut line ends in an ellipsis, never in a hanging separator."""
    line = _summary_line("x" * 40, "y" * 40)

    assert len(line) <= SUMMARY_MAX_LENGTH
    assert line.endswith("…")
    assert not line.rstrip("…").rstrip().endswith("·")


def test_summary_line_keeps_unicode_intact() -> None:
    """Truncation counts characters, so accents are never cut in half."""
    line = _summary_line("ä" * 80)

    assert len(line) <= SUMMARY_MAX_LENGTH
    assert set(line) <= {"ä", "…"}


def test_short_name_caps_one_display_name() -> None:
    """A single long name is shortened before it reaches a line."""
    assert _short_name("Short") == "Short"
    long_name = _short_name("N" * 60)
    assert len(long_name) <= SUMMARY_NAME_MAX_LENGTH
    assert long_name.endswith("…")
    assert _short_name(None) == ""


@pytest.mark.parametrize(
    ("value", "finnish", "expected"),
    [
        ("2026-08-15", True, "15.8.2026"),
        ("2026-08-15", False, "15 Aug 2026"),
        ("2026-12-01", True, "1.12.2026"),
        ("2026-12-01", False, "1 Dec 2026"),
        (None, True, ""),
        ("", False, ""),
        ("not-a-date", True, ""),
    ],
)
def test_summary_date_speaks_each_language(
    value: str | None,
    finnish: bool,
    expected: str,
) -> None:
    """Dates read the way each language writes them, or not at all."""
    assert _summary_date(value, finnish=finnish) == expected


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, "Example manufacturer · Example model"),
        ({CONF_MODEL: None}, "Example manufacturer"),
        ({CONF_MANUFACTURER: None}, "Example model"),
        (
            {CONF_MANUFACTURER: None, CONF_MODEL: None},
            "Tool",
        ),
        (
            {CONF_MANUFACTURER: None, CONF_MODEL: None, CONF_CATEGORY: None},
            "No manufacturer or model recorded",
        ),
    ],
)
async def test_metadata_summary_falls_back_in_order(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    fields: dict,
    expected: str,
) -> None:
    """Manufacturer and model first, then category, then an honest blank."""
    manager = _manager(hass, _asset_with(asset_store_data, **fields))
    flow = await _flow(hass, manager)

    assert flow._summary_metadata(manager.asset(ASSET_UUID)) == expected


async def test_metadata_summary_shortens_long_values(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Two long values still leave a readable line."""
    manager = _manager(
        hass,
        _asset_with(
            asset_store_data,
            **{
                CONF_MANUFACTURER: "Manufacturer with an extremely long name",
                CONF_MODEL: "Model with an equally excessive designation",
            },
        ),
    )
    flow = await _flow(hass, manager)

    summary = flow._summary_metadata(manager.asset(ASSET_UUID))

    assert len(summary) <= SUMMARY_MAX_LENGTH
    assert "·" in summary


async def test_purchase_and_warranty_are_summarized_independently(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """The warranty is the Asset's own, whichever Purchase sits beside it."""
    manager = _manager(hass, asset_store_data)
    flow = await _flow(hass, manager)

    linked = flow._summary_purchase_warranty(manager.asset(ASSET_UUID))

    assert linked == (
        "Purchase: Workshop equipment · Warranty: until 15 Jan 2028"
    )

    # Clearing the Purchase must not touch the warranty half of the line.
    await manager.async_set_asset_purchase(ASSET_UUID, None)
    unlinked = flow._summary_purchase_warranty(manager.asset(ASSET_UUID))

    assert unlinked == "Purchase: Not linked · Warranty: until 15 Jan 2028"
    assert manager.asset(ASSET_UUID)["warranty"]["until"] == "2028-01-15"


@pytest.mark.parametrize(
    ("warranty", "expected"),
    [
        ({"type": WARRANTY_TWO_YEARS, "until": "2028-01-15"}, "until 15 Jan 2028"),
        ({"type": WARRANTY_TWO_YEARS, "until": None}, "Unknown"),
        ({"type": WARRANTY_NONE, "until": None}, "None"),
        ({}, "None"),
    ],
)
async def test_warranty_half_covers_every_recorded_shape(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    warranty: dict,
    expected: str,
) -> None:
    """Whatever is recorded, the warranty half says something true."""
    manager = _manager(hass, _asset_with(asset_store_data, warranty=warranty))
    flow = await _flow(hass, manager)

    assert flow._summary_warranty(manager.asset(ASSET_UUID)) == expected


async def test_purchase_warranty_summary_never_implies_provenance(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Nothing in the line claims the warranty came from the Purchase."""
    manager = _manager(hass, asset_store_data)
    flow = await _flow(hass, manager)

    summary = flow._summary_purchase_warranty(manager.asset(ASSET_UUID))

    assert summary.startswith("Purchase: ")
    assert " · Warranty: " in summary
    for implication in ("from", "via", "covered by", "included"):
        assert implication not in summary.casefold()


async def test_deployed_summary_shows_area_and_date_when_present(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    area_registry: ar.AreaRegistry,
) -> None:
    """Installed reads as where and since when, dropping whatever is missing."""
    workshop = area_registry.async_create("Workshop")
    manager = _manager(
        hass,
        _asset_with(
            asset_store_data,
            **{
                "deployment_state": DEPLOYMENT_STATE_DEPLOYED,
                CONF_HA_AREA_ID: workshop.id,
                CONF_INSTALLED_DATE: "2026-08-15",
            },
        ),
    )
    flow = await _flow(hass, manager)
    asset = manager.asset(ASSET_UUID)

    assert flow._summary_deployment(asset) == (
        "Installed · Workshop · 15 Aug 2026"
    )

    without_date = {**asset, CONF_INSTALLED_DATE: None}
    assert flow._summary_deployment(without_date) == "Installed · Workshop"

    without_area = {**asset, CONF_HA_AREA_ID: None}
    assert flow._summary_deployment(without_area) == "Installed · 15 Aug 2026"

    bare = {**asset, CONF_HA_AREA_ID: None, CONF_INSTALLED_DATE: None}
    assert flow._summary_deployment(bare) == "Installed"


async def test_deployed_summary_handles_a_deleted_area(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A vanished Area reads as unavailable, never as its registry ID."""
    manager = _manager(
        hass,
        _asset_with(
            asset_store_data,
            **{
                "deployment_state": DEPLOYMENT_STATE_DEPLOYED,
                CONF_HA_AREA_ID: "deleted-area",
                CONF_INSTALLED_DATE: None,
            },
        ),
    )
    flow = await _flow(hass, manager)

    summary = flow._summary_deployment(manager.asset(ASSET_UUID))

    assert summary == "Installed · Unavailable Home Assistant Area"
    assert "deleted-area" not in summary
    # Rendering does not repair the stored reference.
    assert manager.asset(ASSET_UUID)[CONF_HA_AREA_ID] == "deleted-area"


async def test_not_installed_summary_is_just_that(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    area_registry: ar.AreaRegistry,
) -> None:
    """Not installed carries no location, and says so when the data disagrees."""
    manager = _manager(
        hass,
        _asset_with(
            asset_store_data,
            **{
                "deployment_state": DEPLOYMENT_STATE_NOT_DEPLOYED,
                CONF_HA_AREA_ID: None,
            },
        ),
    )
    flow = await _flow(hass, manager)

    assert flow._summary_deployment(manager.asset(ASSET_UUID)) == "Not installed"

    storage = area_registry.async_create("Storage")
    inconsistent = {
        **manager.asset(ASSET_UUID),
        CONF_HA_AREA_ID: storage.id,
    }

    summary = flow._summary_deployment(inconsistent)

    assert summary == "Not installed · inconsistent location data"
    assert "Storage" not in summary
    assert storage.id not in summary


async def test_unknown_deployment_summary_is_localized(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """The migrated unknown state reads as a sentence, not as an enum."""
    manager = _manager(
        hass,
        _asset_with(asset_store_data, deployment_state=DEPLOYMENT_STATE_UNKNOWN),
    )
    english = await _flow(hass, manager, "en")
    assert english._summary_deployment(manager.asset(ASSET_UUID)) == (
        "Installation status unknown"
    )

    finnish = await _flow(hass, manager, "fi")
    assert finnish._summary_deployment(manager.asset(ASSET_UUID)) == (
        "Asennustila ei tiedossa"
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (LIFECYCLE_STATUS_ACTIVE, "Active"),
        (LIFECYCLE_STATUS_RETIRED, "Retired"),
        (LIFECYCLE_STATUS_DISPOSED, "Disposed"),
        (LIFECYCLE_STATUS_LOST, "Lost"),
    ],
)
async def test_lifecycle_summary_names_the_status_and_its_date(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    status: str,
    expected: str,
) -> None:
    """Each status reads as words, followed by when it took effect."""
    manager = _manager(hass, asset_store_data)
    await manager.async_set_asset_lifecycle(
        ASSET_UUID,
        status,
        effective_date="2026-09-12",
        notes=None,
    )
    flow = await _flow(hass, manager)

    assert flow._summary_lifecycle(manager.asset(ASSET_UUID)) == (
        f"{expected} · 12 Sep 2026"
    )


async def test_lifecycle_summary_is_a_status_not_a_history(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Only the current position shows, however long the record gets."""
    manager = _manager(hass, asset_store_data)
    flow = await _flow(hass, manager)

    assert flow._summary_lifecycle(manager.asset(ASSET_UUID)) == "Unknown"

    for status, date in (
        (LIFECYCLE_STATUS_ACTIVE, "2026-01-20"),
        (LIFECYCLE_STATUS_RETIRED, "2026-06-01"),
        (LIFECYCLE_STATUS_DISPOSED, "2026-09-20"),
    ):
        await manager.async_set_asset_lifecycle(
            ASSET_UUID, status, effective_date=date, notes=None
        )

    summary = flow._summary_lifecycle(manager.asset(ASSET_UUID))

    assert summary == "Disposed · 20 Sep 2026"
    assert len(manager.lifecycle_events_for_asset(ASSET_UUID)) == 3
    assert "Active" not in summary
    assert "Retired" not in summary


async def test_replacement_summary_shows_only_active_relationships(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A voided record stops being current, and the summary follows."""
    manager = _manager(hass, asset_store_data)
    successor = await manager.async_create_manual_asset(name="Successor unit")
    flow = await _flow(hass, manager)

    assert flow._summary_replacement(manager.asset(ASSET_UUID)) == (
        "No active replacement"
    )

    record = await manager.async_create_asset_replacement(
        ASSET_UUID,
        successor["asset_uuid"],
        reason="failure",
        effective_date=None,
        notes=None,
    )
    replaced = flow._summary_replacement(manager.asset(ASSET_UUID))

    assert replaced == f"Replaced by: Successor unit · {successor['asset_id']}"
    assert record["replacement_uuid"] not in replaced
    assert successor["asset_uuid"] not in replaced

    from_successor = flow._summary_replacement(manager.asset(successor["asset_uuid"]))
    assert from_successor == "Replaces: Workshop device · DL0007"

    await manager.async_void_asset_replacement(
        record["replacement_uuid"],
        void_reason="Recorded in error",
    )

    assert flow._summary_replacement(manager.asset(ASSET_UUID)) == (
        "No active replacement"
    )


async def test_ha_devices_summary_names_the_primary_and_counts_the_rest(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """One name plus a count, never a list and never an ID."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Linked")
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset["asset_uuid"]})

    assert flow._summary_ha_devices(manager.asset(asset["asset_uuid"])) == (
        "Primary: None · Related: 0"
    )

    _owner, primary = _external_device(
        hass,
        device_registry,
        key="summary-primary",
        name="Bathroom thermostat",
    )
    await manager.async_link_asset_device(
        asset["asset_uuid"], primary.id, device=primary
    )

    assert flow._summary_ha_devices(manager.asset(asset["asset_uuid"])) == (
        "Primary: Bathroom thermostat · Related: 0"
    )

    for index in range(2):
        _related_owner, related = _external_device(
            hass,
            device_registry,
            key=f"summary-related-{index}",
            name=f"Related {index}",
        )
        await manager.async_add_related_device(asset["asset_uuid"], related.id)

    summary = flow._summary_ha_devices(manager.asset(asset["asset_uuid"]))

    assert summary == "Primary: Bathroom thermostat · Related: 2"
    assert primary.id not in summary


async def test_ha_devices_summary_counts_stale_references_too(
    hass: HomeAssistant,
) -> None:
    """A broken primary reads as unavailable, and stale related ones still count."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Broken links")
    await manager.async_link_asset_device(asset["asset_uuid"], "gone-primary")
    for stale in ("gone-a", "gone-b"):
        await manager.async_add_related_device(asset["asset_uuid"], stale)
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset["asset_uuid"]})

    summary = flow._summary_ha_devices(manager.asset(asset["asset_uuid"]))

    assert summary == (
        "Primary: Home Assistant device unavailable · Related: 2"
    )
    for stale in ("gone-primary", "gone-a", "gone-b"):
        assert stale not in summary


async def test_every_summary_is_localized_in_finnish(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Finnish readers get Finnish, and never a canonical enum value."""
    manager = _manager(
        hass,
        _asset_with(
            asset_store_data,
            **{
                "deployment_state": DEPLOYMENT_STATE_NOT_DEPLOYED,
                CONF_HA_AREA_ID: None,
            },
        ),
    )
    await manager.async_set_asset_lifecycle(
        ASSET_UUID,
        LIFECYCLE_STATUS_RETIRED,
        effective_date="2026-09-12",
        notes=None,
    )
    flow = await _flow(hass, manager, "fi")
    asset = manager.asset(ASSET_UUID)

    summaries = {
        "metadata": flow._summary_metadata(asset),
        "purchase_warranty": flow._summary_purchase_warranty(asset),
        "deployment": flow._summary_deployment(asset),
        "lifecycle": flow._summary_lifecycle(asset),
        "replacement": flow._summary_replacement(asset),
        "ha_devices": flow._summary_ha_devices(asset),
    }

    assert summaries["purchase_warranty"].startswith("Ostos: ")
    assert "Takuu: 15.1.2028 asti" in summaries["purchase_warranty"]
    assert summaries["deployment"] == "Ei asennettu"
    assert summaries["lifecycle"] == "Käytöstä poistettu · 12.9.2026"
    assert summaries["replacement"] == "Ei aktiivista korvaussuhdetta"
    assert summaries["ha_devices"].startswith("Ensisijainen: ")

    joined = " ".join(summaries.values())
    for raw in RAW_CANONICAL_VALUES:
        assert raw not in joined


async def test_the_hub_renders_every_summary_and_writes_nothing(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """All six reach the hub, and building them changes no stored data."""
    manager = _manager(hass, asset_store_data)
    flow = await _flow(hass, manager)
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        hub = await flow.async_step_manage_asset_menu()

    placeholders = hub["description_placeholders"]
    for key in (
        "metadata",
        "purchase_warranty",
        "deployment",
        "lifecycle",
        "replacement",
        "ha_devices",
    ):
        assert placeholders[key], key
        assert len(placeholders[key]) <= SUMMARY_MAX_LENGTH, key
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


def _hub_step(language: str) -> dict:
    """Return the translated Asset hub step for one language."""
    path = (
        Path(__file__).parents[1]
        / "custom_components"
        / "device_lifecycle"
        / "translations"
        / f"{language}.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))["options"]["step"][
        "manage_asset_menu"
    ]


@pytest.mark.parametrize("language", ["en", "fi"])
def test_each_hub_row_carries_its_own_summary(language: str) -> None:
    """Every action row describes itself with its own summary placeholder.

    This is Home Assistant's `menu_option_descriptions` structure, the same
    one core integrations use to put a line under a menu row, so each
    summary belongs to the row it is about rather than to the step.
    """
    step = _hub_step(language)

    assert step["menu_option_descriptions"] == {
        "edit_asset_metadata": "{metadata}",
        "change_asset_purchase": "{purchase_warranty}",
        "asset_deployment": "{deployment}",
        "asset_lifecycle": "{lifecycle}",
        "asset_replacement": "{replacement}",
        "ha_relationship": "{ha_devices}",
    }


@pytest.mark.parametrize("language", ["en", "fi"])
def test_only_the_six_action_rows_are_described(language: str) -> None:
    """Choosing another Asset is navigation, so it summarizes nothing."""
    step = _hub_step(language)

    assert len(step["menu_options"]) == 7
    assert set(step["menu_option_descriptions"]) == (
        set(step["menu_options"]) - {"manage_asset"}
    )


@pytest.mark.parametrize("language", ["en", "fi"])
def test_the_hub_step_description_stays_general(language: str) -> None:
    """The step itself names the Asset; the rows carry the detail.

    A summary shown in both places would be read twice.
    """
    description = _hub_step(language)["description"]

    assert "{asset}" in description
    for key in (
        "metadata",
        "purchase_warranty",
        "deployment",
        "lifecycle",
        "replacement",
        "ha_devices",
    ):
        assert f"{{{key}}}" not in description, (language, key)


async def test_summaries_never_expose_technical_identifiers(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    area_registry: ar.AreaRegistry,
) -> None:
    """No UUID, device ID, Area ID or replacement UUID reaches the hub."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Fully wired")
    successor = await manager.async_create_manual_asset(name="Successor")
    workshop = area_registry.async_create("Workshop")
    _owner, device = _external_device(
        hass,
        device_registry,
        key="summary-identifiers",
        name="Bench controller",
    )
    await manager.async_link_asset_device(
        asset["asset_uuid"], device.id, device=device
    )
    await manager.async_set_asset_deployment(
        asset["asset_uuid"],
        deployment_state=DEPLOYMENT_STATE_DEPLOYED,
        ha_area_id=workshop.id,
    )
    record = await manager.async_create_asset_replacement(
        asset["asset_uuid"],
        successor["asset_uuid"],
        reason="failure",
        effective_date=None,
        notes=None,
    )
    flow, _entry = _options_flow(hass, manager)
    hub = await flow.async_step_manage_asset({CONF_ASSET_UUID: asset["asset_uuid"]})

    rendered = " ".join(hub["description_placeholders"].values())

    assert asset["asset_id"] in rendered
    assert successor["asset_id"] in rendered
    for identifier in (
        asset["asset_uuid"],
        successor["asset_uuid"],
        device.id,
        workshop.id,
        record["replacement_uuid"],
    ):
        assert identifier not in rendered
