"""The Maintenance component of the Store 3.1 -> 4.1 migration step."""

from __future__ import annotations

import ast
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.device_lifecycle import maintenance, storage
from custom_components.device_lifecycle.maintenance import (
    MaintenanceCompositionError,
    add_maintenance_collections,
    validate_maintenance_collections,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    STORE_TOP_LEVEL_KEYS,
    AssetStoreError,
    _migrate_v3_1_to_v4_1,
    _validate_store_data,
    _validate_store_v3_1_data,
)

from .conftest import ASSET_UUID, as_store_3_1_source
from .test_lifecycle import _manager
from .test_store_v4_1_validation import referencing_scopes

STORE_3_1_KEYS = (
    "next_asset_number",
    "purchases",
    "assets",
    "lifecycle_events",
    "replacement_records",
)


async def _rich_store(hass: HomeAssistant, data: AssetStoreData) -> dict[str, Any]:
    """A valid Store 3.1 source with Purchase, Runtime, Deployment, and
    Lifecycle, built through the production mutations."""
    manager = _manager(hass, _migrate_v3_1_to_v4_1(deepcopy(data)))
    await manager.async_import_legacy_runtime(ASSET_UUID, Decimal("7200.5"))
    await manager.async_create_manual_asset(name="Second Asset")
    rich = as_store_3_1_source(manager._data)
    _validate_store_v3_1_data(rich)
    assert rich["purchases"]
    assert rich["lifecycle_events"]
    assert rich["assets"][ASSET_UUID]["runtime"] == {"total_seconds": "7200.5"}
    assert "deployment_state" in rich["assets"][ASSET_UUID]
    return rich


async def test_adds_only_empty_collections_and_preserves_everything(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData
) -> None:
    """Nothing is inferred from Purchase, Runtime, Deployment, or Lifecycle."""
    original = await _rich_store(hass, asset_store_data_v3_1)

    result = add_maintenance_collections(original)

    assert result["maintenance_schedules"] == {}
    assert result["maintenance_events"] == {}
    assert set(result) == set(STORE_3_1_KEYS) | {
        "maintenance_schedules",
        "maintenance_events",
    }
    for key in STORE_3_1_KEYS:
        assert result[key] == original[key], key


async def test_input_is_untouched_and_result_is_not_aliased(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData
) -> None:
    original = await _rich_store(hass, asset_store_data_v3_1)
    snapshot = deepcopy(original)

    result = add_maintenance_collections(original)
    assert original == snapshot
    assert "maintenance_schedules" not in original

    for key in STORE_3_1_KEYS:
        if isinstance(original[key], dict):
            assert result[key] is not original[key], key
    result["assets"][ASSET_UUID]["name"] = "mutated"
    result["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "0"
    result["purchases"].clear()
    result["lifecycle_events"].clear()
    result["next_asset_number"] = 999
    result["maintenance_schedules"]["x"] = {}
    assert original == snapshot


async def test_deterministic(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData
) -> None:
    original = await _rich_store(hass, asset_store_data_v3_1)
    first = add_maintenance_collections(original)
    second = add_maintenance_collections(original)
    assert first == second
    assert list(first) == list(second)
    assert first["maintenance_schedules"] is not second["maintenance_schedules"]


@pytest.mark.parametrize(
    "existing",
    [
        {"maintenance_schedules": {}},
        {"maintenance_events": {}},
        {"maintenance_schedules": {}, "maintenance_events": {}},
        {"maintenance_schedules": {"x": {}}, "maintenance_events": {"y": {}}},
    ],
)
def test_existing_maintenance_collections_fail_closed(
    asset_store_data_v3_1: AssetStoreData, existing: dict[str, Any]
) -> None:
    """Applies exactly once; existing or partial Maintenance data is never overwritten."""
    candidate: dict[str, Any] = {**deepcopy(asset_store_data_v3_1), **deepcopy(existing)}
    before = deepcopy(candidate)
    with pytest.raises(MaintenanceCompositionError, match="already contains"):
        add_maintenance_collections(candidate)
    assert candidate == before


def test_applying_twice_fails_closed(asset_store_data_v3_1: AssetStoreData) -> None:
    once = add_maintenance_collections(asset_store_data_v3_1)
    with pytest.raises(MaintenanceCompositionError):
        add_maintenance_collections(once)


@pytest.mark.parametrize("candidate", [None, [], "store", ()])
def test_non_mapping_candidate_is_rejected(candidate: Any) -> None:
    with pytest.raises(MaintenanceCompositionError, match="must be a mapping"):
        add_maintenance_collections(candidate)


def test_composable_with_other_top_level_components(
    asset_store_data_v3_1: AssetStoreData,
) -> None:
    """The helper does not own the order or the final shape of the upgrade."""
    archived = {**deepcopy(asset_store_data_v3_1), "future_component": {"kept": True}}
    result = add_maintenance_collections(archived)
    assert result["future_component"] == {"kept": True}


async def test_added_collections_pass_maintenance_validation(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData
) -> None:
    result = add_maintenance_collections(await _rich_store(hass, asset_store_data_v3_1))
    validate_maintenance_collections(
        result["assets"],
        result["maintenance_schedules"],
        result["maintenance_events"],
    )


async def test_store_3_1_source_boundary_stays_real(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData
) -> None:
    """The source is valid 3.1 and the composed candidate is not; the
    production Store 4.1 carries both Maintenance collections."""
    original = await _rich_store(hass, asset_store_data_v3_1)
    _validate_store_v3_1_data(original)
    result = add_maintenance_collections(original)
    with pytest.raises(AssetStoreError, match="invalid top-level shape"):
        _validate_store_v3_1_data(result)  # type: ignore[arg-type]
    assert (STORAGE_VERSION, STORAGE_MINOR_VERSION) == (4, 1)
    assert STORE_TOP_LEVEL_KEYS == set(STORE_3_1_KEYS) | {
        "maintenance_schedules",
        "maintenance_events",
    }
    _validate_store_data(_migrate_v3_1_to_v4_1(original))


def test_helper_is_pure_and_reached_only_through_the_migration_step() -> None:
    """No HA, I/O, clock, or migration dependency.

    The Store 3.1 -> 4.1 step composes this helper, and only the Store
    migration callback dispatches to that step.
    """
    module_path = Path(maintenance.__file__)
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
    assert not any(name.startswith("homeassistant") for name in imports)
    assert "_migrate_v3_1_to_v4" not in module_path.read_text(encoding="utf-8")
    for path in module_path.parent.glob("*.py"):
        if path.name in {"maintenance.py", "maintenance_mutations.py", "storage.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        assert "add_maintenance_collections" not in source, path.name
        assert "_migrate_v3_1_to_v4" not in source, path.name

    storage_tree = ast.parse(
        (module_path.parent / "storage.py").read_text(encoding="utf-8")
    )
    scopes = referencing_scopes(
        storage_tree,
        frozenset({"add_maintenance_collections", "_migrate_v3_1_to_v4_1"}),
    )
    # The step composes the helper, and only the migration callback runs it.
    assert scopes["add_maintenance_collections"] == {"_migrate_v3_1_to_v4_1"}
    assert hasattr(storage, "_migrate_v3_1_to_v4_1")
    assert scopes["_migrate_v3_1_to_v4_1"] == {
        "DeviceLifecycleStore._async_migrate_func"
    }
