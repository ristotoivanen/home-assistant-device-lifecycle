"""Integration setup and ConfigEntry migration quality gates."""

from __future__ import annotations

from unittest.mock import Mock, patch

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle import (
    _async_update_listener,
    async_migrate_entry,
)
from custom_components.device_lifecycle.const import CONFIG_ENTRY_VERSION, DOMAIN


@pytest.mark.parametrize(
    ("version", "expected", "updated"),
    [
        (CONFIG_ENTRY_VERSION + 1, False, False),
        (CONFIG_ENTRY_VERSION, True, False),
        (CONFIG_ENTRY_VERSION - 1, True, True),
    ],
)
async def test_config_entry_migration_is_fail_closed_and_keeps_version_four(
    hass: HomeAssistant,
    version: int,
    expected: bool,
    updated: bool,
) -> None:
    """Future versions fail while current/legacy entries preserve ConfigEntry v4."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, version=version)
    entry.add_to_hass(hass)

    with patch.object(hass.config_entries, "async_update_entry") as update:
        assert await async_migrate_entry(hass, entry) is expected

    assert update.called is updated
    if updated:
        assert update.call_args.kwargs["version"] == CONFIG_ENTRY_VERSION == 4


async def test_update_listener_schedules_exactly_one_parent_reload(
    hass: HomeAssistant,
) -> None:
    """Purchase/runtime ConfigEntry changes retain the single listener boundary."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    with patch.object(
        hass.config_entries, "async_schedule_reload", Mock()
    ) as schedule:
        await _async_update_listener(hass, entry)
    schedule.assert_called_once_with(entry.entry_id)
