"""OptionsFlow coverage for atomic Quick Asset Entry."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock, Mock
from uuid import UUID

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle.config_flow import (
    NO_PURCHASE_SELECTION,
    NO_REPLACEMENT_SELECTION,
    QUICK_SECTION_DETAILS,
    QUICK_SECTION_IDENTITY,
    QUICK_SECTION_LIFECYCLE,
    QUICK_SECTION_RELATIONSHIPS,
    QUICK_SECTION_WARRANTY,
    DeviceLifecycleConfigFlow,
    DeviceLifecycleOptionsFlow,
)
from custom_components.device_lifecycle.const import (
    CONFIG_ENTRY_VERSION,
    CONF_ASSET_NAME,
    CONF_CONFIRM_QUICK_ADD,
    CONF_DEPLOYMENT_STATE,
    CONF_DEVICE_ID,
    CONF_EFFECTIVE_DATE,
    CONF_HA_AREA_ID,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_NOTES,
    CONF_PURCHASE_UUID,
    CONF_REPLACEMENT_REASON,
    CONF_REPLACEMENT_TARGET_ASSET_UUID,
    CONF_RETIRE_PREDECESSOR,
    CONF_UNDEPLOY_PREDECESSOR,
    CONF_WARRANTY_TYPE,
    CONF_WARRANTY_UNTIL,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DOMAIN,
    LIFECYCLE_STATUS_ACTIVE,
    LIFECYCLE_STATUS_DISPOSED,
    WARRANTY_MANUAL,
    WARRANTY_NONE,
    WARRANTY_ONE_YEAR,
    WARRANTY_TWO_YEARS,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    AssetStoreError,
    AssetStoreManager,
)

from .conftest import PURCHASE_UUID

QUICK_UUID = "77777777-7777-4777-8777-777777777777"
DEVICE_ID = "quick-add-device"


def _empty_store() -> AssetStoreData:
    """Return one empty canonical Store 3.1 payload."""
    return {
        "next_asset_number": 1,
        "purchases": {},
        "assets": {},
        "lifecycle_events": {},
        "replacement_records": {},
    }


def _store_with_purchase(
    *,
    configured: bool = True,
    purchase_date: str | None = "2026-08-07",
) -> AssetStoreData:
    """Return an empty Store with one selectable Purchase."""
    data = _empty_store()
    data["purchases"][PURCHASE_UUID] = {
        "purchase_uuid": PURCHASE_UUID,
        "config_subentry_id": "purchase-entry",
        "configured": configured,
        "name": "Quick purchase",
        "purchase_date": purchase_date,
        "seller": None,
        "total_price": None,
        "currency": "EUR",
        "receipt_reference": None,
        "receipt_url": None,
        "notes": None,
        "asset_uuids": [],
    }
    return data


def _manager(
    hass: HomeAssistant,
    data: AssetStoreData | None = None,
) -> AssetStoreManager:
    """Return an initialized in-memory manager with counted saves."""
    manager = AssetStoreManager(hass)
    manager._data = deepcopy(data or _empty_store())
    manager._store.async_save = AsyncMock()
    return manager


def _flow(
    hass: HomeAssistant,
    manager: AssetStoreManager,
) -> tuple[DeviceLifecycleOptionsFlow, MockConfigEntry]:
    """Return a direct parent OptionsFlow and its loaded entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="device_lifecycle_main",
        version=CONFIG_ENTRY_VERSION,
        data={},
        options={},
    )
    entry.runtime_data = manager
    entry.add_to_hass(hass)
    flow = DeviceLifecycleConfigFlow.async_get_options_flow(entry)
    flow.hass = hass
    flow.handler = entry.entry_id
    flow.flow_id = "quick-add-options-flow"
    flow.context = {}
    return flow, entry


def _details(
    name: str = "Quick device",
    *,
    lifecycle_status: str = LIFECYCLE_STATUS_ACTIVE,
    deployment_state: str = DEPLOYMENT_STATE_NOT_DEPLOYED,
    purchase_uuid: str = NO_PURCHASE_SELECTION,
    warranty_type: str = WARRANTY_NONE,
    warranty_until: str | None = None,
    predecessor_uuid: str = NO_REPLACEMENT_SELECTION,
    area_id: str | None = None,
) -> dict[str, dict[str, object]]:
    """Return section-shaped Quick Add details."""
    lifecycle: dict[str, object] = {
        "lifecycle_status": lifecycle_status,
        CONF_DEPLOYMENT_STATE: deployment_state,
    }
    if area_id is not None:
        lifecycle[CONF_HA_AREA_ID] = area_id
    warranty: dict[str, object] = {CONF_WARRANTY_TYPE: warranty_type}
    if warranty_until is not None:
        warranty[CONF_WARRANTY_UNTIL] = warranty_until
    return {
        QUICK_SECTION_IDENTITY: {CONF_ASSET_NAME: name},
        QUICK_SECTION_DETAILS: {},
        QUICK_SECTION_LIFECYCLE: lifecycle,
        QUICK_SECTION_WARRANTY: warranty,
        QUICK_SECTION_RELATIONSHIPS: {
            CONF_PURCHASE_UUID: purchase_uuid,
            CONF_REPLACEMENT_TARGET_ASSET_UUID: predecessor_uuid,
        },
    }


def _external_device(
    hass: HomeAssistant,
    *,
    device_id: str = DEVICE_ID,
    entry_type: dr.DeviceEntryType | None = None,
    domain: str = "test",
) -> dr.DeviceEntry:
    """Register one externally owned HA device."""
    owner = MockConfigEntry(domain=domain, entry_id=f"owner-{device_id}")
    owner.add_to_hass(hass)
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={(domain, device_id)},
        name="HA lamp",
        manufacturer="Acme",
        model="Light 1",
        sw_version="2.0",
        entry_type=entry_type,
    )


async def test_init_and_source_menus(hass: HomeAssistant) -> None:
    """Configure exposes Add device and its two source choices."""
    flow, _entry = _flow(hass, _manager(hass))

    initial = await flow.async_step_init()
    sources = await flow.async_step_quick_add()

    assert initial["menu_options"] == ["quick_add", "manage_asset"]
    assert sources["menu_options"] == ["quick_add_from_ha", "quick_add_manual"]


async def test_manual_quick_add_is_confirmed_and_committed_once(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Manual Quick Add has no pre-confirm writes and one atomic commit/reload."""
    manager = _manager(hass)
    flow, entry = _flow(hass, manager)
    monkeypatch.setattr(
        "custom_components.device_lifecycle.config_flow.uuid4",
        Mock(return_value=UUID(QUICK_UUID)),
    )
    reload_mock = Mock()
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", reload_mock)
    quick_create = AsyncMock(wraps=manager.async_quick_create_asset)
    monkeypatch.setattr(manager, "async_quick_create_asset", quick_create)

    form = await flow.async_step_quick_add_manual()
    confirm = await flow.async_step_quick_add_details(_details())

    assert form["step_id"] == "quick_add_details"
    assert set(form["data_schema"].schema) == {
        QUICK_SECTION_IDENTITY,
        QUICK_SECTION_DETAILS,
        QUICK_SECTION_LIFECYCLE,
        QUICK_SECTION_WARRANTY,
        QUICK_SECTION_RELATIONSHIPS,
    }
    assert confirm["step_id"] == "quick_add_confirm"
    assert manager._store.async_save.await_count == 0
    assert manager.assets() == []

    declined = await flow.async_step_quick_add_confirm(
        {CONF_CONFIRM_QUICK_ADD: False}
    )
    assert declined["errors"] == {"base": "confirmation_required"}
    assert manager._store.async_save.await_count == 0

    result = await flow.async_step_quick_add_confirm(
        {CONF_CONFIRM_QUICK_ADD: True}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == entry.options == {}
    assert manager._store.async_save.await_count == 1
    assert quick_create.await_count == 1
    assert reload_mock.call_count == 1
    asset = manager.asset(QUICK_UUID)
    assert asset is not None
    assert asset["asset_id"] == "DL0001"
    assert asset["deployment_state"] == DEPLOYMENT_STATE_NOT_DEPLOYED
    assert asset["field_sources"] == {
        CONF_ASSET_NAME: "user",
        CONF_DEPLOYMENT_STATE: "user",
    }


async def test_ha_prefill_edit_and_clear_provenance(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged, edited, and cleared HA suggested fields retain exact ownership."""
    manager = _manager(hass)
    flow, _entry = _flow(hass, manager)
    device = _external_device(hass)
    monkeypatch.setattr(
        "custom_components.device_lifecycle.config_flow.uuid4",
        Mock(return_value=UUID(QUICK_UUID)),
    )
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", Mock())

    details_form = await flow.async_step_quick_add_from_ha(
        {CONF_DEVICE_ID: device.id}
    )
    submitted = _details(deployment_state=DEPLOYMENT_STATE_DEPLOYED)
    submitted[QUICK_SECTION_IDENTITY].update(
        {
            CONF_ASSET_NAME: "HA lamp",
            CONF_MANUFACTURER: "Changed maker",
            CONF_MODEL: "",
        }
    )
    await flow.async_step_quick_add_details(submitted)
    await flow.async_step_quick_add_confirm({CONF_CONFIRM_QUICK_ADD: True})

    assert details_form["step_id"] == "quick_add_details"
    asset = manager.asset(QUICK_UUID)
    assert asset is not None
    assert asset["ha_device_refs"] == [{"device_id": device.id, "role": "primary"}]
    assert asset["manufacturer"] == "Changed maker"
    assert asset["model"] is None
    assert asset["field_sources"]["name"] == "home_assistant"
    assert asset["field_sources"]["manufacturer"] == "user"
    assert asset["field_sources"]["model"] == "user"


async def test_ha_device_validation_rejects_invalid_candidates(
    hass: HomeAssistant,
) -> None:
    """Selection rejects missing, service, software, and already-owned devices."""
    manager = _manager(hass)
    flow, _entry = _flow(hass, manager)

    missing = await flow.async_step_quick_add_from_ha(
        {CONF_DEVICE_ID: "missing"}
    )
    service = _external_device(
        hass,
        device_id="service",
        entry_type=dr.DeviceEntryType.SERVICE,
    )
    service_result = await flow.async_step_quick_add_from_ha(
        {CONF_DEVICE_ID: service.id}
    )
    software = _external_device(hass, device_id="software", domain="hacs")
    software_result = await flow.async_step_quick_add_from_ha(
        {CONF_DEVICE_ID: software.id}
    )
    own_entry = MockConfigEntry(domain=DOMAIN)
    own_entry.add_to_hass(hass)
    own = dr.async_get(hass).async_get_or_create(
        config_entry_id=own_entry.entry_id,
        identifiers={(DOMAIN, "own")},
        name="Projection",
    )
    own_result = await flow.async_step_quick_add_from_ha(
        {CONF_DEVICE_ID: own.id}
    )

    assert missing["errors"] == {"base": "device_missing"}
    assert service_result["errors"] == {"base": "service_device_not_allowed"}
    assert software_result["errors"] == {"base": "non_physical_device_not_allowed"}
    assert own_result["errors"] == {
        "base": "device_lifecycle_device_not_allowed"
    }


async def test_details_validation_and_disposed_confirmation(
    hass: HomeAssistant,
) -> None:
    """Details keep date/Area errors local and clearly review disposed state."""
    manager = _manager(hass)
    flow, _entry = _flow(hass, manager)
    await flow.async_step_quick_add_manual()

    invalid_date = _details()
    invalid_date[QUICK_SECTION_LIFECYCLE][CONF_EFFECTIVE_DATE] = "not-a-date"
    date_result = await flow.async_step_quick_add_details(invalid_date)
    assert date_result["errors"] == {
        "base": "invalid_lifecycle_effective_date"
    }

    area = ar.async_get(hass).async_create("Kitchen")
    invalid_area = await flow.async_step_quick_add_details(
        _details(area_id=area.id)
    )
    assert invalid_area["errors"] == {"base": "invalid_area"}

    disposed = await flow.async_step_quick_add_details(
        _details(lifecycle_status=LIFECYCLE_STATUS_DISPOSED)
    )
    assert disposed["step_id"] == "quick_add_confirm"
    assert "disposed" in disposed["description_placeholders"]["disposed_warning"]


async def test_warranty_calculation_and_stale_purchase_error_locality(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calculated warranty is reviewed and stale Purchase failures return to details."""
    data = _empty_store()
    data["purchases"][PURCHASE_UUID] = {
        "purchase_uuid": PURCHASE_UUID,
        "config_subentry_id": "purchase-entry",
        "configured": True,
        "name": "Leap purchase",
        "purchase_date": "2024-02-29",
        "seller": None,
        "total_price": None,
        "currency": "EUR",
        "receipt_reference": None,
        "receipt_url": None,
        "notes": None,
        "asset_uuids": [],
    }
    manager = _manager(hass, data)
    flow, _entry = _flow(hass, manager)
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", Mock())
    await flow.async_step_quick_add_manual()

    confirm = await flow.async_step_quick_add_details(
        _details(
            purchase_uuid=PURCHASE_UUID,
            warranty_type=WARRANTY_TWO_YEARS,
        )
    )
    assert "2026-02-28" in confirm["description_placeholders"]["warranty"]

    manager._data["purchases"][PURCHASE_UUID]["purchase_date"] = "2024-03-01"
    retry = await flow.async_step_quick_add_confirm(
        {CONF_CONFIRM_QUICK_ADD: True}
    )
    assert retry["step_id"] == "quick_add_details"
    assert retry["errors"] == {"base": "purchase_changed"}
    assert manager._store.async_save.await_count == 0


async def test_manual_warranty_without_purchase(hass: HomeAssistant) -> None:
    """Manual warranty is available without a Purchase relationship."""
    flow, _entry = _flow(hass, _manager(hass))
    await flow.async_step_quick_add_manual()

    result = await flow.async_step_quick_add_details(
        _details(
            warranty_type=WARRANTY_MANUAL,
            warranty_until="2028-08-10",
        )
    )

    assert result["step_id"] == "quick_add_confirm"
    assert "2028-08-10" in result["description_placeholders"]["warranty"]


async def test_replacement_review_defaults_and_duplicate_name_labels(
    hass: HomeAssistant,
) -> None:
    """Predecessors use UUID values, name-first labels, and meaningful defaults."""
    manager = _manager(hass)
    first = await manager.async_create_manual_asset(name="Same name")
    second = await manager.async_create_manual_asset(name="Same name")
    flow, _entry = _flow(hass, manager)
    await flow.async_step_quick_add_manual()
    details_form = await flow.async_step_quick_add_details()
    relationships = details_form["data_schema"].schema[
        QUICK_SECTION_RELATIONSHIPS
    ].schema.schema
    target_selector = next(
        validator
        for marker, validator in relationships.items()
        if getattr(marker, "schema", marker)
        == CONF_REPLACEMENT_TARGET_ASSET_UUID
    )
    options = target_selector.config["options"]

    replacement = await flow.async_step_quick_add_details(
        _details(
            deployment_state=DEPLOYMENT_STATE_DEPLOYED,
            predecessor_uuid=first["asset_uuid"],
        )
    )

    assert {option["value"] for option in options} >= {
        first["asset_uuid"],
        second["asset_uuid"],
    }
    duplicate_labels = [
        option["label"] for option in options if option["value"] != "__no_replacement__"
    ]
    assert duplicate_labels == [
        f"Same name · {first['asset_id']}",
        f"Same name · {second['asset_id']}",
    ]
    assert replacement["step_id"] == "quick_add_replacement"


async def test_replacement_commit_and_stale_predecessor_return_to_review(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacement commit is atomic and stale predecessor state stays local."""
    manager = _manager(hass)
    predecessor = await manager.async_create_manual_asset(name="Old device")
    predecessor = await manager.async_set_asset_deployment(
        predecessor["asset_uuid"],
        deployment_state=DEPLOYMENT_STATE_DEPLOYED,
    )
    flow, _entry = _flow(hass, manager)
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", Mock())
    await flow.async_step_quick_add_manual()
    await flow.async_step_quick_add_details(
        _details(
            deployment_state=DEPLOYMENT_STATE_DEPLOYED,
            predecessor_uuid=predecessor["asset_uuid"],
        )
    )
    confirm = await flow.async_step_quick_add_replacement(
        {
            CONF_REPLACEMENT_REASON: "failure",
            CONF_EFFECTIVE_DATE: "2026-08-10",
            CONF_NOTES: "Failed in service",
            CONF_RETIRE_PREDECESSOR: True,
            CONF_UNDEPLOY_PREDECESSOR: True,
        }
    )
    assert confirm["step_id"] == "quick_add_confirm"
    assert confirm["description_placeholders"]["predecessor_name"] == "Old device"
    assert "Retired" in confirm["description_placeholders"][
        "predecessor_lifecycle"
    ]

    await manager.async_set_asset_lifecycle(
        predecessor["asset_uuid"],
        "lost",
        effective_date=None,
        notes=None,
    )
    retry = await flow.async_step_quick_add_confirm(
        {CONF_CONFIRM_QUICK_ADD: True}
    )
    assert retry["step_id"] == "quick_add_replacement"
    assert retry["errors"] == {"base": "predecessor_changed"}


async def test_manager_failure_has_no_reload_and_uuid_is_reused(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistence retry remains on confirmation with one proposed UUID."""
    manager = _manager(hass)
    flow, _entry = _flow(hass, manager)
    uuid_factory = Mock(return_value=UUID(QUICK_UUID))
    monkeypatch.setattr(
        "custom_components.device_lifecycle.config_flow.uuid4",
        uuid_factory,
    )
    reload_mock = Mock()
    monkeypatch.setattr(hass.config_entries, "async_schedule_reload", reload_mock)
    await flow.async_step_quick_add_manual()
    await flow.async_step_quick_add_details(_details())
    requests = []

    async def _fail(request):
        requests.append(request)
        raise AssetStoreError("failed", code="persistence_error")

    monkeypatch.setattr(manager, "async_quick_create_asset", _fail)
    first = await flow.async_step_quick_add_confirm(
        {CONF_CONFIRM_QUICK_ADD: True}
    )
    second = await flow.async_step_quick_add_confirm(
        {CONF_CONFIRM_QUICK_ADD: True}
    )

    assert first["step_id"] == second["step_id"] == "quick_add_confirm"
    assert first["errors"] == {"base": "persistence_error"}
    assert requests[0].asset_uuid == requests[1].asset_uuid == QUICK_UUID
    assert uuid_factory.call_count == 1
    assert reload_mock.call_count == 0


async def test_device_and_area_are_revalidated_before_manager_call(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """External registry references are checked again at final confirmation."""
    manager = _manager(hass)
    flow, _entry = _flow(hass, manager)
    device = _external_device(hass)
    area = ar.async_get(hass).async_create("Office")
    await flow.async_step_quick_add_from_ha({CONF_DEVICE_ID: device.id})
    await flow.async_step_quick_add_details(
        _details(
            deployment_state=DEPLOYMENT_STATE_DEPLOYED,
            area_id=area.id,
        )
    )
    quick_create = AsyncMock()
    monkeypatch.setattr(manager, "async_quick_create_asset", quick_create)

    ar.async_get(hass).async_delete(area.id)
    result = await flow.async_step_quick_add_confirm(
        {CONF_CONFIRM_QUICK_ADD: True}
    )

    assert result["step_id"] == "quick_add_details"
    assert result["errors"] == {"base": "invalid_area"}
    quick_create.assert_not_awaited()


async def test_flow_manager_accepts_section_payload(hass: HomeAssistant) -> None:
    """Home Assistant validates and flattens real section-shaped user data."""
    manager = _manager(hass)
    _direct_flow, entry = _flow(hass, manager)
    initial = await hass.config_entries.options.async_init(entry.entry_id)
    source_menu = await hass.config_entries.options.async_configure(
        initial["flow_id"],
        {"next_step_id": "quick_add"},
    )
    details = await hass.config_entries.options.async_configure(
        source_menu["flow_id"],
        {"next_step_id": "quick_add_manual"},
    )
    confirm = await hass.config_entries.options.async_configure(
        details["flow_id"],
        _details(),
    )

    assert source_menu["step_id"] == "quick_add"
    assert details["step_id"] == "quick_add_details"
    assert confirm["step_id"] == "quick_add_confirm"
    assert manager._store.async_save.await_count == 0


@pytest.mark.parametrize(
    ("details_update", "error_key"),
    [
        ({CONF_ASSET_NAME: ""}, "invalid_quick_create_request"),
        ({CONF_ASSET_NAME: 7}, "invalid_quick_create_request"),
        ({"lifecycle_status": "bad"}, "invalid_lifecycle_status"),
        ({CONF_EFFECTIVE_DATE: "2999-01-01"}, "lifecycle_date_in_future"),
        ({CONF_EFFECTIVE_DATE: 7}, "invalid_lifecycle_effective_date"),
        ({CONF_DEPLOYMENT_STATE: "bad"}, "invalid_deployment_state"),
        ({"installed_date": "2026-8-1"}, "invalid_installed_date"),
        ({CONF_HA_AREA_ID: "missing-area"}, "invalid_area"),
        ({CONF_WARRANTY_TYPE: "bad"}, "invalid_warranty_type"),
        ({CONF_WARRANTY_TYPE: WARRANTY_MANUAL}, "manual_warranty_date_required"),
        (
            {
                CONF_WARRANTY_TYPE: WARRANTY_MANUAL,
                CONF_WARRANTY_UNTIL: "bad",
            },
            "invalid_warranty_date",
        ),
        (
            {CONF_WARRANTY_TYPE: WARRANTY_ONE_YEAR},
            "purchase_date_required_for_warranty",
        ),
        (
            {CONF_REPLACEMENT_TARGET_ASSET_UUID: "missing-asset"},
            "asset_missing",
        ),
    ],
)
async def test_quick_details_errors_remain_on_details(
    hass: HomeAssistant,
    details_update: dict[str, object],
    error_key: str,
) -> None:
    """Metadata, date, domain, and relationship errors remain editable locally."""
    flow, _entry = _flow(hass, _manager(hass))
    await flow.async_step_quick_add_manual()
    values = _details()
    values[QUICK_SECTION_IDENTITY].update(details_update)
    values[QUICK_SECTION_LIFECYCLE].update(details_update)
    values[QUICK_SECTION_WARRANTY].update(details_update)
    values[QUICK_SECTION_RELATIONSHIPS].update(details_update)

    result = await flow.async_step_quick_add_details(values)

    assert result["step_id"] == "quick_add_details"
    assert result["errors"] == {"base": error_key}


@pytest.mark.parametrize(
    ("configured", "purchase_date", "error_key"),
    [
        (False, "2026-08-07", "purchase_not_configured"),
        (True, None, "purchase_date_required_for_warranty"),
        (True, "2026-8-7", "purchase_date_required_for_warranty"),
    ],
)
async def test_purchase_and_warranty_reference_errors_remain_on_details(
    hass: HomeAssistant,
    configured: bool,
    purchase_date: str | None,
    error_key: str,
) -> None:
    """Only current configured Purchases with usable dates support calculation."""
    manager = _manager(
        hass,
        _store_with_purchase(
            configured=configured,
            purchase_date=purchase_date,
        ),
    )
    flow, _entry = _flow(hass, manager)
    await flow.async_step_quick_add_manual()

    result = await flow.async_step_quick_add_details(
        _details(
            purchase_uuid=PURCHASE_UUID,
            warranty_type=WARRANTY_ONE_YEAR,
        )
    )

    assert result["step_id"] == "quick_add_details"
    assert result["errors"] == {"base": error_key}


@pytest.mark.parametrize(
    ("replacement_input", "error_key"),
    [
        (
            {
                CONF_REPLACEMENT_REASON: "bad",
                CONF_RETIRE_PREDECESSOR: True,
                CONF_UNDEPLOY_PREDECESSOR: True,
            },
            "invalid_replacement_reason",
        ),
        (
            {
                CONF_REPLACEMENT_REASON: "failure",
                CONF_EFFECTIVE_DATE: "bad",
                CONF_RETIRE_PREDECESSOR: True,
                CONF_UNDEPLOY_PREDECESSOR: True,
            },
            "invalid_replacement_effective_date",
        ),
        (
            {
                CONF_REPLACEMENT_REASON: "failure",
                CONF_EFFECTIVE_DATE: "2999-01-01",
                CONF_RETIRE_PREDECESSOR: True,
                CONF_UNDEPLOY_PREDECESSOR: True,
            },
            "replacement_date_in_future",
        ),
        (
            {
                CONF_REPLACEMENT_REASON: "failure",
                CONF_NOTES: 7,
                CONF_RETIRE_PREDECESSOR: True,
                CONF_UNDEPLOY_PREDECESSOR: True,
            },
            "invalid_quick_create_request",
        ),
    ],
)
async def test_replacement_errors_remain_on_replacement_step(
    hass: HomeAssistant,
    replacement_input: dict[str, object],
    error_key: str,
) -> None:
    """Replacement-specific validation never discards reviewed Asset details."""
    manager = _manager(hass)
    predecessor = await manager.async_create_manual_asset(name="Old")
    flow, _entry = _flow(hass, manager)
    await flow.async_step_quick_add_manual()
    await flow.async_step_quick_add_details(
        _details(
            deployment_state=DEPLOYMENT_STATE_DEPLOYED,
            predecessor_uuid=predecessor["asset_uuid"],
        )
    )

    result = await flow.async_step_quick_add_replacement(replacement_input)

    assert result["step_id"] == "quick_add_replacement"
    assert result["errors"] == {"base": error_key}


async def test_replacement_noop_states_and_area_removal_are_truthful(
    hass: HomeAssistant,
) -> None:
    """Confirmation distinguishes no-ops from an actual undeploy Area clear."""
    manager = _manager(hass)
    area = ar.async_get(hass).async_create("Garage")
    predecessor = await manager.async_create_manual_asset(
        name="Retired device",
        initial_lifecycle_status="retired",
    )
    predecessor = await manager.async_set_asset_deployment(
        predecessor["asset_uuid"],
        deployment_state=DEPLOYMENT_STATE_DEPLOYED,
        ha_area_id=area.id,
    )
    flow, _entry = _flow(hass, manager)
    await flow.async_step_quick_add_manual()
    await flow.async_step_quick_add_details(
        _details(
            deployment_state=DEPLOYMENT_STATE_DEPLOYED,
            predecessor_uuid=predecessor["asset_uuid"],
        )
    )

    confirm = await flow.async_step_quick_add_replacement(
        {
            CONF_REPLACEMENT_REASON: "upgrade",
            CONF_RETIRE_PREDECESSOR: True,
            CONF_UNDEPLOY_PREDECESSOR: True,
        }
    )

    placeholders = confirm["description_placeholders"]
    assert placeholders["predecessor_lifecycle"] == "Unchanged"
    assert placeholders["predecessor_deployment"] == "Deployed → Not deployed"
    assert placeholders["predecessor_area"] == "Garage → removed"
    assert QUICK_UUID not in str(placeholders)
    assert "DL" not in str(placeholders)


async def test_ha_primary_is_checked_at_selection_and_again_at_commit(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A primary claimed before or after review is rejected without a Store write."""
    manager = _manager(hass)
    device = _external_device(hass)
    owner = await manager.async_create_manual_asset(name="Owner")
    await manager.async_link_asset_device(owner["asset_uuid"], device.id)
    flow, _entry = _flow(hass, manager)
    selected = await flow.async_step_quick_add_from_ha({CONF_DEVICE_ID: device.id})
    assert selected["errors"] == {"base": "device_already_linked"}

    manager2 = _manager(hass)
    flow2, _entry = _flow(hass, manager2)
    await flow2.async_step_quick_add_from_ha({CONF_DEVICE_ID: device.id})
    await flow2.async_step_quick_add_details(
        _details(deployment_state=DEPLOYMENT_STATE_DEPLOYED)
    )
    later_owner = await manager2.async_create_manual_asset(name="Later owner")
    await manager2.async_link_asset_device(later_owner["asset_uuid"], device.id)
    manager2._store.async_save.reset_mock()
    quick_create = AsyncMock()
    monkeypatch.setattr(manager2, "async_quick_create_asset", quick_create)

    result = await flow2.async_step_quick_add_confirm(
        {CONF_CONFIRM_QUICK_ADD: True}
    )

    assert result["step_id"] == "quick_add_from_ha"
    assert result["errors"] == {"base": "device_already_linked"}
    quick_create.assert_not_awaited()
    manager2._store.async_save.assert_not_awaited()


async def test_missing_ha_device_at_commit_returns_to_source(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device disappearing after review returns to the HA selector."""
    manager = _manager(hass)
    flow, _entry = _flow(hass, manager)
    device = _external_device(hass)
    await flow.async_step_quick_add_from_ha({CONF_DEVICE_ID: device.id})
    await flow.async_step_quick_add_details(
        _details(deployment_state=DEPLOYMENT_STATE_DEPLOYED)
    )
    dr.async_get(hass).async_remove_device(device.id)
    quick_create = AsyncMock()
    monkeypatch.setattr(manager, "async_quick_create_asset", quick_create)

    result = await flow.async_step_quick_add_confirm(
        {CONF_CONFIRM_QUICK_ADD: True}
    )

    assert result["step_id"] == "quick_add_from_ha"
    assert result["errors"] == {"base": "device_missing"}
    quick_create.assert_not_awaited()


@pytest.mark.parametrize(
    ("code", "expected_step"),
    [
        ("device_missing", "quick_add_from_ha"),
        ("purchase_missing", "quick_add_details"),
        ("replacement_cycle", "quick_add_replacement"),
        ("persistence_error", "quick_add_confirm"),
    ],
)
async def test_manager_structured_failures_return_to_owned_step(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    expected_step: str,
) -> None:
    """Commit failures remain local to the domain that owns their correction."""
    manager = _manager(hass)
    device = _external_device(hass, device_id=f"device-{code}")
    predecessor = await manager.async_create_manual_asset(name="Old")
    flow, _entry = _flow(hass, manager)
    await flow.async_step_quick_add_from_ha({CONF_DEVICE_ID: device.id})
    await flow.async_step_quick_add_details(
        _details(
            deployment_state=DEPLOYMENT_STATE_DEPLOYED,
            predecessor_uuid=predecessor["asset_uuid"],
        )
    )
    await flow.async_step_quick_add_replacement(
        {
            CONF_REPLACEMENT_REASON: "failure",
            CONF_RETIRE_PREDECESSOR: True,
            CONF_UNDEPLOY_PREDECESSOR: True,
        }
    )
    monkeypatch.setattr(
        manager,
        "async_quick_create_asset",
        AsyncMock(side_effect=AssetStoreError("failed", code=code)),
    )

    result = await flow.async_step_quick_add_confirm(
        {CONF_CONFIRM_QUICK_ADD: True}
    )

    assert result["step_id"] == expected_step
    assert result["errors"] == {"base": code}


async def test_unknown_manager_failure_stays_on_confirmation(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected save failures retain the reviewed command for safe retry."""
    manager = _manager(hass)
    flow, _entry = _flow(hass, manager)
    await flow.async_step_quick_add_manual()
    await flow.async_step_quick_add_details(_details())
    monkeypatch.setattr(
        manager,
        "async_quick_create_asset",
        AsyncMock(side_effect=OSError("disk")),
    )

    result = await flow.async_step_quick_add_confirm(
        {CONF_CONFIRM_QUICK_ADD: True}
    )

    assert result["step_id"] == "quick_add_confirm"
    assert result["errors"] == {"base": "asset_store_error"}


async def test_source_specific_defaults_and_selector_contract(
    hass: HomeAssistant,
) -> None:
    """Manual/HA defaults and canonical selector options are visible and editable."""
    manual_flow, _entry = _flow(hass, _manager(hass))
    manual = await manual_flow.async_step_quick_add_manual()
    manual_lifecycle = manual["data_schema"].schema[
        QUICK_SECTION_LIFECYCLE
    ].schema.schema
    manual_defaults = {
        getattr(marker, "schema", marker): marker.default()
        for marker in manual_lifecycle
        if hasattr(marker, "default") and callable(marker.default)
    }
    assert manual_defaults["lifecycle_status"] == LIFECYCLE_STATUS_ACTIVE
    assert manual_defaults[CONF_DEPLOYMENT_STATE] == DEPLOYMENT_STATE_NOT_DEPLOYED

    manager = _manager(hass)
    ha_flow, _entry = _flow(hass, manager)
    device = _external_device(hass, device_id="defaults")
    ha = await ha_flow.async_step_quick_add_from_ha({CONF_DEVICE_ID: device.id})
    ha_lifecycle = ha["data_schema"].schema[QUICK_SECTION_LIFECYCLE].schema.schema
    ha_defaults = {
        getattr(marker, "schema", marker): marker.default()
        for marker in ha_lifecycle
        if hasattr(marker, "default") and callable(marker.default)
    }
    assert ha_defaults[CONF_DEPLOYMENT_STATE] == DEPLOYMENT_STATE_DEPLOYED
    lifecycle_selector = next(
        validator
        for marker, validator in ha_lifecycle.items()
        if getattr(marker, "schema", marker) == "lifecycle_status"
    )
    assert lifecycle_selector.config["options"] == [
        "unknown",
        "active",
        "retired",
        "disposed",
        "lost",
    ]


async def test_manual_replacement_default_is_visibly_deployed_and_editable(
    hass: HomeAssistant,
) -> None:
    """Selecting a predecessor visibly applies the replacement-aware default."""
    manager = _manager(hass)
    predecessor = await manager.async_create_manual_asset(name="Old device")
    flow, _entry = _flow(hass, manager)
    await flow.async_step_quick_add_manual()

    review = await flow.async_step_quick_add_details(
        _details(predecessor_uuid=predecessor["asset_uuid"])
    )

    assert review["step_id"] == "quick_add_details"
    assert review["errors"] == {"base": "review_replacement_deployment"}
    lifecycle_schema = review["data_schema"].schema[
        QUICK_SECTION_LIFECYCLE
    ].schema.schema
    deployment_marker = next(
        marker
        for marker in lifecycle_schema
        if getattr(marker, "schema", marker) == CONF_DEPLOYMENT_STATE
    )
    assert deployment_marker.description["suggested_value"] == (
        DEPLOYMENT_STATE_DEPLOYED
    )

    explicitly_not_deployed = await flow.async_step_quick_add_details(
        _details(
            deployment_state=DEPLOYMENT_STATE_NOT_DEPLOYED,
            predecessor_uuid=predecessor["asset_uuid"],
        )
    )
    assert explicitly_not_deployed["step_id"] == "quick_add_replacement"
    assert flow._quick_details["deployment_state"] == (
        DEPLOYMENT_STATE_NOT_DEPLOYED
    )


async def test_confirmation_uses_home_assistant_selector_translations(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical machine values are localized for a Finnish final review."""
    monkeypatch.setattr(hass.config, "language", "fi")
    flow, _entry = _flow(hass, _manager(hass))
    await flow.async_step_quick_add_manual()

    confirmation = await flow.async_step_quick_add_details(_details())

    placeholders = confirmation["description_placeholders"]
    assert placeholders["lifecycle"] == "Aktiivinen"
    assert placeholders["deployment"] == "Ei käytössä"
    assert placeholders["warranty"] == "Ei määritetty"


async def test_quick_step_forms_and_uuid_initialization_are_idempotent(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redisplaying source/review steps neither writes nor changes the proposed UUID."""
    manager = _manager(hass)
    flow, _entry = _flow(hass, manager)
    uuid_factory = Mock(return_value=UUID(QUICK_UUID))
    monkeypatch.setattr(
        "custom_components.device_lifecycle.config_flow.uuid4",
        uuid_factory,
    )

    source = await flow.async_step_quick_add_from_ha()
    first = await flow.async_step_quick_add_manual()
    second = await flow.async_step_quick_add_manual()
    await flow.async_step_quick_add_details(_details())
    confirm = await flow.async_step_quick_add_confirm()

    assert source["step_id"] == "quick_add_from_ha"
    assert first["step_id"] == second["step_id"] == "quick_add_details"
    assert confirm["step_id"] == "quick_add_confirm"
    assert uuid_factory.call_count == 1
    manager._store.async_save.assert_not_awaited()


async def test_replacement_step_can_be_redisplayed_without_state_loss(
    hass: HomeAssistant,
) -> None:
    """A normal form redisplay keeps the reviewed predecessor and defaults."""
    manager = _manager(hass)
    predecessor = await manager.async_create_manual_asset(name="Old")
    flow, _entry = _flow(hass, manager)
    await flow.async_step_quick_add_manual()
    await flow.async_step_quick_add_details(
        _details(
            deployment_state=DEPLOYMENT_STATE_DEPLOYED,
            predecessor_uuid=predecessor["asset_uuid"],
        )
    )

    result = await flow.async_step_quick_add_replacement()

    assert result["step_id"] == "quick_add_replacement"
    manager._store.async_save.assert_awaited_once()  # predecessor creation only


async def test_noncanonical_but_parseable_flow_date_is_rejected(
    hass: HomeAssistant,
) -> None:
    """Quick Add uses exact YYYY-MM-DD rather than permissive ISO parsing."""
    flow, _entry = _flow(hass, _manager(hass))
    await flow.async_step_quick_add_manual()
    values = _details()
    values[QUICK_SECTION_LIFECYCLE]["installed_date"] = "20260807"

    result = await flow.async_step_quick_add_details(values)

    assert result["errors"] == {"base": "invalid_installed_date"}


async def test_review_labels_never_fall_back_to_raw_registry_ids(
    hass: HomeAssistant,
) -> None:
    """Unavailable review references remain human-readable and hide raw UUIDs."""
    manager = _manager(hass, _store_with_purchase())
    flow, _entry = _flow(hass, manager)
    device = _external_device(hass, device_id="review-device")
    area = ar.async_get(hass).async_create("Review area")
    await flow.async_step_quick_add_from_ha({CONF_DEVICE_ID: device.id})
    await flow.async_step_quick_add_details(
        _details(
            deployment_state=DEPLOYMENT_STATE_DEPLOYED,
            purchase_uuid=PURCHASE_UUID,
            area_id=area.id,
        )
    )
    manager._data["purchases"].pop(PURCHASE_UUID)
    ar.async_get(hass).async_delete(area.id)
    dr.async_get(hass).async_remove_device(device.id)

    result = await flow._show_quick_confirm()

    placeholders = result["description_placeholders"]
    assert placeholders["purchase"] == "Unavailable Purchase"
    assert placeholders["area"] == "Unavailable Area"
    assert placeholders["ha_device"] == "Unavailable HA device"
    assert PURCHASE_UUID not in str(placeholders)
    assert device.id not in str(placeholders)
    assert area.id not in str(placeholders)


async def test_unnamed_device_review_uses_localized_fallback(
    hass: HomeAssistant,
) -> None:
    """Confirmation does not expose a raw registry ID for an unnamed HA device."""
    manager = _manager(hass)
    flow, _entry = _flow(hass, manager)
    owner = MockConfigEntry(domain="test")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("test", "unnamed")},
    )

    assert flow._quick_device_name(device.id) == "Unnamed HA device"


async def test_predecessor_disappearance_after_manager_validation_error(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacement recovery keeps the reviewed label when the Asset disappeared."""
    manager = _manager(hass)
    predecessor = await manager.async_create_manual_asset(name="Old")
    flow, _entry = _flow(hass, manager)
    await flow.async_step_quick_add_manual()
    await flow.async_step_quick_add_details(
        _details(
            deployment_state=DEPLOYMENT_STATE_DEPLOYED,
            predecessor_uuid=predecessor["asset_uuid"],
        )
    )
    await flow.async_step_quick_add_replacement(
        {
            CONF_REPLACEMENT_REASON: "failure",
            CONF_RETIRE_PREDECESSOR: True,
            CONF_UNDEPLOY_PREDECESSOR: True,
        }
    )
    manager._data["assets"].pop(predecessor["asset_uuid"])
    monkeypatch.setattr(
        manager,
        "async_quick_create_asset",
        AsyncMock(side_effect=AssetStoreError("missing", code="asset_missing")),
    )

    result = await flow.async_step_quick_add_confirm(
        {CONF_CONFIRM_QUICK_ADD: True}
    )

    assert result["step_id"] == "quick_add_replacement"
    assert result["description_placeholders"]["predecessor_name"] == "Old"


@pytest.mark.parametrize("purchase_state", ["missing", "date_missing"])
async def test_purchase_changed_recovery_handles_unavailable_review_source(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
    purchase_state: str,
) -> None:
    """A stale calculated warranty returns to details even if its source degraded."""
    manager = _manager(hass, _store_with_purchase())
    flow, _entry = _flow(hass, manager)
    await flow.async_step_quick_add_manual()
    await flow.async_step_quick_add_details(
        _details(
            purchase_uuid=PURCHASE_UUID,
            warranty_type=WARRANTY_ONE_YEAR,
        )
    )
    if purchase_state == "missing":
        manager._data["purchases"].pop(PURCHASE_UUID)
    else:
        manager._data["purchases"][PURCHASE_UUID]["purchase_date"] = None
    monkeypatch.setattr(
        manager,
        "async_quick_create_asset",
        AsyncMock(
            side_effect=AssetStoreError("changed", code="purchase_changed")
        ),
    )

    result = await flow.async_step_quick_add_confirm(
        {CONF_CONFIRM_QUICK_ADD: True}
    )

    assert result["step_id"] == "quick_add_details"
    assert result["errors"] == {"base": "purchase_changed"}


async def test_first_install_continues_into_loaded_quick_add(
    hass: HomeAssistant,
) -> None:
    """A real user config flow creates the parent and opens Quick Add safely."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.title == "Device Lifecycle"
    assert entry.version == CONFIG_ENTRY_VERSION
    assert entry.state.name == "LOADED"
    assert isinstance(entry.runtime_data, AssetStoreManager)
    assert result["next_flow"][0].value == "options_flow"

    progress = hass.config_entries.options.async_progress_by_handler(entry.entry_id)
    assert len(progress) == 1
    assert progress[0]["step_id"] == "quick_add"
    details = await hass.config_entries.options.async_configure(
        result["next_flow"][1],
        {"next_step_id": "quick_add_manual"},
    )
    assert details["step_id"] == "quick_add_details"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Laitteen elinkaari", "Device Lifecycle"),
        ("My inventory", "My inventory"),
    ],
)
async def test_setup_normalizes_only_the_exact_old_default_title(
    hass: HomeAssistant,
    title: str,
    expected: str,
) -> None:
    """Brand migration preserves every user-customized config-entry title."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="device_lifecycle_main",
        version=CONFIG_ENTRY_VERSION,
        title=title,
        data={},
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.title == expected
