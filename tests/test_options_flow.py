"""OptionsFlow tests for manual Asset creation and management."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
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
    CONF_CURRENCY,
    CONF_DEVICE_IDS,
    CONF_HW_VERSION,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_MODEL_ID,
    CONF_NOTES,
    CONF_PURCHASE_NAME,
    CONF_PURCHASE_UUID,
    CONF_SERIAL_NUMBER,
    CONF_SW_VERSION,
    CONF_WARRANTY_TYPE,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DOMAIN,
    SUBENTRY_TYPE_PURCHASE,
    WARRANTY_NONE,
)
from custom_components.device_lifecycle.models import (
    AssetStoreData,
    PurchaseData,
)
from custom_components.device_lifecycle.storage import (
    AssetStoreError,
    AssetStoreManager,
)

from .conftest import ASSET_UUID, DEVICE_ID, PURCHASE_UUID

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
    assert result["menu_options"] == ["create_manual_asset", "manage_asset"]


async def test_create_manual_asset_menu_path(
    hass: HomeAssistant,
) -> None:
    """The Home Assistant flow manager dispatches the create menu action."""
    manager = _manager(hass)
    _flow, entry = _options_flow(hass, manager)
    initial = await hass.config_entries.options.async_init(entry.entry_id)

    create_form = await hass.config_entries.options.async_configure(
        initial["flow_id"],
        {"next_step_id": "create_manual_asset"},
    )
    completed = await hass.config_entries.options.async_configure(
        initial["flow_id"],
        {
            CONF_ASSET_NAME: "Flow manager Asset",
            CONF_PURCHASE_UUID: NO_PURCHASE_SELECTION,
        },
    )

    assert create_form["type"] is FlowResultType.FORM
    assert create_form["step_id"] == "create_manual_asset"
    assert completed["type"] is FlowResultType.CREATE_ENTRY
    assert manager.assets()[0]["name"] == "Flow manager Asset"


async def test_create_name_only_allocates_one_identity_and_no_subentry(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Name-only creation allocates one UUID/DL ID and no config subentry."""
    manager = _manager(hass)
    uuid_factory = Mock(return_value=UUID(MANUAL_ASSET_UUID))
    monkeypatch.setattr(
        "custom_components.device_lifecycle.storage.uuid4",
        uuid_factory,
    )
    flow, entry = _options_flow(hass, manager)
    subentry_count = len(entry.subentries)

    result = await flow.async_step_create_manual_asset(
        {
            CONF_ASSET_NAME: "Shelf bulb",
            CONF_PURCHASE_UUID: NO_PURCHASE_SELECTION,
        }
    )

    asset = manager.asset(MANUAL_ASSET_UUID)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["description"] == "asset_created"
    assert result["description_placeholders"]["asset_id"] == "DL0001"
    assert uuid_factory.call_count == 1
    assert asset["asset_id"] == "DL0001"
    assert asset["purchase_uuid"] is None
    assert asset["ha_device_refs"] == []
    assert asset["deployment_state"] == DEPLOYMENT_STATE_NOT_DEPLOYED
    assert manager._data["next_asset_number"] == 2
    assert len(entry.subentries) == subentry_count == 0


async def test_create_manual_asset_with_full_metadata(
    hass: HomeAssistant,
) -> None:
    """Every approved physical metadata field reaches Asset Core."""
    manager = _manager(hass)
    flow, _entry = _options_flow(hass, manager)
    user_input = {
        **_full_metadata(),
        CONF_PURCHASE_UUID: NO_PURCHASE_SELECTION,
    }

    await flow.async_step_create_manual_asset(user_input)

    asset = manager.assets()[0]
    for field, value in _full_metadata().items():
        assert asset[field] == value
        assert asset["field_sources"][field] == "user"
    assert asset["purchase_uuid"] is None


async def test_create_manual_asset_with_configured_purchase(
    hass: HomeAssistant,
) -> None:
    """Creation uses the transactional Purchase relationship API."""
    manager = _manager(hass, _store_with_purchase())
    flow, _entry = _options_flow(hass, manager)

    await flow.async_step_create_manual_asset(
        {
            CONF_ASSET_NAME: "Purchased spare",
            CONF_PURCHASE_UUID: PURCHASE_UUID,
        }
    )

    asset = manager.assets()[0]
    assert asset["purchase_uuid"] == PURCHASE_UUID
    assert asset["field_sources"]["purchase_uuid"] == "user"
    assert manager.purchase(PURCHASE_UUID)["asset_uuids"] == [asset["asset_uuid"]]
    assert manager._store.async_save.await_count == 2


async def test_uuid_and_dl_id_are_not_editable_fields(
    hass: HomeAssistant,
) -> None:
    """Identity is absent from create/edit metadata forms."""
    manager = _manager(hass)
    flow, _entry = _options_flow(hass, manager)

    create_form = await flow.async_step_create_manual_asset()
    created = await manager.async_create_manual_asset(name="Identity protected")
    await flow.async_step_manage_asset({CONF_ASSET_UUID: created["asset_uuid"]})
    edit_form = await flow.async_step_edit_asset_metadata()

    assert CONF_ASSET_UUID not in _schema_keys(create_form)
    assert "asset_id" not in _schema_keys(create_form)
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
    invalid = await flow.async_step_create_manual_asset(
        {
            CONF_ASSET_NAME: "",
            CONF_PURCHASE_UUID: NO_PURCHASE_SELECTION,
        }
    )

    assert missing["type"] is FlowResultType.FORM
    assert missing["errors"] == {"base": "asset_missing"}
    assert invalid["type"] is FlowResultType.FORM
    assert invalid["errors"] == {"base": "asset_store_error"}
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


async def test_purchase_first_manual_asset_workflow_end_to_end(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty Purchase receives a manual Asset without any HA device."""
    manager = _manager(hass)
    uuid_factory = Mock(
        side_effect=[UUID(PURCHASE_UUID), UUID(MANUAL_ASSET_UUID)]
    )
    monkeypatch.setattr(
        "custom_components.device_lifecycle.storage.uuid4",
        uuid_factory,
    )
    subentries_data = (
        {
            "data": {
                CONF_DEVICE_IDS: [],
                CONF_PURCHASE_NAME: "Purchase first",
                CONF_CURRENCY: "EUR",
                CONF_WARRANTY_TYPE: WARRANTY_NONE,
            },
            "subentry_type": SUBENTRY_TYPE_PURCHASE,
            "title": "Purchase first",
            "unique_id": None,
        },
    )
    flow, entry = _options_flow(
        hass,
        manager,
        subentries_data=subentries_data,
    )

    await manager.async_reconcile_entry(entry)
    purchase_uuid_before = manager.purchases()[0]["purchase_uuid"]
    counter_before = manager._data["next_asset_number"]
    subentry_count = len(entry.subentries)

    result = await flow.async_step_create_manual_asset(
        {
            CONF_ASSET_NAME: "Received equipment",
            CONF_PURCHASE_UUID: purchase_uuid_before,
        }
    )

    asset = manager.asset(MANUAL_ASSET_UUID)
    purchase = manager.purchase(purchase_uuid_before)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert purchase_uuid_before == PURCHASE_UUID
    assert uuid_factory.call_count == 2
    assert asset["asset_id"] == "DL0001"
    assert manager._data["next_asset_number"] == counter_before + 1
    assert asset["purchase_uuid"] == purchase_uuid_before
    assert asset["ha_device_refs"] == []
    assert purchase["asset_uuids"] == [MANUAL_ASSET_UUID]
    assert len(entry.subentries) == subentry_count == 1


async def test_purchase_assignment_error_is_flow_safe_and_does_not_duplicate_asset(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry after a relationship error reuses the already-created Asset UUID."""
    manager = _manager(hass, _store_with_purchase())
    uuid_factory = Mock(return_value=UUID(MANUAL_ASSET_UUID))
    monkeypatch.setattr(
        "custom_components.device_lifecycle.storage.uuid4",
        uuid_factory,
    )
    flow, _entry = _options_flow(hass, manager)
    user_input = {
        CONF_ASSET_NAME: "Retry relationship",
        CONF_PURCHASE_UUID: PURCHASE_UUID,
    }

    with patch.object(
        manager,
        "async_set_asset_purchase",
        new=AsyncMock(
            side_effect=AssetStoreError("conflicting Purchase relationship")
        ),
    ):
        failed = await flow.async_step_create_manual_asset(user_input)

    assert failed["type"] is FlowResultType.FORM
    assert failed["errors"] == {"base": "purchase_conflict"}
    assert manager.asset_count == 1
    assert uuid_factory.call_count == 1

    completed = await flow.async_step_create_manual_asset(user_input)

    assert completed["type"] is FlowResultType.CREATE_ENTRY
    assert manager.asset_count == 1
    assert uuid_factory.call_count == 1
    assert manager.purchase(PURCHASE_UUID)["asset_uuids"] == [MANUAL_ASSET_UUID]
