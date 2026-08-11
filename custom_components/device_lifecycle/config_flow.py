"""Config, purchase and runtime subentry flows for Device Lifecycle."""

from __future__ import annotations

from datetime import date
from math import isfinite
from typing import Any
from uuid import uuid4

import voluptuous as vol
from homeassistant import config_entries, data_entry_flow
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlowResult,
    ConfigSubentryFlow,
    FlowType,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers import translation as translation_helper
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import PowerConverter

from .const import (
    CONF_ASSET_NAME,
    CONF_ASSET_UUID,
    CONF_CATEGORY,
    CONF_CLEAR_HA_AREA,
    CONF_CLEAR_INSTALLED_DATE,
    CONF_CONFIRM_DISPOSED,
    CONF_CONFIRM_AREA_CLEAR,
    CONF_CONFIRM_QUICK_ADD,
    CONF_CONFIRM_VOID,
    CONF_CURRENCY,
    CONF_DEPLOYMENT_STATE,
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
    CONF_EFFECTIVE_DATE,
    CONF_HA_AREA_ID,
    CONF_HA_RELATIONSHIP_ACTION,
    CONF_HW_VERSION,
    CONF_INSTALLED_DATE,
    CONF_LIFECYCLE_STATUS,
    CONF_MANUFACTURER,
    CONF_MODEL,
    CONF_MODEL_ID,
    CONF_NOTES,
    CONF_PREDECESSOR_ASSET_UUID,
    CONF_POWER_HYSTERESIS,
    CONF_POWER_THRESHOLD,
    CONF_PURCHASE_DATE,
    CONF_PURCHASE_NAME,
    CONF_PURCHASE_PRICE,
    CONF_PURCHASE_UUID,
    CONF_RECEIPT_REFERENCE,
    CONF_RECEIPT_URL,
    CONF_REPLACEMENT_ACTION,
    CONF_REPLACEMENT_REASON,
    CONF_REPLACEMENT_TARGET_ASSET_UUID,
    CONF_REPLACEMENT_UUID,
    CONF_RETIRE_PREDECESSOR,
    CONF_RUNTIME_DATA_VERSION,
    CONF_RUNTIME_MODE,
    CONF_SELLER,
    CONF_SERIAL_NUMBER,
    CONF_SOURCE_ENTITY_ID,
    CONF_SW_VERSION,
    CONF_SUCCESSOR_ASSET_UUID,
    CONF_UNDEPLOY_PREDECESSOR,
    CONF_VOID_REASON,
    CONF_WARRANTY_TYPE,
    CONF_WARRANTY_UNTIL,
    CONFIG_ENTRY_VERSION,
    DEFAULT_POWER_HYSTERESIS,
    DEFAULT_POWER_THRESHOLD,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_UNKNOWN,
    DEPLOYMENT_STATES,
    DOMAIN,
    HA_RELATIONSHIP_ACTION_REPLACE,
    HA_RELATIONSHIP_ACTION_UNLINK,
    HA_RELATIONSHIP_ACTIONS,
    LIFECYCLE_STATUSES,
    LIFECYCLE_STATUS_ACTIVE,
    LIFECYCLE_STATUS_DISPOSED,
    LIFECYCLE_STATUS_RETIRED,
    LIFECYCLE_STATUS_UNKNOWN,
    REPLACEMENT_ACTION_CORRECT,
    REPLACEMENT_ACTION_VOID,
    REPLACEMENT_ACTIONS,
    REPLACEMENT_REASONS,
    RUNTIME_DATA_VERSION,
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
from .models import AssetData, PurchaseData, ReplacementRecordData
from .storage import (
    FIELD_SOURCE_HOME_ASSISTANT,
    FIELD_SOURCE_USER,
    AssetStoreError,
    AssetStoreManager,
    QuickAssetCreateRequest,
    add_calendar_years,
    home_assistant_asset_metadata,
)

MAIN_UNIQUE_ID = "device_lifecycle_main"
NO_PURCHASE_SELECTION = "__no_purchase__"
NO_REPLACEMENT_SELECTION = "__no_replacement__"

QUICK_SECTION_IDENTITY = "identity"
QUICK_SECTION_DETAILS = "details"
QUICK_SECTION_LIFECYCLE = "lifecycle"
QUICK_SECTION_WARRANTY = "warranty"
QUICK_SECTION_RELATIONSHIPS = "relationships"

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
    return add_calendar_years(value, years)


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

        if not isfinite(threshold) or threshold < 0:
            return None, "invalid_power_threshold"
        if not isfinite(hysteresis) or hysteresis < 0:
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
            title="Device Lifecycle",
            data={},
        )

    async def async_on_create_entry(
        self,
        result: ConfigFlowResult,
    ) -> ConfigFlowResult:
        """Continue a new loaded parent entry directly into Quick Add."""
        options_result = await self.hass.config_entries.options.async_init(
            result["result"].entry_id
        )
        quick_add_result = await self.hass.config_entries.options.async_configure(
            options_result["flow_id"],
            {"next_step_id": "quick_add"},
        )
        result["next_flow"] = (
            FlowType.OPTIONS_FLOW,
            quick_add_result["flow_id"],
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

    def _replacement_target_choices(
        self,
        excluded_asset_uuid: str,
    ) -> list[selector.SelectOptionDict]:
        """Return UUID-backed physical Asset targets other than the current Asset."""
        return [
            option
            for option in self._asset_choices()
            if option["value"] != excluded_asset_uuid
        ]

    def _quick_replacement_choices(self) -> list[selector.SelectOptionDict]:
        """Return name-first predecessor choices with safe duplicate labels."""
        assets = self._manager.assets()
        name_counts: dict[str, int] = {}
        for asset in assets:
            name_counts[asset["name"]] = name_counts.get(asset["name"], 0) + 1
        choices = [
            selector.SelectOptionDict(
                value=NO_REPLACEMENT_SELECTION,
                label=self._localized_label("No replacement", "Ei korvaamista"),
            )
        ]
        for asset in sorted(
            assets,
            key=lambda item: (item["name"].casefold(), item["asset_id"]),
        ):
            label = asset["name"]
            if name_counts[label] > 1:
                label = f"{label} · {asset['asset_id']}"
            choices.append(
                selector.SelectOptionDict(
                    value=asset["asset_uuid"],
                    label=label,
                )
            )
        return choices

    def _replacement_record_label(self, record: ReplacementRecordData) -> str:
        """Return a stable Asset-ID label for one replacement record."""
        predecessor = self._manager.asset(record["predecessor_asset_uuid"])
        successor = self._manager.asset(record["successor_asset_uuid"])
        predecessor_label = (
            predecessor["asset_id"] if predecessor is not None else "?"
        )
        successor_label = successor["asset_id"] if successor is not None else "?"
        return f"{predecessor_label} → {successor_label}"

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
        if isinstance(err, AssetStoreError):
            structured_codes = {
                "asset_missing",
                "device_already_linked",
                "device_lifecycle_device_not_allowed",
                "device_missing",
                "invalid_area",
                "invalid_deployment_state",
                "invalid_installed_date",
                "invalid_lifecycle_effective_date",
                "invalid_lifecycle_status",
                "invalid_purchase",
                "invalid_quick_create_request",
                "invalid_replacement_effective_date",
                "invalid_replacement_reason",
                "invalid_warranty_date",
                "invalid_warranty_type",
                "lifecycle_chain_invalid",
                "lifecycle_date_in_future",
                "persistence_error",
                "predecessor_changed",
                "purchase_changed",
                "purchase_date_required_for_warranty",
                "purchase_missing",
                "purchase_not_configured",
                "quick_create_idempotency_conflict",
                "replacement_cycle",
                "replacement_date_in_future",
                "replacement_graph_invalid",
                "replacement_missing",
                "replacement_predecessor_conflict",
                "replacement_self_reference",
                "replacement_successor_conflict",
                "replacement_void_reason_required",
                "manual_warranty_date_required",
                "non_physical_device_not_allowed",
                "service_device_not_allowed",
            }
            if err.code in structured_codes:
                return err.code
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
        """Finish and reload exposure without changing config-entry options."""
        result = self.async_create_entry(
            title="",
            data=dict(self.config_entry.options),
            description=description,
            description_placeholders={
                "asset_id": asset["asset_id"],
                "asset_name": asset["name"],
            },
        )
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        return result

    def _finish_quick_add(self, asset: AssetData) -> ConfigFlowResult:
        """Finish Quick Add with one Store-owned reload and unchanged options."""
        result = self.async_create_entry(
            title="",
            data=dict(self.config_entry.options),
            description="quick_asset_created",
            description_placeholders={"asset_name": asset["name"]},
        )
        self.hass.config_entries.async_schedule_reload(self.config_entry.entry_id)
        return result

    def _ensure_quick_asset_uuid(self) -> None:
        """Allocate one flow-local idempotency UUID, never a DL identity."""
        if not hasattr(self, "_quick_asset_uuid"):
            self._quick_asset_uuid = str(uuid4())

    @staticmethod
    def _flatten_quick_sections(user_input: dict[str, Any]) -> dict[str, Any]:
        """Flatten one level of Data Entry Flow sections for Asset Core."""
        flattened = {
            key: value
            for key, value in user_input.items()
            if key
            not in {
                QUICK_SECTION_IDENTITY,
                QUICK_SECTION_DETAILS,
                QUICK_SECTION_LIFECYCLE,
                QUICK_SECTION_WARRANTY,
                QUICK_SECTION_RELATIONSHIPS,
            }
        }
        for section_key in (
            QUICK_SECTION_IDENTITY,
            QUICK_SECTION_DETAILS,
            QUICK_SECTION_LIFECYCLE,
            QUICK_SECTION_WARRANTY,
            QUICK_SECTION_RELATIONSHIPS,
        ):
            section_values = user_input.get(section_key)
            if isinstance(section_values, dict):
                flattened.update(section_values)
        return flattened

    @staticmethod
    def _nest_quick_details(values: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Return flat reviewed values in the section shape expected by HA."""
        groups = {
            QUICK_SECTION_IDENTITY: (
                CONF_ASSET_NAME,
                CONF_CATEGORY,
                CONF_MANUFACTURER,
                CONF_MODEL,
            ),
            QUICK_SECTION_DETAILS: (
                CONF_MODEL_ID,
                CONF_SERIAL_NUMBER,
                CONF_SW_VERSION,
                CONF_HW_VERSION,
                CONF_NOTES,
            ),
            QUICK_SECTION_LIFECYCLE: (
                CONF_LIFECYCLE_STATUS,
                CONF_EFFECTIVE_DATE,
                CONF_DEPLOYMENT_STATE,
                CONF_INSTALLED_DATE,
                CONF_HA_AREA_ID,
            ),
            QUICK_SECTION_WARRANTY: (
                CONF_WARRANTY_TYPE,
                CONF_WARRANTY_UNTIL,
            ),
            QUICK_SECTION_RELATIONSHIPS: (
                CONF_PURCHASE_UUID,
                CONF_REPLACEMENT_TARGET_ASSET_UUID,
            ),
        }
        return {
            section_key: {
                key: values[key]
                for key in keys
                if key in values and values[key] is not None
            }
            for section_key, keys in groups.items()
        }

    def _quick_metadata_and_sources(
        self,
        values: dict[str, Any],
    ) -> tuple[dict[str, str | None], dict[str, str]]:
        """Normalize editable metadata and preserve exact field provenance."""
        metadata: dict[str, str | None] = {}
        sources: dict[str, str] = {}
        original = getattr(self, "_quick_original_prefill", {})
        for field in ASSET_METADATA_FIELDS:
            submitted = values.get(field)
            if submitted in (None, ""):
                normalized = None
            elif not isinstance(submitted, str):
                raise AssetStoreError(
                    "Quick Add metadata must be text",
                    code="invalid_quick_create_request",
                )
            elif field == CONF_ASSET_NAME:
                normalized = submitted.strip()
            else:
                normalized = submitted
            if field == CONF_ASSET_NAME and not normalized:
                raise AssetStoreError(
                    "Quick Add name is required",
                    code="invalid_quick_create_request",
                )
            metadata[field] = normalized

            original_value = original.get(field)
            if original_value is not None:
                sources[field] = (
                    FIELD_SOURCE_HOME_ASSISTANT
                    if normalized == original_value
                    else FIELD_SOURCE_USER
                )
            elif normalized is not None:
                sources[field] = FIELD_SOURCE_USER
        return metadata, sources

    @staticmethod
    def _quick_canonical_date(
        value: Any,
        *,
        invalid_code: str,
        reject_future: bool = False,
    ) -> str | None:
        """Normalize one optional canonical date for local flow validation."""
        if value in (None, ""):
            return None
        if not isinstance(value, str):
            raise AssetStoreError("Date must use YYYY-MM-DD", code=invalid_code)
        try:
            parsed = date.fromisoformat(value)
        except ValueError as err:
            raise AssetStoreError(
                "Date must use YYYY-MM-DD",
                code=invalid_code,
            ) from err
        if parsed.isoformat() != value:
            raise AssetStoreError("Date must use YYYY-MM-DD", code=invalid_code)
        if reject_future and parsed > dt_util.now().date():
            future_code = (
                "replacement_date_in_future"
                if invalid_code == "invalid_replacement_effective_date"
                else "lifecycle_date_in_future"
            )
            raise AssetStoreError("Date cannot be in the future", code=future_code)
        return value

    def _quick_details_schema(self) -> vol.Schema:
        """Build the single-level sectioned Quick Add details form."""
        original = getattr(self, "_quick_original_prefill", {})
        name_marker: Any = vol.Required(CONF_ASSET_NAME)
        if original.get(CONF_ASSET_NAME):
            name_marker = vol.Required(
                CONF_ASSET_NAME,
                default=original[CONF_ASSET_NAME],
            )
        source = getattr(self, "_quick_source", "manual")
        deployment_default = (
            DEPLOYMENT_STATE_DEPLOYED
            if source == "home_assistant"
            else DEPLOYMENT_STATE_NOT_DEPLOYED
        )
        return vol.Schema(
            {
                vol.Required(QUICK_SECTION_IDENTITY): data_entry_flow.section(
                    vol.Schema(
                        {
                            name_marker: _text_selector(),
                            vol.Optional(CONF_CATEGORY): _text_selector(),
                            vol.Optional(CONF_MANUFACTURER): _text_selector(),
                            vol.Optional(CONF_MODEL): _text_selector(),
                        }
                    ),
                    {"collapsed": False},
                ),
                vol.Required(QUICK_SECTION_DETAILS): data_entry_flow.section(
                    vol.Schema(
                        {
                            vol.Optional(CONF_MODEL_ID): _text_selector(),
                            vol.Optional(CONF_SERIAL_NUMBER): _text_selector(),
                            vol.Optional(CONF_SW_VERSION): _text_selector(),
                            vol.Optional(CONF_HW_VERSION): _text_selector(),
                            vol.Optional(CONF_NOTES): _text_selector(multiline=True),
                        }
                    ),
                    {"collapsed": False},
                ),
                vol.Required(QUICK_SECTION_LIFECYCLE): data_entry_flow.section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_LIFECYCLE_STATUS,
                                default=LIFECYCLE_STATUS_ACTIVE,
                            ): selector.SelectSelector(
                                selector.SelectSelectorConfig(
                                    options=list(LIFECYCLE_STATUSES),
                                    translation_key="lifecycle_status",
                                    mode=selector.SelectSelectorMode.DROPDOWN,
                                )
                            ),
                            vol.Optional(CONF_EFFECTIVE_DATE): selector.DateSelector(),
                            vol.Required(
                                CONF_DEPLOYMENT_STATE,
                                default=deployment_default,
                            ): selector.SelectSelector(
                                selector.SelectSelectorConfig(
                                    options=list(DEPLOYMENT_STATES),
                                    translation_key="deployment_state",
                                    mode=selector.SelectSelectorMode.DROPDOWN,
                                )
                            ),
                            vol.Optional(CONF_INSTALLED_DATE): selector.DateSelector(),
                            vol.Optional(CONF_HA_AREA_ID): selector.AreaSelector(),
                        }
                    ),
                    {"collapsed": False},
                ),
                vol.Required(QUICK_SECTION_WARRANTY): data_entry_flow.section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_WARRANTY_TYPE,
                                default=WARRANTY_NONE,
                            ): selector.SelectSelector(
                                selector.SelectSelectorConfig(
                                    options=list(WARRANTY_TYPES),
                                    translation_key="warranty_type",
                                    mode=selector.SelectSelectorMode.DROPDOWN,
                                )
                            ),
                            vol.Optional(CONF_WARRANTY_UNTIL): selector.DateSelector(),
                        }
                    ),
                    {"collapsed": False},
                ),
                vol.Required(QUICK_SECTION_RELATIONSHIPS): data_entry_flow.section(
                    vol.Schema(
                        {
                            vol.Required(
                                CONF_PURCHASE_UUID,
                                default=NO_PURCHASE_SELECTION,
                            ): selector.SelectSelector(
                                selector.SelectSelectorConfig(
                                    options=self._purchase_choices(),
                                    mode=selector.SelectSelectorMode.DROPDOWN,
                                )
                            ),
                            vol.Required(
                                CONF_REPLACEMENT_TARGET_ASSET_UUID,
                                default=NO_REPLACEMENT_SELECTION,
                            ): selector.SelectSelector(
                                selector.SelectSelectorConfig(
                                    options=self._quick_replacement_choices(),
                                    mode=selector.SelectSelectorMode.DROPDOWN,
                                )
                            ),
                        }
                    ),
                    {"collapsed": False},
                ),
            }
        )

    def _show_quick_details(
        self,
        *,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Show Quick Add details with preserved prefill and retry values."""
        suggested = dict(getattr(self, "_quick_original_prefill", {}))
        suggested.update(getattr(self, "_quick_details_input", {}))
        return self.async_show_form(
            step_id="quick_add_details",
            data_schema=self.add_suggested_values_to_schema(
                self._quick_details_schema(),
                self._nest_quick_details(suggested),
            ),
            errors=errors or {},
        )

    def _quick_predecessor_snapshot(self, asset: AssetData) -> dict[str, Any]:
        """Capture every predecessor field required for review TOCTOU checks."""
        return {
            "asset_uuid": asset["asset_uuid"],
            "name": asset["name"],
            "lifecycle_status": asset["lifecycle"]["status"],
            "current_event_uuid": asset["lifecycle"]["current_event_uuid"],
            "deployment_state": asset[CONF_DEPLOYMENT_STATE],
            "ha_area_id": asset[CONF_HA_AREA_ID],
        }

    def _show_quick_replacement(
        self,
        *,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Show optional predecessor mutations before final review."""
        predecessor = self._quick_predecessor
        defaults = dict(getattr(self, "_quick_replacement_input", {}))
        defaults.setdefault(CONF_REPLACEMENT_REASON, "unknown")
        defaults.setdefault(
            CONF_RETIRE_PREDECESSOR,
            predecessor["lifecycle_status"]
            in (LIFECYCLE_STATUS_ACTIVE, LIFECYCLE_STATUS_UNKNOWN),
        )
        defaults.setdefault(
            CONF_UNDEPLOY_PREDECESSOR,
            predecessor["deployment_state"]
            in (DEPLOYMENT_STATE_DEPLOYED, DEPLOYMENT_STATE_UNKNOWN),
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_REPLACEMENT_REASON,
                    default="unknown",
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(REPLACEMENT_REASONS),
                        translation_key="replacement_reason",
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_EFFECTIVE_DATE): selector.DateSelector(),
                vol.Optional(CONF_NOTES): _text_selector(multiline=True),
                vol.Required(
                    CONF_RETIRE_PREDECESSOR,
                    default=defaults[CONF_RETIRE_PREDECESSOR],
                ): selector.BooleanSelector(),
                vol.Required(
                    CONF_UNDEPLOY_PREDECESSOR,
                    default=defaults[CONF_UNDEPLOY_PREDECESSOR],
                ): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="quick_add_replacement",
            data_schema=self.add_suggested_values_to_schema(schema, defaults),
            errors=errors or {},
            description_placeholders={"predecessor_name": predecessor["name"]},
        )

    def _quick_display_value(self, value: str | None) -> str:
        """Return a human-readable empty value for review placeholders."""
        if value is not None:
            return value
        return self._localized_label("Not specified", "Ei määritetty")

    def _quick_purchase_label(self) -> str:
        """Return the selected Purchase's display label without its UUID."""
        purchase_uuid = self._quick_details["purchase_uuid"]
        if purchase_uuid is None:
            return self._localized_label("No Purchase", "Ei ostosta")
        purchase = self._manager.purchase(purchase_uuid)
        if purchase is None:
            return self._localized_label("Unavailable Purchase", "Osto ei saatavilla")
        return _stored_purchase_label(purchase)

    def _quick_area_name(self, area_id: str | None) -> str:
        """Return an Area name without exposing the registry identifier."""
        if area_id is None:
            return self._localized_label("No Area", "Ei aluetta")
        area = ar.async_get(self.hass).async_get_area(area_id)
        if area is None:
            return self._localized_label("Unavailable Area", "Alue ei saatavilla")
        return area.name

    def _quick_device_name(self, device_id: str | None) -> str:
        """Return an HA device name without exposing its registry identifier."""
        if device_id is None:
            return self._localized_label("No HA device", "Ei HA-laitetta")
        device = dr.async_get(self.hass).async_get(device_id)
        if device is None:
            return self._localized_label("Unavailable HA device", "HA-laite ei saatavilla")
        name = (
            getattr(device, "name_by_user", None)
            or getattr(device, "name", None)
            or getattr(device, "model", None)
        )
        if name not in (None, ""):
            return str(name)
        return self._localized_label("Unnamed HA device", "Nimetön HA-laite")

    async def _quick_selector_label(self, selector_key: str, value: str) -> str:
        """Resolve one canonical selector value through HA translations."""
        translations = await translation_helper.async_get_translations(
            self.hass,
            self.hass.config.language,
            "selector",
            integrations={DOMAIN},
        )
        return translations.get(
            f"component.{DOMAIN}.selector.{selector_key}.options.{value}",
            value,
        )

    async def _show_quick_confirm(
        self,
        *,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Show a read-only human review followed by explicit confirmation."""
        details = self._quick_details
        metadata = details["metadata"]
        predecessor = getattr(self, "_quick_predecessor", None)
        replacement = getattr(self, "_quick_replacement", None)
        predecessor_lifecycle = self._localized_label("Unchanged", "Ei muutosta")
        predecessor_deployment = predecessor_lifecycle
        predecessor_area = predecessor_lifecycle
        predecessor_name = self._localized_label("None", "Ei mitään")
        replacement_reason = self._localized_label("None", "Ei mitään")
        replacement_date = self._quick_display_value(None)
        lifecycle_label = await self._quick_selector_label(
            "lifecycle_status",
            details["lifecycle_status"],
        )
        deployment_label = await self._quick_selector_label(
            "deployment_state",
            details["deployment_state"],
        )
        if predecessor is not None and replacement is not None:
            predecessor_name = predecessor["name"]
            replacement_reason = await self._quick_selector_label(
                "replacement_reason",
                replacement[CONF_REPLACEMENT_REASON],
            )
            replacement_date = self._quick_display_value(
                replacement[CONF_EFFECTIVE_DATE]
            )
            if replacement[CONF_RETIRE_PREDECESSOR] and predecessor[
                "lifecycle_status"
            ] in (LIFECYCLE_STATUS_ACTIVE, LIFECYCLE_STATUS_UNKNOWN):
                old_lifecycle = await self._quick_selector_label(
                    "lifecycle_status",
                    predecessor["lifecycle_status"],
                )
                retired = await self._quick_selector_label(
                    "lifecycle_status",
                    LIFECYCLE_STATUS_RETIRED,
                )
                predecessor_lifecycle = (
                    f"{old_lifecycle} → {retired}"
                )
            if replacement[CONF_UNDEPLOY_PREDECESSOR] and predecessor[
                "deployment_state"
            ] in (DEPLOYMENT_STATE_DEPLOYED, DEPLOYMENT_STATE_UNKNOWN):
                old_deployment = await self._quick_selector_label(
                    "deployment_state",
                    predecessor["deployment_state"],
                )
                not_deployed = await self._quick_selector_label(
                    "deployment_state",
                    DEPLOYMENT_STATE_NOT_DEPLOYED,
                )
                predecessor_deployment = (
                    f"{old_deployment} → {not_deployed}"
                )
                if predecessor["ha_area_id"] is not None:
                    predecessor_area = (
                        f"{self._quick_area_name(predecessor['ha_area_id'])} → "
                        f"{self._localized_label('removed', 'poistetaan')}"
                    )
        warranty = await self._quick_selector_label(
            "warranty_type",
            details["warranty_type"],
        )
        if details["warranty_until"] is not None:
            warranty = f"{warranty} — {details['warranty_until']}"
        return self.async_show_form(
            step_id="quick_add_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CONFIRM_QUICK_ADD,
                        default=False,
                    ): selector.BooleanSelector()
                }
            ),
            errors=errors or {},
            description_placeholders={
                "asset_name": metadata[CONF_ASSET_NAME],
                "manufacturer_model": " ".join(
                    value
                    for value in (
                        metadata[CONF_MANUFACTURER],
                        metadata[CONF_MODEL],
                    )
                    if value
                )
                or self._quick_display_value(None),
                "lifecycle": lifecycle_label,
                "lifecycle_date": self._quick_display_value(
                    details["lifecycle_effective_date"]
                ),
                "deployment": deployment_label,
                "installed_date": self._quick_display_value(
                    details["installed_date"]
                ),
                "area": self._quick_area_name(details["ha_area_id"]),
                "purchase": self._quick_purchase_label(),
                "warranty": warranty,
                "ha_device": self._quick_device_name(
                    getattr(self, "_quick_primary_device_id", None)
                ),
                "predecessor_name": predecessor_name,
                "predecessor_lifecycle": predecessor_lifecycle,
                "predecessor_deployment": predecessor_deployment,
                "predecessor_area": predecessor_area,
                "replacement_reason": replacement_reason,
                "replacement_date": replacement_date,
                "disposed_warning": (
                    self._localized_label(
                        "This Asset will be created as disposed.",
                        "Laite luodaan hävitetyksi.",
                    )
                    if details["lifecycle_status"] == LIFECYCLE_STATUS_DISPOSED
                    else ""
                ),
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
            menu_options=["quick_add", "manage_asset"],
        )

    async def async_step_quick_add(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose whether Quick Add starts from HA or manual metadata."""
        return self.async_show_menu(
            step_id="quick_add",
            menu_options=["quick_add_from_ha", "quick_add_manual"],
        )

    async def async_step_quick_add_manual(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Start manual Quick Add directly at the details form."""
        self._ensure_quick_asset_uuid()
        self._quick_source = "manual"
        self._quick_primary_device_id = None
        self._quick_original_prefill = {}
        return self._show_quick_details()

    async def async_step_quick_add_from_ha(
        self,
        user_input: dict[str, Any] | None = None,
        *,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Select and conservatively validate one physical HA device."""
        flow_errors = dict(errors or {})
        if user_input is not None and not flow_errors:
            device_id = str(user_input.get(CONF_DEVICE_ID) or "")
            device, validation_error = self._validate_ha_link_target(device_id)
            if validation_error is not None:
                flow_errors["base"] = validation_error
            elif self._manager.asset_for_primary_device_id(device_id) is not None:
                flow_errors["base"] = "device_already_linked"
            else:
                self._ensure_quick_asset_uuid()
                self._quick_source = "home_assistant"
                self._quick_primary_device_id = device_id
                self._quick_original_prefill = home_assistant_asset_metadata(
                    device,
                    device_id,
                )
                return self._show_quick_details()

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
            step_id="quick_add_from_ha",
            data_schema=schema,
            errors=flow_errors,
        )

    async def async_step_quick_add_details(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Review all new-Asset domains without mutating canonical Store."""
        if user_input is None:
            return self._show_quick_details()

        values = self._flatten_quick_sections(user_input)
        self._quick_details_input = dict(values)
        try:
            metadata, field_sources = self._quick_metadata_and_sources(values)
            lifecycle_status = str(
                values.get(CONF_LIFECYCLE_STATUS, LIFECYCLE_STATUS_ACTIVE)
            )
            if lifecycle_status not in LIFECYCLE_STATUSES:
                raise AssetStoreError(
                    "Invalid Lifecycle status",
                    code="invalid_lifecycle_status",
                )
            lifecycle_date = self._quick_canonical_date(
                values.get(CONF_EFFECTIVE_DATE),
                invalid_code="invalid_lifecycle_effective_date",
                reject_future=True,
            )
            deployment_state = str(
                values.get(
                    CONF_DEPLOYMENT_STATE,
                    DEPLOYMENT_STATE_DEPLOYED
                    if self._quick_source == "home_assistant"
                    else DEPLOYMENT_STATE_NOT_DEPLOYED,
                )
            )
            if deployment_state not in DEPLOYMENT_STATES:
                raise AssetStoreError(
                    "Invalid Deployment state",
                    code="invalid_deployment_state",
                )
            installed_date = self._quick_canonical_date(
                values.get(CONF_INSTALLED_DATE),
                invalid_code="invalid_installed_date",
            )
            area_id = values.get(CONF_HA_AREA_ID)
            if area_id in (None, ""):
                area_id = None
            elif not isinstance(area_id, str) or (
                ar.async_get(self.hass).async_get_area(area_id) is None
            ):
                raise AssetStoreError("Invalid Area", code="invalid_area")
            if (
                deployment_state == DEPLOYMENT_STATE_NOT_DEPLOYED
                and area_id is not None
            ):
                raise AssetStoreError(
                    "A not-deployed Asset cannot have an Area",
                    code="invalid_area",
                )

            purchase_selection = str(
                values.get(CONF_PURCHASE_UUID) or NO_PURCHASE_SELECTION
            )
            purchase_uuid = (
                None
                if purchase_selection == NO_PURCHASE_SELECTION
                else purchase_selection
            )
            purchase = (
                None
                if purchase_uuid is None
                else self._manager.purchase(purchase_uuid)
            )
            if purchase_uuid is not None and purchase is None:
                raise AssetStoreError(
                    "Selected Purchase no longer exists",
                    code="purchase_missing",
                )
            if purchase is not None and not purchase.get("configured"):
                raise AssetStoreError(
                    "Selected Purchase is not configured",
                    code="purchase_not_configured",
                )

            warranty_type = str(values.get(CONF_WARRANTY_TYPE, WARRANTY_NONE))
            if warranty_type not in WARRANTY_TYPES:
                raise AssetStoreError(
                    "Invalid warranty type",
                    code="invalid_warranty_type",
                )
            warranty_until: str | None = None
            expected_purchase_date: str | None = None
            if warranty_type == WARRANTY_MANUAL:
                warranty_until = self._quick_canonical_date(
                    values.get(CONF_WARRANTY_UNTIL),
                    invalid_code="invalid_warranty_date",
                )
                if warranty_until is None:
                    raise AssetStoreError(
                        "A manual warranty date is required",
                        code="manual_warranty_date_required",
                    )
            elif warranty_type in (WARRANTY_ONE_YEAR, WARRANTY_TWO_YEARS):
                if purchase is None:
                    raise AssetStoreError(
                        "A Purchase is required for calculated warranty",
                        code="purchase_date_required_for_warranty",
                    )
                purchase_date = purchase.get(CONF_PURCHASE_DATE)
                if not isinstance(purchase_date, str):
                    raise AssetStoreError(
                        "The Purchase has no usable date",
                        code="purchase_date_required_for_warranty",
                    )
                years = 1 if warranty_type == WARRANTY_ONE_YEAR else 2
                warranty_until = add_calendar_years(purchase_date, years)
                if warranty_until is None:
                    raise AssetStoreError(
                        "The Purchase date is invalid",
                        code="purchase_date_required_for_warranty",
                    )
                expected_purchase_date = purchase_date
                self._quick_details_input[CONF_WARRANTY_UNTIL] = warranty_until

            replacement_selection = str(
                values.get(CONF_REPLACEMENT_TARGET_ASSET_UUID)
                or NO_REPLACEMENT_SELECTION
            )
            predecessor = (
                None
                if replacement_selection == NO_REPLACEMENT_SELECTION
                else self._manager.asset(replacement_selection)
            )
            if (
                replacement_selection != NO_REPLACEMENT_SELECTION
                and predecessor is None
            ):
                raise AssetStoreError(
                    "Selected predecessor no longer exists",
                    code="asset_missing",
                )
            if (
                predecessor is not None
                and self._quick_source == "manual"
                and deployment_state == DEPLOYMENT_STATE_NOT_DEPLOYED
                and not getattr(
                    self,
                    "_quick_replacement_deployment_reviewed",
                    False,
                )
            ):
                # The predecessor is chosen on the same form as Deployment, so
                # render the replacement-aware default visibly before review.
                # A second submission may explicitly choose any valid state.
                self._quick_replacement_deployment_reviewed = True
                self._quick_details_input[CONF_DEPLOYMENT_STATE] = (
                    DEPLOYMENT_STATE_DEPLOYED
                )
                return self._show_quick_details(
                    errors={"base": "review_replacement_deployment"}
                )
        except AssetStoreError as err:
            return self._show_quick_details(
                errors={"base": self._storage_error_key(err)}
            )

        self._quick_details = {
            "metadata": metadata,
            "field_sources": field_sources,
            "lifecycle_status": lifecycle_status,
            "lifecycle_effective_date": lifecycle_date,
            "deployment_state": deployment_state,
            "installed_date": installed_date,
            "ha_area_id": area_id,
            "warranty_type": warranty_type,
            "warranty_until": warranty_until,
            "purchase_uuid": purchase_uuid,
            "expected_purchase_date": expected_purchase_date,
        }
        if predecessor is None:
            self._quick_predecessor = None
            self._quick_replacement = None
            return await self._show_quick_confirm()
        self._quick_predecessor = self._quick_predecessor_snapshot(predecessor)
        return self._show_quick_replacement()

    async def async_step_quick_add_replacement(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Review replacement history and optional predecessor state changes."""
        if user_input is None:
            return self._show_quick_replacement()
        self._quick_replacement_input = dict(user_input)
        try:
            reason = str(user_input.get(CONF_REPLACEMENT_REASON) or "")
            if reason not in REPLACEMENT_REASONS:
                raise AssetStoreError(
                    "Invalid replacement reason",
                    code="invalid_replacement_reason",
                )
            effective_date = self._quick_canonical_date(
                user_input.get(CONF_EFFECTIVE_DATE),
                invalid_code="invalid_replacement_effective_date",
                reject_future=True,
            )
            notes_value = user_input.get(CONF_NOTES)
            if notes_value in (None, ""):
                notes = None
            elif not isinstance(notes_value, str):
                raise AssetStoreError(
                    "Replacement notes must be text",
                    code="invalid_quick_create_request",
                )
            else:
                notes = notes_value
        except AssetStoreError as err:
            return self._show_quick_replacement(
                errors={"base": self._storage_error_key(err)}
            )
        self._quick_replacement = {
            CONF_REPLACEMENT_REASON: reason,
            CONF_EFFECTIVE_DATE: effective_date,
            CONF_NOTES: notes,
            CONF_RETIRE_PREDECESSOR: bool(
                user_input.get(CONF_RETIRE_PREDECESSOR, False)
            ),
            CONF_UNDEPLOY_PREDECESSOR: bool(
                user_input.get(CONF_UNDEPLOY_PREDECESSOR, False)
            ),
        }
        return await self._show_quick_confirm()

    def _quick_create_request(self) -> QuickAssetCreateRequest:
        """Build one immutable canonical command from reviewed flow state."""
        details = self._quick_details
        predecessor = getattr(self, "_quick_predecessor", None)
        replacement = getattr(self, "_quick_replacement", None)
        return QuickAssetCreateRequest(
            asset_uuid=self._quick_asset_uuid,
            primary_device_id=self._quick_primary_device_id,
            metadata=details["metadata"],
            field_sources=details["field_sources"],
            initial_lifecycle_status=details["lifecycle_status"],
            initial_lifecycle_effective_date=details["lifecycle_effective_date"],
            deployment_state=details["deployment_state"],
            installed_date=details["installed_date"],
            ha_area_id=details["ha_area_id"],
            warranty_type=details["warranty_type"],
            warranty_until=details["warranty_until"],
            purchase_uuid=details["purchase_uuid"],
            expected_purchase_date=details["expected_purchase_date"],
            predecessor_asset_uuid=(
                None if predecessor is None else predecessor["asset_uuid"]
            ),
            expected_predecessor_lifecycle_status=(
                None if predecessor is None else predecessor["lifecycle_status"]
            ),
            expected_predecessor_current_event_uuid=(
                None if predecessor is None else predecessor["current_event_uuid"]
            ),
            expected_predecessor_deployment_state=(
                None if predecessor is None else predecessor["deployment_state"]
            ),
            expected_predecessor_ha_area_id=(
                None if predecessor is None else predecessor["ha_area_id"]
            ),
            replacement_reason=(
                None if replacement is None else replacement[CONF_REPLACEMENT_REASON]
            ),
            replacement_effective_date=(
                None if replacement is None else replacement[CONF_EFFECTIVE_DATE]
            ),
            replacement_notes=(
                None if replacement is None else replacement[CONF_NOTES]
            ),
            retire_predecessor=(
                False
                if replacement is None
                else replacement[CONF_RETIRE_PREDECESSOR]
            ),
            undeploy_predecessor=(
                False
                if replacement is None
                else replacement[CONF_UNDEPLOY_PREDECESSOR]
            ),
        )

    async def async_step_quick_add_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Commit one reviewed Quick Add command exactly once."""
        if user_input is None:
            return await self._show_quick_confirm()
        if not user_input.get(CONF_CONFIRM_QUICK_ADD):
            return await self._show_quick_confirm(
                errors={"base": "confirmation_required"}
            )

        device_id = self._quick_primary_device_id
        if device_id is not None:
            _device, validation_error = self._validate_ha_link_target(device_id)
            if validation_error is not None:
                return await self.async_step_quick_add_from_ha(
                    {CONF_DEVICE_ID: device_id},
                    errors={"base": validation_error},
                )
            if self._manager.asset_for_primary_device_id(device_id) is not None:
                return await self.async_step_quick_add_from_ha(
                    {CONF_DEVICE_ID: device_id},
                    errors={"base": "device_already_linked"},
                )
        area_id = self._quick_details["ha_area_id"]
        if area_id is not None and (
            ar.async_get(self.hass).async_get_area(area_id) is None
        ):
            return self._show_quick_details(errors={"base": "invalid_area"})

        try:
            result = await self._manager.async_quick_create_asset(
                self._quick_create_request()
            )
        except (AssetStoreError, OSError) as err:
            error_key = self._storage_error_key(err)
            if error_key in {
                "device_missing",
                "device_already_linked",
                "service_device_not_allowed",
                "non_physical_device_not_allowed",
                "device_lifecycle_device_not_allowed",
            } and device_id is not None:
                return await self.async_step_quick_add_from_ha(
                    {CONF_DEVICE_ID: device_id},
                    errors={"base": error_key},
                )
            if error_key in {
                "asset_missing",
                "predecessor_changed",
                "invalid_replacement_reason",
                "invalid_replacement_effective_date",
                "replacement_date_in_future",
                "replacement_predecessor_conflict",
                "replacement_successor_conflict",
                "replacement_cycle",
                "replacement_graph_invalid",
            } and getattr(self, "_quick_predecessor", None) is not None:
                predecessor = self._manager.asset(
                    self._quick_predecessor["asset_uuid"]
                )
                if predecessor is not None:
                    self._quick_predecessor = self._quick_predecessor_snapshot(
                        predecessor
                    )
                return self._show_quick_replacement(errors={"base": error_key})
            if error_key in {
                "invalid_quick_create_request",
                "invalid_purchase",
                "purchase_missing",
                "purchase_not_configured",
                "purchase_changed",
                "invalid_lifecycle_status",
                "invalid_lifecycle_effective_date",
                "lifecycle_date_in_future",
                "invalid_deployment_state",
                "invalid_installed_date",
                "invalid_area",
                "invalid_warranty_type",
                "invalid_warranty_date",
                "purchase_date_required_for_warranty",
                "manual_warranty_date_required",
            }:
                if error_key == "purchase_changed":
                    purchase_uuid = self._quick_details["purchase_uuid"]
                    purchase = self._manager.purchase(purchase_uuid)
                    if purchase is not None:
                        purchase_date = purchase.get(CONF_PURCHASE_DATE)
                        years = (
                            1
                            if self._quick_details["warranty_type"]
                            == WARRANTY_ONE_YEAR
                            else 2
                        )
                        recalculated = (
                            add_calendar_years(purchase_date, years)
                            if isinstance(purchase_date, str)
                            else None
                        )
                        self._quick_details_input[CONF_WARRANTY_UNTIL] = recalculated
                return self._show_quick_details(errors={"base": error_key})
            return await self._show_quick_confirm(errors={"base": error_key})
        return self._finish_quick_add(result.asset)

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
                "asset_lifecycle",
                "asset_replacement",
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

    def _show_asset_lifecycle_form(
        self,
        asset: AssetData,
        *,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Show translated canonical lifecycle choices for one Asset."""
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_LIFECYCLE_STATUS,
                    default=asset["lifecycle"]["status"],
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(LIFECYCLE_STATUSES),
                        translation_key="lifecycle_status",
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_EFFECTIVE_DATE): selector.DateSelector(),
                vol.Optional(CONF_NOTES): _text_selector(multiline=True),
            }
        )
        suggested: dict[str, Any] = {
            CONF_LIFECYCLE_STATUS: asset["lifecycle"]["status"],
            CONF_EFFECTIVE_DATE: dt_util.now().date().isoformat(),
        }
        if user_input is not None:
            suggested.update(user_input)
        return self.async_show_form(
            step_id="asset_lifecycle",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors or {},
            description_placeholders={"asset": _asset_label(asset)},
        )

    async def async_step_asset_lifecycle(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Append an explicit lifecycle transition for the selected Asset."""
        asset_uuid = getattr(self, "_selected_asset_uuid", None)
        asset = self._manager.asset(asset_uuid)
        if asset is None:
            return self._show_asset_selection(errors={"base": "asset_missing"})
        if user_input is None:
            return self._show_asset_lifecycle_form(asset)

        status = str(user_input.get(CONF_LIFECYCLE_STATUS) or "")
        if status not in LIFECYCLE_STATUSES:
            return self._show_asset_lifecycle_form(
                asset,
                user_input=user_input,
                errors={"base": "invalid_lifecycle_status"},
            )
        if status == asset["lifecycle"]["status"]:
            return self._show_asset_lifecycle_form(
                asset,
                user_input=user_input,
                errors={"base": "lifecycle_no_change"},
            )
        if status == LIFECYCLE_STATUS_DISPOSED:
            self._pending_lifecycle_update = {
                "asset_uuid": asset["asset_uuid"],
                "status": status,
                "effective_date": user_input.get(CONF_EFFECTIVE_DATE),
                "notes": user_input.get(CONF_NOTES),
            }
            return await self.async_step_confirm_disposed()

        try:
            asset = await self._manager.async_set_asset_lifecycle(
                asset["asset_uuid"],
                status,
                effective_date=user_input.get(CONF_EFFECTIVE_DATE),
                notes=user_input.get(CONF_NOTES),
            )
        except (AssetStoreError, OSError) as err:
            return self._show_asset_lifecycle_form(
                asset,
                user_input=user_input,
                errors={"base": self._storage_error_key(err)},
            )
        return self._finish_asset_action(asset, "asset_lifecycle_updated")

    async def async_step_confirm_disposed(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Require a separate confirmation before recording disposed."""
        pending = getattr(self, "_pending_lifecycle_update", None)
        if pending is None:
            return await self.async_step_asset_lifecycle()
        asset = self._manager.asset(pending["asset_uuid"])
        if asset is None:
            del self._pending_lifecycle_update
            return self._show_asset_selection(errors={"base": "asset_missing"})

        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_CONFIRM_DISPOSED):
                errors["base"] = "confirmation_required"
            else:
                try:
                    asset = await self._manager.async_set_asset_lifecycle(
                        asset["asset_uuid"],
                        pending["status"],
                        effective_date=pending["effective_date"],
                        notes=pending["notes"],
                    )
                except (AssetStoreError, OSError) as err:
                    errors["base"] = self._storage_error_key(err)
                else:
                    del self._pending_lifecycle_update
                    return self._finish_asset_action(
                        asset,
                        "asset_lifecycle_updated",
                    )
        return self.async_show_form(
            step_id="confirm_disposed",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CONFIRM_DISPOSED,
                        default=False,
                    ): selector.BooleanSelector()
                }
            ),
            errors=errors,
            description_placeholders={"asset": _asset_label(asset)},
        )

    async def async_step_asset_replacement(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Show physical replacement operations for the selected Asset."""
        asset_uuid = getattr(self, "_selected_asset_uuid", None)
        asset = self._manager.asset(asset_uuid)
        if asset is None:
            return self._show_asset_selection(errors={"base": "asset_missing"})
        menu_options = ["replacement_replaces", "replacement_replaced_by"]
        if self._manager.replacement_records_for_asset(asset["asset_uuid"]):
            menu_options.append("manage_asset_replacement")
        return self.async_show_menu(
            step_id="asset_replacement",
            menu_options=menu_options,
            description_placeholders={"asset": _asset_label(asset)},
        )

    def _show_replacement_create_form(
        self,
        asset: AssetData,
        *,
        step_id: str,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Show one UUID-backed replacement creation form."""
        schema = vol.Schema(
            {
                vol.Required(CONF_REPLACEMENT_TARGET_ASSET_UUID): (
                    selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=self._replacement_target_choices(
                                asset["asset_uuid"]
                            ),
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                ),
                vol.Required(CONF_REPLACEMENT_REASON, default="unknown"): (
                    selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=list(REPLACEMENT_REASONS),
                            translation_key="replacement_reason",
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                ),
                vol.Optional(CONF_EFFECTIVE_DATE): selector.DateSelector(),
                vol.Optional(CONF_NOTES): _text_selector(multiline=True),
            }
        )
        suggested: dict[str, Any] = {
            CONF_REPLACEMENT_REASON: "unknown",
            CONF_EFFECTIVE_DATE: dt_util.now().date().isoformat(),
        }
        if user_input is not None:
            suggested.update(user_input)
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors or {},
            description_placeholders={"asset": _asset_label(asset)},
        )

    async def _async_replacement_create(
        self,
        *,
        selected_is_successor: bool,
        step_id: str,
        user_input: dict[str, Any] | None,
    ) -> ConfigFlowResult:
        """Create one direction-specific relationship with Store authority."""
        asset_uuid = getattr(self, "_selected_asset_uuid", None)
        asset = self._manager.asset(asset_uuid)
        if asset is None:
            return self._show_asset_selection(errors={"base": "asset_missing"})
        if user_input is None:
            return self._show_replacement_create_form(asset, step_id=step_id)
        target_uuid = str(
            user_input.get(CONF_REPLACEMENT_TARGET_ASSET_UUID) or ""
        )
        if self._manager.asset(target_uuid) is None:
            return self._show_replacement_create_form(
                asset,
                step_id=step_id,
                user_input=user_input,
                errors={"base": "asset_missing"},
            )
        predecessor_uuid = target_uuid if selected_is_successor else asset["asset_uuid"]
        successor_uuid = asset["asset_uuid"] if selected_is_successor else target_uuid
        try:
            await self._manager.async_create_asset_replacement(
                predecessor_uuid,
                successor_uuid,
                reason=str(user_input.get(CONF_REPLACEMENT_REASON) or ""),
                effective_date=user_input.get(CONF_EFFECTIVE_DATE),
                notes=user_input.get(CONF_NOTES),
            )
        except (AssetStoreError, OSError) as err:
            return self._show_replacement_create_form(
                asset,
                step_id=step_id,
                user_input=user_input,
                errors={"base": self._storage_error_key(err)},
            )
        refreshed = self._manager.asset(asset["asset_uuid"])
        if refreshed is None:
            return self._show_asset_selection(errors={"base": "asset_missing"})
        return self._finish_asset_action(refreshed, "asset_replacement_updated")

    async def async_step_replacement_replaces(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Record that this Asset replaces a predecessor Asset."""
        return await self._async_replacement_create(
            selected_is_successor=True,
            step_id="replacement_replaces",
            user_input=user_input,
        )

    async def async_step_replacement_replaced_by(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Record that this Asset was replaced by a successor Asset."""
        return await self._async_replacement_create(
            selected_is_successor=False,
            step_id="replacement_replaced_by",
            user_input=user_input,
        )

    def _show_manage_replacement_form(
        self,
        asset: AssetData,
        *,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Show active records only for correction or explicit voiding."""
        options = [
            selector.SelectOptionDict(
                value=record["replacement_uuid"],
                label=self._replacement_record_label(record),
            )
            for record in self._manager.replacement_records_for_asset(
                asset["asset_uuid"]
            )
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_REPLACEMENT_UUID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(
                    CONF_REPLACEMENT_ACTION,
                    default=REPLACEMENT_ACTION_CORRECT,
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(REPLACEMENT_ACTIONS),
                        translation_key="replacement_action",
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="manage_asset_replacement",
            data_schema=schema,
            errors=errors or {},
            description_placeholders={"asset": _asset_label(asset)},
        )

    async def async_step_manage_asset_replacement(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Choose one current relationship for correction or voiding."""
        asset_uuid = getattr(self, "_selected_asset_uuid", None)
        asset = self._manager.asset(asset_uuid)
        if asset is None:
            return self._show_asset_selection(errors={"base": "asset_missing"})
        active_records = self._manager.replacement_records_for_asset(
            asset["asset_uuid"]
        )
        if not active_records:
            return await self.async_step_asset_replacement()
        if user_input is None:
            return self._show_manage_replacement_form(asset)
        replacement_uuid = str(user_input.get(CONF_REPLACEMENT_UUID) or "")
        active_ids = {record["replacement_uuid"] for record in active_records}
        if replacement_uuid not in active_ids:
            return self._show_manage_replacement_form(
                asset,
                user_input=user_input,
                errors={"base": "replacement_missing"},
            )
        action = str(user_input.get(CONF_REPLACEMENT_ACTION) or "")
        if action not in REPLACEMENT_ACTIONS:
            return self._show_manage_replacement_form(
                asset,
                user_input=user_input,
                errors={"base": "replacement_missing"},
            )
        self._pending_replacement_uuid = replacement_uuid
        if action == REPLACEMENT_ACTION_VOID:
            return await self.async_step_confirm_void_replacement()
        return await self.async_step_correct_asset_replacement()

    def _show_correct_replacement_form(
        self,
        asset: AssetData,
        record: ReplacementRecordData,
        *,
        user_input: dict[str, Any] | None = None,
        errors: dict[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Show both physical endpoints for one atomic correction."""
        options = self._asset_choices()
        schema = vol.Schema(
            {
                vol.Required(CONF_PREDECESSOR_ASSET_UUID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_SUCCESSOR_ASSET_UUID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=options,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_REPLACEMENT_REASON): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(REPLACEMENT_REASONS),
                        translation_key="replacement_reason",
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_EFFECTIVE_DATE): selector.DateSelector(),
                vol.Optional(CONF_NOTES): _text_selector(multiline=True),
                vol.Required(CONF_VOID_REASON): _text_selector(),
            }
        )
        suggested: dict[str, Any] = {
            CONF_PREDECESSOR_ASSET_UUID: record["predecessor_asset_uuid"],
            CONF_SUCCESSOR_ASSET_UUID: record["successor_asset_uuid"],
            CONF_REPLACEMENT_REASON: record["reason"],
            CONF_NOTES: record["notes"] or "",
        }
        if record["effective_date"] is not None:
            suggested[CONF_EFFECTIVE_DATE] = record["effective_date"]
        if user_input is not None:
            suggested.update(user_input)
        return self.async_show_form(
            step_id="correct_asset_replacement",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors or {},
            description_placeholders={
                "asset": _asset_label(asset),
                "relationship": self._replacement_record_label(record),
            },
        )

    async def async_step_correct_asset_replacement(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Invoke one atomic void-and-create correction mutation."""
        asset_uuid = getattr(self, "_selected_asset_uuid", None)
        asset = self._manager.asset(asset_uuid)
        replacement_uuid = getattr(self, "_pending_replacement_uuid", None)
        record = self._manager.replacement_record(replacement_uuid)
        if asset is None:
            return self._show_asset_selection(errors={"base": "asset_missing"})
        if record is None or record["voided_at"] is not None:
            return self._show_manage_replacement_form(
                asset,
                errors={"base": "replacement_missing"},
            )
        if user_input is None:
            return self._show_correct_replacement_form(asset, record)
        try:
            await self._manager.async_correct_asset_replacement(
                record["replacement_uuid"],
                predecessor_asset_uuid=str(
                    user_input.get(CONF_PREDECESSOR_ASSET_UUID) or ""
                ),
                successor_asset_uuid=str(
                    user_input.get(CONF_SUCCESSOR_ASSET_UUID) or ""
                ),
                reason=str(user_input.get(CONF_REPLACEMENT_REASON) or ""),
                effective_date=user_input.get(CONF_EFFECTIVE_DATE),
                notes=user_input.get(CONF_NOTES),
                void_reason=str(user_input.get(CONF_VOID_REASON) or ""),
            )
        except (AssetStoreError, OSError) as err:
            return self._show_correct_replacement_form(
                asset,
                record,
                user_input=user_input,
                errors={"base": self._storage_error_key(err)},
            )
        refreshed = self._manager.asset(asset["asset_uuid"])
        if refreshed is None:
            return self._show_asset_selection(errors={"base": "asset_missing"})
        return self._finish_asset_action(refreshed, "asset_replacement_updated")

    async def async_step_confirm_void_replacement(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Require confirmation and a non-empty reason before voiding."""
        asset_uuid = getattr(self, "_selected_asset_uuid", None)
        asset = self._manager.asset(asset_uuid)
        replacement_uuid = getattr(self, "_pending_replacement_uuid", None)
        record = self._manager.replacement_record(replacement_uuid)
        if asset is None:
            return self._show_asset_selection(errors={"base": "asset_missing"})
        if record is None or record["voided_at"] is not None:
            return self._show_manage_replacement_form(
                asset,
                errors={"base": "replacement_missing"},
            )

        errors: dict[str, str] = {}
        if user_input is not None:
            void_reason = str(user_input.get(CONF_VOID_REASON) or "")
            if not user_input.get(CONF_CONFIRM_VOID):
                errors["base"] = "confirmation_required"
            elif not void_reason.strip():
                errors["base"] = "replacement_void_reason_required"
            else:
                try:
                    await self._manager.async_void_asset_replacement(
                        record["replacement_uuid"],
                        void_reason=void_reason,
                    )
                except (AssetStoreError, OSError) as err:
                    errors["base"] = self._storage_error_key(err)
                else:
                    refreshed = self._manager.asset(asset["asset_uuid"])
                    if refreshed is None:
                        return self._show_asset_selection(
                            errors={"base": "asset_missing"}
                        )
                    return self._finish_asset_action(
                        refreshed,
                        "asset_replacement_updated",
                    )
        schema = vol.Schema(
            {
                vol.Required(CONF_VOID_REASON): _text_selector(),
                vol.Required(
                    CONF_CONFIRM_VOID,
                    default=False,
                ): selector.BooleanSelector(),
            }
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="confirm_void_replacement",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "asset": _asset_label(asset),
                "relationship": self._replacement_record_label(record),
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
                    clean[CONF_RUNTIME_DATA_VERSION] = RUNTIME_DATA_VERSION
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
                    if CONF_RUNTIME_DATA_VERSION in subentry.data:
                        clean[CONF_RUNTIME_DATA_VERSION] = subentry.data[
                            CONF_RUNTIME_DATA_VERSION
                        ]
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
