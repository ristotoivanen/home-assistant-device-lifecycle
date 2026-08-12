"""Meaningful branch coverage for flow validation and recovery boundaries."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.device_lifecycle.config_flow import (
    DeviceLifecycleConfigFlow,
    DeviceLifecycleOptionsFlow,
    PurchaseSubentryFlow,
    RuntimeSubentryFlow,
    _add_years,
    _compact_title,
    _entity_platform,
    _infer_warranty_type,
    _is_retained_legacy_runtime_source,
    _is_service_device,
    _is_valid_runtime_source,
    _prepare_purchase_data,
    _prepare_runtime_data,
    _purchase_title,
    _runtime_source_candidates,
    _runtime_source_error,
    _runtime_source_schema,
    _runtime_start_schema,
    _runtime_title,
    _state_device_class,
    _state_power_unit,
    _stored_purchase_label,
)
from custom_components.device_lifecycle.const import (
    CONFIG_ENTRY_VERSION,
    CONF_ASSET_NAME,
    CONF_ASSET_UUID,
    CONF_CONFIRM_AREA_CLEAR,
    CONF_CURRENCY,
    CONF_DEPLOYMENT_STATE,
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
    CONF_POWER_HYSTERESIS,
    CONF_POWER_THRESHOLD,
    CONF_PURCHASE_DATE,
    CONF_PURCHASE_PRICE,
    CONF_PURCHASE_UUID,
    CONF_RUNTIME_DATA_VERSION,
    CONF_RUNTIME_MODE,
    CONF_SOURCE_ENTITY_ID,
    CONF_WARRANTY_TYPE,
    CONF_WARRANTY_UNTIL,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DOMAIN,
    RUNTIME_DATA_VERSION,
    RUNTIME_MODE_ON,
    RUNTIME_MODE_POWER,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
    WARRANTY_MANUAL,
    WARRANTY_NONE,
    WARRANTY_ONE_YEAR,
)
from custom_components.device_lifecycle.storage import AssetStoreError

from .conftest import ASSET_UUID, DEVICE_ID, PURCHASE_SUBENTRY_ID, PURCHASE_UUID
from .test_options_flow import _manager, _options_flow


def _schema_field(schema, field: str):
    return next(
        validator
        for marker, validator in schema.schema.items()
        if getattr(marker, "schema", marker) == field
    )


def test_runtime_schema_helpers_cover_device_defaults_and_power_fields(
    hass: HomeAssistant,
) -> None:
    """Runtime schemas retain the target default and only show power controls for POWER."""
    hass.states.async_set(
        "sensor.valid_power",
        "10",
        {"device_class": SensorDeviceClass.POWER, "unit_of_measurement": "W"},
    )
    start = _runtime_start_schema(
        hass,
        {CONF_DEVICE_ID: DEVICE_ID, CONF_RUNTIME_MODE: RUNTIME_MODE_POWER},
        include_device=True,
    )
    without_device = _runtime_start_schema(hass, None, include_device=False)
    power = _runtime_source_schema(
        hass,
        RUNTIME_MODE_POWER,
        {
            CONF_SOURCE_ENTITY_ID: "sensor.valid_power",
            CONF_POWER_THRESHOLD: 20,
            CONF_POWER_HYSTERESIS: 4,
        },
    )
    on_state = _runtime_source_schema(hass, RUNTIME_MODE_ON)

    assert _schema_field(start, CONF_DEVICE_ID).config["multiple"] is False
    assert CONF_DEVICE_ID not in {
        getattr(marker, "schema", marker) for marker in without_device.schema
    }
    assert _schema_field(power, CONF_POWER_THRESHOLD).config["unit_of_measurement"] == "W"
    assert _schema_field(power, CONF_POWER_HYSTERESIS).config["unit_of_measurement"] == "W"
    assert CONF_POWER_THRESHOLD not in {
        getattr(marker, "schema", marker) for marker in on_state.schema
    }
    assert _schema_field(power, CONF_SOURCE_ENTITY_ID) is not None


def test_runtime_source_classification_checks_registry_class_units_and_ownership(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
) -> None:
    """Runtime sources must exist, be external, and match mode-specific semantics."""
    hass.states.async_set("switch.machine", "on")
    hass.states.async_set("light.machine", "on")
    hass.states.async_set(
        "sensor.power",
        "12",
        {
            "device_class": SensorDeviceClass.POWER,
            "unit_of_measurement": UnitOfPower.WATT,
        },
    )
    registry_power = entity_registry.async_get_or_create(
        "sensor",
        "external",
        "registry_power",
        original_device_class=SensorDeviceClass.POWER,
    )
    hass.states.async_set(
        registry_power.entity_id, "8", {"unit_of_measurement": "W"}
    )
    entity_registry.async_get_or_create(
        "switch", DOMAIN, "owned", suggested_object_id="owned"
    )
    hass.states.async_set("switch.owned", "on")

    registry_state = hass.states.get(registry_power.entity_id)
    assert registry_state is not None
    assert _entity_platform(entity_registry, "sensor.missing") is None
    assert _state_device_class(entity_registry, registry_state) == "power"
    assert _state_power_unit(registry_state) == "W"
    assert _state_device_class(
        entity_registry, hass.states.get("switch.machine")
    ) is None
    hass.states.async_set("sensor.no_unit", "1")
    no_unit = hass.states.get("sensor.no_unit")
    assert no_unit is not None
    assert _state_power_unit(no_unit) is None

    assert not _is_valid_runtime_source(hass, "switch.missing", RUNTIME_MODE_ON)
    assert not _is_valid_runtime_source(hass, "switch.owned", RUNTIME_MODE_ON)
    assert _is_valid_runtime_source(hass, "switch.machine", RUNTIME_MODE_ON)
    assert _is_valid_runtime_source(hass, "light.machine", RUNTIME_MODE_ON)
    assert _is_valid_runtime_source(hass, "sensor.power", RUNTIME_MODE_POWER)
    assert _is_valid_runtime_source(
        hass, registry_power.entity_id, RUNTIME_MODE_POWER
    )
    assert not _is_valid_runtime_source(hass, "sensor.power", "hours")
    assert _runtime_source_candidates(hass, RUNTIME_MODE_ON) == [
        "light.machine",
        "switch.machine",
    ]


@pytest.mark.parametrize(
    ("entity_id", "mode", "expected"),
    [
        ("switch.missing", RUNTIME_MODE_ON, "source_missing"),
        ("sensor.bad", RUNTIME_MODE_POWER, "invalid_power_source"),
        ("sensor.bad", RUNTIME_MODE_ON, "invalid_on_state_source"),
        ("sensor.bad", "hours", "invalid_runtime_mode"),
    ],
)
def test_runtime_source_errors_are_stable(
    hass: HomeAssistant,
    entity_id: str,
    mode: str,
    expected: str,
) -> None:
    """Each runtime source failure maps to an independently translatable key."""
    if entity_id != "switch.missing":
        hass.states.async_set(entity_id, "1")
    assert _runtime_source_error(hass, entity_id, mode) == expected


def test_runtime_source_schema_retains_missing_persisted_source_as_default(
    hass: HomeAssistant,
) -> None:
    """0.7.2 WP4 / 072-06: reconfigure keeps an already-saved but missing
    source selectable and default; create never receives that allowance."""
    hass.states.async_set("switch.machine", "on")

    reconfigure = _runtime_source_schema(
        hass,
        RUNTIME_MODE_ON,
        {CONF_SOURCE_ENTITY_ID: "switch.removed_device"},
        retained_legacy_source="switch.removed_device",
    )
    field, validator = next(
        (marker, validator)
        for marker, validator in reconfigure.schema.items()
        if getattr(marker, "schema", marker) == CONF_SOURCE_ENTITY_ID
    )

    assert field.default() == "switch.removed_device"
    assert "switch.removed_device" in validator.config["include_entities"]
    # The submitted canonical value is the untouched entity_id, never a label.
    assert validator.config["include_entities"].count("switch.removed_device") == 1

    create = _runtime_source_schema(
        hass,
        RUNTIME_MODE_ON,
        {CONF_SOURCE_ENTITY_ID: "switch.removed_device"},
    )
    create_field, create_validator = next(
        (marker, validator)
        for marker, validator in create.schema.items()
        if getattr(marker, "schema", marker) == CONF_SOURCE_ENTITY_ID
    )
    assert create_field.default is vol.UNDEFINED
    assert "switch.removed_device" not in create_validator.config["include_entities"]


def test_runtime_source_schema_does_not_duplicate_a_currently_valid_source(
    hass: HomeAssistant,
) -> None:
    """A retained source already in the live candidate list is not duplicated."""
    hass.states.async_set("switch.machine", "on")

    schema = _runtime_source_schema(
        hass,
        RUNTIME_MODE_ON,
        {CONF_SOURCE_ENTITY_ID: "switch.machine"},
        retained_legacy_source="switch.machine",
    )
    _, validator = next(
        (marker, validator)
        for marker, validator in schema.schema.items()
        if getattr(marker, "schema", marker) == CONF_SOURCE_ENTITY_ID
    )

    assert validator.config["include_entities"].count("switch.machine") == 1


def test_retained_legacy_runtime_source_matches_only_the_exact_persisted_combo(
    hass: HomeAssistant,
) -> None:
    """0.7.2 WP4 / 072-06 WP4D: the retention exception is exact and narrow.

    Only the literal persisted source, under the persisted mode, while it has
    no HA state, counts as retained. A restored entity, a different entity, a
    changed mode, or an empty submission all fall through to normal rules.
    """
    subentry_data = {
        CONF_SOURCE_ENTITY_ID: "sensor.removed_power_meter",
        CONF_RUNTIME_MODE: RUNTIME_MODE_POWER,
    }

    # The exact persisted source, persisted mode, still missing: retained.
    assert _is_retained_legacy_runtime_source(
        hass, subentry_data, RUNTIME_MODE_POWER, "sensor.removed_power_meter"
    )

    # A different entity_id is never retained, even though it too is missing.
    assert not _is_retained_legacy_runtime_source(
        hass, subentry_data, RUNTIME_MODE_POWER, "sensor.some_other_missing"
    )

    # A changed runtime mode is never retained for the same entity_id.
    assert not _is_retained_legacy_runtime_source(
        hass, subentry_data, RUNTIME_MODE_ON, "sensor.removed_power_meter"
    )

    # An empty/blank submission is never retained.
    assert not _is_retained_legacy_runtime_source(
        hass, subentry_data, RUNTIME_MODE_POWER, ""
    )

    # Once the entity is restored (has a live state again), it is no longer
    # "missing" and naturally falls back to the normal candidate list instead
    # of the retention exception.
    hass.states.async_set(
        "sensor.removed_power_meter",
        "5",
        {"device_class": SensorDeviceClass.POWER, "unit_of_measurement": "W"},
    )
    assert not _is_retained_legacy_runtime_source(
        hass, subentry_data, RUNTIME_MODE_POWER, "sensor.removed_power_meter"
    )
    assert _runtime_source_candidates(hass, RUNTIME_MODE_POWER) == [
        "sensor.removed_power_meter"
    ]


@pytest.mark.parametrize(
    ("user_input", "error"),
    [
        ({CONF_DEVICE_IDS: [DEVICE_ID], CONF_PURCHASE_DATE: "bad"}, "invalid_purchase_date"),
        ({CONF_PURCHASE_PRICE: "bad"}, "invalid_purchase_price"),
        ({CONF_PURCHASE_PRICE: -1}, "invalid_purchase_price"),
        ({CONF_WARRANTY_TYPE: WARRANTY_ONE_YEAR}, "purchase_date_required_for_warranty"),
        ({CONF_WARRANTY_TYPE: WARRANTY_ONE_YEAR, CONF_PURCHASE_DATE: "bad"}, "invalid_purchase_date"),
        ({CONF_WARRANTY_TYPE: WARRANTY_MANUAL}, "manual_warranty_date_required"),
        ({CONF_WARRANTY_TYPE: "lifetime"}, "invalid_warranty_type"),
    ],
)
def test_purchase_validation_error_matrix(
    user_input: dict,
    error: str,
) -> None:
    """Purchase normalization reports every malformed price/date/warranty input."""
    clean, actual = _prepare_purchase_data(user_input, default_currency="EUR")
    assert clean is None
    assert actual == error


def test_purchase_and_title_legacy_normalization_branches() -> None:
    """Legacy warranty inference, leap-day math, fallbacks, and compact titles stay stable."""
    assert _infer_warranty_type({CONF_WARRANTY_UNTIL: "2027-01-01"}) == WARRANTY_MANUAL
    assert _add_years("bad", 1) is None
    assert _add_years("2024-02-29", 1) == "2025-02-28"
    assert _compact_title("x" * 80, 10) == "xxxxxxxxx…"
    assert _stored_purchase_label(
        {
            "purchase_uuid": PURCHASE_UUID,
            "name": None,
            "seller": " Shop ",
            "purchase_date": "2026-08-10",
        }
    ) == "Shop 2026-08-10"
    assert _stored_purchase_label(
        {
            "purchase_uuid": PURCHASE_UUID,
            "name": None,
            "seller": None,
            "purchase_date": None,
        }
    ) == PURCHASE_UUID
    assert _purchase_title(
        {"seller": "Shop", "purchase_date": "2026-08-10"}
    ) == "Shop 2026-08-10"


@pytest.mark.parametrize(
    ("user_input", "error"),
    [
        ({CONF_RUNTIME_MODE: "hours", CONF_SOURCE_ENTITY_ID: "switch.x"}, "invalid_runtime_mode"),
        ({CONF_RUNTIME_MODE: RUNTIME_MODE_ON}, "source_required"),
        ({CONF_RUNTIME_MODE: RUNTIME_MODE_POWER, CONF_SOURCE_ENTITY_ID: "sensor.x"}, "power_threshold_required"),
        ({CONF_RUNTIME_MODE: RUNTIME_MODE_POWER, CONF_SOURCE_ENTITY_ID: "sensor.x", CONF_POWER_THRESHOLD: "bad"}, "invalid_power_threshold"),
        ({CONF_RUNTIME_MODE: RUNTIME_MODE_POWER, CONF_SOURCE_ENTITY_ID: "sensor.x", CONF_POWER_THRESHOLD: 2, CONF_POWER_HYSTERESIS: 3}, "power_hysteresis_too_large"),
    ],
)
def test_runtime_configuration_error_matrix(user_input: dict, error: str) -> None:
    """Runtime normalization rejects every invalid mode/source/power combination."""
    clean, actual = _prepare_runtime_data(user_input)
    assert clean is None
    assert actual == error


def test_on_state_runtime_drops_stale_power_settings() -> None:
    """Changing from POWER to ON removes power-only settings from persistence."""
    clean, error = _prepare_runtime_data(
        {
            CONF_RUNTIME_MODE: RUNTIME_MODE_ON,
            CONF_SOURCE_ENTITY_ID: "switch.machine",
            CONF_POWER_THRESHOLD: 10,
            CONF_POWER_HYSTERESIS: 2,
        }
    )
    assert error is None
    assert clean is not None
    assert CONF_POWER_THRESHOLD not in clean
    assert CONF_POWER_HYSTERESIS not in clean


def test_service_device_runtime_title_and_flow_error_mapping(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Missing/service devices and structured/legacy Store failures remain deterministic."""
    assert not _is_service_device(device_registry, "missing")
    assert _runtime_title(device_registry, "missing") == "Käyttötunnit"
    entry = MockConfigEntry(domain="external", data={})
    entry.add_to_hass(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("external", "named")},
        name="Named Device",
    )
    assert _runtime_title(device_registry, device.id) == "Named Device"
    hass.states.async_set("switch.valid", "on")
    assert _runtime_source_error(hass, "switch.valid", RUNTIME_MODE_ON) is None

    flow = DeviceLifecycleOptionsFlow()
    structured = {
        code: flow._storage_error_key(AssetStoreError("failure", code=code))
        for code in (
            "asset_missing",
            "invalid_lifecycle_effective_date",
            "invalid_replacement_effective_date",
            "lifecycle_date_in_future",
            "replacement_cycle",
            "persistence_error",
        )
    }
    assert structured == {code: code for code in structured}
    legacy = {
        "Asset abc does not exist": "asset_missing",
        "Purchase abc does not exist": "invalid_purchase",
        "device already linked to another Asset": "device_already_linked",
        "device already primary for Asset": "related_device_is_primary",
        "primary HA relationship changed conflict": "ha_relationship_changed",
        "deployment state invalid": "invalid_deployment_state",
        "installed_date invalid": "invalid_installed_date",
        "HA Area invalid": "invalid_area",
        "unclassified": "asset_store_error",
    }
    assert {
        message: flow._storage_error_key(AssetStoreError(message))
        for message in legacy
    } == legacy


async def test_finnish_dynamic_label_and_invalid_quick_purchase_selection(
    hass: HomeAssistant,
) -> None:
    """Dynamic labels localize and stale Purchase UUIDs fail before Asset creation."""
    flow, _entry = _options_flow(hass, _manager(hass))
    with patch.object(hass.config, "language", "fi"):
        assert flow._localized_label("English", "Suomi") == "Suomi"

    await flow.async_step_quick_add_manual()
    result = await flow.async_step_quick_add_details(
        {
            CONF_ASSET_NAME: "Invalid purchase target",
            CONF_PURCHASE_UUID: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        }
    )
    assert result["errors"] == {"base": "purchase_missing"}


async def test_parent_config_flow_creation_and_quick_add_dispatch(
    hass: HomeAssistant,
) -> None:
    """Parent creation preserves ConfigEntry v4 and dispatches Quick Add."""
    flow = DeviceLifecycleConfigFlow()
    flow.hass = hass
    result_entry = SimpleNamespace(entry_id="new-parent")
    with patch.object(flow, "async_set_unique_id", AsyncMock()), patch.object(
        flow, "_abort_if_unique_id_configured"
    ), patch.object(
        flow,
        "async_create_entry",
        Mock(return_value={"result": result_entry}),
    ) as create:
        result = await flow.async_step_user()
    assert result["result"] is result_entry
    assert create.call_args.kwargs["data"] == {}
    assert flow.VERSION == CONFIG_ENTRY_VERSION == 4

    with patch.object(
        hass.config_entries.options,
        "async_init",
        AsyncMock(return_value={"flow_id": "options-flow"}),
    ), patch.object(
        hass.config_entries.options,
        "async_configure",
        AsyncMock(return_value={"flow_id": "options-flow"}),
    ) as configure:
        dispatched = await flow.async_on_create_entry(result)
    assert dispatched["next_flow"][1] == "options-flow"
    configure.assert_awaited_once_with(
        "options-flow",
        {"next_step_id": "quick_add"},
    )


async def test_options_stale_asset_and_no_asset_recovery_forms(
    hass: HomeAssistant,
) -> None:
    """Every legacy management action rechecks canonical Asset existence."""
    empty_flow, _entry = _options_flow(hass, _manager(hass))
    no_assets = await empty_flow.async_step_manage_asset()
    assert no_assets["errors"] == {"base": "no_assets"}

    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Temporary")
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset["asset_uuid"]})
    manager._data["assets"].pop(asset["asset_uuid"])
    manager._data["lifecycle_events"].clear()
    for method in (
        flow.async_step_manage_asset_menu,
        flow.async_step_edit_asset_metadata,
        flow.async_step_change_asset_purchase,
        flow.async_step_asset_deployment,
    ):
        result = await method()
        assert result["errors"] == {"base": "asset_missing"}


async def test_deployment_confirmation_recovery_and_persistence_error(
    hass: HomeAssistant,
) -> None:
    """Deployment confirmation handles stale URLs, disappearing Assets, and failed saves."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Deployment")
    flow, _entry = _options_flow(hass, manager)
    await flow.async_step_manage_asset({CONF_ASSET_UUID: asset["asset_uuid"]})
    no_pending = await flow.async_step_confirm_not_deployed()
    assert no_pending["step_id"] == "asset_deployment"

    flow._pending_deployment_update = {
        "asset_uuid": asset["asset_uuid"],
        "updates": {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED},
        "area_label": "Area",
    }
    manager._data["assets"].pop(asset["asset_uuid"])
    manager._data["lifecycle_events"].clear()
    stale = await flow.async_step_confirm_not_deployed(
        {CONF_CONFIRM_AREA_CLEAR: True}
    )
    assert stale["errors"] == {"base": "asset_missing"}

    manager2 = _manager(hass)
    asset2 = await manager2.async_create_manual_asset(name="Failed deployment")
    flow2, _entry = _options_flow(hass, manager2)
    await flow2.async_step_manage_asset({CONF_ASSET_UUID: asset2["asset_uuid"]})
    flow2._pending_deployment_update = {
        "asset_uuid": asset2["asset_uuid"],
        "updates": {CONF_DEPLOYMENT_STATE: "deployed"},
        "area_label": "Area",
    }
    manager2.async_set_asset_deployment_reporting = AsyncMock(
        side_effect=OSError("save failed")
    )
    failed = await flow2.async_step_confirm_not_deployed(
        {CONF_CONFIRM_AREA_CLEAR: True}
    )
    assert failed["errors"] == {"base": "asset_store_error"}


async def test_purchase_subentry_device_and_normalization_errors(
    hass: HomeAssistant,
) -> None:
    """Purchase create/reconfigure forms recover from stale, service, duplicate, and bad input."""
    flow = PurchaseSubentryFlow()
    flow.hass = hass
    entry = SimpleNamespace(subentries={})
    with patch.object(flow, "_get_entry", return_value=entry):
        initial = await flow.async_step_user()
    assert initial["step_id"] == "user"

    registry = Mock()
    registry.async_get.return_value = None
    with patch.object(flow, "_get_entry", return_value=entry), patch(
        "custom_components.device_lifecycle.config_flow.dr.async_get",
        return_value=registry,
    ):
        missing = await flow.async_step_user(
            {CONF_DEVICE_IDS: [DEVICE_ID], CONF_WARRANTY_TYPE: WARRANTY_NONE}
        )
    assert missing["errors"] == {"base": "device_missing"}

    registry.async_get.return_value = SimpleNamespace(entry_type="service")
    with patch.object(flow, "_get_entry", return_value=entry), patch(
        "custom_components.device_lifecycle.config_flow.dr.async_get",
        return_value=registry,
    ):
        service = await flow.async_step_user(
            {CONF_DEVICE_IDS: [DEVICE_ID], CONF_WARRANTY_TYPE: WARRANTY_NONE}
        )
    assert service["errors"] == {"base": "service_device_not_allowed"}

    existing = SimpleNamespace(
        subentry_id="other",
        subentry_type=SUBENTRY_TYPE_PURCHASE,
        data={CONF_DEVICE_IDS: [DEVICE_ID]},
    )
    entry.subentries = {"other": existing}
    registry.async_get.return_value = SimpleNamespace(entry_type=None)
    with patch.object(flow, "_get_entry", return_value=entry), patch(
        "custom_components.device_lifecycle.config_flow.dr.async_get",
        return_value=registry,
    ):
        duplicate = await flow.async_step_user(
            {CONF_DEVICE_IDS: [DEVICE_ID], CONF_WARRANTY_TYPE: WARRANTY_NONE}
        )
    assert duplicate["errors"] == {"base": "already_tracked"}

    entry.subentries = {}
    with patch.object(flow, "_get_entry", return_value=entry):
        invalid = await flow.async_step_user(
            {CONF_WARRANTY_TYPE: WARRANTY_MANUAL}
        )
    assert invalid["errors"] == {"base": "manual_warranty_date_required"}

    subentry = SimpleNamespace(
        subentry_id=PURCHASE_SUBENTRY_ID,
        subentry_type=SUBENTRY_TYPE_PURCHASE,
        data={
            CONF_PURCHASE_UUID: PURCHASE_UUID,
            CONF_CURRENCY: "EUR",
            CONF_WARRANTY_TYPE: WARRANTY_NONE,
        },
    )
    entry.subentries = {PURCHASE_SUBENTRY_ID: subentry, "other": existing}
    with patch.object(flow, "_get_entry", return_value=entry), patch.object(
        flow, "_get_reconfigure_subentry", return_value=subentry
    ), patch(
        "custom_components.device_lifecycle.config_flow.dr.async_get",
        return_value=registry,
    ):
        reconfigure_duplicate = await flow.async_step_reconfigure(
            {CONF_DEVICE_IDS: [DEVICE_ID], CONF_WARRANTY_TYPE: WARRANTY_NONE}
        )
    assert reconfigure_duplicate["errors"] == {"base": "already_tracked"}


async def test_runtime_subentry_validation_and_recovery_branches(
    hass: HomeAssistant,
) -> None:
    """Runtime two-step create/reconfigure paths surface every authoritative error."""
    flow = RuntimeSubentryFlow()
    flow.hass = hass
    entry = SimpleNamespace(subentries={})
    with patch.object(flow, "_get_entry", return_value=entry):
        initial = await flow.async_step_user()
    assert initial["step_id"] == "user"

    registry = Mock()
    registry.async_get.return_value = None
    with patch.object(flow, "_get_entry", return_value=entry), patch(
        "custom_components.device_lifecycle.config_flow.dr.async_get",
        return_value=registry,
    ):
        missing = await flow.async_step_user(
            {CONF_DEVICE_ID: DEVICE_ID, CONF_RUNTIME_MODE: RUNTIME_MODE_ON}
        )
    assert missing["errors"] == {"base": "device_missing"}

    registry.async_get.return_value = SimpleNamespace(entry_type="service")
    with patch.object(flow, "_get_entry", return_value=entry), patch(
        "custom_components.device_lifecycle.config_flow.dr.async_get",
        return_value=registry,
    ):
        service = await flow.async_step_user(
            {CONF_DEVICE_ID: DEVICE_ID, CONF_RUNTIME_MODE: RUNTIME_MODE_ON}
        )
    assert service["errors"] == {"base": "service_device_not_allowed"}

    runtime_subentry = SimpleNamespace(
        subentry_id="runtime",
        subentry_type=SUBENTRY_TYPE_RUNTIME,
        data={CONF_DEVICE_ID: DEVICE_ID},
    )
    entry.subentries = {"runtime": runtime_subentry}
    registry.async_get.return_value = SimpleNamespace(entry_type=None)
    with patch.object(flow, "_get_entry", return_value=entry), patch(
        "custom_components.device_lifecycle.config_flow.dr.async_get",
        return_value=registry,
    ):
        duplicate = await flow.async_step_user(
            {CONF_DEVICE_ID: DEVICE_ID, CONF_RUNTIME_MODE: RUNTIME_MODE_ON}
        )
    assert duplicate["errors"] == {"base": "runtime_already_tracked"}

    entry.subentries = {}
    with patch.object(flow, "_get_entry", return_value=entry), patch(
        "custom_components.device_lifecycle.config_flow.dr.async_get",
        return_value=registry,
    ):
        invalid_mode = await flow.async_step_user(
            {CONF_DEVICE_ID: DEVICE_ID, CONF_RUNTIME_MODE: "hours"}
        )
    assert invalid_mode["errors"] == {"base": "invalid_runtime_mode"}

    fresh = RuntimeSubentryFlow()
    fresh.hass = hass
    with patch.object(fresh, "async_step_user", AsyncMock(return_value={"step_id": "user"})):
        recovered = await fresh.async_step_runtime_source()
    assert recovered == {"step_id": "user"}

    flow._set_runtime_context(device_id=DEVICE_ID, runtime_mode=RUNTIME_MODE_POWER)
    with patch(
        "custom_components.device_lifecycle.config_flow._runtime_source_error",
        return_value="source_missing",
    ):
        source_error = await flow.async_step_runtime_source(
            {CONF_SOURCE_ENTITY_ID: "sensor.missing"}
        )
    assert source_error["errors"] == {"base": "source_missing"}

    with patch(
        "custom_components.device_lifecycle.config_flow._runtime_source_error",
        return_value=None,
    ):
        prepare_error = await flow.async_step_runtime_source(
            {CONF_SOURCE_ENTITY_ID: "sensor.power"}
        )
    assert prepare_error["errors"] == {"base": "power_threshold_required"}

    subentry = SimpleNamespace(
        data={
            CONF_DEVICE_ID: DEVICE_ID,
            CONF_ASSET_UUID: ASSET_UUID,
            CONF_RUNTIME_MODE: RUNTIME_MODE_ON,
            CONF_RUNTIME_DATA_VERSION: RUNTIME_DATA_VERSION,
        }
    )
    with patch.object(flow, "_get_reconfigure_subentry", return_value=subentry):
        initial_reconfigure = await flow.async_step_reconfigure()
        invalid_reconfigure = await flow.async_step_reconfigure(
            {CONF_RUNTIME_MODE: "hours"}
        )
    assert initial_reconfigure["step_id"] == "reconfigure"
    assert invalid_reconfigure["errors"] == {"base": "invalid_runtime_mode"}

    fresh_reconfigure = RuntimeSubentryFlow()
    fresh_reconfigure.hass = hass
    with patch.object(
        fresh_reconfigure,
        "async_step_reconfigure",
        AsyncMock(return_value={"step_id": "reconfigure"}),
    ):
        recovered_reconfigure = await fresh_reconfigure.async_step_reconfigure_source()
    assert recovered_reconfigure == {"step_id": "reconfigure"}
