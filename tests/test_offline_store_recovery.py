"""Positive evidence for offline-prepared recovery and rejected submissions."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    STORAGE_KEY,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    AssetStoreError,
    AssetStoreManager,
    DeviceLifecycleStore,
    _empty_store_data,
    _validate_store_data,
    json_util,
)

from . import test_persistence_lifecycle as lifecycle
from .conftest import ASSET_UUID
from .test_quick_add_storage import QUICK_UUID, _request

_HISTORY_UUID = "77777777-7777-4777-8777-777777777777"


@dataclass(frozen=True)
class _PreparedStore:
    """File evidence prepared before the dependent HA fixture is constructed."""

    config_dir: Path
    path: Path
    data: AssetStoreData | None
    original: bytes | None


@pytest.fixture
def prepared_store(
    request: pytest.FixtureRequest,
    hass_tmp_config_dir: str,
    asset_store_data: AssetStoreData,
) -> _PreparedStore:
    """Prepare a complete backup or a clean directory without any HA consumer."""
    config_dir = Path(hass_tmp_config_dir)
    path = config_dir / ".storage" / STORAGE_KEY
    assert not path.exists()
    if getattr(request, "param", "fresh") == "fresh":
        return _PreparedStore(config_dir, path, None, None)

    data = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    asset["runtime"]["total_seconds"] = "1000"
    asset["notes"] = "Preserve offline backup customization"
    asset["field_sources"]["notes"] = "user"
    asset["lifecycle"] = {
        "status": "active",
        "current_event_uuid": _HISTORY_UUID,
    }
    data["lifecycle_events"][_HISTORY_UUID] = {
        "event_uuid": _HISTORY_UUID,
        "asset_uuid": ASSET_UUID,
        "previous_event_uuid": None,
        "from_status": "unknown",
        "to_status": "active",
        "effective_date": "2026-01-20",
        "recorded_at": "2026-01-20T12:00:00+00:00",
        "notes": "Preserve offline backup lifecycle history",
    }
    _validate_store_data(data)
    envelope = {
        "version": STORAGE_VERSION,
        "minor_version": STORAGE_MINOR_VERSION,
        "key": STORAGE_KEY,
        "data": data,
    }
    original = json.dumps(envelope).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(original)
    return _PreparedStore(config_dir, path, data, original)


@pytest.fixture
def hass_config_dir(prepared_store: _PreparedStore) -> str:
    """HA depends on this fixture, so canonical preparation precedes its setup."""
    return str(prepared_store.config_dir)


@pytest.fixture(autouse=True)
def real_files():
    """Use real Store loading and the production atomic executor writer."""
    with (
        patch.object(DeviceLifecycleStore, "_async_write_data", lifecycle._REAL_WRITE),
        patch.object(DeviceLifecycleStore, "_async_load", lifecycle._REAL_LOAD),
    ):
        yield


@pytest.mark.parametrize("prepared_store", ["backup"], indirect=True)
async def test_offline_prepared_complete_backup_preserves_identity_and_next_id(
    hass: HomeAssistant, prepared_store: _PreparedStore
) -> None:
    """Load pre-existing backup bytes; this does not simulate an OS restart."""
    before = prepared_store.data
    assert before is not None and prepared_store.original is not None
    assert Path(hass.config.config_dir) == prepared_store.config_dir
    manager = AssetStoreManager(hass)
    with patch.object(
        manager._store, "async_save", wraps=manager._store.async_save
    ) as save:
        await manager.async_setup()
        save.assert_not_awaited()
        assert manager._data == lifecycle._disk(manager) == before
        assert prepared_store.path.read_bytes() == prepared_store.original
        assert not manager._persistence_uncertain
        assert manager.asset(ASSET_UUID)["asset_id"] == "DL0007"
        assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1000)
        assert manager._data["next_asset_number"] == 8

        result = await manager.async_quick_create_asset(_request())
        save.assert_awaited_once()

    expected = deepcopy(before)
    expected["assets"][QUICK_UUID] = result.asset
    expected["next_asset_number"] = 9
    event_uuid = result.asset["lifecycle"]["current_event_uuid"]
    assert event_uuid is not None and event_uuid != _HISTORY_UUID
    assert len(manager._data["lifecycle_events"]) == len(before["lifecycle_events"]) + 1
    expected["lifecycle_events"][event_uuid] = manager._data["lifecycle_events"][
        event_uuid
    ]
    assert result.asset["asset_id"] == "DL0008"
    assert manager._data == lifecycle._disk(manager) == expected
    assert manager.asset(ASSET_UUID) == before["assets"][ASSET_UUID]
    assert (
        manager._data["lifecycle_events"][_HISTORY_UUID]
        == before["lifecycle_events"][_HISTORY_UUID]
    )
    assert not manager._persistence_uncertain


async def test_rejected_pre_submission_request_proves_noncommit_and_allows_first_id(
    hass: HomeAssistant, prepared_store: _PreparedStore
) -> None:
    """No save was submitted; unlike failed I/O, this positively proves noncommit."""
    assert prepared_store.data is None and prepared_store.original is None
    manager = AssetStoreManager(hass)
    await manager.async_setup()
    before = deepcopy(manager._data)
    assert before == _empty_store_data() and not prepared_store.path.exists()
    with patch.object(
        manager._store, "async_save", wraps=manager._store.async_save
    ) as save:
        with pytest.raises(AssetStoreError, match="^Asset name is required$") as error:
            await manager.async_create_manual_asset(name="   ")
        assert error.value.code == "asset_store_error"
        save.assert_not_called()
        save.assert_not_awaited()
        assert manager._data == before
        assert manager._data["next_asset_number"] == 1
        assert not manager._persistence_uncertain and not prepared_store.path.exists()

        result = await manager.async_quick_create_asset(_request())
        save.assert_awaited_once()
        assert save.await_args.args[0] == manager._data

    assert result.asset["asset_uuid"] == QUICK_UUID
    assert result.asset["asset_id"] == "DL0001"
    assert manager._data["next_asset_number"] == 2
    assert manager._data["assets"] == {QUICK_UUID: result.asset}
    assert not manager._persistence_uncertain
    assert json_util.load_json(str(prepared_store.path)) == {
        "version": STORAGE_VERSION,
        "minor_version": STORAGE_MINOR_VERSION,
        "key": STORAGE_KEY,
        "data": manager._data,
    }
