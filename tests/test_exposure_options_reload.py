"""Integration tests for OptionsFlow-driven Asset exposure reloads."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
import logging
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle.config_flow import (
    DeviceLifecycleConfigFlow,
    _runtime_title,
)
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
    CONF_POWER_HYSTERESIS,
    CONF_POWER_THRESHOLD,
    CONF_PURCHASE_DATE,
    CONF_PURCHASE_NAME,
    CONF_PURCHASE_PRICE,
    CONF_PURCHASE_UUID,
    CONF_REPLACEMENT_REASON,
    CONF_REPLACEMENT_TARGET_ASSET_UUID,
    CONF_RECEIPT_REFERENCE,
    CONF_RECEIPT_URL,
    CONF_RUNTIME_DATA_VERSION,
    CONF_RUNTIME_MODE,
    CONF_SELLER,
    CONF_SOURCE_ENTITY_ID,
    CONF_WARRANTY_TYPE,
    CONF_WARRANTY_UNTIL,
    CONFIG_ENTRY_VERSION,
    DEPLOYMENT_STATE_DEPLOYED,
    DOMAIN,
    RUNTIME_DATA_VERSION,
    RUNTIME_MODE_POWER,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
    WARRANTY_NONE,
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
from custom_components.device_lifecycle.migration import (
    lifecycle_unique_id,
    runtime_unique_id,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    STORAGE_KEY,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    AssetStoreError,
)

from .conftest import ASSET_UUID, SOURCE_ENTITY_ID
from .test_quick_add_options_flow import _details

QUICK_REPLAY_UUID = "88888888-8888-4888-8888-888888888888"


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


async def test_quick_add_replay_reload_leaves_registries_unchanged(
    hass: HomeAssistant,
    hass_storage: dict,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.7.2 WP8 / 072-10 Case D: replaying an already-fully-reconciled Quick
    Add still schedules a reload (current, intentional design), but that
    reload changes nothing observable — same Device Registry entry, same
    Entity Registry entries (identical unique_id/entity_id/device_id), no
    duplicate of either, no Store write."""
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
    flow = DeviceLifecycleConfigFlow.async_get_options_flow(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    flow.flow_id = "quick-add-replay-flow"
    flow.context = {}
    monkeypatch.setattr(
        "custom_components.device_lifecycle.config_flow.uuid4",
        Mock(return_value=UUID(QUICK_REPLAY_UUID)),
    )
    await flow.async_step_quick_add_manual()
    await flow.async_step_quick_add_details(_details(name="Replay reconciled Asset"))
    original_schedule_reload = hass.config_entries.async_schedule_reload

    with (
        _verified_store_readback(hass_storage),
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
            wraps=original_schedule_reload,
        ) as schedule_reload,
    ):
        first = await flow.async_step_quick_add_confirm(
            {CONF_CONFIRM_QUICK_ADD: True}
        )
        assert first["type"] is FlowResultType.CREATE_ENTRY
        schedule_reload.assert_called_once_with(entry.entry_id)
        await hass.async_block_till_done()

    manager = entry.runtime_data
    asset_uuid = manager.assets()[0]["asset_uuid"]
    device_before = asset_device_entry(
        device_registry,
        config_entry_id=entry.entry_id,
        asset_uuid=asset_uuid,
    )
    assert device_before is not None
    entities_before = {
        item.unique_id: (item.entity_id, item.device_id)
        for item in er.async_entries_for_config_entry(
            entity_registry, entry.entry_id
        )
    }
    devices_before = {
        item.id
        for item in dr.async_entries_for_config_entry(
            device_registry, entry.entry_id
        )
    }
    canonical_data = deepcopy(manager._data)

    # Exact replay: same flow instance, same idempotency UUID, same
    # unchanged canonical request. HA exposure is already fully reconciled
    # from the first reload above.
    with (
        _verified_store_readback(hass_storage),
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
            wraps=original_schedule_reload,
        ) as schedule_reload,
    ):
        replay = await flow.async_step_quick_add_confirm(
            {CONF_CONFIRM_QUICK_ADD: True}
        )
        assert replay["type"] is FlowResultType.CREATE_ENTRY
        # Current, intentional behavior: replay still schedules a reload.
        schedule_reload.assert_called_once_with(entry.entry_id)
        await hass.async_block_till_done()

    assert len(manager.assets()) == 1
    assert manager._data == canonical_data
    device_after = asset_device_entry(
        device_registry,
        config_entry_id=entry.entry_id,
        asset_uuid=asset_uuid,
    )
    assert device_after is not None
    assert device_after.id == device_before.id
    entities_after = {
        item.unique_id: (item.entity_id, item.device_id)
        for item in er.async_entries_for_config_entry(
            entity_registry, entry.entry_id
        )
    }
    devices_after = {
        item.id
        for item in dr.async_entries_for_config_entry(
            device_registry, entry.entry_id
        )
    }
    # Zero observable difference: no duplicate device, no duplicate entity,
    # no identity churn — the replay reload has no reconciliation work left
    # to do once state is already settled.
    assert entities_after == entities_before
    assert devices_after == devices_before


async def test_quick_add_ambiguous_persistence_replay_reload_exposes_asset(
    hass: HomeAssistant,
    hass_storage: dict,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.7.2 WP8 / 072-10 Case E: recovery/reconciliation replay scenario.

    When the Store's own write acknowledgement is ambiguous (a real,
    reachable code path — see `_async_recover_uncertain_persistence` /
    `async_quick_create_asset`'s ambiguous-save handling), the manager
    recovers within the SAME call by re-reading the persisted snapshot and
    returning `replayed=True` — without ever giving the flow a normal,
    non-exceptional first attempt. No prior reload has ever run for this
    Asset. This proves the unconditional reload in `_finish_quick_add` is
    load-bearing for this case: it performs the Asset's first-ever HA
    exposure (Device + all Entity Registry entries), which
    `async_quick_create_asset` itself never touches (it only ever writes to
    the Store).
    """
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
    flow = DeviceLifecycleConfigFlow.async_get_options_flow(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    flow.flow_id = "quick-add-ambiguous-flow"
    flow.context = {}
    monkeypatch.setattr(
        "custom_components.device_lifecycle.config_flow.uuid4",
        Mock(return_value=UUID(QUICK_REPLAY_UUID)),
    )
    await flow.async_step_quick_add_manual()
    await flow.async_step_quick_add_details(
        _details(name="Ambiguous recovery Asset")
    )

    original_schedule_reload = hass.config_entries.async_schedule_reload

    # Simulate exactly one failed persistence *readback* — the real
    # `DeviceLifecycleStore.async_save` write still lands for real in
    # ``hass_storage`` (only the post-write verification re-read fails,
    # reproducing `AssetStorePersistenceError(ambiguous=True)` from
    # storage.py's own readback path). The manager's internal recovery then
    # re-reads via `async_load_persisted_snapshot`, whose own readback
    # succeeds against the real, already-written data.
    calls = {"count": 0}

    def _load_json(_path: str) -> Any:
        calls["count"] += 1
        if calls["count"] == 1:
            raise HomeAssistantError("simulated readback failure")
        return deepcopy(hass_storage[STORAGE_KEY])

    with (
        patch(
            "custom_components.device_lifecycle.storage.json_util.load_json",
            side_effect=_load_json,
        ),
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
            wraps=original_schedule_reload,
        ) as schedule_reload,
    ):
        result = await flow.async_step_quick_add_confirm(
            {CONF_CONFIRM_QUICK_ADD: True}
        )
        # Success here (rather than a raised AssetStoreError/OSError) proves
        # the manager took the ambiguous-recovery-as-replay path: the first
        # readback attempt always fails, so a normal, non-exceptional first
        # attempt is impossible — this can only have completed via internal
        # replay recovery, on the flow's one and only attempt.
        assert result["type"] is FlowResultType.CREATE_ENTRY
        schedule_reload.assert_called_once_with(entry.entry_id)
        await hass.async_block_till_done()

    # Re-fetch via the entry: the reload above replaced `entry.runtime_data`
    # with a freshly constructed manager loaded from real Store data.
    asset = entry.runtime_data.assets()[0]
    assert asset["asset_uuid"] == QUICK_REPLAY_UUID
    assert asset["name"] == "Ambiguous recovery Asset"
    device = asset_device_entry(
        device_registry,
        config_entry_id=entry.entry_id,
        asset_uuid=asset["asset_uuid"],
    )
    assert device is not None
    exposed_unique_ids = {
        item.unique_id
        for item in er.async_entries_for_config_entry(
            entity_registry, entry.entry_id
        )
    }
    assert lifecycle_unique_id(asset["asset_uuid"]) in exposed_unique_ids
    assert installed_date_unique_id(asset["asset_uuid"]) in exposed_unique_ids


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
            "async_update_asset_metadata_reporting",
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


async def _setup_runtime_entry(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    runtime_subentry_data: dict,
    device_registry: dr.DeviceRegistry,
    *,
    source_state: tuple[str, dict] | None,
) -> tuple[MockConfigEntry, str]:
    """Load a real entry with one Runtime subentry pointed at a real device.

    ``source_state`` is ``(entity_id, attributes)`` to register in HA before
    setup, or ``None`` to leave the persisted source entirely unregistered —
    reproducing a genuinely missing Runtime source (0.7.2 WP4 / 072-06).
    Returns ``(entry, subentry_id)``.

    Stamps the current ``RUNTIME_DATA_VERSION`` provenance marker so the
    Runtime Hours sensor takes the clean "new" init path rather than the
    legacy pre-0.5.7 restore path, which requires an already-existing native
    HA entity that this synthetic fixture never creates (0.7.2 WP6 / 072-08).
    A real subentry created through the actual flow always carries this
    marker (see `async_step_runtime_source`), so this matches production
    shape rather than weakening what is verified.
    """
    owner = MockConfigEntry(domain="hue", title="External owner", data={})
    owner.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("hue", "runtime-source-device")},
        name="Workshop machine",
    )
    if source_state is not None:
        entity_id, attributes = source_state
        hass.states.async_set(entity_id, "12", attributes)

    data = deepcopy(asset_store_data)
    data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": device.id, "role": "primary"}
    ]
    runtime_data = deepcopy(runtime_subentry_data)
    runtime_data[CONF_DEVICE_ID] = device.id
    runtime_data.setdefault(CONF_RUNTIME_DATA_VERSION, RUNTIME_DATA_VERSION)
    subentries_data = (
        {
            "data": runtime_data,
            "subentry_type": SUBENTRY_TYPE_RUNTIME,
            # Match the title the flow itself would recompute from the device,
            # so an unrelated title mismatch cannot masquerade as a real
            # change and force a reload in the no-op test cases below.
            "title": _runtime_title(device_registry, device.id),
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

    subentry = next(
        subentry
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE_RUNTIME
    )
    return entry, subentry.subentry_id


async def _reconfigure_runtime_source(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    subentry_id: str,
    *,
    runtime_mode: str,
    source_input: dict,
) -> tuple[dict, dict]:
    """Drive the real two-step Runtime reconfigure flow to completion.

    Returns ``(source_form, result)`` — the rendered source-step form (before
    submission) and the final flow result.
    """
    initial = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert initial["type"] is FlowResultType.FORM
    assert initial["step_id"] == "reconfigure"

    source_form = await hass.config_entries.subentries.async_configure(
        initial["flow_id"],
        {CONF_RUNTIME_MODE: runtime_mode},
    )
    assert source_form["type"] is FlowResultType.FORM
    assert source_form["step_id"] == "reconfigure_source"

    result = await hass.config_entries.subentries.async_configure(
        initial["flow_id"],
        source_input,
    )
    return source_form, result


def _power_source_input(runtime_subentry_data: dict, source_entity_id: str) -> dict:
    """Return a Runtime source-step submission for the given entity."""
    return {
        CONF_SOURCE_ENTITY_ID: source_entity_id,
        CONF_POWER_THRESHOLD: runtime_subentry_data[CONF_POWER_THRESHOLD],
        CONF_POWER_HYSTERESIS: runtime_subentry_data[CONF_POWER_HYSTERESIS],
    }


async def test_runtime_reconfigure_unchanged_valid_source_is_a_clean_noop(
    hass: HomeAssistant,
    hass_storage: dict,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
    runtime_subentry_data: dict,
) -> None:
    """0.7.2 WP4 / 072-06 Case A: an unchanged, currently valid source stays a no-op."""
    entry, subentry_id = await _setup_runtime_entry(
        hass,
        hass_storage,
        asset_store_data,
        runtime_subentry_data,
        device_registry,
        source_state=(
            SOURCE_ENTITY_ID,
            {"device_class": SensorDeviceClass.POWER, "unit_of_measurement": "W"},
        ),
    )
    before = dict(entry.subentries[subentry_id].data)
    original_schedule_reload = hass.config_entries.async_schedule_reload

    with patch.object(
        hass.config_entries,
        "async_schedule_reload",
        wraps=original_schedule_reload,
    ) as schedule_reload:
        _source_form, result = await _reconfigure_runtime_source(
            hass,
            entry,
            subentry_id,
            runtime_mode=RUNTIME_MODE_POWER,
            source_input=_power_source_input(runtime_subentry_data, SOURCE_ENTITY_ID),
        )
        await hass.async_block_till_done()
        schedule_reload.assert_not_called()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert dict(entry.subentries[subentry_id].data) == before


async def test_runtime_reconfigure_unchanged_missing_source_is_a_clean_noop(
    hass: HomeAssistant,
    hass_storage: dict,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
    runtime_subentry_data: dict,
) -> None:
    """0.7.2 WP4 / 072-06 Case B: an unchanged but missing source is still a no-op.

    The persisted source must remain representable and resubmittable exactly
    as saved, even though it currently has no HA state, with zero Store/
    ConfigSubentry write and zero reload — not the pre-fix ``source_missing``
    form error.
    """
    entry, subentry_id = await _setup_runtime_entry(
        hass,
        hass_storage,
        asset_store_data,
        runtime_subentry_data,
        device_registry,
        source_state=None,
    )
    before = dict(entry.subentries[subentry_id].data)
    assert before[CONF_SOURCE_ENTITY_ID] == SOURCE_ENTITY_ID
    assert hass.states.get(SOURCE_ENTITY_ID) is None
    original_schedule_reload = hass.config_entries.async_schedule_reload

    with patch.object(
        hass.config_entries,
        "async_schedule_reload",
        wraps=original_schedule_reload,
    ) as schedule_reload:
        source_form, result = await _reconfigure_runtime_source(
            hass,
            entry,
            subentry_id,
            runtime_mode=RUNTIME_MODE_POWER,
            source_input=_power_source_input(runtime_subentry_data, SOURCE_ENTITY_ID),
        )
        await hass.async_block_till_done()
        schedule_reload.assert_not_called()

    source_field, source_validator = next(
        (marker, validator)
        for marker, validator in source_form["data_schema"].schema.items()
        if getattr(marker, "schema", marker) == CONF_SOURCE_ENTITY_ID
    )
    assert source_field.default() == SOURCE_ENTITY_ID
    assert SOURCE_ENTITY_ID in source_validator.config["include_entities"]

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert dict(entry.subentries[subentry_id].data) == before


async def test_runtime_reconfigure_retains_missing_source_while_changing_threshold(
    hass: HomeAssistant,
    hass_storage: dict,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
    runtime_subentry_data: dict,
) -> None:
    """0.7.2 WP4 / 072-06 Case C: another field can change while the missing
    source stays fixed — exactly one update/reload, source untouched."""
    entry, subentry_id = await _setup_runtime_entry(
        hass,
        hass_storage,
        asset_store_data,
        runtime_subentry_data,
        device_registry,
        source_state=None,
    )
    original_schedule_reload = hass.config_entries.async_schedule_reload

    with patch.object(
        hass.config_entries,
        "async_schedule_reload",
        wraps=original_schedule_reload,
    ) as schedule_reload:
        _source_form, result = await _reconfigure_runtime_source(
            hass,
            entry,
            subentry_id,
            runtime_mode=RUNTIME_MODE_POWER,
            source_input={
                CONF_SOURCE_ENTITY_ID: SOURCE_ENTITY_ID,
                CONF_POWER_THRESHOLD: 25.0,
                CONF_POWER_HYSTERESIS: runtime_subentry_data[CONF_POWER_HYSTERESIS],
            },
        )
        await hass.async_block_till_done()
        schedule_reload.assert_called_once_with(entry.entry_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = dict(entry.subentries[subentry_id].data)
    assert updated[CONF_SOURCE_ENTITY_ID] == SOURCE_ENTITY_ID
    assert updated[CONF_POWER_THRESHOLD] == 25.0


async def test_runtime_reconfigure_replaces_missing_source_with_valid_source(
    hass: HomeAssistant,
    hass_storage: dict,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
    runtime_subentry_data: dict,
) -> None:
    """0.7.2 WP4 / 072-06 Case D: a missing source can still be replaced normally."""
    replacement = "sensor.workshop_power_v2"
    entry, subentry_id = await _setup_runtime_entry(
        hass,
        hass_storage,
        asset_store_data,
        runtime_subentry_data,
        device_registry,
        source_state=None,
    )
    hass.states.async_set(
        replacement,
        "9",
        {"device_class": SensorDeviceClass.POWER, "unit_of_measurement": "W"},
    )
    original_schedule_reload = hass.config_entries.async_schedule_reload

    with patch.object(
        hass.config_entries,
        "async_schedule_reload",
        wraps=original_schedule_reload,
    ) as schedule_reload:
        _source_form, result = await _reconfigure_runtime_source(
            hass,
            entry,
            subentry_id,
            runtime_mode=RUNTIME_MODE_POWER,
            source_input=_power_source_input(runtime_subentry_data, replacement),
        )
        await hass.async_block_till_done()
        schedule_reload.assert_called_once_with(entry.entry_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    updated = dict(entry.subentries[subentry_id].data)
    assert updated[CONF_SOURCE_ENTITY_ID] == replacement


async def test_runtime_reconfigure_title_only_change_still_causes_one_reload(
    hass: HomeAssistant,
    hass_storage: dict,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
    runtime_subentry_data: dict,
) -> None:
    """0.7.2 WP4 / 072-06 WP4D: a real title change still reloads exactly once.

    The missing-source retention fix must not suppress an unrelated, genuine
    ConfigSubentry change — here, a device rename between save and reconfigure.
    """
    entry, subentry_id = await _setup_runtime_entry(
        hass,
        hass_storage,
        asset_store_data,
        runtime_subentry_data,
        device_registry,
        source_state=(
            SOURCE_ENTITY_ID,
            {"device_class": SensorDeviceClass.POWER, "unit_of_measurement": "W"},
        ),
    )
    device_id = entry.subentries[subentry_id].data[CONF_DEVICE_ID]
    title_before = entry.subentries[subentry_id].title
    device_registry.async_update_device(device_id, name_by_user="Renamed machine")
    original_schedule_reload = hass.config_entries.async_schedule_reload

    with patch.object(
        hass.config_entries,
        "async_schedule_reload",
        wraps=original_schedule_reload,
    ) as schedule_reload:
        _source_form, result = await _reconfigure_runtime_source(
            hass,
            entry,
            subentry_id,
            runtime_mode=RUNTIME_MODE_POWER,
            source_input=_power_source_input(runtime_subentry_data, SOURCE_ENTITY_ID),
        )
        await hass.async_block_till_done()
        schedule_reload.assert_called_once_with(entry.entry_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[subentry_id].title != title_before
    assert dict(entry.subentries[subentry_id].data)[CONF_SOURCE_ENTITY_ID] == (
        SOURCE_ENTITY_ID
    )


# --- 0.7.2 WP6 / 072-08: real ConfigSubentry create/edit/no-op/remove -------


async def _setup_purchase_entry(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict,
    device_registry: dr.DeviceRegistry,
) -> tuple[MockConfigEntry, str, str]:
    """Load a real entry with one Purchase subentry pointed at a real device.

    Returns ``(entry, subentry_id, device_id)``.
    """
    owner = MockConfigEntry(domain="hue", title="External owner", data={})
    owner.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("hue", "purchase-wp6-device")},
        name="Purchased external device",
    )
    data = deepcopy(asset_store_data)
    data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": device.id, "role": "primary"}
    ]
    purchase_data = deepcopy(purchase_subentry_data)
    purchase_data[CONF_DEVICE_IDS] = [device.id]
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

    subentry = next(iter(entry.subentries.values()))
    return entry, subentry.subentry_id, device.id


async def test_purchase_create_reload_persists_correct_data_and_title(
    hass: HomeAssistant,
    hass_storage: dict,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """0.7.2 WP6 / 072-08 Purchase Case A: a real subentry Create reloads
    exactly once and persists the exact submitted data and derived title."""
    owner = MockConfigEntry(domain="hue", title="External owner", data={})
    owner.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("hue", "purchase-create-device")},
        name="New workshop tool",
    )
    data = _relationship_free_store(asset_store_data)

    with _verified_store_readback(hass_storage):
        entry = await _setup_loaded_entry(hass, hass_storage, data)
    assert entry.subentries == {}

    original_schedule_reload = hass.config_entries.async_schedule_reload
    with (
        _verified_store_readback(hass_storage),
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
            wraps=original_schedule_reload,
        ) as schedule_reload,
    ):
        initial = await hass.config_entries.subentries.async_init(
            (entry.entry_id, SUBENTRY_TYPE_PURCHASE),
            context={"source": SOURCE_USER},
        )
        assert initial["type"] is FlowResultType.FORM
        result = await hass.config_entries.subentries.async_configure(
            initial["flow_id"],
            {
                CONF_DEVICE_IDS: [device.id],
                CONF_PURCHASE_NAME: "New tool purchase",
                CONF_PURCHASE_DATE: "2026-08-01",
                CONF_SELLER: "Example seller",
                CONF_PURCHASE_PRICE: 99.5,
                CONF_WARRANTY_TYPE: WARRANTY_NONE,
            },
        )
        await hass.async_block_till_done()
        schedule_reload.assert_called_once_with(entry.entry_id)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "New tool purchase"
    subentry = next(iter(entry.subentries.values()))
    assert subentry.subentry_type == SUBENTRY_TYPE_PURCHASE
    assert subentry.title == "New tool purchase"
    assert subentry.data[CONF_DEVICE_IDS] == [device.id]
    assert subentry.data[CONF_PURCHASE_NAME] == "New tool purchase"
    assert subentry.data[CONF_SELLER] == "Example seller"
    assert subentry.data[CONF_PURCHASE_PRICE] == 99.5
    created_asset = entry.runtime_data.asset_for_primary_device_id(device.id)
    assert created_asset is not None
    assert created_asset["purchase_uuid"] is not None


async def test_purchase_reconfigure_unchanged_data_is_a_clean_noop(
    hass: HomeAssistant,
    hass_storage: dict,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict,
) -> None:
    """0.7.2 WP6 / 072-08 Purchase Case C: resubmitting unchanged data through
    the real reconfigure flow is a true ConfigSubentry no-op."""
    entry, subentry_id, device_id = await _setup_purchase_entry(
        hass,
        hass_storage,
        asset_store_data,
        purchase_subentry_data,
        device_registry,
    )
    before = dict(entry.subentries[subentry_id].data)
    initial = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert initial["type"] is FlowResultType.FORM
    original_schedule_reload = hass.config_entries.async_schedule_reload

    with patch.object(
        hass.config_entries,
        "async_schedule_reload",
        wraps=original_schedule_reload,
    ) as schedule_reload:
        result = await hass.config_entries.subentries.async_configure(
            initial["flow_id"],
            _purchase_edit_input(
                before,
                device_id=device_id,
                installed_date=before[CONF_INSTALLED_DATE],
            ),
        )
        await hass.async_block_till_done()
        schedule_reload.assert_not_called()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert dict(entry.subentries[subentry_id].data) == before


async def test_purchase_remove_reloads_once_and_leaves_no_stale_registry_entries(
    hass: HomeAssistant,
    hass_storage: dict,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict,
) -> None:
    """0.7.2 WP6 / 072-08 Purchase Case D: removing a Purchase subentry
    reloads exactly once, cleanly, and leaves no stale Entity/Device
    Registry entry referencing the removed subentry."""
    entry, subentry_id, _device_id = await _setup_purchase_entry(
        hass,
        hass_storage,
        asset_store_data,
        purchase_subentry_data,
        device_registry,
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
        hass.config_entries.async_remove_subentry(entry, subentry_id)
        await hass.async_block_till_done()
        schedule_reload.assert_called_once_with(entry.entry_id)

    assert subentry_id not in entry.subentries
    assert not [
        item
        for item in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if item.config_subentry_id == subentry_id
    ]
    assert not [
        item
        for item in dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        if item.config_subentry_id == subentry_id
    ]
    purchase = entry.runtime_data.purchase(purchase_subentry_data[CONF_PURCHASE_UUID])
    assert purchase is not None
    assert purchase["configured"] is False


async def test_runtime_create_reload_persists_correct_data_title_and_entity(
    hass: HomeAssistant,
    hass_storage: dict,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """0.7.2 WP6 / 072-08 Runtime Case E: a real subentry Create reloads
    exactly once, persists correct data/title, and creates the Runtime
    Hours sensor with the expected stable Asset-owned unique_id."""
    owner = MockConfigEntry(domain="hue", title="External owner", data={})
    owner.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("hue", "runtime-create-real-device")},
        name="Workshop machine",
    )
    data = deepcopy(asset_store_data)
    data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": device.id, "role": "primary"}
    ]
    hass.states.async_set(
        SOURCE_ENTITY_ID,
        "12",
        {"device_class": SensorDeviceClass.POWER, "unit_of_measurement": "W"},
    )

    with _verified_store_readback(hass_storage):
        entry = await _setup_loaded_entry(hass, hass_storage, data)
    assert entry.subentries == {}

    original_schedule_reload = hass.config_entries.async_schedule_reload
    with (
        _verified_store_readback(hass_storage),
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
            wraps=original_schedule_reload,
        ) as schedule_reload,
    ):
        initial = await hass.config_entries.subentries.async_init(
            (entry.entry_id, SUBENTRY_TYPE_RUNTIME),
            context={"source": SOURCE_USER},
        )
        assert initial["type"] is FlowResultType.FORM
        assert initial["step_id"] == "user"
        source_form = await hass.config_entries.subentries.async_configure(
            initial["flow_id"],
            {CONF_DEVICE_ID: device.id, CONF_RUNTIME_MODE: RUNTIME_MODE_POWER},
        )
        assert source_form["type"] is FlowResultType.FORM
        assert source_form["step_id"] == "runtime_source"
        result = await hass.config_entries.subentries.async_configure(
            initial["flow_id"],
            {
                CONF_SOURCE_ENTITY_ID: SOURCE_ENTITY_ID,
                CONF_POWER_THRESHOLD: 10.0,
                CONF_POWER_HYSTERESIS: 2.0,
            },
        )
        await hass.async_block_till_done()
        schedule_reload.assert_called_once_with(entry.entry_id)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    subentry = next(iter(entry.subentries.values()))
    assert subentry.subentry_type == SUBENTRY_TYPE_RUNTIME
    assert subentry.data[CONF_DEVICE_ID] == device.id
    assert subentry.data[CONF_SOURCE_ENTITY_ID] == SOURCE_ENTITY_ID

    expected_unique_id = runtime_unique_id(ASSET_UUID)
    entity_id = entity_registry.async_get_entity_id(
        Platform.SENSOR, DOMAIN, expected_unique_id
    )
    assert entity_id is not None
    registry_entry = entity_registry.async_get(entity_id)
    assert registry_entry is not None
    assert registry_entry.config_subentry_id == subentry.subentry_id
    assert hass.states.get(entity_id) is not None


async def test_runtime_remove_reloads_once_and_removes_entity_cleanly(
    hass: HomeAssistant,
    hass_storage: dict,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
    runtime_subentry_data: dict,
) -> None:
    """0.7.2 WP6 / 072-08 Runtime Case I: removing a Runtime subentry reloads
    exactly once and the Runtime Hours entity is fully removed from the
    Entity Registry via HA core's own async_clear_config_subentry."""
    entry, subentry_id = await _setup_runtime_entry(
        hass,
        hass_storage,
        asset_store_data,
        runtime_subentry_data,
        device_registry,
        source_state=(
            SOURCE_ENTITY_ID,
            {"device_class": SensorDeviceClass.POWER, "unit_of_measurement": "W"},
        ),
    )
    expected_unique_id = runtime_unique_id(ASSET_UUID)
    entity_id = entity_registry.async_get_entity_id(
        Platform.SENSOR, DOMAIN, expected_unique_id
    )
    assert entity_id is not None
    original_schedule_reload = hass.config_entries.async_schedule_reload

    with (
        _verified_store_readback(hass_storage),
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
            wraps=original_schedule_reload,
        ) as schedule_reload,
    ):
        hass.config_entries.async_remove_subentry(entry, subentry_id)
        await hass.async_block_till_done()
        schedule_reload.assert_called_once_with(entry.entry_id)

    assert subentry_id not in entry.subentries
    assert entity_registry.async_get(entity_id) is None
    assert (
        entity_registry.async_get_entity_id(
            Platform.SENSOR, DOMAIN, expected_unique_id
        )
        is None
    )


async def test_runtime_recreate_after_removal_keeps_stable_unique_id(
    hass: HomeAssistant,
    hass_storage: dict,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
    runtime_subentry_data: dict,
) -> None:
    """0.7.2 WP6 / 072-08 Runtime Case J: recreating Runtime for the same
    Asset/source after removal reuses the stable Asset-owned unique_id and
    does not create a duplicate entity identity."""
    entry, subentry_id = await _setup_runtime_entry(
        hass,
        hass_storage,
        asset_store_data,
        runtime_subentry_data,
        device_registry,
        source_state=(
            SOURCE_ENTITY_ID,
            {"device_class": SensorDeviceClass.POWER, "unit_of_measurement": "W"},
        ),
    )
    device_id = entry.subentries[subentry_id].data[CONF_DEVICE_ID]
    expected_unique_id = runtime_unique_id(ASSET_UUID)
    assert (
        entity_registry.async_get_entity_id(
            Platform.SENSOR, DOMAIN, expected_unique_id
        )
        is not None
    )

    with _verified_store_readback(hass_storage):
        hass.config_entries.async_remove_subentry(entry, subentry_id)
        await hass.async_block_till_done()

    assert (
        entity_registry.async_get_entity_id(
            Platform.SENSOR, DOMAIN, expected_unique_id
        )
        is None
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
        initial = await hass.config_entries.subentries.async_init(
            (entry.entry_id, SUBENTRY_TYPE_RUNTIME),
            context={"source": SOURCE_USER},
        )
        source_form = await hass.config_entries.subentries.async_configure(
            initial["flow_id"],
            {CONF_DEVICE_ID: device_id, CONF_RUNTIME_MODE: RUNTIME_MODE_POWER},
        )
        assert source_form["step_id"] == "runtime_source"
        result = await hass.config_entries.subentries.async_configure(
            initial["flow_id"],
            {
                CONF_SOURCE_ENTITY_ID: SOURCE_ENTITY_ID,
                CONF_POWER_THRESHOLD: 10.0,
                CONF_POWER_HYSTERESIS: 2.0,
            },
        )
        await hass.async_block_till_done()
        schedule_reload.assert_called_once_with(entry.entry_id)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    recreated_entity_id = entity_registry.async_get_entity_id(
        Platform.SENSOR, DOMAIN, expected_unique_id
    )
    assert recreated_entity_id is not None
    matches = [
        item
        for item in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if item.unique_id == expected_unique_id
    ]
    assert len(matches) == 1
