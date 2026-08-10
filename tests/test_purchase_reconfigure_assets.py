"""Purchase reconfigure Asset membership presentation tests."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import selector
import voluptuous as vol

from custom_components.device_lifecycle.config_flow import PurchaseSubentryFlow
from custom_components.device_lifecycle.const import (
    CONF_DEPLOYMENT_STATE,
    CONF_DEVICE_IDS,
    CONF_HA_AREA_ID,
    CONF_INSTALLED_DATE,
    CONF_PURCHASE_NAME,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    SUBENTRY_TYPE_PURCHASE,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import AssetStoreManager

from .conftest import (
    ASSET_UUID,
    PURCHASE_SUBENTRY_ID,
    PURCHASE_UUID,
)

MANUAL_ASSET_UUID = "33333333-3333-4333-8333-333333333333"


def _manager(
    hass: HomeAssistant,
    data: AssetStoreData,
) -> AssetStoreManager:
    """Return an isolated manager with persistent saves mocked."""
    manager = AssetStoreManager(hass)
    manager._data = deepcopy(data)
    manager._store.async_save = AsyncMock()
    return manager


def _flow_context(
    hass: HomeAssistant,
    manager: AssetStoreManager,
    subentry_data: dict,
) -> tuple[PurchaseSubentryFlow, SimpleNamespace, SimpleNamespace]:
    """Return a Purchase flow and its lightweight config entry context."""
    subentry = SimpleNamespace(
        subentry_id=PURCHASE_SUBENTRY_ID,
        subentry_type=SUBENTRY_TYPE_PURCHASE,
        title=str(subentry_data.get(CONF_PURCHASE_NAME) or "Purchase"),
        data=deepcopy(subentry_data),
    )
    entry = SimpleNamespace(
        entry_id="device-lifecycle-entry-id",
        subentries={PURCHASE_SUBENTRY_ID: subentry},
        runtime_data=manager,
    )
    flow = PurchaseSubentryFlow()
    flow.hass = hass
    return flow, entry, subentry


def _store_with_manual_asset(
    asset_store_data: AssetStoreData,
) -> AssetStoreData:
    """Add one user-managed, relationship-only Asset to the fixture Store."""
    data = deepcopy(asset_store_data)
    manual_asset = deepcopy(data["assets"][ASSET_UUID])
    manual_asset.update(
        {
            "asset_uuid": MANUAL_ASSET_UUID,
            "asset_id": "DL0018",
            "name": "Testilaite 0.5.4",
            CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED,
            CONF_INSTALLED_DATE: None,
            CONF_HA_AREA_ID: None,
            "ha_device_refs": [],
        }
    )
    manual_asset["field_sources"]["purchase_uuid"] = "user"
    data["assets"][MANUAL_ASSET_UUID] = manual_asset
    data["purchases"][PURCHASE_UUID]["asset_uuids"] = [
        MANUAL_ASSET_UUID,
        ASSET_UUID,
    ]
    data["next_asset_number"] = 19
    return data


async def test_reconfigure_shows_manual_and_ha_assets_sorted_by_asset_id(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict,
) -> None:
    """Canonical membership includes both Asset kinds in stable ID order."""
    manager = _manager(hass, _store_with_manual_asset(asset_store_data))
    flow, entry, subentry = _flow_context(
        hass,
        manager,
        purchase_subentry_data,
    )

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(
            flow,
            "_get_reconfigure_subentry",
            return_value=subentry,
        ),
    ):
        result = await flow.async_step_reconfigure()

    assert result["type"] is FlowResultType.FORM
    assert result["description_placeholders"]["linked_assets"].splitlines() == [
        "- DL0007 — Workshop device",
        "- DL0018 — Testilaite 0.5.4",
    ]


async def test_reconfigure_empty_purchase_has_localized_empty_state(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict,
) -> None:
    """A Purchase with no members shows English and Finnish empty copy."""
    data = deepcopy(asset_store_data)
    data["assets"] = {}
    data["purchases"][PURCHASE_UUID]["asset_uuids"] = []
    manager = _manager(hass, data)
    flow, entry, subentry = _flow_context(
        hass,
        manager,
        purchase_subentry_data,
    )

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(
            flow,
            "_get_reconfigure_subentry",
            return_value=subentry,
        ),
    ):
        for language, expected in (
            ("en", "No Device Lifecycle Assets are linked."),
            ("fi", "Ei liitettyjä elinkaarilaitteita."),
        ):
            hass.config.language = language
            result = await flow.async_step_reconfigure()
            assert result["description_placeholders"]["linked_assets"] == expected


async def test_reconfigure_keeps_existing_device_selector_behavior(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict,
) -> None:
    """The informational Asset list does not replace or alter DeviceSelector."""
    manager = _manager(hass, asset_store_data)
    flow, entry, subentry = _flow_context(
        hass,
        manager,
        purchase_subentry_data,
    )

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(
            flow,
            "_get_reconfigure_subentry",
            return_value=subentry,
        ),
    ):
        result = await flow.async_step_reconfigure()

    marker = next(
        marker
        for marker in result["data_schema"].schema
        if getattr(marker, "schema", None) == CONF_DEVICE_IDS
    )
    device_selector = result["data_schema"].schema[marker]
    assert isinstance(marker, vol.Optional)
    assert isinstance(device_selector, selector.DeviceSelector)
    assert device_selector.config["multiple"] is True
    assert marker.default() == purchase_subentry_data[CONF_DEVICE_IDS]


async def test_saving_reconfigure_preserves_user_managed_asset_membership(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict,
) -> None:
    """Opening and saving cannot detach a manually assigned Asset."""
    data = _store_with_manual_asset(asset_store_data)
    data["assets"] = {MANUAL_ASSET_UUID: data["assets"][MANUAL_ASSET_UUID]}
    data["purchases"][PURCHASE_UUID]["asset_uuids"] = [MANUAL_ASSET_UUID]
    manager = _manager(hass, data)
    reconfigure_data = deepcopy(purchase_subentry_data)
    reconfigure_data[CONF_DEVICE_IDS] = []
    flow, entry, subentry = _flow_context(hass, manager, reconfigure_data)
    update_result = {"type": "abort", "reason": "reconfigure_successful"}

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(
            flow,
            "_get_reconfigure_subentry",
            return_value=subentry,
        ),
        patch.object(
            flow,
            "async_update_and_abort",
            new=Mock(return_value=update_result),
        ) as update_entry,
    ):
        form = await flow.async_step_reconfigure()
        result = await flow.async_step_reconfigure(reconfigure_data)

    assert "DL0018 — Testilaite 0.5.4" in form["description_placeholders"][
        "linked_assets"
    ]
    assert result == update_result
    subentry.data = update_entry.call_args.kwargs["data"]

    with patch(
        "custom_components.device_lifecycle.storage.dr.async_get",
        return_value=Mock(),
    ):
        await manager.async_reconcile_entry(entry)

    asset = manager.asset(MANUAL_ASSET_UUID)
    assert asset is not None
    assert asset["purchase_uuid"] == PURCHASE_UUID
    assert asset["field_sources"]["purchase_uuid"] == "user"
    assert manager.purchase(PURCHASE_UUID)["asset_uuids"] == [MANUAL_ASSET_UUID]
