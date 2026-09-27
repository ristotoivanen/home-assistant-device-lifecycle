"""Archive Store transform and Archive state validation (WP2)."""

from __future__ import annotations

import ast
import json
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.device_lifecycle import archive, storage
from custom_components.device_lifecycle.archive import (
    ARCHIVED_AT,
    ArchiveCompositionError,
    ArchiveValidationError,
    add_asset_archive_state,
    asset_is_archived,
    validate_asset_archive_state,
)
from custom_components.device_lifecycle.const import (
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DEPLOYMENT_STATE_UNKNOWN,
)
from custom_components.device_lifecycle.maintenance import add_maintenance_collections
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    AssetStoreError,
    _empty_store_data,
    _migrate_v3_1_to_v4_1,
    _validate_store_data,
    _validate_store_v3_1_data,
)
from custom_components.device_lifecycle.store_shape import (
    ASSET_KEYS_3_1,
    ASSET_KEYS_4_1,
    STORE_3_1_TOP_LEVEL_KEYS,
    STORE_4_1_TOP_LEVEL_KEYS,
    preflight_store_3_1_record_shapes,
)

from .conftest import ASSET_UUID, DEVICE_ID, PURCHASE_UUID, as_store_3_1_source
from .test_lifecycle import _manager
from .test_store_v4_1_validation import STORE_4_1_SCOPES, referencing_scopes

SECOND_UUID = "55555555-5555-4555-8555-555555555555"
THIRD_UUID = "66666666-6666-4666-8666-666666666666"
ARCHIVED = "2026-09-26T12:34:56.123456+00:00"
PACKAGE = Path(archive.__file__).parent


def _serialized(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


async def _rich_store(hass: HomeAssistant, data: AssetStoreData) -> dict[str, Any]:
    """A valid Store 3.1 source with Purchase, Runtime, Deployment, HA refs,
    Lifecycle history, and a Replacement, built through the production
    mutations and returned as the Store 3.1 it would have been."""
    manager = _manager(hass, _migrate_v3_1_to_v4_1(deepcopy(data)))
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
    rich = as_store_3_1_source(manager._data)
    _validate_store_v3_1_data(rich)
    assert len(rich["assets"]) == 2
    assert rich["purchases"][PURCHASE_UUID]["asset_uuids"] == [ASSET_UUID]
    assert rich["assets"][ASSET_UUID]["runtime"] == {"total_seconds": "7200.5"}
    assert rich["assets"][ASSET_UUID]["deployment_state"] == DEPLOYMENT_STATE_DEPLOYED
    assert rich["assets"][ASSET_UUID]["ha_device_refs"][0]["device_id"] == DEVICE_ID
    assert rich["lifecycle_events"]
    assert rich["replacement_records"]
    return rich


def _store_4_1_asset(
    asset_store_data_v3_1: AssetStoreData, **fields: Any
) -> dict[str, Any]:
    asset = deepcopy(asset_store_data_v3_1["assets"][ASSET_UUID])
    asset[ARCHIVED_AT] = None
    asset.update(fields)
    return asset


# add_asset_archive_state


async def test_every_asset_gains_only_archived_at_none(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData
) -> None:
    source = await _rich_store(hass, asset_store_data_v3_1)
    result = add_asset_archive_state(source)

    assert list(result) == list(source)
    for key in source:
        if key != "assets":
            assert result[key] == source[key], key
    assert list(result["assets"]) == list(source["assets"])
    for asset_uuid, asset in source["assets"].items():
        transformed = result["assets"][asset_uuid]
        assert transformed[ARCHIVED_AT] is None
        assert {k: v for k, v in transformed.items() if k != ARCHIVED_AT} == asset
        assert set(transformed) == set(asset) | {ARCHIVED_AT}


async def test_deployed_asset_becomes_active_not_archived(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData
) -> None:
    """Composition, not validation: active + deployed is valid and not inferred."""
    result = add_asset_archive_state(await _rich_store(hass, asset_store_data_v3_1))
    asset = result["assets"][ASSET_UUID]
    assert asset["deployment_state"] == DEPLOYMENT_STATE_DEPLOYED
    assert asset[ARCHIVED_AT] is None
    assert asset_is_archived(asset) is False
    validate_asset_archive_state(result["assets"])


def test_empty_assets_mapping() -> None:
    source = _empty_store_data()
    result = add_asset_archive_state(source)
    assert result == source
    assert result["assets"] == {}
    assert result["assets"] is not source["assets"]


def test_one_asset(asset_store_data_v3_1: AssetStoreData) -> None:
    result = add_asset_archive_state(asset_store_data_v3_1)
    assert result["assets"][ASSET_UUID][ARCHIVED_AT] is None
    assert len(result["assets"]) == 1


def test_read_only_mapping_input(asset_store_data_v3_1: AssetStoreData) -> None:
    source = deepcopy(asset_store_data_v3_1)
    frozen = MappingProxyType(
        {
            **source,
            "assets": MappingProxyType(
                {
                    key: MappingProxyType(value)
                    for key, value in source["assets"].items()
                }
            ),
        }
    )
    result = add_asset_archive_state(frozen)
    assert isinstance(result, dict)
    assert isinstance(result["assets"][ASSET_UUID], dict)
    assert result["assets"][ASSET_UUID][ARCHIVED_AT] is None


async def test_input_is_unchanged_and_result_is_not_aliased(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData
) -> None:
    source = await _rich_store(hass, asset_store_data_v3_1)
    snapshot, before = deepcopy(source), _serialized(source)

    result = add_asset_archive_state(source)
    assert source == snapshot
    assert _serialized(source) == before
    assert ARCHIVED_AT not in source["assets"][ASSET_UUID]

    # Mutating the result never reaches the input.
    asset = result["assets"][ASSET_UUID]
    asset["name"] = "mutated"
    asset["runtime"]["total_seconds"] = "0"
    asset["warranty"]["until"] = None
    asset["field_sources"]["name"] = "user"
    asset["ha_device_refs"][0]["device_id"] = "other"
    asset["lifecycle"]["status"] = "disposed"
    asset[ARCHIVED_AT] = ARCHIVED
    result["purchases"][PURCHASE_UUID]["asset_uuids"].append("x")
    result["purchases"][PURCHASE_UUID]["name"] = "mutated"
    next(iter(result["lifecycle_events"].values()))["notes"] = "mutated"
    next(iter(result["replacement_records"].values()))["reason"] = "mutated"
    result["next_asset_number"] = 999
    assert source == snapshot
    assert _serialized(source) == before

    # Mutating the input afterwards never reaches the result.
    fresh = add_asset_archive_state(source)
    fresh_snapshot = deepcopy(fresh)
    source["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "1"
    source["assets"][ASSET_UUID]["ha_device_refs"].clear()
    source["purchases"][PURCHASE_UUID]["asset_uuids"].clear()
    next(iter(source["replacement_records"].values()))["notes"] = "later"
    source["assets"].clear()
    assert fresh == fresh_snapshot


def test_deterministic(asset_store_data_v3_1: AssetStoreData) -> None:
    first = add_asset_archive_state(asset_store_data_v3_1)
    second = add_asset_archive_state(asset_store_data_v3_1)
    assert first == second
    assert _serialized(first) == _serialized(second)


@pytest.mark.parametrize("existing", [None, ARCHIVED, "", 0])
def test_existing_archived_at_fails_closed(
    asset_store_data_v3_1: AssetStoreData, existing: Any
) -> None:
    source = deepcopy(asset_store_data_v3_1)
    source["assets"][ASSET_UUID][ARCHIVED_AT] = existing
    snapshot, before = deepcopy(source), _serialized(source)
    with pytest.raises(ArchiveCompositionError, match="already contains") as err:
        add_asset_archive_state(source)
    assert ASSET_UUID in str(err.value)
    assert "Workshop device" not in str(err.value)
    assert source == snapshot
    assert _serialized(source) == before


def test_applying_twice_fails_closed(asset_store_data_v3_1: AssetStoreData) -> None:
    with pytest.raises(ArchiveCompositionError):
        add_asset_archive_state(add_asset_archive_state(asset_store_data_v3_1))


@pytest.mark.parametrize("candidate", [None, [], "store", 3])
def test_non_mapping_candidate_fails_closed(candidate: Any) -> None:
    with pytest.raises(ArchiveCompositionError, match="must be a mapping"):
        add_asset_archive_state(candidate)


def test_missing_assets_fails_closed(asset_store_data_v3_1: AssetStoreData) -> None:
    source = deepcopy(asset_store_data_v3_1)
    del source["assets"]
    snapshot = deepcopy(source)
    with pytest.raises(ArchiveCompositionError, match="no assets"):
        add_asset_archive_state(source)
    assert source == snapshot


@pytest.mark.parametrize("assets", [None, [], "assets", 1])
def test_non_mapping_assets_fails_closed(
    asset_store_data_v3_1: AssetStoreData, assets: Any
) -> None:
    source = {**deepcopy(asset_store_data_v3_1), "assets": assets}
    with pytest.raises(ArchiveCompositionError, match="not a mapping"):
        add_asset_archive_state(source)


@pytest.mark.parametrize("record", [None, [], "asset", 1])
def test_non_mapping_asset_fails_closed(
    asset_store_data_v3_1: AssetStoreData, record: Any
) -> None:
    source = deepcopy(asset_store_data_v3_1)
    source["assets"][SECOND_UUID] = record
    snapshot = deepcopy(source)
    with pytest.raises(ArchiveCompositionError, match=f"Asset {SECOND_UUID}"):
        add_asset_archive_state(source)
    assert source == snapshot


@pytest.mark.parametrize(
    "order", [(SECOND_UUID, THIRD_UUID), (THIRD_UUID, SECOND_UUID)]
)
def test_first_invalid_asset_is_chosen_by_sorted_key(
    asset_store_data_v3_1: AssetStoreData, order: tuple[str, str]
) -> None:
    source = deepcopy(asset_store_data_v3_1)
    invalid = {
        SECOND_UUID: {**deepcopy(source["assets"][ASSET_UUID]), ARCHIVED_AT: None},
        THIRD_UUID: [],
    }
    source["assets"] = {
        **{uuid: invalid[uuid] for uuid in order},
        ASSET_UUID: source["assets"][ASSET_UUID],
    }
    with pytest.raises(ArchiveCompositionError) as err:
        add_asset_archive_state(source)
    assert str(err.value) == f"Asset {SECOND_UUID} already contains archived_at"


def test_transform_reads_no_clock_and_infers_nothing() -> None:
    source = (PACKAGE / "archive.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not attributes & {"now", "today", "utcnow", "time", "setdefault"}
    assert not names & {"utc_now_iso", "datetime", "dt_util", "hass"}


# asset_is_archived


def test_asset_is_archived(asset_store_data_v3_1: AssetStoreData) -> None:
    assert asset_is_archived(_store_4_1_asset(asset_store_data_v3_1)) is False
    assert (
        asset_is_archived(_store_4_1_asset(asset_store_data_v3_1, archived_at=ARCHIVED))
        is True
    )


def test_missing_archived_at_is_never_read_as_active(
    asset_store_data_v3_1: AssetStoreData,
) -> None:
    with pytest.raises(KeyError):
        asset_is_archived(asset_store_data_v3_1["assets"][ASSET_UUID])


# validate_asset_archive_state


@pytest.mark.parametrize(
    "archived_at",
    [
        None,
        ARCHIVED,
        "2026-09-26T12:34:56+00:00",
        "2999-01-01T00:00:00+00:00",
    ],
)
def test_valid_archive_states(
    asset_store_data_v3_1: AssetStoreData, archived_at: str | None
) -> None:
    assets = {
        ASSET_UUID: _store_4_1_asset(asset_store_data_v3_1, archived_at=archived_at)
    }
    snapshot, before = deepcopy(assets), _serialized(assets)
    validate_asset_archive_state(assets)
    assert assets == snapshot
    assert _serialized(assets) == before


def test_future_archived_at_is_valid_without_a_clock_comparison(
    asset_store_data_v3_1: AssetStoreData,
) -> None:
    assets = {
        ASSET_UUID: _store_4_1_asset(
            asset_store_data_v3_1, archived_at="2999-01-01T00:00:00+00:00"
        )
    }
    validate_asset_archive_state(assets)


@pytest.mark.parametrize(
    "archived_at",
    [
        "2026-09-26T12:34:56Z",
        "2026-09-26T12:34:56-00:00",
        "2026-09-26T12:34:56",
        "2026-09-26T12:34:56.000000+00:00",
        "2026-09-26T14:34:56+02:00",
        "2026-09-26",
        "",
        "not a timestamp",
        0,
        1.5,
        True,
        [],
        {},
    ],
)
def test_non_canonical_archived_at_fails_closed(
    asset_store_data_v3_1: AssetStoreData, archived_at: Any
) -> None:
    assets = {
        ASSET_UUID: _store_4_1_asset(
            asset_store_data_v3_1,
            archived_at=archived_at,
            deployment_state=DEPLOYMENT_STATE_NOT_DEPLOYED,
        )
    }
    snapshot = deepcopy(assets)
    with pytest.raises(ArchiveValidationError) as err:
        validate_asset_archive_state(assets)
    assert str(err.value) == (
        f"Asset {ASSET_UUID} has an invalid archived_at: not a canonical UTC timestamp"
    )
    assert err.value.__cause__ is not None
    assert assets == snapshot


def test_missing_archived_at_fails_validation(
    asset_store_data_v3_1: AssetStoreData,
) -> None:
    assets = deepcopy(asset_store_data_v3_1["assets"])
    with pytest.raises(ArchiveValidationError, match="has no archived_at"):
        validate_asset_archive_state(assets)


def test_archived_and_deployed_fails_closed(
    asset_store_data_v3_1: AssetStoreData,
) -> None:
    assets = {
        ASSET_UUID: _store_4_1_asset(
            asset_store_data_v3_1,
            archived_at=ARCHIVED,
            deployment_state=DEPLOYMENT_STATE_DEPLOYED,
        )
    }
    snapshot = deepcopy(assets)
    with pytest.raises(ArchiveValidationError, match="archived and deployed"):
        validate_asset_archive_state(assets)
    # Nothing is inferred or repaired.
    assert assets == snapshot


@pytest.mark.parametrize(
    "deployment_state",
    [
        DEPLOYMENT_STATE_DEPLOYED,
        DEPLOYMENT_STATE_NOT_DEPLOYED,
        DEPLOYMENT_STATE_UNKNOWN,
    ],
)
def test_active_asset_accepts_every_deployment_state(
    asset_store_data_v3_1: AssetStoreData, deployment_state: str
) -> None:
    validate_asset_archive_state(
        {
            ASSET_UUID: _store_4_1_asset(
                asset_store_data_v3_1, deployment_state=deployment_state
            )
        }
    )


@pytest.mark.parametrize(
    "deployment_state", [DEPLOYMENT_STATE_NOT_DEPLOYED, DEPLOYMENT_STATE_UNKNOWN]
)
def test_archived_asset_accepts_non_deployed_states(
    asset_store_data_v3_1: AssetStoreData, deployment_state: str
) -> None:
    validate_asset_archive_state(
        {
            ASSET_UUID: _store_4_1_asset(
                asset_store_data_v3_1,
                archived_at=ARCHIVED,
                deployment_state=deployment_state,
            )
        }
    )


def test_validation_owns_only_archive_state(
    asset_store_data_v3_1: AssetStoreData,
) -> None:
    """Other Asset rules belong to the whole-Store validator."""
    asset = _store_4_1_asset(asset_store_data_v3_1)
    asset["asset_id"] = "not-an-id"
    asset["runtime"] = "invalid"
    asset["unexpected"] = True
    validate_asset_archive_state({"not-a-uuid": asset})


@pytest.mark.parametrize("assets", [None, [], "assets"])
def test_non_mapping_assets_fails_validation(assets: Any) -> None:
    with pytest.raises(ArchiveValidationError, match="not a mapping"):
        validate_asset_archive_state(assets)


def test_non_mapping_asset_fails_validation(
    asset_store_data_v3_1: AssetStoreData,
) -> None:
    with pytest.raises(ArchiveValidationError, match=f"Asset {SECOND_UUID}"):
        validate_asset_archive_state(
            {ASSET_UUID: _store_4_1_asset(asset_store_data_v3_1), SECOND_UUID: []}
        )


@pytest.mark.parametrize(
    "order", [(SECOND_UUID, THIRD_UUID), (THIRD_UUID, SECOND_UUID)]
)
def test_first_invalid_state_is_chosen_by_sorted_key(
    asset_store_data_v3_1: AssetStoreData, order: tuple[str, str]
) -> None:
    invalid = {
        SECOND_UUID: _store_4_1_asset(
            asset_store_data_v3_1,
            archived_at=ARCHIVED,
            deployment_state=DEPLOYMENT_STATE_DEPLOYED,
        ),
        THIRD_UUID: _store_4_1_asset(asset_store_data_v3_1, archived_at="bad"),
    }
    with pytest.raises(
        ArchiveValidationError, match=f"Asset {SECOND_UUID} is archived"
    ):
        validate_asset_archive_state({uuid: invalid[uuid] for uuid in order})


def test_validation_error_carries_no_field_values(
    asset_store_data_v3_1: AssetStoreData,
) -> None:
    asset = _store_4_1_asset(asset_store_data_v3_1, archived_at="private-value-2026")
    with pytest.raises(ArchiveValidationError) as err:
        validate_asset_archive_state({ASSET_UUID: asset})
    assert "private-value-2026" not in str(err.value)
    assert "Workshop device" not in str(err.value)


# Composition with WP1 and the Maintenance transform


async def test_transforms_commute(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData
) -> None:
    source = await _rich_store(hass, asset_store_data_v3_1)
    snapshot, before = deepcopy(source), _serialized(source)

    archive_then_maintenance = add_maintenance_collections(
        add_asset_archive_state(source)
    )
    maintenance_then_archive = add_asset_archive_state(
        add_maintenance_collections(source)
    )

    assert archive_then_maintenance == maintenance_then_archive
    assert _serialized(archive_then_maintenance) == _serialized(
        maintenance_then_archive
    )
    assert source == snapshot
    assert _serialized(source) == before


async def test_pipeline_produces_the_store_4_1_candidate_shape(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData
) -> None:
    """3.1 validation -> WP1 preflight -> A -> M -> 4.1 validation."""
    source = await _rich_store(hass, asset_store_data_v3_1)
    _validate_store_v3_1_data(source)
    preflight_store_3_1_record_shapes(source)

    candidate = add_maintenance_collections(add_asset_archive_state(source))

    assert set(source) == STORE_3_1_TOP_LEVEL_KEYS
    assert set(candidate) == STORE_4_1_TOP_LEVEL_KEYS
    assert candidate["maintenance_schedules"] == {}
    assert candidate["maintenance_events"] == {}
    for asset_uuid, asset in candidate["assets"].items():
        assert set(source["assets"][asset_uuid]) == ASSET_KEYS_3_1
        assert set(asset) == ASSET_KEYS_4_1
        assert asset[ARCHIVED_AT] is None
    validate_asset_archive_state(candidate["assets"])

    # The candidate is the production Store 4.1 payload, never a 3.1 one.
    _validate_store_data(candidate)
    with pytest.raises(AssetStoreError, match="invalid top-level shape"):
        _validate_store_v3_1_data(candidate)  # type: ignore[arg-type]


# Production boundary


def _imported_modules(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.add(module)
            names.update(f"{module}.{alias.name}".lstrip(".") for alias in node.names)
    return names


def test_archive_module_is_pure() -> None:
    assert _imported_modules(PACKAGE / "archive.py") <= {
        "__future__",
        "__future__.annotations",
        "collections.abc",
        "collections.abc.Mapping",
        "collections.abc.MutableMapping",
        "copy",
        "copy.deepcopy",
        "dataclasses",
        "dataclasses.dataclass",
        "enum",
        "enum.StrEnum",
        "typing",
        "typing.Any",
        "canonical",
        "canonical.CanonicalValueError",
        "canonical.parse_canonical_utc",
        "canonical.require_canonical_uuid",
        "const",
        "const.CONF_DEPLOYMENT_STATE",
        "const.DEPLOYMENT_STATE_DEPLOYED",
    }


def test_archive_is_reachable_only_through_the_store_4_1_code() -> None:
    """Only storage.py imports archive, and only for the Store 4.1 scopes.

    Since WP5 the pure, unwired Maintenance mutation library also imports
    ``asset_is_archived``; its own tests prove nothing in production calls
    it. ``ARCHIVED_AT`` is used by no production module, and only storage.py
    spells ``"archived_at"``: a new Asset is created active.
    """
    future_only = frozenset(
        {
            "ArchiveCompositionError",
            "ArchiveValidationError",
            "add_asset_archive_state",
            "validate_asset_archive_state",
        }
    )
    unused = {"asset_is_archived", "ARCHIVED_AT"}
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name in {"archive.py", "store_shape.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        if path.name == "storage.py":
            assert source.count(f'"{ARCHIVED_AT}": None') == 1
            assert source.count(f'"{ARCHIVED_AT}"') == 1
        else:
            assert f'"{ARCHIVED_AT}"' not in source, path.name
        if path.name == "maintenance_mutations.py":
            assert _imported_modules(path) & {"archive", "archive.asset_is_archived"}
            assert "ARCHIVED_AT" not in source
            continue
        for symbol in unused - (
            {"asset_is_archived"} if path.name == "storage.py" else set()
        ):
            assert symbol not in source, (path.name, symbol)
        if path.name == "storage.py":
            continue
        for name in _imported_modules(path):
            assert name.split(".")[-1] not in {"archive", "store_shape"}, (
                path.name,
                name,
            )
            assert ".archive." not in f".{name}.", (path.name, name)
        for symbol in future_only:
            assert symbol not in source, (path.name, symbol)
    tree = ast.parse((PACKAGE / "storage.py").read_text(encoding="utf-8"))
    for symbol, scopes in referencing_scopes(tree, future_only).items():
        assert scopes <= STORE_4_1_SCOPES, (symbol, scopes)
    # Since WP10 the manager reads Archive state through the one authority,
    # and only in its filtering helpers, the current-management guard,
    # (since WP11) the Maintenance projection helper, and (since WP12) the
    # Runtime quarantine.
    assert referencing_scopes(tree, frozenset({"asset_is_archived"})) == {
        "asset_is_archived": {
            "AssetStoreManager.active_assets",
            "AssetStoreManager.archived_assets",
            "AssetStoreManager._require_active_asset",
            "AssetStoreManager.maintenance_projection",
            "AssetStoreManager.quarantined_runtime_subentries",
            "AssetStoreManager.register_runtime_checkpoint",
        }
    }


async def test_production_store_is_4_1_and_new_assets_are_active(
    hass: HomeAssistant,
) -> None:
    assert (STORAGE_VERSION, STORAGE_MINOR_VERSION) == (4, 1)
    assert set(_empty_store_data()) == STORE_4_1_TOP_LEVEL_KEYS
    assert storage.STORE_TOP_LEVEL_KEYS == STORE_4_1_TOP_LEVEL_KEYS

    manager = _manager(hass, _empty_store_data())
    asset = await manager.async_create_manual_asset(name="Production Asset")
    assert set(asset) == ASSET_KEYS_4_1
    assert len(asset) == 21
    assert asset[ARCHIVED_AT] is None
    assert asset_is_archived(asset) is False
    _validate_store_data(manager._data)

    without_maintenance = {
        key: value
        for key, value in _empty_store_data().items()
        if not key.startswith("maintenance_")
    }
    with pytest.raises(AssetStoreError, match="invalid top-level shape"):
        _validate_store_data(without_maintenance)
