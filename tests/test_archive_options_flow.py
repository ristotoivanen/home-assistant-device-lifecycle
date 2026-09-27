"""Archive and Restore in the Asset management OptionsFlow (WP15).

The flow previews blockers with the manager's read-only
``archive_blockers``, saves Runtime a stopped writer still holds, and then
lets ``async_archive_asset`` decide under its lock. It never undeploys,
removes Runtime tracking, or reloads: WP14's publish refresh updates
entities and Repairs, and the same loaded manager serves the next step.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.device_lifecycle.config_flow import (
    NO_REPLACEMENT_SELECTION,
    NOT_SELECTED,
)
from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_CATEGORY,
    CONF_CONFIRM_ARCHIVE,
    CONF_CONFIRM_RESTORE,
    CONF_CONFIRM_VOID,
    CONF_DEVICE_ID,
    CONF_REPLACEMENT_UUID,
    CONF_VOID_REASON,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DOMAIN,
    SUBENTRY_TYPE_RUNTIME,
)
from custom_components.device_lifecycle.exposure import asset_id_unique_id
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    AssetStoreError,
    AssetStoreManager,
)

from .conftest import ASSET_UUID, DEVICE_ID, capture_reloads
from .test_exposure_options_reload import _verified_store_readback
from .test_options_flow import _manager, _options_flow
from .test_runtime import _Clock, _sensor
from .test_runtime_checkpoint import _add
from .test_stale_device_registry_references import _device, _load, _refs

SECOND = "44444444-4444-4444-8444-444444444444"


def _data(asset_store_data: AssetStoreData, *, archived: bool = False) -> dict:
    data: dict[str, Any] = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    asset["deployment_state"] = DEPLOYMENT_STATE_NOT_DEPLOYED
    asset["runtime"]["total_seconds"] = "1000"
    if archived:
        asset["archived_at"] = "2026-09-26T12:34:56.123456+00:00"
    return data


def _values(options: list[Any]) -> set[str]:
    return {option["value"] for option in options}


async def _hub(flow: Any) -> dict[str, Any]:
    return await flow.async_step_manage_asset({CONF_ASSET_UUID: ASSET_UUID})


def _selector_values(form: dict[str, Any]) -> list[str]:
    field = next(iter(form["data_schema"].schema.values()))
    return [option["value"] for option in field.config["options"]]


# --- Active hub and Archive ------------------------------------------------------


async def test_archive_from_the_hub_needs_no_reload_and_keeps_the_manager(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    flow, entry = _options_flow(hass, manager)
    hub = await _hub(flow)
    assert "confirm_archive_asset" in hub["menu_options"]

    confirm = await flow.async_step_confirm_archive_asset()
    assert confirm["step_id"] == "confirm_archive_asset"
    assert (
        "Nothing currently prevents archiving."
        in (confirm["description_placeholders"]["blockers"])
    )
    unconfirmed = await flow.async_step_confirm_archive_asset(
        {CONF_CONFIRM_ARCHIVE: False}
    )
    assert unconfirmed["errors"] == {
        CONF_CONFIRM_ARCHIVE: "archive_confirmation_required"
    }
    manager._store.async_save.assert_not_awaited()

    with capture_reloads(hass) as reloads:
        archived = await flow.async_step_confirm_archive_asset(
            {CONF_CONFIRM_ARCHIVE: True}
        )
    reloads.assert_not_called()
    assert entry.runtime_data is manager
    assert archived["step_id"] == "archived_asset"
    assert archived["menu_options"] == [
        "confirm_restore_asset",
        "maintenance_history",
        "archived_assets",
    ]
    assert "Device archived" in archived["description_placeholders"]["result"]
    assert manager.asset_archived(ASSET_UUID)
    # Current management no longer offers it; the archived selector does.
    assert ASSET_UUID not in _values(flow._asset_choices())
    assert ASSET_UUID not in _values(flow._replacement_target_choices(SECOND))
    assert ASSET_UUID not in _values(flow._quick_replacement_choices())
    assert ASSET_UUID in _values(flow._archived_choices())
    # A stale hub for it routes to the archived view instead.
    assert (await flow.async_step_manage_asset_menu())["step_id"] == "archived_asset"
    assert (await flow.async_step_confirm_archive_asset())["step_id"] == (
        "archived_asset"
    )


async def test_archive_that_already_happened_elsewhere_is_reported_not_failed(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    flow, entry = _options_flow(hass, manager)
    await _hub(flow)
    await flow.async_step_confirm_archive_asset()
    await manager.async_archive_asset(entry, ASSET_UUID)  # another flow
    manager._store.async_save.reset_mock()

    result = await flow.async_step_confirm_archive_asset({CONF_CONFIRM_ARCHIVE: True})

    assert result["step_id"] == "archived_asset"
    assert "already archived" in result["description_placeholders"]["result"]
    manager._store.async_save.assert_not_awaited()


# --- Blockers ------------------------------------------------------------------------


async def _blocked(flow: Any, manager: AssetStoreManager, code: str) -> dict[str, Any]:
    before = deepcopy(manager._data)
    preview = await flow.async_step_confirm_archive_asset()
    result = await flow.async_step_confirm_archive_asset({CONF_CONFIRM_ARCHIVE: True})
    assert result["step_id"] == "confirm_archive_asset"
    assert result["errors"] == {"base": code}
    assert manager._data == before
    assert not manager.asset_archived(ASSET_UUID)
    return preview


async def test_a_deployed_asset_is_explained_and_never_undeployed(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data = _data(asset_store_data)
    data["assets"][ASSET_UUID]["deployment_state"] = DEPLOYMENT_STATE_DEPLOYED
    manager = _manager(hass, data)
    flow, _entry = _options_flow(hass, manager)
    await _hub(flow)

    preview = await _blocked(flow, manager, "archive_asset_deployed")

    assert "installed" in preview["description_placeholders"]["blockers"]
    assert manager.asset(ASSET_UUID)["deployment_state"] == DEPLOYMENT_STATE_DEPLOYED  # type: ignore[index]
    manager._store.async_save.assert_not_awaited()


async def test_configured_runtime_tracking_is_explained_and_kept(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    runtime = {
        "data": {CONF_DEVICE_ID: DEVICE_ID},
        "subentry_type": SUBENTRY_TYPE_RUNTIME,
        "title": "Runtime",
        "unique_id": None,
    }
    flow, entry = _options_flow(hass, manager, subentries_data=(runtime,))
    subentries = {key: dict(value.data) for key, value in entry.subentries.items()}
    await _hub(flow)

    preview = await _blocked(flow, manager, "archive_runtime_configured")

    assert (
        "Runtime tracking is configured"
        in (preview["description_placeholders"]["blockers"])
    )
    assert {key: dict(value.data) for key, value in entry.subentries.items()} == (
        subentries
    )
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1000)


async def test_a_binding_in_progress_blocks_until_released(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    flow, entry = _options_flow(hass, manager)
    await _hub(flow)
    release = await manager.async_reserve_runtime_binding(entry, ASSET_UUID)

    preview = await _blocked(flow, manager, "archive_runtime_binding_in_progress")
    assert "being set up" in preview["description_placeholders"]["blockers"]

    release()
    again = await flow.async_step_confirm_archive_asset()
    assert "Nothing currently prevents" in again["description_placeholders"]["blockers"]


async def test_active_runtime_is_explained_and_left_running(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    writer = _sensor(hass, manager, _Clock(0))
    await _add(hass, writer)
    flow, _entry = _options_flow(hass, manager)
    await _hub(flow)

    preview = await _blocked(flow, manager, "archive_runtime_writer_active")

    assert "still running" in preview["description_placeholders"]["blockers"]
    assert writer.runtime_durability().observing is True


async def test_unresolved_runtime_is_explained_and_never_cleared(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    manager.mark_runtime_unresolved(ASSET_UUID)
    flow, _entry = _options_flow(hass, manager)
    await _hub(flow)

    preview = await _blocked(flow, manager, "archive_runtime_unresolved")

    assert "Reload Device Lifecycle" in preview["description_placeholders"]["blockers"]
    assert ASSET_UUID in manager._runtime_unresolved


async def _stopped_writer_with_pending(
    hass: HomeAssistant, manager: AssetStoreManager
) -> Any:
    clock = _Clock(0)
    writer = _sensor(hass, manager, clock)
    await _add(hass, writer)
    writer._active_since = 0.0
    clock.value = 10
    manager._store.async_save = AsyncMock(side_effect=OSError("disk"))
    with pytest.raises(AssetStoreError):
        await writer.async_retire_runtime()
    clock.value = 500  # time after the quiesce is never counted
    return writer


async def test_stopped_runtime_is_saved_first_then_archived(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    writer = await _stopped_writer_with_pending(hass, manager)
    flow, entry = _options_flow(hass, manager)
    await _hub(flow)
    preview = await flow.async_step_confirm_archive_asset()
    assert "not saved yet" in preview["description_placeholders"]["blockers"]

    manager._store.async_save = AsyncMock()
    with capture_reloads(hass) as reloads:
        result = await flow.async_step_confirm_archive_asset(
            {CONF_CONFIRM_ARCHIVE: True}
        )

    reloads.assert_not_called()
    assert result["step_id"] == "archived_asset"
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
    assert writer._pending == []
    assert manager.asset_archived(ASSET_UUID)
    assert entry.runtime_data is manager


async def test_a_failed_runtime_save_archives_nothing_and_keeps_the_runtime(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    writer = await _stopped_writer_with_pending(hass, manager)
    flow, _entry = _options_flow(hass, manager)
    await _hub(flow)

    with capture_reloads(hass) as reloads:
        result = await flow.async_step_confirm_archive_asset(
            {CONF_CONFIRM_ARCHIVE: True}
        )

    reloads.assert_not_called()
    assert result["errors"] == {"base": "runtime_checkpoint_failed"}
    assert not manager.asset_archived(ASSET_UUID)
    assert [item.delta for item in writer._pending] == [Decimal("10.0")]
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1000)


async def test_a_blocker_appearing_after_the_preview_is_reported(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """TOCTOU: the preview showed nothing; the manager's check decides."""
    manager = _manager(hass, _data(asset_store_data))
    flow, entry = _options_flow(hass, manager)
    await _hub(flow)
    preview = await flow.async_step_confirm_archive_asset()
    assert (
        "Nothing currently prevents"
        in (preview["description_placeholders"]["blockers"])
    )
    before = deepcopy(manager._data)
    await manager.async_reserve_runtime_binding(entry, ASSET_UUID)

    result = await flow.async_step_confirm_archive_asset({CONF_CONFIRM_ARCHIVE: True})

    assert result["errors"] == {"base": "archive_runtime_binding_in_progress"}
    assert manager._data == before
    assert manager.asset(ASSET_UUID)["archived_at"] is None  # type: ignore[index]
    manager._store.async_save.assert_not_awaited()


# --- Archived devices -------------------------------------------------------------------


async def test_the_archived_selector_lists_only_archived_devices(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    flow, _entry = _options_flow(hass, manager)
    init = await flow.async_step_init()
    assert init["menu_options"] == ["quick_add", "manage_asset", "archived_assets"]

    empty = await flow.async_step_archived_assets()
    assert empty["errors"] == {"base": "no_archived_assets"}
    assert _selector_values(empty) == [NOT_SELECTED]

    manager._data["assets"][ASSET_UUID]["archived_at"] = "2026-09-26T12:34:56+00:00"
    second = await manager.async_create_manual_asset(name="Active heater")
    listed = await flow.async_step_archived_assets()
    assert listed["errors"] == {}
    assert _selector_values(listed) == [NOT_SELECTED, ASSET_UUID]
    assert second["asset_uuid"] not in _selector_values(listed)

    placeholder = await flow.async_step_archived_assets({CONF_ASSET_UUID: NOT_SELECTED})
    assert placeholder["errors"] == {CONF_ASSET_UUID: "archived_asset_required"}
    active = await flow.async_step_archived_assets(
        {CONF_ASSET_UUID: second["asset_uuid"]}
    )
    assert active["errors"] == {"base": "asset_missing"}


async def test_the_archived_view_offers_only_restore_history_and_back(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    second = await manager.async_create_manual_asset(name="New heater")
    await manager.async_create_asset_replacement(
        ASSET_UUID,
        second["asset_uuid"],
        reason="upgrade",
        effective_date=None,
        notes=None,
    )
    manager._data["assets"][ASSET_UUID]["archived_at"] = "2026-09-26T12:34:56+00:00"
    flow, _entry = _options_flow(hass, manager)

    view = await flow.async_step_archived_assets({CONF_ASSET_UUID: ASSET_UUID})

    assert view["step_id"] == "archived_asset"
    assert view["menu_options"] == [
        "confirm_restore_asset",
        "archived_void_replacement",
        "maintenance_history",
        "archived_assets",
    ]
    facts = view["description_placeholders"]["facts"]
    assert "DL0007" in facts
    assert ASSET_UUID not in str(view["description_placeholders"])


async def test_a_replacement_of_an_archived_device_can_be_voided_in_place(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    second = await manager.async_create_manual_asset(name="New heater")
    record = await manager.async_create_asset_replacement(
        ASSET_UUID,
        second["asset_uuid"],
        reason="upgrade",
        effective_date=None,
        notes=None,
    )
    manager._data["assets"][ASSET_UUID]["archived_at"] = "2026-09-26T12:34:56+00:00"
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_archived_assets({CONF_ASSET_UUID: ASSET_UUID})

    choose = await flow.async_step_archived_void_replacement()
    assert choose["step_id"] == "archived_void_replacement"
    missing = await flow.async_step_archived_void_replacement(
        {CONF_REPLACEMENT_UUID: "not-a-record"}
    )
    assert missing["errors"] == {"base": "replacement_missing"}
    confirm = await flow.async_step_archived_void_replacement(
        {CONF_REPLACEMENT_UUID: record["replacement_uuid"]}
    )
    assert confirm["step_id"] == "confirm_void_replacement"
    with capture_reloads(hass) as reloads:
        done = await flow.async_step_confirm_void_replacement(
            {CONF_VOID_REASON: "Recorded by mistake", CONF_CONFIRM_VOID: True}
        )

    reloads.assert_not_called()
    assert done["step_id"] == "archived_asset"
    assert done["menu_options"] == [
        "confirm_restore_asset",
        "maintenance_history",
        "archived_assets",
    ]
    assert manager.replacement_record(record["replacement_uuid"])["voided_at"]  # type: ignore[index]
    assert manager.asset_archived(ASSET_UUID)


async def test_the_archived_view_shows_the_category_and_no_empty_history(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data, archived=True))
    manager._data["assets"][ASSET_UUID][CONF_CATEGORY] = "Heating"
    flow, _entry = _options_flow(hass, manager)

    view = await flow.async_step_archived_assets({CONF_ASSET_UUID: ASSET_UUID})

    assert "Category: Heating" in view["description_placeholders"]["facts"]
    assert view["menu_options"] == [
        "confirm_restore_asset",
        "maintenance_history",
        "archived_assets",
    ]
    manager._data["assets"][ASSET_UUID][CONF_CATEGORY] = "  "
    blank = await flow.async_step_archived_asset()
    assert "Category" not in blank["description_placeholders"]["facts"]
    # With no Replacement left to void, the history step returns to the view.
    result = await flow.async_step_archived_void_replacement()
    assert result["step_id"] == "archived_asset"


async def test_a_failed_restore_save_keeps_the_device_archived(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data, archived=True))
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_archived_assets({CONF_ASSET_UUID: ASSET_UUID})
    manager._store.async_save = AsyncMock(side_effect=OSError("disk"))

    with capture_reloads(hass) as reloads:
        result = await flow.async_step_confirm_restore_asset(
            {CONF_CONFIRM_RESTORE: True}
        )

    reloads.assert_not_called()
    assert result["step_id"] == "confirm_restore_asset"
    assert set(result["errors"]) == {"base"}
    assert manager.asset_archived(ASSET_UUID)


async def test_restore_returns_the_same_device_to_active_management(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data, archived=True))
    flow, entry = _options_flow(hass, manager)
    await flow.async_step_archived_assets({CONF_ASSET_UUID: ASSET_UUID})

    confirm = await flow.async_step_confirm_restore_asset()
    assert confirm["step_id"] == "confirm_restore_asset"
    unconfirmed = await flow.async_step_confirm_restore_asset(
        {CONF_CONFIRM_RESTORE: False}
    )
    assert unconfirmed["errors"] == {
        CONF_CONFIRM_RESTORE: "restore_confirmation_required"
    }
    with capture_reloads(hass) as reloads:
        hub = await flow.async_step_confirm_restore_asset({CONF_CONFIRM_RESTORE: True})

    reloads.assert_not_called()
    assert entry.runtime_data is manager
    assert hub["step_id"] == "manage_asset_menu"
    assert "restored" in hub["description_placeholders"]["result"]
    assert "DL0007" in hub["description_placeholders"]["asset"]
    assert not manager.asset_archived(ASSET_UUID)
    assert ASSET_UUID in _values(flow._asset_choices())
    assert ASSET_UUID not in _values(flow._archived_choices())
    assert entry.subentries == {}
    assert NO_REPLACEMENT_SELECTION in _values(flow._quick_replacement_choices())


async def test_restore_of_a_device_restored_elsewhere_is_reported_not_failed(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data, archived=True))
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_archived_assets({CONF_ASSET_UUID: ASSET_UUID})
    await flow.async_step_confirm_restore_asset()
    await manager.async_restore_asset(ASSET_UUID)  # another flow
    manager._store.async_save.reset_mock()

    result = await flow.async_step_confirm_restore_asset({CONF_CONFIRM_RESTORE: True})

    assert result["step_id"] == "manage_asset_menu"
    assert (
        "already in active management" in (result["description_placeholders"]["result"])
    )
    manager._store.async_save.assert_not_awaited()
    # Rendering the confirmation again for an active device goes to its hub.
    assert (await flow.async_step_confirm_restore_asset())["step_id"] == (
        "manage_asset_menu"
    )
    assert (await flow.async_step_archived_asset())["step_id"] == "manage_asset_menu"


async def test_missing_devices_route_to_a_valid_selector(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    flow, _entry = _options_flow(hass, manager)
    flow._selected_asset_uuid = "99999999-9999-4999-8999-999999999999"
    for step in (
        flow.async_step_archived_asset,
        flow.async_step_archived_void_replacement,
        flow.async_step_confirm_restore_asset,
    ):
        result = await step()
        assert result["step_id"] == "archived_assets"
        assert result["errors"] == {"base": "asset_missing"}
    archive = await flow.async_step_confirm_archive_asset()
    assert archive["step_id"] == "manage_asset"


# --- A loaded entry, end to end -------------------------------------------------------------


@pytest.mark.real_reload
async def test_archive_and_restore_through_home_assistant_without_a_reload(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    data = _data(asset_store_data)
    device = _device(hass, device_registry, "archive-ui", data["assets"][ASSET_UUID])
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(device.id)
    entry = await _load(hass, hass_storage, data)  # type: ignore[arg-type]
    manager = entry.runtime_data
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, asset_id_unique_id(ASSET_UUID)
    )
    assert entity_id is not None
    entries = {
        item.unique_id: (item.id, item.entity_id, item.disabled_by)
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    }

    with _verified_store_readback(hass_storage), capture_reloads(hass) as reloads:
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        flow_id = flow["flow_id"]
        await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": "manage_asset"}
        )
        await hass.config_entries.options.async_configure(
            flow_id, {CONF_ASSET_UUID: ASSET_UUID}
        )
        confirm = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": "confirm_archive_asset"}
        )
        assert confirm["step_id"] == "confirm_archive_asset"
        archived = await hass.config_entries.options.async_configure(
            flow_id, {CONF_CONFIRM_ARCHIVE: True}
        )
        await hass.async_block_till_done()
        assert archived["type"] is FlowResultType.MENU
        assert archived["step_id"] == "archived_asset"
        assert hass.states.get(entity_id).attributes["archived"] is True  # type: ignore[union-attr]

        restore = await hass.config_entries.options.async_configure(
            flow_id, {"next_step_id": "confirm_restore_asset"}
        )
        assert restore["step_id"] == "confirm_restore_asset"
        hub = await hass.config_entries.options.async_configure(
            flow_id, {CONF_CONFIRM_RESTORE: True}
        )
        await hass.async_block_till_done()
        assert hub["step_id"] == "manage_asset_menu"
        assert hass.states.get(entity_id).attributes["archived"] is False  # type: ignore[union-attr]

    reloads.assert_not_called()
    assert entry.runtime_data is manager
    assert {
        item.unique_id: (item.id, item.entity_id, item.disabled_by)
        for item in er.async_entries_for_config_entry(registry, entry.entry_id)
    } == entries
    stored = hass_storage["device_lifecycle.assets"]["data"]["assets"][ASSET_UUID]
    assert stored["archived_at"] is None
    assert stored["deployment_state"] == DEPLOYMENT_STATE_NOT_DEPLOYED
