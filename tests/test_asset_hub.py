"""The Asset hub: one selected Asset, its summaries, and what can be done.

The hub is the read-only centre of Asset management. Everything here is
about what a person sees and where a row takes them; the mutations behind
those rows keep their own tests.
"""

from __future__ import annotations

from copy import deepcopy

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr

from custom_components.device_lifecycle.config_flow import _asset_label
from custom_components.device_lifecycle.const import (
    CONF_ASSET_NAME,
    CONF_ASSET_UUID,
    CONF_MANUFACTURER,
    DEPLOYMENT_STATE_DEPLOYED,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import ASSET_UUID, capture_reloads
from .test_ha_relationship_options_flow import _external_device
from .test_options_flow import (
    _identical_metadata_input,
    _manager,
    _options_flow,
)

HUB_ROWS = [
    "asset_details_warranty_menu",
    "asset_installation_menu",
    "asset_lifecycle_replacement_menu",
    "ha_relationship",
    "manage_asset",
]

SUMMARY_PLACEHOLDERS = [
    "details_warranty",
    "deployment",
    "lifecycle_replacement",
    "ha_devices",
]


async def _hub(
    hass: HomeAssistant,
    manager: AssetStoreManager,
    asset_uuid: str = ASSET_UUID,
):
    """Return a flow positioned on one Asset's hub, and that hub's result."""
    flow, _entry = _options_flow(hass, manager)
    result = await flow.async_step_manage_asset({CONF_ASSET_UUID: asset_uuid})
    return flow, result


async def test_selecting_an_asset_opens_its_hub(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """The picker hands the hub one Asset, by UUID, shown by name and ID."""
    manager = _manager(hass, asset_store_data)

    flow, hub = await _hub(hass, manager)

    assert hub["type"] is FlowResultType.MENU
    assert hub["step_id"] == "manage_asset_menu"
    assert flow._selected_asset_uuid == ASSET_UUID
    assert hub["description_placeholders"]["asset"] == "Workshop device · DL0007"


async def test_hub_offers_exactly_the_five_rows_in_order(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """The hub's shape is fixed: four sections, then choosing another Asset."""
    manager = _manager(hass, asset_store_data)

    _flow, hub = await _hub(hass, manager)

    assert hub["menu_options"] == HUB_ROWS


@pytest.mark.parametrize(
    ("row", "step_id", "result_type"),
    [
        (
            "asset_details_warranty_menu",
            "asset_details_warranty_menu",
            FlowResultType.MENU,
        ),
        ("asset_installation_menu", "asset_installation_menu", FlowResultType.MENU),
        (
            "asset_lifecycle_replacement_menu",
            "asset_lifecycle_replacement_menu",
            FlowResultType.MENU,
        ),
        ("ha_relationship", "ha_relationship", FlowResultType.MENU),
        ("manage_asset", "manage_asset", FlowResultType.FORM),
    ],
)
async def test_every_hub_row_opens_its_own_step(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    row: str,
    step_id: str,
    result_type: FlowResultType,
) -> None:
    """Every section opens a menu; only choosing another Asset is a form."""
    manager = _manager(hass, asset_store_data)
    flow, _opened = await _hub(hass, manager)

    result = await getattr(flow, f"async_step_{row}")()

    assert result["type"] is result_type
    assert result["step_id"] == step_id


async def test_opening_the_hub_writes_nothing(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Looking at an Asset is not a change to it."""
    manager = _manager(hass, asset_store_data)
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: ASSET_UUID})
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        first = await flow.async_step_manage_asset_menu()
        second = await flow.async_step_manage_asset_menu()

    assert first["type"] is second["type"] is FlowResultType.MENU
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


async def test_choosing_another_device_switches_the_hub_context(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """The last row returns to the picker and lands on the chosen Asset."""
    manager = _manager(hass, asset_store_data)
    other = await manager.async_create_manual_asset(name="Second device")
    flow, first_hub = await _hub(hass, manager)
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        picker = await flow.async_step_manage_asset()

        # Navigating to the picker is not yet a choice: nothing has changed.
        assert picker["type"] is FlowResultType.FORM
        assert picker["step_id"] == "manage_asset"
        assert manager._data == before
        manager._store.async_save.assert_not_awaited()
        reload.assert_not_called()

        second_hub = await flow.async_step_manage_asset(
            {CONF_ASSET_UUID: other["asset_uuid"]}
        )

    assert first_hub["description_placeholders"]["asset"] == (
        "Workshop device · DL0007"
    )
    assert second_hub["type"] is FlowResultType.MENU
    assert flow._selected_asset_uuid == other["asset_uuid"]
    assert second_hub["description_placeholders"]["asset"] == (
        f"Second device · {other['asset_id']}"
    )
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


async def test_hub_carries_a_summary_for_every_action(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Each action area has its own placeholder, fed by its own helper."""
    manager = _manager(hass, asset_store_data)

    _flow, hub = await _hub(hass, manager)

    placeholders = hub["description_placeholders"]
    assert set(SUMMARY_PLACEHOLDERS) <= set(placeholders)
    assert all(isinstance(placeholders[key], str) for key in SUMMARY_PLACEHOLDERS)


async def test_summaries_read_the_selected_asset(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
    freezer,
) -> None:
    """The summaries describe this Asset, not a default or a neighbour."""
    freezer.move_to("2026-09-24 12:00:00+00:00")
    data = deepcopy(asset_store_data)
    manager = _manager(hass, data)
    area = hass.data["area_registry"].async_get_or_create("Workshop")
    await manager.async_set_asset_deployment(
        ASSET_UUID,
        deployment_state=DEPLOYMENT_STATE_DEPLOYED,
        ha_area_id=area.id,
    )

    _flow, hub = await _hub(hass, manager)

    placeholders = hub["description_placeholders"]
    asset = manager.asset(ASSET_UUID)
    assert placeholders["details_warranty"].startswith(
        f"Warranty until 15 Jan 2028 · {asset[CONF_MANUFACTURER]}"
    )
    assert "Workshop" in placeholders["deployment"]


async def test_hub_never_shows_technical_identifiers(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Asset UUIDs, HA device IDs and Area IDs stay behind the scenes."""
    manager = _manager(hass, asset_store_data)
    asset = await manager.async_create_manual_asset(name="Bench asset")
    area = hass.data["area_registry"].async_get_or_create("Workshop")
    await manager.async_set_asset_deployment(
        asset["asset_uuid"], ha_area_id=area.id
    )
    _owner, device = _external_device(
        hass,
        device_registry,
        key="hub-identifiers",
        name="Bench controller",
    )
    await manager.async_link_asset_device(
        asset["asset_uuid"], device.id, device=device
    )

    _flow, hub = await _hub(hass, manager, asset["asset_uuid"])

    rendered = " ".join(hub["description_placeholders"].values())
    assert "Bench controller" in rendered
    assert asset["asset_uuid"] not in rendered
    assert device.id not in rendered
    assert area.id not in rendered


async def test_a_result_is_reported_once_where_the_operation_returns(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A completed operation is reported in its section, then never again."""
    manager = _manager(hass, asset_store_data)
    flow, hub = await _hub(hass, manager)

    assert hub["description_placeholders"]["result"] == ""

    completed = await flow.async_step_edit_asset_metadata(
        {CONF_ASSET_NAME: "Renamed workshop device"}
    )

    assert completed["type"] is FlowResultType.MENU
    assert completed["step_id"] == "asset_details_warranty_menu"
    assert completed["description_placeholders"]["result"] == "Asset details updated."

    reopened = await flow.async_step_asset_details_warranty_menu()
    hub = await flow.async_step_manage_asset_menu()

    assert reopened["description_placeholders"]["result"] == ""
    assert hub["description_placeholders"]["result"] == ""


async def test_choosing_another_device_drops_the_previous_result(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A result belongs to the Asset it happened on."""
    manager = _manager(hass, asset_store_data)
    other = await manager.async_create_manual_asset(name="Unrelated device")
    flow, _opened = await _hub(hass, manager)

    completed = await flow.async_step_edit_asset_metadata(
        {CONF_ASSET_NAME: "Renamed workshop device"}
    )
    assert completed["description_placeholders"]["result"] == "Asset details updated."

    # Without an intervening render, so the result is still pending.
    flow._last_result = "asset_updated"
    switched = await flow.async_step_manage_asset(
        {CONF_ASSET_UUID: other["asset_uuid"]}
    )

    assert switched["description_placeholders"]["result"] == ""


async def test_a_no_op_operation_still_returns_to_its_section(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A canonical no-op reloads nothing and keeps the same Asset selected."""
    manager = _manager(hass, asset_store_data)
    flow, _opened = await _hub(hass, manager)
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        result = await flow.async_step_edit_asset_metadata(
            _identical_metadata_input(manager.asset(ASSET_UUID))
        )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "asset_details_warranty_menu"
    assert flow._selected_asset_uuid == ASSET_UUID
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


async def test_asset_labels_are_name_first_and_sorted_by_name(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Selections read as a name, ordered by name, with the ID for identity."""
    data = deepcopy(asset_store_data)
    manager = _manager(hass, data)
    for name in ("banana probe", "Apple sensor", "Banana probe"):
        await manager.async_create_manual_asset(name=name)
    flow, _entry = _options_flow(hass, manager)

    form = await flow.async_step_manage_asset()
    options = form["data_schema"].schema[CONF_ASSET_UUID].config["options"]

    labels = [option["label"] for option in options]
    assert labels == [
        "Apple sensor · DL0009",
        "banana probe · DL0008",
        "Banana probe · DL0010",
        "Workshop device · DL0007",
    ]
    # The value stays the immutable identity, and it is never in the label.
    assert all(option["value"] not in option["label"] for option in options)
    assert {option["value"] for option in options} == set(manager._data["assets"])


def test_asset_label_puts_the_name_before_the_asset_id() -> None:
    """The label helper itself is the single source of that format."""
    label = _asset_label({"asset_id": "DL0032", "name": "Eelin tietokone"})

    assert label == "Eelin tietokone · DL0032"


async def test_hub_aborts_cleanly_when_the_asset_is_gone(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A deleted Asset falls back to the picker rather than rendering nothing."""
    manager = _manager(hass, asset_store_data)
    flow, _opened = await _hub(hass, manager)
    manager._data["assets"].clear()

    result = await flow.async_step_manage_asset_menu()

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manage_asset"
    assert result["errors"] == {"base": "asset_missing"}


async def test_hub_summary_names_the_linked_devices(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Linked Home Assistant devices are listed by the names people gave them."""
    manager = _manager(hass, asset_store_data)
    _primary_owner, primary = _external_device(
        hass,
        device_registry,
        key="hub-primary",
        name="Bench controller",
    )
    _related_owner, related = _external_device(
        hass,
        device_registry,
        key="hub-related",
        name="Bench power meter",
    )
    asset = await manager.async_create_manual_asset(name="Bench asset")
    await manager.async_link_asset_device(
        asset["asset_uuid"], primary.id, device=primary
    )
    await manager.async_add_related_device(asset["asset_uuid"], related.id)

    _flow, hub = await _hub(hass, manager, asset["asset_uuid"])

    assert hub["description_placeholders"]["ha_devices"] == (
        "Primary: Bench controller · Related: 1"
    )
