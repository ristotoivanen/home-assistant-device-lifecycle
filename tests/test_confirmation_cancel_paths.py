"""Saying no to a confirmation, and what that must not do.

Three operations ask before they act: clearing an Area when an Asset stops
being installed, recording a disposal, and voiding a replacement. Declining
the first two is a decision, not an error — it hands back the editor the
person came from. A void's button says Void, so an unconfirmed void stays
on its form and names what is missing; the window's X is how to leave it.
Either way every byte of stored data is left alone.
"""

from __future__ import annotations

from copy import deepcopy

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar

from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_CONFIRM_AREA_CLEAR,
    CONF_CONFIRM_DISPOSED,
    CONF_CONFIRM_VOID,
    CONF_DEPLOYMENT_STATE,
    CONF_HA_AREA_ID,
    CONF_LIFECYCLE_STATUS,
    CONF_REPLACEMENT_ACTION,
    CONF_REPLACEMENT_UUID,
    CONF_VOID_REASON,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    LIFECYCLE_STATUS_ACTIVE,
    LIFECYCLE_STATUS_DISPOSED,
    REPLACEMENT_ACTION_VOID,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import ASSET_UUID, capture_reloads
from .test_options_flow import _manager, _options_flow

HUB_STEP = "manage_asset_menu"


async def _on_asset(
    hass: HomeAssistant,
    manager: AssetStoreManager,
    asset_uuid: str = ASSET_UUID,
):
    """Return a flow sitting on one Asset's hub."""
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset_uuid})
    return flow


def _deployed_in(
    asset_store_data: AssetStoreData,
    area_id: str,
) -> AssetStoreData:
    """Return the fixture store with the Asset installed in one Area."""
    data = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    asset[CONF_DEPLOYMENT_STATE] = DEPLOYMENT_STATE_DEPLOYED
    asset[CONF_HA_AREA_ID] = area_id
    return data


async def _replacement_under_management(hass: HomeAssistant, manager):
    """Return a flow parked on the manage-replacement editor, with its record."""
    old = await manager.async_create_manual_asset(name="Old unit")
    new = await manager.async_create_manual_asset(name="New unit")
    record = await manager.async_create_asset_replacement(
        old["asset_uuid"],
        new["asset_uuid"],
        reason="failure",
        effective_date=None,
        notes=None,
    )
    flow = await _on_asset(hass, manager, old["asset_uuid"])
    return flow, old, record


async def test_declining_to_clear_the_area_returns_to_the_installation_editor(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    area_registry: ar.AreaRegistry,
) -> None:
    """Nothing about the Asset changes, in either domain."""
    office = area_registry.async_create("Office")
    manager = _manager(hass, _deployed_in(asset_store_data, office.id))
    flow = await _on_asset(hass, manager)

    confirmation = await flow.async_step_asset_deployment(
        {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED}
    )
    assert confirmation["step_id"] == "confirm_not_deployed"

    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        declined = await flow.async_step_confirm_not_deployed(
            {CONF_CONFIRM_AREA_CLEAR: False}
        )

    assert declined["type"] is FlowResultType.FORM
    assert declined["step_id"] == "asset_deployment"
    assert not declined["errors"]
    assert flow._selected_asset_uuid == ASSET_UUID
    assert manager._data == before
    asset = manager.asset(ASSET_UUID)
    assert asset[CONF_DEPLOYMENT_STATE] == DEPLOYMENT_STATE_DEPLOYED
    assert asset[CONF_HA_AREA_ID] == office.id
    assert asset["lifecycle"] == before["assets"][ASSET_UUID]["lifecycle"]
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


async def test_confirming_the_area_clear_still_saves_and_returns_to_its_section(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    area_registry: ar.AreaRegistry,
) -> None:
    """The accepted path is unchanged by the cancel contract."""
    office = area_registry.async_create("Office")
    manager = _manager(hass, _deployed_in(asset_store_data, office.id))
    flow = await _on_asset(hass, manager)
    await flow.async_step_asset_deployment(
        {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED}
    )

    with capture_reloads(hass) as reload:
        confirmed = await flow.async_step_confirm_not_deployed(
            {CONF_CONFIRM_AREA_CLEAR: True}
        )

    asset = manager.asset(ASSET_UUID)
    assert confirmed["step_id"] == "asset_installation_menu"
    assert asset[CONF_DEPLOYMENT_STATE] == DEPLOYMENT_STATE_NOT_DEPLOYED
    assert asset[CONF_HA_AREA_ID] is None
    reload.assert_called_once()


async def test_declining_disposal_returns_to_the_lifecycle_editor(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """No transition is recorded, so the history is untouched."""
    manager = _manager(hass, asset_store_data)
    flow = await _on_asset(hass, manager)
    await flow.async_step_asset_lifecycle(
        {CONF_LIFECYCLE_STATUS: LIFECYCLE_STATUS_ACTIVE}
    )

    confirmation = await flow.async_step_asset_lifecycle(
        {CONF_LIFECYCLE_STATUS: LIFECYCLE_STATUS_DISPOSED}
    )
    assert confirmation["step_id"] == "confirm_disposed"

    before = deepcopy(manager._data)
    before_history = deepcopy(manager.lifecycle_events_for_asset(ASSET_UUID))
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        declined = await flow.async_step_confirm_disposed(
            {CONF_CONFIRM_DISPOSED: False}
        )

    assert declined["type"] is FlowResultType.FORM
    assert declined["step_id"] == "asset_lifecycle"
    assert not declined["errors"]
    assert flow._selected_asset_uuid == ASSET_UUID
    assert manager._data == before
    assert manager.lifecycle_events_for_asset(ASSET_UUID) == before_history
    asset = manager.asset(ASSET_UUID)
    assert asset["lifecycle"]["status"] == LIFECYCLE_STATUS_ACTIVE
    assert asset[CONF_DEPLOYMENT_STATE] == (
        before["assets"][ASSET_UUID][CONF_DEPLOYMENT_STATE]
    )
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


async def test_confirming_disposal_still_appends_and_returns_to_its_section(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """The accepted path is unchanged by the cancel contract."""
    manager = _manager(hass, asset_store_data)
    flow = await _on_asset(hass, manager)
    await flow.async_step_asset_lifecycle(
        {CONF_LIFECYCLE_STATUS: LIFECYCLE_STATUS_ACTIVE}
    )
    before_history = len(manager.lifecycle_events_for_asset(ASSET_UUID))
    await flow.async_step_asset_lifecycle(
        {CONF_LIFECYCLE_STATUS: LIFECYCLE_STATUS_DISPOSED}
    )

    with capture_reloads(hass) as reload:
        confirmed = await flow.async_step_confirm_disposed(
            {CONF_CONFIRM_DISPOSED: True}
        )

    assert confirmed["step_id"] == "asset_lifecycle_replacement_menu"
    assert manager.asset(ASSET_UUID)["lifecycle"]["status"] == (
        LIFECYCLE_STATUS_DISPOSED
    )
    assert len(manager.lifecycle_events_for_asset(ASSET_UUID)) == (
        before_history + 1
    )
    reload.assert_called_once()


async def test_an_unconfirmed_void_stays_on_its_form_and_changes_nothing(
    hass: HomeAssistant,
) -> None:
    """The record survives the question intact, and the reason is kept."""
    manager = _manager(hass)
    flow, old, record = await _replacement_under_management(hass, manager)

    confirmation = await flow.async_step_manage_asset_replacement(
        {
            CONF_REPLACEMENT_UUID: record["replacement_uuid"],
            CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_VOID,
        }
    )
    assert confirmation["step_id"] == "confirm_void_replacement"

    before = deepcopy(manager._data)
    before_records = deepcopy(
        manager.replacement_records_for_asset(old["asset_uuid"])
    )
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        declined = await flow.async_step_confirm_void_replacement(
            {CONF_VOID_REASON: "Changed my mind", CONF_CONFIRM_VOID: False}
        )

    assert declined["type"] is FlowResultType.FORM
    assert declined["step_id"] == "confirm_void_replacement"
    assert declined["errors"] == {CONF_CONFIRM_VOID: "void_confirmation_required"}
    reason = next(
        marker
        for marker in declined["data_schema"].schema
        if marker == CONF_VOID_REASON
    )
    assert reason.description["suggested_value"] == "Changed my mind"
    assert flow._selected_asset_uuid == old["asset_uuid"]
    assert manager._data == before
    assert manager.replacement_records_for_asset(old["asset_uuid"]) == (
        before_records
    )
    assert manager.replacement_record(record["replacement_uuid"])["voided_at"] is (
        None
    )
    assert manager.active_replacement_successor(old["asset_uuid"]) is not None
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


async def test_confirming_a_void_still_voids_and_returns_to_the_submenu(
    hass: HomeAssistant,
) -> None:
    """The accepted path is unchanged by the cancel contract."""
    manager = _manager(hass)
    flow, old, record = await _replacement_under_management(hass, manager)
    await flow.async_step_manage_asset_replacement(
        {
            CONF_REPLACEMENT_UUID: record["replacement_uuid"],
            CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_VOID,
        }
    )

    with capture_reloads(hass) as reload:
        confirmed = await flow.async_step_confirm_void_replacement(
            {CONF_VOID_REASON: "Recorded in error", CONF_CONFIRM_VOID: True}
        )

    assert confirmed["step_id"] == "asset_replacement"
    assert manager.replacement_record(record["replacement_uuid"])["voided_at"]
    assert manager.active_replacement_successor(old["asset_uuid"]) is None
    reload.assert_called_once()


async def test_a_confirmed_void_still_needs_its_reason(
    hass: HomeAssistant,
) -> None:
    """Confirming without a reason is a validation error, not a cancel."""
    manager = _manager(hass)
    flow, _old, record = await _replacement_under_management(hass, manager)
    await flow.async_step_manage_asset_replacement(
        {
            CONF_REPLACEMENT_UUID: record["replacement_uuid"],
            CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_VOID,
        }
    )
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        blocked = await flow.async_step_confirm_void_replacement(
            {CONF_VOID_REASON: "   ", CONF_CONFIRM_VOID: True}
        )

    assert blocked["step_id"] == "confirm_void_replacement"
    assert blocked["errors"] == {CONF_VOID_REASON: "replacement_void_reason_required"}
    assert manager._data == before
    assert manager.replacement_record(record["replacement_uuid"])["voided_at"] is (
        None
    )
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


async def test_cancelling_never_reports_a_successful_operation(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    area_registry: ar.AreaRegistry,
) -> None:
    """Declining leaves no feedback behind for the next render to show."""
    office = area_registry.async_create("Office")
    manager = _manager(hass, _deployed_in(asset_store_data, office.id))
    flow = await _on_asset(hass, manager)

    await flow.async_step_asset_deployment(
        {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED}
    )
    await flow.async_step_confirm_not_deployed({CONF_CONFIRM_AREA_CLEAR: False})

    assert flow._last_result is None

    hub = await flow.async_step_manage_asset_menu()
    assert hub["description_placeholders"]["result"] == ""

    await flow.async_step_asset_lifecycle(
        {CONF_LIFECYCLE_STATUS: LIFECYCLE_STATUS_DISPOSED}
    )
    await flow.async_step_confirm_disposed({CONF_CONFIRM_DISPOSED: False})

    assert flow._last_result is None
    reopened = await flow.async_step_manage_asset_menu()
    assert reopened["description_placeholders"]["result"] == ""


async def test_a_cancelled_editor_reads_the_current_canonical_state(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    area_registry: ar.AreaRegistry,
) -> None:
    """The returned form describes the Asset as it is, not as it was asked to be."""
    office = area_registry.async_create("Office")
    manager = _manager(hass, _deployed_in(asset_store_data, office.id))
    flow = await _on_asset(hass, manager)
    await flow.async_step_asset_deployment(
        {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED}
    )

    declined = await flow.async_step_confirm_not_deployed(
        {CONF_CONFIRM_AREA_CLEAR: False}
    )

    defaults = declined["data_schema"]({})
    assert defaults[CONF_DEPLOYMENT_STATE] == DEPLOYMENT_STATE_DEPLOYED
    assert "Location: Office" in declined["description_placeholders"]["facts"]
