"""Lifecycle and runtime sensors for Device Lifecycle."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
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

RUNTIME_REFRESH_INTERVAL = timedelta(minutes=5)


def _parse_date(value: str | None) -> date | None:
    """Parse a Home Assistant date-selector value."""
    if not value:
        return None
    return dt_util.parse_date(value)


def _warranty_type(data: dict[str, Any]) -> str:
    """Return explicit warranty type or infer old pre-0.3.4 data."""
    value = data.get(CONF_WARRANTY_TYPE)
    if value:
        return str(value)
    if data.get(CONF_WARRANTY_UNTIL):
        return WARRANTY_MANUAL
    return WARRANTY_NONE


def _warranty_type_label(value: str) -> str:
    """Return a human-readable Finnish warranty type for attributes."""
    return {
        WARRANTY_NONE: "Ei määritetty",
        WARRANTY_ONE_YEAR: "1 vuosi",
        WARRANTY_TWO_YEARS: "2 vuotta",
        WARRANTY_MANUAL: "Manuaalinen",
    }.get(value, value)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up lifecycle and runtime sensors from config subentries."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    valid_subentry_ids = {
        subentry.subentry_id
        for subentry in entry.subentries.values()
        if subentry.subentry_type
        in (SUBENTRY_TYPE_PURCHASE, SUBENTRY_TYPE_RUNTIME)
    }

    # If an entire subentry was deleted, remove only entities that belonged to
    # that deleted Device Lifecycle subentry.
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
            entities: list[DeviceLifecycleSensor] = []
            expected_unique_ids: set[str] = set()

            for device_id in subentry.data.get(CONF_DEVICE_IDS, []):
                device_entry = device_registry.async_get(device_id)
                if device_entry is None:
                    continue

                unique_id = f"{subentry.subentry_id}_{device_id}_lifecycle"
                expected_unique_ids.add(unique_id)

                entities.append(
                    DeviceLifecycleSensor(
                        data=dict(subentry.data),
                        device_entry=device_entry,
                        unique_id=unique_id,
                        purchase_title=subentry.title,
                        subentry_id=subentry.subentry_id,
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

            expected_unique_ids: set[str] = set()
            runtime_entities: list[DeviceRuntimeHoursSensor] = []

            if device_entry is not None and source_entity_id:
                unique_id = (
                    f"{subentry.subentry_id}_{device_id}_runtime_hours"
                )
                expected_unique_ids.add(unique_id)

                runtime_entities.append(
                    DeviceRuntimeHoursSensor(
                        data=dict(subentry.data),
                        device_entry=device_entry,
                        unique_id=unique_id,
                        subentry_id=subentry.subentry_id,
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
    """Lifecycle information linked to an existing physical HA device."""

    _attr_has_entity_name = True
    _attr_name = "Elinkaari"
    _attr_should_poll = False

    def __init__(
        self,
        *,
        data: dict[str, Any],
        device_entry: dr.DeviceEntry,
        unique_id: str,
        purchase_title: str,
        subentry_id: str,
    ) -> None:
        """Initialize lifecycle sensor."""
        self._data = data
        self._purchase_title = purchase_title
        self.device_entry = device_entry
        self._attr_unique_id = unique_id
        self._attr_config_subentry_id = subentry_id

    @property
    def icon(self) -> str:
        """Return an icon matching warranty state."""
        warranty_until = _parse_date(self._data.get(CONF_WARRANTY_UNTIL))
        if warranty_until is None:
            return "mdi:calendar-question"
        if warranty_until >= dt_util.now().date():
            return "mdi:calendar-check"
        return "mdi:calendar-alert"

    @property
    def native_value(self) -> str:
        """Return a compact, human-readable warranty summary."""
        warranty_until = _parse_date(self._data.get(CONF_WARRANTY_UNTIL))
        if warranty_until is None:
            return "Takuu ei määritetty"

        today = dt_util.now().date()
        days = (warranty_until - today).days
        formatted = (
            f"{warranty_until.day}."
            f"{warranty_until.month}."
            f"{warranty_until.year}"
        )

        if days >= 0:
            return f"Voimassa · {days} pv · {formatted}"

        return f"Päättynyt · {abs(days)} pv sitten · {formatted}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return purchase and automatically discovered device metadata."""
        data = self._data
        device = self.device_entry

        warranty_until = _parse_date(data.get(CONF_WARRANTY_UNTIL))
        warranty_active = (
            warranty_until is not None
            and warranty_until >= dt_util.now().date()
        )

        attrs: dict[str, Any] = {
            "ostos": self._purchase_title,
            "takuun_tyyppi": _warranty_type_label(_warranty_type(data)),
            "takuu_tila": (
                "voimassa"
                if warranty_active
                else "päättynyt"
                if warranty_until is not None
                else "ei_määritetty"
            ),
        }

        mapping = {
            CONF_PURCHASE_NAME: "ostoksen_nimi",
            CONF_PURCHASE_DATE: "ostopaiva",
            CONF_INSTALLED_DATE: "kayttoonottopaiva",
            CONF_WARRANTY_UNTIL: "takuu_paattyy",
            CONF_SELLER: "myyja",
            CONF_PURCHASE_PRICE: "ostoksen_hinta",
            CONF_RECEIPT_REFERENCE: "kuitti_tilausviite",
            CONF_NOTES: "huomautukset",
        }

        for key, attr_name in mapping.items():
            value = data.get(key)
            if value not in (None, ""):
                attrs[attr_name] = value

        if CONF_PURCHASE_PRICE in data:
            attrs["valuutta"] = "EUR"

        if warranty_until is not None:
            attrs["takuuta_jaljella_paivaa"] = max(
                0,
                (warranty_until - dt_util.now().date()).days,
            )

        automatic = {
            "valmistaja": getattr(device, "manufacturer", None),
            "malli": getattr(device, "model", None),
            "mallitunnus": getattr(device, "model_id", None),
            "sarjanumero": getattr(device, "serial_number", None),
            "laiteohjelmisto": getattr(device, "sw_version", None),
            "laitteisto": getattr(device, "hw_version", None),
        }
        for key, value in automatic.items():
            if value not in (None, ""):
                attrs[key] = value

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
    """Cumulative runtime hours for one physical Home Assistant device."""

    _attr_has_entity_name = True
    _attr_name = "Käyttötunnit"
    _attr_icon = "mdi:timer-outline"
    _attr_native_unit_of_measurement = UnitOfTime.HOURS
    _attr_should_poll = False
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 2

    def __init__(
        self,
        *,
        data: dict[str, Any],
        device_entry: dr.DeviceEntry,
        unique_id: str,
        subentry_id: str,
    ) -> None:
        """Initialize runtime hours sensor."""
        self._data = data
        self.device_entry = device_entry
        self._attr_unique_id = unique_id
        self._attr_config_subentry_id = subentry_id

        self._source_entity_id = str(data[CONF_SOURCE_ENTITY_ID])
        self._runtime_mode = str(data[CONF_RUNTIME_MODE])
        self._power_threshold = float(
            data.get(CONF_POWER_THRESHOLD, 0.0)
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
        """Return runtime tracking metadata."""
        source_state = self.hass.states.get(self._source_entity_id)
        attrs: dict[str, Any] = {
            "lahde_entiteetti": self._source_entity_id,
            "lahde_saatavilla": self._source_available(source_state),
            "seurantatapa": self._runtime_mode,
            "aktiivinen": self._active_since is not None,
        }

        if self._runtime_mode == RUNTIME_MODE_POWER:
            attrs["tehoraja_w"] = self._power_threshold

        return attrs

    async def async_added_to_hass(self) -> None:
        """Restore total runtime and start listeners."""
        await super().async_added_to_hass()

        if (
            last_sensor_data := await self.async_get_last_sensor_data()
        ) is not None and last_sensor_data.native_value is not None:
            try:
                self._stored_hours = Decimal(
                    str(last_sensor_data.native_value)
                )
            except (InvalidOperation, TypeError, ValueError):
                self._stored_hours = Decimal("0")

        current_state = self.hass.states.get(self._source_entity_id)
        if self._is_active(current_state):
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
        """Handle only activity or availability transitions."""
        old_state = event.data["old_state"]
        new_state = event.data["new_state"]

        old_active = self._is_active(old_state)
        new_active = self._is_active(new_state)
        old_available = self._source_available(old_state)
        new_available = self._source_available(new_state)

        if (
            old_active == new_active
            and old_available == new_available
        ):
            return

        now = dt_util.utcnow()

        if self._active_since is not None and not new_active:
            self._commit_elapsed(now)

        if self._active_since is None and new_active:
            self._active_since = now

        self.async_write_ha_state()

    @callback
    def _handle_periodic_refresh(self, _now: datetime) -> None:
        """Refresh a running counter without creating source-event churn."""
        if self._active_since is not None:
            self.async_write_ha_state()

    def _commit_elapsed(self, now: datetime) -> None:
        """Move the active interval into the persisted runtime total."""
        if self._active_since is None:
            return

        elapsed = now - self._active_since
        if elapsed.total_seconds() > 0:
            self._stored_hours += (
                Decimal(str(elapsed.total_seconds()))
                / Decimal("3600")
            )

        self._active_since = None

    def _source_available(self, state: State | None) -> bool:
        """Return whether a source state is usable."""
        return (
            state is not None
            and state.state not in (STATE_UNKNOWN, STATE_UNAVAILABLE)
        )

    def _is_active(self, state: State | None) -> bool:
        """Return whether the configured source currently means active."""
        if not self._source_available(state):
            return False

        if self._runtime_mode == RUNTIME_MODE_ON:
            return state.state == STATE_ON

        if self._runtime_mode == RUNTIME_MODE_POWER:
            try:
                return float(state.state) > self._power_threshold
            except (TypeError, ValueError):
                return False

        return False
