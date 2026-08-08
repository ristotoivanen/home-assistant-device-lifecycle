"""Config, purchase and runtime subentry flows for Device Lifecycle."""

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
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
    CONF_INSTALLED_DATE,
    CONF_NOTES,
    CONF_POWER_THRESHOLD,
    CONF_PURCHASE_DATE,
    CONF_PURCHASE_NAME,
    CONF_PURCHASE_PRICE,
    CONF_RECEIPT_REFERENCE,
    CONF_RUNTIME_MODE,
    CONF_SELLER,
    CONF_SOURCE_ENTITY_ID,
    CONF_WARRANTY_TYPE,
    CONF_WARRANTY_UNTIL,
    DEFAULT_POWER_THRESHOLD,
    DOMAIN,
    RUNTIME_MODE_ON,
    RUNTIME_MODE_POWER,
    RUNTIME_MODES,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
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


def _runtime_start_schema(
    defaults: dict[str, Any] | None = None,
    *,
    include_device: bool,
) -> vol.Schema:
    """Build the first runtime step: target device and tracking method."""
    defaults = dict(defaults or {})
    fields: dict[Any, Any] = {}

    if include_device:
        device_key = vol.Required(CONF_DEVICE_ID)
        if defaults.get(CONF_DEVICE_ID):
            device_key = vol.Required(
                CONF_DEVICE_ID,
                default=defaults[CONF_DEVICE_ID],
            )
        fields[device_key] = selector.DeviceSelector()

    fields[
        vol.Required(
            CONF_RUNTIME_MODE,
            default=defaults.get(CONF_RUNTIME_MODE, RUNTIME_MODE_ON),
        )
    ] = selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=list(RUNTIME_MODES),
            translation_key="runtime_mode",
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )

    return vol.Schema(fields)


def _runtime_source_schema(
    runtime_mode: str,
    defaults: dict[str, Any] | None = None,
) -> vol.Schema:
    """Build the second runtime step with only relevant source settings."""
    defaults = dict(defaults or {})
    fields: dict[Any, Any] = {}

    source_key = vol.Required(CONF_SOURCE_ENTITY_ID)
    if defaults.get(CONF_SOURCE_ENTITY_ID):
        source_key = vol.Required(
            CONF_SOURCE_ENTITY_ID,
            default=defaults[CONF_SOURCE_ENTITY_ID],
        )
    fields[source_key] = selector.EntitySelector()

    if runtime_mode == RUNTIME_MODE_POWER:
        fields[
            vol.Required(
                CONF_POWER_THRESHOLD,
                default=defaults.get(
                    CONF_POWER_THRESHOLD,
                    DEFAULT_POWER_THRESHOLD,
                ),
            )
        ] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0,
                max=1000000,
                step=0.1,
                unit_of_measurement="W",
                mode=selector.NumberSelectorMode.BOX,
            )
        )

    return vol.Schema(fields)


def _compact_title(text: str, max_length: int = 52) -> str:
    """Return a compact Home Assistant subentry title."""
    text = text.strip()
    if len(text) > max_length:
        return text[: max_length - 1].rstrip() + "…"
    return text


def _purchase_title(data: dict[str, Any]) -> str:
    """Return a compact subentry title while keeping the full name in data."""
    if name := data.get(CONF_PURCHASE_NAME):
        return _compact_title(str(name))

    seller = str(data.get(CONF_SELLER) or "Ostos").strip()
    purchase_date = str(data.get(CONF_PURCHASE_DATE) or "").strip()
    return _compact_title(f"{seller} {purchase_date}".strip())


def _runtime_title(registry: dr.DeviceRegistry, device_id: str) -> str:
    """Return a compact runtime subentry title based on the target device."""
    device = registry.async_get(device_id)
    if device is None:
        return "Käyttötunnit"

    name = (
        getattr(device, "name_by_user", None)
        or getattr(device, "name", None)
        or getattr(device, "model", None)
        or "Käyttötunnit"
    )
    return _compact_title(str(name))


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


def _used_runtime_device_ids(
    entry: ConfigEntry,
    *,
    exclude_subentry_id: str | None = None,
) -> set[str]:
    """Return devices already having runtime tracking."""
    used: set[str] = set()

    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_RUNTIME:
            continue
        if subentry.subentry_id == exclude_subentry_id:
            continue
        if device_id := subentry.data.get(CONF_DEVICE_ID):
            used.add(str(device_id))

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

        data[CONF_WARRANTY_UNTIL] = warranty_until
        return data, None

    if warranty_type == WARRANTY_MANUAL:
        if not data.get(CONF_WARRANTY_UNTIL):
            return None, "manual_warranty_date_required"
        return data, None

    return None, "invalid_warranty_type"


def _prepare_runtime_data(
    user_input: dict[str, Any],
    *,
    device_id: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate and normalize runtime tracking data."""
    data = {
        key: value
        for key, value in user_input.items()
        if value not in (None, "")
    }

    if device_id is not None:
        data[CONF_DEVICE_ID] = device_id

    runtime_mode = str(data.get(CONF_RUNTIME_MODE, RUNTIME_MODE_ON))
    if runtime_mode not in RUNTIME_MODES:
        return None, "invalid_runtime_mode"

    if not data.get(CONF_SOURCE_ENTITY_ID):
        return None, "source_required"

    if runtime_mode == RUNTIME_MODE_POWER:
        if CONF_POWER_THRESHOLD not in data:
            return None, "power_threshold_required"
        try:
            threshold = float(data[CONF_POWER_THRESHOLD])
        except (TypeError, ValueError):
            return None, "invalid_power_threshold"
        if threshold < 0:
            return None, "invalid_power_threshold"
        data[CONF_POWER_THRESHOLD] = threshold
    else:
        data.pop(CONF_POWER_THRESHOLD, None)

    return data, None


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
            title=(
                "Laitteen elinkaari"
                if self.hass.config.language.lower().startswith("fi")
                else "Device Lifecycle"
            ),
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
        return {
            SUBENTRY_TYPE_PURCHASE: PurchaseSubentryFlow,
            SUBENTRY_TYPE_RUNTIME: RuntimeSubentryFlow,
        }


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


class RuntimeSubentryFlow(ConfigSubentryFlow):
    """Add and edit per-device runtime tracking."""

    def _set_runtime_context(
        self,
        *,
        device_id: str,
        runtime_mode: str,
        defaults: dict[str, Any] | None = None,
    ) -> None:
        """Store selections between the two runtime form steps."""
        self._runtime_context = {
            CONF_DEVICE_ID: device_id,
            CONF_RUNTIME_MODE: runtime_mode,
            "defaults": dict(defaults or {}),
        }

    def _get_runtime_context(self) -> dict[str, Any]:
        """Return selections stored between runtime form steps."""
        return getattr(self, "_runtime_context", {})

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> SubentryFlowResult:
        """Choose the device and runtime detection method."""
        entry = self._get_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            device_id = str(user_input.get(CONF_DEVICE_ID) or "")
            runtime_mode = str(
                user_input.get(CONF_RUNTIME_MODE, RUNTIME_MODE_ON)
            )
            registry = dr.async_get(self.hass)

            if not device_id or registry.async_get(device_id) is None:
                errors["base"] = "device_missing"
            elif device_id in _used_runtime_device_ids(entry):
                errors["base"] = "runtime_already_tracked"
            elif runtime_mode not in RUNTIME_MODES:
                errors["base"] = "invalid_runtime_mode"
            else:
                self._set_runtime_context(
                    device_id=device_id,
                    runtime_mode=runtime_mode,
                )
                return await self.async_step_runtime_source()

        return self.async_show_form(
            step_id="user",
            data_schema=_runtime_start_schema(
                user_input,
                include_device=True,
            ),
            errors=errors,
        )

    async def async_step_runtime_source(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> SubentryFlowResult:
        """Choose the runtime source and the optional power threshold."""
        context = self._get_runtime_context()
        if not context:
            return await self.async_step_user()

        entry = self._get_entry()
        device_id = str(context[CONF_DEVICE_ID])
        runtime_mode = str(context[CONF_RUNTIME_MODE])
        errors: dict[str, str] = {}

        if user_input is not None:
            source_entity_id = str(
                user_input.get(CONF_SOURCE_ENTITY_ID) or ""
            )

            if self.hass.states.get(source_entity_id) is None:
                errors["base"] = "source_missing"
            else:
                combined = {
                    CONF_DEVICE_ID: device_id,
                    CONF_RUNTIME_MODE: runtime_mode,
                    **user_input,
                }
                clean, error = _prepare_runtime_data(combined)
                if error:
                    errors["base"] = error
                elif clean is not None:
                    registry = dr.async_get(self.hass)
                    return self.async_create_entry(
                        title=_runtime_title(registry, device_id),
                        data=clean,
                    )

        return self.async_show_form(
            step_id="runtime_source",
            data_schema=_runtime_source_schema(
                runtime_mode,
                user_input,
            ),
            errors=errors,
        )

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> SubentryFlowResult:
        """Choose the runtime detection method for an existing tracker."""
        subentry = self._get_reconfigure_subentry()
        device_id = str(subentry.data.get(CONF_DEVICE_ID) or "")
        errors: dict[str, str] = {}

        if user_input is not None:
            runtime_mode = str(
                user_input.get(CONF_RUNTIME_MODE, RUNTIME_MODE_ON)
            )
            if runtime_mode not in RUNTIME_MODES:
                errors["base"] = "invalid_runtime_mode"
            else:
                self._set_runtime_context(
                    device_id=device_id,
                    runtime_mode=runtime_mode,
                    defaults=dict(subentry.data),
                )
                return await self.async_step_reconfigure_source()

        defaults = (
            user_input
            if user_input is not None
            else dict(subentry.data)
        )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_runtime_start_schema(
                defaults,
                include_device=False,
            ),
            errors=errors,
        )

    async def async_step_reconfigure_source(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> SubentryFlowResult:
        """Edit the source settings for an existing runtime tracker."""
        context = self._get_runtime_context()
        if not context:
            return await self.async_step_reconfigure()

        entry = self._get_entry()
        subentry = self._get_reconfigure_subentry()
        device_id = str(context[CONF_DEVICE_ID])
        runtime_mode = str(context[CONF_RUNTIME_MODE])
        saved_defaults = dict(context.get("defaults", {}))
        errors: dict[str, str] = {}

        if user_input is not None:
            source_entity_id = str(
                user_input.get(CONF_SOURCE_ENTITY_ID) or ""
            )

            if self.hass.states.get(source_entity_id) is None:
                errors["base"] = "source_missing"
            else:
                combined = {
                    CONF_RUNTIME_MODE: runtime_mode,
                    **user_input,
                }
                clean, error = _prepare_runtime_data(
                    combined,
                    device_id=device_id,
                )
                if error:
                    errors["base"] = error
                elif clean is not None:
                    registry = dr.async_get(self.hass)
                    return self.async_update_and_abort(
                        entry,
                        subentry,
                        title=_runtime_title(registry, device_id),
                        data=clean,
                    )

        defaults = (
            user_input
            if user_input is not None
            else saved_defaults
        )

        return self.async_show_form(
            step_id="reconfigure_source",
            data_schema=_runtime_source_schema(
                runtime_mode,
                defaults,
            ),
            errors=errors,
        )
