"""Store 4.1 activation through the real Home Assistant Store load path.

Every scenario loads a raw persisted envelope through ``AssetStoreManager``
and Home Assistant's own ``Store``: migration, save, direct readback, and
publication. Nothing is published unless the 4.1 write was verified.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, UnsupportedStorageVersionError
from homeassistant.helpers.storage import Store

from custom_components.device_lifecycle import storage
from custom_components.device_lifecycle.const import (
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    STORAGE_KEY,
    AssetStoreError,
    AssetStoreManager,
    AssetStorePersistenceError,
    DeviceLifecycleStore,
    _empty_store_data,
    _migrate_v1_to_v2_1,
    _migrate_v3_1_to_v4_1,
    _validate_store_data,
)
from custom_components.device_lifecycle.store_shape import (
    ASSET_KEYS_4_1,
    PURCHASE_KEYS,
    STORE_4_1_TOP_LEVEL_KEYS,
)

from .conftest import ASSET_UUID, PURCHASE_UUID

ENVELOPE_KEYS = {"version", "minor_version", "key", "data"}
FUTURE_ARCHIVED_AT = "2999-01-01T00:00:00+00:00"


def _envelope(version: int, minor_version: int, data: Any) -> dict[str, Any]:
    return {
        "version": version,
        "minor_version": minor_version,
        "key": STORAGE_KEY,
        "data": deepcopy(data),
    }


def _bytes(envelope: Any) -> str:
    """The persisted file content, as the JSON writer would order it."""
    return json.dumps(envelope, separators=(",", ":"))


@contextmanager
def _readback(hass_storage: dict[str, Any]) -> Iterator[None]:
    """Serve Device Lifecycle's direct readback from the storage fixture."""
    with patch.object(
        storage.json_util,
        "load_json",
        side_effect=lambda _path: deepcopy(hass_storage[STORAGE_KEY]),
    ):
        yield


@contextmanager
def _save_spy() -> Iterator[Any]:
    with patch.object(
        DeviceLifecycleStore,
        "async_save",
        autospec=True,
        side_effect=DeviceLifecycleStore.async_save,
    ) as save:
        yield save


async def _assert_setup_fails_unpublished(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    error: type[Exception],
    *,
    match: str | None = None,
) -> Exception:
    """Setup fails, nothing is written, and nothing is published."""
    before = _bytes(hass_storage[STORAGE_KEY])
    manager = AssetStoreManager(hass)
    with (
        _readback(hass_storage),
        _save_spy() as save,
        pytest.raises(error, match=match) as raised,
    ):
        await manager.async_setup()
    save.assert_not_called()
    assert _bytes(hass_storage[STORAGE_KEY]) == before
    assert manager._data == _empty_store_data()
    assert manager.assets() == []
    return raised.value


def _source(request: pytest.FixtureRequest, version: tuple[int, int]) -> Any:
    if version == (1, 1):
        return request.getfixturevalue("asset_store_data_v1_1")
    if version == (1, 2):
        return request.getfixturevalue("asset_store_data_v1_2")
    if version == (2, 1):
        return _migrate_v1_to_v2_1(
            deepcopy(request.getfixturevalue("asset_store_data_v1_2")), 2
        )
    return request.getfixturevalue("asset_store_data_v3_1")


# Supported sources: one verified 4.1 write, then publication


@pytest.mark.parametrize("version", [(1, 1), (1, 2), (2, 1), (3, 1)])
async def test_supported_source_is_persisted_as_exact_store_4_1(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    request: pytest.FixtureRequest,
    version: tuple[int, int],
) -> None:
    source = _source(request, version)
    expected = await DeviceLifecycleStore(hass)._async_migrate_func(
        *version, deepcopy(source)
    )
    hass_storage[STORAGE_KEY] = _envelope(*version, source)

    manager = AssetStoreManager(hass)
    with _readback(hass_storage), _save_spy() as save:
        await manager.async_setup()

    save.assert_called_once()
    persisted = hass_storage[STORAGE_KEY]
    assert set(persisted) == ENVELOPE_KEYS
    assert (persisted["version"], persisted["minor_version"]) == (4, 1)
    assert persisted["key"] == STORAGE_KEY
    assert persisted["data"] == expected
    assert manager._data == expected
    assert set(expected) == STORE_4_1_TOP_LEVEL_KEYS
    assert expected["maintenance_schedules"] == expected["maintenance_events"] == {}
    for asset in expected["assets"].values():
        assert set(asset) == ASSET_KEYS_4_1
        assert asset["archived_at"] is None
    for purchase in expected["purchases"].values():
        assert set(purchase) == PURCHASE_KEYS
    _validate_store_data(persisted["data"])
    if version == (3, 1):
        assert expected == _migrate_v3_1_to_v4_1(source)

    # The verified 4.1 file loads again as is, without another write.
    reloaded = AssetStoreManager(hass)
    with _readback(hass_storage), _save_spy() as save:
        await reloaded.async_setup()
    save.assert_not_called()
    assert reloaded._data == expected


async def test_fresh_install_writes_store_4_1(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    assert STORAGE_KEY not in hass_storage
    manager = AssetStoreManager(hass)
    with _readback(hass_storage):
        await manager.async_setup()
        assert STORAGE_KEY not in hass_storage
        asset = await manager.async_create_manual_asset(name="First Asset")

    persisted = hass_storage[STORAGE_KEY]
    assert (persisted["version"], persisted["minor_version"]) == (4, 1)
    assert set(persisted["data"]) == STORE_4_1_TOP_LEVEL_KEYS
    stored = persisted["data"]["assets"][asset["asset_uuid"]]
    assert set(stored) == ASSET_KEYS_4_1
    assert stored["archived_at"] is None
    _validate_store_data(persisted["data"])


async def test_new_asset_after_migration_is_active(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data_v3_1: AssetStoreData,
) -> None:
    hass_storage[STORAGE_KEY] = _envelope(3, 1, asset_store_data_v3_1)
    manager = AssetStoreManager(hass)
    with _readback(hass_storage):
        await manager.async_setup()
        asset = await manager.async_create_manual_asset(name="New Asset")

    assert asset["archived_at"] is None
    persisted = hass_storage[STORAGE_KEY]["data"]
    assert persisted["assets"][asset["asset_uuid"]]["archived_at"] is None
    assert persisted["assets"][ASSET_UUID]["archived_at"] is None


# Incompatible 3.1 sources fail before any transform or write


@pytest.mark.parametrize(
    ("kind", "record_id"),
    [("assets", ASSET_UUID), ("purchases", PURCHASE_UUID)],
)
@pytest.mark.parametrize("missing", [False, True])
async def test_incompatible_source_shape_fails_and_leaves_the_file_identical(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data_v3_1: AssetStoreData,
    kind: str,
    record_id: str,
    missing: bool,
) -> None:
    source: dict[str, Any] = deepcopy(asset_store_data_v3_1)
    record = source[kind][record_id]
    if missing:
        del record["notes"]
    else:
        record["mystery_field"] = {"evidence": [1]}
    hass_storage[STORAGE_KEY] = _envelope(3, 1, source)

    with patch.object(storage, "add_asset_archive_state") as transform:
        error = await _assert_setup_fails_unpublished(
            hass, hass_storage, AssetStoreError
        )
    transform.assert_not_called()
    assert isinstance(error, AssetStoreError)
    assert error.code == "store_migration_source_incompatible"
    assert record_id in str(error)
    if not missing:
        assert hass_storage[STORAGE_KEY]["data"][kind][record_id]["mystery_field"] == {
            "evidence": [1]
        }


async def test_store_3_1_validation_precedes_the_shape_preflight(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data_v3_1: AssetStoreData,
) -> None:
    """An invalid 3.1 source is rejected by the 3.1 rules, not the preflight."""
    source: dict[str, Any] = deepcopy(asset_store_data_v3_1)
    source["assets"][ASSET_UUID]["runtime"] = {"total_seconds": "-1"}
    source["assets"][ASSET_UUID]["mystery_field"] = 1
    hass_storage[STORAGE_KEY] = _envelope(3, 1, source)

    with patch.object(storage, "preflight_store_3_1_record_shapes") as preflight:
        error = await _assert_setup_fails_unpublished(
            hass, hass_storage, AssetStoreError
        )
    preflight.assert_not_called()
    assert isinstance(error, AssetStoreError)
    assert error.code != "store_migration_source_incompatible"
    assert "Runtime" in str(error)


# Persistence failures publish nothing and are retried safely


async def test_save_failure_publishes_nothing_and_retry_migrates(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data_v3_1: AssetStoreData,
) -> None:
    hass_storage[STORAGE_KEY] = _envelope(3, 1, asset_store_data_v3_1)
    before = _bytes(hass_storage[STORAGE_KEY])
    manager = AssetStoreManager(hass)
    with (
        patch.object(
            DeviceLifecycleStore,
            "async_save",
            side_effect=AssetStorePersistenceError("write failed"),
        ),
        pytest.raises(AssetStorePersistenceError),
    ):
        await manager.async_setup()
    assert _bytes(hass_storage[STORAGE_KEY]) == before
    assert manager._data == _empty_store_data()

    retry = AssetStoreManager(hass)
    with _readback(hass_storage):
        await retry.async_setup()
    assert hass_storage[STORAGE_KEY]["version"] == 4
    assert retry._data == _migrate_v3_1_to_v4_1(asset_store_data_v3_1)


@pytest.mark.parametrize("write_landed", [False, True])
async def test_ambiguous_readback_publishes_nothing_and_retry_succeeds(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data_v3_1: AssetStoreData,
    write_landed: bool,
) -> None:
    """The readback cannot tell whether the 4.1 write landed. Setup fails;
    the retry either migrates the untouched 3.1 file again or loads the
    4.1 file that did land, without a second write."""
    original = _envelope(3, 1, asset_store_data_v3_1)
    hass_storage[STORAGE_KEY] = deepcopy(original)
    expected = _migrate_v3_1_to_v4_1(asset_store_data_v3_1)

    manager = AssetStoreManager(hass)
    with (
        patch.object(
            storage.json_util,
            "load_json",
            side_effect=HomeAssistantError("read failed"),
        ),
        pytest.raises(AssetStorePersistenceError) as raised,
    ):
        await manager.async_setup()
    assert raised.value.ambiguous is True
    assert manager._data == _empty_store_data()
    assert manager.assets() == []
    assert hass_storage[STORAGE_KEY] == _envelope(4, 1, expected)
    if not write_landed:
        hass_storage[STORAGE_KEY] = deepcopy(original)

    retry = AssetStoreManager(hass)
    with _readback(hass_storage), _save_spy() as save:
        await retry.async_setup()
    assert save.call_count == (0 if write_landed else 1)
    assert retry._data == expected
    assert hass_storage[STORAGE_KEY] == _envelope(4, 1, expected)


@pytest.mark.parametrize("write_landed", [False, True])
async def test_mismatching_readback_publishes_nothing_and_retry_succeeds(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data_v3_1: AssetStoreData,
    write_landed: bool,
) -> None:
    original = _envelope(3, 1, asset_store_data_v3_1)
    hass_storage[STORAGE_KEY] = deepcopy(original)
    expected = _migrate_v3_1_to_v4_1(asset_store_data_v3_1)

    manager = AssetStoreManager(hass)
    with (
        patch.object(
            storage.json_util,
            "load_json",
            side_effect=lambda _path: deepcopy(original),
        ),
        pytest.raises(AssetStorePersistenceError) as raised,
    ):
        await manager.async_setup()
    assert raised.value.ambiguous is False
    assert manager._data == _empty_store_data()
    if not write_landed:
        hass_storage[STORAGE_KEY] = deepcopy(original)

    retry = AssetStoreManager(hass)
    with _readback(hass_storage):
        await retry.async_setup()
    assert retry._data == expected
    assert hass_storage[STORAGE_KEY] == _envelope(4, 1, expected)


# Unsupported versions are refused without a write


@pytest.mark.parametrize("minor_version", [0, 2])
async def test_other_store_4_minor_versions_are_refused(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
    minor_version: int,
) -> None:
    hass_storage[STORAGE_KEY] = _envelope(4, minor_version, asset_store_data)
    await _assert_setup_fails_unpublished(
        hass, hass_storage, AssetStoreError, match="expected 4.1"
    )


@pytest.mark.parametrize("minor_version", [0, 1])
async def test_store_5_is_refused_by_home_assistant(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
    minor_version: int,
) -> None:
    hass_storage[STORAGE_KEY] = _envelope(5, minor_version, asset_store_data)
    with patch.object(DeviceLifecycleStore, "_async_migrate_func") as migrate:
        await _assert_setup_fails_unpublished(
            hass, hass_storage, UnsupportedStorageVersionError
        )
    migrate.assert_not_called()


async def test_a_store_3_1_reader_refuses_store_4_1(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    """A release that reads Store 3.1 cannot load a written 4.1 file, so a
    downgrade after the first 4.1 write needs the pre-upgrade backup."""
    hass_storage[STORAGE_KEY] = _envelope(4, 1, asset_store_data)
    before = _bytes(hass_storage[STORAGE_KEY])
    old_reader: Store[Any] = Store(
        hass, 3, STORAGE_KEY, private=True, atomic_writes=True, minor_version=1
    )
    with pytest.raises(UnsupportedStorageVersionError):
        await old_reader.async_load()
    assert _bytes(hass_storage[STORAGE_KEY]) == before


# Archive state invariants on load


async def test_archived_and_deployed_asset_fails_setup(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    data: dict[str, Any] = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    asset["archived_at"] = "2026-09-26T12:34:56.123456+00:00"
    asset["deployment_state"] = DEPLOYMENT_STATE_DEPLOYED
    hass_storage[STORAGE_KEY] = _envelope(4, 1, data)

    error = await _assert_setup_fails_unpublished(hass, hass_storage, AssetStoreError)
    assert isinstance(error, AssetStoreError)
    assert error.code == "store_archive_state_invalid"


async def test_future_archived_at_loads_without_a_clock_comparison(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    data: dict[str, Any] = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    asset["archived_at"] = FUTURE_ARCHIVED_AT
    asset["deployment_state"] = DEPLOYMENT_STATE_NOT_DEPLOYED
    hass_storage[STORAGE_KEY] = _envelope(4, 1, data)
    before = _bytes(hass_storage[STORAGE_KEY])

    manager = AssetStoreManager(hass)
    with _readback(hass_storage), _save_spy() as save:
        await manager.async_setup()
    save.assert_not_called()
    assert _bytes(hass_storage[STORAGE_KEY]) == before
    assert manager._data["assets"][ASSET_UUID]["archived_at"] == FUTURE_ARCHIVED_AT
