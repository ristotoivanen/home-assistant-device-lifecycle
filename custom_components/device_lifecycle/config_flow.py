"""Config, purchase and runtime subentry flows for Device Lifecycle."""

from __future__ import annotations

from datetime import date
from math import isfinite
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlowResult,
    ConfigSubentryFlow,
    FlowType,
    OptionsFlow,
    SOURCE_USER,
    SubentryFlowContext,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import PowerConverter

from .const import (
    CONFIG_ENTRY_VERSION,
    CONF_ASSET_NAME,
    CONF_ASSET_UUID,
    CONF_CATEGORY,
    CONF_CLEAR_HA_AREA,
    CONF_CLEAR_INSTALLED_DATE,
    CONF_CONFIRM_AREA_CLEAR,
    CONF_CURRENCY,
    CONF_DEPLOYMENT_STATE,
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
    CONF_HW_VERSION,
    CONF_HA_AREA_ID,
    CONF_HA_RELATIONSHIP_ACTION,
    CONF_INSTALLED_DATE,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_MODEL_ID,
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
    CONF_SERIAL_NUMBER,
    CONF_SOURCE_ENTITY_ID,
    CONF_SW_VERSION,
    CONF_WARRANTY_TYPE,
    CONF_WARRANTY_UNTIL,
    DEFAULT_POWER_HYSTERESIS,
    DEFAULT_POWER_THRESHOLD,
    DEPLOYMENT_STATES,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DOMAIN,
    HA_RELATIONSHIP_ACTION_REPLACE,
    HA_RELATIONSHIP_ACTION_UNLINK,
    HA_RELATIONSHIP_ACTIONS,
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
from .models import AssetData, PurchaseData
from .storage import AssetStoreError, AssetStoreManager

MAIN_UNIQUE_ID = "device_lifecycle_main"
NO_PURCHASE_SELECTION = "__no_purchase__"

ASSET_METADATA_FIELDS = (
    CONF_ASSET_NAME,
    CONF_CATEGORY,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_MODEL_ID,
    CONF_SERIAL_NUMBER,
    CONF_SW_VERSION,
    CONF_HW_VERSION,
    CONF_NOTES,
)

ON_STATE_SOURCE_DOMAINS = frozenset(
    {
        "binary_sensor",
        "fan",
        "input_boolean",
        "light",
        "switch",
    }
)

# Keep this list deliberately short and conservative. These integrations create
# software/system devices rather than physical assets users would normally buy.
DEVICE_EXCLUDED_INTEGRATIONS = frozenset(
    {
        DOMAIN,
        "better_thermostat",
        "browser_mod",
        "hacs",
        "hassio",
        "spook",
    }
)


def _text_selector(
    *,
    multiline: bool = False,
    selector_type: selector.TextSelectorType = selector.TextSelectorType.TEXT,
) -> selector.TextSelector:
    """Return a text selector."""
    return selector.TextSelector(
        selector.TextSelectorConfig(
            multiline=multiline,
            type=selector_type,
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


def _physical_device_selector(
    hass: HomeAssistant,
    *,
    multiple: bool,
) -> selector.DeviceSelector:
    """Return a conservative selector for physical-device candidates."""
    integrations = sorted(
        {
            entry.domain
            for entry in hass.config_entries.async_entries()
            if entry.domain not in DEVICE_EXCLUDED_INTEGRATIONS
        }
    )

    if not integrations:
        return selector.DeviceSelector(
            selector.DeviceSelectorConfig(multiple=multiple)
        )

    return selector.DeviceSelector(
        selector.DeviceSelectorConfig(
            multiple=multiple,
            filter=[
                selector.DeviceFilterSelectorConfig(integration=integration)
                for integration in integrations
            ],
        )
    )


def _is_service_device(
    registry: dr.DeviceRegistry,
    device_id: str,
) -> bool:
    """Return whether a registry entry represents a HA service, not an asset."""
    device = registry.async_get(device_id)
    if device is None:
        return False

    entry_type = getattr(device, "entry_type", None)
    return str(getattr(entry_type, "value", entry_type)) == "service"


def _primary_device_id(asset: AssetData) -> str | None:
    """Return the stored primary HA relationship without using it as identity."""
    for reference in asset.get("ha_device_refs", []):
        if reference.get("role") == "primary":
            return str(reference.get("device_id") or "") or None
    return None


def _related_device_ids(asset: AssetData) -> list[str]:
    """Return all stored related HA relationships in persistent order."""
    return [
        str(reference.get("device_id") or "")
        for reference in asset.get("ha_device_refs", [])
        if reference.get("role") == "related" and reference.get("device_id")
    ]


def _purchase_schema(
    hass: HomeAssistant,
    defaults: dict[str, Any] | None = None,
) -> vol.Schema:
    """Build the add/edit purchase form."""
    defaults = _purchase_defaults(defaults)

    def optional(key: str, sel: Any):
        if key in defaults and defaults[key] not in (None, ""):
            return vol.Optional(key, default=defaults[key]), sel
        return vol.Optional(key), sel

    fields: dict[Any, Any] = {
        vol.Optional(
            CONF_DEVICE_IDS,
            default=defaults.get(CONF_DEVICE_IDS, []),
        ): _physical_device_selector(hass, multiple=True),
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

    currency = str(defaults.get(CONF_CURRENCY) or hass.config.currency)
    key, val = optional(
        CONF_PURCHASE_PRICE,
        selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0,
                max=1000000,
                step=0.01,
                unit_of_measurement=currency,
                mode=selector.NumberSelectorMode.BOX,
            )
        ),
    )
    fields[key] = val

    key, val = optional(CONF_RECEIPT_REFERENCE, _text_selector())
    fields[key] = val

    key, val = optional(
        CONF_RECEIPT_URL,
        _text_selector(selector_type=selector.TextSelectorType.URL),
    )
    fields[key] = val

    key, val = optional(CONF_NOTES, _text_selector(multiline=True))
    fields[key] = val

    return vol.Schema(fields)


def _asset_metadata_schema(
    purchase_options: list[selector.SelectOptionDict] | None = None,
) -> vol.Schema:
    """Build the manual Asset create/edit metadata form."""
    fields: dict[Any, Any] = {
        vol.Required(CONF_ASSET_NAME): _text_selector(),
        vol.Optional(CONF_CATEGORY): _text_selector(),
        vol.Optional(CONF_MANUFACTURER): _text_selector(),
        vol.Optional(CONF_MODEL): _text_selector(),
        vol.Optional(CONF_MODEL_ID): _text_selector(),
        vol.Optional(CONF_SERIAL_NUMBER): _text_selector(),
        vol.Optional(CONF_SW_VERSION): _text_selector(),
        vol.Optional(CONF_HW_VERSION): _text_selector(),
        vol.Optional(CONF_NOTES): _text_selector(multiline=True),
    }

    if purchase_options is not None:
        fields[
            vol.Required(
                CONF_PURCHASE_UUID,
                default=NO_PURCHASE_SELECTION,
            )
        ] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=purchase_options,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )

    return vol.Schema(fields)


def _runtime_start_schema(
    hass: HomeAssistant,
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
        fields[device_key] = _physical_device_selector(hass, multiple=False)

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


def _entity_platform(
    entity_registry: er.EntityRegistry,
    entity_id: str,
) -> str | None:
    """Return the integration platform that owns an entity, if registered."""
    entry = entity_registry.async_get(entity_id)
    return entry.platform if entry is not None else None


def _state_device_class(
    entity_registry: er.EntityRegistry,
    state: State,
) -> str | None:
    """Return a normalized sensor device class from state or registry."""
    device_class = state.attributes.get("device_class")

    if device_class is None:
        registry_entry = entity_registry.async_get(state.entity_id)
        if registry_entry is not None:
            device_class = getattr(
                registry_entry,
                "original_device_class",
                None,
            )

    if device_class is None:
        return None

    return str(getattr(device_class, "value", device_class))


def _state_power_unit(state: State) -> str | None:
    """Return a normalized power unit from a state, if present."""
    unit = state.attributes.get("unit_of_measurement")
    if unit is None:
        return None
    return str(getattr(unit, "value", unit))


def _is_valid_runtime_source(
    hass: HomeAssistant,
    entity_id: str,
    runtime_mode: str,
) -> bool:
    """Return whether an entity is a suitable runtime source for the mode."""
    state = hass.states.get(entity_id)
    if state is None:
        return False

    entity_registry = er.async_get(hass)
    if _entity_platform(entity_registry, entity_id) == DOMAIN:
        return False

    domain = entity_id.split(".", 1)[0]

    if runtime_mode == RUNTIME_MODE_ON:
        return domain in ON_STATE_SOURCE_DOMAINS

    if runtime_mode == RUNTIME_MODE_POWER:
        return (
            domain == "sensor"
            and _state_device_class(entity_registry, state)
            == SensorDeviceClass.POWER.value
            and _state_power_unit(state) in PowerConverter.VALID_UNITS
        )

    return False


def _runtime_source_candidates(
    hass: HomeAssistant,
    runtime_mode: str,
) -> list[str]:
    """Return runtime source entities suitable for the selected mode."""
    return sorted(
        state.entity_id
        for state in hass.states.async_all()
        if _is_valid_runtime_source(
            hass,
            state.entity_id,
            runtime_mode,
        )
    )


def _runtime_source_schema(
    hass: HomeAssistant,
    runtime_mode: str,
    defaults: dict[str, Any] | None = None,
) -> vol.Schema:
    """Build the second runtime step with only relevant source settings."""
    defaults = dict(defaults or {})
    fields: dict[Any, Any] = {}

    source_key = vol.Required(CONF_SOURCE_ENTITY_ID)
    default_source = str(defaults.get(CONF_SOURCE_ENTITY_ID) or "")
    if default_source and _is_valid_runtime_source(
        hass,
        default_source,
        runtime_mode,
    ):
        source_key = vol.Required(
            CONF_SOURCE_ENTITY_ID,
            default=default_source,
        )

    fields[source_key] = selector.EntitySelector(
        selector.EntitySelectorConfig(
            include_entities=_runtime_source_candidates(
                hass,
                runtime_mode,
            )
        )
    )

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

        fields[
            vol.Required(
                CONF_POWER_HYSTERESIS,
                default=defaults.get(
                    CONF_POWER_HYSTERESIS,
                    DEFAULT_POWER_HYSTERESIS,
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


def _asset_label(asset: AssetData) -> str:
    """Return the stable Asset choice label shown in management flows."""
    return f"{asset['asset_id']} — {asset['name']}"


def _purchase_asset_summary(
    entry: ConfigEntry,
    subentry_id: str,
    language: str,
) -> str:
    """Return canonical Purchase membership as localized read-only text."""
    manager = getattr(entry, "runtime_data", None)
    purchase = (
        manager.purchase_for_subentry(subentry_id)
        if isinstance(manager, AssetStoreManager)
        else None
    )
    assets = []
    if purchase is not None:
        assets = [
            asset
            for asset_uuid in purchase.get("asset_uuids", [])
            if (asset := manager.asset(asset_uuid)) is not None
        ]

    if assets:
        return "\n".join(
            f"- {_asset_label(asset)}"
            for asset in sorted(assets, key=lambda item: item["asset_id"])
        )

    if language.lower().startswith("fi"):
        return "Ei liitettyjä elinkaarilaitteita."
    return "No Device Lifecycle Assets are linked."


def _stored_purchase_label(purchase: PurchaseData) -> str:
    """Return a readable label for one stored Purchase relationship."""
    if purchase.get("name"):
        return str(purchase["name"])

    seller = str(purchase.get("seller") or "").strip()
    purchase_date = str(purchase.get("purchase_date") or "").strip()
    label = f"{seller} {purchase_date}".strip()
    return label or purchase["purchase_uuid"]


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
        used.update(str(device_id) for device_id in subentry.data.get(CONF_DEVICE_IDS, []))

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
        result = purchase_date.replace(
            year=purchase_date.year + years,
            month=2,
            day=28,
        )

    return result.isoformat()


def _prepare_purchase_data(
    user_input: dict[str, Any],
    *,
    preserved_data: dict[str, Any] | None = None,
    default_currency: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate purchase data while preserving stable Asset Core references."""
    data = {
        key: value
        for key, value in user_input.items()
        if value not in (None, "")
    }

    # For a new purchase, an omitted installation date defaults to the
    # purchase date. Reconfiguration never forces this default so users can
    # later change or clear the installation date independently.
    if (
        preserved_data is None
        and data.get(CONF_DEVICE_IDS)
        and CONF_PURCHASE_DATE in data
        and CONF_INSTALLED_DATE not in data
    ):
        purchase_date = dt_util.parse_date(str(data[CONF_PURCHASE_DATE]))
        if purchase_date is None:
            return None, "invalid_purchase_date"
        data[CONF_INSTALLED_DATE] = purchase_date.isoformat()

    if CONF_PURCHASE_PRICE in data:
        try:
            purchase_price = float(data[CONF_PURCHASE_PRICE])
        except (TypeError, ValueError):
            return None, "invalid_purchase_price"
        if not isfinite(purchase_price) or purchase_price < 0:
            return None, "invalid_purchase_price"
        data[CONF_PURCHASE_PRICE] = purchase_price

    warranty_type = str(data.get(CONF_WARRANTY_TYPE, WARRANTY_NONE))
    data[CONF_WARRANTY_TYPE] = warranty_type

    if warranty_type == WARRANTY_NONE:
        data.pop(CONF_WARRANTY_UNTIL, None)
    elif warranty_type in (WARRANTY_ONE_YEAR, WARRANTY_TWO_YEARS):
        purchase_date = data.get(CONF_PURCHASE_DATE)
        if not purchase_date:
            return None, "purchase_date_required_for_warranty"

        years = 1 if warranty_type == WARRANTY_ONE_YEAR else 2
        warranty_until = _add_years(str(purchase_date), years)
        if warranty_until is None:
            return None, "invalid_purchase_date"

        data[CONF_WARRANTY_UNTIL] = warranty_until
    elif warranty_type == WARRANTY_MANUAL:
        if not data.get(CONF_WARRANTY_UNTIL):
            return None, "manual_warranty_date_required"
    else:
        return None, "invalid_warranty_type"

    preserved = dict(preserved_data or {})
    data[CONF_CURRENCY] = str(
        preserved.get(CONF_CURRENCY) or default_currency
    )
    if purchase_uuid := preserved.get(CONF_PURCHASE_UUID):
        data[CONF_PURCHASE_UUID] = str(purchase_uuid)

    return data, None


def _prepare_runtime_data(
    user_input: dict[str, Any],
    *,
    device_id: str | None = None,
    asset_uuid: str | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate and normalize runtime tracking data."""
    data = {
        key: value
        for key, value in user_input.items()
        if value not in (None, "")
    }

    if device_id is not None:
        data[CONF_DEVICE_ID] = device_id
    if asset_uuid:
        data[CONF_ASSET_UUID] = asset_uuid

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
            hysteresis = float(
                data.get(
                    CONF_POWER_HYSTERESIS,
                    DEFAULT_POWER_HYSTERESIS,
                )
            )
        except (TypeError, ValueError):
            return None, "invalid_power_threshold"

        if threshold < 0:
            return None, "invalid_power_threshold"
        if hysteresis < 0:
            return None, "invalid_power_hysteresis"
        if hysteresis > threshold:
            return None, "power_hysteresis_too_large"

        data[CONF_POWER_THRESHOLD] = threshold
        data[CONF_POWER_HYSTERESIS] = hysteresis
    else:
        data.pop(CONF_POWER_THRESHOLD, None)
        data.pop(CONF_POWER_HYSTERESIS, None)

    return data, None


def _runtime_source_error(
    hass: HomeAssistant,
    source_entity_id: str,
    runtime_mode: str,
) -> str | None:
    """Return a localized flow error key for an unsuitable runtime source."""
    if hass.states.get(source_entity_id) is None:
        return "source_missing"

    if _is_valid_runtime_source(hass, source_entity_id, runtime_mode):
        return None

    if runtime_mode == RUNTIME_MODE_POWER:
        return "invalid_power_source"

    if runtime_mode == RUNTIME_MODE_ON:
        return "invalid_on_state_source"

    return "invalid_runtime_mode"


class DeviceLifecycleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create exactly one parent integration entry."""

    VERSION = CONFIG_ENTRY_VERSION

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> DeviceLifecycleOptionsFlow:
        """Return the parent integration Asset management flow."""
        return DeviceLifecycleOptionsFlow()

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


class DeviceLifecycleOptionsFlow(OptionsFlow):
    """Manage physical Assets through the single parent integration."""

    @property
    def _manager(self) -> AssetStoreManager:
        """Return the loaded Asset Store manager for the parent entry."""
        return self.config_entry.runtime_data

    def _localized_label(self, english: str, finnish: str) -> str:
        """Return a minimal localized dynamic selector label."""
        if self.hass.config.language.lower().startswith("fi"):
            return finnish
        return english

    def _asset_choices(self) -> list[selector.SelectOptionDict]:
        """Return every Asset keyed by immutable UUID."""
        return [
            selector.SelectOptionDict(
                value=asset["asset_uuid"],
                label=_asset_label(asset),
            )
            for asset in sorted(
                self._manager.assets(),
                key=lambda item: item["asset_id"],
            )
        ]

    def _purchase_choices(
        self,
        current_purchase_uuid: str | None = None,
    ) -> list[selector.SelectOptionDict]:
        """Return configured Purchases plus the current historical relationship."""
        choices = [
            selector.SelectOptionDict(
                value=NO_PURCHASE_SELECTION,
                label=self._localized_label("No Purchase", "Ei ostosta"),
            )
        ]
        purchases = sorted(
            self._manager.purchases(),
            key=lambda item: (
                _stored_purchase_label(item).casefold(),
                item["purchase_uuid"],
            ),
        )
        for purchase in purchases:
            if (
                not purchase.get("configured")
                and purchase["purchase_uuid"] != current_purchase_uuid
            ):
                continue
            label = _stored_purchase_label(purchase)
            if not purchase.get("configured"):
                label = self._localized_label(
                    f"Historical — {label}",
                    f"Historiallinen — {label}",
                )
            choices.append(
                selector.SelectOptionDict(
                    value=purchase["purchase_uuid"],
                    label=label,
                )
            )
        return choices

    def _metadata_input(self, user_input: dict[str, Any]) -> dict[str, Any]:
        """Return exactly the editable physical metadata fields."""
        return {field: user_input.get(field) for field in ASSET_METADATA_FIELDS}

    def _storage_error_key(self, err: Exception) -> str:
        """Map storage failures to safe flow errors without fabricating data."""
        message = str(err).lower()
        if "asset" in message and "does not exist" in message:
            return "asset_missing"
        if "purchase" in message and (
            "does not exist" in message or "not currently configured" in message
        ):
            return "invalid_purchase"
        if "already linked to another asset" in message:
            return "device_already_linked"
        if "already primary for asset" in message:
            return "related_device_is_primary"
        if "relationship" in message or "conflict" in message:
            if "primary ha relationship changed" in message:
                return "ha_relationship_changed"
            return "purchase_conflict"
        if "deployment state" in message:
            return "invalid_deployment_state"
        if "installed_date" in message:
            return "invalid_installed_date"
        if "ha area" in message:
            return "invalid_area"
        return "asset_store_error"

    def _area_label(self, area_id: str | None) -> str:
        """Describe a current Area without guessing from its name."""
        if area_id is None:
            return self._localized_label("No Area", "Ei aluetta")
        area = ar.async_get(self.hass).async_get_area(area_id)
        if area is None:
            return self._localized_label(
                f"Unavailable Home Assistant Area (stored ID: {area_id})",
                f"Alue ei ole enää käytettävissä (tallennettu tunnus: {area_id})",
            )
        return f"{area.name} ({area.id})"

    def _ha_device_label(self, device_id: str | None) -> str:
        """Describe a relationship without guessing a replacement device."""
        if device_id is None:
            return self._localized_label(
                "No primary Home Assistant device",
                "Ei ensisijaista Home Assistant -laitetta",
            )
        device = dr.async_get(self.hass).async_get(device_id)
        if device is None:
            return self._localized_label(
                f"Unavailable Home Assistant device (stored ID: {device_id})",
                "Home Assistant -laite ei ole enää käytettävissä "
                f"(tallennettu tunnus: {device_id})",
            )
        name = (
            getattr(device, "name_by_user", None)
            or getattr(device, "name", None)
            or getattr(device, "model", None)
            or device_id
        )
        return f"{name} ({device_id})"

    def _related_device_summary(self, asset: AssetData) -> str:
        """Describe every related relationship, including stale references."""
        device_ids = _related_device_ids(asset)
        if not device_ids:
            return self._localized_label(
                "No related Home Assistant devices",
                "Ei liittyviä Home Assistant -laitteita",
            )
        return "; ".join(
            self._ha_device_label(device_id) for device_id in device_ids
        )

    def _related_device_options(
        self,
        asset: AssetData,
    ) -> list[selector.SelectOptionDict]:
        """Return stored related references as removable selector options."""
        return [
            selector.SelectOptionDict(
                value=device_id,
                label=self._ha_device_label(device_id),
            )
            for device_id in _related_device_ids(asset)
        ]

    def _validate_ha_link_target(
        self,
        device_id: str,
    ) -> tuple[dr.DeviceEntry | None, str | None]:
        """Validate an existing operational device without claiming it."""
        registry = dr.async_get(self.hass)
        device = registry.async_get(device_id)
        if device is None:
            return None, "device_missing"
        if _is_service_device(registry, device_id):
            return None, "service_device_not_allowed"

        config_entry_id = device.config_entry_id
        owner_entry = (
            self.hass.config_entries.async_get_entry(config_entry_id)
            if config_entry_id is not None
            else None
        )
        identifier_domains = {
            str(identifier[0]) for identifier in device.identifiers
        }
        if (
            self.config_entry.entry_id == config_entry_id
            or DOMAIN in identifier_domains
            or (owner_entry is not None and owner_entry.domain == DOMAIN)
        ):
            return None, "device_lifecycle_device_not_allowed"
        if owner_entry is None:
            return None, "device_missing"
        if (
            owner_entry.domain in DEVICE_EXCLUDED_INTEGRATIONS
            or identifier_domains.intersection(DEVICE_EXCLUDED_INTEGRATIONS)
        ):
            return None, "non_physical_device_not_allowed"
        return device, None

    def _ha_relationship_dependency_error(
        self,
        device_id: str,
    ) -> str | None:
        """Return the active subentry dependency blocking unlink/replacement."""
        for subentry in self.config_entry.subentries.values():
            if subentry.subentry_type != SUBENTRY_TYPE_PURCHASE:
                continue
            if device_id in {
                str(value) for value in subentry.data.get(CONF_DEVICE_IDS, [])
            }:
                return "ha_device_purchase_dependency"
        for subentry in self.config_entry.subentries.values():
            if (
                subentry.subentry_type == SUBENTRY_TYPE_RUNTIME
                and str(subentry.data.get(CONF_DEVICE_ID) or "") == device_id
            ):
                return "ha_device_runtime_dependency"
        return None

    def _finish_asset_action(
        self,
        asset: AssetData,
        description: str,
    ) -> ConfigFlowResult:
        """Finish without changing parent config-entry options."""
        return self.async_create_entry(
            title="",
            data=dict(self.config_entry.options),
            description=description,
            description_placeholders={
                "asset_id": asset["asset_id"],
                "asset_name": asset["name"],
            },
        )

    def _show_asset_selection(
        self,
        *,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Show the all-Asset selector, including legacy and Runtime Assets."""
        choices = self._asset_choices()
        if not choices and not errors:
            errors = {"base": "no_assets"}
        return self.async_show_form(
            step_id="manage_asset",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ASSET_UUID): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=choices,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=errors or {},
        )

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show the extensible Asset management entry menu."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["create_manual_asset", "manage_asset"],
        )

    async def async_step_create_manual_asset(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Create one ordinary Asset Core record without requiring an HA device."""
        errors: dict[str, str] = {}
        purchase_choices = self._purchase_choices()

        if user_input is not None:
            purchase_uuid = str(
                user_input.get(CONF_PURCHASE_UUID) or NO_PURCHASE_SELECTION
            )
            valid_purchase_uuids = {
                option["value"] for option in purchase_choices
            }
            if purchase_uuid not in valid_purchase_uuids:
                errors["base"] = "invalid_purchase"
            else:
                pending_asset_uuid = getattr(
                    self,
                    "_pending_created_asset_uuid",
                    None,
                )
                try:
                    if pending_asset_uuid is None:
                        asset = await self._manager.async_create_manual_asset(
                            **self._metadata_input(user_input)
                        )
                        self._pending_created_asset_uuid = asset["asset_uuid"]
                    else:
                        asset = await self._manager.async_update_asset_metadata(
                            pending_asset_uuid,
                            **self._metadata_input(user_input),
                        )

                    if purchase_uuid != NO_PURCHASE_SELECTION:
                        asset = await self._manager.async_set_asset_purchase(
                            asset["asset_uuid"],
                            purchase_uuid,
                        )
                except (AssetStoreError, OSError) as err:
                    errors["base"] = self._storage_error_key(err)
                else:
                    return self._finish_asset_action(asset, "asset_created")

        schema = _asset_metadata_schema(purchase_choices)
        suggested = dict(user_input or {})
        suggested.setdefault(CONF_PURCHASE_UUID, NO_PURCHASE_SELECTION)
        return self.async_show_form(
            step_id="create_manual_asset",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    async def async_step_manage_asset(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Select any existing Asset by UUID-backed DLxxxx label."""
        if user_input is not None:
            asset_uuid = str(user_input.get(CONF_ASSET_UUID) or "")
            if self._manager.asset(asset_uuid) is None:
                return self._show_asset_selection(
                    errors={"base": "asset_missing"}
                )
            self._selected_asset_uuid = asset_uuid
            return await self.async_step_manage_asset_menu()

        return self._show_asset_selection()

    async def async_step_manage_asset_menu(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show actions for the selected Asset, ready for later extensions."""
        asset_uuid = getattr(self, "_selected_asset_uuid", None)
        asset = self._manager.asset(asset_uuid)
        if asset is None:
            return self._show_asset_selection(errors={"base": "asset_missing"})
        return self.async_show_menu(
            step_id="manage_asset_menu",
            menu_options=[
                "edit_asset_metadata",
                "change_asset_purchase",
                "asset_deployment",
                "ha_relationship",
            ],
            description_placeholders={"asset": _asset_label(asset)},
        )

    async def async_step_edit_asset_metadata(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Edit physical metadata while preserving all Asset relationships."""
        asset_uuid = getattr(self, "_selected_asset_uuid", None)
        asset = self._manager.asset(asset_uuid)
        if asset is None:
            return self._show_asset_selection(errors={"base": "asset_missing"})

        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                asset = await self._manager.async_update_asset_metadata(
                    asset["asset_uuid"],
                    **self._metadata_input(user_input),
                )
            except (AssetStoreError, OSError) as err:
                errors["base"] = self._storage_error_key(err)
            else:
                return self._finish_asset_action(asset, "asset_updated")

        defaults = {
            field: asset.get(field) or ""
            for field in ASSET_METADATA_FIELDS
        }
        if user_input is not None:
            defaults.update(user_input)
        return self.async_show_form(
            step_id="edit_asset_metadata",
            data_schema=self.add_suggested_values_to_schema(
                _asset_metadata_schema(),
                defaults,
            ),
            errors=errors,
            description_placeholders={"asset": _asset_label(asset)},
        )

    async def async_step_change_asset_purchase(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Assign, preserve, or clear the selected Asset's Purchase."""
        asset_uuid = getattr(self, "_selected_asset_uuid", None)
        asset = self._manager.asset(asset_uuid)
        if asset is None:
            return self._show_asset_selection(errors={"base": "asset_missing"})

        current_purchase_uuid = asset.get("purchase_uuid")
        purchase_choices = self._purchase_choices(current_purchase_uuid)
        current_selection = current_purchase_uuid or NO_PURCHASE_SELECTION
        current_label = next(
            (
                option["label"]
                for option in purchase_choices
                if option["value"] == current_selection
            ),
            current_selection,
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            selected = str(
                user_input.get(CONF_PURCHASE_UUID) or NO_PURCHASE_SELECTION
            )
            valid_purchase_uuids = {
                option["value"] for option in purchase_choices
            }
            if selected not in valid_purchase_uuids:
                errors["base"] = "invalid_purchase"
            else:
                try:
                    asset = await self._manager.async_set_asset_purchase(
                        asset["asset_uuid"],
                        None if selected == NO_PURCHASE_SELECTION else selected,
                    )
                except (AssetStoreError, OSError) as err:
                    errors["base"] = self._storage_error_key(err)
                else:
                    return self._finish_asset_action(
                        asset,
                        "asset_purchase_updated",
                    )

        return self.async_show_form(
            step_id="change_asset_purchase",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PURCHASE_UUID,
                        default=current_selection,
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=purchase_choices,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "asset": _asset_label(asset),
                "current_purchase": current_label,
            },
        )

    def _show_asset_deployment_form(
        self,
        asset: AssetData,
        *,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Show explicit deployment fields without inferring HA relationships."""
        fields: dict[Any, Any] = {
            vol.Required(
                CONF_DEPLOYMENT_STATE,
                default=asset[CONF_DEPLOYMENT_STATE],
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(DEPLOYMENT_STATES),
                    translation_key="deployment_state",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(CONF_INSTALLED_DATE): selector.DateSelector(),
            vol.Required(
                CONF_CLEAR_INSTALLED_DATE,
                default=False,
            ): selector.BooleanSelector(),
            vol.Optional(CONF_HA_AREA_ID): selector.AreaSelector(),
            vol.Required(
                CONF_CLEAR_HA_AREA,
                default=False,
            ): selector.BooleanSelector(),
        }
        suggested: dict[str, Any] = {
            CONF_DEPLOYMENT_STATE: asset[CONF_DEPLOYMENT_STATE],
            CONF_CLEAR_INSTALLED_DATE: False,
            CONF_CLEAR_HA_AREA: False,
        }
        if installed_date := asset.get(CONF_INSTALLED_DATE):
            suggested[CONF_INSTALLED_DATE] = installed_date
        current_area_id = asset.get(CONF_HA_AREA_ID)
        if (
            current_area_id is not None
            and ar.async_get(self.hass).async_get_area(current_area_id) is not None
        ):
            suggested[CONF_HA_AREA_ID] = current_area_id
        if user_input is not None:
            suggested.update(user_input)

        return self.async_show_form(
            step_id="asset_deployment",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(fields),
                suggested,
            ),
            errors=errors or {},
            description_placeholders={
                "asset": _asset_label(asset),
                "current_area": self._area_label(current_area_id),
            },
        )

    async def async_step_asset_deployment(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Edit explicit deployment state, date, and Asset Area metadata."""
        asset_uuid = getattr(self, "_selected_asset_uuid", None)
        asset = self._manager.asset(asset_uuid)
        if asset is None:
            return self._show_asset_selection(errors={"base": "asset_missing"})

        if user_input is None:
            return self._show_asset_deployment_form(asset)

        errors: dict[str, str] = {}
        deployment_state = str(user_input.get(CONF_DEPLOYMENT_STATE) or "")
        updates: dict[str, str | None] = {}
        if deployment_state not in DEPLOYMENT_STATES:
            errors["base"] = "invalid_deployment_state"
        elif deployment_state != asset[CONF_DEPLOYMENT_STATE]:
            updates[CONF_DEPLOYMENT_STATE] = deployment_state

        if user_input.get(CONF_CLEAR_INSTALLED_DATE):
            if asset.get(CONF_INSTALLED_DATE) is not None:
                updates[CONF_INSTALLED_DATE] = None
        elif selected_date := user_input.get(CONF_INSTALLED_DATE):
            try:
                parsed_date = dt_util.parse_date(str(selected_date))
            except (TypeError, ValueError):
                parsed_date = None
            if parsed_date is None:
                errors["base"] = "invalid_installed_date"
            elif parsed_date.isoformat() != asset.get(CONF_INSTALLED_DATE):
                updates[CONF_INSTALLED_DATE] = parsed_date.isoformat()

        current_area_id = asset.get(CONF_HA_AREA_ID)
        if user_input.get(CONF_CLEAR_HA_AREA):
            if current_area_id is not None:
                updates[CONF_HA_AREA_ID] = None
        elif selected_area := user_input.get(CONF_HA_AREA_ID):
            selected_area = str(selected_area)
            if selected_area != current_area_id:
                if ar.async_get(self.hass).async_get_area(selected_area) is None:
                    errors["base"] = "invalid_area"
                else:
                    updates[CONF_HA_AREA_ID] = selected_area

        if (
            deployment_state == DEPLOYMENT_STATE_NOT_DEPLOYED
            and current_area_id is None
            and updates.get(CONF_HA_AREA_ID) is not None
        ):
            errors["base"] = "invalid_area"

        if errors:
            return self._show_asset_deployment_form(
                asset,
                user_input=user_input,
                errors=errors,
            )

        if (
            current_area_id is not None
            and asset[CONF_DEPLOYMENT_STATE] != DEPLOYMENT_STATE_NOT_DEPLOYED
            and deployment_state == DEPLOYMENT_STATE_NOT_DEPLOYED
        ):
            updates[CONF_DEPLOYMENT_STATE] = DEPLOYMENT_STATE_NOT_DEPLOYED
            updates[CONF_HA_AREA_ID] = None
            self._pending_deployment_update = {
                "asset_uuid": asset["asset_uuid"],
                "updates": updates,
                "area_label": self._area_label(current_area_id),
            }
            return await self.async_step_confirm_not_deployed()

        try:
            if updates:
                asset = await self._manager.async_set_asset_deployment(
                    asset["asset_uuid"],
                    **updates,
                )
        except (AssetStoreError, OSError) as err:
            return self._show_asset_deployment_form(
                asset,
                user_input=user_input,
                errors={"base": self._storage_error_key(err)},
            )
        return self._finish_asset_action(asset, "asset_deployment_updated")

    async def async_step_confirm_not_deployed(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Require confirmation before a not-deployed transition clears Area."""
        pending = getattr(self, "_pending_deployment_update", None)
        if pending is None:
            return await self.async_step_asset_deployment()

        asset = self._manager.asset(pending["asset_uuid"])
        if asset is None:
            del self._pending_deployment_update
            return self._show_asset_selection(errors={"base": "asset_missing"})

        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_CONFIRM_AREA_CLEAR):
                del self._pending_deployment_update
                return await self.async_step_asset_deployment()
            try:
                asset = await self._manager.async_set_asset_deployment(
                    asset["asset_uuid"],
                    **pending["updates"],
                )
            except (AssetStoreError, OSError) as err:
                errors["base"] = self._storage_error_key(err)
            else:
                del self._pending_deployment_update
                return self._finish_asset_action(
                    asset,
                    "asset_deployment_updated",
                )

        return self.async_show_form(
            step_id="confirm_not_deployed",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CONFIRM_AREA_CLEAR,
                        default=False,
                    ): selector.BooleanSelector()
                }
            ),
            errors=errors,
            description_placeholders={
                "asset": _asset_label(asset),
                "current_area": pending["area_label"],
            },
        )

    async def async_step_ha_relationship(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show primary and related HA relationships before managing either."""
        asset_uuid = getattr(self, "_selected_asset_uuid", None)
        asset = self._manager.asset(asset_uuid)
        if asset is None:
            return self._show_asset_selection(errors={"base": "asset_missing"})

        menu_options = ["manage_primary_device", "add_related_device"]
        if _related_device_ids(asset):
            menu_options.append("remove_related_device")
        return self.async_show_menu(
            step_id="ha_relationship",
            menu_options=menu_options,
            description_placeholders={
                "asset": _asset_label(asset),
                "current_primary": self._ha_device_label(
                    _primary_device_id(asset)
                ),
                "related_devices": self._related_device_summary(asset),
            },
        )

    def _show_primary_device_form(
        self,
        asset: AssetData,
        *,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
        owner_asset_id: str | None = None,
    ) -> ConfigFlowResult:
        """Show the current primary relationship and safe link actions."""
        current_device_id = _primary_device_id(asset)
        fields: dict[Any, Any] = {}
        if current_device_id is None:
            fields[vol.Required(CONF_DEVICE_ID)] = _physical_device_selector(
                self.hass,
                multiple=False,
            )
        else:
            fields[
                vol.Required(
                    CONF_HA_RELATIONSHIP_ACTION,
                    default=HA_RELATIONSHIP_ACTION_REPLACE,
                )
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(HA_RELATIONSHIP_ACTIONS),
                    translation_key="ha_relationship_action",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            )
            fields[vol.Optional(CONF_DEVICE_ID)] = _physical_device_selector(
                self.hass,
                multiple=False,
            )

        schema = vol.Schema(fields)
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="manage_primary_device",
            data_schema=schema,
            errors=errors or {},
            description_placeholders={
                "asset": _asset_label(asset),
                "current_device": self._ha_device_label(current_device_id),
                "owner_asset_id": owner_asset_id
                or self._localized_label("another Asset", "toinen laite"),
            },
        )

    async def async_step_manage_primary_device(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Link, inspect, unlink, or atomically replace a primary HA reference."""
        asset_uuid = getattr(self, "_selected_asset_uuid", None)
        asset = self._manager.asset(asset_uuid)
        if asset is None:
            return self._show_asset_selection(errors={"base": "asset_missing"})
        if user_input is None:
            return self._show_primary_device_form(asset)

        current_device_id = _primary_device_id(asset)
        action = (
            str(user_input.get(CONF_HA_RELATIONSHIP_ACTION) or "")
            if current_device_id is not None
            else HA_RELATIONSHIP_ACTION_REPLACE
        )
        if action not in HA_RELATIONSHIP_ACTIONS:
            return self._show_primary_device_form(
                asset,
                user_input=user_input,
                errors={"base": "invalid_ha_relationship_action"},
            )

        if action == HA_RELATIONSHIP_ACTION_UNLINK:
            if current_device_id is None:
                return self._show_primary_device_form(
                    asset,
                    user_input=user_input,
                    errors={"base": "device_missing"},
                )
            if dependency_error := self._ha_relationship_dependency_error(
                current_device_id
            ):
                return self._show_primary_device_form(
                    asset,
                    user_input=user_input,
                    errors={"base": dependency_error},
                )
            try:
                asset = await self._manager.async_unlink_asset_device(
                    asset["asset_uuid"],
                    expected_device_id=current_device_id,
                )
            except (AssetStoreError, OSError) as err:
                return self._show_primary_device_form(
                    asset,
                    user_input=user_input,
                    errors={"base": self._storage_error_key(err)},
                )
            return self._finish_asset_action(
                asset,
                "asset_ha_relationship_updated",
            )

        target_device_id = str(user_input.get(CONF_DEVICE_ID) or "")
        if not target_device_id:
            return self._show_primary_device_form(
                asset,
                user_input=user_input,
                errors={"base": "device_missing"},
            )
        if (
            current_device_id is not None
            and target_device_id != current_device_id
            and (
                dependency_error := self._ha_relationship_dependency_error(
                    current_device_id
                )
            )
        ):
            return self._show_primary_device_form(
                asset,
                user_input=user_input,
                errors={"base": dependency_error},
            )

        device, validation_error = self._validate_ha_link_target(
            target_device_id
        )
        if validation_error is not None:
            return self._show_primary_device_form(
                asset,
                user_input=user_input,
                errors={"base": validation_error},
            )

        existing_owner = self._manager.asset_for_primary_device_id(
            target_device_id
        )
        if (
            existing_owner is not None
            and existing_owner["asset_uuid"] != asset["asset_uuid"]
        ):
            return self._show_primary_device_form(
                asset,
                user_input=user_input,
                errors={"base": "device_already_linked"},
                owner_asset_id=existing_owner["asset_id"],
            )

        try:
            asset = await self._manager.async_link_asset_device(
                asset["asset_uuid"],
                target_device_id,
                replace=current_device_id is not None,
                device=device,
                expected_current_device_id=current_device_id,
            )
        except (AssetStoreError, OSError) as err:
            error_key = self._storage_error_key(err)
            owner = self._manager.asset_for_primary_device_id(target_device_id)
            return self._show_primary_device_form(
                asset,
                user_input=user_input,
                errors={"base": error_key},
                owner_asset_id=(owner or {}).get("asset_id"),
            )
        return self._finish_asset_action(
            asset,
            "asset_ha_relationship_updated",
        )

    def _show_add_related_device_form(
        self,
        asset: AssetData,
        *,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Show one DeviceSelector for adding a related relationship."""
        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): _physical_device_selector(
                    self.hass,
                    multiple=False,
                )
            }
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="add_related_device",
            data_schema=schema,
            errors=errors or {},
            description_placeholders={
                "asset": _asset_label(asset),
                "current_primary": self._ha_device_label(
                    _primary_device_id(asset)
                ),
                "related_devices": self._related_device_summary(asset),
            },
        )

    async def async_step_add_related_device(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Add one validated non-exclusive related HA relationship."""
        asset_uuid = getattr(self, "_selected_asset_uuid", None)
        asset = self._manager.asset(asset_uuid)
        if asset is None:
            return self._show_asset_selection(errors={"base": "asset_missing"})
        if user_input is None:
            return self._show_add_related_device_form(asset)

        target_device_id = str(user_input.get(CONF_DEVICE_ID) or "")
        if not target_device_id:
            return self._show_add_related_device_form(
                asset,
                user_input=user_input,
                errors={"base": "device_missing"},
            )

        _device, validation_error = self._validate_ha_link_target(
            target_device_id
        )
        if validation_error is not None:
            return self._show_add_related_device_form(
                asset,
                user_input=user_input,
                errors={"base": validation_error},
            )

        try:
            asset = await self._manager.async_add_related_device(
                asset["asset_uuid"],
                target_device_id,
            )
        except (AssetStoreError, OSError) as err:
            return self._show_add_related_device_form(
                asset,
                user_input=user_input,
                errors={"base": self._storage_error_key(err)},
            )
        return self._finish_asset_action(asset, "asset_related_device_added")

    def _show_remove_related_device_form(
        self,
        asset: AssetData,
        *,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Show stored related references, including stale device IDs."""
        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=self._related_device_options(asset),
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="remove_related_device",
            data_schema=schema,
            errors=errors or {},
            description_placeholders={
                "asset": _asset_label(asset),
                "current_primary": self._ha_device_label(
                    _primary_device_id(asset)
                ),
                "related_devices": self._related_device_summary(asset),
            },
        )

    async def async_step_remove_related_device(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Remove one exact stored related reference without registry lookup."""
        asset_uuid = getattr(self, "_selected_asset_uuid", None)
        asset = self._manager.asset(asset_uuid)
        if asset is None:
            return self._show_asset_selection(errors={"base": "asset_missing"})
        related_device_ids = _related_device_ids(asset)
        if not related_device_ids:
            return await self.async_step_ha_relationship()
        if user_input is None:
            return self._show_remove_related_device_form(asset)

        target_device_id = str(user_input.get(CONF_DEVICE_ID) or "")
        if target_device_id not in related_device_ids:
            return self._show_remove_related_device_form(
                asset,
                user_input=user_input,
                errors={"base": "related_device_not_found"},
            )

        try:
            asset = await self._manager.async_remove_related_device(
                asset["asset_uuid"],
                target_device_id,
            )
        except (AssetStoreError, OSError) as err:
            return self._show_remove_related_device_form(
                asset,
                user_input=user_input,
                errors={"base": self._storage_error_key(err)},
            )
        return self._finish_asset_action(asset, "asset_related_device_removed")


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
            device_ids = [
                str(item)
                for item in user_input.get(CONF_DEVICE_IDS, []) or []
            ]

            if device_ids:
                registry = dr.async_get(self.hass)

                if any(
                    registry.async_get(device_id) is None
                    for device_id in device_ids
                ):
                    errors["base"] = "device_missing"
                elif any(
                    _is_service_device(registry, device_id)
                    for device_id in device_ids
                ):
                    errors["base"] = "service_device_not_allowed"
                elif _used_device_ids(entry).intersection(device_ids):
                    errors["base"] = "already_tracked"

            if not errors:
                clean, error = _prepare_purchase_data(
                    user_input,
                    default_currency=str(self.hass.config.currency),
                )
                if error:
                    errors["base"] = error
                elif clean is not None:
                    return self.async_create_entry(
                        title=_purchase_title(clean),
                        data=clean,
                    )

        return self.async_show_form(
            step_id="user",
            data_schema=_purchase_schema(self.hass, user_input),
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
            device_ids = [
                str(item)
                for item in user_input.get(CONF_DEVICE_IDS, []) or []
            ]

            if device_ids:
                registry = dr.async_get(self.hass)

                if any(
                    registry.async_get(device_id) is None
                    for device_id in device_ids
                ):
                    errors["base"] = "device_missing"
                elif any(
                    _is_service_device(registry, device_id)
                    for device_id in device_ids
                ):
                    errors["base"] = "service_device_not_allowed"
                elif _used_device_ids(
                    entry,
                    exclude_subentry_id=subentry.subentry_id,
                ).intersection(device_ids):
                    errors["base"] = "already_tracked"

            if not errors:
                clean, error = _prepare_purchase_data(
                    user_input,
                    preserved_data=dict(subentry.data),
                    default_currency=str(self.hass.config.currency),
                )
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
            data_schema=_purchase_schema(self.hass, defaults),
            errors=errors,
            description_placeholders={
                "linked_assets": _purchase_asset_summary(
                    entry,
                    subentry.subentry_id,
                    self.hass.config.language,
                )
            },
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
            elif _is_service_device(registry, device_id):
                errors["base"] = "service_device_not_allowed"
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
                self.hass,
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

        device_id = str(context[CONF_DEVICE_ID])
        runtime_mode = str(context[CONF_RUNTIME_MODE])
        errors: dict[str, str] = {}

        if user_input is not None:
            source_entity_id = str(
                user_input.get(CONF_SOURCE_ENTITY_ID) or ""
            )

            if error := _runtime_source_error(
                self.hass,
                source_entity_id,
                runtime_mode,
            ):
                errors["base"] = error
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
                self.hass,
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
                self.hass,
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

            if error := _runtime_source_error(
                self.hass,
                source_entity_id,
                runtime_mode,
            ):
                errors["base"] = error
            else:
                combined = {
                    CONF_RUNTIME_MODE: runtime_mode,
                    **user_input,
                }
                clean, error = _prepare_runtime_data(
                    combined,
                    device_id=device_id,
                    asset_uuid=str(
                        subentry.data.get(CONF_ASSET_UUID) or ""
                    )
                    or None,
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
                self.hass,
                runtime_mode,
                defaults,
            ),
            errors=errors,
        )
