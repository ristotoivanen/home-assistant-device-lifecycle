"""Behavioral coverage for every remaining config and options flow boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
import pytest
import voluptuous as vol

from custom_components.device_lifecycle.config_flow import (
    NO_PURCHASE_SELECTION,
    PurchaseSubentryFlow,
    RuntimeSubentryFlow,
    _asset_metadata_schema,
    _used_runtime_device_ids,
)
from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_CLEAR_HA_AREA,
    CONF_CLEAR_INSTALLED_DATE,
    CONF_CURRENCY,
    CONF_DEPLOYMENT_STATE,
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
    CONF_HA_AREA_ID,
    CONF_HA_RELATIONSHIP_ACTION,
    CONF_INSTALLED_DATE,
    CONF_POWER_HYSTERESIS,
    CONF_POWER_THRESHOLD,
    CONF_PURCHASE_NAME,
    CONF_PURCHASE_UUID,
    CONF_RUNTIME_DATA_VERSION,
    CONF_RUNTIME_MODE,
    CONF_SOURCE_ENTITY_ID,
    CONF_WARRANTY_TYPE,
    CONF_WARRANTY_UNTIL,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    HA_RELATIONSHIP_ACTION_UNLINK,
    RUNTIME_MODE_ON,
    RUNTIME_MODE_POWER,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
    WARRANTY_MANUAL,
    WARRANTY_NONE,
)
from custom_components.device_lifecycle.storage import AssetStoreError

from .conftest import ASSET_UUID, DEVICE_ID, PURCHASE_SUBENTRY_ID, PURCHASE_UUID
from .test_ha_relationship_options_flow import _external_device
from .test_options_flow import _manager, _options_flow, _store_with_purchase


def _subentry_data(subentry_type: str, data: dict[str, object], title: str) -> dict:
    """Build one MockConfigEntry subentry projection."""
    return {
        "data": data,
        "subentry_type": subentry_type,
        "title": title,
        "unique_id": None,
    }


def test_optional_purchase_schema_and_malformed_runtime_subentry_are_safe() -> None:
    """Reusable schemas and duplicate scans retain their defensive contracts."""
    options = [
        selector.SelectOptionDict(
            value=NO_PURCHASE_SELECTION,
            label="No Purchase",
        )
    ]
    schema = _asset_metadata_schema(options)
    purchase_marker, purchase_selector = next(
        (marker, validator)
        for marker, validator in schema.schema.items()
        if getattr(marker, "schema", marker) == CONF_PURCHASE_UUID
    )
    assert purchase_marker.default() == NO_PURCHASE_SELECTION
    assert purchase_selector.config["options"] == options

    malformed = SimpleNamespace(
        subentry_id="malformed",
        subentry_type=SUBENTRY_TYPE_RUNTIME,
        data={},
    )
    valid = SimpleNamespace(
        subentry_id="valid",
        subentry_type=SUBENTRY_TYPE_RUNTIME,
        data={CONF_DEVICE_ID: "other-device"},
    )
    entry = SimpleNamespace(subentries={"malformed": malformed, "valid": valid})
    assert _used_runtime_device_ids(entry) == {"other-device"}


async def test_stale_registry_owner_and_unrelated_dependencies_fail_safely(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Stale ownership is rejected and unrelated subentries do not block linking."""
    _owner, device = _external_device(
        hass,
        device_registry,
        key="stale-owner",
    )
    flow, _entry = _options_flow(hass, _manager(hass))
    real_get_entry = hass.config_entries.async_get_entry
    with patch.object(
        hass.config_entries,
        "async_get_entry",
        side_effect=lambda entry_id: (
            None
            if entry_id == device.config_entry_id
            else real_get_entry(entry_id)
        ),
    ):
        stale = await flow.async_step_quick_add_from_ha(
            {CONF_DEVICE_ID: device.id}
        )
    assert stale["errors"] == {"base": "device_missing"}

    subentries = (
        _subentry_data(
            SUBENTRY_TYPE_PURCHASE,
            {CONF_DEVICE_IDS: ["purchase-one"]},
            "Purchase one",
        ),
        _subentry_data(
            SUBENTRY_TYPE_PURCHASE,
            {CONF_DEVICE_IDS: ["purchase-two"]},
            "Purchase two",
        ),
        _subentry_data(
            SUBENTRY_TYPE_RUNTIME,
            {CONF_DEVICE_ID: "runtime-one"},
            "Runtime one",
        ),
        _subentry_data(
            SUBENTRY_TYPE_RUNTIME,
            {CONF_DEVICE_ID: "runtime-two"},
            "Runtime two",
        ),
    )
    dependency_flow, _entry = _options_flow(
        hass,
        _manager(hass),
        subentries_data=subentries,
    )
    assert dependency_flow._ha_relationship_dependency_error("target") is None


async def test_purchase_change_persistence_conflict_stays_on_form(
    hass: HomeAssistant,
) -> None:
    """A relationship persistence conflict writes nothing and schedules no reload."""
    manager = _manager(hass, _store_with_purchase())
    asset = await manager.async_create_manual_asset(name="Purchase failure")
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset["asset_uuid"]})
    manager.async_set_asset_purchase_reporting = AsyncMock(
        side_effect=AssetStoreError("Purchase relationship conflict")
    )

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        result = await flow.async_step_change_asset_purchase(
            {CONF_PURCHASE_UUID: PURCHASE_UUID}
        )

    assert result["errors"] == {"base": "purchase_conflict"}
    manager.async_set_asset_purchase_reporting.assert_awaited_once()
    reload.assert_not_called()


async def test_deployment_noop_clear_and_same_values_do_not_write(
    hass: HomeAssistant,
) -> None:
    """Clearing absent values or resubmitting current values is a safe no-op.

    0.7.2 WP2 / F-2: a canonical Asset no-op must produce zero Store write
    AND zero reload, not merely zero write.
    """
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Deployment no-op")
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset["asset_uuid"]})
    manager._store.async_save.reset_mock()

    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        cleared = await flow.async_step_asset_deployment(
            {
                CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED,
                CONF_CLEAR_INSTALLED_DATE: True,
                CONF_CLEAR_HA_AREA: True,
            }
        )

    assert cleared["type"] is FlowResultType.CREATE_ENTRY
    manager._store.async_save.assert_not_awaited()
    reload.assert_not_called()

    area = ar.async_get(hass).async_create("Existing Area")
    asset = await manager.async_set_asset_deployment(
        asset["asset_uuid"],
        deployment_state=DEPLOYMENT_STATE_DEPLOYED,
        installed_date="2026-08-10",
        ha_area_id=area.id,
    )
    same_flow, _entry = _options_flow(hass, manager)
    await same_flow.async_step_manage_asset({CONF_ASSET_UUID: asset["asset_uuid"]})
    manager._store.async_save.reset_mock()

    with patch.object(hass.config_entries, "async_schedule_reload") as same_reload:
        same = await same_flow.async_step_asset_deployment(
            {
                CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED,
                CONF_INSTALLED_DATE: "2026-08-10",
                CONF_HA_AREA_ID: area.id,
            }
        )

    assert same["type"] is FlowResultType.CREATE_ENTRY
    manager._store.async_save.assert_not_awaited()
    same_reload.assert_not_called()


async def test_deployment_malformed_date_and_not_deployed_area_are_local_errors(
    hass: HomeAssistant,
) -> None:
    """Defensive date parsing and the Area invariant preserve Store state."""

    class InvalidDateValue:
        def __str__(self) -> str:
            raise ValueError("not representable as a date")

    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Deployment errors")
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset["asset_uuid"]})
    before = manager.asset(asset["asset_uuid"])

    malformed = await flow.async_step_asset_deployment(
        {
            CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED,
            CONF_INSTALLED_DATE: InvalidDateValue(),
        }
    )
    assert malformed["errors"] == {"base": "invalid_installed_date"}

    area = ar.async_get(hass).async_create("Forbidden Area")
    invalid_area = await flow.async_step_asset_deployment(
        {
            CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED,
            CONF_HA_AREA_ID: area.id,
        }
    )
    assert invalid_area["errors"] == {"base": "invalid_area"}
    assert manager.asset(asset["asset_uuid"]) == before


@pytest.mark.parametrize(
    "step_name",
    (
        "async_step_ha_relationship",
        "async_step_manage_primary_device",
        "async_step_add_related_device",
        "async_step_remove_related_device",
    ),
)
async def test_relationship_steps_recover_if_selected_asset_disappears(
    hass: HomeAssistant,
    step_name: str,
) -> None:
    """Every relationship action rechecks the selected canonical Asset."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Disappearing Asset")
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset["asset_uuid"]})
    manager._data["assets"].pop(asset["asset_uuid"])
    manager._data["lifecycle_events"].clear()

    result = await getattr(flow, step_name)()

    assert result["step_id"] == "manage_asset"
    assert result["errors"] == {"base": "asset_missing"}


async def test_primary_relationship_invalid_input_and_unlink_failure_retry(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Invalid actions, missing targets, and failed unlink remain retryable."""
    _owner, device = _external_device(
        hass,
        device_registry,
        key="unlink-failure",
    )
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Primary errors")
    await manager.async_link_asset_device(asset["asset_uuid"], device.id)
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset["asset_uuid"]})

    invalid_action = await flow.async_step_manage_primary_device(
        {CONF_HA_RELATIONSHIP_ACTION: "transfer"}
    )
    assert invalid_action["errors"] == {
        "base": "invalid_ha_relationship_action"
    }

    manager.async_unlink_asset_device_reporting = AsyncMock(side_effect=OSError("disk"))
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        failed = await flow.async_step_manage_primary_device(
            {CONF_HA_RELATIONSHIP_ACTION: HA_RELATIONSHIP_ACTION_UNLINK}
        )
    assert failed["errors"] == {"base": "asset_store_error"}
    reload.assert_not_called()

    empty_manager = _manager(hass)
    empty_asset = await empty_manager.async_create_manual_asset(name="No primary")
    empty_flow, _entry = _options_flow(hass, empty_manager)
    await empty_flow.async_step_manage_asset(
        {CONF_ASSET_UUID: empty_asset["asset_uuid"]}
    )
    missing_target = await empty_flow.async_step_manage_primary_device({})
    assert missing_target["errors"] == {"base": "device_missing"}


async def test_related_relationship_empty_stale_and_persistence_recovery(
    hass: HomeAssistant,
) -> None:
    """Related-device forms recover from empty, stale, and failed mutations."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Related errors")
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset["asset_uuid"]})

    missing_add = await flow.async_step_add_related_device({})
    assert missing_add["errors"] == {"base": "device_missing"}

    no_relationship = await flow.async_step_remove_related_device()
    assert no_relationship["step_id"] == "ha_relationship"
    assert "remove_related_device" not in no_relationship["menu_options"]

    await manager.async_add_related_device(
        asset["asset_uuid"],
        "stored-related-device",
    )
    invalid = await flow.async_step_remove_related_device(
        {CONF_DEVICE_ID: "not-the-stored-device"}
    )
    assert invalid["errors"] == {"base": "related_device_not_found"}

    manager.async_remove_related_device = AsyncMock(side_effect=OSError("disk"))
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        failed = await flow.async_step_remove_related_device(
            {CONF_DEVICE_ID: "stored-related-device"}
        )
    assert failed["errors"] == {"base": "asset_store_error"}
    reload.assert_not_called()


async def test_purchase_create_with_device_and_manual_warranty_succeeds(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """An eligible untracked device and valid manual warranty create a Purchase."""
    _owner, device = _external_device(
        hass,
        device_registry,
        key="purchase-create",
    )
    flow = PurchaseSubentryFlow()
    flow.hass = hass
    entry = SimpleNamespace(subentries={})
    created = {"type": "create_entry"}
    with patch.object(flow, "_get_entry", return_value=entry), patch.object(
        flow,
        "async_create_entry",
        Mock(return_value=created),
    ) as create:
        result = await flow.async_step_user(
            {
                CONF_DEVICE_IDS: [device.id],
                CONF_PURCHASE_NAME: "Manual warranty Purchase",
                CONF_WARRANTY_TYPE: WARRANTY_MANUAL,
                CONF_WARRANTY_UNTIL: "2028-08-10",
            }
        )

    assert result == created
    assert create.call_args.kwargs["data"][CONF_DEVICE_IDS] == [device.id]
    assert create.call_args.kwargs["data"][CONF_WARRANTY_UNTIL] == "2028-08-10"


async def test_purchase_reconfigure_missing_service_and_validation_recovery(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Purchase reconfigure keeps each device and normalization error local."""
    _owner, service = _external_device(
        hass,
        device_registry,
        key="purchase-service",
        entry_type=dr.DeviceEntryType.SERVICE,
    )
    subentry = SimpleNamespace(
        subentry_id=PURCHASE_SUBENTRY_ID,
        subentry_type=SUBENTRY_TYPE_PURCHASE,
        data={
            CONF_PURCHASE_UUID: PURCHASE_UUID,
            CONF_CURRENCY: "EUR",
            CONF_WARRANTY_TYPE: WARRANTY_NONE,
        },
    )
    entry = SimpleNamespace(
        subentries={PURCHASE_SUBENTRY_ID: subentry},
        runtime_data=None,
    )
    flow = PurchaseSubentryFlow()
    flow.hass = hass
    with patch.object(flow, "_get_entry", return_value=entry), patch.object(
        flow,
        "_get_reconfigure_subentry",
        return_value=subentry,
    ):
        missing = await flow.async_step_reconfigure(
            {CONF_DEVICE_IDS: ["missing-device"], CONF_WARRANTY_TYPE: WARRANTY_NONE}
        )
        service_result = await flow.async_step_reconfigure(
            {CONF_DEVICE_IDS: [service.id], CONF_WARRANTY_TYPE: WARRANTY_NONE}
        )
        invalid = await flow.async_step_reconfigure(
            {CONF_WARRANTY_TYPE: WARRANTY_MANUAL}
        )

    assert missing["errors"] == {"base": "device_missing"}
    assert service_result["errors"] == {"base": "service_device_not_allowed"}
    assert invalid["errors"] == {"base": "manual_warranty_date_required"}


async def test_runtime_create_walks_valid_two_step_flow_and_ignores_malformed_peer(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Runtime creation ignores malformed peers and persists one validated source."""
    _owner, device = _external_device(
        hass,
        device_registry,
        key="runtime-create",
    )
    malformed = SimpleNamespace(
        subentry_id="malformed",
        subentry_type=SUBENTRY_TYPE_RUNTIME,
        data={},
    )
    other = SimpleNamespace(
        subentry_id="other",
        subentry_type=SUBENTRY_TYPE_RUNTIME,
        data={CONF_DEVICE_ID: "other-runtime-device"},
    )
    entry = SimpleNamespace(subentries={"malformed": malformed, "other": other})
    flow = RuntimeSubentryFlow()
    flow.hass = hass
    with patch.object(flow, "_get_entry", return_value=entry):
        source_form = await flow.async_step_user(
            {CONF_DEVICE_ID: device.id, CONF_RUNTIME_MODE: RUNTIME_MODE_ON}
        )
    assert source_form["step_id"] == "runtime_source"

    hass.states.async_set("switch.runtime_create", "on")
    created = {"type": "create_entry"}
    with patch.object(
        flow,
        "async_create_entry",
        Mock(return_value=created),
    ) as create:
        result = await flow.async_step_runtime_source(
            {CONF_SOURCE_ENTITY_ID: "switch.runtime_create"}
        )
    assert result == created
    stored = create.call_args.kwargs["data"]
    assert stored[CONF_DEVICE_ID] == device.id
    assert stored[CONF_RUNTIME_MODE] == RUNTIME_MODE_ON
    assert stored[CONF_RUNTIME_DATA_VERSION] == 1


async def test_runtime_create_still_rejects_a_missing_source(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """0.7.2 WP4 / 072-06 Case E: creation gains no missing-source allowance.

    The retained-legacy-source exception is reconfigure-only; a brand-new
    Runtime tracker must still be rejected for a source with no HA state.
    """
    _owner, device = _external_device(
        hass,
        device_registry,
        key="runtime-create-missing",
    )
    flow = RuntimeSubentryFlow()
    flow.hass = hass
    entry = SimpleNamespace(subentries={})
    with patch.object(flow, "_get_entry", return_value=entry):
        source_form = await flow.async_step_user(
            {CONF_DEVICE_ID: device.id, CONF_RUNTIME_MODE: RUNTIME_MODE_ON}
        )
    assert source_form["step_id"] == "runtime_source"

    field, validator = next(
        (marker, validator)
        for marker, validator in source_form["data_schema"].schema.items()
        if getattr(marker, "schema", marker) == CONF_SOURCE_ENTITY_ID
    )
    assert field.default is vol.UNDEFINED
    assert "switch.never_existed" not in validator.config["include_entities"]

    result = await flow.async_step_runtime_source(
        {CONF_SOURCE_ENTITY_ID: "switch.never_existed"}
    )

    assert result["errors"] == {"base": "source_missing"}


async def test_legacy_runtime_reconfigure_source_errors_retry_then_succeed(
    hass: HomeAssistant,
) -> None:
    """Legacy Runtime edits preserve target identity and omit a fabricated marker."""
    subentry = SimpleNamespace(
        subentry_id="legacy-runtime",
        subentry_type=SUBENTRY_TYPE_RUNTIME,
        data={
            CONF_DEVICE_ID: DEVICE_ID,
            CONF_ASSET_UUID: ASSET_UUID,
            CONF_RUNTIME_MODE: RUNTIME_MODE_ON,
            CONF_SOURCE_ENTITY_ID: "switch.old_source",
        },
    )
    entry = SimpleNamespace(subentries={subentry.subentry_id: subentry})
    flow = RuntimeSubentryFlow()
    flow.hass = hass
    with patch.object(flow, "_get_entry", return_value=entry), patch.object(
        flow,
        "_get_reconfigure_subentry",
        return_value=subentry,
    ):
        source_form = await flow.async_step_reconfigure(
            {CONF_RUNTIME_MODE: RUNTIME_MODE_POWER}
        )
        missing = await flow.async_step_reconfigure_source(
            {CONF_SOURCE_ENTITY_ID: "sensor.missing"}
        )
        with patch(
            "custom_components.device_lifecycle.config_flow._runtime_source_error",
            return_value=None,
        ):
            invalid = await flow.async_step_reconfigure_source(
                {CONF_SOURCE_ENTITY_ID: "sensor.power"}
            )

        updated = {"type": "abort", "reason": "reconfigure_successful"}
        with patch(
            "custom_components.device_lifecycle.config_flow._runtime_source_error",
            return_value=None,
        ), patch.object(
            flow,
            "async_update_and_abort",
            Mock(return_value=updated),
        ) as update:
            result = await flow.async_step_reconfigure_source(
                {
                    CONF_SOURCE_ENTITY_ID: "sensor.power",
                    CONF_POWER_THRESHOLD: 10,
                    CONF_POWER_HYSTERESIS: 2,
                }
            )

    assert source_form["step_id"] == "reconfigure_source"
    assert missing["errors"] == {"base": "source_missing"}
    assert invalid["errors"] == {"base": "power_threshold_required"}
    assert result == updated
    stored = update.call_args.kwargs["data"]
    assert stored[CONF_DEVICE_ID] == DEVICE_ID
    assert stored[CONF_ASSET_UUID] == ASSET_UUID
    assert CONF_RUNTIME_DATA_VERSION not in stored
