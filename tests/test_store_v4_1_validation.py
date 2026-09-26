"""Inactive Store 4.1 validator and 3.1 -> 4.1 migration step (WP3)."""

from __future__ import annotations

import ast
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.device_lifecycle import storage
from custom_components.device_lifecycle.const import (
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    STORAGE_KEY,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    AssetStoreError,
    AssetStoreManager,
    DeviceLifecycleStore,
    _empty_store_data,
    _migrate_v1_to_v2_1,
    _migrate_v3_1_to_v4_1,
    _validate_store_data,
    _validate_store_v4_1_data,
)
from custom_components.device_lifecycle.store_shape import (
    ASSET_KEYS_3_1,
    ASSET_KEYS_4_1,
    PURCHASE_KEYS,
    STORE_3_1_TOP_LEVEL_KEYS,
    STORE_4_1_TOP_LEVEL_KEYS,
)

from .conftest import ASSET_UUID, DEVICE_ID, PURCHASE_UUID
from .test_lifecycle import _manager
from .test_maintenance_validation import _event, _schedule

SECOND_UUID = "55555555-5555-4555-8555-555555555555"
MISSING_UUID = "99999999-9999-4999-8999-999999999999"
SCHEDULE_UUID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
EVENT_UUID = "cccccccc-cccc-4ccc-8ccc-ccccccccccc1"
ARCHIVED = "2026-09-26T12:34:56.123456+00:00"
PACKAGE = Path(storage.__file__).parent
INACTIVE_STORE_4_1 = frozenset({"_validate_store_v4_1_data", "_migrate_v3_1_to_v4_1"})


def _serialized(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


async def _rich_store(hass: HomeAssistant, data: AssetStoreData) -> dict[str, Any]:
    """A valid Store 3.1 payload with Purchase, Runtime, Deployment, HA refs,
    Lifecycle history, and a Replacement."""
    manager = _manager(hass, data)
    await manager.async_import_legacy_runtime(ASSET_UUID, Decimal("7200.5"))
    await manager.async_set_asset_deployment(
        ASSET_UUID, deployment_state=DEPLOYMENT_STATE_DEPLOYED
    )
    second = await manager.async_create_manual_asset(name="Second Asset")
    await manager.async_create_asset_replacement(
        ASSET_UUID,
        second["asset_uuid"],
        reason="upgrade",
        effective_date=None,
        notes=None,
    )
    rich = deepcopy(manager._data)
    _validate_store_data(rich)
    assert rich["lifecycle_events"] and rich["replacement_records"]
    return rich


def _v4_1(asset_store_data: AssetStoreData) -> dict[str, Any]:
    return _migrate_v3_1_to_v4_1(deepcopy(asset_store_data))


def _rejects(data: Any, match: str) -> AssetStoreError:
    with pytest.raises(AssetStoreError, match=match) as err:
        _validate_store_v4_1_data(data)
    return err.value


# Store 4.1 validator: accepted payloads


def test_migrated_store_is_valid_4_1(asset_store_data: AssetStoreData) -> None:
    data = _v4_1(asset_store_data)
    snapshot, before = deepcopy(data), _serialized(data)
    _validate_store_v4_1_data(data)
    assert data == snapshot
    assert _serialized(data) == before


@pytest.mark.parametrize("archived_at", [ARCHIVED, "2999-01-01T00:00:00+00:00"])
def test_archived_not_deployed_asset_is_valid(
    asset_store_data: AssetStoreData, archived_at: str
) -> None:
    data = _v4_1(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    asset["deployment_state"] = DEPLOYMENT_STATE_NOT_DEPLOYED
    asset["archived_at"] = archived_at
    _validate_store_v4_1_data(data)


def test_active_deployed_asset_is_valid(asset_store_data: AssetStoreData) -> None:
    data = _v4_1(asset_store_data)
    data["assets"][ASSET_UUID]["deployment_state"] = DEPLOYMENT_STATE_DEPLOYED
    _validate_store_v4_1_data(data)


def test_populated_maintenance_collections_are_valid(
    asset_store_data: AssetStoreData,
) -> None:
    data = _v4_1(asset_store_data)
    data["maintenance_schedules"] = {
        SCHEDULE_UUID: _schedule(SCHEDULE_UUID, ASSET_UUID)
    }
    data["maintenance_events"] = {
        EVENT_UUID: _event(EVENT_UUID, ASSET_UUID, [SCHEDULE_UUID])
    }
    _validate_store_v4_1_data(data)


def test_archived_asset_keeps_valid_maintenance(
    asset_store_data: AssetStoreData,
) -> None:
    data = _v4_1(asset_store_data)
    data["assets"][ASSET_UUID]["archived_at"] = ARCHIVED
    data["maintenance_schedules"] = {
        SCHEDULE_UUID: _schedule(SCHEDULE_UUID, ASSET_UUID)
    }
    _validate_store_v4_1_data(data)


# Top level


@pytest.mark.parametrize("key", sorted(STORE_4_1_TOP_LEVEL_KEYS))
def test_missing_top_level_key_is_rejected(
    asset_store_data: AssetStoreData, key: str
) -> None:
    data = _v4_1(asset_store_data)
    del data[key]
    error = _rejects(data, "invalid top-level shape")
    assert f"missing=['{key}']" in str(error)


def test_unexpected_top_level_key_is_rejected(asset_store_data: AssetStoreData) -> None:
    data = _v4_1(asset_store_data)
    data["extension"] = {"secret": "value"}
    error = _rejects(data, r"unexpected=\['extension'\]")
    assert "value" not in str(error)


@pytest.mark.parametrize("data", [None, [], "store"])
def test_non_mapping_payload_is_rejected(data: Any) -> None:
    _rejects(data, "not a mapping")


@pytest.mark.parametrize(
    "key",
    [
        "purchases",
        "assets",
        "lifecycle_events",
        "replacement_records",
        "maintenance_schedules",
        "maintenance_events",
    ],
)
def test_non_mapping_collection_is_rejected(
    asset_store_data: AssetStoreData, key: str
) -> None:
    data = _v4_1(asset_store_data)
    data[key] = []
    _rejects(data, "invalid top-level structure")


# Record shapes


def test_asset_without_archived_at_is_rejected(
    asset_store_data: AssetStoreData,
) -> None:
    data = _v4_1(asset_store_data)
    del data["assets"][ASSET_UUID]["archived_at"]
    error = _rejects(data, "record shape is invalid")
    assert error.code == "store_record_shape_invalid"
    assert "missing=['archived_at']" in str(error)


def test_unexpected_asset_key_is_rejected(asset_store_data: AssetStoreData) -> None:
    data = _v4_1(asset_store_data)
    data["assets"][ASSET_UUID]["mystery_field"] = "private"
    error = _rejects(data, r"unexpected=\['mystery_field'\]")
    assert "private" not in str(error)
    assert data["assets"][ASSET_UUID]["mystery_field"] == "private"


def test_missing_existing_asset_key_is_rejected(
    asset_store_data: AssetStoreData,
) -> None:
    data = _v4_1(asset_store_data)
    del data["assets"][ASSET_UUID]["notes"]
    _rejects(data, r"missing=\['notes'\]")


def test_non_mapping_asset_is_rejected(asset_store_data: AssetStoreData) -> None:
    data = _v4_1(asset_store_data)
    data["assets"][ASSET_UUID] = []
    _rejects(data, "record is not a mapping")


@pytest.mark.parametrize(
    ("change", "match"),
    [
        (lambda p: p.pop("seller"), r"missing=\['seller'\]"),
        (lambda p: p.update(legacy=True), r"unexpected=\['legacy'\]"),
    ],
)
def test_invalid_purchase_shape_is_rejected(
    asset_store_data: AssetStoreData, change: Any, match: str
) -> None:
    data = _v4_1(asset_store_data)
    change(data["purchases"][PURCHASE_UUID])
    error = _rejects(data, match)
    assert error.code == "store_record_shape_invalid"


def test_non_mapping_purchase_is_rejected(asset_store_data: AssetStoreData) -> None:
    data = _v4_1(asset_store_data)
    data["purchases"][PURCHASE_UUID] = "purchase"
    _rejects(data, "purchases record .* is not a mapping")


# Archive state


@pytest.mark.parametrize(
    "archived_at", ["2026-09-26T12:34:56Z", "2026-09-26T12:34:56", 0, ""]
)
def test_non_canonical_archived_at_is_rejected(
    asset_store_data: AssetStoreData, archived_at: Any
) -> None:
    data = _v4_1(asset_store_data)
    data["assets"][ASSET_UUID]["deployment_state"] = DEPLOYMENT_STATE_NOT_DEPLOYED
    data["assets"][ASSET_UUID]["archived_at"] = archived_at
    error = _rejects(data, "Archive state is invalid")
    assert error.code == "store_archive_state_invalid"
    assert error.__cause__ is not None


def test_archived_and_deployed_is_rejected(asset_store_data: AssetStoreData) -> None:
    data = _v4_1(asset_store_data)
    data["assets"][ASSET_UUID]["deployment_state"] = DEPLOYMENT_STATE_DEPLOYED
    data["assets"][ASSET_UUID]["archived_at"] = ARCHIVED
    snapshot = deepcopy(data)
    error = _rejects(data, "archived and deployed")
    assert error.code == "store_archive_state_invalid"
    assert data == snapshot


# Shared invariants still apply


def _second_asset(data: dict[str, Any], **fields: Any) -> None:
    asset = deepcopy(data["assets"][ASSET_UUID])
    asset.update(
        {
            "asset_uuid": SECOND_UUID,
            "asset_id": "DL0001",
            "purchase_uuid": None,
            "ha_device_refs": [],
            "lifecycle": {"status": "unknown", "current_event_uuid": None},
        }
    )
    asset["field_sources"] = {
        key: value
        for key, value in asset["field_sources"].items()
        if key != "purchase_uuid"
    }
    asset.update(fields)
    data["assets"][SECOND_UUID] = asset


def _invalid_uuid(data: dict[str, Any]) -> None:
    data["assets"]["not-a-uuid"] = data["assets"].pop(ASSET_UUID)


def _invalid_asset_id(data: dict[str, Any]) -> None:
    data["assets"][ASSET_UUID]["asset_id"] = "X1"


def _duplicate_asset_id(data: dict[str, Any]) -> None:
    _second_asset(data, asset_id=data["assets"][ASSET_UUID]["asset_id"])


def _invalid_runtime(data: dict[str, Any]) -> None:
    data["assets"][ASSET_UUID]["runtime"] = {"total_seconds": "-1"}


def _duplicate_primary(data: dict[str, Any]) -> None:
    _second_asset(data, ha_device_refs=[{"device_id": DEVICE_ID, "role": "primary"}])


def _broken_membership(data: dict[str, Any]) -> None:
    data["purchases"][PURCHASE_UUID]["asset_uuids"].append(MISSING_UUID)


def _broken_lifecycle(data: dict[str, Any]) -> None:
    data["assets"][ASSET_UUID]["lifecycle"]["current_event_uuid"] = MISSING_UUID


def _broken_replacement(data: dict[str, Any]) -> None:
    data["replacement_records"][MISSING_UUID] = {
        "replacement_uuid": MISSING_UUID,
        "predecessor_asset_uuid": ASSET_UUID,
        "successor_asset_uuid": ASSET_UUID,
        "reason": "upgrade",
        "effective_date": None,
        "recorded_at": ARCHIVED,
        "notes": None,
        "voided_at": None,
        "void_reason": None,
    }


@pytest.mark.parametrize(
    "breaker",
    [
        _invalid_uuid,
        _invalid_asset_id,
        _duplicate_asset_id,
        _invalid_runtime,
        _duplicate_primary,
        _broken_membership,
        _broken_lifecycle,
        _broken_replacement,
    ],
)
def test_shared_invariants_apply_to_4_1_as_to_3_1(
    asset_store_data: AssetStoreData, breaker: Any
) -> None:
    store_3_1 = deepcopy(asset_store_data)
    breaker(store_3_1)
    with pytest.raises(AssetStoreError) as error_3_1:
        _validate_store_data(store_3_1)

    store_4_1 = _v4_1(asset_store_data)
    breaker(store_4_1)
    with pytest.raises(AssetStoreError) as error_4_1:
        _validate_store_v4_1_data(store_4_1)
    assert str(error_4_1.value) == str(error_3_1.value)
    assert error_4_1.value.code == error_3_1.value.code


# Maintenance authority


def test_schedule_of_missing_asset_is_rejected(
    asset_store_data: AssetStoreData,
) -> None:
    data = _v4_1(asset_store_data)
    data["maintenance_schedules"] = {
        SCHEDULE_UUID: _schedule(SCHEDULE_UUID, MISSING_UUID)
    }
    error = _rejects(data, "Maintenance data is invalid")
    assert error.code == "store_maintenance_invalid"


def test_event_with_missing_schedule_is_rejected(
    asset_store_data: AssetStoreData,
) -> None:
    data = _v4_1(asset_store_data)
    data["maintenance_events"] = {
        EVENT_UUID: _event(EVENT_UUID, ASSET_UUID, [SCHEDULE_UUID])
    }
    _rejects(data, "Maintenance data is invalid")


def test_correction_of_active_event_is_rejected(
    asset_store_data: AssetStoreData,
) -> None:
    data = _v4_1(asset_store_data)
    other = "cccccccc-cccc-4ccc-8ccc-ccccccccccc2"
    data["maintenance_events"] = {
        EVENT_UUID: _event(EVENT_UUID, ASSET_UUID, []),
        other: _event(other, ASSET_UUID, [], corrects_event_uuid=EVENT_UUID),
    }
    _rejects(data, "Maintenance data is invalid")


def test_anchor_without_interval_is_rejected(asset_store_data: AssetStoreData) -> None:
    data = _v4_1(asset_store_data)
    schedule = _schedule(
        SCHEDULE_UUID,
        ASSET_UUID,
        runtime_interval_seconds=None,
        initial_anchor={"date": None, "runtime_seconds": "10"},
    )
    data["maintenance_schedules"] = {SCHEDULE_UUID: schedule}
    _rejects(data, "Maintenance data is invalid")


# Version separation


def test_store_3_1_validator_rejects_4_1(asset_store_data: AssetStoreData) -> None:
    with pytest.raises(AssetStoreError, match="invalid top-level shape"):
        _validate_store_data(_v4_1(asset_store_data))  # type: ignore[arg-type]


def test_store_4_1_validator_rejects_3_1(asset_store_data: AssetStoreData) -> None:
    error = _rejects(deepcopy(asset_store_data), "invalid top-level shape")
    assert "maintenance_events" in str(error)
    assert "maintenance_schedules" in str(error)


def test_4_1_top_level_with_3_1_assets_is_rejected(
    asset_store_data: AssetStoreData,
) -> None:
    data = {
        **deepcopy(asset_store_data),
        "maintenance_schedules": {},
        "maintenance_events": {},
    }
    _rejects(data, r"missing=\['archived_at'\]")


# 3.1 -> 4.1 migration step


async def test_migration_adds_only_archive_state_and_empty_maintenance(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    source = await _rich_store(hass, asset_store_data)
    snapshot, before = deepcopy(source), _serialized(source)

    result = _migrate_v3_1_to_v4_1(source)

    assert source == snapshot
    assert _serialized(source) == before
    assert set(result) == STORE_4_1_TOP_LEVEL_KEYS
    assert result["maintenance_schedules"] == {}
    assert result["maintenance_events"] == {}
    for key in STORE_3_1_TOP_LEVEL_KEYS - {"assets"}:
        assert result[key] == source[key], key
    for asset_uuid, asset in source["assets"].items():
        migrated = result["assets"][asset_uuid]
        assert set(migrated) == ASSET_KEYS_4_1
        assert migrated["archived_at"] is None
        assert {k: v for k, v in migrated.items() if k != "archived_at"} == asset
    for purchase in result["purchases"].values():
        assert set(purchase) == PURCHASE_KEYS
    # A deployed Asset is migrated as active; nothing is inferred.
    assert result["assets"][ASSET_UUID]["deployment_state"] == DEPLOYMENT_STATE_DEPLOYED


async def test_migration_result_is_not_aliased(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    source = await _rich_store(hass, asset_store_data)
    snapshot = deepcopy(source)
    result = _migrate_v3_1_to_v4_1(source)

    asset = result["assets"][ASSET_UUID]
    asset["name"] = "mutated"
    asset["runtime"]["total_seconds"] = "0"
    asset["ha_device_refs"].clear()
    result["purchases"][PURCHASE_UUID]["asset_uuids"].append("x")
    next(iter(result["lifecycle_events"].values()))["notes"] = "mutated"
    next(iter(result["replacement_records"].values()))["reason"] = "mutated"
    result["maintenance_schedules"]["x"] = {}
    assert source == snapshot

    fresh = _migrate_v3_1_to_v4_1(source)
    fresh_snapshot = deepcopy(fresh)
    source["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "1"
    source["purchases"][PURCHASE_UUID]["asset_uuids"].clear()
    assert fresh == fresh_snapshot


def test_migration_is_deterministic(asset_store_data: AssetStoreData) -> None:
    assert _v4_1(asset_store_data) == _v4_1(asset_store_data)


@pytest.mark.parametrize(
    ("kind", "record_id"),
    [("assets", ASSET_UUID), ("purchases", PURCHASE_UUID)],
)
@pytest.mark.parametrize("missing", [False, True])
def test_incompatible_source_fails_before_transform(
    asset_store_data: AssetStoreData, kind: str, record_id: str, missing: bool
) -> None:
    source = deepcopy(asset_store_data)
    record = source[kind][record_id]
    if missing:
        del record["notes"]
    else:
        record["mystery_field"] = {"evidence": [1]}
    snapshot, before = deepcopy(source), _serialized(source)

    with (
        patch.object(storage, "add_asset_archive_state") as archive_transform,
        patch.object(storage, "add_maintenance_collections") as maintenance_transform,
        pytest.raises(AssetStoreError) as err,
    ):
        _migrate_v3_1_to_v4_1(source)

    assert err.value.code == "store_migration_source_incompatible"
    assert record_id in str(err.value)
    archive_transform.assert_not_called()
    maintenance_transform.assert_not_called()
    assert source == snapshot
    assert _serialized(source) == before
    if not missing:
        assert source[kind][record_id]["mystery_field"] == {"evidence": [1]}


def test_invalid_3_1_source_fails_with_the_3_1_error(
    asset_store_data: AssetStoreData,
) -> None:
    source = deepcopy(asset_store_data)
    source["assets"][ASSET_UUID]["runtime"] = {"total_seconds": "-1"}
    with pytest.raises(AssetStoreError) as expected:
        _validate_store_data(deepcopy(source))
    with pytest.raises(AssetStoreError) as err:
        _migrate_v3_1_to_v4_1(source)
    assert str(err.value) == str(expected.value)


@pytest.mark.parametrize("extra", ["maintenance_schedules", "archived_at"])
def test_4_1_payload_is_not_a_migration_source(
    asset_store_data: AssetStoreData, extra: str
) -> None:
    source = _v4_1(asset_store_data)
    if extra == "archived_at":
        source = {k: v for k, v in source.items() if not k.startswith("maintenance")}
    with pytest.raises(AssetStoreError):
        _migrate_v3_1_to_v4_1(source)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("target", "error"),
    [
        ("add_asset_archive_state", "ArchiveCompositionError"),
        ("add_maintenance_collections", "MaintenanceCompositionError"),
    ],
)
def test_composition_failure_fails_closed(
    asset_store_data: AssetStoreData, target: str, error: str
) -> None:
    source = deepcopy(asset_store_data)
    snapshot = deepcopy(source)
    with (
        patch.object(storage, target, side_effect=getattr(storage, error)("boom")),
        pytest.raises(AssetStoreError) as err,
    ):
        _migrate_v3_1_to_v4_1(source)
    assert err.value.code == "store_migration_composition_failed"
    assert isinstance(err.value.__cause__, getattr(storage, error))
    assert source == snapshot


def test_migration_order_is_archive_then_maintenance_then_final_validation(
    asset_store_data: AssetStoreData,
) -> None:
    calls: list[str] = []
    real = {
        name: getattr(storage, name)
        for name in (
            "_validate_store_data",
            "preflight_store_3_1_record_shapes",
            "add_asset_archive_state",
            "add_maintenance_collections",
            "_validate_store_v4_1_data",
        )
    }

    def _recorder(name: str) -> Any:
        def _call(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            return real[name](*args, **kwargs)

        return _call

    with patch.multiple(storage, **{name: _recorder(name) for name in real}):
        storage._migrate_v3_1_to_v4_1(deepcopy(asset_store_data))
    assert calls == [
        "_validate_store_data",
        "preflight_store_3_1_record_shapes",
        "add_asset_archive_state",
        "add_maintenance_collections",
        "_validate_store_v4_1_data",
    ]


@pytest.mark.parametrize(
    ("version", "fixture_name"),
    [((1, 1), "asset_store_data_v1_1"), ((1, 2), "asset_store_data_v1_2")],
)
async def test_future_chain_from_1_x(
    hass: HomeAssistant,
    request: pytest.FixtureRequest,
    version: tuple[int, int],
    fixture_name: str,
) -> None:
    """Existing dispatch to 3.1, then the inactive step, invoked directly."""
    source = request.getfixturevalue(fixture_name)
    store_3_1 = await DeviceLifecycleStore(hass)._async_migrate_func(
        *version, deepcopy(source)
    )
    result = _migrate_v3_1_to_v4_1(store_3_1)
    assert all(asset["archived_at"] is None for asset in result["assets"].values())
    assert set(result) == STORE_4_1_TOP_LEVEL_KEYS


async def test_future_chain_from_2_1_and_3_1(
    hass: HomeAssistant,
    asset_store_data_v1_2: AssetStoreData,
    asset_store_data: AssetStoreData,
) -> None:
    store_2_1 = _migrate_v1_to_v2_1(deepcopy(asset_store_data_v1_2), 2)
    store_3_1 = await DeviceLifecycleStore(hass)._async_migrate_func(2, 1, store_2_1)
    for source in (store_3_1, asset_store_data):
        result = _migrate_v3_1_to_v4_1(source)
        _validate_store_v4_1_data(result)
        assert result["maintenance_schedules"] == result["maintenance_events"] == {}


# Current production behavior is unchanged


def test_production_store_constants_are_3_1(hass: HomeAssistant) -> None:
    assert (STORAGE_VERSION, STORAGE_MINOR_VERSION) == (3, 1)
    store = DeviceLifecycleStore(hass)
    assert (store.version, store.minor_version, store.key) == (3, 1, STORAGE_KEY)
    assert storage.STORE_TOP_LEVEL_KEYS == STORE_3_1_TOP_LEVEL_KEYS
    assert _empty_store_data() == {
        "next_asset_number": 1,
        "purchases": {},
        "assets": {},
        "lifecycle_events": {},
        "replacement_records": {},
    }


async def test_production_asset_has_20_keys(hass: HomeAssistant) -> None:
    asset = await _manager(hass).async_create_manual_asset(name="Production Asset")
    assert set(asset) == ASSET_KEYS_3_1
    assert "archived_at" not in asset


@pytest.mark.parametrize(
    ("version", "fixture_name"),
    [
        ((1, 1), "asset_store_data_v1_1"),
        ((1, 2), "asset_store_data_v1_2"),
        ((3, 1), "asset_store_data"),
    ],
)
async def test_current_dispatch_still_produces_3_1(
    hass: HomeAssistant,
    request: pytest.FixtureRequest,
    version: tuple[int, int],
    fixture_name: str,
) -> None:
    source = request.getfixturevalue(fixture_name)
    migrated = await DeviceLifecycleStore(hass)._async_migrate_func(
        *version, deepcopy(source)
    )
    assert set(migrated) == STORE_3_1_TOP_LEVEL_KEYS
    for asset in migrated["assets"].values():
        assert set(asset) == ASSET_KEYS_3_1


async def test_current_dispatch_rejects_4_1_payloads_and_unknown_versions(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    store = DeviceLifecycleStore(hass)
    with pytest.raises(AssetStoreError, match="invalid top-level shape"):
        await store._async_migrate_func(3, 1, _v4_1(asset_store_data))
    for version in ((3, 2), (4, 1), (2, 2), (0, 1)):
        with pytest.raises(AssetStoreError, match="Unsupported Asset Core Store"):
            await store._async_migrate_func(*version, deepcopy(asset_store_data))


async def test_production_paths_never_call_store_4_1_code(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data_v1_1: AssetStoreData,
) -> None:
    """Load with migration, recover, and mutate: the 4.1 code is never called."""
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": deepcopy(asset_store_data_v1_1),
    }
    with (
        patch.object(storage, "_validate_store_v4_1_data") as validate_4_1,
        patch.object(storage, "_migrate_v3_1_to_v4_1") as migrate_4_1,
        patch.object(DeviceLifecycleStore, "async_save"),
    ):
        manager = AssetStoreManager(hass)
        await manager.async_setup()
        await manager.async_create_manual_asset(name="After setup")
        manager._persistence_uncertain = True
        with patch.object(
            manager._store,
            "async_load_persisted_snapshot",
            return_value=deepcopy(manager._data),
        ):
            await manager.async_create_manual_asset(name="After recovery")
    validate_4_1.assert_not_called()
    migrate_4_1.assert_not_called()
    assert set(manager._data) == STORE_3_1_TOP_LEVEL_KEYS
    assert all(
        set(asset) == ASSET_KEYS_3_1 for asset in manager._data["assets"].values()
    )


# Static reachability: shared by the WP1, WP2, and Maintenance boundary tests

# Pure helpers that production may use only inside the inactive Store 4.1 code.
STORE_4_1_ONLY_SYMBOLS = frozenset(
    {
        "ASSET_KEYS_4_1",
        "PURCHASE_KEYS",
        "STORE_4_1_TOP_LEVEL_KEYS",
        "StoreShapeError",
        "preflight_store_3_1_record_shapes",
        "require_exact_record_keys",
        "ArchiveCompositionError",
        "ArchiveValidationError",
        "add_asset_archive_state",
        "validate_asset_archive_state",
        "MAINTENANCE_COLLECTION_KEYS",
        "MaintenanceCompositionError",
        "MaintenanceValidationError",
        "add_maintenance_collections",
        "validate_maintenance_collections",
    }
)
PURE_STORE_4_1_LIBRARIES = frozenset(
    {
        "archive.py",
        "store_shape.py",
        "maintenance.py",
        "maintenance_mutations.py",
        "maintenance_projection.py",
    }
)


def _imported_modules(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").rsplit(".", 1)[-1])
            if not node.module:
                names.update(alias.name for alias in node.names)
    return names


def referencing_scopes(tree: ast.Module, names: frozenset[str]) -> dict[str, set[str]]:
    """Map each referenced name to the top-level functions or methods using it.

    Module-level references (other than imports) are reported as ``<module>``.
    """
    found: dict[str, set[str]] = {}

    def _visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                continue
            child_scope = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_scope = child.name if scope == "<module>" else scope
            elif isinstance(child, ast.ClassDef) and scope == "<module>":
                child_scope = f"{child.name}"
            if isinstance(child, ast.Name) and child.id in names:
                found.setdefault(child.id, set()).add(scope)
            elif isinstance(child, ast.Attribute) and child.attr in names:
                found.setdefault(child.attr, set()).add(scope)
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) and isinstance(node, ast.ClassDef):
                child_scope = f"{node.name}.{child.name}"
            _visit(child, child_scope)

    _visit(tree, "<module>")
    return found


def _production_trees() -> dict[str, ast.Module]:
    return {
        path.name: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(PACKAGE.glob("*.py"))
    }


def test_only_storage_imports_the_store_4_1_helpers() -> None:
    allowed = {"storage.py", "archive.py", "store_shape.py", "maintenance.py"}
    for name, tree in _production_trees().items():
        imported = _imported_modules(tree) & {"store_shape", "archive"}
        if name not in allowed:
            assert not imported, name
    # Maintenance itself stays free of Archive until WP5.
    assert not _imported_modules(_production_trees()["maintenance.py"]) & {
        "archive",
        "store_shape",
    }


def test_store_4_1_helpers_are_used_only_inside_the_inactive_functions() -> None:
    for name, tree in _production_trees().items():
        # The pure libraries own these symbols; their own tests prove that no
        # production module is wired to them.
        if name in PURE_STORE_4_1_LIBRARIES:
            continue
        scopes = referencing_scopes(tree, STORE_4_1_ONLY_SYMBOLS | INACTIVE_STORE_4_1)
        for symbol, users in scopes.items():
            assert users <= INACTIVE_STORE_4_1, (name, symbol, users)


def test_inactive_functions_are_not_called_by_production() -> None:
    """Only the inactive migration step itself calls the 4.1 validator."""
    for name, tree in _production_trees().items():
        scopes = referencing_scopes(tree, INACTIVE_STORE_4_1)
        assert scopes.get("_migrate_v3_1_to_v4_1", set()) == set(), name
        assert scopes.get("_validate_store_v4_1_data", set()) <= {
            "_migrate_v3_1_to_v4_1"
        }, name


@pytest.mark.parametrize(
    "scope",
    [
        "DeviceLifecycleStore._async_migrate_func",
        "AssetStoreManager.async_setup",
        "AssetStoreManager._async_recover_uncertain_persistence",
        "AssetStoreManager._async_mutate_reporting",
        "AssetStoreManager.async_quick_create_asset",
    ],
)
def test_production_validation_paths_use_the_3_1_validator(scope: str) -> None:
    tree = _production_trees()["storage.py"]
    scopes = referencing_scopes(
        tree, INACTIVE_STORE_4_1 | frozenset({"_validate_store_data"})
    )
    assert scope in scopes["_validate_store_data"]
    for inactive in INACTIVE_STORE_4_1:
        assert scope not in scopes.get(inactive, set())


def test_mutate_delegates_to_mutate_reporting() -> None:
    tree = _production_trees()["storage.py"]
    scopes = referencing_scopes(tree, frozenset({"_async_mutate_reporting"}))
    assert "AssetStoreManager._async_mutate" in scopes["_async_mutate_reporting"]
