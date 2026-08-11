"""Exposure preflight collision and rollback quality gates."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_DEVICE_ID,
    DOMAIN,
    SUBENTRY_TYPE_RUNTIME,
)
from custom_components.device_lifecycle.exposure import (
    AssetDevicePlan,
    EntityRegistryUpdatePlan,
    ExposureMigrationPlan,
    _discover_attempt_devices,
    _primary_device_id,
    _rollback_exposure_registry,
    _runtime_subentries_by_asset,
    asset_device_entry,
    asset_device_identifier,
    build_exposure_migration_plan,
)
from custom_components.device_lifecycle.migration import (
    lifecycle_unique_id,
    runtime_unique_id,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import AssetStoreError

from .conftest import ASSET_UUID
from .test_exposure import _entry


def test_primary_reference_with_empty_device_id_is_not_guessed(
    asset_store_data: AssetStoreData,
) -> None:
    """An empty stored primary reference remains absent instead of becoming identity."""
    asset = deepcopy(asset_store_data["assets"][ASSET_UUID])
    asset["ha_device_refs"] = [{"device_id": "", "role": "primary"}]
    assert _primary_device_id(asset) is None


def test_runtime_subentry_preflight_rejects_missing_wrong_and_duplicate_assets(
    asset_store_data: AssetStoreData,
) -> None:
    """Runtime ownership must resolve to one Asset and its exact primary device."""
    asset = deepcopy(asset_store_data["assets"][ASSET_UUID])
    assets = {ASSET_UUID: asset}
    incomplete = SimpleNamespace(
        subentry_id="incomplete",
        subentry_type=SUBENTRY_TYPE_RUNTIME,
        data={CONF_ASSET_UUID: "", CONF_DEVICE_ID: ""},
    )
    assert _runtime_subentries_by_asset(
        SimpleNamespace(subentries={"incomplete": incomplete}), assets
    ) == {}

    missing = SimpleNamespace(
        subentry_id="missing",
        subentry_type=SUBENTRY_TYPE_RUNTIME,
        data={
            CONF_ASSET_UUID: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            CONF_DEVICE_ID: "device",
        },
    )
    with pytest.raises(AssetStoreError, match="missing Asset"):
        _runtime_subentries_by_asset(
            SimpleNamespace(subentries={"missing": missing}), assets
        )

    wrong = SimpleNamespace(
        subentry_id="wrong",
        subentry_type=SUBENTRY_TYPE_RUNTIME,
        data={CONF_ASSET_UUID: ASSET_UUID, CONF_DEVICE_ID: "wrong-device"},
    )
    with pytest.raises(AssetStoreError, match="exact primary"):
        _runtime_subentries_by_asset(
            SimpleNamespace(subentries={"wrong": wrong}), assets
        )

    first = SimpleNamespace(
        subentry_id="a",
        subentry_type=SUBENTRY_TYPE_RUNTIME,
        data={CONF_ASSET_UUID: ASSET_UUID, CONF_DEVICE_ID: "existing-ha-device-id"},
    )
    second = SimpleNamespace(
        subentry_id="b",
        subentry_type=SUBENTRY_TYPE_RUNTIME,
        data=dict(first.data),
    )
    with pytest.raises(AssetStoreError, match="multiple Runtime subentries"):
        _runtime_subentries_by_asset(
            SimpleNamespace(subentries={"a": first, "b": second}), assets
        )


def test_duplicate_asset_snapshots_fail_exposure_preflight(
    asset_store_data: AssetStoreData,
) -> None:
    """A duplicate canonical UUID is rejected before any registry mutation."""
    asset = asset_store_data["assets"][ASSET_UUID]
    with pytest.raises(AssetStoreError, match="duplicate canonical UUIDs"):
        build_exposure_migration_plan(
            entry=SimpleNamespace(entry_id="entry", subentries={}),
            assets=[asset, deepcopy(asset)],
            device_registry=SimpleNamespace(devices={}),
            entity_registry=SimpleNamespace(entities={}),
        )


async def test_foreign_asset_device_projection_fails_closed(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """The deterministic Asset identifier cannot be adopted from a subentry device."""
    entry = _entry(hass, device_id="external-device")
    subentry_id = next(iter(entry.subentries))
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=subentry_id,
        identifiers={asset_device_identifier(ASSET_UUID)},
        name="Ambiguous Asset Device",
    )

    with pytest.raises(AssetStoreError, match="externally owned"):
        asset_device_entry(
            device_registry,
            config_entry_id=entry.entry_id,
            asset_uuid=ASSET_UUID,
        )


@pytest.mark.parametrize("unique_id", ["orphan_lifecycle", "orphan_runtime_hours"])
async def test_orphan_legacy_entity_ids_fail_preflight(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
    unique_id: str,
) -> None:
    """Noncanonical lifecycle/runtime IDs are never remapped from metadata."""
    entry = _entry(hass)
    entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        unique_id,
        config_entry=entry,
    )
    with pytest.raises(AssetStoreError, match="canonical Asset"):
        build_exposure_migration_plan(
            entry=entry,
            assets=[asset_store_data["assets"][ASSET_UUID]],
            device_registry=device_registry,
            entity_registry=entity_registry,
        )


async def test_stale_canonical_runtime_entity_is_left_for_platform_cleanup(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """No Runtime subentry means preflight must not infer new subentry ownership."""
    entry = _entry(hass)
    entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        runtime_unique_id(ASSET_UUID),
        config_entry=entry,
    )
    plan = build_exposure_migration_plan(
        entry=entry,
        assets=[asset_store_data["assets"][ASSET_UUID]],
        device_registry=device_registry,
        entity_registry=entity_registry,
    )
    assert not [update for update in plan.entity_updates if update.kind == "runtime"]


def test_entity_disappearing_between_lookup_and_read_fails_preflight(
    asset_store_data: AssetStoreData,
) -> None:
    """A registry race after unique-ID resolution aborts the complete plan."""
    registry = Mock()
    registry.entities = {}
    registry.async_get_entity_id.side_effect = lambda platform, domain, unique_id: (
        "sensor.disappeared"
        if unique_id == lifecycle_unique_id(ASSET_UUID)
        else None
    )
    registry.async_get.return_value = None
    with pytest.raises(AssetStoreError, match="disappeared during exposure preflight"):
        build_exposure_migration_plan(
            entry=SimpleNamespace(entry_id="entry", subentries={}),
            assets=[asset_store_data["assets"][ASSET_UUID]],
            device_registry=SimpleNamespace(devices={}),
            entity_registry=registry,
        )


async def test_attempt_discovery_and_rollback_report_identity_races(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Compensating rollback records disappeared/changed rows and exact new devices."""
    entry = _entry(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={asset_device_identifier(ASSET_UUID)},
        name="Attempted Asset",
    )
    plan = ExposureMigrationPlan(
        config_entry_id=entry.entry_id,
        devices=(
            AssetDevicePlan(
                asset_uuid=ASSET_UUID,
                asset_id="DL0007",
                name="Attempted Asset",
                manufacturer=None,
                model=None,
                model_id=None,
                serial_number=None,
                sw_version=None,
                hw_version=None,
                existing_device_id=None,
            ),
        ),
        entity_updates=(),
    )
    created: list[str] = []
    _discover_attempt_devices(
        plan=plan,
        registry=device_registry,
        attempted_missing_assets={ASSET_UUID},
        created_device_ids=created,
    )
    assert created == [device.id]

    updates = [
        EntityRegistryUpdatePlan(
            kind="lifecycle",
            asset_uuid=ASSET_UUID,
            entity_id="sensor.disappeared",
            unique_id=lifecycle_unique_id(ASSET_UUID),
            original_device_id=None,
            original_config_subentry_id=None,
            desired_config_subentry_id=None,
        ),
        EntityRegistryUpdatePlan(
            kind="runtime",
            asset_uuid=ASSET_UUID,
            entity_id="sensor.changed",
            unique_id=runtime_unique_id(ASSET_UUID),
            original_device_id=None,
            original_config_subentry_id=None,
            desired_config_subentry_id=None,
        ),
    ]
    entity_registry = Mock()
    entity_registry.async_get.side_effect = [
        SimpleNamespace(unique_id="different"),
        None,
    ]
    failures = _rollback_exposure_registry(
        plan=plan,
        device_registry=device_registry,
        entity_registry=entity_registry,
        attempted_updates=updates,
        created_device_ids=[],
    )
    assert failures == [
        "entity sensor.changed changed unique ID during rollback",
        "entity sensor.disappeared disappeared during rollback",
    ]

    assert _rollback_exposure_registry(
        plan=plan,
        device_registry=device_registry,
        entity_registry=Mock(),
        attempted_updates=[],
        created_device_ids=["already-disappeared"],
    ) == []
