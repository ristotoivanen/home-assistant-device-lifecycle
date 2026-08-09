"""Entity-registry migrations for Device Lifecycle Asset Core."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_ASSET_UUID,
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
    DOMAIN,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
)
from .storage import AssetStoreError, AssetStoreManager

_LOGGER = logging.getLogger(__name__)


def lifecycle_unique_id(asset_uuid: str) -> str:
    """Return the stable Asset-owned lifecycle entity unique ID."""
    return f"{asset_uuid}_lifecycle"


def runtime_unique_id(asset_uuid: str) -> str:
    """Return the stable Asset-owned runtime entity unique ID."""
    return f"{asset_uuid}_runtime_hours"


def _migrate_or_relink_entity(
    registry: er.EntityRegistry,
    *,
    old_unique_id: str,
    new_unique_id: str,
    config_subentry_id: str,
    device_id: str,
) -> None:
    """Migrate an existing entity without changing its entity_id/history."""
    new_entity_id = registry.async_get_entity_id(
        Platform.SENSOR,
        DOMAIN,
        new_unique_id,
    )
    old_entity_id = registry.async_get_entity_id(
        Platform.SENSOR,
        DOMAIN,
        old_unique_id,
    )

    if (
        new_entity_id is not None
        and old_entity_id is not None
        and new_entity_id != old_entity_id
    ):
        raise AssetStoreError(
            f"Both legacy and Asset Core entities exist for {new_unique_id}; "
            "refusing an ambiguous automatic merge"
        )

    entity_id = new_entity_id or old_entity_id
    if entity_id is None:
        return

    registry_entry = registry.async_get(entity_id)
    if registry_entry is None:
        return

    update: dict[str, str] = {}
    if new_entity_id is None:
        update["new_unique_id"] = new_unique_id
    if registry_entry.config_subentry_id != config_subentry_id:
        update["config_subentry_id"] = config_subentry_id
    if registry_entry.device_id != device_id:
        update["device_id"] = device_id

    if not update:
        return

    registry.async_update_entity(entity_id, **update)
    _LOGGER.info(
        "Migrated Device Lifecycle entity %s to Asset Core unique id %s",
        entity_id,
        new_unique_id,
    )


async def async_migrate_entity_registry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    manager: AssetStoreManager,
) -> None:
    """Move 0.4.x entity unique IDs to stable Asset UUID based IDs.

    Home Assistant restore data is keyed by entity_id, so keeping entity_id intact
    also keeps existing Runtime hours restore data available during this migration.
    """
    registry = er.async_get(hass)

    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_TYPE_PURCHASE:
            for device_id_value in subentry.data.get(CONF_DEVICE_IDS, []):
                device_id = str(device_id_value)
                asset = manager.asset_for_device_id(device_id)
                if asset is None:
                    continue

                _migrate_or_relink_entity(
                    registry,
                    old_unique_id=(
                        f"{subentry.subentry_id}_{device_id}_lifecycle"
                    ),
                    new_unique_id=lifecycle_unique_id(asset["asset_uuid"]),
                    config_subentry_id=subentry.subentry_id,
                    device_id=device_id,
                )

        elif subentry.subentry_type == SUBENTRY_TYPE_RUNTIME:
            device_id = str(subentry.data.get(CONF_DEVICE_ID) or "")
            if not device_id:
                continue

            asset = manager.asset(str(subentry.data.get(CONF_ASSET_UUID) or ""))
            if asset is None:
                asset = manager.asset_for_device_id(device_id)
            if asset is None:
                continue

            _migrate_or_relink_entity(
                registry,
                old_unique_id=(
                    f"{subentry.subentry_id}_{device_id}_runtime_hours"
                ),
                new_unique_id=runtime_unique_id(asset["asset_uuid"]),
                config_subentry_id=subentry.subentry_id,
                device_id=device_id,
            )
