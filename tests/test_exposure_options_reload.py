"""Integration tests for OptionsFlow-driven Asset exposure reloads."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
import logging
from unittest.mock import AsyncMock, patch

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle.const import (
    CONF_ASSET_NAME,
    CONF_ASSET_UUID,
    CONF_CONFIRM_QUICK_ADD,
    CONF_DEVICE_IDS,
    CONF_DEPLOYMENT_STATE,
    CONF_DEVICE_ID,
    CONF_INSTALLED_DATE,
    CONF_MANUFACTURER,
    CONF_NOTES,
    CONF_PURCHASE_DATE,
    CONF_PURCHASE_NAME,
    CONF_PURCHASE_PRICE,
    CONF_REPLACEMENT_REASON,
    CONF_REPLACEMENT_TARGET_ASSET_UUID,
    CONF_RECEIPT_REFERENCE,
    CONF_RECEIPT_URL,
    CONF_SELLER,
    CONF_WARRANTY_TYPE,
    CONF_WARRANTY_UNTIL,
    CONFIG_ENTRY_VERSION,
    DEPLOYMENT_STATE_DEPLOYED,
    DOMAIN,
    SUBENTRY_TYPE_PURCHASE,
)
from custom_components.device_lifecycle.exposure import (
    asset_device_entry,
    asset_id_unique_id,
    deployment_unique_id,
    installed_date_unique_id,
    lifecycle_status_unique_id,
    relationships_unique_id,
    replacement_unique_id,
)
from custom_components.device_lifecycle.migration import lifecycle_unique_id
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    STORAGE_KEY,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    AssetStoreError,
)

from .conftest import ASSET_UUID
from .test_quick_add_options_flow import _details


def _relationship_free_store(data: AssetStoreData) -> AssetStoreData:
    """Return one valid Asset with no Purchase or external relationship."""
    result = deepcopy(data)
    asset = result["assets"][ASSET_UUID]
    asset["purchase_uuid"] = None
    asset["field_sources"].pop("purchase_uuid", None)
    asset["ha_device_refs"] = []
    result["purchases"] = {}
    return result


@contextmanager
def _verified_store_readback(hass_storage: dict) -> Iterator[None]:
    """Bridge the storage fixture to Device Lifecycle's direct verification."""
    with patch(
        "custom_components.device_lifecycle.storage.json_util.load_json",
        side_effect=lambda _path: deepcopy(hass_storage[STORAGE_KEY]),
    ):
        yield


async def _setup_loaded_entry(
    hass: HomeAssistant,
    hass_storage: dict,
    data: AssetStoreData,
    *,
    subentries_data: tuple[dict[str, object], ...] = (),
) -> MockConfigEntry:
    """Load the real integration from one exact Store 2.1 payload."""
    hass_storage[STORAGE_KEY] = {
        "version": STORAGE_VERSION,
        "minor_version": STORAGE_MINOR_VERSION,
        "key": STORAGE_KEY,
        "data": deepcopy(data),
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Device Lifecycle",
        unique_id="device_lifecycle_main",
        version=CONFIG_ENTRY_VERSION,
        data={},
        options={},
        subentries_data=subentries_data,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _start_asset_action(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    asset_uuid: str,
    action: str,
) -> str:
    """Navigate the real Home Assistant OptionsFlow manager to one action."""
    initial = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = initial["flow_id"]
    selection = await hass.config_entries.options.async_configure(
        flow_id,
        {"next_step_id": "manage_asset"},
    )
    assert selection["type"] is FlowResultType.FORM
    menu = await hass.config_entries.options.async_configure(
        flow_id,
        {CONF_ASSET_UUID: asset_uuid},
    )
    assert menu["type"] is FlowResultType.MENU
    action_result = await hass.config_entries.options.async_configure(
        flow_id,
        {"next_step_id": action},
    )
    assert action_result["type"] in (
        FlowResultType.FORM,
        FlowResultType.MENU,
    )
    return flow_id


def _entity_id(registry: er.EntityRegistry, unique_id: str) -> str:
    """Return one required Device Lifecycle sensor Entity Registry ID."""
    entity_id = registry.async_get_entity_id(
        Platform.SENSOR,
        DOMAIN,
        unique_id,
    )
    assert entity_id is not None
    return entity_id


def _purchase_edit_input(
    purchase_data: dict,
    *,
    device_id: str,
    installed_date: str,
) -> dict:
    """Return exactly the editable Purchase fields for a real reconfigure flow."""
    editable_fields = (
        CONF_PURCHASE_NAME,
        CONF_PURCHASE_DATE,
        CONF_WARRANTY_TYPE,
        CONF_WARRANTY_UNTIL,
        CONF_SELLER,
        CONF_PURCHASE_PRICE,
        CONF_RECEIPT_REFERENCE,
        CONF_RECEIPT_URL,
        CONF_NOTES,
    )
    result = {
        key: purchase_data[key] for key in editable_fields if key in purchase_data
    }
    result[CONF_DEVICE_IDS] = [device_id]
    result[CONF_INSTALLED_DATE] = installed_date
    return result


async def test_quick_add_reload_exposes_device_and_seven_entities(
    hass: HomeAssistant,
    hass_storage: dict,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A successful real create flow schedules one reload and exposes the Asset."""
    caplog.set_level(logging.WARNING)
    entry = await _setup_loaded_entry(
        hass,
        hass_storage,
        {
            "next_asset_number": 1,
            "purchases": {},
            "assets": {},
            "lifecycle_events": {},
            "replacement_records": {},
        },
    )
    initial = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = initial["flow_id"]
    source_menu = await hass.config_entries.options.async_configure(
        flow_id,
        {"next_step_id": "quick_add"},
    )
    create_form = await hass.config_entries.options.async_configure(
        flow_id,
        {"next_step_id": "quick_add_manual"},
    )
    confirm = await hass.config_entries.options.async_configure(
        flow_id,
        _details(name="Immediately exposed Asset"),
    )
    assert source_menu["type"] is FlowResultType.MENU
    assert create_form["type"] is FlowResultType.FORM
    assert confirm["step_id"] == "quick_add_confirm"
    original_schedule_reload = hass.config_entries.async_schedule_reload

    with (
        _verified_store_readback(hass_storage),
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
            wraps=original_schedule_reload,
        ) as schedule_reload,
    ):
        completed = await hass.config_entries.options.async_configure(
            flow_id,
            {CONF_CONFIRM_QUICK_ADD: True},
        )
        assert completed["type"] is FlowResultType.CREATE_ENTRY
        schedule_reload.assert_called_once_with(entry.entry_id)
        await hass.async_block_till_done()

    asset = entry.runtime_data.assets()[0]
    asset_uuid = asset["asset_uuid"]
    device = asset_device_entry(
        device_registry,
        config_entry_id=entry.entry_id,
        asset_uuid=asset_uuid,
    )
    assert device is not None
    expected_unique_ids = {
        lifecycle_unique_id(asset_uuid),
        deployment_unique_id(asset_uuid),
        installed_date_unique_id(asset_uuid),
        relationships_unique_id(asset_uuid),
        asset_id_unique_id(asset_uuid),
        lifecycle_status_unique_id(asset_uuid),
        replacement_unique_id(asset_uuid),
    }
    assert {
        item.unique_id
        for item in er.async_entries_for_config_entry(
            entity_registry,
            entry.entry_id,
        )
        if item.unique_id in expected_unique_ids
    } == expected_unique_ids
    installed_date_entry = entity_registry.async_get(
        _entity_id(entity_registry, installed_date_unique_id(asset_uuid))
    )
    assert installed_date_entry is not None
    assert installed_date_entry.config_entry_id == entry.entry_id
    assert installed_date_entry.config_subentry_id is None
    assert installed_date_entry.device_id == device.id
    for unique_id in (
        lifecycle_status_unique_id(asset_uuid),
        replacement_unique_id(asset_uuid),
    ):
        registry_entry = entity_registry.async_get(
            _entity_id(entity_registry, unique_id)
        )
        assert registry_entry is not None
        assert registry_entry.config_entry_id == entry.entry_id
        assert registry_entry.config_subentry_id is None
        assert registry_entry.device_id == device.id
    replacement_entry = entity_registry.async_get(
        _entity_id(entity_registry, replacement_unique_id(asset_uuid))
    )
    assert replacement_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert entry.options == {}
    assert not any(
        record.name.startswith("custom_components.device_lifecycle")
        and (
            "deprecated" in record.getMessage().lower()
            or "will be removed" in record.getMessage().lower()
        )
        for record in caplog.records
    )


async def test_manage_replacement_reload_enables_canonical_entities(
    hass: HomeAssistant,
    hass_storage: dict,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """The standalone replacement flow receives canonical visibility on reload."""
    successor_uuid = "44444444-4444-4444-8444-444444444444"
    data = _relationship_free_store(asset_store_data)
    successor = deepcopy(data["assets"][ASSET_UUID])
    successor.update(
        {
            "asset_uuid": successor_uuid,
            "asset_id": "DL0008",
            "name": "Replacement Asset",
        }
    )
    data["assets"][successor_uuid] = successor
    data["next_asset_number"] = 9
    entry = await _setup_loaded_entry(hass, hass_storage, data)
    for asset_uuid in (ASSET_UUID, successor_uuid):
        replacement_entry = entity_registry.async_get(
            _entity_id(entity_registry, replacement_unique_id(asset_uuid))
        )
        assert replacement_entry is not None
        assert replacement_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION

    flow_id = await _start_asset_action(
        hass,
        entry,
        ASSET_UUID,
        "asset_replacement",
    )
    replacement_form = await hass.config_entries.options.async_configure(
        flow_id,
        {"next_step_id": "replacement_replaced_by"},
    )
    assert replacement_form["type"] is FlowResultType.FORM
    original_schedule_reload = hass.config_entries.async_schedule_reload

    with (
        _verified_store_readback(hass_storage),
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
            wraps=original_schedule_reload,
        ) as schedule_reload,
    ):
        completed = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_REPLACEMENT_TARGET_ASSET_UUID: successor_uuid,
                CONF_REPLACEMENT_REASON: "failure",
            },
        )
        assert completed["type"] is FlowResultType.CREATE_ENTRY
        schedule_reload.assert_called_once_with(entry.entry_id)
        await hass.async_block_till_done()

    for asset_uuid in (ASSET_UUID, successor_uuid):
        replacement_entry = entity_registry.async_get(
            _entity_id(entity_registry, replacement_unique_id(asset_uuid))
        )
        assert replacement_entry is not None
        assert replacement_entry.disabled_by is None
    assert entry.options == {}


async def test_edit_metadata_flow_reload_refreshes_asset_device(
    hass: HomeAssistant,
    hass_storage: dict,
    device_registry: dr.DeviceRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """A successful real metadata edit refreshes canonical Device metadata."""
    entry = await _setup_loaded_entry(
        hass,
        hass_storage,
        _relationship_free_store(asset_store_data),
    )
    flow_id = await _start_asset_action(
        hass,
        entry,
        ASSET_UUID,
        "edit_asset_metadata",
    )
    original_schedule_reload = hass.config_entries.async_schedule_reload

    with (
        _verified_store_readback(hass_storage),
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
            wraps=original_schedule_reload,
        ) as schedule_reload,
    ):
        completed = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_ASSET_NAME: "Reloaded Asset name",
                CONF_MANUFACTURER: "Reloaded manufacturer",
            },
        )
        assert completed["type"] is FlowResultType.CREATE_ENTRY
        schedule_reload.assert_called_once_with(entry.entry_id)
        await hass.async_block_till_done()

    device = asset_device_entry(
        device_registry,
        config_entry_id=entry.entry_id,
        asset_uuid=ASSET_UUID,
    )
    assert device is not None
    assert device.name == "Reloaded Asset name"
    assert device.manufacturer == "Reloaded manufacturer"
    assert entry.options == {}


async def test_deployment_flow_reload_refreshes_entity_state(
    hass: HomeAssistant,
    hass_storage: dict,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """A successful real deployment mutation refreshes its exposure entity."""
    entry = await _setup_loaded_entry(
        hass,
        hass_storage,
        _relationship_free_store(asset_store_data),
    )
    flow_id = await _start_asset_action(
        hass,
        entry,
        ASSET_UUID,
        "asset_deployment",
    )
    original_schedule_reload = hass.config_entries.async_schedule_reload

    with (
        _verified_store_readback(hass_storage),
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
            wraps=original_schedule_reload,
        ) as schedule_reload,
    ):
        completed = await hass.config_entries.options.async_configure(
            flow_id,
            {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED},
        )
        assert completed["type"] is FlowResultType.CREATE_ENTRY
        schedule_reload.assert_called_once_with(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get(
        _entity_id(entity_registry, deployment_unique_id(ASSET_UUID))
    )
    assert state is not None
    assert state.state == DEPLOYMENT_STATE_DEPLOYED
    assert entry.runtime_data.asset(ASSET_UUID)["deployment_state"] == (
        DEPLOYMENT_STATE_DEPLOYED
    )


async def test_relationship_flow_reload_refreshes_entity_references(
    hass: HomeAssistant,
    hass_storage: dict,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """A successful real relationship mutation refreshes exact references."""
    owner = MockConfigEntry(domain="hue", title="External owner", data={})
    owner.add_to_hass(hass)
    external = device_registry.async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("hue", "options-reload-primary")},
        name="External primary",
    )
    entry = await _setup_loaded_entry(
        hass,
        hass_storage,
        _relationship_free_store(asset_store_data),
    )
    flow_id = await _start_asset_action(
        hass,
        entry,
        ASSET_UUID,
        "ha_relationship",
    )
    primary_form = await hass.config_entries.options.async_configure(
        flow_id,
        {"next_step_id": "manage_primary_device"},
    )
    assert primary_form["type"] is FlowResultType.FORM
    original_schedule_reload = hass.config_entries.async_schedule_reload

    with (
        _verified_store_readback(hass_storage),
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
            wraps=original_schedule_reload,
        ) as schedule_reload,
    ):
        completed = await hass.config_entries.options.async_configure(
            flow_id,
            {CONF_DEVICE_ID: external.id},
        )
        assert completed["type"] is FlowResultType.CREATE_ENTRY
        schedule_reload.assert_called_once_with(entry.entry_id)
        await hass.async_block_till_done()

    state = hass.states.get(
        _entity_id(entity_registry, relationships_unique_id(ASSET_UUID))
    )
    assert state is not None
    assert state.state == "present"
    assert state.attributes["primary_state"] == "present"
    assert state.attributes["primary_device_id"] == external.id
    assert entry.runtime_data.asset(ASSET_UUID)["ha_device_refs"] == [
        {"device_id": external.id, "role": "primary"}
    ]


async def test_failed_asset_mutation_does_not_schedule_reload(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
) -> None:
    """A storage failure stays in the flow and never schedules a reload."""
    entry = await _setup_loaded_entry(
        hass,
        hass_storage,
        _relationship_free_store(asset_store_data),
    )
    flow_id = await _start_asset_action(
        hass,
        entry,
        ASSET_UUID,
        "edit_asset_metadata",
    )
    manager = entry.runtime_data

    with (
        patch.object(
            manager,
            "async_update_asset_metadata",
            AsyncMock(side_effect=AssetStoreError("injected storage failure")),
        ),
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
        ) as schedule_reload,
    ):
        failed = await hass.config_entries.options.async_configure(
            flow_id,
            {CONF_ASSET_NAME: "Must not reload"},
        )

    assert failed["type"] is FlowResultType.FORM
    assert failed["errors"] == {"base": "asset_store_error"}
    schedule_reload.assert_not_called()
    assert entry.runtime_data is manager
    assert entry.options == {}


@pytest.mark.parametrize(
    ("provenance", "canonical_before", "canonical_after"),
    [
        ("purchase", "2026-01-20", "2026-08-07"),
        ("user", "2026-02-02", "2026-02-02"),
    ],
)
async def test_purchase_edit_reload_refreshes_canonical_installation_date(
    hass: HomeAssistant,
    hass_storage: dict,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict,
    provenance: str,
    canonical_before: str,
    canonical_after: str,
) -> None:
    """A real Purchase edit reload exposes canonical provenance-safe date data."""
    owner = MockConfigEntry(domain="hue", title="External owner", data={})
    owner.add_to_hass(hass)
    external = device_registry.async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("hue", f"purchase-date-{provenance}")},
        name="Purchased external device",
    )
    data = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    asset["installed_date"] = canonical_before
    asset["field_sources"]["installed_date"] = provenance
    asset["ha_device_refs"] = [{"device_id": external.id, "role": "primary"}]
    purchase_data = deepcopy(purchase_subentry_data)
    purchase_data[CONF_DEVICE_IDS] = [external.id]
    subentries_data = (
        {
            "data": purchase_data,
            "subentry_type": SUBENTRY_TYPE_PURCHASE,
            "title": "Workshop equipment",
            "unique_id": None,
        },
    )

    with _verified_store_readback(hass_storage):
        entry = await _setup_loaded_entry(
            hass,
            hass_storage,
            data,
            subentries_data=subentries_data,
        )

    initial_state = hass.states.get(
        _entity_id(entity_registry, installed_date_unique_id(ASSET_UUID))
    )
    assert initial_state is not None
    assert initial_state.state == canonical_before
    purchase_subentry = next(iter(entry.subentries.values()))
    initial = await entry.start_subentry_reconfigure_flow(
        hass,
        purchase_subentry.subentry_id,
    )
    assert initial["type"] is FlowResultType.FORM
    original_schedule_reload = hass.config_entries.async_schedule_reload

    with (
        _verified_store_readback(hass_storage),
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
            wraps=original_schedule_reload,
        ) as schedule_reload,
    ):
        completed = await hass.config_entries.subentries.async_configure(
            initial["flow_id"],
            _purchase_edit_input(
                purchase_data,
                device_id=external.id,
                installed_date="2026-08-07",
            ),
        )
        assert completed["type"] is FlowResultType.ABORT
        assert completed["reason"] == "reconfigure_successful"
        await hass.async_block_till_done()
        schedule_reload.assert_called_once_with(entry.entry_id)

    refreshed_state = hass.states.get(
        _entity_id(entity_registry, installed_date_unique_id(ASSET_UUID))
    )
    assert refreshed_state is not None
    assert refreshed_state.state == canonical_after
    refreshed_asset = entry.runtime_data.asset(ASSET_UUID)
    assert refreshed_asset["installed_date"] == canonical_after
    assert refreshed_asset["field_sources"]["installed_date"] == provenance
