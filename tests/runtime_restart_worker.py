"""Fresh-process HA startup from the source-identity test's synthetic disk files.

Invoked explicitly by test_runtime_source_identity.py, never against user data.
Core ConfigEntries and both registries load through real version-pinned Store I/O.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant import bootstrap
from homeassistant.config_entries import SOURCE_RECONFIGURE
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import category_registry as cr
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers import label_registry as lr
from homeassistant.helpers import restore_state as rs
from homeassistant.helpers.storage import Store

from custom_components.device_lifecycle.const import DOMAIN
from custom_components.device_lifecycle.sensor import DeviceRuntimeHoursSensor
from custom_components.device_lifecycle.storage import (
    STORAGE_KEY,
    DeviceLifecycleStore,
    json_util,
)
from tests.conftest import ASSET_UUID
from tests.test_persistence_lifecycle import _REAL_LOAD, _REAL_WRITE, _disk
from tests.test_runtime import _ORIGINAL_ASYNC_WRITE_DATA, _Clock

pytestmark = pytest.mark.real_reload


@pytest.fixture
def hass_config_dir():
    path = Path(os.environ["DL_BL04_RESTART_CONFIG"])
    assert path.is_relative_to(Path(os.environ["DL_BL04_RESTART_TEST_ROOT"]))
    return str(path)


@pytest.fixture
def load_registries():
    # The plugin's default setup intentionally loads empty mocked registries.
    # This worker instead boots the persisted registries through real Store I/O.
    return False


@pytest.fixture(autouse=True)
def real_files():
    with (
        patch.object(Store, "_async_load", _REAL_LOAD),
        patch.object(Store, "_async_write_data", _ORIGINAL_ASYNC_WRITE_DATA),
        patch.object(DeviceLifecycleStore, "_async_load", _REAL_LOAD),
        patch.object(DeviceLifecycleStore, "_async_write_data", _REAL_WRITE),
    ):
        yield


async def test_restored_runtime(hass):
    await hass.config_entries.async_initialize()
    # Match HA startup ordering. Loading a Device Registry after HA is already
    # running takes its late-registration branch, which has no STOP cleanup for
    # the removal debouncer on the pinned versions.
    hass.set_state(CoreState.not_running)
    dr.async_setup(hass)
    for registry in (ar, cr, dr, er, fr, ir, lr, rs):
        await registry.async_load(hass)
    hass.data[bootstrap.DATA_REGISTRIES_LOADED] = None
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    entity_registry = er.async_get(hass)
    entry = next(e for e in hass.config_entries.async_entries(DOMAIN))
    subentry = next(iter(entry.subentries.values()))
    source = str(subentry.data["source_entity_id"])
    mode = str(subentry.data["runtime_mode"])
    phase = os.environ.get("DL_BL04_RESTART_PHASE", "verify")
    assert phase in ("verify", "original", "replace-only", "verify-reauthorize")
    starting_total = Decimal(os.environ.get("DL_BL04_RESTART_TOTAL", "1010"))
    binding = subentry.data.get("source_identity")
    original_marker = subentry.data.get("runtime_data_version")
    if phase == "replace-only":
        original = entity_registry.async_get(source)
        assert original.id == binding["registry_id"]
        owner = hass.config_entries.async_get_entry(original.config_entry_id)
        entity_registry.async_remove(source)
        replacement = entity_registry.async_get_or_create(
            original.domain,
            original.platform,
            "bl04-between-process-replacement",
            config_entry=owner,
            device_id=original.device_id,
            suggested_object_id=source.split(".", 1)[1],
            original_device_class="power" if mode == "power" else None,
        )
        assert replacement.entity_id == source and replacement.id != original.id
        assert subentry.data["source_identity"] == binding
        await entity_registry._store.async_save(entity_registry._data_to_save())
        stored = await hass.async_add_executor_job(
            json_util.load_json, hass.config.path(".storage", STORAGE_KEY)
        )
        assert (
            Decimal(stored["data"]["assets"][ASSET_UUID]["runtime"]["total_seconds"])
            == starting_total
        )
        Path(hass.config.path(f"restart-{phase}.json")).write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "old_registry_id": original.id,
                    "new_registry_id": replacement.id,
                    "total": str(starting_total),
                    "binding_unchanged": True,
                }
            )
        )
        # Let registry notifications schedule their cleanup before HA shutdown
        # cancels it. This phase deliberately never loads the DL integration.
        await hass.async_block_till_done()
        return
    clock = _Clock(100_000)
    original_init = DeviceRuntimeHoursSensor.__init__

    def timed_init(self, **kwargs):
        kwargs["monotonic"] = clock
        original_init(self, **kwargs)

    hass.states.async_set(
        source,
        "on" if mode == "on_state" else "100",
        {
            "device_class": "power",
            "unit_of_measurement": "W",
        },
    )
    if os.environ["DL_BL04_RESTART_CASE"] == "replacement":
        assert entity_registry.async_get(source).id != binding["registry_id"]
    with patch.object(DeviceRuntimeHoursSensor, "__init__", timed_init):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        manager = entry.runtime_data
        writer = manager._runtime_writers[ASSET_UUID].checkpoint.__self__
        clock.value += 30
        expected = starting_total + (
            30 if os.environ["DL_BL04_RESTART_CASE"] == "valid" else 0
        )
        assert await writer.async_checkpoint_runtime() == expected
        assert manager._data == _disk(manager)
        assert manager.asset(ASSET_UUID)["asset_id"] == "DL0007"
        assert manager.asset(ASSET_UUID)["notes"] == "Keep my customization"
        paused_total = expected
        if phase == "verify-reauthorize":
            assert writer._active_since is None and expected == starting_total
            assert (
                subentry.data["source_identity"]["authorization_id"]
                == binding["authorization_id"]
            )
            raw = {"source_entity_id": source, "confirm_source": True}
            if mode == "power":
                raw.update({"power_threshold": 10, "power_hysteresis": 2})
            initial = await hass.config_entries.subentries.async_init(
                (entry.entry_id, subentry.subentry_type),
                context={
                    "source": SOURCE_RECONFIGURE,
                    "subentry_id": subentry.subentry_id,
                },
            )
            source_form = await hass.config_entries.subentries.async_configure(
                initial["flow_id"], {"runtime_mode": mode}
            )
            result = await hass.config_entries.subentries.async_configure(
                source_form["flow_id"], raw
            )
            assert result["reason"] == "reconfigure_successful"
            await hass.async_block_till_done()
            assert (
                subentry.data["source_identity"]["registry_id"]
                == entity_registry.async_get(source).id
            )
            assert (
                subentry.data["source_identity"]["authorization_id"]
                != binding["authorization_id"]
            )
            assert subentry.data.get("runtime_data_version") == original_marker
            manager = entry.runtime_data
            writer = manager._runtime_writers[ASSET_UUID].checkpoint.__self__
            clock.value += 20
            expected += 20
            assert await writer.async_checkpoint_runtime() == expected
            assert manager._data == _disk(manager)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.config_entries._store.async_save(hass.config_entries._data_to_save())
        Path(hass.config.path(f"restart-{phase}.json")).write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "paused_total": str(paused_total),
                    "final_total": str(expected),
                    "asset_uuid": ASSET_UUID,
                    "asset_id": "DL0007",
                    "registry_id": entity_registry.async_get(source).id
                    if entity_registry.async_get(source)
                    else None,
                    "authorization": dict(subentry.data.get("source_identity", {})),
                }
            )
        )
