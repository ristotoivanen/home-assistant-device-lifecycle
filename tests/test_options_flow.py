"""OptionsFlow tests for manual Asset creation and management."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle.config_flow import (
    NO_PURCHASE_SELECTION,
    DeviceLifecycleConfigFlow,
    DeviceLifecycleOptionsFlow,
)
from custom_components.device_lifecycle.const import (
    CONFIG_ENTRY_VERSION,
    CONF_ASSET_NAME,
    CONF_ASSET_UUID,
    CONF_CATEGORY,
    CONF_HW_VERSION,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_MODEL_ID,
    CONF_NOTES,
    CONF_PURCHASE_UUID,
    CONF_SERIAL_NUMBER,
    CONF_SW_VERSION,
    DOMAIN,
)
from custom_components.device_lifecycle.models import (
    AssetStoreData,
    PurchaseData,
)
from custom_components.device_lifecycle.storage import (
    AssetStoreError,
    AssetStoreManager,
)

from .conftest import ASSET_UUID, PURCHASE_UUID

MANUAL_ASSET_UUID = "33333333-3333-4333-8333-333333333333"
RUNTIME_ASSET_UUID = "44444444-4444-4444-8444-444444444444"


def _manager(
    hass: HomeAssistant,
    data: AssetStoreData | None = None,
) -> AssetStoreManager:
    """Return an isolated manager with persistent saves mocked."""
    manager = AssetStoreManager(hass)
    if data is not None:
        manager._data = deepcopy(data)
    manager._store.async_save = AsyncMock()
    return manager


def _purchase(
    purchase_uuid: str = PURCHASE_UUID,
    *,
    configured: bool = True,
) -> PurchaseData:
    """Return one valid Purchase available to Asset management."""
    return {
        "purchase_uuid": purchase_uuid,
        "config_subentry_id": "purchase-subentry-id",
        "configured": configured,
        "name": "Purchase first",
        "purchase_date": "2026-08-10",
        "seller": "Example seller",
        "total_price": "199.95",
        "currency": "EUR",
        "receipt_reference": "ORDER-OPTIONS",
        "receipt_url": None,
        "notes": None,
        "asset_uuids": [],
    }


def _store_with_purchase(*, configured: bool = True) -> AssetStoreData:
    """Return an empty-Asset Store containing one Purchase."""
    purchase = _purchase(configured=configured)
    return {
        "next_asset_number": 1,
        "purchases": {PURCHASE_UUID: purchase},
        "assets": {},
        "lifecycle_events": {},
        "replacement_records": {},
    }


def _options_flow(
    hass: HomeAssistant,
    manager: AssetStoreManager,
    *,
    subentries_data: tuple[dict, ...] = (),
) -> tuple[DeviceLifecycleOptionsFlow, MockConfigEntry]:
    """Return a parent config entry and its initialized OptionsFlow object."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="device_lifecycle_main",
        version=CONFIG_ENTRY_VERSION,
        data={},
        options={},
        subentries_data=subentries_data,
    )
    entry.runtime_data = manager
    entry.add_to_hass(hass)
    flow = DeviceLifecycleConfigFlow.async_get_options_flow(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    flow.flow_id = "test-options-flow"
    flow.context = {}
    return flow, entry


def _schema_keys(result: dict) -> set[str]:
    """Return the plain keys exposed by a flow form schema."""
    return {
        str(getattr(marker, "schema", marker))
        for marker in result["data_schema"].schema
    }


def _select_options(result: dict, field: str) -> list[dict[str, str]]:
    """Return dynamic selector options for one form field."""
    for marker, validator in result["data_schema"].schema.items():
        if getattr(marker, "schema", marker) == field:
            return list(validator.config["options"])
    raise AssertionError(f"Missing selector field {field}")


def _full_metadata(name: str = "Shelf bulb") -> dict[str, str]:
    """Return every editable Asset metadata field."""
    return {
        CONF_ASSET_NAME: name,
        CONF_CATEGORY: "Lighting",
        CONF_MANUFACTURER: "Philips",
        CONF_MODEL: "Hue White",
        CONF_MODEL_ID: "LWA001",
        CONF_SERIAL_NUMBER: "SERIAL-OPTIONS",
        CONF_SW_VERSION: "1.2.3",
        CONF_HW_VERSION: "A",
        CONF_NOTES: "Stored on shelf",
    }


async def test_parent_options_flow_opens_asset_menu(
    hass: HomeAssistant,
) -> None:
    """Configure opens the single parent integration Asset menu."""
    flow, entry = _options_flow(hass, _manager(hass))

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert isinstance(flow, DeviceLifecycleOptionsFlow)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == ["quick_add", "manage_asset"]


def _unloaded_parent_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return a registered parent entry that was never set up.

    Deliberately does not set ``entry.runtime_data`` — this mirrors an entry
    that failed setup, was unloaded, or has not finished loading yet
    (0.7.2 WP3 / 072-05).
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="device_lifecycle_main",
        version=CONFIG_ENTRY_VERSION,
        data={},
        options={},
    )
    entry.add_to_hass(hass)
    return entry


async def test_options_flow_init_with_runtime_data_behaves_unchanged(
    hass: HomeAssistant,
) -> None:
    """0.7.2 WP3 / 072-05 Case A: a normally loaded entry is unaffected."""
    flow, entry = _options_flow(hass, _manager(hass))

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.options.async_init(entry.entry_id)

    assert isinstance(flow, DeviceLifecycleOptionsFlow)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == ["quick_add", "manage_asset"]
    reload.assert_not_called()


async def test_options_flow_init_without_runtime_data_aborts_cleanly(
    hass: HomeAssistant,
) -> None:
    """0.7.2 WP3 / 072-05 Case B: an unloaded entry aborts instead of raising.

    Opening the OptionsFlow while ``runtime_data`` is absent must not raise
    an uncaught AttributeError; it must abort cleanly with a translated
    reason, with no Store mutation and no reload.
    """
    entry = _unloaded_parent_entry(hass)

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await hass.config_entries.options.async_init(entry.entry_id)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "entry_not_loaded"
    reload.assert_not_called()


async def test_options_flow_no_step_reachable_after_runtime_data_abort(
    hass: HomeAssistant,
) -> None:
    """0.7.2 WP3 / 072-05 Case C: no manager-dependent step follows the abort."""
    entry = _unloaded_parent_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT

    with pytest.raises(UnknownFlow):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"next_step_id": "manage_asset"},
        )


async def test_uuid_and_dl_id_are_not_editable_fields(
    hass: HomeAssistant,
) -> None:
    """Identity is absent from create/edit metadata forms."""
    manager = _manager(hass)
    flow, _entry = _options_flow(hass, manager)

    created = await manager.async_create_manual_asset(name="Identity protected")
    await flow.async_step_manage_asset({CONF_ASSET_UUID: created["asset_uuid"]})
    edit_form = await flow.async_step_edit_asset_metadata()

    assert CONF_ASSET_UUID not in _schema_keys(edit_form)
    assert "asset_id" not in _schema_keys(edit_form)


async def test_manage_asset_lists_manual_legacy_and_runtime_assets_by_uuid(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manage Asset is origin-agnostic and labels immutable UUID choices."""
    data = deepcopy(asset_store_data)
    runtime_asset = deepcopy(data["assets"][ASSET_UUID])
    runtime_asset.update(
        {
            "asset_uuid": RUNTIME_ASSET_UUID,
            "asset_id": "DL0008",
            "name": "Runtime-created Asset",
            "purchase_uuid": None,
            "ha_device_refs": [
                {"device_id": "runtime-ha-device", "role": "primary"}
            ],
        }
    )
    runtime_asset["field_sources"].pop("purchase_uuid", None)
    data["assets"][RUNTIME_ASSET_UUID] = runtime_asset
    data["next_asset_number"] = 9
    manager = _manager(hass, data)
    monkeypatch.setattr(
        "custom_components.device_lifecycle.storage.uuid4",
        lambda: UUID(MANUAL_ASSET_UUID),
    )
    manual = await manager.async_create_manual_asset(name="Manual Asset")
    flow, _entry = _options_flow(hass, manager)

    form = await flow.async_step_manage_asset()
    options = _select_options(form, CONF_ASSET_UUID)

    assert [option["value"] for option in options] == [
        ASSET_UUID,
        RUNTIME_ASSET_UUID,
        MANUAL_ASSET_UUID,
    ]
    assert [option["label"] for option in options] == [
        "DL0007 — Workshop device",
        "DL0008 — Runtime-created Asset",
        "DL0009 — Manual Asset",
    ]

    selected = await flow.async_step_manage_asset(
        {CONF_ASSET_UUID: manual["asset_uuid"]}
    )
    assert selected["type"] is FlowResultType.MENU
    assert flow._selected_asset_uuid == MANUAL_ASSET_UUID


async def test_missing_asset_and_validation_failures_return_flow_errors(
    hass: HomeAssistant,
) -> None:
    """Missing identity and invalid metadata never crash or fabricate Assets."""
    manager = _manager(hass)
    flow, _entry = _options_flow(hass, manager)

    missing = await flow.async_step_manage_asset(
        {CONF_ASSET_UUID: MANUAL_ASSET_UUID}
    )
    assert missing["type"] is FlowResultType.FORM
    assert missing["errors"] == {"base": "asset_missing"}
    assert manager.asset_count == 0


async def test_edit_existing_ha_linked_asset_clears_metadata_without_identity_change(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Metadata edits and clears preserve UUID, DL ID and external HA reference."""
    manager = _manager(hass, asset_store_data)
    flow, _entry = _options_flow(hass, manager)
    before = manager.asset(ASSET_UUID)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: ASSET_UUID})
    edit_input = {
        CONF_ASSET_NAME: "User workshop device",
        CONF_CATEGORY: before["category"],
        CONF_MANUFACTURER: before["manufacturer"],
        CONF_MODEL: "User model",
        CONF_MODEL_ID: before["model_id"],
        CONF_SERIAL_NUMBER: "",
        CONF_SW_VERSION: before["sw_version"],
        CONF_HW_VERSION: before["hw_version"],
        CONF_NOTES: before["notes"],
    }

    result = await flow.async_step_edit_asset_metadata(edit_input)

    updated = manager.asset(ASSET_UUID)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert updated["asset_uuid"] == before["asset_uuid"]
    assert updated["asset_id"] == before["asset_id"]
    assert updated["purchase_uuid"] == before["purchase_uuid"]
    assert updated["ha_device_refs"] == before["ha_device_refs"]
    assert updated["name"] == "User workshop device"
    assert updated["model"] == "User model"
    assert updated["serial_number"] is None
    assert updated["field_sources"]["name"] == "user"
    assert updated["field_sources"]["model"] == "user"
    assert updated["field_sources"]["serial_number"] == "user"


async def test_user_metadata_survives_later_ha_refresh(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    existing_entry,
) -> None:
    """Options edits remain protected by existing HA provenance behavior."""
    manager = _manager(hass, asset_store_data)
    flow, _entry = _options_flow(hass, manager)
    before = manager.asset(ASSET_UUID)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: ASSET_UUID})
    await flow.async_step_edit_asset_metadata(
        {
            CONF_ASSET_NAME: "User-owned name",
            CONF_CATEGORY: before["category"],
            CONF_MANUFACTURER: before["manufacturer"],
            CONF_MODEL: "User-owned model",
            CONF_MODEL_ID: before["model_id"],
            CONF_SERIAL_NUMBER: before["serial_number"],
            CONF_SW_VERSION: before["sw_version"],
            CONF_HW_VERSION: before["hw_version"],
            CONF_NOTES: before["notes"],
        }
    )
    registry = Mock()
    registry.async_get.return_value = Mock(
        name_by_user="HA replacement name",
        name="HA replacement name",
        manufacturer="HA manufacturer",
        model="HA replacement model",
        model_id="HA model ID",
        serial_number="HA serial",
        sw_version="HA software",
        hw_version="HA hardware",
    )

    with patch(
        "custom_components.device_lifecycle.storage.dr.async_get",
        return_value=registry,
    ):
        await manager.async_reconcile_entry(existing_entry)

    refreshed = manager.asset(ASSET_UUID)
    assert refreshed["name"] == "User-owned name"
    assert refreshed["model"] == "User-owned model"
    assert refreshed["ha_device_refs"] == before["ha_device_refs"]


async def test_metadata_true_canonical_noop_writes_and_reloads_nothing(
    hass: HomeAssistant,
) -> None:
    """0.7.2 WP2 / F-2: resubmitting identical, already user-owned metadata is
    a true canonical no-op — zero Store write and zero reload."""
    metadata = _full_metadata(name="Shelf bulb")
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(
        name=metadata[CONF_ASSET_NAME],
        category=metadata[CONF_CATEGORY],
        manufacturer=metadata[CONF_MANUFACTURER],
        model=metadata[CONF_MODEL],
        model_id=metadata[CONF_MODEL_ID],
        serial_number=metadata[CONF_SERIAL_NUMBER],
        sw_version=metadata[CONF_SW_VERSION],
        hw_version=metadata[CONF_HW_VERSION],
        notes=metadata[CONF_NOTES],
    )
    assert all(
        asset["field_sources"][field] == "user"
        for field in (
            "name",
            "category",
            "manufacturer",
            "model",
            "model_id",
            "serial_number",
            "sw_version",
            "hw_version",
            "notes",
        )
    )
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset["asset_uuid"]})
    manager._store.async_save.reset_mock()

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await flow.async_step_edit_asset_metadata(metadata)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert manager.asset(asset["asset_uuid"]) == asset
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


def _identical_metadata_input(before: dict) -> dict[str, Any]:
    """Return a full metadata-editor echo of every field's current value.

    Mirrors exactly what the real form resubmits when the user touches
    nothing: `add_suggested_values_to_schema` prefills every field from the
    persisted asset, so a genuinely untouched submission still carries all
    nine keys (0.7.2 WP5 / 072-07).
    """
    return {
        CONF_ASSET_NAME: before["name"],
        CONF_CATEGORY: before["category"],
        CONF_MANUFACTURER: before["manufacturer"],
        CONF_MODEL: before["model"],
        CONF_MODEL_ID: before["model_id"],
        CONF_SERIAL_NUMBER: before["serial_number"],
        CONF_SW_VERSION: before["sw_version"],
        CONF_HW_VERSION: before["hw_version"],
        CONF_NOTES: before["notes"],
    }


async def test_metadata_untouched_ha_owned_field_stays_ha_owned_and_is_a_noop(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """0.7.2 WP5 / 072-07 Case A: an untouched HA-owned field must NOT be
    claimed as user-owned merely because the editor echoes its value back.

    Relocates the manager-layer semantic previously (and wrongly) asserted
    here at the flow level — see
    `test_manager_explicit_identical_resubmission_still_claims_ha_owned_field`
    in test_storage_mutations.py for the preserved manager-API behavior.
    """
    manager = _manager(hass, asset_store_data)
    before = manager.asset(ASSET_UUID)
    assert before["field_sources"]["manufacturer"] == "home_assistant"
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: ASSET_UUID})
    manager._store.async_save.reset_mock()

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await flow.async_step_edit_asset_metadata(
            _identical_metadata_input(before)
        )

    updated = manager.asset(ASSET_UUID)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert updated["manufacturer"] == before["manufacturer"]
    assert updated["field_sources"]["manufacturer"] == "home_assistant"
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


async def test_metadata_untouched_purchase_owned_field_stays_purchase_owned(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """0.7.2 WP5 / 072-07 Case B: an untouched purchase-owned field must NOT
    be claimed as user-owned by an unmodified resubmission."""
    data = deepcopy(asset_store_data)
    data["assets"][ASSET_UUID]["field_sources"]["notes"] = "purchase"
    manager = _manager(hass, data)
    before = manager.asset(ASSET_UUID)
    assert before["field_sources"]["notes"] == "purchase"
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: ASSET_UUID})
    manager._store.async_save.reset_mock()

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await flow.async_step_edit_asset_metadata(
            _identical_metadata_input(before)
        )

    updated = manager.asset(ASSET_UUID)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert updated["notes"] == before["notes"]
    assert updated["field_sources"]["notes"] == "purchase"
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


async def test_metadata_untouched_empty_field_does_not_become_user_owned(
    hass: HomeAssistant,
) -> None:
    """0.7.2 WP5 / 072-07 Case C: an untouched empty/absent field must not
    gain a user-owned `field_sources` entry from a blank resubmission."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Bare asset")
    assert "category" not in asset["field_sources"]
    assert asset["category"] is None
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset["asset_uuid"]})
    manager._store.async_save.reset_mock()

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await flow.async_step_edit_asset_metadata(
            _identical_metadata_input(asset)
        )

    updated = manager.asset(asset["asset_uuid"])
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert updated["category"] is None
    assert "category" not in updated["field_sources"]
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


async def test_metadata_user_changes_one_ha_owned_field_value(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """0.7.2 WP5 / 072-07 Case D: a genuinely new value for an HA-owned field
    is a real, deliberate edit — exactly one Store write/reload."""
    manager = _manager(hass, asset_store_data)
    before = manager.asset(ASSET_UUID)
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: ASSET_UUID})
    manager._store.async_save.reset_mock()

    edit_input = _identical_metadata_input(before)
    edit_input[CONF_MANUFACTURER] = "New manufacturer"

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await flow.async_step_edit_asset_metadata(edit_input)

    updated = manager.asset(ASSET_UUID)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert updated["manufacturer"] == "New manufacturer"
    assert updated["field_sources"]["manufacturer"] == "user"
    manager._store.async_save.assert_awaited_once()
    reload.assert_called_once()


async def test_metadata_user_clears_one_populated_ha_owned_field(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """0.7.2 WP5 / 072-07 Case E: deliberately clearing a populated HA-owned
    field is a real edit — the clear persists and becomes user-owned."""
    manager = _manager(hass, asset_store_data)
    before = manager.asset(ASSET_UUID)
    assert before["serial_number"] == "SERIAL-1"
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: ASSET_UUID})
    manager._store.async_save.reset_mock()

    edit_input = _identical_metadata_input(before)
    edit_input[CONF_SERIAL_NUMBER] = ""

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await flow.async_step_edit_asset_metadata(edit_input)

    updated = manager.asset(ASSET_UUID)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert updated["serial_number"] is None
    assert updated["field_sources"]["serial_number"] == "user"
    # Every other untouched field keeps its previous provenance.
    assert updated["field_sources"]["manufacturer"] == "home_assistant"
    manager._store.async_save.assert_awaited_once()
    reload.assert_called_once()


async def test_metadata_only_the_changed_fields_become_user_owned(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """0.7.2 WP5 / 072-07 Case F: editing several fields at once still leaves
    every untouched field's provenance alone — one canonical mutation."""
    manager = _manager(hass, asset_store_data)
    before = manager.asset(ASSET_UUID)
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: ASSET_UUID})
    manager._store.async_save.reset_mock()

    edit_input = _identical_metadata_input(before)
    edit_input[CONF_MANUFACTURER] = "New manufacturer"
    edit_input[CONF_MODEL] = "New model"

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await flow.async_step_edit_asset_metadata(edit_input)

    updated = manager.asset(ASSET_UUID)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert updated["manufacturer"] == "New manufacturer"
    assert updated["model"] == "New model"
    assert updated["field_sources"]["manufacturer"] == "user"
    assert updated["field_sources"]["model"] == "user"
    # Untouched HA-owned fields are unaffected by the two real edits.
    assert updated["field_sources"]["name"] == "home_assistant"
    assert updated["field_sources"]["model_id"] == "home_assistant"
    assert updated["field_sources"]["serial_number"] == "home_assistant"
    assert updated["field_sources"]["sw_version"] == "home_assistant"
    assert updated["field_sources"]["hw_version"] == "home_assistant"
    manager._store.async_save.assert_awaited_once()
    reload.assert_called_once()


async def test_metadata_blank_name_is_rejected_and_leaves_no_trace(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Final audit Finding 1 / WP5 Case C: a whitespace-only Name submission
    through the real metadata editor flow is rejected as a normal form
    error, not silently accepted, not a crash, and not a Store write."""
    manager = _manager(hass, asset_store_data)
    before = manager.asset(ASSET_UUID)
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: ASSET_UUID})
    manager._store.async_save.reset_mock()

    edit_input = _identical_metadata_input(before)
    edit_input[CONF_ASSET_NAME] = "   "

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await flow.async_step_edit_asset_metadata(edit_input)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "asset_store_error"}
    assert manager.asset(ASSET_UUID) == before
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


async def test_changed_metadata_fields_itself_rejects_blank_name(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Final audit Finding 1 / WP5 Case C (isolation): `_changed_metadata_fields`
    is the exact new WP5 boundary that must reject a blank Name on its own,
    before ever reaching the manager or `_validate_store_data`'s independent
    structural safety net — asserted directly so a regression in this
    specific helper cannot hide behind that separate defense-in-depth check.
    """
    manager = _manager(hass, asset_store_data)
    before = manager.asset(ASSET_UUID)
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: ASSET_UUID})

    edit_input = _identical_metadata_input(before)
    edit_input[CONF_ASSET_NAME] = "   "

    with pytest.raises(AssetStoreError, match="Asset name is required"):
        flow._changed_metadata_fields(before, edit_input)


async def test_assign_and_clear_purchase_preserves_manual_asset_identity(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Later Purchase assignment and clearing never replace Asset identity."""
    manager = _manager(hass, _store_with_purchase())
    monkeypatch.setattr(
        "custom_components.device_lifecycle.storage.uuid4",
        lambda: UUID(MANUAL_ASSET_UUID),
    )
    asset = await manager.async_create_manual_asset(name="Assign later")
    identity = (asset["asset_uuid"], asset["asset_id"])
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset["asset_uuid"]})

    await flow.async_step_change_asset_purchase(
        {CONF_PURCHASE_UUID: PURCHASE_UUID}
    )

    assigned = manager.asset(MANUAL_ASSET_UUID)
    assert assigned["purchase_uuid"] == PURCHASE_UUID
    assert assigned["field_sources"]["purchase_uuid"] == "user"
    assert manager.purchase(PURCHASE_UUID)["asset_uuids"] == [MANUAL_ASSET_UUID]

    clear_flow, _entry = _options_flow(hass, manager)
    await clear_flow.async_step_manage_asset({CONF_ASSET_UUID: MANUAL_ASSET_UUID})
    await clear_flow.async_step_change_asset_purchase(
        {CONF_PURCHASE_UUID: NO_PURCHASE_SELECTION}
    )

    cleared = manager.asset(MANUAL_ASSET_UUID)
    assert (cleared["asset_uuid"], cleared["asset_id"]) == identity
    assert cleared["purchase_uuid"] is None
    assert manager.purchase(PURCHASE_UUID)["asset_uuids"] == []


async def test_purchase_true_canonical_noop_writes_and_reloads_nothing(
    hass: HomeAssistant,
) -> None:
    """0.7.2 WP2 / F-2: resubmitting the same already-assigned Purchase is a
    true canonical no-op — zero Store write and zero reload."""
    manager = _manager(hass, _store_with_purchase())
    asset = await manager.async_create_manual_asset(name="Purchase no-op")
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset["asset_uuid"]})
    await flow.async_step_change_asset_purchase(
        {CONF_PURCHASE_UUID: PURCHASE_UUID}
    )
    assert (
        manager.asset(asset["asset_uuid"])["field_sources"]["purchase_uuid"]
        == "user"
    )

    same_flow, _entry = _options_flow(hass, manager)
    await same_flow.async_step_manage_asset({CONF_ASSET_UUID: asset["asset_uuid"]})
    manager._store.async_save.reset_mock()

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await same_flow.async_step_change_asset_purchase(
            {CONF_PURCHASE_UUID: PURCHASE_UUID}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()


async def test_historical_purchase_is_visible_but_not_a_new_target(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Only an Asset's current historical relationship appears in its choices."""
    data = deepcopy(asset_store_data)
    data["purchases"][PURCHASE_UUID]["configured"] = False
    data["assets"][ASSET_UUID]["field_sources"]["purchase_uuid"] = "user"
    manager = _manager(hass, data)
    flow, _entry = _options_flow(hass, manager)
    before = deepcopy(manager._data)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: ASSET_UUID})

    current_form = await flow.async_step_change_asset_purchase()
    current_options = _select_options(current_form, CONF_PURCHASE_UUID)

    assert [option["value"] for option in current_options] == [
        NO_PURCHASE_SELECTION,
        PURCHASE_UUID,
    ]
    assert "Historical" in current_options[1]["label"]
    assert manager._data == before

    manual = await manager.async_create_manual_asset(name="No historical link")
    new_flow, _entry = _options_flow(hass, manager)
    await new_flow.async_step_manage_asset({CONF_ASSET_UUID: manual["asset_uuid"]})
    invalid = await new_flow.async_step_change_asset_purchase(
        {CONF_PURCHASE_UUID: PURCHASE_UUID}
    )

    assert invalid["type"] is FlowResultType.FORM
    assert invalid["errors"] == {"base": "invalid_purchase"}
    assert manager.asset(manual["asset_uuid"])["purchase_uuid"] is None
