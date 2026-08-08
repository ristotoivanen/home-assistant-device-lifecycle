"""Device Lifecycle integration."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the single Device Lifecycle parent entry."""
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Adding, removing or editing a config subentry updates the parent
    # ConfigEntry. Reload the integration so the entity platform sees the new
    # purchase immediately.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload Device Lifecycle."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload after a purchase subentry changes."""
    hass.config_entries.async_schedule_reload(entry.entry_id)
