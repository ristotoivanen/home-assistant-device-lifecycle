"""Device Lifecycle integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import CONFIG_ENTRY_VERSION
from .exposure import async_reconcile_exposure_registry
from .migration import async_migrate_entity_registry
from .storage import AssetStoreManager

PLATFORMS = [Platform.SENSOR]
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the single Device Lifecycle parent entry."""
    manager = AssetStoreManager(hass)
    await manager.async_setup()
    await manager.async_reconcile_entry(entry)

    # 0.5.0 changes entity ownership from purchase/device-derived unique IDs to
    # immutable Asset UUIDs while preserving the existing entity_id and history.
    await async_migrate_entity_registry(hass, entry, manager)

    # 0.6.0 then performs a complete read-only exposure preflight before it
    # creates deterministic Asset Devices or moves any existing entities.
    await async_reconcile_exposure_registry(hass, entry, manager)

    entry.runtime_data = manager

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Adding, removing or editing a config subentry updates the parent ConfigEntry.
    # Reload after the initial reconciliation so the store sees each change first.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Device Lifecycle."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload after a purchase or runtime subentry changes."""
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate the parent ConfigEntry schema to Asset Core generation four."""
    _LOGGER.debug(
        "Migrating Device Lifecycle config entry from version %s to %s",
        entry.version,
        CONFIG_ENTRY_VERSION,
    )

    if entry.version > CONFIG_ENTRY_VERSION:
        _LOGGER.error(
            "Cannot downgrade Device Lifecycle config entry version %s to %s",
            entry.version,
            CONFIG_ENTRY_VERSION,
        )
        return False

    if entry.version < CONFIG_ENTRY_VERSION:
        # Purchase/runtime payload normalization is intentionally handled by the
        # versioned AssetStoreManager during setup. The original subentry data is
        # left intact until the new private store has been written successfully.
        hass.config_entries.async_update_entry(
            entry,
            version=CONFIG_ENTRY_VERSION,
        )

    _LOGGER.info(
        "Device Lifecycle config entry migration to version %s successful",
        CONFIG_ENTRY_VERSION,
    )
    return True
