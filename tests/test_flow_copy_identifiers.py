"""Nothing in ordinary flow copy shows a registry ID to the person reading it.

Home Assistant device IDs, Area IDs and Asset UUIDs are how the integration
finds things, not how anybody recognizes them. The DLxxxx Asset ID is the
one identifier people are meant to see. Selector values are unaffected: a
removal still targets the exact stored reference, it just stops printing it.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr

from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_DEVICE_ID,
    CONF_HA_AREA_ID,
    DEPLOYMENT_STATE_DEPLOYED,
)
from custom_components.device_lifecycle.storage import AssetStoreManager

from .test_ha_relationship_options_flow import _external_device, _schema_validator
from .test_options_flow import _manager, _options_flow

STALE_DEVICE_IDS = ("gone-first", "gone-second", "gone-third")


async def _on_asset(
    hass: HomeAssistant,
    manager: AssetStoreManager,
    asset_uuid: str,
):
    """Return a flow sitting on one Asset's hub."""
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset_uuid})
    return flow


async def test_primary_device_is_named_not_identified(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """The primary relationship reads as the device's own name."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Named primary")
    _owner, device = _external_device(
        hass,
        device_registry,
        key="copy-primary",
        name="Kitchen socket",
    )
    await manager.async_link_asset_device(asset["asset_uuid"], device.id)
    flow = await _on_asset(hass, manager, asset["asset_uuid"])

    submenu = await flow.async_step_ha_relationship()
    form = await flow.async_step_manage_primary_device()

    assert submenu["description_placeholders"]["current_primary"] == (
        "Kitchen socket"
    )
    assert form["description_placeholders"]["current_device"] == "Kitchen socket"
    assert device.id not in " ".join(submenu["description_placeholders"].values())
    assert device.id not in " ".join(form["description_placeholders"].values())


async def test_related_devices_are_named_not_identified(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Related relationships read as names, and removal still targets IDs."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Named related")
    devices = []
    for index, name in enumerate(("Hallway sensor", "Porch light")):
        _owner, device = _external_device(
            hass,
            device_registry,
            key=f"copy-related-{index}",
            name=name,
        )
        await manager.async_add_related_device(asset["asset_uuid"], device.id)
        devices.append(device)
    flow = await _on_asset(hass, manager, asset["asset_uuid"])

    submenu = await flow.async_step_ha_relationship()
    form = await flow.async_step_remove_related_device()
    options = list(_schema_validator(form, CONF_DEVICE_ID).config["options"])

    related_text = submenu["description_placeholders"]["related_devices"]
    assert related_text == "Hallway sensor; Porch light"
    for device in devices:
        assert device.id not in related_text
    assert [option["label"] for option in options] == [
        "Hallway sensor",
        "Porch light",
    ]
    # Label is for people, value is for the removal itself.
    assert [option["value"] for option in options] == [
        device.id for device in devices
    ]


async def test_a_single_stale_reference_reads_as_unavailable(
    hass: HomeAssistant,
) -> None:
    """One broken reference needs no number to be understood."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="One stale")
    await manager.async_add_related_device(asset["asset_uuid"], "gone-first")
    flow = await _on_asset(hass, manager, asset["asset_uuid"])

    submenu = await flow.async_step_ha_relationship()
    form = await flow.async_step_remove_related_device()
    options = list(_schema_validator(form, CONF_DEVICE_ID).config["options"])

    assert submenu["description_placeholders"]["related_devices"] == (
        "Home Assistant device unavailable"
    )
    assert "gone-first" not in submenu["description_placeholders"][
        "related_devices"
    ]
    assert options == [
        {"value": "gone-first", "label": "Home Assistant device unavailable"}
    ]


async def test_several_stale_references_stay_distinguishable(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Numbering replaces the registry ID as the way to tell them apart."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Many stale")
    _owner, live = _external_device(
        hass,
        device_registry,
        key="copy-live",
        name="Bench meter",
    )
    await manager.async_add_related_device(asset["asset_uuid"], STALE_DEVICE_IDS[0])
    await manager.async_add_related_device(asset["asset_uuid"], live.id)
    await manager.async_add_related_device(asset["asset_uuid"], STALE_DEVICE_IDS[1])
    await manager.async_add_related_device(asset["asset_uuid"], STALE_DEVICE_IDS[2])
    flow = await _on_asset(hass, manager, asset["asset_uuid"])

    submenu = await flow.async_step_ha_relationship()
    form = await flow.async_step_remove_related_device()
    options = list(_schema_validator(form, CONF_DEVICE_ID).config["options"])

    labels = [option["label"] for option in options]
    assert labels == [
        "Home Assistant device unavailable 1",
        "Bench meter",
        "Home Assistant device unavailable 2",
        "Home Assistant device unavailable 3",
    ]
    assert len(set(labels)) == len(labels)
    # The overview describes them with the same numbers the picker offers.
    assert submenu["description_placeholders"]["related_devices"] == (
        "; ".join(labels)
    )
    assert [option["value"] for option in options] == [
        STALE_DEVICE_IDS[0],
        live.id,
        STALE_DEVICE_IDS[1],
        STALE_DEVICE_IDS[2],
    ]
    rendered = " ".join(submenu["description_placeholders"].values()) + " ".join(
        labels
    )
    for stale_id in STALE_DEVICE_IDS:
        assert stale_id not in rendered


async def test_removing_a_numbered_stale_reference_removes_that_exact_one(
    hass: HomeAssistant,
) -> None:
    """The value behind a friendly label still points at one stored reference."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Remove stale")
    for stale_id in STALE_DEVICE_IDS[:2]:
        await manager.async_add_related_device(asset["asset_uuid"], stale_id)
    flow = await _on_asset(hass, manager, asset["asset_uuid"])
    form = await flow.async_step_remove_related_device()
    options = list(_schema_validator(form, CONF_DEVICE_ID).config["options"])
    second = next(
        option
        for option in options
        if option["label"] == "Home Assistant device unavailable 2"
    )

    removed = await flow.async_step_remove_related_device(
        {CONF_DEVICE_ID: second["value"]}
    )

    assert removed["step_id"] == "ha_relationship"
    assert manager.asset(asset["asset_uuid"])["ha_device_refs"] == [
        {"device_id": STALE_DEVICE_IDS[0], "role": "related"}
    ]


async def test_area_context_is_named_not_identified(
    hass: HomeAssistant,
    area_registry: ar.AreaRegistry,
) -> None:
    """Deployment copy names the Area, and a stale one reads as unavailable."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Placed unit")
    workshop = area_registry.async_create("Workshop")
    await manager.async_set_asset_deployment(
        asset["asset_uuid"],
        deployment_state=DEPLOYMENT_STATE_DEPLOYED,
        ha_area_id=workshop.id,
    )
    flow = await _on_asset(hass, manager, asset["asset_uuid"])

    placed = await flow.async_step_asset_deployment()

    assert placed["description_placeholders"]["current_area"] == "Workshop"
    assert workshop.id not in " ".join(placed["description_placeholders"].values())

    manager._data["assets"][asset["asset_uuid"]][CONF_HA_AREA_ID] = "gone-area"
    stale_flow = await _on_asset(hass, manager, asset["asset_uuid"])
    stale = await stale_flow.async_step_asset_deployment()

    assert stale["description_placeholders"]["current_area"] == (
        "Unavailable Home Assistant Area"
    )
    assert "gone-area" not in " ".join(stale["description_placeholders"].values())
    assert manager.asset(asset["asset_uuid"])[CONF_HA_AREA_ID] == "gone-area"


async def test_asset_uuid_never_reaches_any_of_this_copy(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """The Asset's own UUID stays behind the DLxxxx identity everywhere."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Hidden identity")
    _owner, device = _external_device(
        hass,
        device_registry,
        key="copy-uuid",
        name="Bench controller",
    )
    await manager.async_link_asset_device(asset["asset_uuid"], device.id)
    flow = await _on_asset(hass, manager, asset["asset_uuid"])

    rendered = []
    for step in (
        "manage_asset_menu",
        "ha_relationship",
        "asset_replacement",
        "asset_deployment",
        "manage_primary_device",
    ):
        result = await getattr(flow, f"async_step_{step}")()
        rendered.extend((result.get("description_placeholders") or {}).values())

    joined = " ".join(rendered)
    assert asset["asset_id"] in joined
    assert asset["asset_uuid"] not in joined
    assert device.id not in joined
