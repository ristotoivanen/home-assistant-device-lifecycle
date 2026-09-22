"""Contract tests for the shared `schedule_reload` isolation fixture."""

from __future__ import annotations

from unittest.mock import Mock, call

import pytest
from homeassistant.config_entries import ConfigEntryState, UnknownEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult, FlowResultType

from custom_components.device_lifecycle.const import (
    CONF_ASSET_NAME,
    CONF_ASSET_UUID,
)
from custom_components.device_lifecycle.storage import AssetStoreManager

from .test_options_flow import _manager, _options_flow


async def _rename_asset_through_options_flow(
    hass: HomeAssistant,
) -> tuple[AssetStoreManager, str, object, FlowResult]:
    """Run one real canonical OptionsFlow mutation on a test-owned manager.

    The result type is left to the caller: this manager's data is in-memory
    only, so under the real_reload opt-out the mutation's reload legitimately
    replaces it with one loaded from the empty test Store.
    """
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Reload probe")
    flow, entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset["asset_uuid"]})
    result = await flow.async_step_edit_asset_metadata(
        {CONF_ASSET_NAME: "Renamed reload probe"}
    )
    return manager, asset["asset_uuid"], entry, result


async def test_mutation_reload_is_recorded_but_never_executed(
    hass: HomeAssistant,
    schedule_reload: Mock,
) -> None:
    """Draining the event loop cannot swap the test-owned runtime manager."""
    manager, asset_uuid, entry, result = await _rename_asset_through_options_flow(
        hass
    )

    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.MENU
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.runtime_data is manager
    schedule_reload.assert_called_once_with(entry.entry_id)
    flow, _entry = _options_flow(hass, manager)
    menu = await flow.async_step_manage_asset({CONF_ASSET_UUID: asset_uuid})
    assert menu["type"] is FlowResultType.MENU


async def test_intercept_keeps_the_known_entry_contract(
    hass: HomeAssistant,
    schedule_reload: Mock,
) -> None:
    """An unknown entry is rejected exactly like Home Assistant rejects it."""
    with pytest.raises(UnknownEntry):
        hass.config_entries.async_schedule_reload("missing-entry-id")

    with pytest.raises(UnknownEntry):
        await hass.config_entries.async_reload("missing-entry-id")

    schedule_reload.assert_not_called()


async def test_awaited_reload_is_recorded_but_never_executed(
    hass: HomeAssistant,
    schedule_reload: Mock,
) -> None:
    """The awaited mechanism reports success without an unload/setup cycle."""
    manager = _manager(hass)
    _flow, entry = _options_flow(hass, manager)

    assert await hass.config_entries.async_reload(entry.entry_id) is True
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.runtime_data is manager
    schedule_reload.assert_called_once_with(entry.entry_id)


async def test_both_reload_mechanisms_share_one_recorder(
    hass: HomeAssistant,
    schedule_reload: Mock,
) -> None:
    """DS-6 asserts that a reload happened, never which mechanism ran."""
    manager = _manager(hass)
    _flow, entry = _options_flow(hass, manager)

    hass.config_entries.async_schedule_reload(entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)

    assert schedule_reload.call_count == 2
    assert schedule_reload.call_args_list == [
        call(entry.entry_id),
        call(entry.entry_id),
    ]


@pytest.mark.real_reload
async def test_real_reload_opt_out_executes_home_assistant_reload(
    hass: HomeAssistant,
    schedule_reload: None,
) -> None:
    """The opt-out runs the real reload that replaces `runtime_data`.

    Both mechanisms must be Home Assistant's own under the opt-out, since
    the awaited one is what an asset mutation now continues through.
    """
    manager, _asset_uuid, entry, _result = await _rename_asset_through_options_flow(
        hass
    )

    await hass.async_block_till_done()

    assert schedule_reload is None
    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, AssetStoreManager)
    assert entry.runtime_data is not manager

    mutation_manager = entry.runtime_data

    assert await hass.config_entries.async_reload(entry.entry_id) is True

    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, AssetStoreManager)
    assert entry.runtime_data is not mutation_manager
