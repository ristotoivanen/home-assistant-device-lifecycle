"""Exact Store record shapes and the Store 3.1 migration-source preflight."""

from __future__ import annotations

import ast
import json
from copy import deepcopy
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.device_lifecycle import storage, store_shape
from custom_components.device_lifecycle.const import (
    CONF_CURRENCY,
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
    CONF_PURCHASE_NAME,
    CONF_SOURCE_ENTITY_ID,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    LIFECYCLE_STATUS_ACTIVE,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
    WARRANTY_NONE,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    STORE_TOP_LEVEL_KEYS,
    AssetStoreManager,
    DeviceLifecycleStore,
    QuickAssetCreateRequest,
    _empty_store_data,
    _migrate_v1_to_v2_1,
    _migrate_v2_1_to_v3_1,
    _validate_store_data,
    _validate_store_v3_1_data,
)
from custom_components.device_lifecycle.store_shape import (
    ASSET_KEYS_3_1,
    ASSET_KEYS_4_1,
    PURCHASE_KEYS,
    STORE_3_1_TOP_LEVEL_KEYS,
    STORE_4_1_TOP_LEVEL_KEYS,
    StoreShapeError,
    preflight_store_3_1_record_shapes,
    require_exact_record_keys,
)

from .conftest import ASSET_UUID, DEVICE_ID, PURCHASE_UUID
from .test_store_v4_1_validation import STORE_4_1_SCOPES, referencing_scopes

QUICK_UUID = "44444444-4444-4444-8444-444444444444"
SECOND_UUID = "55555555-5555-4555-8555-555555555555"
THIRD_UUID = "66666666-6666-4666-8666-666666666666"
PACKAGE = Path(store_shape.__file__).parent


def _serialized(data: Any) -> str:
    """Deterministic structural serialization, not the on-disk formatting."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _assert_unchanged(before: Any, snapshot: Any, after: Any) -> None:
    assert after == snapshot
    assert _serialized(after) == before


def _manager(
    hass: HomeAssistant, data: AssetStoreData | None = None
) -> AssetStoreManager:
    manager = AssetStoreManager(hass)
    if data is not None:
        manager._data = deepcopy(data)
    manager._store.async_save = AsyncMock()
    return manager


def _entry(*subentries: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="device-lifecycle-entry-id",
        subentries={subentry.subentry_id: subentry for subentry in subentries},
    )


def _assert_exact_3_1_records(data: AssetStoreData) -> None:
    assert data["assets"]
    for asset in data["assets"].values():
        assert set(asset) == ASSET_KEYS_3_1
    for purchase in data["purchases"].values():
        assert set(purchase) == PURCHASE_KEYS
    preflight_store_3_1_record_shapes(data)


def _assert_exact_4_1_records(data: AssetStoreData) -> None:
    assert data["assets"]
    require_exact_record_keys(data["assets"], ASSET_KEYS_4_1, kind="assets")
    require_exact_record_keys(data["purchases"], PURCHASE_KEYS, kind="purchases")
    for asset in data["assets"].values():
        assert asset["archived_at"] is None
    _validate_store_data(data)


# Frozen constants


def test_constant_sizes_and_relations() -> None:
    assert len(STORE_3_1_TOP_LEVEL_KEYS) == 5
    assert len(STORE_4_1_TOP_LEVEL_KEYS) == 7
    assert len(ASSET_KEYS_3_1) == 20
    assert len(ASSET_KEYS_4_1) == 21
    assert len(PURCHASE_KEYS) == 12
    assert STORE_4_1_TOP_LEVEL_KEYS - STORE_3_1_TOP_LEVEL_KEYS == {
        "maintenance_schedules",
        "maintenance_events",
    }
    assert ASSET_KEYS_4_1 - ASSET_KEYS_3_1 == {"archived_at"}
    for constant in (
        STORE_3_1_TOP_LEVEL_KEYS,
        STORE_4_1_TOP_LEVEL_KEYS,
        ASSET_KEYS_3_1,
        ASSET_KEYS_4_1,
        PURCHASE_KEYS,
    ):
        assert isinstance(constant, frozenset)


def test_store_4_1_top_level_keys_match_production() -> None:
    assert STORE_4_1_TOP_LEVEL_KEYS == STORE_TOP_LEVEL_KEYS


# Producer conformance: the constants are what production actually writes


async def test_manual_asset_creation_matches_asset_4_1(hass: HomeAssistant) -> None:
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Manual Asset", notes="n")
    assert set(asset) == ASSET_KEYS_4_1
    assert asset["archived_at"] is None
    _assert_exact_4_1_records(manager._data)


async def test_quick_asset_creation_matches_asset_4_1(hass: HomeAssistant) -> None:
    manager = _manager(hass)
    result = await manager.async_quick_create_asset(
        QuickAssetCreateRequest(
            asset_uuid=QUICK_UUID,
            primary_device_id=None,
            metadata={
                "name": "Quick Asset",
                "category": None,
                "manufacturer": None,
                "model": None,
                "model_id": None,
                "serial_number": None,
                "sw_version": None,
                "hw_version": None,
                "notes": None,
            },
            field_sources={"name": "user"},
            initial_lifecycle_status=LIFECYCLE_STATUS_ACTIVE,
            initial_lifecycle_effective_date=None,
            deployment_state=DEPLOYMENT_STATE_NOT_DEPLOYED,
            installed_date=None,
            ha_area_id=None,
            warranty_type=WARRANTY_NONE,
            warranty_until=None,
            purchase_uuid=None,
        )
    )
    assert set(result.asset) == ASSET_KEYS_4_1
    _assert_exact_4_1_records(manager._data)


async def test_purchase_reconciliation_creates_exact_purchase_and_asset(
    hass: HomeAssistant,
) -> None:
    """_new_purchase, the reconciliation update, and _new_asset from a device."""
    manager = _manager(hass, _empty_store_data())
    purchase_subentry = SimpleNamespace(
        subentry_id="purchase-subentry",
        subentry_type=SUBENTRY_TYPE_PURCHASE,
        title="Purchase",
        data={
            CONF_PURCHASE_NAME: "Purchase",
            CONF_CURRENCY: "EUR",
            CONF_DEVICE_IDS: ["purchase-device"],
        },
    )
    runtime_subentry = SimpleNamespace(
        subentry_id="runtime-subentry",
        subentry_type=SUBENTRY_TYPE_RUNTIME,
        title="Runtime",
        data={
            CONF_DEVICE_ID: "runtime-device",
            CONF_SOURCE_ENTITY_ID: "switch.source",
        },
    )
    with patch.object(hass.config_entries, "async_update_subentry"):
        await manager.async_reconcile_entry(_entry(purchase_subentry, runtime_subentry))

    data = manager._data
    assert len(data["purchases"]) == 1
    assert len(data["assets"]) == 2
    _assert_exact_4_1_records(data)


async def test_reconciliation_update_of_existing_purchase_keeps_exact_shape(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
) -> None:
    manager = _manager(hass, asset_store_data)
    subentry = SimpleNamespace(
        subentry_id=asset_store_data["purchases"][PURCHASE_UUID]["config_subentry_id"],
        subentry_type=SUBENTRY_TYPE_PURCHASE,
        title="Purchase",
        data=deepcopy(purchase_subentry_data),
    )
    with patch.object(hass.config_entries, "async_update_subentry"):
        await manager.async_reconcile_entry(_entry(subentry))
    assert manager._data["assets"][ASSET_UUID]["ha_device_refs"][0]["device_id"] == (
        DEVICE_ID
    )
    _assert_exact_4_1_records(manager._data)


async def test_representative_store_fixtures_match(
    asset_store_data: AssetStoreData,
    asset_store_data_v3_1: AssetStoreData,
) -> None:
    _assert_exact_4_1_records(asset_store_data)
    _validate_store_v3_1_data(asset_store_data_v3_1)
    _assert_exact_3_1_records(asset_store_data_v3_1)


@pytest.mark.parametrize(
    ("version", "fixture_name"),
    [((1, 1), "asset_store_data_v1_1"), ((1, 2), "asset_store_data_v1_2")],
)
async def test_existing_1_x_migrations_produce_exact_records(
    hass: HomeAssistant,
    request: pytest.FixtureRequest,
    version: tuple[int, int],
    fixture_name: str,
) -> None:
    """The real Store migration callback, not a parallel implementation.

    The historical steps produce an exact Store 3.1 source, and the callback
    turns it into exact Store 4.1 records.
    """
    source = request.getfixturevalue(fixture_name)
    store_3_1 = _migrate_v2_1_to_v3_1(
        _migrate_v1_to_v2_1(deepcopy(source), version[1])
    )
    _assert_exact_3_1_records(store_3_1)
    migrated = await DeviceLifecycleStore(hass)._async_migrate_func(
        *version, deepcopy(source)
    )
    _assert_exact_4_1_records(migrated)


async def test_existing_2_1_migration_produces_exact_records(
    hass: HomeAssistant,
    asset_store_data_v1_2: AssetStoreData,
) -> None:
    """A 2.1 payload is what the production 1.x -> 2.1 step produces."""
    store_2_1 = _migrate_v1_to_v2_1(deepcopy(asset_store_data_v1_2), 2)
    _assert_exact_3_1_records(_migrate_v2_1_to_v3_1(deepcopy(store_2_1)))
    migrated = await DeviceLifecycleStore(hass)._async_migrate_func(2, 1, store_2_1)
    _assert_exact_4_1_records(migrated)


# Fail-closed behavior


def _asset(asset_store_data_v3_1: AssetStoreData, asset_uuid: str) -> dict[str, Any]:
    asset = deepcopy(asset_store_data_v3_1["assets"][ASSET_UUID])
    asset["asset_uuid"] = asset_uuid
    return asset


@pytest.mark.parametrize(
    ("change", "missing", "unexpected"),
    [
        (lambda record: record.pop("notes"), ("notes",), ()),
        (lambda record: record.update(mystery_field=1), (), ("mystery_field",)),
        (
            lambda record: record.update(zeta=1, alpha=None, archived_at=None),
            (),
            ("alpha", "archived_at", "zeta"),
        ),
        (
            lambda record: (record.pop("runtime"), record.update(extra=1)),
            ("runtime",),
            ("extra",),
        ),
    ],
)
def test_invalid_asset_key_set_fails_closed(
    asset_store_data_v3_1: AssetStoreData,
    change: Any,
    missing: tuple[str, ...],
    unexpected: tuple[str, ...],
) -> None:
    data = deepcopy(asset_store_data_v3_1)
    change(data["assets"][ASSET_UUID])
    before, snapshot = _serialized(data), deepcopy(data)
    with pytest.raises(StoreShapeError) as err:
        preflight_store_3_1_record_shapes(data)
    assert err.value.kind == "assets"
    assert err.value.record_id == ASSET_UUID
    assert err.value.missing == missing
    assert err.value.unexpected == unexpected
    assert isinstance(err.value, ValueError)
    _assert_unchanged(before, snapshot, data)


@pytest.mark.parametrize(
    ("change", "missing", "unexpected"),
    [
        (lambda record: record.pop("receipt_url"), ("receipt_url",), ()),
        (lambda record: record.update(legacy=True), (), ("legacy",)),
        (lambda record: record.update(b=1, a=2), (), ("a", "b")),
    ],
)
def test_invalid_purchase_key_set_fails_closed(
    asset_store_data_v3_1: AssetStoreData,
    change: Any,
    missing: tuple[str, ...],
    unexpected: tuple[str, ...],
) -> None:
    data = deepcopy(asset_store_data_v3_1)
    change(data["purchases"][PURCHASE_UUID])
    before, snapshot = _serialized(data), deepcopy(data)
    with pytest.raises(StoreShapeError) as err:
        preflight_store_3_1_record_shapes(data)
    assert err.value.kind == "purchases"
    assert err.value.record_id == PURCHASE_UUID
    assert err.value.missing == missing
    assert err.value.unexpected == unexpected
    _assert_unchanged(before, snapshot, data)


@pytest.mark.parametrize("kind", ["assets", "purchases"])
@pytest.mark.parametrize("value", [None, [], "record", 1])
def test_non_mapping_record_fails_closed(
    asset_store_data_v3_1: AssetStoreData, kind: str, value: Any
) -> None:
    data = deepcopy(asset_store_data_v3_1)
    record_id = ASSET_UUID if kind == "assets" else PURCHASE_UUID
    data[kind][record_id] = value
    before, snapshot = _serialized(data), deepcopy(data)
    with pytest.raises(StoreShapeError, match="not a mapping") as err:
        preflight_store_3_1_record_shapes(data)
    assert (err.value.kind, err.value.record_id) == (kind, record_id)
    assert (err.value.missing, err.value.unexpected) == ((), ())
    _assert_unchanged(before, snapshot, data)


@pytest.mark.parametrize(
    "order",
    [(SECOND_UUID, THIRD_UUID), (THIRD_UUID, SECOND_UUID)],
)
def test_first_invalid_asset_is_chosen_by_sorted_key_not_insertion(
    asset_store_data_v3_1: AssetStoreData, order: tuple[str, str]
) -> None:
    data = deepcopy(asset_store_data_v3_1)
    invalid = {
        SECOND_UUID: {**_asset(asset_store_data_v3_1, SECOND_UUID), "second": 1},
        THIRD_UUID: {**_asset(asset_store_data_v3_1, THIRD_UUID), "third": 1},
    }
    assets = {uuid: invalid[uuid] for uuid in order}
    assets[ASSET_UUID] = data["assets"][ASSET_UUID]
    data["assets"] = assets
    with pytest.raises(StoreShapeError) as err:
        preflight_store_3_1_record_shapes(data)
    assert err.value.record_id == SECOND_UUID
    assert err.value.unexpected == ("second",)


@pytest.mark.parametrize(
    "order",
    [(SECOND_UUID, THIRD_UUID), (THIRD_UUID, SECOND_UUID)],
)
def test_first_invalid_purchase_is_chosen_by_sorted_key_not_insertion(
    asset_store_data_v3_1: AssetStoreData, order: tuple[str, str]
) -> None:
    data = deepcopy(asset_store_data_v3_1)
    base = data["purchases"][PURCHASE_UUID]
    invalid = {
        SECOND_UUID: {**deepcopy(base), "purchase_uuid": SECOND_UUID, "second": 1},
        THIRD_UUID: [],
    }
    data["purchases"] = {uuid: invalid[uuid] for uuid in order}
    with pytest.raises(StoreShapeError) as err:
        preflight_store_3_1_record_shapes(data)
    assert err.value.record_id == SECOND_UUID


def test_assets_are_checked_before_purchases(asset_store_data_v3_1: AssetStoreData) -> None:
    data = deepcopy(asset_store_data_v3_1)
    data["assets"][ASSET_UUID]["asset_extra"] = 1
    data["purchases"][PURCHASE_UUID]["purchase_extra"] = 1
    with pytest.raises(StoreShapeError) as err:
        preflight_store_3_1_record_shapes(data)
    assert err.value.kind == "assets"


@pytest.mark.parametrize("data", [None, [], "store", 3])
def test_non_mapping_payload_fails_closed(data: Any) -> None:
    with pytest.raises(StoreShapeError) as err:
        preflight_store_3_1_record_shapes(data)
    assert (err.value.kind, err.value.record_id) == ("store", None)


@pytest.mark.parametrize("kind", ["assets", "purchases"])
def test_missing_collection_fails_closed(
    asset_store_data_v3_1: AssetStoreData, kind: str
) -> None:
    data = deepcopy(asset_store_data_v3_1)
    del data[kind]
    before, snapshot = _serialized(data), deepcopy(data)
    with pytest.raises(StoreShapeError, match="missing") as err:
        preflight_store_3_1_record_shapes(data)
    assert (err.value.kind, err.value.record_id) == (kind, None)
    _assert_unchanged(before, snapshot, data)


@pytest.mark.parametrize("kind", ["assets", "purchases"])
@pytest.mark.parametrize("value", [None, [], "collection", 0])
def test_non_mapping_collection_fails_closed(
    asset_store_data_v3_1: AssetStoreData, kind: str, value: Any
) -> None:
    data = deepcopy(asset_store_data_v3_1)
    data[kind] = value
    with pytest.raises(StoreShapeError, match="collection is not a mapping") as err:
        preflight_store_3_1_record_shapes(data)
    assert (err.value.kind, err.value.record_id) == (kind, None)


def test_non_string_record_keys_are_ordered_deterministically() -> None:
    records = {2: {"a": 1}, "1": {"a": 1, "b": 2}}
    with pytest.raises(StoreShapeError) as err:
        require_exact_record_keys(records, frozenset({"a"}), kind="things")
    assert err.value.record_id == "1"
    assert err.value.unexpected == ("b",)


def test_error_carries_no_field_values(asset_store_data_v3_1: AssetStoreData) -> None:
    data = deepcopy(asset_store_data_v3_1)
    secret = "private-serial-value-123"
    data["assets"][ASSET_UUID]["serial_number"] = secret
    data["assets"][ASSET_UUID]["mystery_field"] = "private-mystery-value"
    del data["assets"][ASSET_UUID]["notes"]
    with pytest.raises(StoreShapeError) as err:
        preflight_store_3_1_record_shapes(data)
    rendered = f"{err.value}{err.value.args!r}{vars(err.value)!r}"
    assert secret not in rendered
    assert "private-mystery-value" not in rendered
    assert "Workshop device" not in rendered
    assert str(err.value) == (
        f"assets record {ASSET_UUID}: record has an invalid key set; "
        "missing=['notes'], unexpected=['mystery_field']"
    )


# Unknown fields are evidence


@pytest.mark.parametrize(
    ("kind", "record_id"),
    [("assets", ASSET_UUID), ("purchases", PURCHASE_UUID)],
)
def test_unknown_field_survives_rejection(
    asset_store_data_v3_1: AssetStoreData, kind: str, record_id: str
) -> None:
    data = deepcopy(asset_store_data_v3_1)
    evidence = {"nested": ["kept", 1]}
    data[kind][record_id]["mystery_field"] = evidence
    before, snapshot = _serialized(data), deepcopy(data)
    with pytest.raises(StoreShapeError):
        preflight_store_3_1_record_shapes(data)
    assert data[kind][record_id]["mystery_field"] is evidence
    assert evidence == {"nested": ["kept", 1]}
    _assert_unchanged(before, snapshot, data)


# Non-mutation of accepted input and read-only inputs


def test_accepted_input_is_unchanged(asset_store_data_v3_1: AssetStoreData) -> None:
    data = deepcopy(asset_store_data_v3_1)
    before, snapshot = _serialized(data), deepcopy(data)
    assert preflight_store_3_1_record_shapes(data) is None
    _assert_unchanged(before, snapshot, data)


def test_read_only_mappings_are_accepted(asset_store_data_v3_1: AssetStoreData) -> None:
    frozen = MappingProxyType(
        {
            key: (
                MappingProxyType(
                    {
                        record_id: MappingProxyType(record)
                        for record_id, record in value.items()
                    }
                )
                if key in ("assets", "purchases")
                else value
            )
            for key, value in deepcopy(asset_store_data_v3_1).items()
        }
    )
    preflight_store_3_1_record_shapes(frozen)


def test_only_record_shapes_are_checked(asset_store_data_v3_1: AssetStoreData) -> None:
    """Semantic Store 3.1 validation stays with _validate_store_data."""
    data = deepcopy(asset_store_data_v3_1)
    data["assets"][ASSET_UUID]["asset_id"] = "not-an-asset-id"
    data["purchases"][PURCHASE_UUID]["asset_uuids"] = ["missing"]
    data["extra_top_level"] = {}
    del data["lifecycle_events"]
    preflight_store_3_1_record_shapes(data)


def test_empty_collections_pass() -> None:
    preflight_store_3_1_record_shapes(_empty_store_data())


# Production boundary: Store 4.1 active, shape helpers only in Store code


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


def test_store_shape_is_reachable_only_through_the_store_validators() -> None:
    """Only storage.py imports store_shape, and only for Store validation
    and migration."""
    public = {
        name
        for name in vars(store_shape)
        if not name.startswith("_")
        and getattr(getattr(store_shape, name), "__module__", store_shape.__name__)
        == store_shape.__name__
    } - {"annotations", "Any", "Iterable", "Mapping"}
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name in {"store_shape.py", "storage.py"}:
            continue
        imported = _imported_modules(path)
        assert not any(
            name.split(".")[-1] == "store_shape" or ".store_shape." in f".{name}."
            for name in imported
        ), path.name
        source = path.read_text(encoding="utf-8")
        for symbol in public:
            assert symbol not in source, (path.name, symbol)
    tree = ast.parse((PACKAGE / "storage.py").read_text(encoding="utf-8"))
    allowed = {
        "STORE_3_1_TOP_LEVEL_KEYS": {"_validate_store_v3_1_data"},
        "STORE_4_1_TOP_LEVEL_KEYS": set(STORE_4_1_SCOPES) | {"<module>"},
    }
    for symbol, scopes in referencing_scopes(tree, frozenset(public)).items():
        assert scopes <= allowed.get(symbol, STORE_4_1_SCOPES), (symbol, scopes)


def test_store_shape_is_pure() -> None:
    assert _imported_modules(PACKAGE / "store_shape.py") <= {
        "__future__",
        "__future__.annotations",
        "collections.abc",
        "collections.abc.Iterable",
        "collections.abc.Mapping",
        "typing",
        "typing.Any",
    }


def test_production_store_is_4_1(hass: HomeAssistant) -> None:
    assert (STORAGE_VERSION, STORAGE_MINOR_VERSION) == (4, 1)
    store = DeviceLifecycleStore(hass)
    assert (store.version, store.minor_version) == (4, 1)
    empty = _empty_store_data()
    assert set(empty) == STORE_4_1_TOP_LEVEL_KEYS
    assert empty["maintenance_schedules"] == empty["maintenance_events"] == {}
    assert storage.STORE_TOP_LEVEL_KEYS == STORE_4_1_TOP_LEVEL_KEYS
