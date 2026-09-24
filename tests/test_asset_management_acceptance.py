"""0.7.4 acceptance: whole Asset-management journeys through real reloads.

The per-feature tests pin each contract precisely, mostly against a
test-owned manager. What they cannot show is that the contracts still hold
together when one person walks through several of them in one sitting and
every mutation tears the entry down and sets it up again. These journeys run
through Home Assistant's own OptionsFlow manager with genuine reloads, so the
manager behind the flow is replaced after every change, and they assert what
the person would see at each stop rather than re-deriving helper outputs.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr

from custom_components.device_lifecycle.config_flow import NO_PURCHASE_SELECTION
from custom_components.device_lifecycle.const import (
    CONF_ASSET_NAME,
    CONF_ASSET_UUID,
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
    CONF_PURCHASE_NAME,
    CONF_PURCHASE_UUID,
    CONF_REPLACEMENT_REASON,
    CONF_REPLACEMENT_TARGET_ASSET_UUID,
    SUBENTRY_TYPE_PURCHASE,
)
from custom_components.device_lifecycle.models import AssetStoreData

from .conftest import ASSET_UUID, DEVICE_ID, PURCHASE_UUID
from .test_exposure_options_reload import (
    _setup_loaded_entry,
    _start_asset_action,
    _verified_store_readback,
)
from .test_ha_relationship_options_flow import _external_device
from .test_options_flow import _identical_metadata_input
from .test_purchase_reconciliation import (
    SECOND_PURCHASE_UUID,
    _data_with_second_purchase,
)

pytestmark = pytest.mark.real_reload

HUB = "manage_asset_menu"
REPLACEMENT = "asset_replacement"
HA_DEVICES = "ha_relationship"


def _purchase_subentries(
    purchase_subentry_data: dict[str, Any],
    device_id: str,
) -> tuple[dict[str, object], ...]:
    """Return the legacy Purchase and a second, device-free one to relink to.

    The legacy subentry keeps listing the Asset's primary HA device, because
    no flow rewrites `device_ids`. Every reload therefore replays that stale
    projection against whatever the person chose.
    """
    legacy = deepcopy(purchase_subentry_data)
    legacy[CONF_DEVICE_IDS] = [device_id]
    second = deepcopy(purchase_subentry_data)
    second.update(
        {
            CONF_PURCHASE_UUID: SECOND_PURCHASE_UUID,
            CONF_PURCHASE_NAME: "Second purchase",
            CONF_DEVICE_IDS: [],
        }
    )
    return tuple(
        {
            "data": data,
            "subentry_type": SUBENTRY_TYPE_PURCHASE,
            "title": data[CONF_PURCHASE_NAME],
            "unique_id": None,
        }
        for data in (legacy, second)
    )


@pytest.mark.parametrize(
    ("selection", "linked_purchase"),
    [
        (SECOND_PURCHASE_UUID, SECOND_PURCHASE_UUID),
        (NO_PURCHASE_SELECTION, None),
    ],
    ids=["relinked", "cleared"],
)
async def test_a_user_purchase_choice_survives_the_reload_it_triggers(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
    selection: str,
    linked_purchase: str | None,
) -> None:
    """G2 and G1 together: the relink's own reload keeps the entry usable.

    Before 0.7.4 the reload that follows this save replayed the legacy
    Purchase projection during setup, which raised and left the whole entry
    in SETUP_ERROR. The G2 fix and the G1 reload criterion are pinned
    separately elsewhere; this is the one place the person's actual path
    runs through both, with a real unload/setup in between.

    The legacy HA device is a real Device Registry device, as it is for any
    Purchase created through the UI.
    """
    _owner, device = _external_device(
        hass,
        device_registry,
        key="legacy-purchase-device",
        name="Workshop device",
    )
    data = _data_with_second_purchase(asset_store_data)
    data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": device.id, "role": "primary"}
    ]
    with _verified_store_readback(hass_storage):
        entry = await _setup_loaded_entry(
            hass,
            hass_storage,
            data,
            subentries_data=_purchase_subentries(
                purchase_subentry_data,
                device.id,
            ),
        )
    original_manager = entry.runtime_data
    before = deepcopy(original_manager.asset(ASSET_UUID))
    assert before["purchase_uuid"] == PURCHASE_UUID
    assert before["field_sources"]["purchase_uuid"] == "purchase"

    flow_id = await _start_asset_action(
        hass,
        entry,
        ASSET_UUID,
        "change_asset_purchase",
    )

    with _verified_store_readback(hass_storage):
        section = await hass.config_entries.options.async_configure(
            flow_id,
            {CONF_PURCHASE_UUID: selection},
        )

        # The save's reload ran setup again, and setup did not fail on it.
        # (That the flow waits for it is pinned by the gated-setup test in
        # test_asset_hub_reload_continuity.py.)
        assert entry.state is ConfigEntryState.LOADED
        manager = entry.runtime_data
        assert manager is not original_manager

        # The save returns to Details & warranty, with its result once.
        assert section["type"] is FlowResultType.MENU
        assert section["step_id"] == "asset_details_warranty_menu"
        assert section["description_placeholders"]["result"] == (
            "Linked purchase updated."
        )

        # The person's choice is canonical in the manager setup rebuilt, and
        # the legacy projection neither moved it back nor re-added membership.
        asset = manager.asset(ASSET_UUID)
        assert asset["purchase_uuid"] == linked_purchase
        assert asset["field_sources"]["purchase_uuid"] == "user"
        assert ASSET_UUID not in manager.purchase(PURCHASE_UUID)["asset_uuids"]
        assert manager.purchase(SECOND_PURCHASE_UUID)["asset_uuids"] == (
            [ASSET_UUID] if linked_purchase == SECOND_PURCHASE_UUID else []
        )

        # A warranty is the Asset's own fact, not the Purchase's.
        assert asset["warranty"] == before["warranty"]
        assert asset["installed_date"] == before["installed_date"]
        assert asset["asset_id"] == before["asset_id"]

        # The person keeps working: the editor reopens from the section on
        # the new state.
        editor = await hass.config_entries.options.async_configure(
            flow_id,
            {"next_step_id": "change_asset_purchase"},
        )
        assert editor["type"] is FlowResultType.FORM
        schema = editor["data_schema"].schema
        default = next(
            key.default() for key in schema if key == CONF_PURCHASE_UUID
        )
        assert default == selection

        await hass.async_block_till_done()


def _rendered(result: dict[str, Any]) -> list[str]:
    """Return every string a flow result puts in front of the person."""
    shown = [
        str(value)
        for value in (result.get("description_placeholders") or {}).values()
    ]
    schema = result.get("data_schema")
    if schema is not None:
        for validator in schema.schema.values():
            config = getattr(validator, "config", None)
            if isinstance(config, dict):
                shown.extend(
                    str(option["label"])
                    for option in config.get("options", [])
                    if isinstance(option, dict)
                )
    return shown


async def test_a_finnish_management_session_end_to_end(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    """One person, one flow, three reloads, two Assets, all in Finnish.

    Walks hub → direct editor → Replacement submenu → HA devices submenu →
    another Asset, checking at every stop where the person landed, what the
    result line says, that it is said once, and that nothing on screen is a
    technical identifier or an internal key.
    """
    hass.config.language = "fi"
    with _verified_store_readback(hass_storage):
        entry = await _setup_loaded_entry(hass, hass_storage, asset_store_data)
        other = await entry.runtime_data.async_create_manual_asset(
            name="Vanha mittari"
        )
    _owner, device = _external_device(
        hass,
        device_registry,
        key="acceptance-journey",
        name="Pöytämittari",
    )
    managers = [entry.runtime_data]
    screens: list[dict[str, Any]] = []

    async def step(user_input: dict[str, Any]) -> dict[str, Any]:
        result = await hass.config_entries.options.async_configure(
            flow_id, user_input
        )
        screens.append(result)
        return result

    initial = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = initial["flow_id"]

    with _verified_store_readback(hass_storage):
        # Picker: name first, sorted by name, the immutable UUID as value.
        picker = await step({"next_step_id": "manage_asset"})
        options = picker["data_schema"].schema[CONF_ASSET_UUID].config["options"]
        assert [option["label"] for option in options] == [
            f"Vanha mittari · {other['asset_id']}",
            "Workshop device · DL0007",
        ]
        assert [option["value"] for option in options] == [
            other["asset_uuid"],
            ASSET_UUID,
        ]

        hub = await step({CONF_ASSET_UUID: ASSET_UUID})
        assert hub["step_id"] == HUB
        assert hub["description_placeholders"]["asset"] == (
            "Workshop device · DL0007"
        )
        assert hub["description_placeholders"]["result"] == ""

        # Section menu → editor → the same section, from the new manager.
        section = await step({"next_step_id": "asset_details_warranty_menu"})
        assert section["step_id"] == "asset_details_warranty_menu"
        assert section["menu_options"] == [
            "edit_asset_metadata",
            "change_asset_purchase",
            HUB,
        ]
        await step({"next_step_id": "edit_asset_metadata"})
        edit = _identical_metadata_input(managers[-1].asset(ASSET_UUID))
        edit[CONF_ASSET_NAME] = "Työpajan laite"
        section = await step(edit)
        managers.append(entry.runtime_data)
        assert section["step_id"] == "asset_details_warranty_menu"
        assert section["description_placeholders"]["asset"] == (
            "Työpajan laite · DL0007"
        )
        assert section["description_placeholders"]["result"] == (
            "Perustiedot päivitettiin."
        )
        # Back to the hub: the result stayed in the section it belongs to.
        hub = await step({"next_step_id": HUB})
        assert hub["step_id"] == HUB
        assert hub["description_placeholders"]["result"] == ""

        # Replacement submenu, reached through Lifecycle & replacement: the
        # operation stays in the submenu.
        await step({"next_step_id": "asset_lifecycle_replacement_menu"})
        submenu = await step({"next_step_id": "asset_replacement"})
        assert submenu["step_id"] == REPLACEMENT
        assert submenu["description_placeholders"]["result"] == ""
        await step({"next_step_id": "replacement_replaces"})
        submenu = await step(
            {
                CONF_REPLACEMENT_TARGET_ASSET_UUID: other["asset_uuid"],
                CONF_REPLACEMENT_REASON: "failure",
            }
        )
        managers.append(entry.runtime_data)
        assert submenu["step_id"] == REPLACEMENT
        assert submenu["description_placeholders"]["result"] == (
            "Korvaaminen päivitettiin."
        )

        # Back is navigation, to the section and then the hub: the result
        # stayed in the submenu it belongs to.
        section = await step({"next_step_id": "asset_lifecycle_replacement_menu"})
        assert section["description_placeholders"]["result"] == ""
        hub = await step({"next_step_id": HUB})
        assert hub["step_id"] == HUB
        assert hub["description_placeholders"]["result"] == ""
        assert hub["description_placeholders"]["lifecycle_replacement"] == (
            "Ei tiedossa · korvaa: Vanha mittari"
        )

        # HA devices submenu: same shape, its own result, its own Back.
        await step({"next_step_id": "ha_relationship"})
        await step({"next_step_id": "add_related_device"})
        submenu = await step({CONF_DEVICE_ID: device.id})
        managers.append(entry.runtime_data)
        assert submenu["step_id"] == HA_DEVICES
        assert submenu["description_placeholders"]["result"] == (
            "Liittyvä Home Assistant -laite lisättiin."
        )
        hub = await step({"next_step_id": HUB})
        assert hub["description_placeholders"]["result"] == ""

        # Another Asset: new context, no result carried over, and its own
        # summary sees the relationship recorded from the first Asset.
        await step({"next_step_id": "manage_asset"})
        other_hub = await step({CONF_ASSET_UUID: other["asset_uuid"]})
        assert other_hub["step_id"] == HUB
        assert other_hub["description_placeholders"]["asset"] == (
            f"Vanha mittari · {other['asset_id']}"
        )
        assert other_hub["description_placeholders"]["result"] == ""
        assert other_hub["description_placeholders"]["lifecycle_replacement"] == (
            "Aktiivinen · korvattu: Työpajan laite"
        )

        await hass.async_block_till_done()

    # Every mutation was a genuine reload the same flow continued through.
    assert entry.state is ConfigEntryState.LOADED
    assert len({id(manager) for manager in managers}) == 4
    flow = hass.config_entries.options._progress[flow_id]
    assert flow._manager is managers[-1]
    assert flow._selected_asset_uuid == other["asset_uuid"]

    # Nothing on any screen was an identifier, an internal key, or English.
    replacement_uuids = list(managers[-1]._data["replacement_records"])
    assert len(replacement_uuids) == 1
    forbidden = {
        ASSET_UUID,
        other["asset_uuid"],
        device.id,
        DEVICE_ID,
        *replacement_uuids,
        "asset_updated",
        "asset_replacement_updated",
        "asset_related_device_added",
    }
    shown = [text for screen in screens for text in _rendered(screen)]
    for text in shown:
        for value in forbidden:
            assert value not in text, (value, text)
    for english in ("updated", "Replaces", "Replaced by", "Related"):
        assert not any(english in text for text in shown), english
