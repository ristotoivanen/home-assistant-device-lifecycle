"""BL-03: recover durable Store state before any subsequent mutation."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import MethodType
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    AssetStoreManager,
    AssetStorePersistenceError,
    _validate_store_data,
    json_util,
)

from .conftest import ASSET_UUID

_ORIGINAL_ASYNC_WRITE_DATA = Store._async_write_data


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Keep real atomic writes in an isolated test configuration directory."""
    return hass_tmp_config_dir


@pytest.fixture
async def persisted_manager(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> AssetStoreManager:
    """Create a valid Store file using Home Assistant's real atomic writer."""
    manager = AssetStoreManager(hass)
    data = deepcopy(asset_store_data)
    data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "1000"
    _validate_store_data(data)
    manager._data = data
    manager._store._async_write_data = MethodType(
        _ORIGINAL_ASYNC_WRITE_DATA, manager._store
    )
    await manager._store.async_save(data)
    return manager


async def test_readback_mismatch_cannot_overwrite_newer_runtime(
    persisted_manager: AssetStoreManager,
) -> None:
    """A metadata retry must preserve every field of a different durable snapshot."""
    manager = persisted_manager
    original_read = json_util.load_json
    reads = 0

    def _readback(path: str) -> Any:
        nonlocal reads
        reads += 1
        if reads == 1:
            # Model a different valid snapshot landing after the atomic write
            # and before verification. This is a real, isolated Store file.
            persisted = original_read(path)
            asset = persisted["data"]["assets"][ASSET_UUID]
            asset["runtime"]["total_seconds"] = "2000"
            asset["notes"] = "Newer durable notes"
            asset["field_sources"]["notes"] = "user"
            _validate_store_data(persisted["data"])
            Path(path).write_text(json.dumps(persisted))
        return original_read(path)

    with patch.object(json_util, "load_json", side_effect=_readback):
        with pytest.raises(AssetStorePersistenceError):
            await manager.async_update_asset_metadata(
                ASSET_UUID, name="First metadata edit"
            )
        durable = original_read(manager._store.path)
        assert durable["data"]["assets"][ASSET_UUID]["runtime"] == {
            "total_seconds": "2000"
        }
        assert manager.runtime_total_seconds(ASSET_UUID) == 1000

        await manager.async_update_asset_metadata(ASSET_UUID, category="Second edit")

    expected = deepcopy(durable["data"])
    expected["assets"][ASSET_UUID]["category"] = "Second edit"
    expected["assets"][ASSET_UUID]["field_sources"]["category"] = "user"
    final = original_read(manager._store.path)
    # Check the observed overwrite first so RED identifies the original defect,
    # rather than merely a missing internal uncertainty flag.
    assert final["data"]["assets"][ASSET_UUID]["runtime"] == {
        "total_seconds": "2000"
    }
    assert final["data"] == expected
    assert manager._data == expected
    assert reads == 3  # failed verification, recovery, successful verification
    assert manager._persistence_uncertain is False
