"""DS-6: which Asset mutations need a ConfigEntry reload, and which do not.

0.7.4 changes the mechanism an Asset mutation reloads through, from the
fire-and-forget `async_schedule_reload` to an awaited `async_reload` the
flow continues after. The contract about *whether* a mutation reloads is
deliberately unchanged from 0.7.3, so these tests assert only that a reload
was or was not requested — never which Home Assistant call delivered it.
"""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr

from custom_components.device_lifecycle.config_flow import NO_PURCHASE_SELECTION
from custom_components.device_lifecycle.const import (
    CONF_ASSET_NAME,
    CONF_ASSET_UUID,
    CONF_DEPLOYMENT_STATE,
    CONF_DEVICE_ID,
    CONF_EFFECTIVE_DATE,
    CONF_HA_RELATIONSHIP_ACTION,
    CONF_LIFECYCLE_STATUS,
    CONF_PURCHASE_UUID,
    CONF_REPLACEMENT_REASON,
    CONF_REPLACEMENT_TARGET_ASSET_UUID,
    DEPLOYMENT_STATE_DEPLOYED,
    HA_RELATIONSHIP_ACTION_REPLACE,
    LIFECYCLE_STATUS_ACTIVE,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import ASSET_UUID, PURCHASE_UUID, capture_reloads
from .test_ha_relationship_options_flow import _external_device
from .test_options_flow import _manager, _options_flow, _store_with_purchase


async def _on_asset(
    hass: HomeAssistant,
    manager: AssetStoreManager,
    asset_uuid: str = ASSET_UUID,
):
    """Return an OptionsFlow positioned on one Asset's management menu."""
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset_uuid})
    return flow


async def test_metadata_change_reloads_and_resubmission_does_not(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Metadata: a real edit reloads, an unchanged resubmission does not."""
    manager = _manager(hass, asset_store_data)
    flow = await _on_asset(hass, manager)

    with capture_reloads(hass) as reload:
        changed = await flow.async_step_edit_asset_metadata(
            {CONF_ASSET_NAME: "Renamed workshop device"}
        )

    assert changed["type"] is FlowResultType.MENU
    reload.assert_called_once()

    same_flow = await _on_asset(hass, manager)
    with capture_reloads(hass) as noop_reload:
        unchanged = await same_flow.async_step_edit_asset_metadata(
            {CONF_ASSET_NAME: "Renamed workshop device"}
        )

    assert unchanged["type"] is FlowResultType.MENU
    noop_reload.assert_not_called()


async def test_purchase_change_reloads_and_resubmission_does_not(
    hass: HomeAssistant,
) -> None:
    """Purchase: assigning reloads, re-assigning the same Purchase does not."""
    manager = _manager(hass, _store_with_purchase())
    asset = await manager.async_create_manual_asset(name="Purchase target")
    flow = await _on_asset(hass, manager, asset["asset_uuid"])

    with capture_reloads(hass) as reload:
        assigned = await flow.async_step_change_asset_purchase(
            {CONF_PURCHASE_UUID: PURCHASE_UUID}
        )

    assert assigned["type"] is FlowResultType.MENU
    reload.assert_called_once()

    same_flow = await _on_asset(hass, manager, asset["asset_uuid"])
    with capture_reloads(hass) as noop_reload:
        await same_flow.async_step_change_asset_purchase(
            {CONF_PURCHASE_UUID: PURCHASE_UUID}
        )

    noop_reload.assert_not_called()

    clear_flow = await _on_asset(hass, manager, asset["asset_uuid"])
    with capture_reloads(hass) as clear_reload:
        await clear_flow.async_step_change_asset_purchase(
            {CONF_PURCHASE_UUID: NO_PURCHASE_SELECTION}
        )

    clear_reload.assert_called_once()


async def test_deployment_change_reloads_and_resubmission_does_not(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Deployment: a state change reloads, the same state does not."""
    manager = _manager(hass, asset_store_data)
    flow = await _on_asset(hass, manager)

    with capture_reloads(hass) as reload:
        changed = await flow.async_step_asset_deployment(
            {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED}
        )

    assert changed["type"] is FlowResultType.MENU
    reload.assert_called_once()

    same_flow = await _on_asset(hass, manager)
    with capture_reloads(hass) as noop_reload:
        await same_flow.async_step_asset_deployment(
            {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED}
        )

    noop_reload.assert_not_called()


async def test_lifecycle_change_reloads_and_resubmission_does_not(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Lifecycle: a status change reloads, the same status does not."""
    manager = _manager(hass, asset_store_data)
    flow = await _on_asset(hass, manager)

    with capture_reloads(hass) as reload:
        changed = await flow.async_step_asset_lifecycle(
            {CONF_LIFECYCLE_STATUS: LIFECYCLE_STATUS_ACTIVE}
        )

    assert changed["type"] is FlowResultType.MENU
    reload.assert_called_once()

    same_flow = await _on_asset(hass, manager)
    with capture_reloads(hass) as noop_reload:
        await same_flow.async_step_asset_lifecycle(
            {CONF_LIFECYCLE_STATUS: LIFECYCLE_STATUS_ACTIVE}
        )

    noop_reload.assert_not_called()


async def test_replacement_record_reloads(hass: HomeAssistant) -> None:
    """Replacement: recording a physical replacement always reloads."""
    manager = _manager(hass)
    old = await manager.async_create_manual_asset(name="Old")
    new = await manager.async_create_manual_asset(name="New")
    flow = await _on_asset(hass, manager, new["asset_uuid"])

    with capture_reloads(hass) as reload:
        recorded = await flow.async_step_replacement_replaces(
            {
                CONF_REPLACEMENT_TARGET_ASSET_UUID: old["asset_uuid"],
                CONF_REPLACEMENT_REASON: "failure",
                CONF_EFFECTIVE_DATE: None,
            }
        )

    assert recorded["type"] is FlowResultType.MENU
    reload.assert_called_once()


async def test_ha_relationship_change_reloads_and_resubmission_does_not(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """HA relationship: a primary change reloads, the same primary does not."""
    _entry, device = _external_device(
        hass,
        device_registry,
        key="ds6-primary",
    )
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Relationship target")
    flow = await _on_asset(hass, manager, asset["asset_uuid"])

    with capture_reloads(hass) as reload:
        linked = await flow.async_step_manage_primary_device(
            {
                CONF_HA_RELATIONSHIP_ACTION: HA_RELATIONSHIP_ACTION_REPLACE,
                CONF_DEVICE_ID: device.id,
            }
        )

    assert linked["type"] is FlowResultType.MENU
    reload.assert_called_once()

    same_flow = await _on_asset(hass, manager, asset["asset_uuid"])
    with capture_reloads(hass) as noop_reload:
        await same_flow.async_step_manage_primary_device(
            {
                CONF_HA_RELATIONSHIP_ACTION: HA_RELATIONSHIP_ACTION_REPLACE,
                CONF_DEVICE_ID: device.id,
            }
        )

    noop_reload.assert_not_called()


async def test_no_change_path_keeps_the_same_selected_asset(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A canonical no-op reloads nothing and continues on the same Asset."""
    manager = _manager(hass, asset_store_data)
    flow = await _on_asset(hass, manager)
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        result = await flow.async_step_asset_lifecycle(
            {CONF_LIFECYCLE_STATUS: manager.asset(ASSET_UUID)["lifecycle"]["status"]}
        )

    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "manage_asset_menu"
    assert flow._selected_asset_uuid == ASSET_UUID
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


async def test_persistent_reload_failure_aborts_with_entry_not_loaded(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A reload that never makes the entry usable again aborts cleanly.

    This is the fallback for a genuine setup failure only: the mutation is
    already persisted, but the flow cannot keep managing an Asset through a
    manager that no longer exists.
    """
    manager = _manager(hass, asset_store_data)
    flow = await _on_asset(hass, manager)
    entry = flow.config_entry

    async def _reload_without_recovering(entry_id: str) -> bool:
        del entry.runtime_data
        return False

    with patch.object(
        hass.config_entries,
        "async_reload",
        new_callable=AsyncMock,
        side_effect=_reload_without_recovering,
    ):
        result = await flow.async_step_edit_asset_metadata(
            {CONF_ASSET_NAME: "Renamed before the failure"}
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"
    # The mutation itself still committed; only the continuation stopped.
    assert manager.asset(ASSET_UUID)["name"] == "Renamed before the failure"


@pytest.mark.parametrize(
    ("case", "returns", "state", "keeps_runtime_data", "usable"),
    [
        # A reload that completed: the only outcome the flow may continue on.
        ("reloaded", True, ConfigEntryState.LOADED, True, True),
        # Unload failed. Home Assistant deletes `runtime_data` only after a
        # successful unload, so the stale manager is still attached here.
        ("unload failed", False, ConfigEntryState.FAILED_UNLOAD, True, False),
        # Setup after the unload failed: no manager, entry not loaded.
        ("setup failed", False, ConfigEntryState.SETUP_ERROR, False, False),
        # Unload succeeded but the entry was disabled, so it is never set up
        # again — and `async_reload` still reports True.
        ("disabled", True, ConfigEntryState.NOT_LOADED, False, False),
    ],
)
async def test_reload_helper_matches_home_assistant_reload_outcomes(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    case: str,
    returns: bool,
    state: ConfigEntryState,
    keeps_runtime_data: bool,
    usable: bool,
) -> None:
    """Every `async_reload` outcome maps to the right usability verdict.

    Neither signal alone is sufficient: "unload failed" returns False while
    keeping a usable-looking manager, and "disabled" returns True with none.
    """
    manager = _manager(hass, asset_store_data)
    flow = await _on_asset(hass, manager)
    entry = flow.config_entry

    async def _reload(entry_id: str) -> bool:
        if not keeps_runtime_data:
            del entry.runtime_data
        entry.mock_state(hass, state)
        return returns

    with patch.object(
        hass.config_entries,
        "async_reload",
        new_callable=AsyncMock,
        side_effect=_reload,
    ):
        assert await flow._async_apply_reload(True) is usable, case


async def test_reload_helper_skips_unchanged_mutations(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """`changed=False` performs zero reloads and still reports usable."""
    manager = _manager(hass, asset_store_data)
    flow = await _on_asset(hass, manager)

    with capture_reloads(hass) as reload:
        assert await flow._async_apply_reload(False) is True

    reload.assert_not_called()
