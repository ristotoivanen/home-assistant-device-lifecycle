"""The Maintenance component of the future Store upgrade, as inactive library code."""

from __future__ import annotations

import ast
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.device_lifecycle import maintenance
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
    _validate_store_data,
)

from .conftest import ASSET_UUID
from .test_lifecycle import _manager

STORE_3_1_KEYS = (
    "next_asset_number",
    "purchases",
    "assets",
    "lifecycle_events",
    "replacement_records",
)


async def _rich_store(hass: HomeAssistant, data: AssetStoreData) -> dict[str, Any]:
    """A valid Store 3.1 payload with Purchase, Runtime, Deployment, and Lifecycle."""
    manager = _manager(hass, data)
    await manager.async_import_legacy_runtime(ASSET_UUID, Decimal("7200.5"))
    await manager.async_create_manual_asset(name="Second Asset")
    rich = deepcopy(manager._data)
    _validate_store_data(rich)
    assert rich["purchases"]
    assert rich["lifecycle_events"]
    assert rich["assets"][ASSET_UUID]["runtime"] == {"total_seconds": "7200.5"}
    assert "deployment_state" in rich["assets"][ASSET_UUID]
    return rich


async def test_adds_only_empty_collections_and_preserves_everything(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """Nothing is inferred from Purchase, Runtime, Deployment, or Lifecycle."""
    original = await _rich_store(hass, asset_store_data)

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
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    original = await _rich_store(hass, asset_store_data)
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
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    original = await _rich_store(hass, asset_store_data)
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
    asset_store_data: AssetStoreData, existing: dict[str, Any]
) -> None:
    """Applies exactly once; existing or partial Maintenance data is never overwritten."""
    candidate: dict[str, Any] = {**deepcopy(asset_store_data), **deepcopy(existing)}
    before = deepcopy(candidate)
    with pytest.raises(MaintenanceCompositionError, match="already contains"):
        add_maintenance_collections(candidate)
    assert candidate == before


def test_applying_twice_fails_closed(asset_store_data: AssetStoreData) -> None:
    once = add_maintenance_collections(asset_store_data)
    with pytest.raises(MaintenanceCompositionError):
        add_maintenance_collections(once)


@pytest.mark.parametrize("candidate", [None, [], "store", ()])
def test_non_mapping_candidate_is_rejected(candidate: Any) -> None:
    with pytest.raises(MaintenanceCompositionError, match="must be a mapping"):
        add_maintenance_collections(candidate)


def test_composable_with_other_top_level_components(
    asset_store_data: AssetStoreData,
) -> None:
    """The helper does not own the order or the final shape of the upgrade."""
    archived = {**deepcopy(asset_store_data), "future_component": {"kept": True}}
    result = add_maintenance_collections(archived)
    assert result["future_component"] == {"kept": True}


async def test_added_collections_pass_maintenance_validation(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    result = add_maintenance_collections(await _rich_store(hass, asset_store_data))
    validate_maintenance_collections(
        result["assets"],
        result["maintenance_schedules"],
        result["maintenance_events"],
    )


async def test_store_3_1_activation_boundary_stays_real(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """The original is valid 3.1; the composed candidate deliberately is not."""
    original = await _rich_store(hass, asset_store_data)
    _validate_store_data(original)
    result = add_maintenance_collections(original)
    with pytest.raises(AssetStoreError, match="invalid top-level shape"):
        _validate_store_data(result)  # type: ignore[arg-type]
    assert (STORAGE_VERSION, STORAGE_MINOR_VERSION) == (3, 1)
    assert set(STORE_3_1_KEYS) == STORE_TOP_LEVEL_KEYS


def test_helper_is_pure_and_not_wired_into_production() -> None:
    """No HA, I/O, clock, or migration dependency; nothing in production calls it."""
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
        if path.name in {"maintenance.py", "maintenance_mutations.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        assert "add_maintenance_collections" not in source, path.name
        assert "_migrate_v3_1_to_v4" not in source, path.name
