"""BL-04 real-registry safety discriminators and explicitly labeled controls."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import storage as ha_storage
from homeassistant.util.file import WriteError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle.config_flow import _is_valid_runtime_source
from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_DEVICE_ID,
    CONF_POWER_HYSTERESIS,
    CONF_POWER_THRESHOLD,
    CONF_RUNTIME_DATA_VERSION,
    CONF_RUNTIME_MODE,
    CONF_SOURCE_ENTITY_ID,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    RUNTIME_DATA_VERSION,
    RUNTIME_MODE_ON,
    RUNTIME_MODE_POWER,
    SUBENTRY_TYPE_RUNTIME,
)
from custom_components.device_lifecycle.sensor import DeviceRuntimeHoursSensor
from custom_components.device_lifecycle.storage import (
    DeviceLifecycleStore,
    json_util,
)
from tests.conftest import ASSET_UUID
from tests.test_exposure_options_reload import _reconfigure_runtime_source
from tests.test_persistence_lifecycle import (
    _REAL_LOAD,
    _REAL_WRITE,
    _blocked,
    _disk,
    _Gate,
    _seed,
)
from tests.test_runtime import _Clock
from tests.test_stale_device_registry_references import _device

pytestmark = pytest.mark.real_reload


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir):
    return hass_tmp_config_dir


@dataclass
class Scenario:
    hass: HomeAssistant
    entry: Any
    registry: er.EntityRegistry
    source: er.RegistryEntry
    owner: Any
    device: Any
    clock: _Clock
    mode: str
    events: list

    @property
    def manager(self):
        return self.entry.runtime_data

    @property
    def writer(self):
        return self.manager._runtime_writers[ASSET_UUID].checkpoint.__self__

    @property
    def active(self):
        return "on" if self.mode == RUNTIME_MODE_ON else "100"

    @property
    def inactive(self):
        return "off" if self.mode == RUNTIME_MODE_ON else "0"

    @property
    def attrs(self):
        return (
            {}
            if self.mode == RUNTIME_MODE_ON
            else {"device_class": "power", "unit_of_measurement": "W"}
        )

    async def state(self, value, entity_id=None):
        self.hass.states.async_set(
            entity_id or self.source.entity_id, value, self.attrs
        )
        await self.hass.async_block_till_done()

    async def start_checkpoint(self):
        await self.state(self.active)
        assert self.writer._active_since == 0
        self.clock.value = 10
        assert await self.writer.async_checkpoint_runtime() == Decimal(1010)
        assert self.manager._data == _disk(self.manager)

    def replace_now(self, kind="remove-reuse"):
        old_id = self.source.entity_id
        if kind == "rename-reuse":
            renamed = self.registry.async_update_entity(
                old_id, new_entity_id=old_id + "_renamed"
            )
            assert renamed.id == self.source.id
        else:
            self.registry.async_remove(old_id)
        self.hass.states.async_remove(old_id)
        same_owner = kind == "same-owner-device-reuse"
        other = (
            self.owner
            if same_owner
            else MockConfigEntry(domain="esphome", title="Different source", data={})
        )
        if not same_owner:
            other.add_to_hass(self.hass)
        new = self.registry.async_get_or_create(
            self.source.domain,
            "hue" if same_owner else "esphome",
            "bl04-replacement",
            config_entry=other,
            suggested_object_id="bl04_source",
            device_id=self.device.id if same_owner else None,
            original_device_class="power" if self.mode == RUNTIME_MODE_POWER else None,
        )
        assert new.entity_id == old_id and new.id != self.source.id
        if same_owner:
            assert (new.domain, new.platform, new.config_entry_id, new.device_id) == (
                self.source.domain,
                self.source.platform,
                self.source.config_entry_id,
                self.source.device_id,
            )
        self.hass.states.async_set(old_id, self.active, self.attrs)
        return new

    def observe(self, request, **extra):
        """Keep numeric and whole-Store assertions in each permanent regression."""
        assert self.manager._data == _disk(self.manager)


@pytest.fixture(params=[RUNTIME_MODE_ON, RUNTIME_MODE_POWER])
async def scenario(request, hass, asset_store_data, device_registry, entity_registry):
    data = deepcopy(asset_store_data)
    device = _device(hass, device_registry, "bl04-asset", data["assets"][ASSET_UUID])
    data["assets"][ASSET_UUID]["ha_device_refs"] = [
        {"device_id": device.id, "role": "primary"}
    ]
    owner = hass.config_entries.async_get_entry(device.config_entry_id)
    mode = request.param
    source = entity_registry.async_get_or_create(
        "switch" if mode == RUNTIME_MODE_ON else "sensor",
        "hue",
        "bl04-original",
        config_entry=owner,
        device_id=device.id,
        suggested_object_id="bl04_source",
        original_device_class="power" if mode == RUNTIME_MODE_POWER else None,
    )
    raw = {
        CONF_DEVICE_ID: device.id,
        CONF_ASSET_UUID: ASSET_UUID,
        CONF_SOURCE_ENTITY_ID: source.entity_id,
        CONF_RUNTIME_MODE: mode,
        CONF_RUNTIME_DATA_VERSION: RUNTIME_DATA_VERSION,
        # Literal authorized contract: RED must test continuity of an approved
        # source, rather than pass merely because all legacy trackers pause.
        "source_identity": {
            "version": 1,
            "authorization_id": "11111111111141118111111111111111",
            "registry_id": source.id,
            "domain": source.domain,
            "platform": source.platform,
            "unique_id": source.unique_id,
            "config_entry_id": source.config_entry_id,
            "config_subentry_id": source.config_subentry_id,
            "device_id": source.device_id,
        },
    }
    if mode == RUNTIME_MODE_POWER:
        raw.update({CONF_POWER_THRESHOLD: 10.0, CONF_POWER_HYSTERESIS: 2.0})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Device Lifecycle",
        unique_id="device_lifecycle_main",
        version=CONFIG_ENTRY_VERSION,
        data={},
        subentries_data=(
            {
                "data": raw,
                "subentry_type": SUBENTRY_TYPE_RUNTIME,
                "title": device.name,
                "unique_id": None,
            },
        ),
    )
    clock = _Clock()
    events = []
    original_init = DeviceRuntimeHoursSensor.__init__

    def timed_init(self, **kwargs):
        kwargs["monotonic"] = clock
        original_init(self, **kwargs)

    def registry_event(event):
        if str(event.data.get("entity_id", "")).startswith(source.entity_id):
            events.append({"clock": clock.value, "registry": dict(event.data)})

    def state_event(event):
        if event.data["entity_id"] == source.entity_id:
            new = event.data["new_state"]
            events.append(
                {
                    "clock": clock.value,
                    "state": new.state if new else None,
                    "context": new.context.id if new else None,
                }
            )

    with (
        patch.object(DeviceLifecycleStore, "_async_write_data", _REAL_WRITE),
        patch.object(DeviceLifecycleStore, "_async_load", _REAL_LOAD),
        patch.object(DeviceRuntimeHoursSensor, "__init__", timed_init),
    ):
        await _seed(hass, data)
        entry.add_to_hass(hass)
        s = Scenario(
            hass, entry, entity_registry, source, owner, device, clock, mode, events
        )
        hass.states.async_set(source.entity_id, s.inactive, s.attrs)
        assert _is_valid_runtime_source(hass, source.entity_id, mode)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert s.writer._active_since is None
        stops = [
            hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, registry_event),
            hass.bus.async_listen("state_changed", state_event),
        ]
        try:
            yield s
        finally:
            for stop in stops:
                stop()
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()


@pytest.mark.parametrize(
    "kind", ["remove-reuse", "rename-reuse", "same-owner-device-reuse"]
)
@pytest.mark.parametrize("drain", ["explicit", "periodic", "unload"])
async def test_reused_source_never_contributes_runtime(scenario, request, kind, drain):
    s = scenario
    await s.start_checkpoint()
    before = deepcopy(s.manager._data)
    replacement = s.replace_now(kind)
    await s.hass.async_block_till_done()
    assert _is_valid_runtime_source(s.hass, replacement.entity_id, s.mode)
    s.clock.value = 40
    if drain == "explicit":
        await s.writer.async_checkpoint_runtime()
    elif drain == "periodic":
        await s.writer._handle_periodic_checkpoint(None)
    else:
        await s.writer.async_prepare_runtime_unload()
    s.observe(request, required_safe_total="1010")
    assert s.manager.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
    assert s.manager._data == _disk(s.manager) == before


@pytest.mark.parametrize("kind", ["remove-reuse", "same-owner-device-reuse"])
async def test_stopped_replacement_requires_authorization_after_reload(
    scenario, request, kind
):
    s = scenario
    await s.start_checkpoint()
    before = deepcopy(s.manager._data)
    config = dict(next(iter(s.entry.subentries.values())).data)
    assert await s.hass.config_entries.async_unload(s.entry.entry_id)
    s.replace_now(kind)
    await s.hass.async_block_till_done()
    assert dict(next(iter(s.entry.subentries.values())).data) == config
    assert await s.hass.config_entries.async_setup(s.entry.entry_id)
    await s.hass.async_block_till_done()
    s.clock.value = 40
    await s.writer.async_checkpoint_runtime()
    s.observe(request, required_safe_total="1010", configuration_unchanged=True)
    assert s.manager.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
    assert s.manager._data == _disk(s.manager) == before


async def test_missing_at_setup_cannot_authorize_later_replacement(scenario, request):
    s = scenario
    before = deepcopy(s.manager._data)
    assert await s.hass.config_entries.async_unload(s.entry.entry_id)
    s.registry.async_remove(s.source.entity_id)
    s.hass.states.async_remove(s.source.entity_id)
    assert await s.hass.config_entries.async_setup(s.entry.entry_id)
    await s.hass.async_block_till_done()
    assert s.writer._active_since is None
    other = MockConfigEntry(domain="esphome", title="Later source", data={})
    other.add_to_hass(s.hass)
    replacement = s.registry.async_get_or_create(
        s.source.domain,
        "esphome",
        "bl04-later",
        config_entry=other,
        suggested_object_id="bl04_source",
        original_device_class="power" if s.mode == RUNTIME_MODE_POWER else None,
    )
    assert replacement.entity_id == s.source.entity_id and replacement.id != s.source.id
    await s.state(s.active)
    s.clock.value = 30
    await s.writer.async_checkpoint_runtime()
    s.observe(request, required_safe_total="1000")
    assert s.manager.runtime_total_seconds(ASSET_UUID) == Decimal(1000)
    assert s.manager._data == _disk(s.manager) == before


async def test_queued_identical_replacement_must_not_invent_safe_elapsed(
    scenario, request
):
    s = scenario
    await s.start_checkpoint()
    before = deepcopy(s.manager._data)
    await s.writer._runtime_lock.acquire()
    try:
        s.replace_now()
        await asyncio.sleep(0)
        checkpoint = asyncio.create_task(s.writer.async_checkpoint_runtime())
        await _blocked(checkpoint)
        s.clock.value = 40
    finally:
        s.writer._runtime_lock.release()
    await checkpoint
    await s.hass.async_block_till_done()
    s.observe(
        request, required_safe_total="1010", replacement_clock=10, processing_clock=40
    )
    assert s.manager.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
    assert s.manager._data == _disk(s.manager) == before


@pytest.mark.parametrize("fault", ["before-write", "readback", "cancel-after-write"])
async def test_replacement_keeps_valid_pending_delta_only(scenario, request, fault):
    s = scenario
    await s.state(s.active)
    s.clock.value = 10
    if fault == "cancel-after-write":
        gate = _Gate(s.hass)
        with patch.object(ha_storage, "write_utf8_file_atomic", side_effect=gate.write):
            checkpoint = asyncio.create_task(s.writer.async_checkpoint_runtime())
            try:
                await asyncio.wait_for(gate.started.wait(), 5)
                checkpoint.cancel()
                await _blocked(checkpoint)
                s.replace_now()
            finally:
                gate.release.set()
                with pytest.raises(asyncio.CancelledError):
                    await checkpoint
    else:
        if fault == "before-write":
            target, attribute, failure = (
                ha_storage,
                "write_utf8_file_atomic",
                WriteError("BL04 injected before write"),
            )
        else:
            target, attribute, failure = (
                json_util,
                "load_json",
                RuntimeError("BL04 lost readback"),
            )
        with patch.object(target, attribute, side_effect=failure):
            await s.writer._handle_periodic_checkpoint(None)
        assert [(p.expected_total, p.delta) for p in s.writer._pending] == [
            (Decimal(1000), Decimal(10))
        ]
        s.replace_now()
    await s.hass.async_block_till_done()
    assert s.manager.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
    valid = deepcopy(s.manager._data)
    s.clock.value = 40
    await s.writer.async_checkpoint_runtime()
    s.observe(
        request, required_safe_total="1010", valid_pending_delta="10", fault=fault
    )
    assert s.manager.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
    assert s.manager._data == _disk(s.manager) == valid


@pytest.mark.parametrize("middle", ["normal", "unavailable"])
async def test_control_same_verified_row_supports_normal_transitions(
    scenario, request, middle
):
    s = scenario
    await s.start_checkpoint()
    s.clock.value = 20
    await s.state(s.inactive if middle == "normal" else "unavailable")
    s.clock.value = 30
    await s.state(s.active)
    s.clock.value = 40
    await s.state(s.inactive)
    s.observe(request, required_total="1030", control=True)
    assert s.registry.async_get(s.source.entity_id).id == s.source.id
    assert s.manager.runtime_total_seconds(ASSET_UUID) == Decimal(1030)
    assert s.manager._data == _disk(s.manager)


async def test_control_explicit_different_source_reconfiguration_keeps_total(
    scenario, request
):
    s = scenario
    await s.start_checkpoint()
    chosen = s.registry.async_get_or_create(
        s.source.domain,
        "hue",
        "bl04-chosen",
        config_entry=s.owner,
        suggested_object_id="bl04_chosen",
        original_device_class="power" if s.mode == RUNTIME_MODE_POWER else None,
    )
    await s.state(s.active, chosen.entity_id)
    subentry = next(iter(s.entry.subentries.values()))
    raw = {CONF_SOURCE_ENTITY_ID: chosen.entity_id}
    if s.mode == RUNTIME_MODE_POWER:
        raw.update({CONF_POWER_THRESHOLD: 10.0, CONF_POWER_HYSTERESIS: 2.0})
    _, result = await _reconfigure_runtime_source(
        s.hass, s.entry, subentry.subentry_id, runtime_mode=s.mode, source_input=raw
    )
    assert result["reason"] == "reconfigure_successful"
    await s.hass.async_block_till_done()
    assert s.writer._source_entity_id == chosen.entity_id
    s.clock.value = 40
    await s.writer.async_checkpoint_runtime()
    s.observe(
        request, required_total="1040", explicit_source=chosen.entity_id, control=True
    )
    assert s.manager.runtime_total_seconds(ASSET_UUID) == Decimal(1040)
    assert s.manager._data == _disk(s.manager)


async def test_registry_less_source_is_paused(scenario, request):
    s = scenario
    s.registry.async_remove(s.source.entity_id)
    await s.hass.async_block_till_done()
    assert s.registry.async_get(s.source.entity_id) is None
    assert not _is_valid_runtime_source(s.hass, s.source.entity_id, s.mode)
    await s.state(s.active)
    s.clock.value = 10
    await s.writer.async_checkpoint_runtime()
    s.observe(request)
    assert s.manager.runtime_total_seconds(ASSET_UUID) == Decimal(1000)


async def test_same_address_reconfigure_records_explicit_new_authorization(
    scenario, request
):
    """Two different consent histories have identical persisted DL configuration."""
    s = scenario
    await s.start_checkpoint()
    replacement = s.replace_now("same-owner-device-reuse")
    await s.hass.async_block_till_done()
    subentry = next(iter(s.entry.subentries.values()))
    before = subentry.as_dict()
    modified = s.entry.modified_at
    store_bytes = Path(s.manager._store.path).read_bytes()
    raw = {CONF_SOURCE_ENTITY_ID: replacement.entity_id}
    if s.mode == RUNTIME_MODE_POWER:
        raw.update({CONF_POWER_THRESHOLD: 10.0, CONF_POWER_HYSTERESIS: 2.0})
    _, result = await _reconfigure_runtime_source(
        s.hass, s.entry, subentry.subentry_id, runtime_mode=s.mode, source_input=raw
    )
    await s.hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    after = s.entry.subentries[subentry.subentry_id].as_dict()
    assert after != before
    assert after["data"]["source_identity"]["registry_id"] == replacement.id
    assert (
        after["data"]["source_identity"]["authorization_id"]
        != before["data"]["source_identity"]["authorization_id"]
    )
    assert s.entry.modified_at != modified
    assert Path(s.manager._store.path).read_bytes() == store_bytes
    s.observe(
        request,
        observational_not_safety_acceptance=True,
        same_address_authorization_updates_binding=True,
    )


async def test_observation_registry_metadata_can_change_without_row_replacement(
    scenario, request
):
    s = scenario
    original = s.source
    renamed = s.registry.async_update_entity(
        original.entity_id, new_entity_id=original.entity_id + "_metadata"
    )
    assert renamed.id == original.id and renamed.unique_id == original.unique_id
    changed = s.registry.async_update_entity(
        renamed.entity_id, new_unique_id="bl04-migrated-unique"
    )
    assert changed.id == original.id and changed.unique_id != original.unique_id
    other = MockConfigEntry(domain="esphome", title="New owner", data={})
    other.add_to_hass(s.hass)
    changed = s.registry.async_update_entity(
        changed.entity_id, config_entry_id=other.entry_id, device_id=None
    )
    assert changed.id == original.id
    assert (
        changed.config_entry_id != original.config_entry_id
        and changed.device_id is None
    )
    s.observe(
        request,
        observational_not_safety_acceptance=True,
        preserved_row_id=changed.id,
        new_platform=changed.platform,
        new_unique_id=changed.unique_id,
        new_owner=changed.config_entry_id,
        new_device=changed.device_id,
    )
