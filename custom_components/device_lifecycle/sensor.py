"""Lifecycle and runtime sensors for Device Lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorExtraStoredData,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    EVENT_HOMEASSISTANT_STOP,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state as rs
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import PowerConverter

from .const import (
    CONF_ASSET_UUID,
    CONF_DEVICE_ID,
    CONF_POWER_HYSTERESIS,
    CONF_POWER_THRESHOLD,
    CONF_RUNTIME_DATA_VERSION,
    CONF_RUNTIME_MODE,
    CONF_SOURCE_ENTITY_ID,
    DEFAULT_POWER_HYSTERESIS,
    DOMAIN,
    RUNTIME_DATA_VERSION,
    RUNTIME_MODE_ON,
    RUNTIME_MODE_POWER,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
    WARRANTY_MANUAL,
    WARRANTY_NONE,
    WARRANTY_ONE_YEAR,
    WARRANTY_TWO_YEARS,
)
from .exposure import (
    asset_device_entry,
    asset_id_unique_id,
    deployment_unique_id,
    relationships_unique_id,
)
from .migration import lifecycle_unique_id, runtime_unique_id
from .models import AssetData, PurchaseData
from .storage import AssetStoreError, AssetStoreManager

RUNTIME_REFRESH_INTERVAL = timedelta(minutes=5)
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PendingRuntimeDelta:
    """One sealed Runtime commit that can be retried idempotently."""

    expected_total: Decimal
    delta: Decimal


def _parse_date(value: str | None) -> date | None:
    """Parse a Home Assistant date-selector value."""
    if not value:
        return None
    return dt_util.parse_date(value)


def _is_finnish(language: str) -> bool:
    """Return whether the Home Assistant system language is Finnish."""
    return language.lower().startswith("fi")


def _warranty_type_label(value: str, language: str) -> str:
    """Return a human-readable warranty type for attributes."""
    if _is_finnish(language):
        labels = {
            WARRANTY_NONE: "Ei määritetty",
            WARRANTY_ONE_YEAR: "1 vuosi",
            WARRANTY_TWO_YEARS: "2 vuotta",
            WARRANTY_MANUAL: "Manuaalinen",
        }
    else:
        labels = {
            WARRANTY_NONE: "Not specified",
            WARRANTY_ONE_YEAR: "1 year",
            WARRANTY_TWO_YEARS: "2 years",
            WARRANTY_MANUAL: "Manual",
        }
    return labels.get(value, value)


def _format_warranty_date(value: date, language: str) -> str:
    """Format a warranty date for the configured Home Assistant language."""
    if _is_finnish(language):
        return f"{value.day}.{value.month}.{value.year}"

    month = (
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    )[value.month - 1]
    return f"{value.day} {month} {value.year}"


def _power_value_watts(state: State) -> float | None:
    """Return a numeric power state normalized to watts."""
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None

    if not isfinite(value):
        return None

    unit = state.attributes.get("unit_of_measurement")
    if unit is None:
        return None
    unit = str(getattr(unit, "value", unit))
    if unit not in PowerConverter.VALID_UNITS:
        return None

    return PowerConverter.convert(value, unit, UnitOfPower.WATT)


def _legacy_restore_runtime_seconds(
    hass: HomeAssistant,
    entity_id: str,
) -> Decimal:
    """Read exact legacy native sensor hours and convert them to seconds."""
    stored = rs.async_get(hass).last_states.get(entity_id)
    if stored is None or stored.extra_data is None:
        raise AssetStoreError("legacy native restore data is missing")

    sensor_data = SensorExtraStoredData.from_dict(stored.extra_data.as_dict())
    if sensor_data is None or sensor_data.native_value is None:
        raise AssetStoreError("legacy native Runtime value is missing")

    unit = sensor_data.native_unit_of_measurement
    if unit is None or str(getattr(unit, "value", unit)) != UnitOfTime.HOURS:
        raise AssetStoreError("legacy Runtime unit is missing or is not hours")

    native_value = sensor_data.native_value
    if isinstance(native_value, bool) or not isinstance(
        native_value,
        (Decimal, int, str),
    ):
        raise AssetStoreError("legacy native Runtime value is not safely decimal")
    try:
        hours = Decimal(native_value)
    except (InvalidOperation, TypeError, ValueError) as err:
        raise AssetStoreError("legacy native Runtime value is malformed") from err
    if not hours.is_finite() or hours < 0:
        raise AssetStoreError("legacy native Runtime value is negative or non-finite")
    return hours * Decimal(3600)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Asset exposure and Runtime from normalized Asset Core data."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    manager: AssetStoreManager = entry.runtime_data
    runtime_asset_uuids: set[str] = set()

    assets = sorted(manager.assets(), key=lambda item: item["asset_uuid"])
    asset_devices: dict[str, dr.DeviceEntry] = {}
    for asset in assets:
        device = asset_device_entry(
            device_registry,
            config_entry_id=entry.entry_id,
            asset_uuid=asset["asset_uuid"],
        )
        if device is None:
            raise AssetStoreError(
                f"Asset Device is missing for {asset['asset_uuid']} after "
                "exposure registry migration"
            )
        asset_devices[asset["asset_uuid"]] = device

    valid_subentry_ids = {
        subentry.subentry_id
        for subentry in entry.subentries.values()
        if subentry.subentry_type
        in (SUBENTRY_TYPE_PURCHASE, SUBENTRY_TYPE_RUNTIME)
    }

    # If an entire subentry was deleted, remove only entities that belonged to
    # that deleted Device Lifecycle subentry. Asset Core data itself is retained.
    for registry_entry in er.async_entries_for_config_entry(
        entity_registry,
        entry.entry_id,
    ):
        if (
            registry_entry.platform == DOMAIN
            and registry_entry.config_subentry_id is not None
            and registry_entry.config_subentry_id not in valid_subentry_ids
        ):
            entity_registry.async_remove(registry_entry.entity_id)

    # Lifecycle and the new static exposure entities are parent/Asset-owned.
    # Purchase subentries must no longer retain any Device Lifecycle entities.
    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_TYPE_PURCHASE:
            _remove_unexpected_subentry_entities(
                entity_registry=entity_registry,
                entry=entry,
                subentry_id=subentry.subentry_id,
                expected_unique_ids=set(),
            )

    parent_entities: list[SensorEntity] = []
    relationship_entities: list[DeviceRelationshipsSensor] = []
    for asset in assets:
        purchase = manager.purchase(asset.get("purchase_uuid"))
        purchase_title: str | None = None
        if purchase is not None:
            purchase_subentry_id = purchase.get("config_subentry_id")
            purchase_subentry = (
                entry.subentries.get(purchase_subentry_id)
                if purchase_subentry_id is not None
                else None
            )
            purchase_title = (
                purchase_subentry.title
                if purchase_subentry is not None
                else str(purchase.get("name") or purchase["purchase_uuid"])
            )
        device_entry = asset_devices[asset["asset_uuid"]]
        parent_entities.extend(
            (
                DeviceLifecycleSensor(
                    asset=asset,
                    purchase=purchase,
                    device_entry=device_entry,
                    unique_id=lifecycle_unique_id(asset["asset_uuid"]),
                    purchase_title=purchase_title,
                ),
                DeviceDeploymentSensor(
                    asset=asset,
                    device_entry=device_entry,
                ),
                DeviceAssetIdSensor(
                    asset=asset,
                    device_entry=device_entry,
                ),
            )
        )
        relationships = DeviceRelationshipsSensor(
            asset=asset,
            device_entry=device_entry,
            device_registry=device_registry,
        )
        parent_entities.append(relationships)
        relationship_entities.append(relationships)

    if parent_entities:
        async_add_entities(parent_entities)

    if relationship_entities:
        coordinator = _RelationshipsRegistryCoordinator(
            hass,
            relationship_entities,
        )
        unsubscribe = coordinator.async_start()
        if hasattr(entry, "async_on_unload"):
            entry.async_on_unload(unsubscribe)

    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_TYPE_RUNTIME:
            device_id = str(subentry.data.get(CONF_DEVICE_ID) or "")
            source_entity_id = str(
                subentry.data.get(CONF_SOURCE_ENTITY_ID) or ""
            )

            asset = manager.asset(str(subentry.data.get(CONF_ASSET_UUID) or ""))
            primary_asset = (
                manager.asset_for_primary_device_id(device_id)
                if device_id
                else None
            )
            if (
                asset is None
                or primary_asset is None
                or asset["asset_uuid"] != primary_asset["asset_uuid"]
            ):
                asset = primary_asset

            expected_unique_ids: set[str] = set()
            runtime_entities: list[DeviceRuntimeHoursSensor] = []

            if asset is not None:
                unique_id = runtime_unique_id(asset["asset_uuid"])
                expected_unique_ids.add(unique_id)
                asset_uuid = asset["asset_uuid"]

                if source_entity_id and asset_uuid in runtime_asset_uuids:
                    _LOGGER.error(
                        "Refusing a second active Runtime writer for Asset %s",
                        asset_uuid,
                    )
                elif source_entity_id:
                    runtime_asset_uuids.add(asset_uuid)
                    canonical_total = manager.runtime_total_seconds(asset_uuid)

                    if canonical_total is None:
                        marker = subentry.data.get(CONF_RUNTIME_DATA_VERSION)
                        try:
                            if (
                                isinstance(marker, int)
                                and not isinstance(marker, bool)
                                and marker == RUNTIME_DATA_VERSION
                            ):
                                canonical_total = (
                                    await manager.async_initialize_new_runtime(
                                        asset_uuid
                                    )
                                )
                            elif marker is None:
                                entity_id = entity_registry.async_get_entity_id(
                                    "sensor",
                                    DOMAIN,
                                    unique_id,
                                )
                                if entity_id is None:
                                    raise AssetStoreError(
                                        "existing Runtime entity identity is missing"
                                    )
                                restored_seconds = (
                                    _legacy_restore_runtime_seconds(
                                        hass,
                                        entity_id,
                                    )
                                )
                                canonical_total = (
                                    await manager.async_import_legacy_runtime(
                                        asset_uuid,
                                        restored_seconds,
                                    )
                                )
                            else:
                                raise AssetStoreError(
                                    "Runtime provenance marker is invalid"
                                )
                        except (AssetStoreError, OSError) as err:
                            _LOGGER.error(
                                "Runtime migration for Asset %s is unresolved: %s. "
                                "The canonical total remains uninitialized and "
                                "migration will retry on reload",
                                asset_uuid,
                                err,
                            )

                    if canonical_total is not None:
                        initialized_asset = manager.asset(asset_uuid)
                        if initialized_asset is not None:
                            runtime_entities.append(
                                DeviceRuntimeHoursSensor(
                                    data=dict(subentry.data),
                                    asset=initialized_asset,
                                    device_entry=asset_devices[asset_uuid],
                                    unique_id=unique_id,
                                    manager=manager,
                                )
                            )

            _remove_unexpected_subentry_entities(
                entity_registry=entity_registry,
                entry=entry,
                subentry_id=subentry.subentry_id,
                expected_unique_ids=expected_unique_ids,
            )

            if runtime_entities:
                async_add_entities(
                    runtime_entities,
                    config_subentry_id=subentry.subentry_id,
                )


def _remove_unexpected_subentry_entities(
    *,
    entity_registry: er.EntityRegistry,
    entry: ConfigEntry,
    subentry_id: str,
    expected_unique_ids: set[str],
) -> None:
    """Remove only stale Device Lifecycle entities from one subentry."""
    for registry_entry in er.async_entries_for_config_entry(
        entity_registry,
        entry.entry_id,
    ):
        if (
            registry_entry.platform == DOMAIN
            and registry_entry.config_subentry_id == subentry_id
            and registry_entry.unique_id not in expected_unique_ids
        ):
            entity_registry.async_remove(registry_entry.entity_id)


class DeviceLifecycleSensor(SensorEntity):
    """Lifecycle information for one persistent physical Asset."""

    _attr_has_entity_name = True
    _attr_translation_key = "lifecycle"
    _attr_should_poll = False

    def __init__(
        self,
        *,
        asset: AssetData,
        purchase: PurchaseData | None,
        device_entry: dr.DeviceEntry,
        unique_id: str,
        purchase_title: str | None,
    ) -> None:
        """Initialize lifecycle sensor."""
        self._asset = asset
        self._purchase = purchase
        self._purchase_title = purchase_title
        self.device_entry = device_entry
        self._attr_unique_id = unique_id

    @property
    def icon(self) -> str:
        """Return an icon matching warranty state."""
        warranty_until = _parse_date(self._asset["warranty"].get("until"))
        if warranty_until is None:
            return "mdi:calendar-question"
        if warranty_until >= dt_util.now().date():
            return "mdi:calendar-check"
        return "mdi:calendar-alert"

    @property
    def native_value(self) -> str:
        """Return a compact warranty summary in the HA system language."""
        language = self.hass.config.language
        warranty_until = _parse_date(self._asset["warranty"].get("until"))

        if warranty_until is None:
            return (
                "Takuu ei määritetty"
                if _is_finnish(language)
                else "Warranty not specified"
            )

        today = dt_util.now().date()
        days = (warranty_until - today).days
        formatted = _format_warranty_date(warranty_until, language)

        if _is_finnish(language):
            if days >= 0:
                return f"Voimassa · {days} pv · {formatted}"
            return f"Päättynyt · {abs(days)} pv sitten · {formatted}"

        if days >= 0:
            unit = "day" if days == 1 else "days"
            return f"Active · {days} {unit} · {formatted}"

        elapsed = abs(days)
        unit = "day" if elapsed == 1 else "days"
        return f"Expired · {elapsed} {unit} ago · {formatted}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return stable Asset data plus compatible purchase metadata."""
        asset = self._asset
        purchase = self._purchase
        warranty = asset["warranty"]
        warranty_type = str(warranty.get("type") or WARRANTY_NONE)
        warranty_until = _parse_date(warranty.get("until"))
        warranty_active = (
            warranty_until is not None
            and warranty_until >= dt_util.now().date()
        )

        attrs: dict[str, Any] = {
            "asset_id": asset["asset_id"],
            "asset_uuid": asset["asset_uuid"],
            "takuun_tyyppi": _warranty_type_label(
                warranty_type,
                self.hass.config.language,
            ),
            "takuu_tila": (
                "voimassa"
                if warranty_active
                else "päättynyt"
                if warranty_until is not None
                else "ei_määritetty"
            ),
        }

        if purchase is not None:
            attrs["purchase_uuid"] = purchase["purchase_uuid"]
            if self._purchase_title is not None:
                attrs["ostos"] = self._purchase_title
            purchase_mapping = {
                "name": "ostoksen_nimi",
                "purchase_date": "ostopaiva",
                "seller": "myyja",
                "total_price": "ostoksen_hinta",
                "receipt_reference": "kuitti_tilausviite",
                "receipt_url": "kuitti_url",
                "notes": "huomautukset",
            }
            for key, attr_name in purchase_mapping.items():
                value = purchase.get(key)  # type: ignore[literal-required]
                if value in (None, ""):
                    continue
                if key == "total_price":
                    attrs[attr_name] = float(str(value))
                else:
                    attrs[attr_name] = value

            if purchase.get("total_price") is not None:
                attrs["valuutta"] = purchase["currency"]

        if asset.get("installed_date"):
            attrs["kayttoonottopaiva"] = asset["installed_date"]
        if warranty.get("until"):
            attrs["takuu_paattyy"] = warranty["until"]
        if warranty_until is not None:
            attrs["takuuta_jaljella_paivaa"] = max(
                0,
                (warranty_until - dt_util.now().date()).days,
            )

        asset_mapping = {
            "manufacturer": "valmistaja",
            "model": "malli",
            "model_id": "mallitunnus",
            "serial_number": "sarjanumero",
            "sw_version": "laiteohjelmisto",
            "hw_version": "laitteisto",
        }
        for key, attr_name in asset_mapping.items():
            value = asset.get(key)  # type: ignore[literal-required]
            if value not in (None, ""):
                attrs[attr_name] = value

        if asset.get("category"):
            attrs["kategoria"] = asset["category"]
        if asset.get("notes"):
            attrs["laitteen_huomautukset"] = asset["notes"]

        return attrs

    async def async_added_to_hass(self) -> None:
        """Refresh warranty state once per day."""
        await super().async_added_to_hass()

        @callback
        def _midnight_update(*_: Any) -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_track_time_change(
                self.hass,
                _midnight_update,
                hour=0,
                minute=0,
                second=5,
            )
        )


def _device_display_name(device: dr.DeviceEntry | None) -> str | None:
    """Return current read-only Device Registry display metadata."""
    if device is None:
        return None
    value = device.name_by_user or device.name or device.model
    return str(value) if value not in (None, "") else None


class DeviceDeploymentSensor(SensorEntity):
    """Projection of one Asset's canonical deployment state."""

    _attr_has_entity_name = True
    _attr_translation_key = "deployment"
    _attr_icon = "mdi:map-marker-radius-outline"
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["unknown", "not_deployed", "deployed"]

    def __init__(
        self,
        *,
        asset: AssetData,
        device_entry: dr.DeviceEntry,
    ) -> None:
        """Initialize a Deployment projection."""
        self._asset = asset
        self.device_entry = device_entry
        self._attr_unique_id = deployment_unique_id(asset["asset_uuid"])

    @property
    def native_value(self) -> str:
        """Return the exact canonical Deployment state."""
        return self._asset["deployment_state"]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return explicit deployment details without Area synchronization."""
        area_id = self._asset.get("ha_area_id")
        area = (
            ar.async_get(self.hass).async_get_area(area_id)
            if area_id is not None
            else None
        )
        area_state = (
            "not_set"
            if area_id is None
            else "present"
            if area is not None
            else "missing"
        )
        return {
            "installed_date": self._asset.get("installed_date"),
            "asset_area_id": area_id,
            "asset_area_name": area.name if area is not None else None,
            "asset_area_state": area_state,
        }


class DeviceRelationshipsSensor(SensorEntity):
    """Read-only projection of exact external Device Registry references."""

    _attr_has_entity_name = True
    _attr_translation_key = "relationships"
    _attr_icon = "mdi:devices"
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["none", "present", "missing"]
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        *,
        asset: AssetData,
        device_entry: dr.DeviceEntry,
        device_registry: dr.DeviceRegistry,
    ) -> None:
        """Initialize a Relationships projection."""
        self._asset = asset
        self._device_registry = device_registry
        self.device_entry = device_entry
        self._attr_unique_id = relationships_unique_id(asset["asset_uuid"])
        self.referenced_device_ids = frozenset(
            str(reference["device_id"])
            for reference in asset.get("ha_device_refs", [])
        )

    @property
    def native_value(self) -> str:
        """Return none, present, or missing from exact registry resolution."""
        if not self.referenced_device_ids:
            return "none"
        if any(
            self._device_registry.async_get(device_id) is None
            for device_id in self.referenced_device_ids
        ):
            return "missing"
        return "present"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return deterministic relationship state and derived display names."""
        primary_id: str | None = None
        related_ids: list[str] = []
        for reference in self._asset.get("ha_device_refs", []):
            device_id = str(reference["device_id"])
            if reference["role"] == "primary":
                primary_id = device_id
            elif reference["role"] == "related":
                related_ids.append(device_id)

        primary_device = (
            self._device_registry.async_get(primary_id)
            if primary_id is not None
            else None
        )
        primary_state = (
            "not_linked"
            if primary_id is None
            else "present"
            if primary_device is not None
            else "missing"
        )
        related_devices: list[dict[str, str | None]] = []
        missing_related_count = 0
        for device_id in related_ids:
            device = self._device_registry.async_get(device_id)
            state = "present" if device is not None else "missing"
            if device is None:
                missing_related_count += 1
            related_devices.append(
                {
                    "device_id": device_id,
                    "name": _device_display_name(device),
                    "state": state,
                }
            )

        return {
            "primary_state": primary_state,
            "primary_device_id": primary_id,
            "primary_device_name": _device_display_name(primary_device),
            "related_devices": related_devices,
            "related_device_count": len(related_ids),
            "missing_related_device_count": missing_related_count,
        }


class DeviceAssetIdSensor(SensorEntity):
    """Diagnostic projection of one permanent human-facing Asset ID."""

    _attr_has_entity_name = True
    _attr_translation_key = "asset_id"
    _attr_icon = "mdi:identifier"
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        *,
        asset: AssetData,
        device_entry: dr.DeviceEntry,
    ) -> None:
        """Initialize an Asset ID projection."""
        self._asset_id = asset["asset_id"]
        self.device_entry = device_entry
        self._attr_unique_id = asset_id_unique_id(asset["asset_uuid"])

    @property
    def native_value(self) -> str:
        """Return the permanent DLxxxx identity."""
        return self._asset_id


class _RelationshipsRegistryCoordinator:
    """One Device Registry listener shared by all relationship entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entities: list[DeviceRelationshipsSensor],
    ) -> None:
        """Index relationship entities by their exact stored device IDs."""
        self._hass = hass
        self._entities_by_device_id: dict[
            str,
            list[DeviceRelationshipsSensor],
        ] = {}
        for entity in entities:
            for device_id in entity.referenced_device_ids:
                self._entities_by_device_id.setdefault(device_id, []).append(
                    entity
                )

    @callback
    def _async_registry_updated(
        self,
        event: Event[dr.EventDeviceRegistryUpdatedData],
    ) -> None:
        """Refresh only projections that store the exact changed registry ID."""
        for entity in self._entities_by_device_id.get(
            event.data["device_id"],
            (),
        ):
            entity.async_write_ha_state()

    @callback
    def async_start(self) -> Callable[[], None]:
        """Subscribe once to Device Registry changes."""
        return self._hass.bus.async_listen(
            dr.EVENT_DEVICE_REGISTRY_UPDATED,
            self._async_registry_updated,
        )


class DeviceRuntimeHoursSensor(SensorEntity):
    """Projection of one Asset's canonical cumulative Runtime total."""

    _attr_has_entity_name = True
    _attr_translation_key = "runtime_hours"
    _attr_icon = "mdi:timer-outline"
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        *,
        data: dict[str, Any],
        asset: AssetData,
        device_entry: dr.DeviceEntry,
        unique_id: str,
        manager: AssetStoreManager,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialize a canonical Runtime projection."""
        total = asset["runtime"]["total_seconds"]
        if total is None:
            raise AssetStoreError(
                f"Asset {asset['asset_uuid']} Runtime is not initialized"
            )

        self._data = data
        self._asset_uuid = asset["asset_uuid"]
        self._asset_id = asset["asset_id"]
        self._manager = manager
        self._monotonic = monotonic
        self.device_entry = device_entry
        self._attr_unique_id = unique_id

        self._source_entity_id = str(data[CONF_SOURCE_ENTITY_ID])
        self._runtime_mode = str(data[CONF_RUNTIME_MODE])
        self._power_threshold = float(data.get(CONF_POWER_THRESHOLD, 0.0))
        self._power_hysteresis = float(
            data.get(
                CONF_POWER_HYSTERESIS,
                DEFAULT_POWER_HYSTERESIS,
            )
        )

        self._committed_seconds = Decimal(total)
        self._pending: list[_PendingRuntimeDelta] = []
        self._active_since: float | None = None
        self._runtime_lock = asyncio.Lock()
        self._removing = False
        self._unsub_source: Callable[[], None] | None = None
        self._unsub_interval: Callable[[], None] | None = None
        self._unsub_shutdown: Callable[[], None] | None = None

    @property
    def native_value(self) -> Decimal:
        """Return committed, pending, and currently active Runtime in hours."""
        total_seconds = self._committed_seconds + sum(
            (item.delta for item in self._pending),
            start=Decimal(0),
        )
        if self._active_since is not None:
            elapsed = self._monotonic() - self._active_since
            if elapsed > 0:
                total_seconds += Decimal(str(elapsed))
        return (total_seconds / Decimal(3600)).quantize(
            Decimal("0.000001")
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return Runtime tracking metadata and stable Asset identity."""
        source_state = self.hass.states.get(self._source_entity_id)
        attrs: dict[str, Any] = {
            "asset_id": self._asset_id,
            "asset_uuid": self._asset_uuid,
            "lahde_entiteetti": self._source_entity_id,
            "lahde_saatavilla": self._source_available(source_state),
            "seurantatapa": self._runtime_mode,
            "aktiivinen": self._active_since is not None,
        }

        if self._runtime_mode == RUNTIME_MODE_POWER:
            attrs["tehoraja_w"] = self._power_threshold
            attrs["hystereesi_w"] = self._power_hysteresis
            attrs["pysaytysraja_w"] = max(
                0.0,
                self._power_threshold - self._power_hysteresis,
            )

        return attrs

    async def async_added_to_hass(self) -> None:
        """Start a fresh observable interval and register Runtime listeners."""
        await super().async_added_to_hass()

        current_state = self.hass.states.get(self._source_entity_id)
        if self._is_active(current_state, currently_active=False):
            self._active_since = self._monotonic()

        # These are intentionally not async_on_remove callbacks: Home Assistant
        # runs those before async_will_remove_from_hass(), while Runtime must
        # attempt its final checkpoint before listener cleanup.
        self._unsub_source = async_track_state_change_event(
            self.hass,
            [self._source_entity_id],
            self._handle_source_change,
        )
        self._unsub_interval = async_track_time_interval(
            self.hass,
            self._handle_periodic_checkpoint,
            RUNTIME_REFRESH_INTERVAL,
        )
        self._unsub_shutdown = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP,
            self._handle_shutdown,
        )

        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Attempt a final serialized checkpoint before listener cleanup."""
        self._removing = True
        try:
            async with self._runtime_lock:
                self._seal_active(self._monotonic(), continue_active=False)
                await self._async_flush_pending()
                self.async_write_ha_state()
        finally:
            self._cleanup_runtime_listeners()
        await super().async_will_remove_from_hass()

    async def _handle_source_change(
        self,
        event: Event[EventStateChangedData],
    ) -> None:
        """Handle activity and availability transitions with hysteresis."""
        if self._removing:
            return

        old_state = event.data["old_state"]
        new_state = event.data["new_state"]

        async with self._runtime_lock:
            currently_active = self._active_since is not None
            new_active = self._is_active(
                new_state,
                currently_active=currently_active,
            )
            old_available = self._source_available(old_state)
            new_available = self._source_available(new_state)

            if (
                new_active == currently_active
                and old_available == new_available
            ):
                return

            now = self._monotonic()
            if currently_active and not new_active:
                self._seal_active(now, continue_active=False)
            elif not currently_active and new_active:
                self._active_since = now

            await self._async_flush_pending()
            self.async_write_ha_state()

    async def _handle_periodic_checkpoint(self, _now: datetime) -> None:
        """Seal active elapsed and durably retry all pending Runtime deltas."""
        if self._removing:
            return
        async with self._runtime_lock:
            if self._active_since is not None:
                self._seal_active(self._monotonic(), continue_active=True)
            await self._async_flush_pending()
            self.async_write_ha_state()

    async def _handle_shutdown(self, _event: Event) -> None:
        """Attempt a final checkpoint during normal Home Assistant shutdown."""
        async with self._runtime_lock:
            self._seal_active(self._monotonic(), continue_active=False)
            await self._async_flush_pending()
            self.async_write_ha_state()

    def _seal_active(self, now: float, *, continue_active: bool) -> None:
        """Move observable active time to an ordered retryable pending delta."""
        if self._active_since is None:
            return

        elapsed = now - self._active_since
        if elapsed > 0:
            delta = Decimal(str(elapsed))
            expected = self._committed_seconds + sum(
                (item.delta for item in self._pending),
                start=Decimal(0),
            )
            self._pending.append(_PendingRuntimeDelta(expected, delta))

        self._active_since = now if continue_active else None

    async def _async_flush_pending(self) -> None:
        """Commit pending deltas in order, retaining every failed delta."""
        while self._pending:
            pending = self._pending[0]
            try:
                committed = await self._manager.async_commit_runtime_delta(
                    self._asset_uuid,
                    expected_total=pending.expected_total,
                    delta=pending.delta,
                )
            except (AssetStoreError, OSError) as err:
                _LOGGER.error(
                    "Runtime checkpoint for Asset %s failed; %s seconds remain "
                    "pending and will be retried: %s",
                    self._asset_uuid,
                    sum(
                        (item.delta for item in self._pending),
                        start=Decimal(0),
                    ),
                    err,
                )
                return

            expected_committed = pending.expected_total + pending.delta
            if committed != expected_committed:
                _LOGGER.error(
                    "Runtime checkpoint for Asset %s returned an unexpected total; "
                    "the delta remains pending",
                    self._asset_uuid,
                )
                return
            self._committed_seconds = committed
            self._pending.pop(0)

    def _cleanup_runtime_listeners(self) -> None:
        """Remove Runtime listeners after the final checkpoint attempt."""
        for attribute in (
            "_unsub_source",
            "_unsub_interval",
            "_unsub_shutdown",
        ):
            unsubscribe = getattr(self, attribute)
            if unsubscribe is not None:
                unsubscribe()
                setattr(self, attribute, None)

    def _source_available(self, state: State | None) -> bool:
        """Return whether a source state is usable."""
        return (
            state is not None
            and state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
        )

    def _is_active(
        self,
        state: State | None,
        *,
        currently_active: bool,
    ) -> bool:
        """Return whether the configured source currently means active."""
        if not self._source_available(state):
            return False

        if self._runtime_mode == RUNTIME_MODE_ON:
            return state.state == STATE_ON

        if self._runtime_mode == RUNTIME_MODE_POWER:
            value = _power_value_watts(state)
            if value is None:
                return False

            if currently_active:
                stop_threshold = max(
                    0.0,
                    self._power_threshold - self._power_hysteresis,
                )
                if stop_threshold == 0.0:
                    return value > 0.0
                return value >= stop_threshold

            return value > self._power_threshold

        return False
