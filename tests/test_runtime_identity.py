"""Canonical Runtime ConfigSubentry identity resolution (WP6)."""

from __future__ import annotations

import ast
from copy import deepcopy
from datetime import UTC, datetime
from functools import partial
from itertools import product
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from homeassistant.core import HomeAssistant

from custom_components.device_lifecycle import runtime_identity, storage
from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_DEVICE_ID,
    CONF_RUNTIME_MODE,
    CONF_SOURCE_ENTITY_ID,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.runtime_identity import (
    PRIMARY_ROLE,
    primary_device_id,
    resolve_runtime_subentry_asset,
    runtime_subentries_resolving_to,
)
from custom_components.device_lifecycle.storage import (
    DEVICE_ROLE_PRIMARY,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    AssetStoreManager,
    _empty_store_data,
    _valid_uuid,
)

from .conftest import ASSET_UUID, DEVICE_ID

ASSET_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
ASSET_B = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"
ASSET_C = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3"
UNKNOWN_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa9"
DEV_A = "device-a"
DEV_B = "device-b"
DEV_X = "device-x"
PACKAGE = Path(runtime_identity.__file__).parent


def _asset(asset_uuid: str, primary: str | None, *related: str) -> dict[str, Any]:
    refs: list[dict[str, str]] = []
    if primary is not None:
        refs.append({"device_id": primary, "role": "primary"})
    refs.extend({"device_id": device, "role": "related"} for device in related)
    return {"asset_uuid": asset_uuid, "ha_device_refs": refs}


def _assets() -> dict[str, dict[str, Any]]:
    return {
        ASSET_A: _asset(ASSET_A, DEV_A),
        ASSET_B: _asset(ASSET_B, DEV_B, DEV_X),
        ASSET_C: _asset(ASSET_C, None),
    }


def _data(device_id: Any = DEV_B, asset_uuid: Any = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        CONF_RUNTIME_MODE: "on_state",
        CONF_SOURCE_ENTITY_ID: "switch.source",
    }
    if device_id is not None:
        data[CONF_DEVICE_ID] = device_id
    if asset_uuid is not None:
        data[CONF_ASSET_UUID] = asset_uuid
    return data


def _subentry(
    subentry_id: str,
    data: dict[str, Any],
    subentry_type: str = SUBENTRY_TYPE_RUNTIME,
) -> SimpleNamespace:
    return SimpleNamespace(
        subentry_id=subentry_id,
        subentry_type=subentry_type,
        data=MappingProxyType(data),
    )


def _pre_wp6_lookup(assets: dict[str, Any], raw: dict[str, Any]) -> str | None:
    """Verbatim oracle of the lookup in _reconcile_entry_data at 5325a4c.

    It returned the Asset object or created one; the identity it chose is
    the Asset UUID, or None where it went on to create an Asset. A subentry
    without a device was skipped, which is also None here.
    """
    device_id = str(raw.get(CONF_DEVICE_ID) or "")
    if not device_id:
        return None
    asset = None
    referenced_asset_uuid = _valid_uuid(raw.get(CONF_ASSET_UUID))
    if referenced_asset_uuid is not None:
        candidate = assets.get(referenced_asset_uuid)
        if candidate is not None and primary_device_id(candidate) in (None, device_id):
            asset = candidate
    if asset is None:
        for candidate in assets.values():
            if primary_device_id(candidate) == device_id:
                asset = candidate
                break
    return None if asset is None else asset["asset_uuid"]


# Resolution matrix


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param(_data(DEV_A, ASSET_A), ASSET_A, id="named-matching-device"),
        pytest.param(_data(DEV_X, ASSET_C), ASSET_C, id="named-without-primary"),
        pytest.param(_data(DEV_B, ASSET_A), ASSET_B, id="named-contradicts-owned"),
        pytest.param(
            _data("device-free", ASSET_A), None, id="named-contradicts-unowned"
        ),
        pytest.param(_data(DEV_B), ASSET_B, id="legacy-device-only"),
        pytest.param(_data(DEV_B, UNKNOWN_UUID), ASSET_B, id="unknown-uuid"),
        pytest.param(_data("device-free"), None, id="unknown-device"),
        pytest.param(_data(None, ASSET_A), None, id="missing-device"),
        pytest.param(_data("", ASSET_A), None, id="empty-device"),
        pytest.param(_data(DEV_X), None, id="related-device-is-not-identity"),
        pytest.param(_data(DEV_B, "not-a-uuid"), ASSET_B, id="malformed-uuid"),
        pytest.param(_data(DEV_A, ASSET_A.upper()), ASSET_A, id="uppercase-uuid-hint"),
        pytest.param(_data(DEV_A, 7), ASSET_A, id="non-string-uuid-hint"),
    ],
)
def test_resolution_matrix(data: dict[str, Any], expected: str | None) -> None:
    assets = _assets()
    before = (deepcopy(assets), deepcopy(data))
    assert resolve_runtime_subentry_asset(assets, data) == expected
    assert _pre_wp6_lookup(assets, data) == expected
    assert (assets, data) == before


def test_archived_asset_is_identified() -> None:
    """Identity lookup includes archived Assets; eligibility is the caller's."""
    assets = _assets()
    assets[ASSET_B]["archived_at"] = "2026-09-26T12:00:00+00:00"
    assert resolve_runtime_subentry_asset(assets, _data(DEV_B)) == ASSET_B
    assert resolve_runtime_subentry_asset(assets, _data(DEV_B, ASSET_B)) == ASSET_B


def test_read_only_inputs_are_accepted() -> None:
    assets = MappingProxyType(
        {key: MappingProxyType(value) for key, value in _assets().items()}
    )
    assert (
        resolve_runtime_subentry_asset(assets, MappingProxyType(_data(DEV_B)))
        == ASSET_B
    )


# Exhaustive equivalence with the pre-WP6 reconciliation lookup


_DEVICE_VALUES = [None, "", DEV_A, DEV_B, DEV_X, "device-free", 0, 5]
_UUID_VALUES = [
    None,
    "",
    ASSET_A,
    ASSET_B,
    ASSET_C,
    UNKNOWN_UUID,
    ASSET_A.upper(),
    "{" + ASSET_B + "}",
    ASSET_C.replace("-", ""),
    "not-a-uuid",
    0,
    7,
    ["x"],
]


@pytest.mark.parametrize("assets_order", ["forward", "reversed"])
def test_resolver_equals_the_pre_wp6_lookup(assets_order: str) -> None:
    assets = _assets()
    if assets_order == "reversed":
        assets = dict(reversed(list(assets.items())))
    for device_value, uuid_value in product(_DEVICE_VALUES, _UUID_VALUES):
        data = _data(device_value, uuid_value)
        assert resolve_runtime_subentry_asset(assets, data) == _pre_wp6_lookup(
            assets, data
        ), (device_value, uuid_value)


def test_uuid_hint_parsing_equals_storage() -> None:
    for value in [*_UUID_VALUES, UUID(ASSET_A), b"bytes", 1.5, {}]:
        assert runtime_identity._asset_uuid_hint(value) == _valid_uuid(value), value


def test_primary_device_definition_is_shared_with_storage() -> None:
    assert PRIMARY_ROLE == DEVICE_ROLE_PRIMARY
    assert storage._primary_device_id is primary_device_id
    assert primary_device_id(_asset(ASSET_A, DEV_A, DEV_X)) == DEV_A
    assert primary_device_id(_asset(ASSET_A, None, DEV_X)) is None
    assert (
        primary_device_id({"ha_device_refs": [{"role": "primary", "device_id": ""}]})
        is None
    )
    assert primary_device_id({}) is None


# runtime_subentries_resolving_to


def test_no_subentries() -> None:
    assert runtime_subentries_resolving_to([], _assets(), ASSET_B) == ()


def test_subentries_resolving_to_an_asset() -> None:
    assets = _assets()
    archived = deepcopy(assets)
    archived[ASSET_B]["archived_at"] = "2026-09-26T12:00:00+00:00"
    subentries = [
        _subentry("runtime-3", _data(DEV_B, ASSET_B)),
        _subentry("runtime-1", _data(DEV_B)),
        _subentry("runtime-2", _data(DEV_A, ASSET_A)),
        _subentry("runtime-4", _data("device-free")),
        _subentry("purchase-1", {"device_ids": [DEV_B]}, SUBENTRY_TYPE_PURCHASE),
        _subentry("runtime-5", _data(DEV_B, ASSET_A)),
    ]
    before = deepcopy([dict(subentry.data) for subentry in subentries])
    expected = ("runtime-1", "runtime-3", "runtime-5")
    assert runtime_subentries_resolving_to(subentries, assets, ASSET_B) == expected
    assert (
        runtime_subentries_resolving_to(list(reversed(subentries)), assets, ASSET_B)
        == expected
    )
    assert runtime_subentries_resolving_to(subentries, archived, ASSET_B) == expected
    assert runtime_subentries_resolving_to(subentries, assets, ASSET_A) == (
        "runtime-2",
    )
    assert runtime_subentries_resolving_to(subentries, assets, ASSET_C) == ()
    assert [dict(subentry.data) for subentry in subentries] == before


def test_single_subentry_and_duplicates() -> None:
    subentry = _subentry("runtime-1", _data(DEV_B))
    assert runtime_subentries_resolving_to(
        [subentry, subentry], _assets(), ASSET_B
    ) == ("runtime-1",)


# Reconciliation still behaves exactly as before


def _manager(hass: HomeAssistant, data: AssetStoreData) -> AssetStoreManager:
    manager = AssetStoreManager(hass)
    manager._data = deepcopy(data)
    manager._store.async_save = AsyncMock()
    return manager


def _entry(*subentries: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="device-lifecycle-entry-id",
        subentries={subentry.subentry_id: subentry for subentry in subentries},
    )


async def _reconcile(
    manager: AssetStoreManager, *subentries: SimpleNamespace
) -> dict[str, dict[str, Any]]:
    with patch.object(manager.hass.config_entries, "async_update_subentry") as update:
        await manager.async_reconcile_entry(_entry(*subentries))
    return {
        call.args[1].subentry_id: dict(call.kwargs["data"])
        for call in update.call_args_list
    }


async def test_unknown_device_still_creates_one_asset(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, asset_store_data)
    raw = _data("new-runtime-device")
    rewrites = await _reconcile(manager, _subentry("runtime-new", raw))

    created = [a for key, a in manager._data["assets"].items() if key != ASSET_UUID]
    assert len(created) == 1
    new_asset = created[0]
    assert primary_device_id(new_asset) == "new-runtime-device"
    assert new_asset["field_sources"]["name"] == "home_assistant"
    assert rewrites == {
        "runtime-new": {**raw, CONF_ASSET_UUID: new_asset["asset_uuid"]}
    }
    # Reconciling again finds the created Asset and allocates nothing.
    count = len(manager._data["assets"])
    rewritten = _subentry("runtime-new", rewrites["runtime-new"])
    assert await _reconcile(manager, rewritten) == {}
    assert len(manager._data["assets"]) == count


async def test_legacy_device_only_subentry_resolves_and_is_rewritten(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, asset_store_data)
    before_assets = deepcopy(manager._data["assets"])
    raw = _data(DEVICE_ID)

    rewrites = await _reconcile(manager, _subentry("runtime-legacy", raw))

    assert rewrites == {"runtime-legacy": {**raw, CONF_ASSET_UUID: ASSET_UUID}}
    assert list(rewrites["runtime-legacy"]) == [*raw, CONF_ASSET_UUID]
    assert manager._data["assets"].keys() == before_assets.keys()
    assert (
        manager._data["assets"][ASSET_UUID]["ha_device_refs"]
        == before_assets[ASSET_UUID]["ha_device_refs"]
    )


async def test_named_asset_without_primary_gets_the_device(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data = deepcopy(asset_store_data)
    data["assets"][ASSET_UUID]["ha_device_refs"] = []
    data["purchases"] = {}
    data["assets"][ASSET_UUID]["purchase_uuid"] = None
    del data["assets"][ASSET_UUID]["field_sources"]["purchase_uuid"]
    manager = _manager(hass, data)
    raw = _data("runtime-device", ASSET_UUID)

    rewrites = await _reconcile(manager, _subentry("runtime-named", raw))

    assert rewrites == {}
    assert list(manager._data["assets"]) == [ASSET_UUID]
    assert primary_device_id(manager._data["assets"][ASSET_UUID]) == "runtime-device"


async def test_contradicting_named_asset_follows_the_device(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, asset_store_data)
    raw = _data("unowned-device", ASSET_UUID)

    rewrites = await _reconcile(manager, _subentry("runtime-moved", raw))

    created = [key for key in manager._data["assets"] if key != ASSET_UUID]
    assert len(created) == 1
    assert rewrites == {"runtime-moved": {**raw, CONF_ASSET_UUID: created[0]}}
    assert primary_device_id(manager._data["assets"][ASSET_UUID]) == DEVICE_ID


async def test_subentry_without_device_is_still_skipped(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, asset_store_data)
    before = deepcopy(manager._data["assets"])
    assert await _reconcile(manager, _subentry("runtime-broken", _data(None))) == {}
    assert manager._data["assets"] == before


class _FrozenDatetime(datetime):
    """Observed timestamps are identical in both runs."""

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


_SCENARIOS = {
    "named-matching": [("runtime-1", _data(DEVICE_ID, ASSET_UUID))],
    "legacy-device-only": [("runtime-1", _data(DEVICE_ID))],
    "unknown-device": [("runtime-1", _data("new-device"))],
    "contradicting-named": [("runtime-1", _data("new-device", ASSET_UUID))],
    "unknown-uuid": [("runtime-1", _data(DEVICE_ID, UNKNOWN_UUID))],
    "uppercase-hint": [("runtime-1", _data(DEVICE_ID, ASSET_UUID.upper()))],
    "missing-device": [("runtime-1", _data(None, ASSET_UUID))],
    "several": [
        ("runtime-b", _data("new-device")),
        ("runtime-a", _data(DEVICE_ID)),
        ("runtime-c", _data("new-device")),
        ("runtime-d", _data("other-device", ASSET_UUID)),
    ],
}


@pytest.mark.parametrize("scenario", sorted(_SCENARIOS))
async def test_reconciliation_equals_the_pre_wp6_lookup(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    """Run each scenario with the resolver and with the old lookup oracle."""
    results = []
    for lookup in (resolve_runtime_subentry_asset, _pre_wp6_lookup):
        uuids = iter(
            UUID(f"dddddddd-dddd-4ddd-8ddd-{index:012d}") for index in range(1, 50)
        )
        next_uuid = partial(next, uuids)
        monkeypatch.setattr(storage, "uuid4", next_uuid)
        monkeypatch.setattr(storage, "uuid", SimpleNamespace(uuid4=next_uuid))
        monkeypatch.setattr(storage, "datetime", _FrozenDatetime)
        monkeypatch.setattr(storage, "resolve_runtime_subentry_asset", lookup)
        manager = _manager(hass, asset_store_data)
        subentries = [_subentry(key, data) for key, data in _SCENARIOS[scenario]]
        rewrites = await _reconcile(manager, *subentries)
        results.append((manager._data, rewrites))
    assert results[0] == results[1]


# Boundaries


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
    return names


def test_module_is_pure_and_identity_only() -> None:
    path = PACKAGE / "runtime_identity.py"
    assert _imports(path) == {
        "__future__",
        "collections.abc",
        "typing",
        "uuid",
        ".const",
    }
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assert not any(isinstance(node, ast.AsyncFunctionDef) for node in ast.walk(tree))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert not names & {"hass", "dt_util", "datetime", "asset_is_archived"}
    assert not attributes & {"now", "today", "async_get", "async_update_subentry"}
    # Archive-blind: the Archive field is never consulted.
    assert "archived_at" not in path.read_text(encoding="utf-8")


def test_only_storage_consumes_the_resolver() -> None:
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name in {"runtime_identity.py", "storage.py"}:
            continue
        assert "runtime_identity" not in path.read_text(encoding="utf-8"), path.name


def test_reconciliation_has_one_copy_of_the_rule() -> None:
    """The asset_uuid-versus-primary-device decision lives only in the resolver."""
    source = (PACKAGE / "storage.py").read_text(encoding="utf-8")
    assert "referenced_asset_uuid" not in source
    assert 'resolve_runtime_subentry_asset(data["assets"], raw)' in source


def test_production_store_is_4_1() -> None:
    assert (STORAGE_VERSION, STORAGE_MINOR_VERSION) == (4, 1)
    assert set(_empty_store_data()) == {
        "next_asset_number",
        "purchases",
        "assets",
        "lifecycle_events",
        "replacement_records",
        "maintenance_schedules",
        "maintenance_events",
    }
