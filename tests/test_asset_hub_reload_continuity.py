"""G1: an OptionsFlow survives the real reload its own mutation triggers.

Nothing here is mocked away. The entry is set up through Home Assistant,
the flow is driven through Home Assistant's own OptionsFlow manager, and
the mutation's reload runs a genuine unload/setup cycle that replaces
`runtime_data`. The point is to prove continuity across that replacement,
not that `async_reload` returned True.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components import device_lifecycle
from custom_components.device_lifecycle.const import (
    CONF_DEPLOYMENT_STATE,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_UNKNOWN,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import ASSET_UUID
from .test_exposure_options_reload import (
    _setup_loaded_entry,
    _start_asset_action,
    _verified_store_readback,
)

pytestmark = pytest.mark.real_reload


def _options_flow_handler(hass: HomeAssistant, flow_id: str) -> Any:
    """Return the live OptionsFlow object Home Assistant is driving."""
    return hass.config_entries.options._progress.get(flow_id)


@contextmanager
def _cycle_spies() -> Iterator[tuple[list[str], list[str]]]:
    """Record real integration unload/setup calls while still running them."""
    unloads: list[str] = []
    setups: list[str] = []
    real_unload = device_lifecycle.async_unload_entry
    real_setup = device_lifecycle.async_setup_entry

    async def _unload(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        unloads.append(entry.entry_id)
        return await real_unload(hass, entry)

    async def _setup(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        setups.append(entry.entry_id)
        return await real_setup(hass, entry)

    with (
        patch.object(device_lifecycle, "async_unload_entry", _unload),
        patch.object(device_lifecycle, "async_setup_entry", _setup),
    ):
        yield unloads, setups


async def test_options_flow_survives_the_real_reload_it_triggers(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
) -> None:
    """The same flow keeps managing the same Asset through a real reload."""
    with _verified_store_readback(hass_storage):
        entry = await _setup_loaded_entry(hass, hass_storage, asset_store_data)
    original_manager = entry.runtime_data
    assert isinstance(original_manager, AssetStoreManager)
    assert original_manager.asset(ASSET_UUID)[CONF_DEPLOYMENT_STATE] == (
        DEPLOYMENT_STATE_UNKNOWN
    )

    flow_id = await _start_asset_action(
        hass,
        entry,
        ASSET_UUID,
        "asset_deployment",
    )
    flow = _options_flow_handler(hass, flow_id)
    assert flow is not None
    assert flow._selected_asset_uuid == ASSET_UUID

    with (
        _verified_store_readback(hass_storage),
        _cycle_spies() as (unloads, setups),
    ):
        completed = await hass.config_entries.options.async_configure(
            flow_id,
            {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED},
        )

        # Asserted before the event loop is drained on purpose. A scheduled
        # reload would still be a pending task here, leaving the torn-down
        # manager in place for the next step; only an awaited one is already
        # finished by the time the mutation's result comes back.
        assert unloads == [entry.entry_id]
        assert setups == [entry.entry_id]
        assert entry.state is ConfigEntryState.LOADED
        new_manager = entry.runtime_data
        assert isinstance(new_manager, AssetStoreManager)
        assert new_manager is not original_manager

        # The mutation is canonical in the manager the reload installed, not
        # only in the one that performed it.
        assert new_manager.asset(ASSET_UUID)[CONF_DEPLOYMENT_STATE] == (
            DEPLOYMENT_STATE_DEPLOYED
        )

        # The flow survived: same object, same selection, no abort.
        assert completed["type"] is FlowResultType.MENU
        assert completed["step_id"] == "manage_asset_menu"
        assert _options_flow_handler(hass, flow_id) is flow
        assert flow._selected_asset_uuid == ASSET_UUID

        # And the next Asset-management step opens normally against the new
        # manager, which is exactly what the pre-0.7.4 scheduled reload raced.
        next_step = await hass.config_entries.options.async_configure(
            flow_id,
            {"next_step_id": "asset_deployment"},
        )

        assert next_step["type"] is FlowResultType.FORM
        assert next_step["step_id"] == "asset_deployment"
        assert flow._manager is new_manager

        await hass.async_block_till_done()


async def test_the_mutation_result_waits_for_its_reload_to_finish(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
) -> None:
    """The flow does not produce its next step until the reload completed.

    This is the guarantee a scheduled reload cannot give. Holding the
    integration's setup open makes the difference deterministic rather than
    a race: a fire-and-forget reload lets the mutation return while setup is
    still blocked, leaving the next step to read a torn-down entry.
    """
    with _verified_store_readback(hass_storage):
        entry = await _setup_loaded_entry(hass, hass_storage, asset_store_data)
    flow_id = await _start_asset_action(
        hass,
        entry,
        ASSET_UUID,
        "asset_deployment",
    )
    original_manager = entry.runtime_data

    release_setup = asyncio.Event()
    setup_reached = asyncio.Event()
    real_setup = device_lifecycle.async_setup_entry

    async def _gated_setup(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        setup_reached.set()
        await release_setup.wait()
        return await real_setup(hass, entry)

    with (
        _verified_store_readback(hass_storage),
        patch.object(device_lifecycle, "async_setup_entry", _gated_setup),
    ):
        mutation = asyncio.ensure_future(
            hass.config_entries.options.async_configure(
                flow_id,
                {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED},
            )
        )
        await asyncio.wait_for(setup_reached.wait(), timeout=5)

        assert not mutation.done()

        release_setup.set()
        completed = await mutation
        await hass.async_block_till_done()

    assert completed["type"] is FlowResultType.MENU
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not original_manager


async def test_second_mutation_in_the_same_flow_uses_the_reloaded_manager(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
) -> None:
    """Continuity holds for more than one hop: each reload is picked up."""
    with _verified_store_readback(hass_storage):
        entry = await _setup_loaded_entry(hass, hass_storage, asset_store_data)
    flow_id = await _start_asset_action(
        hass,
        entry,
        ASSET_UUID,
        "asset_deployment",
    )
    flow = _options_flow_handler(hass, flow_id)
    managers = [entry.runtime_data]

    with _verified_store_readback(hass_storage):
        first = await hass.config_entries.options.async_configure(
            flow_id,
            {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED},
        )
        # Captured without draining the loop: each reload must already be
        # complete when its own mutation returns.
        managers.append(entry.runtime_data)

        await hass.config_entries.options.async_configure(
            flow_id,
            {"next_step_id": "asset_deployment"},
        )
        second = await hass.config_entries.options.async_configure(
            flow_id,
            {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_UNKNOWN},
        )
        managers.append(entry.runtime_data)
        await hass.async_block_till_done()

    assert first["type"] is FlowResultType.MENU
    assert second["type"] is FlowResultType.MENU
    assert len({id(manager) for manager in managers}) == 3
    assert entry.state is ConfigEntryState.LOADED
    assert _options_flow_handler(hass, flow_id) is flow
    assert flow._selected_asset_uuid == ASSET_UUID
    assert flow._manager is managers[-1]
    assert managers[-1].asset(ASSET_UUID)[CONF_DEPLOYMENT_STATE] == (
        DEPLOYMENT_STATE_UNKNOWN
    )
