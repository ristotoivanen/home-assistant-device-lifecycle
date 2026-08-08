"""Config and purchase subentry flows for Device Lifecycle."""

from __future__ import annotations

from datetime import date
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlowResult,
    ConfigSubentryFlow,
    FlowType,
    SOURCE_USER,
    SubentryFlowContext,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEVICE_IDS,
    CONF_INSTALLED_DATE,
    CONF_NOTES,
    CONF_PURCHASE_DATE,
    CONF_PURCHASE_NAME,
    CONF_PURCHASE_PRICE,
    CONF_RECEIPT_REFERENCE,
    CONF_SELLER,
    CONF_WARRANTY_TYPE,
    CONF_WARRANTY_UNTIL,
    DOMAIN,
    SUBENTRY_TYPE_PURCHASE,
    WARRANTY_MANUAL,
    WARRANTY_NONE,
    WARRANTY_ONE_YEAR,
    WARRANTY_TWO_YEARS,
    WARRANTY_TYPES,
)

MAIN_UNIQUE_ID = "device_lifecycle_main"


def _text_selector(*, multiline: bool = False) -> selector.TextSelector:
    """Return a text selector."""
    return selector.TextSelector(
        selector.TextSelectorConfig(
            multiline=multiline,
            type=selector.TextSelectorType.TEXT,
        )
    )


def _infer_warranty_type(data: dict[str, Any]) -> str:
    """Infer warranty type for entries created before 0.3.4."""
    warranty_type = data.get(CONF_WARRANTY_TYPE)
    if warranty_type in WARRANTY_TYPES:
        return str(warranty_type)

    if data.get(CONF_WARRANTY_UNTIL):
        return WARRANTY_MANUAL

    return WARRANTY_NONE


def _purchase_defaults(data: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize defaults, including old 0.3.x purchases."""
    defaults = dict(data or {})
    defaults[CONF_WARRANTY_TYPE] = _infer_warranty_type(defaults)
    return defaults


def _purchase_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Build the add/edit purchase form."""
    defaults = _purchase_defaults(defaults)

    def optional(key: str, sel: Any):
        if key in defaults and defaults[key] not in (None, ""):
            return vol.Optional(key, default=defaults[key]), sel
        return vol.Optional(key), sel

    fields: dict[Any, Any] = {
        vol.Required(
            CONF_DEVICE_IDS,
            default=defaults.get(CONF_DEVICE_IDS, []),
        ): selector.DeviceSelector(
            selector.DeviceSelectorConfig(multiple=True)
        ),
    }

    key, val = optional(CONF_PURCHASE_NAME, _text_selector())
    fields[key] = val

    key, val = optional(CONF_PURCHASE_DATE, selector.DateSelector())
    fields[key] = val

    key, val = optional(CONF_INSTALLED_DATE, selector.DateSelector())
    fields[key] = val

    fields[
        vol.Required(
            CONF_WARRANTY_TYPE,
            default=defaults.get(CONF_WARRANTY_TYPE, WARRANTY_NONE),
        )
    ] = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=list(WARRANTY_TYPES),
            translation_key="warranty_type",
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )

    key, val = optional(CONF_WARRANTY_UNTIL, selector.DateSelector())
    fields[key] = val

    key, val = optional(CONF_SELLER, _text_selector())
    fields[key] = val

    key, val = optional(
        CONF_PURCHASE_PRICE,
        selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0,
                max=1000000,
                step=0.01,
                unit_of_measurement="€",
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
    )
    fields[key] = val

    key, val = optional(CONF_RECEIPT_REFERENCE, _text_selector())
    fields[key] = val

    key, val = optional(CONF_NOTES, _text_selector(multiline=True))
    fields[key] = val

    return vol.Schema(fields)


def _purchase_title(data: dict[str, Any]) -> str:
    """Return a compact subentry title while keeping the full name in data."""
    if name := data.get(CONF_PURCHASE_NAME):
        text = str(name).strip()
        max_length = 52
        if len(text) > max_length:
            text = text[: max_length - 1].rstrip() + "…"
        return text

    seller = str(data.get(CONF_SELLER) or "Ostos").strip()
    purchase_date = str(data.get(CONF_PURCHASE_DATE) or "").strip()
    return f"{seller} {purchase_date}".strip()


def _used_device_ids(
    entry: ConfigEntry,
    *,
    exclude_subentry_id: str | None = None,
) -> set[str]:
    """Return devices already used by another purchase."""
    used: set[str] = set()

    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_PURCHASE:
            continue
        if subentry.subentry_id == exclude_subentry_id:
            continue
        used.update(subentry.data.get(CONF_DEVICE_IDS, []))

    return used


def _add_years(value: str, years: int) -> str | None:
    """Add calendar years to a YYYY-MM-DD date safely."""
    purchase_date: date | None = dt_util.parse_date(value)
    if purchase_date is None:
        return None

    try:
        result = purchase_date.replace(year=purchase_date.year + years)
    except ValueError:
        # Handles leap-day purchases: 29 Feb -> 28 Feb.
        result = purchase_date.replace(
            year=purchase_date.year + years,
            month=2,
            day=28,
        )

    return result.isoformat()


def _prepare_purchase_data(
    user_input: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate and normalize warranty data before saving."""
    data = {
        key: value
        for key, value in user_input.items()
        if value not in (None, "")
    }

    warranty_type = str(data.get(CONF_WARRANTY_TYPE, WARRANTY_NONE))
    data[CONF_WARRANTY_TYPE] = warranty_type

    if warranty_type == WARRANTY_NONE:
        data.pop(CONF_WARRANTY_UNTIL, None)
        return data, None

    if warranty_type in (WARRANTY_ONE_YEAR, WARRANTY_TWO_YEARS):
        purchase_date = data.get(CONF_PURCHASE_DATE)
        if not purchase_date:
            return None, "purchase_date_required_for_warranty"

        years = 1 if warranty_type == WARRANTY_ONE_YEAR else 2
        warranty_until = _add_years(str(purchase_date), years)
        if warranty_until is None:
            return None, "invalid_purchase_date"

        # Ignore any old/manual date and always recalculate from purchase date.
        data[CONF_WARRANTY_UNTIL] = warranty_until
        return data, None

    if warranty_type == WARRANTY_MANUAL:
        if not data.get(CONF_WARRANTY_UNTIL):
            return None, "manual_warranty_date_required"
        return data, None

    return None, "invalid_warranty_type"


class DeviceLifecycleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create exactly one parent integration entry."""

    VERSION = 3

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Create the parent entry once."""
        await self.async_set_unique_id(MAIN_UNIQUE_ID)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title="Laitteen elinkaari",
            data={},
        )

    async def async_on_create_entry(
        self,
        result: ConfigFlowResult,
    ) -> ConfigFlowResult:
        """Immediately open the first purchase flow after parent creation."""
        subentry_result = await self.hass.config_entries.subentries.async_init(
            (result["result"].entry_id, SUBENTRY_TYPE_PURCHASE),
            context=SubentryFlowContext(source=SOURCE_USER),
        )
        result["next_flow"] = (
            FlowType.CONFIG_SUBENTRIES_FLOW,
            subentry_result["flow_id"],
        )
        return result

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls,
        config_entry: ConfigEntry,
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return supported subentry types."""
        return {SUBENTRY_TYPE_PURCHASE: PurchaseSubentryFlow}


class PurchaseSubentryFlow(ConfigSubentryFlow):
    """Add and edit purchases under the single parent integration."""

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> SubentryFlowResult:
        """Add one purchase containing one or many devices."""
        entry = self._get_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            device_ids = list(user_input.get(CONF_DEVICE_IDS, []))

            if not device_ids:
                errors["base"] = "no_devices"
            else:
                registry = dr.async_get(self.hass)

                if any(registry.async_get(device_id) is None for device_id in device_ids):
                    errors["base"] = "device_missing"
                elif _used_device_ids(entry).intersection(device_ids):
                    errors["base"] = "already_tracked"
                else:
                    clean, error = _prepare_purchase_data(user_input)
                    if error:
                        errors["base"] = error
                    elif clean is not None:
                        return self.async_create_entry(
                            title=_purchase_title(clean),
                            data=clean,
                        )

        return self.async_show_form(
            step_id="user",
            data_schema=_purchase_schema(user_input),
            errors=errors,
        )

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> SubentryFlowResult:
        """Edit an existing purchase."""
        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        errors: dict[str, str] = {}

        if user_input is not None:
            device_ids = list(user_input.get(CONF_DEVICE_IDS, []))

            if not device_ids:
                errors["base"] = "no_devices"
            else:
                registry = dr.async_get(self.hass)

                if any(registry.async_get(device_id) is None for device_id in device_ids):
                    errors["base"] = "device_missing"
                elif _used_device_ids(
                    entry,
                    exclude_subentry_id=subentry.subentry_id,
                ).intersection(device_ids):
                    errors["base"] = "already_tracked"
                else:
                    clean, error = _prepare_purchase_data(user_input)
                    if error:
                        errors["base"] = error
                    elif clean is not None:
                        return self.async_update_and_abort(
                            entry,
                            subentry,
                            title=_purchase_title(clean),
                            data=clean,
                        )

        defaults = (
            user_input
            if user_input is not None
            else _purchase_defaults(dict(subentry.data))
        )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_purchase_schema(defaults),
            errors=errors,
        )
