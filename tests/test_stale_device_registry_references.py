"""A Home Assistant device that has left the Device Registry never stops setup.

Device Lifecycle stores Home Assistant Device Registry IDs as references to
things it does not own: an Asset's primary and related devices, the devices a
Purchase configuration lists, and the device a Runtime configuration tracks.
Another integration can remove any of those devices at any time.

Before 0.7.5 one such removal was enough to leave the whole entry in
SETUP_ERROR on the next setup: the entity-registry relink replayed the
Purchase or Runtime configuration and asked Home Assistant to attach an
entity to a device that no longer existed, and Home Assistant refused.

These tests pin the rule that replaced it. A missing external device is an
unresolved reference, not a failure: setup continues, everything else keeps
working, the stored reference is kept exactly as it was for the person to
repair, and nothing is written, cleaned up or chosen on their behalf.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.device_lifecycle.const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
    CONF_HA_RELATIONSHIP_ACTION,
    CONF_RUNTIME_DATA_VERSION,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    HA_RELATIONSHIP_ACTION_REPLACE,
    RUNTIME_DATA_VERSION,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
)
from custom_components.device_lifecycle.exposure import relationships_unique_id
from custom_components.device_lifecycle.migration import (
    _device_can_be_linked,
    lifecycle_unique_id,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    STORAGE_KEY,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    DeviceLifecycleStore,
)

from .conftest import ASSET_UUID, PURCHASE_UUID
from .test_exposure_options_reload import (
    _purchase_edit_input,
    _setup_loaded_entry,
    _verified_store_readback,
)
from .test_ha_relationship_options_flow import _external_device

pytestmark = pytest.mark.real_reload

SECOND_ASSET_UUID = "44444444-4444-4444-8444-444444444444"
INTEGRATION = Path(__file__).parents[1] / "custom_components" / "device_lifecycle"


# --- Building an installation -------------------------------------------------


def _device(
    hass: HomeAssistant,
    registry: dr.DeviceRegistry,
    key: str,
    asset: dict[str, Any],
) -> dr.DeviceEntry:
    """Return an external device whose metadata already matches its Asset.

    Matching metadata keeps the first setup from refreshing the Asset, so
    every Store write a test sees afterwards is one it caused.
    """
    _owner, device = _external_device(
        hass,
        registry,
        key=key,
        name=asset["name"],
        manufacturer=asset["manufacturer"],
        model=asset["model"],
        model_id=asset["model_id"],
        serial_number=asset["serial_number"],
        sw_version=asset["sw_version"],
        hw_version=asset["hw_version"],
    )
    return device


def _purchase(purchase_subentry_data: dict[str, Any], *device_ids: str) -> dict:
    """Return one Purchase configuration listing exactly these devices."""
    data = deepcopy(purchase_subentry_data)
    data[CONF_DEVICE_IDS] = list(device_ids)
    return {
        "data": data,
        "subentry_type": SUBENTRY_TYPE_PURCHASE,
        "title": data["purchase_name"],
        "unique_id": None,
    }


def _runtime(runtime_subentry_data: dict[str, Any], device_id: str) -> dict:
    """Return one Runtime configuration tracking this device."""
    data = deepcopy(runtime_subentry_data)
    data[CONF_DEVICE_ID] = device_id
    data[CONF_RUNTIME_DATA_VERSION] = RUNTIME_DATA_VERSION
    return {
        "data": data,
        "subentry_type": SUBENTRY_TYPE_RUNTIME,
        "title": "Workshop device",
        "unique_id": None,
    }


def _refs(primary: str | None, *related: str) -> list[dict[str, str]]:
    """Return an Asset's stored Home Assistant device references."""
    refs = [] if primary is None else [{"device_id": primary, "role": "primary"}]
    refs.extend({"device_id": device_id, "role": "related"} for device_id in related)
    return refs


def _with_second_asset(data: AssetStoreData) -> AssetStoreData:
    """Add a second, fully valid Asset to the same Purchase."""
    second = deepcopy(data["assets"][ASSET_UUID])
    second.update(
        {
            "asset_uuid": SECOND_ASSET_UUID,
            "asset_id": "DL0008",
            "name": "Garage heater",
            "serial_number": "SERIAL-2",
        }
    )
    data["assets"][SECOND_ASSET_UUID] = second
    data["purchases"][PURCHASE_UUID]["asset_uuids"].append(SECOND_ASSET_UUID)
    data["next_asset_number"] = 9
    return data


async def _load(
    hass: HomeAssistant,
    hass_storage: dict,
    data: AssetStoreData,
    *subentries: dict,
):
    """Set the real integration up and require it to load."""
    with _verified_store_readback(hass_storage):
        entry = await _setup_loaded_entry(
            hass,
            hass_storage,
            data,
            subentries_data=subentries,
        )
    assert entry.state is ConfigEntryState.LOADED
    return entry


@contextmanager
def _recorded_saves() -> Iterator[list[AssetStoreData]]:
    """Record every Device Lifecycle Store save while still performing it."""
    saves: list[AssetStoreData] = []
    original = DeviceLifecycleStore.async_save

    async def _spy(self: DeviceLifecycleStore, data: AssetStoreData) -> None:
        saves.append(deepcopy(data))
        await original(self, data)

    with patch.object(DeviceLifecycleStore, "async_save", _spy):
        yield saves


async def _reload(hass: HomeAssistant, hass_storage: dict, entry) -> None:
    """Reload the entry for real, as a restart or a saved change does."""
    with _verified_store_readback(hass_storage):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()


def _stored(hass_storage: dict) -> dict[str, Any]:
    """Return the persisted Store payload, envelope included."""
    return deepcopy(hass_storage[STORAGE_KEY])


def _entities(hass: HomeAssistant, entry) -> dict[str, str]:
    """Return this entry's sensor identities: unique ID -> entity ID.

    Every entity must also keep its registry entry: one removed and created
    again under the same entity ID would lose the person's customizations.
    """
    registry = er.async_get(hass)
    entries = [
        item
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
        if item.platform == DOMAIN
    ]
    registry_ids = {item.unique_id: item.id for item in entries}
    known = hass.data.setdefault("_stale_registry_test_ids", {})
    for unique_id, registry_id in registry_ids.items():
        assert known.setdefault(unique_id, registry_id) == registry_id, unique_id
    return {item.unique_id: item.entity_id for item in entries}


def _state(hass: HomeAssistant, unique_id: str):
    """Return the current state of one Device Lifecycle sensor."""
    entity_id = er.async_get(hass).async_get_entity_id(
        Platform.SENSOR,
        DOMAIN,
        unique_id,
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state


# --- The regression ---------------------------------------------------------


async def test_a_purchase_device_that_left_home_assistant_no_longer_stops_setup(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 0.7.3/0.7.4 known issue: SETUP_ERROR after the device is removed.

    The Purchase configuration still lists the device and the Asset still
    names it as primary. Both references stay exactly as stored; the entry
    loads; the Asset's entities keep their identities and states.
    """
    data = deepcopy(asset_store_data)
    device = _device(hass, device_registry, "stale-primary", data["assets"][ASSET_UUID])
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(device.id)
    purchase = _purchase(purchase_subentry_data, device.id)
    entry = await _load(hass, hass_storage, data, purchase)
    stored = _stored(hass_storage)
    entities = _entities(hass, entry)
    subentries = {key: dict(item.data) for key, item in entry.subentries.items()}

    device_registry.async_remove_device(device.id)
    await hass.async_block_till_done()
    caplog.clear()
    with _recorded_saves() as saves:
        await _reload(hass, hass_storage, entry)

    assert entry.state is ConfigEntryState.LOADED
    # One warning for the one unresolved reference, naming the Asset by its
    # Asset ID and the configuration that lists it, not by registry ID.
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name.startswith("custom_components.device_lifecycle")
    ]
    assert warnings == [
        (
            "The Home Assistant device that a Purchase configuration lists for "
            "Asset DL0007 is no longer in the Device Registry; keeping the "
            "stored reference and continuing setup"
        )
    ]
    assert not [
        record for record in caplog.records if record.levelno >= logging.ERROR
    ]
    # Nothing was written, migrated, cleaned up or re-pointed.
    assert saves == []
    assert _stored(hass_storage) == stored
    assert stored["version"] == STORAGE_VERSION
    assert stored["minor_version"] == STORAGE_MINOR_VERSION
    assert entry.version == CONFIG_ENTRY_VERSION
    assert {key: dict(item.data) for key, item in entry.subentries.items()} == (
        subentries
    )
    asset = entry.runtime_data.asset(ASSET_UUID)
    assert asset["ha_device_refs"] == _refs(device.id)
    assert asset["purchase_uuid"] == PURCHASE_UUID
    # Same entities, same identities; only the reference reads as missing.
    assert _entities(hass, entry) == entities
    assert _state(hass, lifecycle_unique_id(ASSET_UUID)).state != "unavailable"
    relationships = _state(hass, relationships_unique_id(ASSET_UUID))
    assert relationships.state == "missing"
    assert relationships.attributes["primary_state"] == "missing"
    # The Asset's lifecycle entity stays on its own Asset Device.
    lifecycle = er.async_get(hass).async_get(entities[lifecycle_unique_id(ASSET_UUID)])
    asset_device = device_registry.async_get(lifecycle.device_id)
    assert asset_device is not None
    assert asset_device.identifiers == {(DOMAIN, ASSET_UUID)}


async def test_a_tracked_runtime_device_that_left_home_assistant_no_longer_stops_setup(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    """The same class of failure through a Runtime configuration."""
    data = deepcopy(asset_store_data)
    device = _device(hass, device_registry, "stale-runtime", data["assets"][ASSET_UUID])
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(device.id)
    runtime = _runtime(runtime_subentry_data, device.id)
    entry = await _load(hass, hass_storage, data, runtime)
    stored = _stored(hass_storage)
    entities = _entities(hass, entry)

    device_registry.async_remove_device(device.id)
    await hass.async_block_till_done()
    with _recorded_saves() as saves:
        await _reload(hass, hass_storage, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert saves == []
    assert _stored(hass_storage) == stored
    assert entry.runtime_data.asset(ASSET_UUID)["ha_device_refs"] == _refs(device.id)
    runtime_data = next(iter(entry.subentries.values())).data
    assert runtime_data[CONF_DEVICE_ID] == device.id
    assert _entities(hass, entry) == entities


# --- What keeps working next to it ------------------------------------------


async def test_a_valid_primary_device_behaves_exactly_as_before(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    """Case A: nothing changes for a reference that still resolves."""
    data = deepcopy(asset_store_data)
    device = _device(hass, device_registry, "valid-primary", data["assets"][ASSET_UUID])
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(device.id)
    entry = await _load(
        hass, hass_storage, data, _purchase(purchase_subentry_data, device.id)
    )
    stored = _stored(hass_storage)
    entities = _entities(hass, entry)

    with _recorded_saves() as saves:
        await _reload(hass, hass_storage, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert saves == []
    assert _stored(hass_storage) == stored
    assert _entities(hass, entry) == entities
    relationships = _state(hass, relationships_unique_id(ASSET_UUID))
    assert relationships.state == "present"
    assert relationships.attributes["primary_state"] == "present"
    assert relationships.attributes["primary_device_name"] == "Workshop device"


async def test_one_stale_related_device_leaves_the_valid_ones_working(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Cases C, D and E: related references resolve one by one."""
    data = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    primary = _device(hass, device_registry, "related-primary", asset)
    _owner, kept = _external_device(
        hass, device_registry, key="related-kept", name="Wall socket"
    )
    _owner, gone = _external_device(
        hass, device_registry, key="related-gone", name="Old hub"
    )
    asset["ha_device_refs"] = _refs(primary.id, gone.id, kept.id)
    entry = await _load(hass, hass_storage, data)
    stored = _stored(hass_storage)
    entities = _entities(hass, entry)
    before = _state(hass, relationships_unique_id(ASSET_UUID))
    assert before.state == "present"
    assert before.attributes["missing_related_device_count"] == 0

    device_registry.async_remove_device(gone.id)
    await hass.async_block_till_done()
    with _recorded_saves() as saves:
        await _reload(hass, hass_storage, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert saves == []
    assert _stored(hass_storage) == stored
    assert entry.runtime_data.asset(ASSET_UUID)["ha_device_refs"] == (
        _refs(primary.id, gone.id, kept.id)
    )
    assert _entities(hass, entry) == entities
    after = _state(hass, relationships_unique_id(ASSET_UUID))
    assert after.state == "missing"
    assert after.attributes["primary_state"] == "present"
    assert after.attributes["missing_related_device_count"] == 1
    assert [
        (item["name"], item["state"]) for item in after.attributes["related_devices"]
    ] == [(None, "missing"), ("Wall socket", "present")]


async def test_one_stale_asset_does_not_hold_back_another(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    """Case F: two Assets in one Purchase, and only one device is gone."""
    data = _with_second_asset(deepcopy(asset_store_data))
    gone = _device(hass, device_registry, "two-gone", data["assets"][ASSET_UUID])
    valid = _device(
        hass, device_registry, "two-valid", data["assets"][SECOND_ASSET_UUID]
    )
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(gone.id)
    data["assets"][SECOND_ASSET_UUID]["ha_device_refs"] = _refs(valid.id)
    entry = await _load(
        hass,
        hass_storage,
        data,
        _purchase(purchase_subentry_data, gone.id, valid.id),
    )
    stored = _stored(hass_storage)
    entities = _entities(hass, entry)
    # Every enabled entity of the valid Asset; disabled ones have no state.
    second_states = {
        unique_id: state.state
        for unique_id, entity_id in entities.items()
        if unique_id.startswith(SECOND_ASSET_UUID)
        and (state := hass.states.get(entity_id)) is not None
    }
    assert len(second_states) >= 5

    device_registry.async_remove_device(gone.id)
    await hass.async_block_till_done()
    with _recorded_saves() as saves:
        await _reload(hass, hass_storage, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert saves == []
    assert _stored(hass_storage) == stored
    assert _entities(hass, entry) == entities
    # The valid Asset is fully operational: every entity has its state back.
    assert {
        unique_id: hass.states.get(entities[unique_id]).state
        for unique_id in second_states
    } == second_states
    second = entry.runtime_data.asset(SECOND_ASSET_UUID)
    assert second["ha_device_refs"] == _refs(valid.id)
    assert second["purchase_uuid"] == PURCHASE_UUID
    assert _state(hass, relationships_unique_id(SECOND_ASSET_UUID)).state == (
        "present"
    )
    assert _state(hass, relationships_unique_id(ASSET_UUID)).state == "missing"


async def test_the_stale_reference_survives_every_later_setup(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    """Case G: reloads and a full unload/setup keep it, and keep loading."""
    data = deepcopy(asset_store_data)
    device = _device(hass, device_registry, "stale-kept", data["assets"][ASSET_UUID])
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(device.id)
    entry = await _load(
        hass,
        hass_storage,
        data,
        _purchase(purchase_subentry_data, device.id),
        _runtime(runtime_subentry_data, device.id),
    )
    stored = _stored(hass_storage)
    entities = _entities(hass, entry)
    device_registry.async_remove_device(device.id)
    await hass.async_block_till_done()

    with _recorded_saves() as saves:
        for _cycle in range(2):
            await _reload(hass, hass_storage, entry)
            assert entry.state is ConfigEntryState.LOADED
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        with _verified_store_readback(hass_storage):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert saves == []
    assert _stored(hass_storage) == stored
    assert _entities(hass, entry) == entities
    assert entry.runtime_data.asset(ASSET_UUID)["ha_device_refs"] == _refs(device.id)


async def test_setup_changes_no_other_domain_data(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    """Case L: lifecycle, installation, purchase, warranty and replacement stay.

    The installation carries real history on both Assets first, so an
    unchanged Store proves more than an empty one would.
    """
    data = _with_second_asset(deepcopy(asset_store_data))
    gone = _device(hass, device_registry, "cross-gone", data["assets"][ASSET_UUID])
    valid = _device(
        hass, device_registry, "cross-valid", data["assets"][SECOND_ASSET_UUID]
    )
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(gone.id)
    data["assets"][SECOND_ASSET_UUID]["ha_device_refs"] = _refs(valid.id)
    entry = await _load(
        hass,
        hass_storage,
        data,
        _purchase(purchase_subentry_data, gone.id, valid.id),
    )
    manager = entry.runtime_data
    with _verified_store_readback(hass_storage):
        await manager.async_set_asset_lifecycle_reporting(
            ASSET_UUID, "retired", effective_date="2026-09-01", notes=None
        )
        await manager.async_create_asset_replacement(
            ASSET_UUID,
            SECOND_ASSET_UUID,
            reason="failure",
            effective_date="2026-09-01",
            notes=None,
        )
    stored = _stored(hass_storage)["data"]
    assert stored["lifecycle_events"]
    assert stored["replacement_records"]
    entities = _entities(hass, entry)

    device_registry.async_remove_device(gone.id)
    await hass.async_block_till_done()
    with _recorded_saves() as saves:
        await _reload(hass, hass_storage, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert saves == []
    after = _stored(hass_storage)["data"]
    for key in (
        "assets",
        "purchases",
        "lifecycle_events",
        "replacement_records",
        "next_asset_number",
    ):
        assert after[key] == stored[key], key
    assert _entities(hass, entry) == entities


# --- Repairing it is an explicit choice -------------------------------------


async def test_the_person_repairs_the_reference_through_the_existing_ui(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    """Case J: nothing moves until the person chooses, then only that moves.

    The Purchase configuration still lists the gone device, so it says so
    and keeps it until it is deselected. The primary device can then be
    replaced with a device the person picks, and the entry reloads clean.
    """
    data = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    gone = _device(hass, device_registry, "repair-gone", asset)
    _owner, new = _external_device(
        hass, device_registry, key="repair-new", name="New workshop device"
    )
    asset["ha_device_refs"] = _refs(gone.id)
    entry = await _load(
        hass, hass_storage, data, _purchase(purchase_subentry_data, gone.id)
    )
    entities = _entities(hass, entry)
    device_registry.async_remove_device(gone.id)
    await hass.async_block_till_done()
    await _reload(hass, hass_storage, entry)
    assert entry.state is ConfigEntryState.LOADED

    # Replacing is refused while a Purchase configuration still lists it.
    flow_id = await _open_primary_device(hass, entry)
    refused = await hass.config_entries.options.async_configure(
        flow_id,
        {
            CONF_HA_RELATIONSHIP_ACTION: HA_RELATIONSHIP_ACTION_REPLACE,
            CONF_DEVICE_ID: new.id,
        },
    )
    assert refused["step_id"] == "manage_primary_device"
    assert refused["errors"] == {"base": "ha_device_purchase_dependency"}
    hass.config_entries.options.async_abort(flow_id)
    assert entry.runtime_data.asset(ASSET_UUID)["ha_device_refs"] == _refs(gone.id)

    # The Purchase keeps the gone device until it is deselected.
    purchase = next(iter(entry.subentries.values()))
    edit = _purchase_edit_input(
        purchase_subentry_data, device_id=gone.id, installed_date="2026-01-20"
    )
    kept = await entry.start_subentry_reconfigure_flow(hass, purchase.subentry_id)
    still_listed = await hass.config_entries.subentries.async_configure(
        kept["flow_id"], edit
    )
    assert still_listed["type"] is FlowResultType.FORM
    assert still_listed["errors"] == {"base": "device_missing"}
    assert purchase.data[CONF_DEVICE_IDS] == [gone.id]
    hass.config_entries.subentries.async_abort(kept["flow_id"])

    deselect = await entry.start_subentry_reconfigure_flow(hass, purchase.subentry_id)
    with _verified_store_readback(hass_storage):
        done = await hass.config_entries.subentries.async_configure(
            deselect["flow_id"], {**edit, CONF_DEVICE_IDS: []}
        )
        await hass.async_block_till_done()
    assert done["reason"] == "reconfigure_successful"
    assert entry.state is ConfigEntryState.LOADED
    assert entry.subentries[purchase.subentry_id].data[CONF_DEVICE_IDS] == []
    # Deselecting touched the Purchase only; the Asset still names the device.
    assert entry.runtime_data.asset(ASSET_UUID)["ha_device_refs"] == _refs(gone.id)

    # Now the person picks the replacement, and only that reference changes.
    before = deepcopy(entry.runtime_data.asset(ASSET_UUID))
    flow_id = await _open_primary_device(hass, entry)
    with _verified_store_readback(hass_storage):
        saved = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_HA_RELATIONSHIP_ACTION: HA_RELATIONSHIP_ACTION_REPLACE,
                CONF_DEVICE_ID: new.id,
            },
        )
        await hass.async_block_till_done()
    assert saved["step_id"] == "ha_relationship"
    assert entry.state is ConfigEntryState.LOADED
    after = entry.runtime_data.asset(ASSET_UUID)
    assert after["ha_device_refs"] == _refs(new.id)
    # Metadata Home Assistant provides follows the new primary device, as it
    # always has; everything the Asset owns itself stays exactly as it was.
    followed = {"ha_device_refs"} | {
        field
        for field, source in before["field_sources"].items()
        if source == "home_assistant"
    }
    assert {key: value for key, value in after.items() if key not in followed} == {
        key: value for key, value in before.items() if key not in followed
    }
    assert after["asset_id"] == before["asset_id"]
    assert after["warranty"] == before["warranty"]
    hass.config_entries.options.async_abort(flow_id)

    await _reload(hass, hass_storage, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert _entities(hass, entry) == entities
    assert _state(hass, relationships_unique_id(ASSET_UUID)).state == "present"


async def _open_primary_device(hass: HomeAssistant, entry) -> str:
    """Open the primary Home Assistant device form for the Asset."""
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = flow["flow_id"]
    await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "manage_asset"}
    )
    await hass.config_entries.options.async_configure(
        flow_id, {"asset_uuid": ASSET_UUID}
    )
    await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "ha_relationship"}
    )
    form = await hass.config_entries.options.async_configure(
        flow_id, {"next_step_id": "manage_primary_device"}
    )
    assert form["step_id"] == "manage_primary_device"
    # The stale reference is named, never shown by its registry ID.
    stale_id = entry.runtime_data.asset(ASSET_UUID)["ha_device_refs"][0]["device_id"]
    assert form["description_placeholders"]["current_device"] == (
        "Home Assistant device unavailable"
    )
    assert all(
        stale_id not in str(value)
        for value in form["description_placeholders"].values()
    )
    return flow_id


# --- The question asked is Home Assistant's own ------------------------------


async def test_linking_asks_what_home_assistant_itself_will_accept(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Present, removed, and on each supported registry model.

    Home Assistant 2026.8 keys `devices` by device ID; 2026.9+ iterates
    entries and resolves a pre-migration composite ID to a stand-in that it
    will not attach an entity to. Both shapes are asked their own question.
    """
    _owner, device = _external_device(hass, device_registry, key="linkable")
    assert _device_can_be_linked(device_registry, device.id)
    device_registry.async_remove_device(device.id)
    assert not _device_can_be_linked(device_registry, device.id)

    keyed = SimpleNamespace(devices={"kept": object()})
    assert _device_can_be_linked(keyed, "kept")
    assert not _device_can_be_linked(keyed, "gone")

    asked: list[tuple[str, dict[str, Any]]] = []

    def _async_get(device_id: str, **kwargs: Any) -> object | None:
        asked.append((device_id, kwargs))
        return object() if device_id == "kept" else None

    iterated = SimpleNamespace(devices=[], async_get=_async_get)
    assert _device_can_be_linked(iterated, "kept")
    assert not _device_can_be_linked(iterated, "composite")
    assert asked == [
        ("kept", {"include_composite_devices": False}),
        ("composite", {"include_composite_devices": False}),
    ]


# --- No broad handler hides the rest ------------------------------------------


def test_no_new_broad_exception_handler() -> None:
    """A missing device is handled by name; nothing swallows every error.

    The only broad handlers are the exposure rollback's, which must keep
    unwinding whatever failed and re-raise it.
    """
    broad: list[tuple[str, str]] = []
    for path in sorted(INTEGRATION.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                names = (
                    [node.type]
                    if not isinstance(node.type, ast.Tuple)
                    else list(node.type.elts)
                )
                if node.type is None or any(
                    isinstance(name, ast.Name)
                    and name.id in ("Exception", "BaseException")
                    for name in names
                ):
                    broad.append((path.name, function.name))

    assert sorted(set(broad)) == [
        ("exposure.py", "_rollback_exposure_registry"),
        ("exposure.py", "async_reconcile_exposure_registry"),
    ]
