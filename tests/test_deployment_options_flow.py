"""OptionsFlow tests for explicit Asset deployment management."""

from __future__ import annotations

from copy import deepcopy
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle.config_flow import (
    DeviceLifecycleOptionsFlow,
)
from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_CLEAR_HA_AREA,
    CONF_CLEAR_INSTALLED_DATE,
    CONF_CONFIRM_AREA_CLEAR,
    CONF_DEPLOYMENT_STATE,
    CONF_HA_AREA_ID,
    CONF_INSTALLED_DATE,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DEPLOYMENT_STATE_UNKNOWN,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import ASSET_UUID
from .test_options_flow import _manager, _options_flow


async def _select_asset(
    hass: HomeAssistant,
    manager: AssetStoreManager,
    asset_uuid: str = ASSET_UUID,
) -> tuple[DeviceLifecycleOptionsFlow, dict]:
    """Return an OptionsFlow positioned on one Asset's management menu."""
    flow, _entry = _options_flow(hass, manager)
    result = await flow.async_step_manage_asset({CONF_ASSET_UUID: asset_uuid})
    return flow, result


def _schema_validator(result: dict, field: str):
    """Return one form field's selector validator."""
    for marker, validator in result["data_schema"].schema.items():
        if getattr(marker, "schema", marker) == field:
            return validator
    raise AssertionError(f"Missing field {field}")


def _deployment_defaults(result: dict) -> dict:
    """Validate an empty form submission to expose field defaults."""
    return result["data_schema"]({})


def _deployed_store(
    asset_store_data: AssetStoreData,
    *,
    area_id: str | None = None,
) -> AssetStoreData:
    """Return representative migrated data marked explicitly deployed."""
    data = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    asset[CONF_DEPLOYMENT_STATE] = DEPLOYMENT_STATE_DEPLOYED
    asset[CONF_HA_AREA_ID] = area_id
    return data


async def test_deployment_action_and_migrated_unknown_are_visible(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Every Asset menu exposes Deployment and migrated Assets show unknown."""
    manager = _manager(hass, asset_store_data)
    flow, menu = await _select_asset(hass, manager)

    form = await flow.async_step_asset_deployment()

    assert menu["type"] is FlowResultType.MENU
    assert menu["menu_options"] == [
        "edit_asset_metadata",
        "change_asset_purchase",
        "asset_deployment",
        "asset_lifecycle",
        "asset_replacement",
        "ha_relationship",
    ]
    assert form["type"] is FlowResultType.FORM
    assert form["step_id"] == "asset_deployment"
    assert _deployment_defaults(form)[CONF_DEPLOYMENT_STATE] == (
        DEPLOYMENT_STATE_UNKNOWN
    )
    assert isinstance(_schema_validator(form, CONF_HA_AREA_ID), selector.AreaSelector)
    assert manager.asset(ASSET_UUID)[CONF_DEPLOYMENT_STATE] == (
        DEPLOYMENT_STATE_UNKNOWN
    )


async def test_manual_asset_starts_not_deployed_and_can_deploy_without_area(
    hass: HomeAssistant,
) -> None:
    """A manual Asset starts not deployed and needs no Area to be deployed."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Shelf spare")
    flow, _menu = await _select_asset(hass, manager, asset["asset_uuid"])
    form = await flow.async_step_asset_deployment()

    result = await flow.async_step_asset_deployment(
        {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED}
    )

    updated = manager.asset(asset["asset_uuid"])
    assert _deployment_defaults(form)[CONF_DEPLOYMENT_STATE] == (
        DEPLOYMENT_STATE_NOT_DEPLOYED
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert updated[CONF_DEPLOYMENT_STATE] == DEPLOYMENT_STATE_DEPLOYED
    assert updated[CONF_INSTALLED_DATE] is None
    assert updated[CONF_HA_AREA_ID] is None
    assert updated["asset_uuid"] == asset["asset_uuid"]
    assert updated["asset_id"] == asset["asset_id"]


async def test_migrated_unknown_can_deploy_with_date_and_area(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    area_registry: ar.AreaRegistry,
) -> None:
    """Unknown migrates only through explicit date, state, and Area choices."""
    office = area_registry.async_create("Office")
    manager = _manager(hass, asset_store_data)
    before = manager.asset(ASSET_UUID)
    flow, _menu = await _select_asset(hass, manager)

    result = await flow.async_step_asset_deployment(
        {
            CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED,
            CONF_INSTALLED_DATE: "2026-08-10",
            CONF_HA_AREA_ID: office.id,
        }
    )

    updated = manager.asset(ASSET_UUID)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert updated[CONF_DEPLOYMENT_STATE] == DEPLOYMENT_STATE_DEPLOYED
    assert updated[CONF_INSTALLED_DATE] == "2026-08-10"
    assert updated[CONF_HA_AREA_ID] == office.id
    assert updated["field_sources"][CONF_DEPLOYMENT_STATE] == "user"
    assert updated["field_sources"][CONF_INSTALLED_DATE] == "user"
    assert updated["field_sources"][CONF_HA_AREA_ID] == "user"
    assert updated["asset_uuid"] == before["asset_uuid"]
    assert updated["asset_id"] == before["asset_id"]
    assert updated["purchase_uuid"] == before["purchase_uuid"]
    assert updated["ha_device_refs"] == before["ha_device_refs"]


async def test_installation_date_set_change_clear_and_state_independence(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Date changes are explicit and never follow a deployment state change."""
    data = _deployed_store(asset_store_data)
    manager = _manager(hass, data)
    original_date = manager.asset(ASSET_UUID)[CONF_INSTALLED_DATE]
    flow, _menu = await _select_asset(hass, manager)

    await flow.async_step_asset_deployment(
        {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED}
    )

    assert manager.asset(ASSET_UUID)[CONF_INSTALLED_DATE] == original_date

    change_flow, _menu = await _select_asset(hass, manager)
    await change_flow.async_step_asset_deployment(
        {
            CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED,
            CONF_INSTALLED_DATE: "2026-07-01",
        }
    )
    assert manager.asset(ASSET_UUID)[CONF_INSTALLED_DATE] == "2026-07-01"

    clear_flow, _menu = await _select_asset(hass, manager)
    await clear_flow.async_step_asset_deployment(
        {
            CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED,
            CONF_CLEAR_INSTALLED_DATE: True,
        }
    )
    assert manager.asset(ASSET_UUID)[CONF_INSTALLED_DATE] is None


async def test_area_can_change_and_clear_while_deployed(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    area_registry: ar.AreaRegistry,
) -> None:
    """Valid Area replacement and explicit Area clearing use Asset storage only."""
    office = area_registry.async_create("Office")
    workshop = area_registry.async_create("Workshop")
    manager = _manager(hass, _deployed_store(asset_store_data, area_id=office.id))
    flow, _menu = await _select_asset(hass, manager)

    await flow.async_step_asset_deployment(
        {
            CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED,
            CONF_HA_AREA_ID: workshop.id,
        }
    )
    assert manager.asset(ASSET_UUID)[CONF_HA_AREA_ID] == workshop.id

    clear_flow, _menu = await _select_asset(hass, manager)
    await clear_flow.async_step_asset_deployment(
        {
            CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED,
            CONF_CLEAR_HA_AREA: True,
        }
    )
    assert manager.asset(ASSET_UUID)[CONF_HA_AREA_ID] is None


async def test_external_device_area_is_unchanged_and_link_does_not_infer_state(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    area_registry: ar.AreaRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Asset deployment never changes or derives from an external HA device."""
    external_entry = MockConfigEntry(domain="hue", data={})
    external_entry.add_to_hass(hass)
    device_area = area_registry.async_create("Device Area")
    asset_area = area_registry.async_create("Asset Area")
    device = device_registry.async_get_or_create(
        config_entry_id=external_entry.entry_id,
        identifiers={("hue", "external-bulb")},
    )
    device = device_registry.async_update_device(device.id, area_id=device_area.id)
    assert device is not None
    data = deepcopy(asset_store_data)
    asset_data = data["assets"][ASSET_UUID]
    asset_data["ha_device_refs"] = [
        {"device_id": device.id, "role": "primary"}
    ]
    manager = _manager(hass, data)
    flow, _menu = await _select_asset(hass, manager)
    form = await flow.async_step_asset_deployment()

    await flow.async_step_asset_deployment(
        {
            CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED,
            CONF_HA_AREA_ID: asset_area.id,
        }
    )

    updated = manager.asset(ASSET_UUID)
    assert _deployment_defaults(form)[CONF_DEPLOYMENT_STATE] == (
        DEPLOYMENT_STATE_UNKNOWN
    )
    assert updated[CONF_DEPLOYMENT_STATE] == DEPLOYMENT_STATE_DEPLOYED
    assert updated[CONF_HA_AREA_ID] == asset_area.id
    assert updated["ha_device_refs"] == [
        {"device_id": device.id, "role": "primary"}
    ]
    assert device_registry.async_get(device.id).area_id == device_area.id


async def test_not_deployed_area_clear_requires_confirmation_and_can_cancel(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    area_registry: ar.AreaRegistry,
) -> None:
    """The first submission never silently clears an existing deployment Area."""
    office = area_registry.async_create("Office")
    manager = _manager(hass, _deployed_store(asset_store_data, area_id=office.id))
    before = deepcopy(manager._data)
    flow, _menu = await _select_asset(hass, manager)

    confirmation = await flow.async_step_asset_deployment(
        {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED}
    )

    assert confirmation["type"] is FlowResultType.FORM
    assert confirmation["step_id"] == "confirm_not_deployed"
    assert manager._data == before
    assert manager._store.async_save.await_count == 0

    cancelled = await flow.async_step_confirm_not_deployed(
        {CONF_CONFIRM_AREA_CLEAR: False}
    )

    assert cancelled["type"] is FlowResultType.FORM
    assert cancelled["step_id"] == "asset_deployment"
    assert manager._data == before
    assert manager._store.async_save.await_count == 0


async def test_confirm_not_deployed_clears_area_and_preserves_identity_and_date(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    area_registry: ar.AreaRegistry,
) -> None:
    """Confirmed transition clears only Area plus the explicit state change."""
    office = area_registry.async_create("Office")
    manager = _manager(hass, _deployed_store(asset_store_data, area_id=office.id))
    before = manager.asset(ASSET_UUID)
    flow, _menu = await _select_asset(hass, manager)
    await flow.async_step_asset_deployment(
        {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED}
    )

    result = await flow.async_step_confirm_not_deployed(
        {CONF_CONFIRM_AREA_CLEAR: True}
    )

    updated = manager.asset(ASSET_UUID)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert updated[CONF_DEPLOYMENT_STATE] == DEPLOYMENT_STATE_NOT_DEPLOYED
    assert updated[CONF_HA_AREA_ID] is None
    assert updated[CONF_INSTALLED_DATE] == before[CONF_INSTALLED_DATE]
    assert updated["asset_uuid"] == before["asset_uuid"]
    assert updated["asset_id"] == before["asset_id"]
    assert updated["purchase_uuid"] == before["purchase_uuid"]
    assert updated["ha_device_refs"] == before["ha_device_refs"]
    assert manager._store.async_save.await_count == 1


async def test_stale_area_opens_and_remains_until_explicit_replacement(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    area_registry: ar.AreaRegistry,
) -> None:
    """A stale ID is displayed and preserved without any name-based remap."""
    stale_area_id = "deleted-area-id"
    replacement = area_registry.async_create("Replacement Area")
    manager = _manager(
        hass,
        _deployed_store(asset_store_data, area_id=stale_area_id),
    )
    flow, _menu = await _select_asset(hass, manager)

    form = await flow.async_step_asset_deployment()
    unchanged = await flow.async_step_asset_deployment(
        {
            CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED,
            CONF_INSTALLED_DATE: "2026-08-01",
        }
    )

    assert form["type"] is FlowResultType.FORM
    assert "Unavailable" in form["description_placeholders"]["current_area"]
    assert stale_area_id in form["description_placeholders"]["current_area"]
    assert unchanged["type"] is FlowResultType.CREATE_ENTRY
    assert manager.asset(ASSET_UUID)[CONF_HA_AREA_ID] == stale_area_id

    repair_flow, _menu = await _select_asset(hass, manager)
    repaired = await repair_flow.async_step_asset_deployment(
        {
            CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED,
            CONF_HA_AREA_ID: replacement.id,
        }
    )

    assert repaired["type"] is FlowResultType.CREATE_ENTRY
    assert manager.asset(ASSET_UUID)[CONF_HA_AREA_ID] == replacement.id


async def test_stale_area_can_be_explicitly_cleared(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """An unresolved ID can be removed without blocking the flow."""
    manager = _manager(
        hass,
        _deployed_store(asset_store_data, area_id="stale-area-id"),
    )
    flow, _menu = await _select_asset(hass, manager)

    result = await flow.async_step_asset_deployment(
        {
            CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED,
            CONF_CLEAR_HA_AREA: True,
        }
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert manager.asset(ASSET_UUID)[CONF_HA_AREA_ID] is None


@pytest.mark.parametrize(
    ("user_input", "error"),
    [
        ({CONF_DEPLOYMENT_STATE: "invented"}, "invalid_deployment_state"),
        (
            {
                CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED,
                CONF_INSTALLED_DATE: "not-a-date",
            },
            "invalid_installed_date",
        ),
        (
            {
                CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED,
                CONF_HA_AREA_ID: "missing-area-id",
            },
            "invalid_area",
        ),
    ],
)
async def test_invalid_deployment_input_is_flow_safe(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    user_input: dict,
    error: str,
) -> None:
    """Invalid state, date, and new Area choices never mutate the Store."""
    manager = _manager(hass, asset_store_data)
    before = deepcopy(manager._data)
    flow, _menu = await _select_asset(hass, manager)

    result = await flow.async_step_asset_deployment(user_input)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    assert manager._data == before
    assert manager._store.async_save.await_count == 0


async def test_asset_disappearing_during_deployment_returns_flow_error(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A concurrently removed Asset is not recreated under another identity."""
    manager = _manager(hass, asset_store_data)
    flow, _menu = await _select_asset(hass, manager)
    manager._data["assets"].pop(ASSET_UUID)

    result = await flow.async_step_asset_deployment(
        {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "manage_asset"
    assert result["errors"] == {"base": "asset_missing"}
    assert manager.asset_count == 0


async def test_store_save_failure_leaves_deployment_unchanged(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A failed atomic save is surfaced and publishes no partial update."""
    manager = _manager(hass, asset_store_data)
    manager._store.async_save = AsyncMock(side_effect=OSError("disk unavailable"))
    before = deepcopy(manager._data)
    flow, _menu = await _select_asset(hass, manager)

    result = await flow.async_step_asset_deployment(
        {CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_DEPLOYED}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "asset_deployment"
    assert result["errors"] == {"base": "asset_store_error"}
    assert manager._data == before
