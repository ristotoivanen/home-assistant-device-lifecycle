"""Maintenance preparation binary sensors for Device Lifecycle."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .maintenance_entities import (
    MAINTENANCE_PREPARATION_SUFFIX,
    MaintenanceEntity,
    MaintenanceEntityCoordinator,
    maintenance_preparation_unique_id,
)
from .maintenance_projection import PreparationState
from .models import MaintenanceScheduleData
from .storage import AssetStoreManager


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one preparation sensor per Schedule with a reminder."""
    manager: AssetStoreManager = entry.runtime_data
    MaintenanceEntityCoordinator(
        hass,
        entry,
        manager,
        domain="binary_sensor",
        suffixes=(MAINTENANCE_PREPARATION_SUFFIX,),
        expected_unique_ids=_preparation_unique_ids,
        factory=_preparation_sensors,
        async_add_entities=async_add_entities,
    ).async_setup()


def _preparation_unique_ids(schedule: MaintenanceScheduleData) -> set[str]:
    """Only a Schedule with a preparation reminder has a preparation sensor."""
    if schedule["preparation_reminder"] is None:
        return set()
    return {maintenance_preparation_unique_id(schedule["schedule_uuid"])}


def _preparation_sensors(
    manager: AssetStoreManager,
    schedule: MaintenanceScheduleData,
    device_entry: dr.DeviceEntry,
) -> list[MaintenanceEntity]:
    return [
        MaintenancePreparationBinarySensor(
            manager=manager,
            schedule=schedule,
            device_entry=device_entry,
            unique_id=unique_id,
        )
        for unique_id in sorted(_preparation_unique_ids(schedule))
    ]


class MaintenancePreparationBinarySensor(MaintenanceEntity, BinarySensorEntity):
    """Whether the preparation window before a calendar due date is open.

    ACTIVE is on and INACTIVE is off. UNKNOWN is Home Assistant's unknown
    (``is_on`` is ``None``), never off. A disabled Schedule has no active
    projection, so no preparation is active. An archived Asset makes the
    entity unavailable, never off. No device class describes this exactly,
    so none is set.
    """

    _attr_translation_key = "maintenance_preparation"

    @property
    def is_on(self) -> bool | None:
        """Return the projected preparation state."""
        projection = self._projection
        if projection is None:
            return None
        if not projection.active:
            return False
        if projection.preparation is PreparationState.ACTIVE:
            return True
        if projection.preparation is PreparationState.INACTIVE:
            return False
        return None
