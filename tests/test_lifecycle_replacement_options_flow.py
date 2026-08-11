"""Home Assistant OptionsFlow tests for lifecycle and physical replacement."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock, Mock, patch

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


async def test_lifecycle_malformed_effective_date_has_specific_flow_error(
    hass: HomeAssistant,
) -> None:
    """Malformed transition dates are not presented as valid dates in the future."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Malformed lifecycle date")
    flow = await _select(hass, manager, asset["asset_uuid"])
    manager._store.async_save.reset_mock()

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as schedule_reload:
        result = await flow.async_step_asset_lifecycle(
            {
                CONF_LIFECYCLE_STATUS: "retired",
                CONF_EFFECTIVE_DATE: "20260810",
            }
        )

    assert result["errors"] == {"base": "invalid_lifecycle_effective_date"}
    manager._store.async_save.assert_not_awaited()
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


async def test_replacement_malformed_effective_date_has_specific_flow_error(
    hass: HomeAssistant,
) -> None:
    """Malformed replacement dates use their stable error and never reload."""
    manager = _manager(hass)
    old, new, _other = await _three_assets(manager)
    flow = await _select(hass, manager, old["asset_uuid"])
    manager._store.async_save.reset_mock()

    with patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as schedule_reload:
        result = await flow.async_step_replacement_replaced_by(
            {
                CONF_REPLACEMENT_TARGET_ASSET_UUID: new["asset_uuid"],
                CONF_REPLACEMENT_REASON: "failure",
                CONF_EFFECTIVE_DATE: "20260810",
            }
        )

    assert result["errors"] == {"base": "invalid_replacement_effective_date"}
    manager._store.async_save.assert_not_awaited()
    schedule_reload.assert_not_called()


async def test_lifecycle_forms_reject_invalid_status_and_unconfirmed_disposal(
    hass: HomeAssistant,
) -> None:
    """The initial form, enum guard, and disposal confirmation keep Store authority."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Lifecycle forms")
    flow = await _select(hass, manager, asset["asset_uuid"])

    initial = await flow.async_step_asset_lifecycle()
    invalid = await flow.async_step_asset_lifecycle(
        {CONF_LIFECYCLE_STATUS: "stored", CONF_NOTES: "Not a lifecycle state"}
    )
    confirmation = await flow.async_step_asset_lifecycle(
        {CONF_LIFECYCLE_STATUS: "disposed"}
    )
    declined = await flow.async_step_confirm_disposed(
        {CONF_CONFIRM_DISPOSED: False}
    )

    assert initial["step_id"] == "asset_lifecycle"
    assert invalid["errors"] == {"base": "invalid_lifecycle_status"}
    assert confirmation["step_id"] == "confirm_disposed"
    assert declined["errors"] == {"base": "confirmation_required"}
    assert manager.asset(asset["asset_uuid"])["lifecycle"]["status"] == "active"


async def test_disposal_confirmation_stale_and_persistence_error_paths(
    hass: HomeAssistant,
) -> None:
    """A disappeared Asset or failed save cannot publish a pending disposal."""
    stale_manager = _manager(hass)
    stale_asset = await stale_manager.async_create_manual_asset(name="Stale disposal")
    stale_flow = await _select(hass, stale_manager, stale_asset["asset_uuid"])
    await stale_flow.async_step_asset_lifecycle(
        {CONF_LIFECYCLE_STATUS: "disposed"}
    )
    stale_manager._data["assets"].pop(stale_asset["asset_uuid"])
    stale_manager._data["lifecycle_events"].clear()
    stale = await stale_flow.async_step_confirm_disposed(
        {CONF_CONFIRM_DISPOSED: True}
    )

    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Failed disposal")
    flow = await _select(hass, manager, asset["asset_uuid"])
    await flow.async_step_asset_lifecycle({CONF_LIFECYCLE_STATUS: "disposed"})
    manager._store.async_save = AsyncMock(side_effect=OSError("save failed"))
    failed = await flow.async_step_confirm_disposed(
        {CONF_CONFIRM_DISPOSED: True}
    )

    assert stale["step_id"] == "manage_asset"
    assert stale["errors"] == {"base": "asset_missing"}
    assert not hasattr(stale_flow, "_pending_lifecycle_update")
    assert failed["errors"] == {"base": "persistence_error"}
    assert manager.asset(asset["asset_uuid"])["lifecycle"]["status"] == "active"


async def test_disposal_confirmation_without_pending_update_returns_to_lifecycle(
    hass: HomeAssistant,
) -> None:
    """A stale confirmation URL safely returns to the current lifecycle form."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="No pending disposal")
    flow = await _select(hass, manager, asset["asset_uuid"])

    result = await flow.async_step_confirm_disposed()

    assert result["step_id"] == "asset_lifecycle"


async def test_replacement_menu_and_initial_forms_cover_current_graph_state(
    hass: HomeAssistant,
) -> None:
    """Replacement menus expose management only when an active record exists."""
    manager = _manager(hass)
    old, new, _other = await _three_assets(manager)
    flow = await _select(hass, manager, old["asset_uuid"])

    empty_menu = await flow.async_step_asset_replacement()
    initial_create = await flow.async_step_replacement_replaced_by()
    record = await manager.async_create_asset_replacement(
        old["asset_uuid"],
        new["asset_uuid"],
        reason="failure",
        effective_date=None,
        notes=None,
    )
    populated_menu = await flow.async_step_asset_replacement()
    initial_manage = await flow.async_step_manage_asset_replacement()

    assert empty_menu["menu_options"] == [
        "replacement_replaces",
        "replacement_replaced_by",
    ]
    assert initial_create["step_id"] == "replacement_replaced_by"
    assert populated_menu["menu_options"][-1] == "manage_asset_replacement"
    assert initial_manage["step_id"] == "manage_asset_replacement"
    record_selector = next(
        validator
        for marker, validator in initial_manage["data_schema"].schema.items()
        if getattr(marker, "schema", marker) == CONF_REPLACEMENT_UUID
    )
    assert record["replacement_uuid"] in {
        option["value"] for option in record_selector.config["options"]
    }


async def test_replacement_selected_asset_stale_paths_return_to_selector(
    hass: HomeAssistant,
) -> None:
    """Every replacement entry point rechecks the selected Asset at submit time."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Selected then removed")
    flow = await _select(hass, manager, asset["asset_uuid"])
    manager._data["assets"].pop(asset["asset_uuid"])
    manager._data["lifecycle_events"].clear()

    menu = await flow.async_step_asset_replacement()
    create = await flow.async_step_replacement_replaced_by()
    manage = await flow.async_step_manage_asset_replacement()
    correct = await flow.async_step_correct_asset_replacement()
    void = await flow.async_step_confirm_void_replacement()

    for result in (menu, create, manage, correct, void):
        assert result["step_id"] == "manage_asset"
        assert result["errors"] == {"base": "asset_missing"}


async def test_manage_replacement_stale_selection_action_and_empty_graph(
    hass: HomeAssistant,
) -> None:
    """The management form rejects stale record/action values and graph changes."""
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

    missing = await flow.async_step_manage_asset_replacement(
        {
            CONF_REPLACEMENT_UUID: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            CONF_REPLACEMENT_ACTION: REPLACEMENT_ACTION_CORRECT,
        }
    )
    invalid_action = await flow.async_step_manage_asset_replacement(
        {
            CONF_REPLACEMENT_UUID: record["replacement_uuid"],
            CONF_REPLACEMENT_ACTION: "delete",
        }
    )
    await manager.async_void_asset_replacement(
        record["replacement_uuid"], void_reason="Graph changed"
    )
    empty = await flow.async_step_manage_asset_replacement()

    assert missing["errors"] == {"base": "replacement_missing"}
    assert invalid_action["errors"] == {"base": "replacement_missing"}
    assert empty["step_id"] == "asset_replacement"


async def test_correction_stale_record_error_and_disappearing_asset_paths(
    hass: HomeAssistant,
) -> None:
    """Correction revalidates the record and never reloads after failure."""
    manager = _manager(hass)
    old, wrong, correct = await _three_assets(manager)
    record = await manager.async_create_asset_replacement(
        old["asset_uuid"],
        wrong["asset_uuid"],
        reason="failure",
        effective_date="2026-08-09",
        notes="Original",
    )
    flow = await _select(hass, manager, old["asset_uuid"])
    flow._pending_replacement_uuid = record["replacement_uuid"]

    initial = await flow.async_step_correct_asset_replacement()
    with patch.object(
        manager,
        "async_correct_asset_replacement",
        AsyncMock(side_effect=OSError("save failed")),
    ), patch.object(hass.config_entries, "async_schedule_reload") as reload:
        failed = await flow.async_step_correct_asset_replacement(
            {
                CONF_PREDECESSOR_ASSET_UUID: old["asset_uuid"],
                CONF_SUCCESSOR_ASSET_UUID: correct["asset_uuid"],
                CONF_REPLACEMENT_REASON: "upgrade",
                CONF_VOID_REASON: "Wrong endpoint",
            }
        )
    assert initial["step_id"] == "correct_asset_replacement"
    assert failed["errors"] == {"base": "asset_store_error"}
    reload.assert_not_called()

    await manager.async_void_asset_replacement(
        record["replacement_uuid"], void_reason="Made stale"
    )
    stale = await flow.async_step_correct_asset_replacement()
    assert stale["errors"] == {"base": "replacement_missing"}

    manager2 = _manager(hass)
    a, b, c = await _three_assets(manager2)
    active = await manager2.async_create_asset_replacement(
        a["asset_uuid"], b["asset_uuid"], reason="failure", effective_date=None, notes=None
    )
    flow2 = await _select(hass, manager2, a["asset_uuid"])
    flow2._pending_replacement_uuid = active["replacement_uuid"]
    with patch.object(manager2, "asset", Mock(side_effect=[a, None])):
        disappeared = await flow2.async_step_correct_asset_replacement(
            {
                CONF_PREDECESSOR_ASSET_UUID: a["asset_uuid"],
                CONF_SUCCESSOR_ASSET_UUID: c["asset_uuid"],
                CONF_REPLACEMENT_REASON: "other",
                CONF_VOID_REASON: "Correct then disappear",
            }
        )
    assert disappeared["step_id"] == "manage_asset"
    assert disappeared["errors"] == {"base": "asset_missing"}


async def test_void_confirmation_guards_errors_and_disappearing_asset(
    hass: HomeAssistant,
) -> None:
    """Void requires confirmation/reason and handles stale and failed mutations."""
    manager = _manager(hass)
    old, new, _other = await _three_assets(manager)
    record = await manager.async_create_asset_replacement(
        old["asset_uuid"], new["asset_uuid"], reason="failure", effective_date=None, notes=None
    )
    flow = await _select(hass, manager, old["asset_uuid"])
    flow._pending_replacement_uuid = record["replacement_uuid"]

    initial = await flow.async_step_confirm_void_replacement()
    unconfirmed = await flow.async_step_confirm_void_replacement(
        {CONF_VOID_REASON: "Reason", CONF_CONFIRM_VOID: False}
    )
    empty_reason = await flow.async_step_confirm_void_replacement(
        {CONF_VOID_REASON: "   ", CONF_CONFIRM_VOID: True}
    )
    with patch.object(
        manager,
        "async_void_asset_replacement",
        AsyncMock(side_effect=OSError("save failed")),
    ):
        failed = await flow.async_step_confirm_void_replacement(
            {CONF_VOID_REASON: "Reason", CONF_CONFIRM_VOID: True}
        )

    assert initial["step_id"] == "confirm_void_replacement"
    assert unconfirmed["errors"] == {"base": "confirmation_required"}
    assert empty_reason["errors"] == {"base": "replacement_void_reason_required"}
    assert failed["errors"] == {"base": "asset_store_error"}

    manager2 = _manager(hass)
    a, b, _c = await _three_assets(manager2)
    active = await manager2.async_create_asset_replacement(
        a["asset_uuid"], b["asset_uuid"], reason="failure", effective_date=None, notes=None
    )
    flow2 = await _select(hass, manager2, a["asset_uuid"])
    flow2._pending_replacement_uuid = active["replacement_uuid"]
    with patch.object(manager2, "asset", Mock(side_effect=[a, None])):
        disappeared = await flow2.async_step_confirm_void_replacement(
            {CONF_VOID_REASON: "Valid", CONF_CONFIRM_VOID: True}
        )
    assert disappeared["step_id"] == "manage_asset"

    flow2._pending_replacement_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    stale = await flow2.async_step_confirm_void_replacement()
    assert stale["errors"] == {"base": "replacement_missing"}


async def test_replacement_create_detects_post_mutation_asset_disappearance(
    hass: HomeAssistant,
) -> None:
    """The completion path does not reload if the selected Asset disappears."""
    manager = _manager(hass)
    old, new, _other = await _three_assets(manager)
    flow = await _select(hass, manager, old["asset_uuid"])

    with patch.object(manager, "asset", Mock(side_effect=[old, new, None])), patch.object(
        hass.config_entries, "async_schedule_reload"
    ) as reload:
        result = await flow.async_step_replacement_replaced_by(
            {
                CONF_REPLACEMENT_TARGET_ASSET_UUID: new["asset_uuid"],
                CONF_REPLACEMENT_REASON: "failure",
            }
        )

    assert result["step_id"] == "manage_asset"
    assert result["errors"] == {"base": "asset_missing"}
    reload.assert_not_called()
