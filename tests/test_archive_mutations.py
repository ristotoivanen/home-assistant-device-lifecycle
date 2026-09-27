"""Archive and Restore snapshot mutations (WP4)."""

from __future__ import annotations

import ast
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from custom_components.device_lifecycle import archive, storage
from custom_components.device_lifecycle.archive import (
    ARCHIVED_AT,
    ArchiveAssetRequest,
    ArchiveMutationError,
    ArchiveOutcome,
    RestoreAssetRequest,
    apply_archive_request,
    archive_state_matches,
    validate_asset_archive_state,
)
from custom_components.device_lifecycle.const import (
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DEPLOYMENT_STATE_UNKNOWN,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    _empty_store_data,
    _migrate_v3_1_to_v4_1,
    _validate_store_data,
)
from custom_components.device_lifecycle.store_shape import (
    ASSET_KEYS_4_1,
    STORE_4_1_TOP_LEVEL_KEYS,
)

from .conftest import ASSET_UUID
from .test_lifecycle import _manager
from .test_maintenance_validation import _event, _schedule
from .test_store_v4_1_validation import _rich_store, referencing_scopes

NOW = "2026-09-26T12:34:56.123456+00:00"
EARLIER = "2026-01-01T00:00:00+00:00"
FUTURE = "2999-01-01T00:00:00+00:00"
MISSING_UUID = "99999999-9999-4999-8999-999999999999"
SCHEDULE_UUID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
EVENT_UUID = "cccccccc-cccc-4ccc-8ccc-ccccccccccc1"
PACKAGE = Path(archive.__file__).parent
WP4_SYMBOLS = frozenset(
    {
        "ArchiveOutcome",
        "ArchiveMutationError",
        "ArchiveAssetRequest",
        "RestoreAssetRequest",
        "ArchiveRequest",
        "apply_archive_request",
        "archive_state_matches",
    }
)


def _serialized(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


async def _store(hass: HomeAssistant, data: AssetStoreData) -> dict[str, Any]:
    """A rich valid Store 4.1 candidate: two Assets, Purchase, Runtime,
    Lifecycle, Replacement, and Maintenance."""
    store = _migrate_v3_1_to_v4_1(await _rich_store(hass, data))
    store["maintenance_schedules"] = {
        SCHEDULE_UUID: _schedule(SCHEDULE_UUID, ASSET_UUID)
    }
    store["maintenance_events"] = {
        EVENT_UUID: _event(EVENT_UUID, ASSET_UUID, [SCHEDULE_UUID])
    }
    _validate_store_data(store)
    assert len(store["assets"]) == 2
    return store


def _active(store: dict[str, Any], deployment_state: str) -> dict[str, Any]:
    store["assets"][ASSET_UUID]["deployment_state"] = deployment_state
    return store


def _archived(store: dict[str, Any], archived_at: str = EARLIER) -> dict[str, Any]:
    asset = store["assets"][ASSET_UUID]
    asset["deployment_state"] = DEPLOYMENT_STATE_NOT_DEPLOYED
    asset[ARCHIVED_AT] = archived_at
    return store


def _only_archived_at_changed(before: dict[str, Any], after: dict[str, Any]) -> None:
    expected = deepcopy(before)
    expected["assets"][ASSET_UUID][ARCHIVED_AT] = after["assets"][ASSET_UUID][
        ARCHIVED_AT
    ]
    assert after == expected
    assert _serialized(after) == _serialized(expected)


# Requests


@pytest.mark.parametrize("request_type", [ArchiveAssetRequest, RestoreAssetRequest])
def test_request_accepts_canonical_uuid(request_type: type) -> None:
    assert request_type(ASSET_UUID).asset_uuid == ASSET_UUID


@pytest.mark.parametrize("request_type", [ArchiveAssetRequest, RestoreAssetRequest])
@pytest.mark.parametrize(
    "asset_uuid",
    [
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1".upper(),
        "{" + ASSET_UUID + "}",
        ASSET_UUID.replace("-", ""),
        f"urn:uuid:{ASSET_UUID}",
        "not-a-uuid",
        "",
        None,
        1,
    ],
)
def test_request_rejects_non_canonical_uuid(
    request_type: type, asset_uuid: Any
) -> None:
    with pytest.raises(ArchiveMutationError) as err:
        request_type(asset_uuid)
    assert err.value.code == "archive_request_invalid"
    assert err.value.__cause__ is not None


def test_requests_are_frozen() -> None:
    request = ArchiveAssetRequest(ASSET_UUID)
    with pytest.raises(AttributeError):
        request.asset_uuid = MISSING_UUID  # type: ignore[misc]


@pytest.mark.parametrize(
    "request_value", [None, "archive", object(), {"asset_uuid": ASSET_UUID}]
)
def test_unknown_request_type_fails_closed(
    asset_store_data_v3_1: AssetStoreData, request_value: Any
) -> None:
    assets = _migrate_v3_1_to_v4_1(asset_store_data_v3_1)["assets"]
    before = deepcopy(assets)
    with pytest.raises(TypeError, match="Unknown Archive request"):
        apply_archive_request(assets, request_value, observed_utc=NOW)
    with pytest.raises(TypeError, match="Unknown Archive request"):
        archive_state_matches(assets[ASSET_UUID], request_value)
    assert assets == before


# Archive


@pytest.mark.parametrize(
    "deployment_state", [DEPLOYMENT_STATE_NOT_DEPLOYED, DEPLOYMENT_STATE_UNKNOWN]
)
async def test_archive_changes_only_archived_at(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData, deployment_state: str
) -> None:
    store = _active(await _store(hass, asset_store_data_v3_1), deployment_state)
    before = deepcopy(store)
    assets = store["assets"]
    other_asset = next(value for key, value in assets.items() if key != ASSET_UUID)
    runtime = assets[ASSET_UUID]["runtime"]

    outcome = apply_archive_request(
        assets, ArchiveAssetRequest(ASSET_UUID), observed_utc=NOW
    )

    assert outcome is ArchiveOutcome.CHANGED
    assert assets[ASSET_UUID][ARCHIVED_AT] == NOW
    _only_archived_at_changed(before, store)
    # The detached candidate is changed in place; nothing else is replaced.
    assert store["assets"] is assets
    assert assets[ASSET_UUID]["runtime"] is runtime
    assert next(v for k, v in assets.items() if k != ASSET_UUID) is other_asset
    validate_asset_archive_state(assets)
    _validate_store_data(store)


async def test_future_observed_time_is_accepted(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData
) -> None:
    store = _active(await _store(hass, asset_store_data_v3_1), DEPLOYMENT_STATE_NOT_DEPLOYED)
    outcome = apply_archive_request(
        store["assets"], ArchiveAssetRequest(ASSET_UUID), observed_utc=FUTURE
    )
    assert outcome is ArchiveOutcome.CHANGED
    assert store["assets"][ASSET_UUID][ARCHIVED_AT] == FUTURE


async def test_deployed_asset_cannot_be_archived(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData
) -> None:
    store = _active(await _store(hass, asset_store_data_v3_1), DEPLOYMENT_STATE_DEPLOYED)
    before = deepcopy(store)
    with pytest.raises(ArchiveMutationError) as err:
        apply_archive_request(
            store["assets"], ArchiveAssetRequest(ASSET_UUID), observed_utc=NOW
        )
    assert err.value.code == "archive_asset_deployed"
    assert ASSET_UUID in str(err.value)
    # Nothing is undeployed, cleared, or inferred.
    assert store == before


async def test_deployed_check_precedes_timestamp_validation(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData
) -> None:
    store = _active(await _store(hass, asset_store_data_v3_1), DEPLOYMENT_STATE_DEPLOYED)
    with pytest.raises(ArchiveMutationError) as err:
        apply_archive_request(
            store["assets"], ArchiveAssetRequest(ASSET_UUID), observed_utc="bad"
        )
    assert err.value.code == "archive_asset_deployed"


@pytest.mark.parametrize(
    "observed_utc",
    [
        "2026-09-26T12:34:56Z",
        "2026-09-26T12:34:56",
        "2026-09-26T14:34:56+02:00",
        "2026-09-26T12:34:56-00:00",
        "2026-09-26T12:34:56.000000+00:00",
        "",
        None,
        0,
    ],
)
async def test_invalid_observed_time_fails_before_mutation(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData, observed_utc: Any
) -> None:
    store = _active(await _store(hass, asset_store_data_v3_1), DEPLOYMENT_STATE_NOT_DEPLOYED)
    before = deepcopy(store)
    with pytest.raises(ArchiveMutationError) as err:
        apply_archive_request(
            store["assets"], ArchiveAssetRequest(ASSET_UUID), observed_utc=observed_utc
        )
    assert err.value.code == "archive_observed_utc_invalid"
    assert err.value.__cause__ is not None
    assert store == before


@pytest.mark.parametrize("archived_at", [EARLIER, FUTURE])
async def test_archive_of_archived_asset_is_no_op_and_keeps_timestamp(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData, archived_at: str
) -> None:
    store = _archived(await _store(hass, asset_store_data_v3_1), archived_at)
    before, serialized = deepcopy(store), _serialized(store)

    outcome = apply_archive_request(
        store["assets"], ArchiveAssetRequest(ASSET_UUID), observed_utc=NOW
    )

    assert outcome is ArchiveOutcome.NO_OP
    assert store["assets"][ASSET_UUID][ARCHIVED_AT] == archived_at
    assert store == before
    assert _serialized(store) == serialized


async def test_archive_no_op_precedes_timestamp_validation(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData
) -> None:
    store = _archived(await _store(hass, asset_store_data_v3_1))
    before = deepcopy(store)
    outcome = apply_archive_request(
        store["assets"], ArchiveAssetRequest(ASSET_UUID), observed_utc="bad"
    )
    assert outcome is ArchiveOutcome.NO_OP
    assert store == before


@pytest.mark.parametrize("request_type", [ArchiveAssetRequest, RestoreAssetRequest])
async def test_missing_asset_is_not_found(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData, request_type: type
) -> None:
    store = await _store(hass, asset_store_data_v3_1)
    before = deepcopy(store)
    with pytest.raises(ArchiveMutationError) as err:
        apply_archive_request(
            store["assets"], request_type(MISSING_UUID), observed_utc=NOW
        )
    assert err.value.code == "archive_asset_not_found"
    assert MISSING_UUID in str(err.value)
    assert "Workshop device" not in str(err.value)
    assert store == before


# Restore


@pytest.mark.parametrize("observed_utc", [NOW, None, "not validated", 0])
async def test_restore_clears_archived_at_and_ignores_observed_time(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData, observed_utc: Any
) -> None:
    store = _archived(await _store(hass, asset_store_data_v3_1))
    before = deepcopy(store)

    outcome = apply_archive_request(
        store["assets"], RestoreAssetRequest(ASSET_UUID), observed_utc=observed_utc
    )

    assert outcome is ArchiveOutcome.CHANGED
    assert store["assets"][ASSET_UUID][ARCHIVED_AT] is None
    _only_archived_at_changed(before, store)
    # The previous timestamp is not kept anywhere.
    assert EARLIER not in _serialized(store)
    _validate_store_data(store)


async def test_restore_of_active_asset_is_no_op(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData
) -> None:
    store = _active(await _store(hass, asset_store_data_v3_1), DEPLOYMENT_STATE_DEPLOYED)
    before, serialized = deepcopy(store), _serialized(store)
    outcome = apply_archive_request(
        store["assets"], RestoreAssetRequest(ASSET_UUID), observed_utc=None
    )
    assert outcome is ArchiveOutcome.NO_OP
    assert store == before
    assert _serialized(store) == serialized


def test_restore_has_no_deployment_precondition() -> None:
    """Restore never reads Deployment, even of a record that is otherwise invalid."""
    assets = {ASSET_UUID: {ARCHIVED_AT: EARLIER}}
    outcome = apply_archive_request(
        assets, RestoreAssetRequest(ASSET_UUID), observed_utc=None
    )
    assert outcome is ArchiveOutcome.CHANGED
    assert assets == {ASSET_UUID: {ARCHIVED_AT: None}}


async def test_archive_then_restore_round_trip(
    hass: HomeAssistant, asset_store_data_v3_1: AssetStoreData
) -> None:
    store = _active(await _store(hass, asset_store_data_v3_1), DEPLOYMENT_STATE_NOT_DEPLOYED)
    original = deepcopy(store)
    apply_archive_request(
        store["assets"], ArchiveAssetRequest(ASSET_UUID), observed_utc=NOW
    )
    apply_archive_request(
        store["assets"], RestoreAssetRequest(ASSET_UUID), observed_utc=None
    )
    assert store == original


# Strict Archive field access


@pytest.mark.parametrize("request_type", [ArchiveAssetRequest, RestoreAssetRequest])
def test_missing_archived_at_is_never_read_as_active(
    asset_store_data_v3_1: AssetStoreData, request_type: type
) -> None:
    assets = deepcopy(asset_store_data_v3_1["assets"])
    before = deepcopy(assets)
    with pytest.raises(KeyError):
        apply_archive_request(assets, request_type(ASSET_UUID), observed_utc=NOW)
    with pytest.raises(KeyError):
        archive_state_matches(assets[ASSET_UUID], request_type(ASSET_UUID))
    assert assets == before


# State matcher for ambiguous persistence


@pytest.mark.parametrize(
    ("archived_at", "archive_matches", "restore_matches"),
    [
        (None, False, True),
        (NOW, True, False),
        (EARLIER, True, False),
        (FUTURE, True, False),
    ],
)
def test_archive_state_matches(
    archived_at: str | None, archive_matches: bool, restore_matches: bool
) -> None:
    asset = {ARCHIVED_AT: archived_at}
    assert (
        archive_state_matches(asset, ArchiveAssetRequest(ASSET_UUID)) is archive_matches
    )
    assert (
        archive_state_matches(asset, RestoreAssetRequest(ASSET_UUID)) is restore_matches
    )


def test_state_matcher_ignores_the_timestamp_and_other_facts() -> None:
    """Archive requested at A, persisted at B: the state matches."""
    requested, persisted = NOW, EARLIER
    assert requested != persisted
    asset = {
        ARCHIVED_AT: persisted,
        "deployment_state": DEPLOYMENT_STATE_DEPLOYED,
        "runtime": {"total_seconds": None},
    }
    before = deepcopy(asset)
    assert archive_state_matches(asset, ArchiveAssetRequest(ASSET_UUID))
    assert asset == before


# Production reachability


def _production_trees() -> dict[str, ast.Module]:
    return {
        path.name: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(PACKAGE.glob("*.py"))
        if path.name != "archive.py"
    }


WP4_MANAGER_SCOPES = {
    "AssetStoreManager.archive_blockers",
    "AssetStoreManager.async_archive_asset",
    "AssetStoreManager.async_restore_asset",
    "AssetStoreManager._apply_archive_request",
    "AssetStoreManager._archive_state_resolver",
    "AssetStoreManager.async_reserve_runtime_binding",
    "AssetStoreManager.runtime_binding_target",
}


def test_wp4_symbols_are_used_only_by_the_store_manager_api() -> None:
    """Since WP13 the Store manager's Archive and Restore API is the only
    production user of the WP4 mutations."""
    for name, tree in _production_trees().items():
        scopes = referencing_scopes(tree, WP4_SYMBOLS)
        if name == "config_flow.py":
            # Since WP15 the OptionsFlow reads the manager API's result type,
            # and only that, in its Archive and Restore steps.
            assert scopes == {
                "ArchiveOutcome": {
                    "DeviceLifecycleOptionsFlow.async_step_confirm_archive_asset",
                    "DeviceLifecycleOptionsFlow._async_archive",
                    "DeviceLifecycleOptionsFlow.async_step_confirm_restore_asset",
                }
            }
            continue
        if name != "storage.py":
            assert scopes == {}, name
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    imported = {alias.name for alias in node.names}
                    assert not imported & WP4_SYMBOLS, (name, imported)
            continue
        for symbol, users in scopes.items():
            assert users <= WP4_MANAGER_SCOPES, (symbol, users)


def test_archive_module_owns_no_runtime_or_home_assistant_logic() -> None:
    tree = ast.parse((PACKAGE / "archive.py").read_text(encoding="utf-8"))
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {
        node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
    }
    assert not {name for name in names | attributes if "runtime" in name.lower()}
    assert not names & {"hass", "dt_util", "datetime", "utc_now_iso", "ConfigEntry"}
    assert not attributes & {"now", "today", "utcnow", "subentries", "async_save"}


async def test_production_store_is_4_1_without_archive_management(
    hass: HomeAssistant,
) -> None:
    """Store 4.1 is active and new Assets are active; only the Store
    manager's API archives or restores one."""
    assert (STORAGE_VERSION, STORAGE_MINOR_VERSION) == (4, 1)
    assert set(_empty_store_data()) == STORE_4_1_TOP_LEVEL_KEYS
    asset = await _manager(hass, _empty_store_data()).async_create_manual_asset(
        name="Production Asset"
    )
    assert set(asset) == ASSET_KEYS_4_1
    assert asset[ARCHIVED_AT] is None
    scopes = referencing_scopes(
        ast.parse((PACKAGE / "storage.py").read_text(encoding="utf-8")),
        WP4_SYMBOLS,
    )
    assert set().union(*scopes.values()) <= WP4_MANAGER_SCOPES
    assert storage.STORE_TOP_LEVEL_KEYS == STORE_4_1_TOP_LEVEL_KEYS
