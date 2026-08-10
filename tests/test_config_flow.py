"""Regression tests for Device Lifecycle 0.5.3 flow normalization."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from homeassistant.core import HomeAssistant

from custom_components.device_lifecycle.config_flow import (
    PurchaseSubentryFlow,
    _prepare_purchase_data,
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
    CONF_POWER_HYSTERESIS,
    CONF_POWER_THRESHOLD,
    CONF_PURCHASE_DATE,
    CONF_PURCHASE_UUID,
    CONF_RUNTIME_MODE,
    CONF_SOURCE_ENTITY_ID,
    CONF_WARRANTY_TYPE,
    CONF_WARRANTY_UNTIL,
    RUNTIME_MODE_POWER,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
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


async def test_0_5_3_purchase_flow_requires_a_device_as_intentional_change_point(
    hass: HomeAssistant,
) -> None:
    """Capture the current restriction that 0.5.4 will intentionally remove."""
    flow = PurchaseSubentryFlow()
    flow.hass = hass
    entry = SimpleNamespace(subentries={})
    form_result = {"type": "form"}

    with (
        patch.object(flow, "_get_entry", return_value=entry),
        patch.object(
            flow,
            "async_show_form",
            new=Mock(return_value=form_result),
        ) as show_form,
    ):
        result = await flow.async_step_user({CONF_DEVICE_IDS: []})

    assert result == form_result
    assert show_form.call_args.kwargs["errors"] == {"base": "no_devices"}
