"""F5: flow-level durable Quick Add replay across cancellation and reload."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import storage as ha_storage
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle.config_flow import (
    NO_PURCHASE_SELECTION,
    NO_REPLACEMENT_SELECTION,
    QUICK_SECTION_DETAILS,
    QUICK_SECTION_IDENTITY,
    QUICK_SECTION_LIFECYCLE,
    DeviceLifecycleConfigFlow,
    DeviceLifecycleOptionsFlow,
)
from custom_components.device_lifecycle.const import (
    CONF_CONFIRM_QUICK_ADD,
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
    CONF_INSTALLED_DATE,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_NOTES,
    CONF_REPLACEMENT_REASON,
    CONF_RETIRE_PREDECESSOR,
    CONF_UNDEPLOY_PREDECESSOR,
    CONFIG_ENTRY_VERSION,
    DEPLOYMENT_STATE_DEPLOYED,
    DOMAIN,
    SUBENTRY_TYPE_PURCHASE,
    WARRANTY_NONE,
    WARRANTY_TWO_YEARS,
)
from custom_components.device_lifecycle.maintenance_mutations import (
    CalendarIntervalInput,
    CreateScheduleRequest,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    AssetStoreManager,
    DeviceLifecycleStore,
    json_util,
)

from . import test_persistence_lifecycle as lifecycle
from .conftest import ASSET_UUID, PURCHASE_UUID
from .test_quick_add_options_flow import _details, _external_device

pytestmark = pytest.mark.real_reload


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    return hass_tmp_config_dir


@pytest.fixture(autouse=True)
def real_files():
    with (
        patch.object(DeviceLifecycleStore, "_async_write_data", lifecycle._REAL_WRITE),
        patch.object(DeviceLifecycleStore, "_async_load", lifecycle._REAL_LOAD),
    ):
        yield


def _flow(hass: HomeAssistant, entry: MockConfigEntry) -> DeviceLifecycleOptionsFlow:
    flow = DeviceLifecycleConfigFlow.async_get_options_flow(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    flow.flow_id = "durable-replay-flow"
    flow.context = {}
    return flow


async def _installation(
    hass: HomeAssistant,
    data: AssetStoreData,
    purchase_data: dict[str, Any],
) -> MockConfigEntry:
    await lifecycle._seed(hass, data)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Device Lifecycle",
        unique_id="device_lifecycle_main",
        version=CONFIG_ENTRY_VERSION,
        data={},
        options={},
        subentries_data=(
            {
                "subentry_type": SUBENTRY_TYPE_PURCHASE,
                "title": "Workshop equipment",
                "unique_id": None,
                "data": purchase_data | {CONF_DEVICE_IDS: []},
            },
        ),
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _review(
    flow: DeviceLifecycleOptionsFlow,
    *,
    device_id: str | None,
    composite: bool = False,
) -> None:
    if device_id is None:
        await flow.async_step_quick_add_manual()
    else:
        await flow.async_step_quick_add_from_ha({CONF_DEVICE_ID: device_id})
    details = _details(
        name="Reviewed light",
        deployment_state=DEPLOYMENT_STATE_DEPLOYED,
        purchase_uuid=PURCHASE_UUID if composite else NO_PURCHASE_SELECTION,
        warranty_type=WARRANTY_TWO_YEARS if composite else WARRANTY_NONE,
        predecessor_uuid=ASSET_UUID if composite else NO_REPLACEMENT_SELECTION,
    )
    details[QUICK_SECTION_IDENTITY][CONF_MANUFACTURER] = "User-owned maker"
    details[QUICK_SECTION_IDENTITY][CONF_MODEL] = ""
    details[QUICK_SECTION_DETAILS][CONF_NOTES] = "Preserve reviewed notes"
    details[QUICK_SECTION_LIFECYCLE][CONF_INSTALLED_DATE] = "2026-01-20"
    result = await flow.async_step_quick_add_details(details)
    if composite:
        assert result["step_id"] == "quick_add_replacement"
        result = await flow.async_step_quick_add_replacement(
            {
                CONF_REPLACEMENT_REASON: "failure",
                CONF_RETIRE_PREDECESSOR: True,
                CONF_UNDEPLOY_PREDECESSOR: True,
            }
        )
    assert result["step_id"] == "quick_add_confirm" and not result.get("errors")


async def _cancelled_commit(
    hass: HomeAssistant,
    flow: DeviceLifecycleOptionsFlow,
    *,
    fail_readback: bool = False,
) -> None:
    gate = lifecycle._Gate(hass)
    read = json_util.load_json
    reads = 0

    def _read(path):
        nonlocal reads
        reads += 1
        if fail_readback and reads == 1:
            raise RuntimeError("lost readback after durable replacement")
        return read(path)

    with (
        patch.object(ha_storage, "write_utf8_file_atomic", side_effect=gate.write),
        patch.object(json_util, "load_json", side_effect=_read),
    ):
        confirm = asyncio.create_task(
            flow.async_step_quick_add_confirm({CONF_CONFIRM_QUICK_ADD: True})
        )
        try:
            await asyncio.wait_for(gate.started.wait(), 5)
            assert not confirm.done()
            confirm.cancel()
            await asyncio.sleep(0)
            assert not confirm.done()
        finally:
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await confirm


@pytest.mark.parametrize("source", ["ha", "manual"])
@pytest.mark.parametrize("transition", ["published", "recovery", "reload"])
@pytest.mark.parametrize(
    "composite", [False, True], ids=["asset", "purchase-replacement"]
)
async def test_cancelled_confirmation_replays_exact_durable_command(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    source: str,
    transition: str,
    composite: bool,
) -> None:
    """Same UUID succeeds after cancellation, recovery or true HA reload."""
    entry = await _installation(hass, asset_store_data, purchase_subentry_data)
    manager: AssetStoreManager = entry.runtime_data
    await manager.async_mutate_maintenance(
        CreateScheduleRequest(
            schedule_uuid="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1",
            asset_uuid=ASSET_UUID,
            name="Existing maintenance",
            calendar_interval=CalendarIntervalInput(6, "months"),
        )
    )
    flow = _flow(hass, entry)
    device = _external_device(hass) if source == "ha" else None
    await _review(
        flow, device_id=None if device is None else device.id, composite=composite
    )
    proposed = flow._quick_asset_uuid
    reloads = Mock()
    try:
        with patch.object(hass.config_entries, "async_schedule_reload", reloads):
            await _cancelled_commit(hass, flow, fail_readback=transition == "recovery")
            assert lifecycle._disk(manager)["assets"][proposed]["asset_id"] == "DL0008"
            reloads.assert_not_called()
            if transition == "recovery":
                assert (
                    manager._persistence_uncertain and manager.asset(proposed) is None
                )
                await manager.async_update_asset_metadata(
                    ASSET_UUID, category="Recovery trigger"
                )
            elif transition == "reload":
                assert await hass.config_entries.async_reload(entry.entry_id)
                await hass.async_block_till_done()
                assert (
                    entry.runtime_data is not manager
                    and entry.state is ConfigEntryState.LOADED
                )
            manager = entry.runtime_data
            before = deepcopy(lifecycle._disk(manager))
            before_bytes = Path(manager._store.path).read_bytes()
            with patch.object(
                manager._store, "async_save", wraps=manager._store.async_save
            ) as save:
                result = await flow.async_step_quick_add_confirm(
                    {CONF_CONFIRM_QUICK_ADD: True}
                )
            assert result["type"] is FlowResultType.CREATE_ENTRY
            assert result["description"] == "quick_asset_created"
            assert result["description_placeholders"] == {
                "asset_name": "Reviewed light"
            }
            assert flow._quick_asset_uuid == proposed
            save.assert_not_awaited()
            reloads.assert_called_once_with(entry.entry_id)
            assert Path(manager._store.path).read_bytes() == before_bytes
            assert manager._data == lifecycle._disk(manager) == before
            assert before["next_asset_number"] == 9 and len(before["assets"]) == 2
            created = before["assets"][proposed]
            assert (
                created["manufacturer"] == "User-owned maker"
                and created["model"] is None
            )
            assert created["field_sources"]["manufacturer"] == "user"
            assert created["field_sources"].get("model") == (
                "user" if source == "ha" else None
            )
            assert created["ha_device_refs"] == (
                [] if device is None else [{"device_id": device.id, "role": "primary"}]
            )
            assert len(before["maintenance_schedules"]) == 1
            if composite:
                assert (
                    before["purchases"][PURCHASE_UUID]["asset_uuids"].count(proposed)
                    == 1
                )
                assert len(before["replacement_records"]) == 1
                assert len(before["lifecycle_events"]) == 2
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("source", ["ha", "manual"])
@pytest.mark.parametrize("change", ["metadata", "provenance", "replacement"])
async def test_same_uuid_with_incompatible_command_is_not_successful_replay(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    source: str,
    change: str,
) -> None:
    entry = await _installation(hass, asset_store_data, purchase_subentry_data)
    manager = entry.runtime_data
    flow = _flow(hass, entry)
    device = _external_device(hass) if source == "ha" else None
    await _review(
        flow,
        device_id=None if device is None else device.id,
        composite=change == "replacement",
    )
    try:
        with patch.object(
            hass.config_entries, "async_schedule_reload", Mock()
        ) as reload:
            await _cancelled_commit(hass, flow)
            before = deepcopy(manager._data)
            before_bytes = Path(manager._store.path).read_bytes()
            if change == "metadata":
                flow._quick_details["metadata"]["name"] = "Changed command"
            elif change == "provenance":
                # Removing reviewed provenance is a validly shaped but different
                # command; it must not be accepted merely because UUID matches.
                flow._quick_details["field_sources"].pop("manufacturer")
            else:
                flow._quick_replacement[CONF_NOTES] = "Changed replacement history"
            result = await flow.async_step_quick_add_confirm(
                {CONF_CONFIRM_QUICK_ADD: True}
            )
            assert result["type"] is FlowResultType.FORM
            expected_error = (
                "invalid_quick_create_request"
                if change == "provenance"
                else "quick_create_idempotency_conflict"
            )
            assert result["errors"] == {"base": expected_error}
            reload.assert_not_called()
            assert manager._data == lifecycle._disk(manager) == before
            assert Path(manager._store.path).read_bytes() == before_bytes
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_link_to_another_proposed_uuid_remains_rejected(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    cancel_first: bool,
) -> None:
    entry = await _installation(hass, asset_store_data, purchase_subentry_data)
    manager = entry.runtime_data
    device = _external_device(hass)
    first, other = _flow(hass, entry), _flow(hass, entry)
    await _review(first, device_id=device.id)
    await _review(other, device_id=device.id)
    assert first._quick_asset_uuid != other._quick_asset_uuid
    try:
        with patch.object(
            hass.config_entries, "async_schedule_reload", Mock()
        ) as reload:
            if cancel_first:
                await _cancelled_commit(hass, first)
            else:
                assert (
                    await first.async_step_quick_add_confirm(
                        {CONF_CONFIRM_QUICK_ADD: True}
                    )
                )["type"] is FlowResultType.CREATE_ENTRY
            reload.reset_mock()
            before = deepcopy(manager._data)
            before_bytes = Path(manager._store.path).read_bytes()
            result = await other.async_step_quick_add_confirm(
                {CONF_CONFIRM_QUICK_ADD: True}
            )
            assert result["type"] is FlowResultType.FORM
            assert result["errors"] == {"base": "device_already_linked"}
            assert other._quick_asset_uuid not in before["assets"]
            reload.assert_not_called()
            assert manager._data == lifecycle._disk(manager) == before
            assert Path(manager._store.path).read_bytes() == before_bytes
    finally:
        assert await hass.config_entries.async_unload(entry.entry_id)
