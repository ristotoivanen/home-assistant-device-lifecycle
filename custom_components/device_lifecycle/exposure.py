"""Derived Home Assistant registry projection for Device Lifecycle Assets."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    CONF_ASSET_UUID,
    CONF_DEVICE_ID,
    DOMAIN,
    SUBENTRY_TYPE_RUNTIME,
)
from .migration import lifecycle_unique_id, runtime_unique_id
from .models import AssetData
from .storage import AssetStoreError, AssetStoreManager

_LOGGER = logging.getLogger(__name__)

ExposureEntityKind = Literal[
    "lifecycle",
    "runtime",
    "deployment",
    "installed_date",
    "relationships",
    "asset_id",
    "lifecycle_status",
    "replacement",
]

_ENTITY_KIND_ORDER: dict[ExposureEntityKind, int] = {
    "lifecycle": 0,
    "runtime": 1,
    "deployment": 2,
    "installed_date": 3,
    "relationships": 4,
    "asset_id": 5,
    "lifecycle_status": 6,
    "replacement": 7,
}


def asset_device_identifier(asset_uuid: str) -> tuple[str, str]:
    """Return the only identifier of a derived Asset Device."""
    return (DOMAIN, asset_uuid)


def deployment_unique_id(asset_uuid: str) -> str:
    """Return the stable Asset-owned Deployment entity unique ID."""
    return f"{asset_uuid}_deployment"


def installed_date_unique_id(asset_uuid: str) -> str:
    """Return the stable Asset-owned Installation Date entity unique ID."""
    return f"{asset_uuid}_installed_date"


def relationships_unique_id(asset_uuid: str) -> str:
    """Return the stable Asset-owned Relationships entity unique ID."""
    return f"{asset_uuid}_relationships"


def asset_id_unique_id(asset_uuid: str) -> str:
    """Return the stable Asset-owned Asset ID entity unique ID."""
    return f"{asset_uuid}_asset_id"


def lifecycle_status_unique_id(asset_uuid: str) -> str:
    """Return the stable Asset-owned Lifecycle Status unique ID."""
    return f"{asset_uuid}_lifecycle_status"


def replacement_unique_id(asset_uuid: str) -> str:
    """Return the stable Asset-owned Replacement unique ID."""
    return f"{asset_uuid}_replacement"


@dataclass(frozen=True)
class AssetDevicePlan:
    """One deterministic Device Registry desired state."""

    asset_uuid: str
    asset_id: str
    name: str
    manufacturer: str | None
    model: str | None
    model_id: str | None
    serial_number: str | None
    sw_version: str | None
    hw_version: str | None
    existing_device_id: str | None


@dataclass(frozen=True)
class EntityRegistryUpdatePlan:
    """One existing Entity Registry entry and its desired ownership."""

    kind: ExposureEntityKind
    asset_uuid: str
    entity_id: str
    unique_id: str
    original_device_id: str | None
    original_config_subentry_id: str | None
    desired_config_subentry_id: str | None


@dataclass(frozen=True)
class ExposureMigrationPlan:
    """Complete read-only plan built before exposure registry mutation."""

    config_entry_id: str
    devices: tuple[AssetDevicePlan, ...]
    entity_updates: tuple[EntityRegistryUpdatePlan, ...]


def _matching_asset_devices(
    registry: dr.DeviceRegistry,
    asset_uuid: str,
) -> list[dr.DeviceEntry]:
    """Return every active device carrying one exact canonical identifier."""
    identifier = asset_device_identifier(asset_uuid)
    return [
        device
        for device in registry.devices.values()
        if identifier in device.identifiers
    ]


def asset_device_entry(
    registry: dr.DeviceRegistry,
    *,
    config_entry_id: str,
    asset_uuid: str,
    require_parent: bool = True,
) -> dr.DeviceEntry | None:
    """Resolve an exact, unambiguous Device Lifecycle Asset Device."""
    matches = _matching_asset_devices(registry, asset_uuid)
    if not matches:
        return None
    if len(matches) != 1:
        raise AssetStoreError(
            f"Asset {asset_uuid} has {len(matches)} Device Registry devices with "
            "its canonical identifier"
        )

    device = matches[0]
    expected_identifier = {asset_device_identifier(asset_uuid)}
    if (
        device.config_entry_id != config_entry_id
        or device.identifiers != expected_identifier
        or device.connections
        or (require_parent and device.config_subentry_id is not None)
    ):
        raise AssetStoreError(
            f"Asset {asset_uuid} has an ambiguous or externally owned Device "
            "Registry identity"
        )
    return device


def _primary_device_id(asset: AssetData) -> str | None:
    """Return the exact stored primary relationship without fallback."""
    for reference in asset.get("ha_device_refs", []):
        if reference.get("role") == "primary":
            return str(reference.get("device_id") or "") or None
    return None


def _runtime_subentries_by_asset(
    entry: ConfigEntry,
    assets_by_uuid: dict[str, AssetData],
) -> dict[str, str]:
    """Resolve active Runtime ownership without using related relationships."""
    result: dict[str, str] = {}
    for subentry in sorted(
        (
            item
            for item in entry.subentries.values()
            if item.subentry_type == SUBENTRY_TYPE_RUNTIME
        ),
        key=lambda item: item.subentry_id,
    ):
        asset_uuid = str(subentry.data.get(CONF_ASSET_UUID) or "")
        device_id = str(subentry.data.get(CONF_DEVICE_ID) or "")
        if not asset_uuid or not device_id:
            continue
        asset = assets_by_uuid.get(asset_uuid)
        if asset is None:
            raise AssetStoreError(
                f"Runtime subentry {subentry.subentry_id} references missing Asset "
                f"{asset_uuid}"
            )
        if _primary_device_id(asset) != device_id:
            raise AssetStoreError(
                f"Runtime subentry {subentry.subentry_id} does not reference the "
                f"exact primary device of Asset {asset_uuid}"
            )
        if asset_uuid in result:
            raise AssetStoreError(
                f"Asset {asset_uuid} is referenced by multiple Runtime subentries"
            )
        result[asset_uuid] = subentry.subentry_id
    return result


def _validate_owned_entity(
    registry_entry: er.RegistryEntry,
    *,
    entry: ConfigEntry,
    unique_id: str,
) -> None:
    """Fail closed rather than taking over an ambiguous registry identity."""
    if (
        registry_entry.platform != DOMAIN
        or registry_entry.config_entry_id != entry.entry_id
        or registry_entry.unique_id != unique_id
    ):
        raise AssetStoreError(
            f"Entity Registry identity {unique_id} is not unambiguously owned by "
            "this Device Lifecycle config entry"
        )


def build_exposure_migration_plan(
    *,
    entry: ConfigEntry,
    assets: list[AssetData],
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> ExposureMigrationPlan:
    """Build and validate the complete 0.6 exposure plan without mutation."""
    assets_by_uuid = {asset["asset_uuid"]: asset for asset in assets}
    if len(assets_by_uuid) != len(assets):
        raise AssetStoreError("Asset snapshots contain duplicate canonical UUIDs")

    device_plans: list[AssetDevicePlan] = []
    for asset_uuid in sorted(assets_by_uuid):
        asset = assets_by_uuid[asset_uuid]
        device = asset_device_entry(
            device_registry,
            config_entry_id=entry.entry_id,
            asset_uuid=asset_uuid,
            require_parent=False,
        )
        canonical_name = str(asset.get("name") or "").strip()
        device_plans.append(
            AssetDevicePlan(
                asset_uuid=asset_uuid,
                asset_id=asset["asset_id"],
                name=canonical_name or asset["asset_id"],
                manufacturer=asset.get("manufacturer"),
                model=asset.get("model"),
                model_id=asset.get("model_id"),
                serial_number=asset.get("serial_number"),
                sw_version=asset.get("sw_version"),
                hw_version=asset.get("hw_version"),
                existing_device_id=device.id if device is not None else None,
            )
        )

    runtime_subentries = _runtime_subentries_by_asset(entry, assets_by_uuid)
    unique_ids_by_kind: dict[ExposureEntityKind, dict[str, str]] = {
        "lifecycle": {
            lifecycle_unique_id(asset_uuid): asset_uuid for asset_uuid in assets_by_uuid
        },
        "runtime": {
            runtime_unique_id(asset_uuid): asset_uuid for asset_uuid in assets_by_uuid
        },
        "deployment": {
            deployment_unique_id(asset_uuid): asset_uuid
            for asset_uuid in assets_by_uuid
        },
        "installed_date": {
            installed_date_unique_id(asset_uuid): asset_uuid
            for asset_uuid in assets_by_uuid
        },
        "relationships": {
            relationships_unique_id(asset_uuid): asset_uuid
            for asset_uuid in assets_by_uuid
        },
        "asset_id": {
            asset_id_unique_id(asset_uuid): asset_uuid for asset_uuid in assets_by_uuid
        },
        "lifecycle_status": {
            lifecycle_status_unique_id(asset_uuid): asset_uuid
            for asset_uuid in assets_by_uuid
        },
        "replacement": {
            replacement_unique_id(asset_uuid): asset_uuid
            for asset_uuid in assets_by_uuid
        },
    }

    # Any current-entry lifecycle/runtime entry must already have converged to a
    # canonical Asset UUID in the logically separate legacy migration.
    canonical_lifecycle_ids = unique_ids_by_kind["lifecycle"]
    canonical_runtime_ids = unique_ids_by_kind["runtime"]
    for registry_entry in entity_registry.entities.values():
        if (
            registry_entry.platform != DOMAIN
            or registry_entry.config_entry_id != entry.entry_id
        ):
            continue
        if registry_entry.unique_id.endswith("_lifecycle"):
            if registry_entry.unique_id not in canonical_lifecycle_ids:
                raise AssetStoreError(
                    f"Lifecycle entity {registry_entry.entity_id} does not resolve "
                    "to exactly one canonical Asset"
                )
        elif registry_entry.unique_id.endswith("_runtime_hours"):
            if registry_entry.unique_id not in canonical_runtime_ids:
                raise AssetStoreError(
                    f"Runtime entity {registry_entry.entity_id} does not resolve "
                    "to exactly one canonical Asset"
                )

    update_plans: list[EntityRegistryUpdatePlan] = []
    for kind in (
        "lifecycle",
        "runtime",
        "deployment",
        "installed_date",
        "relationships",
        "asset_id",
        "lifecycle_status",
        "replacement",
    ):
        for unique_id, asset_uuid in sorted(unique_ids_by_kind[kind].items()):
            entity_id = entity_registry.async_get_entity_id(
                Platform.SENSOR,
                DOMAIN,
                unique_id,
            )
            if entity_id is None:
                continue
            registry_entry = entity_registry.async_get(entity_id)
            if registry_entry is None:
                raise AssetStoreError(
                    f"Entity Registry identity {unique_id} disappeared during "
                    "exposure preflight"
                )
            _validate_owned_entity(
                registry_entry,
                entry=entry,
                unique_id=unique_id,
            )

            desired_subentry_id = (
                runtime_subentries.get(asset_uuid) if kind == "runtime" else None
            )
            # A stale Runtime entity with no active Runtime subentry is left for
            # the existing platform cleanup; there is no subentry to infer.
            if kind == "runtime" and desired_subentry_id is None:
                continue

            update_plans.append(
                EntityRegistryUpdatePlan(
                    kind=kind,
                    asset_uuid=asset_uuid,
                    entity_id=registry_entry.entity_id,
                    unique_id=registry_entry.unique_id,
                    original_device_id=registry_entry.device_id,
                    original_config_subentry_id=(registry_entry.config_subentry_id),
                    desired_config_subentry_id=desired_subentry_id,
                )
            )

    update_plans.sort(
        key=lambda item: (
            _ENTITY_KIND_ORDER[item.kind],
            item.asset_uuid,
            item.entity_id,
        )
    )
    return ExposureMigrationPlan(
        config_entry_id=entry.entry_id,
        devices=tuple(device_plans),
        entity_updates=tuple(update_plans),
    )


def _device_metadata(plan: AssetDevicePlan) -> dict[str, str | None]:
    """Return only canonical Asset metadata supported by Device Registry."""
    return {
        "name": plan.name,
        "manufacturer": plan.manufacturer,
        "model": plan.model,
        "model_id": plan.model_id,
        "serial_number": plan.serial_number,
        "sw_version": plan.sw_version,
        "hw_version": plan.hw_version,
    }


def _discover_attempt_devices(
    *,
    plan: ExposureMigrationPlan,
    registry: dr.DeviceRegistry,
    attempted_missing_assets: set[str],
    created_device_ids: list[str],
) -> None:
    """Track devices proven new by a zero-match preflight and attempted create."""
    known = set(created_device_ids)
    for device_plan in plan.devices:
        if device_plan.asset_uuid not in attempted_missing_assets:
            continue
        matches = _matching_asset_devices(registry, device_plan.asset_uuid)
        if len(matches) != 1:
            continue
        device = matches[0]
        if (
            device.id not in known
            and device.config_entry_id == plan.config_entry_id
            and device.config_subentry_id is None
            and device.identifiers == {asset_device_identifier(device_plan.asset_uuid)}
            and not device.connections
        ):
            created_device_ids.append(device.id)
            known.add(device.id)


def _rollback_exposure_registry(
    *,
    plan: ExposureMigrationPlan,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    attempted_updates: list[EntityRegistryUpdatePlan],
    created_device_ids: list[str],
) -> list[str]:
    """Best-effort compensating rollback, returning actionable failures."""
    failures: list[str] = []
    for update in reversed(attempted_updates):
        current = entity_registry.async_get(update.entity_id)
        if current is None:
            failures.append(f"entity {update.entity_id} disappeared during rollback")
            continue
        if current.unique_id != update.unique_id:
            failures.append(
                f"entity {update.entity_id} changed unique ID during rollback"
            )
            continue
        if (
            current.device_id == update.original_device_id
            and current.config_subentry_id == update.original_config_subentry_id
        ):
            continue
        try:
            entity_registry.async_update_entity(
                update.entity_id,
                device_id=update.original_device_id,
                config_subentry_id=update.original_config_subentry_id,
            )
        except Exception as err:  # noqa: BLE001 - rollback must continue
            failures.append(f"entity {update.entity_id}: {err}")

    planned_missing_identifiers = {
        asset_device_identifier(item.asset_uuid)
        for item in plan.devices
        if item.existing_device_id is None
    }
    for device_id in reversed(created_device_ids):
        device = device_registry.async_get(device_id)
        if device is None:
            continue
        if er.async_entries_for_device(
            entity_registry,
            device_id,
            include_disabled_entities=True,
        ):
            failures.append(f"created Asset Device {device_id} remains referenced")
            continue
        if (
            device.config_entry_id != plan.config_entry_id
            or device.config_subentry_id is not None
            or len(device.identifiers) != 1
            or next(iter(device.identifiers)) not in planned_missing_identifiers
            or device.connections
        ):
            failures.append(
                f"created Asset Device {device_id} no longer has the exact "
                "setup-attempt identity"
            )
            continue
        try:
            device_registry.async_remove_device(device_id)
        except Exception as err:  # noqa: BLE001 - rollback must continue
            failures.append(f"created Asset Device {device_id}: {err}")
    return failures


async def async_reconcile_exposure_registry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    manager: AssetStoreManager,
) -> ExposureMigrationPlan:
    """Reconcile the derived 0.6 projection after a complete preflight."""
    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    plan = build_exposure_migration_plan(
        entry=entry,
        assets=manager.assets(),
        device_registry=device_registry,
        entity_registry=entity_registry,
    )

    created_device_ids: list[str] = []
    attempted_missing_assets: set[str] = set()
    attempted_updates: list[EntityRegistryUpdatePlan] = []
    asset_device_ids: dict[str, str] = {}

    try:
        for device_plan in plan.devices:
            if device_plan.existing_device_id is None:
                attempted_missing_assets.add(device_plan.asset_uuid)
                device = device_registry.async_get_or_create(
                    config_entry_id=entry.entry_id,
                    config_subentry_id=None,
                    identifiers={asset_device_identifier(device_plan.asset_uuid)},
                    **_device_metadata(device_plan),
                )
            else:
                device = device_registry.async_update_device(
                    device_plan.existing_device_id,
                    new_config_subentry_id=None,
                    **_device_metadata(device_plan),
                )
                if device is None:
                    raise AssetStoreError(
                        f"Asset Device {device_plan.existing_device_id} disappeared "
                        "after exposure preflight"
                    )
            if (
                device_plan.existing_device_id is not None
                and device.id != device_plan.existing_device_id
            ):
                raise AssetStoreError(
                    f"Asset {device_plan.asset_uuid} resolved to a different Device "
                    "Registry identity after preflight"
                )
            if device_plan.existing_device_id is None:
                created_device_ids.append(device.id)
            verified = asset_device_entry(
                device_registry,
                config_entry_id=entry.entry_id,
                asset_uuid=device_plan.asset_uuid,
            )
            if verified is None or verified.id != device.id:
                raise AssetStoreError(
                    f"Asset Device reconciliation failed for {device_plan.asset_uuid}"
                )
            asset_device_ids[device_plan.asset_uuid] = verified.id

        for update in plan.entity_updates:
            desired_device_id = asset_device_ids[update.asset_uuid]
            current = entity_registry.async_get(update.entity_id)
            if (
                current is None
                or current.unique_id != update.unique_id
                or current.config_entry_id != entry.entry_id
                or current.platform != DOMAIN
            ):
                raise AssetStoreError(
                    f"Entity {update.entity_id} changed after exposure preflight"
                )
            if (
                current.device_id == desired_device_id
                and current.config_subentry_id == update.desired_config_subentry_id
            ):
                continue
            attempted_updates.append(update)
            entity_registry.async_update_entity(
                update.entity_id,
                device_id=desired_device_id,
                config_subentry_id=update.desired_config_subentry_id,
            )
    except Exception as err:
        _discover_attempt_devices(
            plan=plan,
            registry=device_registry,
            attempted_missing_assets=attempted_missing_assets,
            created_device_ids=created_device_ids,
        )
        rollback_failures = _rollback_exposure_registry(
            plan=plan,
            device_registry=device_registry,
            entity_registry=entity_registry,
            attempted_updates=attempted_updates,
            created_device_ids=created_device_ids,
        )
        if rollback_failures:
            _LOGGER.error(
                "Device Lifecycle exposure migration failed and compensating "
                "rollback was incomplete. Asset Store remains canonical; reload "
                "after repairing the registry errors. Migration error: %s. "
                "Rollback errors: %s",
                err,
                "; ".join(rollback_failures),
            )
            raise AssetStoreError(
                "Exposure registry migration failed and rollback was incomplete: "
                + "; ".join(rollback_failures)
            ) from err
        raise AssetStoreError(
            f"Exposure registry migration failed and was rolled back: {err}"
        ) from err

    return plan
