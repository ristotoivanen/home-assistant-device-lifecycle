"""Lifecycle and runtime sensors for Device Lifecycle."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import isfinite
from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
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
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
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
    CONF_DEVICE_IDS,
    CONF_POWER_HYSTERESIS,
    CONF_POWER_THRESHOLD,
    CONF_RUNTIME_MODE,
    CONF_SOURCE_ENTITY_ID,
    DEFAULT_POWER_HYSTERESIS,
    DOMAIN,
    RUNTIME_MODE_ON,
    RUNTIME_MODE_POWER,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
    WARRANTY_MANUAL,
    WARRANTY_NONE,
    WARRANTY_ONE_YEAR,
    WARRANTY_TWO_YEARS,
)
from .migration import lifecycle_unique_id, runtime_unique_id
from .models import AssetData, PurchaseData
from .storage import AssetStoreManager

RUNTIME_REFRESH_INTERVAL = timedelta(minutes=5)


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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up lifecycle and runtime sensors from normalized Asset Core data."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    manager: AssetStoreManager = entry.runtime_data

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

    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_TYPE_PURCHASE:
            purchase = manager.purchase_for_subentry(subentry.subentry_id)
            entities: list[DeviceLifecycleSensor] = []
            expected_unique_ids: set[str] = set()

            if purchase is not None:
                for device_id_value in subentry.data.get(CONF_DEVICE_IDS, []):
                    device_id = str(device_id_value)
                    device_entry = device_registry.async_get(device_id)
                    asset = manager.asset_for_device_id(device_id)
                    if device_entry is None or asset is None:
                        continue
                    if asset.get("purchase_uuid") != purchase["purchase_uuid"]:
                        continue

                    unique_id = lifecycle_unique_id(asset["asset_uuid"])
                    expected_unique_ids.add(unique_id)
                    entities.append(
                        DeviceLifecycleSensor(
                            asset=asset,
                            purchase=purchase,
                            device_entry=device_entry,
                            unique_id=unique_id,
                            purchase_title=subentry.title,
                        )
                    )

            _remove_unexpected_subentry_entities(
                entity_registry=entity_registry,
                entry=entry,
                subentry_id=subentry.subentry_id,
                expected_unique_ids=expected_unique_ids,
            )

            if entities:
                async_add_entities(
                    entities,
                    config_subentry_id=subentry.subentry_id,
                )

        elif subentry.subentry_type == SUBENTRY_TYPE_RUNTIME:
            device_id = str(subentry.data.get(CONF_DEVICE_ID) or "")
            source_entity_id = str(
                subentry.data.get(CONF_SOURCE_ENTITY_ID) or ""
            )
            device_entry = device_registry.async_get(device_id)

            asset = manager.asset(str(subentry.data.get(CONF_ASSET_UUID) or ""))
            if asset is None and device_id:
                asset = manager.asset_for_device_id(device_id)

            expected_unique_ids: set[str] = set()
            runtime_entities: list[DeviceRuntimeHoursSensor] = []

            if device_entry is not None and source_entity_id and asset is not None:
                unique_id = runtime_unique_id(asset["asset_uuid"])
                expected_unique_ids.add(unique_id)
                runtime_entities.append(
                    DeviceRuntimeHoursSensor(
                        data=dict(subentry.data),
                        asset=asset,
                        device_entry=device_entry,
                        unique_id=unique_id,
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
        purchase: PurchaseData,
        device_entry: dr.DeviceEntry,
        unique_id: str,
        purchase_title: str,
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
            "purchase_uuid": purchase["purchase_uuid"],
            "ostos": self._purchase_title,
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


class DeviceRuntimeHoursSensor(RestoreSensor):
    """Cumulative runtime hours for one persistent physical Asset."""

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
    ) -> None:
        """Initialize runtime hours sensor."""
        self._data = data
        self._asset_uuid = asset["asset_uuid"]
        self._asset_id = asset["asset_id"]
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

        self._stored_hours = Decimal("0")
        self._active_since: datetime | None = None

    @property
    def native_value(self) -> Decimal:
        """Return cumulative runtime in hours."""
        total = self._stored_hours

        if self._active_since is not None:
            elapsed = dt_util.utcnow() - self._active_since
            total += Decimal(str(elapsed.total_seconds())) / Decimal("3600")

        return total.quantize(Decimal("0.000001"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return runtime tracking metadata and stable Asset identity."""
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
        """Restore total runtime and start listeners."""
        await super().async_added_to_hass()

        if (
            last_sensor_data := await self.async_get_last_sensor_data()
        ) is not None and last_sensor_data.native_value is not None:
            try:
                self._stored_hours = Decimal(str(last_sensor_data.native_value))
            except (InvalidOperation, TypeError, ValueError):
                self._stored_hours = Decimal("0")

        current_state = self.hass.states.get(self._source_entity_id)
        if self._is_active(current_state, currently_active=False):
            self._active_since = dt_util.utcnow()

        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [self._source_entity_id],
                self._handle_source_change,
            )
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._handle_periodic_refresh,
                RUNTIME_REFRESH_INTERVAL,
            )
        )

        self.async_write_ha_state()

    @callback
    def _handle_source_change(
        self,
        event: Event[EventStateChangedData],
    ) -> None:
        """Handle activity or availability transitions with hysteresis."""
        old_state = event.data["old_state"]
        new_state = event.data["new_state"]

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

        now = dt_util.utcnow()

        if currently_active and not new_active:
            self._commit_elapsed(now)
        elif not currently_active and new_active:
            self._active_since = now

        self.async_write_ha_state()

    @callback
    def _handle_periodic_refresh(self, _now: datetime) -> None:
        """Refresh a running counter without creating source-event churn."""
        if self._active_since is not None:
            self.async_write_ha_state()

    def _commit_elapsed(self, now: datetime) -> None:
        """Move the active interval into the restored runtime total."""
        if self._active_since is None:
            return

        elapsed = now - self._active_since
        if elapsed.total_seconds() > 0:
            self._stored_hours += Decimal(
                str(elapsed.total_seconds())
            ) / Decimal("3600")

        self._active_since = None

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
