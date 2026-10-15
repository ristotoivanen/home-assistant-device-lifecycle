"""Remember an existing Store across supported reloads and failed setup retries."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage as ha_storage
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle.const import (
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    SUBENTRY_TYPE_PURCHASE,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    AssetStoreError,
    AssetStoreManager,
    AssetStorePersistenceError,
    DeviceLifecycleStore,
    _empty_store_data,
    json_util,
)

from . import test_persistence_lifecycle as lifecycle
from .conftest import ASSET_UUID
from .test_quick_add_storage import QUICK_UUID, SECOND_QUICK_UUID, _request

pytestmark = pytest.mark.real_reload


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    return hass_tmp_config_dir


@pytest.fixture(autouse=True)
def real_files():
    """Use isolated real files and the production writer and reader."""
    with (
        patch.object(DeviceLifecycleStore, "_async_write_data", lifecycle._REAL_WRITE),
        patch.object(DeviceLifecycleStore, "_async_load", lifecycle._REAL_LOAD),
    ):
        yield


def _entry(hass: HomeAssistant, purchase_data: dict[str, Any]) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Device Lifecycle",
        unique_id="device_lifecycle_main",
        version=CONFIG_ENTRY_VERSION,
        data={},
        options={},
        subentries_data=(
            {
                "subentry_type": SUBENTRY_TYPE_PURCHASE,
                "title": "Workshop equipment",
                "unique_id": None,
                "data": purchase_data,
            },
        ),
    )
    entry.add_to_hass(hass)
    return entry


async def _reject(manager: AssetStoreManager) -> None:
    with pytest.raises(AssetStoreError) as error:
        await manager.async_quick_create_asset(_request())
    assert error.value.code == "entry_not_loaded"


@pytest.mark.parametrize("preexisting_file", [False, True])
async def test_real_reload_missing_known_store_requires_backup_without_reallocation(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    preexisting_file: bool,
) -> None:
    """Neither an initial Purchase save nor an older file may become first install."""
    if preexisting_file:
        await lifecycle._seed(hass, asset_store_data)
    entry = _entry(hass, purchase_subentry_data)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    old = entry.runtime_data
    path = Path(old._store.path)
    original = path.read_bytes()
    before = deepcopy(old._data)
    assert before["assets"] and lifecycle._disk(old) == before
    next_number = before["next_asset_number"]
    path.unlink()

    failed_owners = []
    for _ in range(3):
        assert not await hass.config_entries.async_reload(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_ERROR
        assert not path.exists(), "reload must not recreate a known missing Store"
        failed = old._lifecycle.owner
        assert not failed._setup_ready
        await _reject(failed)
        failed_owners.append(failed)
    await _reject(old)
    assert not path.exists()

    path.write_bytes(original)
    assert await hass.config_entries.async_reload(entry.entry_id)
    new = entry.runtime_data
    assert new._data == lifecycle._disk(new) == before
    assert path.read_bytes() == original
    if preexisting_file:
        assert new.asset(ASSET_UUID)["notes"] == "Keep my customization"
        assert new.asset(ASSET_UUID)["runtime"]["total_seconds"] == "1000"
    for failed in failed_owners:
        await _reject(failed)
    result = await new.async_quick_create_asset(_request())
    assert result.asset["asset_id"] == f"DL{next_number:04d}"
    assert new._data == lifecycle._disk(new)
    for asset_uuid, asset in before["assets"].items():
        assert new.asset(asset_uuid) == asset
    for collection, values in before.items():
        if collection not in {"assets", "next_asset_number", "lifecycle_events"}:
            assert new._data[collection] == values
    assert before["lifecycle_events"].items() <= new._data["lifecycle_events"].items()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_first_ambiguous_quick_resolution_missing_file_does_not_claim_noncommit(
    hass: HomeAssistant,
) -> None:
    """Immediate Quick resolution needs a verified snapshot to clear uncertainty."""
    entry = await lifecycle._loaded_entry(hass)
    old = entry.runtime_data
    path = Path(old._store.path)
    load_json = json_util.load_json
    original = None

    def lose_file_before_first_readback(filename):
        nonlocal original
        if original is None:
            original = path.read_bytes()
            path.unlink()
            raise RuntimeError("injected file loss after atomic replacement")
        return load_json(filename)

    with (
        patch.object(json_util, "load_json", side_effect=lose_file_before_first_readback),
        pytest.raises(AssetStorePersistenceError) as error,
    ):
        await old.async_quick_create_asset(_request())
    assert error.value.ambiguous and old._persistence_uncertain
    assert old._data == _empty_store_data() and not path.exists()
    with pytest.raises(AssetStoreError):
        await old.async_quick_create_asset(_request(asset_uuid=SECOND_QUICK_UUID))
    assert old._persistence_uncertain and not path.exists()
    assert not await hass.config_entries.async_reload(entry.entry_id)
    assert not old._lifecycle.owner._setup_ready and not path.exists()
    assert original is not None
    path.write_bytes(original)
    assert await hass.config_entries.async_reload(entry.entry_id)
    manager = entry.runtime_data
    before = deepcopy(lifecycle._disk(manager))
    replay = await manager.async_quick_create_asset(_request())
    assert replay.replayed and manager._data == before == lifecycle._disk(manager)
    assert path.read_bytes() == original
    second = await manager.async_quick_create_asset(_request(asset_uuid=SECOND_QUICK_UUID))
    assert second.asset["asset_id"] == "DL0002"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_failed_first_load_remembers_file_across_empty_failed_owners(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
) -> None:
    """Observed file existence survives setup failure before any data is loaded."""
    seed = AssetStoreManager(hass)
    await seed._store.async_save(deepcopy(asset_store_data))
    path = Path(seed._store.path)
    original = path.read_bytes()
    entry = _entry(hass, purchase_subentry_data)
    with patch.object(
        DeviceLifecycleStore, "async_load", side_effect=OSError("injected load failure")
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    first = seed._lifecycle.owner
    assert not first._setup_ready and first._data == _empty_store_data()
    path.unlink()
    for _ in range(2):
        assert not await hass.config_entries.async_reload(entry.entry_id)
        assert not path.exists()
        assert not first._lifecycle.owner._setup_ready
        await _reject(first._lifecycle.owner)
    path.write_bytes(original)
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert lifecycle._disk(entry.runtime_data)["assets"][ASSET_UUID]["asset_id"] == "DL0007"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_known_valid_empty_store_cannot_become_unknown_first_install(
    hass: HomeAssistant,
) -> None:
    """A positive disk observation is evidence even when the snapshot is empty."""
    seed = AssetStoreManager(hass)
    await seed._store.async_save(_empty_store_data())
    entry = await lifecycle._loaded_entry(hass)
    old = entry.runtime_data
    path = Path(old._store.path)
    original = path.read_bytes()
    assert old._data == _empty_store_data()
    path.unlink()
    assert not await hass.config_entries.async_reload(entry.entry_id)
    assert not path.exists()
    await _reject(old._lifecycle.owner)
    path.write_bytes(original)
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert entry.runtime_data._data == _empty_store_data()
    result = await entry.runtime_data.async_quick_create_asset(_request())
    assert result.asset["asset_id"] == "DL0001"
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_same_owner_setup_remembers_first_acknowledged_snapshot(
    hass: HomeAssistant,
) -> None:
    """Defensive repeated setup cannot erase its own first verified creation."""
    manager = AssetStoreManager(hass)
    await manager.async_setup()
    await manager.async_quick_create_asset(_request())
    before = deepcopy(manager._data)
    path = Path(manager._store.path)
    original = path.read_bytes()
    path.unlink()
    with pytest.raises(AssetStoreError):
        await manager.async_setup()
    assert not path.exists() and not manager._setup_ready
    await _reject(manager)
    path.write_bytes(original)
    await manager.async_setup()
    assert manager._data == lifecycle._disk(manager) == before
    assert manager.asset(QUICK_UUID)["asset_id"] == "DL0001"


@pytest.mark.parametrize("reload", [False, True])
async def test_first_ambiguous_write_missing_file_stays_closed_until_backup(
    hass: HomeAssistant, reload: bool
) -> None:
    """An empty old snapshot does not prove an ambiguous first save was absent."""
    entry = await lifecycle._loaded_entry(hass)
    old = entry.runtime_data
    path = Path(old._store.path)
    assert not path.exists() and old._data == _empty_store_data()
    gate = lifecycle._Gate(hass)
    load_json = json_util.load_json
    reads = 0

    def fail_first_readback(filename):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise RuntimeError("injected lost readback acknowledgement")
        return load_json(filename)

    with (
        patch.object(ha_storage, "write_utf8_file_atomic", side_effect=gate.write),
        patch.object(json_util, "load_json", side_effect=fail_first_readback),
    ):
        mutation = asyncio.create_task(old.async_quick_create_asset(_request()))
        try:
            await asyncio.wait_for(gate.started.wait(), 5)
            mutation.cancel()
            await lifecycle._blocked(mutation)
        finally:
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await mutation
    assert old._data == _empty_store_data() and old._persistence_uncertain
    original = path.read_bytes()
    before = deepcopy(lifecycle._disk(old))
    assert before["assets"][QUICK_UUID]["asset_id"] == "DL0001"
    path.unlink()
    for _ in range(2):
        if reload:
            assert not await hass.config_entries.async_reload(entry.entry_id)
            assert not old._lifecycle.owner._setup_ready
            await _reject(old._lifecycle.owner)
        else:
            with pytest.raises(AssetStoreError):
                await old.async_quick_create_asset(_request(asset_uuid=SECOND_QUICK_UUID))
            assert old._persistence_uncertain and old._data == _empty_store_data()
        assert not path.exists(), "uncertainty cannot authorize a fresh allocator"

    path.write_bytes(original)
    if reload:
        assert await hass.config_entries.async_reload(entry.entry_id)
    manager = entry.runtime_data
    replay = await manager.async_quick_create_asset(_request())
    assert replay.replayed
    assert manager._data == lifecycle._disk(manager) == before
    assert path.read_bytes() == original
    second = await manager.async_quick_create_asset(_request(asset_uuid=SECOND_QUICK_UUID))
    assert second.asset["asset_id"] == "DL0002"
    assert manager.asset(QUICK_UUID) == before["assets"][QUICK_UUID]
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("replacement", [False, True])
@pytest.mark.parametrize("ambiguous", [False, True])
async def test_pre_setup_write_evidence_survives_later_missing_store_setup(
    hass: HomeAssistant, replacement: bool, ambiguous: bool
) -> None:
    """A manager can persist before setup; ownership absence cannot erase it."""
    old = AssetStoreManager(hass)
    assert old._lifecycle.owner is None
    if ambiguous:
        gate = lifecycle._Gate(hass)
        load_json = json_util.load_json
        reads = 0

        def fail_first_readback(filename):
            nonlocal reads
            reads += 1
            if reads == 1:
                raise RuntimeError("injected lost pre-setup acknowledgement")
            return load_json(filename)

        with (
            patch.object(ha_storage, "write_utf8_file_atomic", side_effect=gate.write),
            patch.object(json_util, "load_json", side_effect=fail_first_readback),
        ):
            mutation = asyncio.create_task(old.async_quick_create_asset(_request()))
            try:
                await asyncio.wait_for(gate.started.wait(), 5)
                mutation.cancel()
                await lifecycle._blocked(mutation)
            finally:
                gate.release.set()
                with pytest.raises(asyncio.CancelledError):
                    await mutation
        assert old._persistence_uncertain and old._data == _empty_store_data()
    else:
        acknowledged = await old.async_quick_create_asset(_request())
        assert acknowledged.asset["asset_id"] == "DL0001"
    assert old._lifecycle.owner is None
    memory = deepcopy(old._data)
    before = deepcopy(lifecycle._disk(old))
    assert before["assets"][QUICK_UUID]["asset_id"] == "DL0001"
    assert before["next_asset_number"] == 2
    path = Path(old._store.path)
    original = path.read_bytes()
    path.unlink()

    manager = AssetStoreManager(hass) if replacement else old
    with pytest.raises(AssetStoreError, match="could not be loaded safely"):
        await manager.async_setup()
    assert not path.exists() and not manager._setup_ready
    assert old._data == memory
    await _reject(manager)
    path.write_bytes(original)
    await manager.async_setup()
    assert manager._data == lifecycle._disk(manager) == before
    second = await manager.async_quick_create_asset(_request(asset_uuid=SECOND_QUICK_UUID))
    assert second.asset["asset_id"] == "DL0002"
    assert manager.asset(QUICK_UUID) == before["assets"][QUICK_UUID]
    assert manager._data["next_asset_number"] == 3
    assert manager._data == lifecycle._disk(manager)
    if replacement:
        await _reject(old)


async def test_proven_noncommit_before_setup_does_not_mark_a_store_as_existing(
    hass: HomeAssistant,
) -> None:
    """A trusted explicit noncommit result provides the required positive proof."""
    old = AssetStoreManager(hass)
    with (
        patch.object(
            old._store,
            "async_save",
            new=AsyncMock(side_effect=AssetStorePersistenceError("proven noncommit")),
        ),
        pytest.raises(AssetStorePersistenceError) as error,
    ):
        await old.async_quick_create_asset(_request())
    assert not error.value.ambiguous and not old._persistence_uncertain
    assert old._data == _empty_store_data()
    assert not Path(old._store.path).exists() and not old._lifecycle.store_seen
    manager = AssetStoreManager(hass)
    await manager.async_setup()
    assert manager._data == _empty_store_data() and not manager._lifecycle.store_seen
    result = await manager.async_quick_create_asset(_request())
    assert result.asset["asset_id"] == "DL0001"
    assert manager._data == lifecycle._disk(manager)
