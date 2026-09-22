"""Contract tests for the shared `schedule_reload` isolation fixture."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from homeassistant.config_entries import ConfigEntryState, UnknownEntry
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.device_lifecycle.const import (
    CONF_ASSET_NAME,
    CONF_ASSET_UUID,
)
from custom_components.device_lifecycle.storage import AssetStoreManager

from .test_options_flow import _manager, _options_flow


async def _rename_asset_through_options_flow(
    hass: HomeAssistant,
) -> tuple[AssetStoreManager, str, object]:
    """Run one real canonical OptionsFlow mutation on a test-owned manager."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Reload probe")
    flow, entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset["asset_uuid"]})
    result = await flow.async_step_edit_asset_metadata(
        {CONF_ASSET_NAME: "Renamed reload probe"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    return manager, asset["asset_uuid"], entry


async def test_mutation_reload_is_recorded_but_never_executed(
    hass: HomeAssistant,
    schedule_reload: Mock,
) -> None:
    """Draining the event loop cannot swap the test-owned runtime manager."""
    manager, asset_uuid, entry = await _rename_asset_through_options_flow(hass)

    await hass.async_block_till_done()

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

    schedule_reload.assert_not_called()


@pytest.mark.real_reload
async def test_real_reload_opt_out_executes_home_assistant_reload(
    hass: HomeAssistant,
    schedule_reload: None,
) -> None:
    """The opt-out runs the real reload that replaces `runtime_data`.

    This is the background reload that previously raced later steps of
    isolated OptionsFlow tests.
    """
    manager, _asset_uuid, entry = await _rename_asset_through_options_flow(hass)

    await hass.async_block_till_done()

    assert schedule_reload is None
    assert entry.state is ConfigEntryState.LOADED
    assert isinstance(entry.runtime_data, AssetStoreManager)
    assert entry.runtime_data is not manager
