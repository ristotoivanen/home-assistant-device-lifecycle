"""Home Assistant OptionsFlow tests for lifecycle and physical replacement."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.util import dt as dt_util

from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_CONFIRM_DISPOSED,
    CONF_CONFIRM_VOID,
    CONF_EFFECTIVE_DATE,
    CONF_LIFECYCLE_STATUS,
    CONF_NOTES,
    CONF_PREDECESSOR_ASSET_UUID,
    CONF_REPLACEMENT_ACTION,
    CONF_REPLACEMENT_REASON,
    CONF_REPLACEMENT_TARGET_ASSET_UUID,
    CONF_REPLACEMENT_UUID,
    CONF_SUCCESSOR_ASSET_UUID,
    CONF_VOID_REASON,
    REPLACEMENT_ACTION_CORRECT,
    REPLACEMENT_ACTION_VOID,
)
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import ASSET_UUID
from .test_options_flow import _manager, _options_flow


async def _select(
    hass: HomeAssistant,
    manager: AssetStoreManager,
    asset_uuid: str,
):
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset_uuid})
    return flow


async def _three_assets(manager: AssetStoreManager):
    return [
        await manager.async_create_manual_asset(name=name)
        for name in ("Old", "New", "Other")
    ]


async def test_lifecycle_happy_path_has_exactly_one_reload(
    hass: HomeAssistant,
    asset_store_data,
) -> None:
    manager = _manager(hass, asset_store_data)
    flow = await _select(hass, manager, ASSET_UUID)
    manager._store.async_save.reset_mock()

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as schedule_reload:
        result = await flow.async_step_asset_lifecycle(
            {
                CONF_LIFECYCLE_STATUS: "active",
                CONF_EFFECTIVE_DATE: dt_util.now().date().isoformat(),
                CONF_NOTES: "Inventory confirmed",
            }
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["description"] == "asset_lifecycle_updated"
    assert manager.asset(ASSET_UUID)["lifecycle"]["status"] == "active"
    manager._store.async_save.assert_awaited_once()
    schedule_reload.assert_called_once()


async def test_lifecycle_noop_writes_and_reloads_nothing(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Active")
    flow = await _select(hass, manager, asset["asset_uuid"])
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as schedule_reload:
        result = await flow.async_step_asset_lifecycle(
            {
                CONF_LIFECYCLE_STATUS: "active",
                CONF_EFFECTIVE_DATE: dt_util.now().date().isoformat(),
            }
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "lifecycle_no_change"}
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()
    schedule_reload.assert_not_called()


async def test_lifecycle_future_date_and_persistence_failure_reload_zero_times(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Lifecycle errors")
    flow = await _select(hass, manager, asset["asset_uuid"])

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as schedule_reload:
        future = await flow.async_step_asset_lifecycle(
            {
                CONF_LIFECYCLE_STATUS: "retired",
                CONF_EFFECTIVE_DATE: (
                    dt_util.now().date() + timedelta(days=1)
                ).isoformat(),
            }
        )
        manager._store.async_save = AsyncMock(side_effect=OSError("save failed"))
        failed = await flow.async_step_asset_lifecycle(
            {CONF_LIFECYCLE_STATUS: "retired"}
        )

    assert future["errors"] == {"base": "lifecycle_date_in_future"}
    assert failed["errors"] == {"base": "persistence_error"}
    assert manager.asset(asset["asset_uuid"])["lifecycle"]["status"] == "active"
    schedule_reload.assert_not_called()


async def test_disposed_requires_separate_confirmation(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Dispose carefully")
    flow = await _select(hass, manager, asset["asset_uuid"])
    manager._store.async_save.reset_mock()

    first = await flow.async_step_asset_lifecycle(
        {CONF_LIFECYCLE_STATUS: "disposed", CONF_NOTES: "Recycled"}
    )
    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as schedule_reload:
        result = await flow.async_step_confirm_disposed(
            {CONF_CONFIRM_DISPOSED: True}
        )

    assert first["type"] is FlowResultType.FORM
    assert first["step_id"] == "confirm_disposed"
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert manager.asset(asset["asset_uuid"])["lifecycle"]["status"] == "disposed"
    manager._store.async_save.assert_awaited_once()
    schedule_reload.assert_called_once()


async def test_lifecycle_asset_disappearing_before_submit_is_safe(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Temporary")
    flow = await _select(hass, manager, asset["asset_uuid"])
    manager._data["assets"].pop(asset["asset_uuid"])
    manager._data["lifecycle_events"].clear()

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as schedule_reload:
        result = await flow.async_step_asset_lifecycle(
            {CONF_LIFECYCLE_STATUS: "retired"}
        )

    assert result["step_id"] == "manage_asset"
    assert result["errors"] == {"base": "asset_missing"}
    schedule_reload.assert_not_called()


async def test_replacement_both_direction_workflows(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    old, new, other = await _three_assets(manager)
    manager._store.async_save.reset_mock()
    replaces_flow = await _select(hass, manager, new["asset_uuid"])

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as schedule_reload:
        replaces = await replaces_flow.async_step_replacement_replaces(
            {
                CONF_REPLACEMENT_TARGET_ASSET_UUID: old["asset_uuid"],
                CONF_REPLACEMENT_REASON: "failure",
                CONF_EFFECTIVE_DATE: None,
                CONF_NOTES: "Physical replacement",
            }
        )
    assert replaces["type"] is FlowResultType.CREATE_ENTRY
    assert manager.active_replacement_successor(old["asset_uuid"])["asset_uuid"] == (
        new["asset_uuid"]
    )
    schedule_reload.assert_called_once()

    await manager.async_void_asset_replacement(
        manager.replacement_records_for_asset(old["asset_uuid"])[0][
            "replacement_uuid"
        ],
        void_reason="Exercise reverse workflow",
    )
    manager._store.async_save.reset_mock()
    replaced_by_flow = await _select(hass, manager, old["asset_uuid"])
    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as schedule_reload:
        replaced_by = await replaced_by_flow.async_step_replacement_replaced_by(
            {
                CONF_REPLACEMENT_TARGET_ASSET_UUID: other["asset_uuid"],
                CONF_REPLACEMENT_REASON: "upgrade",
            }
        )
    assert replaced_by["type"] is FlowResultType.CREATE_ENTRY
    assert manager.active_replacement_successor(old["asset_uuid"])["asset_uuid"] == (
        other["asset_uuid"]
    )
    manager._store.async_save.assert_awaited_once()
    schedule_reload.assert_called_once()


async def test_replacement_stale_target_and_graph_change_are_store_authoritative(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    old, new, other = await _three_assets(manager)
    stale_flow = await _select(hass, manager, new["asset_uuid"])
    manager._data["assets"].pop(other["asset_uuid"])
    current_event = other["lifecycle"]["current_event_uuid"]
    manager._data["lifecycle_events"].pop(current_event)

    stale = await stale_flow.async_step_replacement_replaces(
        {
            CONF_REPLACEMENT_TARGET_ASSET_UUID: other["asset_uuid"],
            CONF_REPLACEMENT_REASON: "other",
        }
    )
    assert stale["errors"] == {"base": "asset_missing"}

    conflict_flow = await _select(hass, manager, old["asset_uuid"])
    await manager.async_create_asset_replacement(
        old["asset_uuid"],
        new["asset_uuid"],
        reason="failure",
        effective_date=None,
        notes=None,
    )
    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as schedule_reload:
        conflict = await conflict_flow.async_step_replacement_replaced_by(
            {
                CONF_REPLACEMENT_TARGET_ASSET_UUID: new["asset_uuid"],
                CONF_REPLACEMENT_REASON: "failure",
            }
        )
    assert conflict["errors"] == {"base": "replacement_predecessor_conflict"}
    schedule_reload.assert_not_called()


async def test_replacement_cycle_and_incoming_conflict_are_flow_errors(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    a, b, c = await _three_assets(manager)
    await manager.async_create_asset_replacement(
        a["asset_uuid"],
        b["asset_uuid"],
        reason="failure",
        effective_date=None,
        notes=None,
    )
    cycle_flow = await _select(hass, manager, b["asset_uuid"])
    cycle = await cycle_flow.async_step_replacement_replaced_by(
        {
            CONF_REPLACEMENT_TARGET_ASSET_UUID: a["asset_uuid"],
            CONF_REPLACEMENT_REASON: "failure",
        }
    )
    assert cycle["errors"] == {"base": "replacement_cycle"}

    incoming_flow = await _select(hass, manager, c["asset_uuid"])
    incoming = await incoming_flow.async_step_replacement_replaced_by(
        {
            CONF_REPLACEMENT_TARGET_ASSET_UUID: b["asset_uuid"],
            CONF_REPLACEMENT_REASON: "other",
        }
    )
    assert incoming["errors"] == {"base": "replacement_successor_conflict"}


async def test_void_workflow_confirms_reason_and_reloads_once(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    old, new, _other = await _three_assets(manager)
    record = await manager.async_create_asset_replacement(
        old["asset_uuid"],
        new["asset_uuid"],
        reason="failure",
        effective_date=None,
        notes=None,
    )
    flow = await _select(hass, manager, old["asset_uuid"])
    confirmation = await flow.async_step_manage_asset_replacement(
        {
            CONF_REPLACEMENT_UUID: record["replacement_uuid"],
            CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_VOID,
        }
    )
    manager._store.async_save.reset_mock()

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as schedule_reload:
        result = await flow.async_step_confirm_void_replacement(
            {
                CONF_VOID_REASON: "Incorrect mapping",
                CONF_CONFIRM_VOID: True,
            }
        )

    assert confirmation["step_id"] == "confirm_void_replacement"
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert manager.replacement_record(record["replacement_uuid"])["voided_at"]
    manager._store.async_save.assert_awaited_once()
    schedule_reload.assert_called_once()


async def test_correction_workflow_is_one_atomic_save_and_reload(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    old, wrong, correct = await _three_assets(manager)
    record = await manager.async_create_asset_replacement(
        old["asset_uuid"],
        wrong["asset_uuid"],
        reason="failure",
        effective_date=None,
        notes=None,
    )
    flow = await _select(hass, manager, old["asset_uuid"])
    form = await flow.async_step_manage_asset_replacement(
        {
            CONF_REPLACEMENT_UUID: record["replacement_uuid"],
            CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_CORRECT,
        }
    )
    manager._store.async_save.reset_mock()

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as schedule_reload:
        result = await flow.async_step_correct_asset_replacement(
            {
                CONF_PREDECESSOR_ASSET_UUID: old["asset_uuid"],
                CONF_SUCCESSOR_ASSET_UUID: correct["asset_uuid"],
                CONF_REPLACEMENT_REASON: "warranty_rma",
                CONF_VOID_REASON: "Wrong successor",
            }
        )

    assert form["step_id"] == "correct_asset_replacement"
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert manager.active_replacement_successor(old["asset_uuid"])["asset_uuid"] == (
        correct["asset_uuid"]
    )
    manager._store.async_save.assert_awaited_once()
    schedule_reload.assert_called_once()


async def test_replacement_persistence_failure_has_zero_reload(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    old, new, _other = await _three_assets(manager)
    flow = await _select(hass, manager, old["asset_uuid"])
    before = deepcopy(manager._data)
    manager._store.async_save = AsyncMock(side_effect=OSError("save failed"))

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as schedule_reload:
        result = await flow.async_step_replacement_replaced_by(
            {
                CONF_REPLACEMENT_TARGET_ASSET_UUID: new["asset_uuid"],
                CONF_REPLACEMENT_REASON: "failure",
            }
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "persistence_error"}
    assert manager._data == before
    schedule_reload.assert_not_called()
