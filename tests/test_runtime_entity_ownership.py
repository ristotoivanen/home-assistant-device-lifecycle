"""Parent-owned Runtime Entity Registry identity (WP7).

The Runtime ConfigSubentry owns only the Runtime tracking configuration and
whether a writer exists. The Runtime entity's Entity Registry identity is
Asset/parent-owned (``config_subentry_id is None``), so removing Runtime
tracking never deletes it and adding tracking again reuses it.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.device_lifecycle import exposure, sensor
from custom_components.device_lifecycle.const import (
    CONF_DEVICE_ID,
    DOMAIN,
    SUBENTRY_TYPE_RUNTIME,
)
from custom_components.device_lifecycle.exposure import (
    asset_device_entry,
    async_reconcile_exposure_registry,
)
from custom_components.device_lifecycle.migration import runtime_unique_id
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    AssetStoreError,
)

from .conftest import ASSET_UUID, SOURCE_ENTITY_ID
from .test_exposure import _entry, _external_device, _manager
from .test_exposure_options_reload import _setup_runtime_entry, _verified_store_readback

POWER = {"device_class": SensorDeviceClass.POWER, "unit_of_measurement": "W"}


def _runtime_subentry_id(entry: Any) -> str:
    return next(
        item.subentry_id
        for item in entry.subentries.values()
        if item.subentry_type == SUBENTRY_TYPE_RUNTIME
    )


def _subentry_owned_runtime(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> tuple[Any, Any, er.RegistryEntry]:
    """An installed pre-WP7 Runtime entity owned by its Runtime subentry."""
    external = _external_device(hass, device_registry)
    asset_store_data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": external.id, "role": "primary"}
    ]
    asset_store_data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "1234"
    manager = _manager(hass, asset_store_data)
    entry = _entry(hass, device_id=external.id)
    runtime = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        runtime_unique_id(ASSET_UUID),
        suggested_object_id="custom_runtime",
        config_entry=entry,
        config_subentry_id=_runtime_subentry_id(entry),
        device_id=external.id,
    )
    return manager, entry, runtime


# Migration of an installed entity


async def test_installed_runtime_entity_moves_to_parent_in_place(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    manager, entry, runtime = _subentry_owned_runtime(
        hass, device_registry, entity_registry, asset_store_data
    )
    entity_registry.async_update_entity(
        runtime.entity_id,
        name="My runtime",
        icon="mdi:timer",
        hidden_by=er.RegistryEntryHider.USER,
        area_id="workshop",
    )
    before = entity_registry.async_get(runtime.entity_id)
    store_before = deepcopy(manager._data)

    await async_reconcile_exposure_registry(hass, entry, manager)

    after = entity_registry.async_get(runtime.entity_id)
    asset_device = asset_device_entry(
        device_registry, config_entry_id=entry.entry_id, asset_uuid=ASSET_UUID
    )
    assert asset_device is not None
    assert after.id == before.id  # the same registry entry, never recreated
    assert after.entity_id == before.entity_id
    assert after.unique_id == runtime_unique_id(ASSET_UUID)
    assert after.platform == DOMAIN
    assert after.config_entry_id == entry.entry_id
    assert after.config_subentry_id is None
    assert after.device_id == asset_device.id
    assert after.name == "My runtime"
    assert after.icon == "mdi:timer"
    assert after.hidden_by is er.RegistryEntryHider.USER
    assert after.area_id == "workshop"
    assert after.disabled_by is None
    # The Runtime subentry still exists and still owns the tracking config.
    assert _runtime_subentry_id(entry) in entry.subentries
    # Registry placement only: canonical Store and Runtime are untouched.
    assert manager._data == store_before
    assert manager.runtime_total_seconds(ASSET_UUID) == 1234


async def test_user_disabled_runtime_entity_stays_user_disabled(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    manager, entry, runtime = _subentry_owned_runtime(
        hass, device_registry, entity_registry, asset_store_data
    )
    entity_registry.async_update_entity(
        runtime.entity_id,
        name="Disabled runtime",
        disabled_by=er.RegistryEntryDisabler.USER,
    )

    await async_reconcile_exposure_registry(hass, entry, manager)
    await async_reconcile_exposure_registry(hass, entry, manager)

    after = entity_registry.async_get(runtime.entity_id)
    assert after.config_subentry_id is None
    assert after.disabled_by is er.RegistryEntryDisabler.USER
    assert after.name == "Disabled runtime"


async def test_second_reconciliation_writes_nothing(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    manager, entry, runtime = _subentry_owned_runtime(
        hass, device_registry, entity_registry, asset_store_data
    )
    await async_reconcile_exposure_registry(hass, entry, manager)

    with patch.object(
        entity_registry,
        "async_update_entity",
        wraps=entity_registry.async_update_entity,
    ) as update:
        await async_reconcile_exposure_registry(hass, entry, manager)

    assert update.call_count == 0
    assert entity_registry.async_get(runtime.entity_id).config_subentry_id is None


async def test_failed_reconciliation_restores_subentry_placement(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """The move takes part in the existing compensating rollback."""
    manager, entry, runtime = _subentry_owned_runtime(
        hass, device_registry, entity_registry, asset_store_data
    )
    original = entity_registry.async_get(runtime.entity_id)
    real_update = entity_registry.async_update_entity
    failed = False

    def _fail_after_runtime(entity_id: str, **values: Any) -> Any:
        nonlocal failed
        result = real_update(entity_id, **values)
        if entity_id == runtime.entity_id and not failed:
            failed = True
            raise RuntimeError("injected failure after the Runtime move")
        return result

    with (
        patch.object(entity_registry, "async_update_entity", _fail_after_runtime),
        pytest.raises(AssetStoreError, match="rolled back"),
    ):
        await async_reconcile_exposure_registry(hass, entry, manager)

    restored = entity_registry.async_get(runtime.entity_id)
    assert restored.config_subentry_id == original.config_subentry_id
    assert restored.config_subentry_id == _runtime_subentry_id(entry)
    assert restored.device_id == original.device_id
    assert restored.entity_id == original.entity_id


async def test_reverse_placement_remains_possible_for_code_rollback(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """A 0.7.7 build plans the Runtime entity back into its Runtime subentry.

    The 0.7.7 plan differs only in the Runtime entity's desired
    ``config_subentry_id``; applying that plan through the same exposure
    reconciliation moves a parent-owned entity back in place, so reverting
    WP7 before Store 4.1 is an ordinary code rollback.
    """
    manager, entry, runtime = _subentry_owned_runtime(
        hass, device_registry, entity_registry, asset_store_data
    )
    await async_reconcile_exposure_registry(hass, entry, manager)
    parent_owned = entity_registry.async_get(runtime.entity_id)
    assert parent_owned.config_subentry_id is None

    runtime_subentry_id = _runtime_subentry_id(entry)
    real_plan = exposure.build_exposure_migration_plan

    def _plan_0_7_7(**kwargs: Any) -> exposure.ExposureMigrationPlan:
        plan = real_plan(**kwargs)
        return replace(
            plan,
            entity_updates=tuple(
                replace(update, desired_config_subentry_id=runtime_subentry_id)
                if update.kind == "runtime"
                else update
                for update in plan.entity_updates
            ),
        )

    with patch.object(exposure, "build_exposure_migration_plan", _plan_0_7_7):
        await async_reconcile_exposure_registry(hass, entry, manager)

    reverted = entity_registry.async_get(runtime.entity_id)
    assert reverted.id == parent_owned.id
    assert reverted.entity_id == parent_owned.entity_id
    assert reverted.unique_id == parent_owned.unique_id
    assert reverted.config_subentry_id == runtime_subentry_id
    assert reverted.device_id == parent_owned.device_id


async def test_deleted_subentry_cleanup_keeps_the_parent_owned_runtime(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """Platform cleanup still removes entities of a deleted subentry, not Runtime."""
    manager, entry, runtime = _subentry_owned_runtime(
        hass, device_registry, entity_registry, asset_store_data
    )
    runtime_subentry_id = _runtime_subentry_id(entry)
    stale = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        "stale_subentry_owned_extra",
        config_entry=entry,
        config_subentry_id=runtime_subentry_id,
    )
    await async_reconcile_exposure_registry(hass, entry, manager)
    assert entity_registry.async_get(runtime.entity_id).config_subentry_id is None

    # The platform sets up after the Runtime subentry is gone, but before Home
    # Assistant cleared the entities that the subentry still owned.
    remaining = MappingProxyType(
        {
            key: value
            for key, value in entry.subentries.items()
            if key != runtime_subentry_id
        }
    )
    platform_entry = SimpleNamespace(
        entry_id=entry.entry_id, subentries=remaining, runtime_data=manager
    )
    added: list[Any] = []
    await sensor.async_setup_entry(hass, platform_entry, added.extend)

    assert entity_registry.async_get(stale.entity_id) is None
    kept = entity_registry.async_get(runtime.entity_id)
    assert kept is not None
    assert kept.unique_id == runtime_unique_id(ASSET_UUID)
    assert kept.config_subentry_id is None
    # Without Runtime tracking no Runtime entity object or writer exists.
    assert not [
        entity
        for entity in added
        if isinstance(entity, sensor.DeviceRuntimeHoursSensor)
    ]
    assert manager._runtime_writers == {}


# Remove and re-add Runtime tracking in a loaded entry


@pytest.mark.real_reload
async def test_remove_and_readd_tracking_keeps_one_identity(
    hass: HomeAssistant,
    hass_storage: dict,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
    runtime_subentry_data: dict,
) -> None:
    entry, subentry_id = await _setup_runtime_entry(
        hass,
        hass_storage,
        asset_store_data,
        runtime_subentry_data,
        device_registry,
        source_state=(SOURCE_ENTITY_ID, POWER),
    )
    unique_id = runtime_unique_id(ASSET_UUID)
    entity_id = entity_registry.async_get_entity_id(Platform.SENSOR, DOMAIN, unique_id)
    assert entity_id is not None
    registry_id = entity_registry.async_get(entity_id).id
    assert entity_registry.async_get(entity_id).config_subentry_id is None
    entity_registry.async_update_entity(entity_id, name="Workshop runtime")
    subentry = entry.subentries[subentry_id]
    subentry_data = dict(subentry.data)

    with _verified_store_readback(hass_storage):
        # The source goes idle, so no Runtime accumulates from here on.
        hass.states.async_set(SOURCE_ENTITY_ID, "0", POWER)
        await hass.async_block_till_done()
        hass.config_entries.async_remove_subentry(entry, subentry_id)
        await hass.async_block_till_done()

    manager = entry.runtime_data
    kept = entity_registry.async_get(entity_id)
    assert kept is not None
    assert kept.id == registry_id
    assert kept.unique_id == unique_id
    assert kept.config_subentry_id is None
    assert kept.name == "Workshop runtime"
    assert manager._runtime_writers == {}
    total_without_tracking = manager.runtime_total_seconds(ASSET_UUID)
    assert total_without_tracking is not None

    with _verified_store_readback(hass_storage):
        hass.config_entries.async_add_subentry(
            entry,
            ConfigSubentry(
                data=subentry_data,
                subentry_type=SUBENTRY_TYPE_RUNTIME,
                title=subentry.title,
                unique_id=None,
            ),
        )
        await hass.async_block_till_done()

    manager = entry.runtime_data
    matches = [
        item
        for item in er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        if item.unique_id == unique_id
    ]
    assert len(matches) == 1
    reused = matches[0]
    assert reused.id == registry_id
    assert reused.entity_id == entity_id
    assert reused.config_subentry_id is None
    assert reused.name == "Workshop runtime"
    assert list(manager._runtime_writers) == [ASSET_UUID]
    # Runtime resumes from the canonical total; nothing is inferred for the gap.
    assert manager.runtime_total_seconds(ASSET_UUID) == total_without_tracking
    state = hass.states.get(entity_id)
    assert state is not None
    assert float(state.state) == pytest.approx(
        float(total_without_tracking) / 3600, abs=1e-6
    )
    assert (
        entry.subentries[_runtime_subentry_id(entry)].data[CONF_DEVICE_ID]
        == (subentry_data[CONF_DEVICE_ID])
    )


def test_store_is_4_1() -> None:
    assert (STORAGE_VERSION, STORAGE_MINOR_VERSION) == (4, 1)
