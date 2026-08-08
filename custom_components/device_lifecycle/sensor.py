"""Lifecycle sensors for purchase subentries."""

from __future__ import annotations

from datetime import date
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_change
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
)


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
    """Set up one lifecycle sensor per selected device in every purchase."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)

    valid_purchase_subentry_ids = {
        subentry.subentry_id
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE_PURCHASE
    }

    # Explicit safety cleanup: if an entire purchase was deleted, remove only
    # lifecycle entities that belonged to that deleted purchase. Never touch
    # entities from another integration or another existing purchase.
    for registry_entry in er.async_entries_for_config_entry(
        entity_registry,
        entry.entry_id,
    ):
        if (
            registry_entry.platform == DOMAIN
            and registry_entry.config_subentry_id is not None
            and registry_entry.config_subentry_id not in valid_purchase_subentry_ids
        ):
            entity_registry.async_remove(registry_entry.entity_id)

    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_TYPE_PURCHASE:
            continue

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

        # When a device is removed from an existing purchase, remove only the
        # lifecycle entity that belongs to this exact purchase subentry.
        for registry_entry in er.async_entries_for_config_entry(
            entity_registry,
            entry.entry_id,
        ):
            if (
                registry_entry.platform == DOMAIN
                and registry_entry.config_subentry_id == subentry.subentry_id
                and registry_entry.unique_id not in expected_unique_ids
            ):
                entity_registry.async_remove(registry_entry.entity_id)

        if entities:
            async_add_entities(
                entities,
                config_subentry_id=subentry.subentry_id,
            )


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

        # Link the helper entity to the existing physical HA device without
        # claiming ownership of that device.
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
        formatted = f"{warranty_until.day}.{warranty_until.month}.{warranty_until.year}"

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
