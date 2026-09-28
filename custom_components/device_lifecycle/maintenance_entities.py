"""Shared plumbing for the Maintenance entities (not a platform).

One current projection per Maintenance Schedule is exposed on the owning
Asset Device: a status and a next-maintenance sensor, and a preparation
binary_sensor for a Schedule with a preparation reminder. Every rule comes
from ``AssetStoreManager.maintenance_projection``; nothing here derives a
due state, a date, or "today".

Each platform keeps one ``MaintenanceEntityCoordinator``: one publish
listener and one local-midnight trigger shared by all its Maintenance
entities. A publish that changed an Asset recomputes that Asset's entities,
adds entities for a new Schedule or a re-added reminder under the same
unique ID, and removes the registry entries of a hard-deleted Schedule or
of a removed reminder. Archive and Restore only change availability; they
never add, remove, disable, or recreate an entity.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import suppress
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_change

from .const import DOMAIN
from .exposure import asset_device_entry
from .maintenance_projection import MaintenanceProjection
from .models import MaintenanceScheduleData
from .storage import AssetStoreManager

MAINTENANCE_STATUS_SUFFIX = "_maintenance_status"
MAINTENANCE_DUE_DATE_SUFFIX = "_maintenance_due_date"
MAINTENANCE_PREPARATION_SUFFIX = "_maintenance_preparation"

# The status sensor's states: the due states, and ``disabled`` as a
# presentation value for a disabled Schedule. Never persisted.
MAINTENANCE_STATUSES = ("ok", "unknown", "due", "overdue", "disabled")


def maintenance_status_unique_id(schedule_uuid: str) -> str:
    """Return the Schedule-owned Maintenance status unique ID."""
    return f"{schedule_uuid}{MAINTENANCE_STATUS_SUFFIX}"


def maintenance_due_date_unique_id(schedule_uuid: str) -> str:
    """Return the Schedule-owned next-maintenance date unique ID."""
    return f"{schedule_uuid}{MAINTENANCE_DUE_DATE_SUFFIX}"


def maintenance_preparation_unique_id(schedule_uuid: str) -> str:
    """Return the Schedule-owned Maintenance preparation unique ID."""
    return f"{schedule_uuid}{MAINTENANCE_PREPARATION_SUFFIX}"


class MaintenanceEntity(Entity):
    """One Schedule's current projection on its Asset Device.

    Holds identities and the latest detached projection only, never a Store
    reference. Availability is read from the canonical Archive state each
    time: an archived Asset makes the entity unavailable, which takes
    precedence over any presentation value.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        *,
        manager: AssetStoreManager,
        schedule: MaintenanceScheduleData,
        device_entry: dr.DeviceEntry,
        unique_id: str,
    ) -> None:
        """Bind the entity to its Schedule and Asset Device."""
        self._manager = manager
        self._asset_uuid = schedule["asset_uuid"]
        self._schedule_uuid = schedule["schedule_uuid"]
        self.device_entry = device_entry
        self._attr_unique_id = unique_id
        self._projection: MaintenanceProjection | None = None
        self._following = False
        self.refresh_projection(schedule)

    async def async_added_to_hass(self) -> None:
        """Start receiving refreshes from the platform coordinator."""
        await super().async_added_to_hass()
        self._following = True

    async def async_will_remove_from_hass(self) -> None:
        """Stop receiving refreshes."""
        self._following = False
        await super().async_will_remove_from_hass()

    @property
    def following(self) -> bool:
        """Return whether the entity is added and writes its state."""
        return self._following

    @property
    def schedule_uuid(self) -> str:
        """Return the canonical Schedule UUID."""
        return self._schedule_uuid

    @property
    def asset_uuid(self) -> str:
        """Return the owning Asset UUID."""
        return self._asset_uuid

    def refresh_projection(self, schedule: MaintenanceScheduleData) -> None:
        """Re-read the Schedule's name and its current projection."""
        placeholders = {"schedule": schedule["name"]}
        if getattr(self, "_attr_translation_placeholders", None) != placeholders:
            self._attr_translation_placeholders = placeholders
            # The translated name is cached; a renamed Schedule renames the
            # entity's display name, never its entity_id or unique_id.
            with suppress(AttributeError):
                delattr(self, "name")
        self._projection = self._manager.maintenance_projection(self._schedule_uuid)

    @property
    def available(self) -> bool:
        """Unavailable only while the owning Asset is archived."""
        return self._projection is not None and not self._manager.asset_archived(
            self._asset_uuid
        )

    @property
    def active_projection(self) -> MaintenanceProjection | None:
        """Return the projection if it is active, else ``None``."""
        projection = self._projection
        return projection if projection is not None and projection.active else None


EntityFactory = Callable[
    [AssetStoreManager, MaintenanceScheduleData, dr.DeviceEntry],
    list[MaintenanceEntity],
]


def _schedules_by_asset(
    manager: AssetStoreManager,
) -> list[tuple[str, list[MaintenanceScheduleData]]]:
    """Every Asset's Schedules, ordered by Asset ID, then name, then UUID."""
    assets = sorted(
        manager.assets(), key=lambda item: (item["asset_id"], item["asset_uuid"])
    )
    return [
        (
            asset["asset_uuid"],
            manager.maintenance_schedules_for_asset(asset["asset_uuid"]),
        )
        for asset in assets
    ]


class MaintenanceEntityCoordinator:
    """The one publish listener and midnight trigger of a platform's entities."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        manager: AssetStoreManager,
        *,
        domain: str,
        suffixes: Iterable[str],
        expected_unique_ids: Callable[[MaintenanceScheduleData], set[str]],
        factory: EntityFactory,
        async_add_entities: AddConfigEntryEntitiesCallback,
    ) -> None:
        """Describe the platform's Maintenance entities."""
        self._hass = hass
        self._entry = entry
        self._manager = manager
        self._domain = domain
        self._suffixes = tuple(suffixes)
        self._expected_unique_ids = expected_unique_ids
        self._factory = factory
        self._async_add_entities = async_add_entities
        self._entities: dict[str, MaintenanceEntity] = {}
        self._midnight_started = False

    def _is_maintenance_entry(self, registry_entry: er.RegistryEntry) -> bool:
        return (
            registry_entry.platform == DOMAIN
            and registry_entry.domain == self._domain
            and registry_entry.config_entry_id == self._entry.entry_id
            and registry_entry.unique_id.endswith(self._suffixes)
        )

    @callback
    def _remove_orphans(self, expected: set[str], asset_uuids: set[str] | None) -> None:
        """Remove this platform's Maintenance entries no Schedule expects.

        Only the deterministic ``_maintenance_*`` identities of this entry
        and platform are considered; with ``asset_uuids`` only entries whose
        entity is known to belong to one of those Assets.
        """
        registry = er.async_get(self._hass)
        for registry_entry in er.async_entries_for_config_entry(
            registry, self._entry.entry_id
        ):
            if (
                not self._is_maintenance_entry(registry_entry)
                or registry_entry.unique_id in expected
            ):
                continue
            entity = self._entities.get(registry_entry.unique_id)
            if asset_uuids is not None and (
                entity is None or entity.asset_uuid not in asset_uuids
            ):
                continue
            self._entities.pop(registry_entry.unique_id, None)
            registry.async_remove(registry_entry.entity_id)

    def _build(
        self, asset_uuid: str, schedules: list[MaintenanceScheduleData]
    ) -> list[MaintenanceEntity]:
        """Create the entities of Schedules not represented yet."""
        missing = [
            schedule
            for schedule in schedules
            if self._expected_unique_ids(schedule) - set(self._entities)
        ]
        if not missing:
            return []
        device_entry = asset_device_entry(
            dr.async_get(self._hass),
            config_entry_id=self._entry.entry_id,
            asset_uuid=asset_uuid,
        )
        if device_entry is None:
            # A new Asset gets its Asset Device on the next setup, and its
            # Maintenance entities with it.
            return []
        created: list[MaintenanceEntity] = []
        for schedule in missing:
            for entity in self._factory(self._manager, schedule, device_entry):
                unique_id = str(entity.unique_id)
                if unique_id not in self._entities:
                    self._entities[unique_id] = entity
                    created.append(entity)
        return created

    @callback
    def async_setup(self) -> None:
        """Clean up orphans, add every current entity, and start following."""
        per_asset = _schedules_by_asset(self._manager)
        expected = {
            unique_id
            for _asset_uuid, schedules in per_asset
            for schedule in schedules
            for unique_id in self._expected_unique_ids(schedule)
        }
        self._remove_orphans(expected, None)
        created = [
            entity
            for asset_uuid, schedules in per_asset
            for entity in self._build(asset_uuid, schedules)
        ]
        self._add(created)
        self._entry.async_on_unload(
            self._manager.async_add_publish_listener(self._handle_store_publish)
        )

    @callback
    def _add(self, created: list[MaintenanceEntity]) -> None:
        """Add new entities and, with the first one, the midnight trigger."""
        if not created:
            return
        # Parent-owned: no config subentry is passed.
        self._async_add_entities(created)
        if not self._midnight_started:
            self._midnight_started = True
            self._entry.async_on_unload(
                async_track_time_change(
                    self._hass, self._handle_midnight, hour=0, minute=0, second=0
                )
            )

    @callback
    def _handle_store_publish(self, changed: frozenset[str]) -> None:
        """Follow a Store commit for the Assets it changed."""
        created: list[MaintenanceEntity] = []
        for asset_uuid in sorted(changed):
            schedules = self._manager.maintenance_schedules_for_asset(asset_uuid)
            expected = {
                unique_id
                for schedule in schedules
                for unique_id in self._expected_unique_ids(schedule)
            }
            self._remove_orphans(expected, {asset_uuid})
            by_uuid = {schedule["schedule_uuid"]: schedule for schedule in schedules}
            for entity in list(self._entities.values()):
                schedule = by_uuid.get(entity.schedule_uuid)
                if entity.asset_uuid == asset_uuid and schedule is not None:
                    self._write(entity, schedule)
            created.extend(self._build(asset_uuid, schedules))
        self._add(created)

    @callback
    def _handle_midnight(self, _now: Any) -> None:
        """Recompute every projection when the local civil date changes.

        Only reads: the projection takes the new date from the manager.
        """
        by_uuid = {
            schedule["schedule_uuid"]: schedule
            for _asset_uuid, schedules in _schedules_by_asset(self._manager)
            for schedule in schedules
        }
        for entity in list(self._entities.values()):
            schedule = by_uuid.get(entity.schedule_uuid)
            if schedule is not None:
                self._write(entity, schedule)

    @callback
    def _write(
        self, entity: MaintenanceEntity, schedule: MaintenanceScheduleData
    ) -> None:
        entity.refresh_projection(schedule)
        if entity.following:
            entity.async_write_ha_state()
