"""The four direct editors reached from the Asset hub's sections.

Asset details, Linked purchase, Installation & location and Lifecycle each
open their own form from a hub section and, once saved, hand the person back
to that section for the same Asset. These tests own that navigation contract: what each path
returns to, whether it reloads, and what it must leave alone in the
neighbouring domains.
"""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.util import dt as dt_util

from custom_components.device_lifecycle.config_flow import NO_PURCHASE_SELECTION
from custom_components.device_lifecycle.const import (
    CONF_ASSET_NAME,
    CONF_ASSET_UUID,
    CONF_CONFIRM_AREA_CLEAR,
    CONF_CONFIRM_DISPOSED,
    CONF_DEPLOYMENT_STATE,
    CONF_EFFECTIVE_DATE,
    CONF_HA_AREA_ID,
    CONF_LIFECYCLE_STATUS,
    CONF_NOTES,
    CONF_PURCHASE_UUID,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DEPLOYMENT_STATE_UNKNOWN,
    LIFECYCLE_STATUS_ACTIVE,
    LIFECYCLE_STATUS_DISPOSED,
    LIFECYCLE_STATUS_RETIRED,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import ASSET_UUID, PURCHASE_UUID, capture_reloads
from .test_options_flow import (
    _identical_metadata_input,
    _manager,
    _options_flow,
)
from .test_purchase_reconciliation import (
    SECOND_PURCHASE_UUID,
    _data_with_second_purchase,
)

HUB_STEP = "manage_asset_menu"
DETAILS_SECTION = "asset_details_warranty_menu"
INSTALLATION_SECTION = "asset_installation_menu"
LIFECYCLE_SECTION = "asset_lifecycle_replacement_menu"
# Editor -> the section it was opened from, and returns to after a save.
SECTION_OF = {
    "edit_asset_metadata": DETAILS_SECTION,
    "change_asset_purchase": DETAILS_SECTION,
    "asset_deployment": INSTALLATION_SECTION,
    "asset_lifecycle": LIFECYCLE_SECTION,
}


async def _hub_flow(
    hass: HomeAssistant,
    manager: AssetStoreManager,
    asset_uuid: str = ASSET_UUID,
):
    """Return a flow sitting on one Asset's hub."""
    flow, _entry = _options_flow(hass, manager)
    hub = await flow.async_step_manage_asset({CONF_ASSET_UUID: asset_uuid})
    assert hub["step_id"] == HUB_STEP
    return flow


def _identity(manager: AssetStoreManager, asset_uuid: str) -> tuple[str, str]:
    """Return the identity that no editor may ever change."""
    asset = manager.asset(asset_uuid)
    return asset["asset_uuid"], asset["asset_id"]


async def test_details_change_saves_reloads_and_returns_to_its_section(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Asset details: a real edit saves, reloads, and lands on its section."""
    manager = _manager(hass, asset_store_data)
    flow = await _hub_flow(hass, manager)
    identity = _identity(manager, ASSET_UUID)

    form = await flow.async_step_edit_asset_metadata()
    edit = _identical_metadata_input(manager.asset(ASSET_UUID))
    edit[CONF_ASSET_NAME] = "Bench computer"

    with capture_reloads(hass) as reload:
        saved = await flow.async_step_edit_asset_metadata(edit)

    assert form["step_id"] == "edit_asset_metadata"
    assert saved["type"] is FlowResultType.MENU
    assert saved["step_id"] == DETAILS_SECTION
    assert flow._selected_asset_uuid == ASSET_UUID
    assert manager.asset(ASSET_UUID)["name"] == "Bench computer"
    assert _identity(manager, ASSET_UUID) == identity
    reload.assert_called_once()


async def test_details_resubmission_returns_to_its_section_without_reloading(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Asset details: touching nothing is not a change, and still goes back."""
    manager = _manager(hass, asset_store_data)
    flow = await _hub_flow(hass, manager)
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        saved = await flow.async_step_edit_asset_metadata(
            _identical_metadata_input(manager.asset(ASSET_UUID))
        )

    assert saved["step_id"] == DETAILS_SECTION
    assert flow._selected_asset_uuid == ASSET_UUID
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


async def test_purchase_relink_returns_to_its_section_and_keeps_warranty(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Linked purchase: relinking moves the Purchase, nothing else."""
    manager = _manager(hass, _data_with_second_purchase(asset_store_data))
    flow = await _hub_flow(hass, manager)
    identity = _identity(manager, ASSET_UUID)
    warranty = deepcopy(manager.asset(ASSET_UUID)["warranty"])
    installed_date = manager.asset(ASSET_UUID)["installed_date"]

    with capture_reloads(hass) as reload:
        saved = await flow.async_step_change_asset_purchase(
            {CONF_PURCHASE_UUID: SECOND_PURCHASE_UUID}
        )

    asset = manager.asset(ASSET_UUID)
    assert saved["step_id"] == DETAILS_SECTION
    assert flow._selected_asset_uuid == ASSET_UUID
    assert asset["purchase_uuid"] == SECOND_PURCHASE_UUID
    assert asset["field_sources"]["purchase_uuid"] == "user"
    assert asset["warranty"] == warranty
    assert asset["installed_date"] == installed_date
    assert _identity(manager, ASSET_UUID) == identity
    reload.assert_called_once()


async def test_purchase_cleared_returns_to_its_section_and_keeps_warranty(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Linked purchase: choosing no Purchase does not strip the warranty."""
    manager = _manager(hass, asset_store_data)
    flow = await _hub_flow(hass, manager)
    warranty = deepcopy(manager.asset(ASSET_UUID)["warranty"])

    with capture_reloads(hass) as reload:
        saved = await flow.async_step_change_asset_purchase(
            {CONF_PURCHASE_UUID: NO_PURCHASE_SELECTION}
        )

    asset = manager.asset(ASSET_UUID)
    assert saved["step_id"] == DETAILS_SECTION
    assert asset["purchase_uuid"] is None
    assert asset["field_sources"]["purchase_uuid"] == "user"
    assert asset["warranty"] == warranty
    assert manager.purchase(PURCHASE_UUID)["asset_uuids"] == []
    reload.assert_called_once()


async def test_deployment_change_returns_to_its_section_and_leaves_lifecycle_alone(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    area_registry: ar.AreaRegistry,
) -> None:
    """Installation & location: a normal save touches only deployment."""
    manager = _manager(hass, asset_store_data)
    flow = await _hub_flow(hass, manager)
    identity = _identity(manager, ASSET_UUID)
    lifecycle = deepcopy(manager.asset(ASSET_UUID)["lifecycle"])
    office = area_registry.async_create("Office")

    with capture_reloads(hass) as reload:
        saved = await flow.async_step_asset_deployment(
            {
                CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED,
                CONF_HA_AREA_ID: office.id,
            }
        )

    asset = manager.asset(ASSET_UUID)
    assert saved["step_id"] == INSTALLATION_SECTION
    assert flow._selected_asset_uuid == ASSET_UUID
    assert asset[CONF_DEPLOYMENT_STATE] == DEPLOYMENT_STATE_DEPLOYED
    assert asset[CONF_HA_AREA_ID] == office.id
    assert asset["lifecycle"] == lifecycle
    assert manager._data["lifecycle_events"] == {}
    assert _identity(manager, ASSET_UUID) == identity
    reload.assert_called_once()


async def test_deployment_resubmission_returns_to_its_section_without_reloading(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Installation & location: the same state is a no-op, not a write."""
    manager = _manager(hass, asset_store_data)
    flow = await _hub_flow(hass, manager)
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        saved = await flow.async_step_asset_deployment(
            {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_UNKNOWN}
        )

    assert saved["step_id"] == INSTALLATION_SECTION
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


async def test_confirmed_not_deployed_clears_the_area_and_returns_to_its_section(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    area_registry: ar.AreaRegistry,
) -> None:
    """Installation & location: the Area-clearing confirmation ends there."""
    office = area_registry.async_create("Office")
    data = deepcopy(asset_store_data)
    asset_data = data["assets"][ASSET_UUID]
    asset_data[CONF_DEPLOYMENT_STATE] = DEPLOYMENT_STATE_DEPLOYED
    asset_data[CONF_HA_AREA_ID] = office.id
    manager = _manager(hass, data)
    flow = await _hub_flow(hass, manager)
    identity = _identity(manager, ASSET_UUID)
    lifecycle = deepcopy(manager.asset(ASSET_UUID)["lifecycle"])

    confirmation = await flow.async_step_asset_deployment(
        {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED}
    )

    assert confirmation["type"] is FlowResultType.FORM
    assert confirmation["step_id"] == "confirm_not_deployed"

    with capture_reloads(hass) as reload:
        saved = await flow.async_step_confirm_not_deployed(
            {CONF_CONFIRM_AREA_CLEAR: True}
        )

    asset = manager.asset(ASSET_UUID)
    assert saved["step_id"] == INSTALLATION_SECTION
    assert flow._selected_asset_uuid == ASSET_UUID
    assert asset[CONF_DEPLOYMENT_STATE] == DEPLOYMENT_STATE_NOT_DEPLOYED
    assert asset[CONF_HA_AREA_ID] is None
    assert asset["lifecycle"] == lifecycle
    assert _identity(manager, ASSET_UUID) == identity
    reload.assert_called_once()


async def test_active_asset_may_be_not_deployed(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Deployment and lifecycle are independent: active + not_deployed is valid."""
    manager = _manager(hass, asset_store_data)
    flow = await _hub_flow(hass, manager)

    await flow.async_step_asset_lifecycle(
        {CONF_LIFECYCLE_STATUS: LIFECYCLE_STATUS_ACTIVE}
    )
    saved = await flow.async_step_asset_deployment(
        {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED}
    )

    asset = manager.asset(ASSET_UUID)
    assert saved["step_id"] == INSTALLATION_SECTION
    assert asset["lifecycle"]["status"] == LIFECYCLE_STATUS_ACTIVE
    assert asset[CONF_DEPLOYMENT_STATE] == DEPLOYMENT_STATE_NOT_DEPLOYED


async def test_lifecycle_change_returns_to_its_section_and_appends_history(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Lifecycle: a transition is appended, deployment is untouched."""
    manager = _manager(hass, asset_store_data)
    flow = await _hub_flow(hass, manager)
    identity = _identity(manager, ASSET_UUID)
    deployment = manager.asset(ASSET_UUID)[CONF_DEPLOYMENT_STATE]

    with capture_reloads(hass) as reload:
        saved = await flow.async_step_asset_lifecycle(
            {
                CONF_LIFECYCLE_STATUS: LIFECYCLE_STATUS_ACTIVE,
                CONF_EFFECTIVE_DATE: dt_util.now().date().isoformat(),
                CONF_NOTES: "Back in service",
            }
        )

    asset = manager.asset(ASSET_UUID)
    history = manager.lifecycle_events_for_asset(ASSET_UUID)
    assert saved["step_id"] == LIFECYCLE_SECTION
    assert flow._selected_asset_uuid == ASSET_UUID
    assert asset["lifecycle"]["status"] == LIFECYCLE_STATUS_ACTIVE
    assert asset[CONF_DEPLOYMENT_STATE] == deployment
    assert len(history) == 1
    assert _identity(manager, ASSET_UUID) == identity
    reload.assert_called_once()


async def test_lifecycle_history_is_append_only_across_transitions(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Lifecycle: later transitions add to the record, never rewrite it."""
    manager = _manager(hass, asset_store_data)
    flow = await _hub_flow(hass, manager)

    await flow.async_step_asset_lifecycle(
        {CONF_LIFECYCLE_STATUS: LIFECYCLE_STATUS_ACTIVE}
    )
    first = deepcopy(manager.lifecycle_events_for_asset(ASSET_UUID))

    saved = await flow.async_step_asset_lifecycle(
        {CONF_LIFECYCLE_STATUS: LIFECYCLE_STATUS_RETIRED}
    )

    history = manager.lifecycle_events_for_asset(ASSET_UUID)
    assert saved["step_id"] == LIFECYCLE_SECTION
    assert len(first) == 1
    assert len(history) == 2
    assert history[0] == first[0]
    assert history[-1]["from_status"] == LIFECYCLE_STATUS_ACTIVE
    assert history[-1]["to_status"] == LIFECYCLE_STATUS_RETIRED


async def test_same_lifecycle_status_returns_to_its_section_without_a_transition(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Lifecycle: resubmitting the current status records nothing."""
    manager = _manager(hass, asset_store_data)
    flow = await _hub_flow(hass, manager)
    await flow.async_step_asset_lifecycle(
        {CONF_LIFECYCLE_STATUS: LIFECYCLE_STATUS_ACTIVE}
    )
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with capture_reloads(hass) as reload:
        saved = await flow.async_step_asset_lifecycle(
            {CONF_LIFECYCLE_STATUS: LIFECYCLE_STATUS_ACTIVE}
        )

    assert saved["step_id"] == LIFECYCLE_SECTION
    assert flow._selected_asset_uuid == ASSET_UUID
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


async def test_confirmed_disposed_returns_to_its_section_and_leaves_deployment(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Lifecycle: disposal needs its own confirmation and ends in its section."""
    manager = _manager(hass, asset_store_data)
    flow = await _hub_flow(hass, manager)
    identity = _identity(manager, ASSET_UUID)
    deployment = manager.asset(ASSET_UUID)[CONF_DEPLOYMENT_STATE]

    confirmation = await flow.async_step_asset_lifecycle(
        {CONF_LIFECYCLE_STATUS: LIFECYCLE_STATUS_DISPOSED}
    )

    assert confirmation["type"] is FlowResultType.FORM
    assert confirmation["step_id"] == "confirm_disposed"
    assert manager.asset(ASSET_UUID)["lifecycle"]["status"] != (
        LIFECYCLE_STATUS_DISPOSED
    )

    with capture_reloads(hass) as reload:
        saved = await flow.async_step_confirm_disposed(
            {CONF_CONFIRM_DISPOSED: True}
        )

    asset = manager.asset(ASSET_UUID)
    assert saved["step_id"] == LIFECYCLE_SECTION
    assert flow._selected_asset_uuid == ASSET_UUID
    assert asset["lifecycle"]["status"] == LIFECYCLE_STATUS_DISPOSED
    assert asset[CONF_DEPLOYMENT_STATE] == deployment
    assert _identity(manager, ASSET_UUID) == identity
    reload.assert_called_once()


@pytest.mark.parametrize(
    ("step", "payload", "result"),
    [
        (
            "edit_asset_metadata",
            {CONF_ASSET_NAME: "Renamed for the result"},
            "Asset details updated.",
        ),
        (
            "change_asset_purchase",
            {CONF_PURCHASE_UUID: NO_PURCHASE_SELECTION},
            "Linked purchase updated.",
        ),
        (
            "asset_deployment",
            {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED},
            "Installation & location updated.",
        ),
        (
            "asset_lifecycle",
            {CONF_LIFECYCLE_STATUS: LIFECYCLE_STATUS_ACTIVE},
            "Lifecycle updated.",
        ),
    ],
)
async def test_each_editor_reports_its_result_once(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    step: str,
    payload: dict,
    result: str,
) -> None:
    """Every direct editor names what it did, once, in the section it returns to."""
    manager = _manager(hass, asset_store_data)
    flow = await _hub_flow(hass, manager)
    if step == "edit_asset_metadata":
        payload = {
            **_identical_metadata_input(manager.asset(ASSET_UUID)),
            **payload,
        }

    saved = await getattr(flow, f"async_step_{step}")(payload)

    assert saved["step_id"] == SECTION_OF[step]
    assert saved["description_placeholders"]["result"] == result

    reopened = await getattr(flow, f"async_step_{SECTION_OF[step]}")()
    hub = await flow.async_step_manage_asset_menu()

    assert reopened["description_placeholders"]["result"] == ""
    assert hub["description_placeholders"]["result"] == ""


async def test_the_section_after_a_save_reads_the_current_manager(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """The section renders post-mutation data, never the pre-mutation snapshot.

    The manager is swapped underneath the flow the way a reload swaps it, so
    a section reading a cached Asset would still show the old identity.
    """
    manager = _manager(hass, asset_store_data)
    flow, entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: ASSET_UUID})

    edit = _identical_metadata_input(manager.asset(ASSET_UUID))
    edit[CONF_ASSET_NAME] = "Saved through the old manager"

    async def _swap_manager(entry_id: str) -> bool:
        replacement = _manager(hass, manager._data)
        replacement._data["assets"][ASSET_UUID]["name"] = "Only in the new manager"
        entry.runtime_data = replacement
        return True

    with patch.object(
        hass.config_entries,
        "async_reload",
        new_callable=AsyncMock,
        side_effect=_swap_manager,
    ):
        saved = await flow.async_step_edit_asset_metadata(edit)

    assert saved["step_id"] == DETAILS_SECTION
    assert flow._manager is entry.runtime_data
    assert flow._manager is not manager
    assert saved["description_placeholders"]["asset"] == (
        "Only in the new manager · DL0007"
    )
