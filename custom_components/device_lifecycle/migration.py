"""Entity-registry migrations for Device Lifecycle Asset Core."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
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


def _device_can_be_linked(registry: dr.DeviceRegistry, device_id: str) -> bool:
    """Return whether Home Assistant will attach an entity to this device ID.

    Device Lifecycle stores Device Registry IDs it does not own, and another
    integration can remove the device at any time. Home Assistant refuses to
    attach an entity to a device ID that is not in the registry, including a
    pre-migration composite ID that `async_get` still resolves to a
    read-only stand-in. This asks the same question Home Assistant's own
    check asks: exact ID membership on 2026.8, where `devices` is a device-ID
    mapping, and `async_get` without composites on 2026.9+, which deprecates
    that mapping use.
    """
    devices = registry.devices
    if isinstance(devices, Mapping):
        return device_id in devices
    return registry.async_get(device_id, include_composite_devices=False) is not None


def _migrate_or_relink_entity(
    registry: er.EntityRegistry,
    *,
    old_unique_id: str,
    new_unique_id: str,
    config_subentry_id: str,
    device_id: str | None,
) -> None:
    """Migrate an existing entity without changing its entity_id/history.

    A `device_id` of None leaves the entity's device link as it is: the
    referenced Home Assistant device is gone, and the Asset Device keeps it.
    """
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
    if device_id is not None and registry_entry.device_id != device_id:
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
    device_registry = dr.async_get(hass)

    def _linkable(device_id: str, asset_id: str, source: str) -> str | None:
        """Return the device ID to link, or None for a device that is gone.

        A missing device is an unresolved reference, not a setup failure.
        The stored reference is kept exactly as it is for the person to
        repair, and nothing else about the Asset changes.
        """
        if _device_can_be_linked(device_registry, device_id):
            return device_id
        _LOGGER.warning(
            "The Home Assistant device that %s lists for Asset %s is no longer "
            "in the Device Registry; keeping the stored reference and "
            "continuing setup",
            source,
            asset_id,
        )
        return None

    for subentry in entry.subentries.values():
        if subentry.subentry_type == SUBENTRY_TYPE_PURCHASE:
            for device_id_value in subentry.data.get(CONF_DEVICE_IDS, []):
                device_id = str(device_id_value)
                asset = manager.asset_for_primary_device_id(device_id)
                if asset is None:
                    continue

                _migrate_or_relink_entity(
                    registry,
                    old_unique_id=(
                        f"{subentry.subentry_id}_{device_id}_lifecycle"
                    ),
                    new_unique_id=lifecycle_unique_id(asset["asset_uuid"]),
                    config_subentry_id=subentry.subentry_id,
                    device_id=_linkable(
                        device_id,
                        asset["asset_id"],
                        "a Purchase configuration",
                    ),
                )

        elif subentry.subentry_type == SUBENTRY_TYPE_RUNTIME:
            device_id = str(subentry.data.get(CONF_DEVICE_ID) or "")
            if not device_id:
                continue

            asset = manager.asset(str(subentry.data.get(CONF_ASSET_UUID) or ""))
            primary_asset = manager.asset_for_primary_device_id(device_id)
            if (
                asset is None
                or primary_asset is None
                or asset["asset_uuid"] != primary_asset["asset_uuid"]
            ):
                asset = primary_asset
            if asset is None:
                continue

            _migrate_or_relink_entity(
                registry,
                old_unique_id=(
                    f"{subentry.subentry_id}_{device_id}_runtime_hours"
                ),
                new_unique_id=runtime_unique_id(asset["asset_uuid"]),
                config_subentry_id=subentry.subentry_id,
                device_id=_linkable(
                    device_id,
                    asset["asset_id"],
                    "a Runtime configuration",
                ),
            )
