"""Legacy entity migration ambiguity and stale-reference quality gates."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
    DOMAIN,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
)
from custom_components.device_lifecycle.migration import (
    _migrate_or_relink_entity,
    async_migrate_entity_registry,
)
from custom_components.device_lifecycle.storage import AssetStoreError


def test_legacy_and_canonical_entity_collision_fails_closed() -> None:
    """Two registry rows are never guessed into one Recorder identity."""
    registry = Mock()
    registry.async_get_entity_id.side_effect = ["sensor.new", "sensor.old"]

    with pytest.raises(AssetStoreError, match="ambiguous automatic merge"):
        _migrate_or_relink_entity(
            registry,
            old_unique_id="old",
            new_unique_id="new",
            config_subentry_id="purchase",
            device_id="device",
        )


def test_disappeared_and_already_converged_registry_rows_need_no_update() -> None:
    """Stale lookups and already-canonical ownership are deterministic no-ops."""
    disappeared = Mock()
    disappeared.async_get_entity_id.side_effect = ["sensor.new", None]
    disappeared.async_get.return_value = None
    _migrate_or_relink_entity(
        disappeared,
        old_unique_id="old",
        new_unique_id="new",
        config_subentry_id="purchase",
        device_id="device",
    )
    disappeared.async_update_entity.assert_not_called()

    converged = Mock()
    converged.async_get_entity_id.side_effect = ["sensor.new", None]
    converged.async_get.return_value = SimpleNamespace(
        config_subentry_id="purchase", device_id="device"
    )
    _migrate_or_relink_entity(
        converged,
        old_unique_id="old",
        new_unique_id="new",
        config_subentry_id="purchase",
        device_id="device",
    )
    converged.async_update_entity.assert_not_called()


async def test_stale_purchase_and_runtime_subentries_are_not_guessed(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
) -> None:
    """Missing device/Asset relationships are skipped without fabricating identity."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        subentries_data=(
            {
                "data": {CONF_DEVICE_IDS: ["missing-purchase-device"]},
                "subentry_type": SUBENTRY_TYPE_PURCHASE,
                "title": "Purchase",
                "unique_id": None,
            },
            {
                "data": {CONF_DEVICE_ID: "", CONF_ASSET_UUID: ""},
                "subentry_type": SUBENTRY_TYPE_RUNTIME,
                "title": "Empty runtime",
                "unique_id": None,
            },
            {
                "data": {
                    CONF_DEVICE_ID: "missing-runtime-device",
                    CONF_ASSET_UUID: "missing-asset",
                },
                "subentry_type": SUBENTRY_TYPE_RUNTIME,
                "title": "Stale runtime",
                "unique_id": None,
            },
        ),
    )
    entry.add_to_hass(hass)
    manager = Mock()
    manager.asset_for_primary_device_id.return_value = None
    manager.asset.return_value = None

    await async_migrate_entity_registry(hass, entry, manager)

    assert not entity_registry.entities
