"""Regression tests for Device Lifecycle 0.5.3 flow normalization."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from homeassistant.core import HomeAssistant
import voluptuous as vol

from custom_components.device_lifecycle.config_flow import (
    PurchaseSubentryFlow,
    _prepare_purchase_data,
    _purchase_schema,
    _prepare_runtime_data,
    _used_device_ids,
    _used_runtime_device_ids,
)
from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_CURRENCY,
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
    CONF_INSTALLED_DATE,
    CONF_NOTES,
    CONF_POWER_HYSTERESIS,
    CONF_POWER_THRESHOLD,
    CONF_PURCHASE_DATE,
    CONF_PURCHASE_NAME,
    CONF_PURCHASE_PRICE,
    CONF_PURCHASE_UUID,
    CONF_RECEIPT_REFERENCE,
    CONF_RECEIPT_URL,
    CONF_RUNTIME_MODE,
    CONF_SELLER,
    CONF_SOURCE_ENTITY_ID,
    CONF_WARRANTY_TYPE,
    CONF_WARRANTY_UNTIL,
    RUNTIME_MODE_POWER,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
    WARRANTY_NONE,
    WARRANTY_TWO_YEARS,
)

from .conftest import (
    ASSET_UUID,
    DEVICE_ID,
    PURCHASE_SUBENTRY_ID,
    PURCHASE_UUID,
    RUNTIME_SUBENTRY_ID,
    SOURCE_ENTITY_ID,
)


def test_new_purchase_defaults_installation_date_and_calculates_warranty() -> None:
    """Protect 0.5.3 Purchase defaults for newly created purchases."""
    clean, error = _prepare_purchase_data(
        {
            CONF_DEVICE_IDS: [DEVICE_ID],
            CONF_PURCHASE_DATE: "2026-02-28",
            CONF_WARRANTY_TYPE: WARRANTY_TWO_YEARS,
        },
        default_currency="EUR",
    )

    assert error is None
    assert clean is not None
    assert clean[CONF_INSTALLED_DATE] == "2026-02-28"
    assert clean[CONF_WARRANTY_UNTIL] == "2028-02-28"
    assert clean[CONF_CURRENCY] == "EUR"


def test_purchase_reconfigure_preserves_uuid_currency_and_cleared_date() -> None:
    """Protect stable references and independent date clearing on reconfigure."""
    clean, error = _prepare_purchase_data(
        {
            CONF_DEVICE_IDS: [DEVICE_ID],
            CONF_PURCHASE_DATE: "2026-02-28",
            CONF_WARRANTY_TYPE: WARRANTY_TWO_YEARS,
        },
        preserved_data={
            CONF_PURCHASE_UUID: PURCHASE_UUID,
            CONF_CURRENCY: "SEK",
            CONF_INSTALLED_DATE: "2026-03-01",
        },
        default_currency="EUR",
    )

    assert error is None
    assert clean is not None
    assert CONF_INSTALLED_DATE not in clean
    assert clean[CONF_PURCHASE_UUID] == PURCHASE_UUID
    assert clean[CONF_CURRENCY] == "SEK"


def test_runtime_reconfigure_preserves_target_asset_and_hysteresis() -> None:
    """Protect the immutable Runtime target and existing power behavior."""
    clean, error = _prepare_runtime_data(
        {
            CONF_RUNTIME_MODE: RUNTIME_MODE_POWER,
            CONF_SOURCE_ENTITY_ID: SOURCE_ENTITY_ID,
            CONF_POWER_THRESHOLD: 10.0,
            CONF_POWER_HYSTERESIS: 2.0,
        },
        device_id=DEVICE_ID,
        asset_uuid=ASSET_UUID,
    )

    assert error is None
    assert clean is not None
    assert clean[CONF_DEVICE_ID] == DEVICE_ID
    assert clean[CONF_ASSET_UUID] == ASSET_UUID
    assert clean[CONF_POWER_THRESHOLD] == 10.0
    assert clean[CONF_POWER_HYSTERESIS] == 2.0


def test_used_device_helpers_keep_purchase_and_runtime_scopes_separate() -> None:
    """Protect duplicate checks without conflating Purchase and Runtime usage."""
    purchase = SimpleNamespace(
        subentry_id=PURCHASE_SUBENTRY_ID,
        subentry_type=SUBENTRY_TYPE_PURCHASE,
        data={CONF_DEVICE_IDS: [DEVICE_ID]},
    )
    runtime = SimpleNamespace(
        subentry_id=RUNTIME_SUBENTRY_ID,
        subentry_type=SUBENTRY_TYPE_RUNTIME,
        data={CONF_DEVICE_ID: DEVICE_ID},
    )
    entry = SimpleNamespace(
        subentries={
            purchase.subentry_id: purchase,
            runtime.subentry_id: runtime,
        }
    )

    assert _used_device_ids(entry) == {DEVICE_ID}
    assert _used_runtime_device_ids(entry) == {DEVICE_ID}
    assert _used_device_ids(
        entry,
        exclude_subentry_id=PURCHASE_SUBENTRY_ID,
    ) == set()
    assert _used_runtime_device_ids(
        entry,
        exclude_subentry_id=RUNTIME_SUBENTRY_ID,
    ) == set()


def test_empty_purchase_does_not_default_installation_date() -> None:
    """An empty Purchase carries no placeholder Asset installation metadata."""
    clean, error = _prepare_purchase_data(
        {
            CONF_PURCHASE_DATE: "2026-02-28",
            CONF_WARRANTY_TYPE: WARRANTY_NONE,
        },
        default_currency="EUR",
    )

    assert error is None
    assert clean is not None
    assert CONF_DEVICE_IDS not in clean
    assert CONF_INSTALLED_DATE not in clean


def test_purchase_schema_makes_device_selection_optional(
    hass: HomeAssistant,
) -> None:
    """The Purchase form no longer requires the HA device selector field."""
    schema = _purchase_schema(hass)
    device_marker = next(
        marker
        for marker in schema.schema
        if getattr(marker, "schema", None) == CONF_DEVICE_IDS
    )

    assert isinstance(device_marker, vol.Optional)


async def test_purchase_flow_creates_purchase_without_devices(
    hass: HomeAssistant,
) -> None:
    """Purchase creation accepts no device field and retains all metadata."""
    flow = PurchaseSubentryFlow()
    flow.hass = hass
    entry = SimpleNamespace(subentries={})
    create_result = {"type": "create_entry"}
    user_input = {
        CONF_PURCHASE_NAME: "Future equipment",
        CONF_PURCHASE_DATE: "2026-08-10",
        CONF_SELLER: "Example seller",
        CONF_PURCHASE_PRICE: 249.95,
        CONF_RECEIPT_REFERENCE: "ORDER-EMPTY",
        CONF_RECEIPT_URL: "https://example.invalid/receipt",
        CONF_NOTES: "Assets will be received later",
        CONF_WARRANTY_TYPE: WARRANTY_NONE,
    }

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(
            flow,
            "async_create_entry",
            new=Mock(return_value=create_result),
        ) as create_entry,
        patch(
            "custom_components.device_lifecycle.config_flow.dr.async_get"
        ) as registry_get,
    ):
        result = await flow.async_step_user(user_input)

    assert result == create_result
    registry_get.assert_not_called()
    stored = create_entry.call_args.kwargs["data"]
    assert CONF_DEVICE_IDS not in stored
    assert CONF_INSTALLED_DATE not in stored
    assert stored == {
        **user_input,
        CONF_CURRENCY: str(hass.config.currency),
    }


async def test_purchase_flow_accepts_explicit_empty_device_selection(
    hass: HomeAssistant,
) -> None:
    """An explicit empty selector value is valid during Purchase creation."""
    flow = PurchaseSubentryFlow()
    flow.hass = hass
    entry = SimpleNamespace(subentries={})
    create_result = {"type": "create_entry"}

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(
            flow,
            "async_create_entry",
            new=Mock(return_value=create_result),
        ) as create_entry,
    ):
        result = await flow.async_step_user(
            {
                CONF_DEVICE_IDS: [],
                CONF_PURCHASE_NAME: "Empty selection",
                CONF_WARRANTY_TYPE: WARRANTY_NONE,
            }
        )

    assert result == create_result
    assert create_entry.call_args.kwargs["data"][CONF_DEVICE_IDS] == []


async def test_zero_device_purchase_can_be_reconfigured(
    hass: HomeAssistant,
) -> None:
    """An existing empty Purchase remains editable without adding devices."""
    flow = PurchaseSubentryFlow()
    flow.hass = hass
    subentry = SimpleNamespace(
        subentry_id=PURCHASE_SUBENTRY_ID,
        subentry_type=SUBENTRY_TYPE_PURCHASE,
        data={
            CONF_DEVICE_IDS: [],
            CONF_PURCHASE_NAME: "Future equipment",
            CONF_PURCHASE_UUID: PURCHASE_UUID,
            CONF_CURRENCY: "SEK",
            CONF_WARRANTY_TYPE: WARRANTY_NONE,
        },
    )
    entry = SimpleNamespace(subentries={PURCHASE_SUBENTRY_ID: subentry})
    update_result = {"type": "abort", "reason": "reconfigure_successful"}

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(flow, "_get_reconfigure_subentry", return_value=subentry),
        patch.object(
            flow,
            "async_update_and_abort",
            new=Mock(return_value=update_result),
        ) as update_entry,
        patch(
            "custom_components.device_lifecycle.config_flow.dr.async_get"
        ) as registry_get,
    ):
        result = await flow.async_step_reconfigure(
            {
                CONF_DEVICE_IDS: [],
                CONF_PURCHASE_NAME: "Edited future equipment",
                CONF_NOTES: "Still waiting",
                CONF_WARRANTY_TYPE: WARRANTY_NONE,
            }
        )

    assert result == update_result
    registry_get.assert_not_called()
    stored = update_entry.call_args.kwargs["data"]
    assert stored[CONF_DEVICE_IDS] == []
    assert stored[CONF_PURCHASE_NAME] == "Edited future equipment"
    assert stored[CONF_NOTES] == "Still waiting"
    assert stored[CONF_PURCHASE_UUID] == PURCHASE_UUID
    assert stored[CONF_CURRENCY] == "SEK"


async def test_empty_purchase_reconfigure_can_add_devices_later(
    hass: HomeAssistant,
) -> None:
    """The existing Purchase edit flow can add HA devices after creation."""
    flow = PurchaseSubentryFlow()
    flow.hass = hass
    subentry = SimpleNamespace(
        subentry_id=PURCHASE_SUBENTRY_ID,
        subentry_type=SUBENTRY_TYPE_PURCHASE,
        data={
            CONF_PURCHASE_NAME: "Purchase first",
            CONF_PURCHASE_UUID: PURCHASE_UUID,
            CONF_CURRENCY: "EUR",
            CONF_WARRANTY_TYPE: WARRANTY_NONE,
        },
    )
    entry = SimpleNamespace(subentries={PURCHASE_SUBENTRY_ID: subentry})
    registry = Mock()
    registry.async_get.return_value = SimpleNamespace(entry_type=None)
    update_result = {"type": "abort", "reason": "reconfigure_successful"}

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(flow, "_get_reconfigure_subentry", return_value=subentry),
        patch.object(
            flow,
            "async_update_and_abort",
            new=Mock(return_value=update_result),
        ) as update_entry,
        patch(
            "custom_components.device_lifecycle.config_flow.dr.async_get",
            return_value=registry,
        ),
    ):
        result = await flow.async_step_reconfigure(
            {
                CONF_DEVICE_IDS: [DEVICE_ID],
                CONF_PURCHASE_NAME: "Purchase first",
                CONF_WARRANTY_TYPE: WARRANTY_NONE,
            }
        )

    assert result == update_result
    stored = update_entry.call_args.kwargs["data"]
    assert stored[CONF_DEVICE_IDS] == [DEVICE_ID]
    assert stored[CONF_PURCHASE_UUID] == PURCHASE_UUID
    assert stored[CONF_CURRENCY] == "EUR"
