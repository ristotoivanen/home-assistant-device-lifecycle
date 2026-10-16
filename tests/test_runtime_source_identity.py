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
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import storage as ha_storage
from homeassistant.util.file import WriteError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle.config_flow import (
    _is_valid_runtime_source,
    _runtime_source_error,
)
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
    AssetStoreError,
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
            if hass.config_entries.async_get_entry(entry.entry_id) is entry:
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
    try:
        await checkpoint
    except AssetStoreError as err:
        # Durable invalidation uses HA's ConfigSubentry update/reload path.
        # If unload wins the lock, the old queued writer must stay quiesced.
        assert err.code == "runtime_writer_quiesced"
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
    raw = {CONF_SOURCE_ENTITY_ID: chosen.entity_id, "confirm_source": True}
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
    assert (
        _runtime_source_error(s.hass, s.source.entity_id, s.mode)
        == "source_registry_required"
    )
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
    raw = {CONF_SOURCE_ENTITY_ID: replacement.entity_id, "confirm_source": True}
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
    await s.hass.async_block_till_done()
    s.observe(
        request,
        observational_not_safety_acceptance=True,
        preserved_row_id=changed.id,
        new_platform=changed.platform,
        new_unique_id=changed.unique_id,
        new_owner=changed.config_entry_id,
        new_device=changed.device_id,
    )


async def test_legacy_tracker_requires_explicit_confirmation_without_reset(scenario):
    """An existing address, even active and registered, is not past consent."""
    s = scenario
    assert await s.hass.config_entries.async_unload(s.entry.entry_id)
    subentry = next(iter(s.entry.subentries.values()))
    raw = dict(subentry.data)
    raw.pop("source_identity")
    s.hass.config_entries.async_update_subentry(s.entry, subentry, data=raw)
    await s.state(s.active)
    assert await s.hass.config_entries.async_setup(s.entry.entry_id)
    await s.hass.async_block_till_done()
    s.clock.value = 40
    assert await s.writer.async_checkpoint_runtime() == Decimal(1000)
    assert (
        s.writer.extra_state_attributes["runtime_source_status"]
        == "legacy_confirmation_required"
    )
    assert dict(subentry.data) == raw
    source_input = {CONF_SOURCE_ENTITY_ID: s.source.entity_id}
    if s.mode == RUNTIME_MODE_POWER:
        source_input.update({CONF_POWER_THRESHOLD: 10, CONF_POWER_HYSTERESIS: 2})
    _, result = await _reconfigure_runtime_source(
        s.hass,
        s.entry,
        subentry.subentry_id,
        runtime_mode=s.mode,
        source_input=source_input,
    )
    assert result["errors"] == {"base": "source_confirmation_required"}
    assert dict(subentry.data) == raw
    result = await s.hass.config_entries.subentries.async_configure(
        result["flow_id"], {**source_input, "confirm_source": True}
    )
    assert result["reason"] == "reconfigure_successful"
    await s.hass.async_block_till_done()
    assert subentry.data[CONF_RUNTIME_DATA_VERSION] == raw[CONF_RUNTIME_DATA_VERSION]
    assert subentry.data[CONF_ASSET_UUID] == ASSET_UUID
    assert subentry.data["source_identity"]["registry_id"] == s.source.id
    s.clock.value = 60
    assert await s.writer.async_checkpoint_runtime() == Decimal(1020)
    assert s.manager._data == _disk(s.manager)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 2),
        ("version", True),
        ("registry_id", "another-row"),
        ("unique_id", "another-channel"),
        ("config_subentry_id", "another-subentry"),
        ("device_id", "another-device"),
        ("authorization_id", ""),
    ],
)
async def test_unverified_saved_binding_never_authorizes_current_state(
    scenario, field, value
):
    s = scenario
    assert await s.hass.config_entries.async_unload(s.entry.entry_id)
    subentry = next(iter(s.entry.subentries.values()))
    raw = dict(subentry.data)
    raw["source_identity"] = {**raw["source_identity"], field: value}
    # bool True compares equal to int 1 in Python dicts. Force HA to publish
    # this malformed fixture rather than reject it as an unchanged update.
    raw["source_identity"]["authorization_id"] = (
        "22222222222242228222222222222222" if field != "authorization_id" else value
    )
    s.hass.config_entries.async_update_subentry(s.entry, subentry, data=raw)
    await s.state(s.active)
    assert await s.hass.config_entries.async_setup(s.entry.entry_id)
    await s.hass.async_block_till_done()
    s.clock.value = 40
    assert await s.writer.async_checkpoint_runtime() == Decimal(1000)
    assert s.writer._active_since is None
    assert s.manager._data == _disk(s.manager)


@pytest.mark.parametrize("change", ["unique_id", "config_entry_id", "device_id"])
async def test_origin_change_preserving_row_id_still_revokes_authorization(
    scenario, change
):
    s = scenario
    await s.start_checkpoint()
    if change == "unique_id":
        row = s.registry.async_update_entity(
            s.source.entity_id, new_unique_id="migrated"
        )
    elif change == "device_id":
        row = s.registry.async_update_entity(s.source.entity_id, device_id=None)
    else:
        owner = MockConfigEntry(domain="hue", data={})
        owner.add_to_hass(s.hass)
        row = s.registry.async_update_entity(
            s.source.entity_id, config_entry_id=owner.entry_id
        )
    assert row.id == s.source.id
    await s.hass.async_block_till_done()
    s.clock.value = 40
    assert await s.writer.async_checkpoint_runtime() == Decimal(1010)
    assert (
        "invalidated" in next(iter(s.entry.subentries.values())).data["source_identity"]
    )
    assert s.writer._active_since is None


async def test_presentation_metadata_and_state_context_do_not_revoke(scenario):
    s = scenario
    await s.start_checkpoint()
    s.registry.async_update_entity(s.source.entity_id, name="Renamed display only")
    await s.hass.async_block_till_done()
    s.clock.value = 40
    await s.state(s.inactive)
    assert s.manager.runtime_total_seconds(ASSET_UUID) == Decimal(1040)
    assert s.writer.extra_state_attributes["runtime_source_status"] == "authorized"
    assert (
        "invalidated"
        not in next(iter(s.entry.subentries.values())).data["source_identity"]
    )


async def test_aba_return_before_event_dispatch_stays_revoked_across_reload(scenario):
    s = scenario
    await s.start_checkpoint()
    old_writer = s.writer
    old_callback = old_writer._source_invalidated
    subentry = next(iter(s.entry.subentries.values()))
    old_token = subentry.data["source_identity"]["authorization_id"]
    # No await: equality at callback processing time must not erase the gap.
    s.registry.async_remove(s.source.entity_id)
    s.hass.states.async_remove(s.source.entity_id)
    restored = s.registry.async_get_or_create(
        s.source.domain,
        s.source.platform,
        s.source.unique_id,
        config_entry=s.owner,
        device_id=s.device.id,
        suggested_object_id="bl04_source",
        original_device_class="power" if s.mode == RUNTIME_MODE_POWER else None,
    )
    assert restored.id == s.source.id and restored.entity_id == s.source.entity_id
    s.hass.states.async_set(restored.entity_id, s.active, s.attrs)
    await s.hass.async_block_till_done()
    s.clock.value = 40
    assert await s.writer.async_checkpoint_runtime() == Decimal(1010)
    assert "invalidated" in subentry.data["source_identity"]
    assert await s.hass.config_entries.async_reload(s.entry.entry_id)
    s.clock.value = 50
    assert await s.writer.async_checkpoint_runtime() == Decimal(1010)
    raw = {CONF_SOURCE_ENTITY_ID: restored.entity_id, "confirm_source": True}
    if s.mode == RUNTIME_MODE_POWER:
        raw.update({CONF_POWER_THRESHOLD: 10, CONF_POWER_HYSTERESIS: 2})
    _, result = await _reconfigure_runtime_source(
        s.hass, s.entry, subentry.subentry_id, runtime_mode=s.mode, source_input=raw
    )
    assert result["reason"] == "reconfigure_successful"
    await s.hass.async_block_till_done()
    assert subentry.data["source_identity"]["authorization_id"] != old_token
    # An old buffered callback cannot revoke the fresh authorization or count.
    old_callback("stale_callback")
    assert "invalidated" not in subentry.data["source_identity"]
    s.clock.value = 70
    assert await s.writer.async_checkpoint_runtime() == Decimal(1030)
    assert old_writer._removing


async def test_verified_rename_pauses_until_explicit_address_confirmation(scenario):
    s = scenario
    await s.start_checkpoint()
    renamed = s.registry.async_update_entity(
        s.source.entity_id, new_entity_id=s.source.entity_id + "_renamed"
    )
    assert renamed.id == s.source.id
    await s.state(s.active, renamed.entity_id)
    await s.hass.async_block_till_done()
    s.clock.value = 40
    assert await s.writer.async_checkpoint_runtime() == Decimal(1010)
    subentry = next(iter(s.entry.subentries.values()))
    raw = {CONF_SOURCE_ENTITY_ID: renamed.entity_id, "confirm_source": True}
    if s.mode == RUNTIME_MODE_POWER:
        raw.update({CONF_POWER_THRESHOLD: 10, CONF_POWER_HYSTERESIS: 2})
    _, result = await _reconfigure_runtime_source(
        s.hass, s.entry, subentry.subentry_id, runtime_mode=s.mode, source_input=raw
    )
    assert result["reason"] == "reconfigure_successful"
    await s.hass.async_block_till_done()
    s.clock.value = 60
    assert await s.writer.async_checkpoint_runtime() == Decimal(1030)
    assert subentry.data["source_identity"]["registry_id"] == s.source.id
    assert subentry.data[CONF_SOURCE_ENTITY_ID] == renamed.entity_id


async def test_actual_unload_after_replacement_cannot_seal_uncertain_tail(scenario):
    s = scenario
    await s.start_checkpoint()
    manager = s.manager
    before = deepcopy(manager._data)
    s.replace_now()
    # Teardown can win before the registry callback; its own validation is vital.
    s.clock.value = 40
    assert await s.hass.config_entries.async_unload(s.entry.entry_id)
    await s.hass.async_block_till_done()
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
    assert manager._data == _disk(manager) == before


async def test_failed_reauthorization_reload_keeps_valid_pending_until_retry(scenario):
    s = scenario
    await s.state(s.active)
    s.clock.value = 10
    old_writer = s.writer
    with patch.object(
        ha_storage,
        "write_utf8_file_atomic",
        side_effect=WriteError("injected disk failure"),
    ):
        await old_writer._handle_periodic_checkpoint(None)
        replacement = s.replace_now()
        await s.hass.async_block_till_done()
        assert old_writer._active_since is None
        assert [(p.expected_total, p.delta) for p in old_writer._pending] == [
            (Decimal(1000), Decimal(10))
        ]
        subentry = next(iter(s.entry.subentries.values()))
        raw = {CONF_SOURCE_ENTITY_ID: replacement.entity_id, "confirm_source": True}
        if s.mode == RUNTIME_MODE_POWER:
            raw.update({CONF_POWER_THRESHOLD: 10, CONF_POWER_HYSTERESIS: 2})
        _, result = await _reconfigure_runtime_source(
            s.hass, s.entry, subentry.subentry_id, runtime_mode=s.mode, source_input=raw
        )
        assert result["errors"] == {"base": "runtime_pending_retry"}
        assert "invalidated" in subentry.data["source_identity"]
        await s.hass.async_block_till_done()
        assert s.writer is old_writer
        s.clock.value = 40
        assert old_writer._active_since is None
        assert len(old_writer._pending) == 1
        assert s.manager.runtime_total_seconds(ASSET_UUID) == Decimal(1000)
    result = await s.hass.config_entries.subentries.async_configure(
        result["flow_id"], raw
    )
    assert result["reason"] == "reconfigure_successful"
    await s.hass.async_block_till_done()
    assert old_writer._pending == []
    assert s.manager.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
    s.clock.value = 60
    assert await s.writer.async_checkpoint_runtime() == Decimal(1030)
    assert s.manager._data == _disk(s.manager)


async def test_cancellation_requested_after_durable_write_excludes_replacement(
    scenario,
):
    """Unlike the frozen probe, cancellation is requested after disk readback."""
    import threading

    s = scenario
    await s.state(s.active)
    s.clock.value = 10
    started = asyncio.Event()
    release = threading.Event()
    original_load = json_util.load_json
    first = True

    def durable_readback(path, *args, **kwargs):
        nonlocal first
        result = original_load(path, *args, **kwargs)
        if first and str(path) == s.manager._store.path:
            first = False
            s.hass.loop.call_soon_threadsafe(started.set)
            assert release.wait(10)
        return result

    # A Mock target runs inline in HA's pytest executor wrapper. Use the real
    # function target so this barrier actually blocks a background executor.
    with patch.object(json_util, "load_json", new=durable_readback):
        checkpoint = asyncio.create_task(s.writer.async_checkpoint_runtime())
        try:
            await asyncio.wait_for(started.wait(), 5)
            # Independently prove durability before requesting cancellation.
            persisted = await s.hass.async_add_executor_job(
                original_load, s.manager._store.path
            )
            assert Decimal(
                persisted["data"]["assets"][ASSET_UUID]["runtime"]["total_seconds"]
            ) == Decimal(1010)
            checkpoint.cancel()
            await _blocked(checkpoint)
            s.replace_now()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await checkpoint
    await s.hass.async_block_till_done()
    s.clock.value = 40
    assert await s.writer.async_checkpoint_runtime() == Decimal(1010)
    assert s.writer._pending == []
    assert s.writer._active_since is None
    assert s.manager._data == _disk(s.manager)


@pytest.mark.parametrize(
    "restart_case", ["valid", "replacement", "legacy", "invalidated-aba"]
)
async def test_fresh_process_validates_persisted_authorization(
    scenario, tmp_path, restart_case
):
    """A new Python/HA process reads actual ConfigEntries, registry and Store files."""
    import os
    import shutil
    import sys

    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers.storage import Store

    from tests.test_runtime import _ORIGINAL_ASYNC_WRITE_DATA

    s = scenario
    await s.start_checkpoint()
    if restart_case == "invalidated-aba":
        s.registry.async_remove(s.source.entity_id)
        s.hass.states.async_remove(s.source.entity_id)
        restored = s.registry.async_get_or_create(
            s.source.domain,
            s.source.platform,
            s.source.unique_id,
            config_entry=s.owner,
            device_id=s.device.id,
            suggested_object_id="bl04_source",
        )
        assert restored.id == s.source.id
        await s.hass.async_block_till_done()
    assert await s.hass.config_entries.async_unload(s.entry.entry_id)
    if restart_case == "replacement":
        s.replace_now("same-owner-device-reuse")
        await s.hass.async_block_till_done()
    if restart_case == "legacy":
        subentry = next(iter(s.entry.subentries.values()))
        raw = dict(subentry.data)
        raw.pop("source_identity")
        s.hass.config_entries.async_update_subentry(s.entry, subentry, data=raw)
    # Persist using HA's actual serializers and Store backend, not hand-built rows.
    with patch.object(Store, "_async_write_data", _ORIGINAL_ASYNC_WRITE_DATA):
        for owner in (s.hass.config_entries, s.registry, dr.async_get(s.hass)):
            # The plugin creates registry StoreWithoutWriteLoad instances.
            # A real Store with the pinned envelope versions exercises actual I/O.
            real = Store(
                s.hass,
                owner._store.version,
                owner._store.key,
                minor_version=owner._store.minor_version,
            )
            await real.async_save(owner._data_to_save())
    config = tmp_path / "fresh-process-config"
    shutil.copytree(Path(s.hass.config.config_dir) / ".storage", config / ".storage")
    env = {
        **os.environ,
        "DL_BL04_RESTART_CONFIG": str(config),
        "DL_BL04_RESTART_TEST_ROOT": str(tmp_path),
        "DL_BL04_RESTART_CASE": restart_case,
    }
    worker = Path(__file__).with_name("runtime_restart_worker.py")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-ra",
        str(worker),
        "--basetemp=" + str(tmp_path / "worker-temp"),
        "--override-ini=cache_dir=" + str(tmp_path / "worker-cache"),
        "--junitxml=" + str(tmp_path / "restart-worker.xml"),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await asyncio.wait_for(proc.communicate(), 30)
    (tmp_path / "restart-worker.log").write_bytes(output)
    assert proc.returncode == 0, output.decode()


async def test_confirmation_does_not_authorize_a_replacement_during_preflight(scenario):
    s = scenario
    await s.start_checkpoint()
    subentry = next(iter(s.entry.subentries.values()))
    before = dict(subentry.data)
    original_checkpoint = s.manager.async_checkpoint_runtime

    async def replace_during_checkpoint(asset_uuid):
        total = await original_checkpoint(asset_uuid)
        s.replace_now()
        return total

    raw = {CONF_SOURCE_ENTITY_ID: s.source.entity_id, "confirm_source": True}
    if s.mode == RUNTIME_MODE_POWER:
        raw.update({CONF_POWER_THRESHOLD: 10, CONF_POWER_HYSTERESIS: 2})
    with patch.object(
        s.manager, "async_checkpoint_runtime", new=replace_during_checkpoint
    ):
        _, result = await _reconfigure_runtime_source(
            s.hass, s.entry, subentry.subentry_id, runtime_mode=s.mode, source_input=raw
        )
    assert result["errors"] == {"base": "source_confirmation_required"}
    await s.hass.async_block_till_done()
    assert (
        subentry.data["source_identity"]["authorization_id"]
        == before["source_identity"]["authorization_id"]
    )
    assert "invalidated" in subentry.data["source_identity"]
    s.clock.value = 40
    assert await s.writer.async_checkpoint_runtime() == Decimal(1010)


async def test_missing_state_is_distinct_from_missing_registry_and_can_resume(scenario):
    s = scenario
    assert await s.hass.config_entries.async_unload(s.entry.entry_id)
    s.hass.states.async_remove(s.source.entity_id)
    assert await s.hass.config_entries.async_setup(s.entry.entry_id)
    await s.hass.async_block_till_done()
    attrs = s.writer.extra_state_attributes
    assert attrs["runtime_source_status"] == "authorized"
    assert attrs["runtime_source_state"] == "missing"
    s.clock.value = 10
    await s.state("unavailable")
    assert s.writer.extra_state_attributes["runtime_source_state"] == "unavailable"
    s.clock.value = 20
    await s.state(s.active)
    s.clock.value = 40
    assert await s.writer.async_checkpoint_runtime() == Decimal(1020)
    assert s.manager._data == _disk(s.manager)


async def test_create_cannot_authorize_replacement_while_reservation_waits(scenario):
    s = scenario
    subentry = next(iter(s.entry.subentries.values()))
    s.hass.config_entries.async_remove_subentry(s.entry, subentry.subentry_id)
    await s.hass.async_block_till_done()
    assert not s.entry.subentries and not s.manager._runtime_writers
    initial = await s.hass.config_entries.subentries.async_init(
        (s.entry.entry_id, SUBENTRY_TYPE_RUNTIME), context={"source": SOURCE_USER}
    )
    source_form = await s.hass.config_entries.subentries.async_configure(
        initial["flow_id"], {CONF_DEVICE_ID: s.device.id, CONF_RUNTIME_MODE: s.mode}
    )
    submitted = {CONF_SOURCE_ENTITY_ID: s.source.entity_id}
    if s.mode == RUNTIME_MODE_POWER:
        submitted.update({CONF_POWER_THRESHOLD: 10, CONF_POWER_HYSTERESIS: 2})
    manager = s.manager
    await manager._mutation_lock.acquire()
    create = asyncio.create_task(
        s.hass.config_entries.subentries.async_configure(
            source_form["flow_id"], submitted
        )
    )
    try:
        await _blocked(create)
        s.replace_now("same-owner-device-reuse")
    finally:
        manager._mutation_lock.release()
    result = await create
    await s.hass.async_block_till_done()
    s.clock.value = 30
    assert await s.manager.async_checkpoint_runtime(ASSET_UUID) == Decimal(1000)
    assert result["type"] == "form"
    assert result["errors"] == {"base": "source_changed_during_save"}
    assert not s.entry.subentries and not s.manager._runtime_writers
    assert s.manager._data == _disk(s.manager)


@pytest.mark.parametrize(
    "drain",
    [
        "checkpoint",
        "periodic",
        "prepare-unload",
        "remove",
        "retire",
        "shutdown",
        "actual-unload",
    ],
)
async def test_nested_registry_aba_never_projects_or_seals_before_events_drain(
    scenario,
    drain,
):
    s = scenario
    await s.start_checkpoint()
    manager = s.manager
    tasks = []
    projected = []

    @callback
    def nested_mutation(_event):
        s.registry.async_remove(s.source.entity_id)
        s.hass.states.async_remove(s.source.entity_id)
        restored = s.registry.async_get_or_create(
            s.source.domain,
            s.source.platform,
            s.source.unique_id,
            config_entry=s.owner,
            device_id=s.device.id,
            suggested_object_id="bl04_source",
            original_device_class="power" if s.mode == RUNTIME_MODE_POWER else None,
        )
        assert restored.id == s.source.id and restored.entity_id == s.source.entity_id
        s.hass.states.async_set(restored.entity_id, s.active, s.attrs)
        s.clock.value = 40
        projected.append(s.writer.native_value)
        operations = {
            "checkpoint": s.writer.async_checkpoint_runtime,
            "periodic": lambda: s.writer._handle_periodic_checkpoint(None),
            "prepare-unload": s.writer.async_prepare_runtime_unload,
            "remove": s.writer.async_will_remove_from_hass,
            "retire": s.writer.async_retire_runtime,
            "shutdown": lambda: s.writer._handle_shutdown(None),
            "actual-unload": lambda: s.hass.config_entries.async_unload(
                s.entry.entry_id
            ),
        }
        tasks.append(s.hass.async_create_task(operations[drain]()))

    s.hass.bus.async_listen_once("bl04_nested_identity_mutation", nested_mutation)
    s.hass.bus.async_fire("bl04_nested_identity_mutation")
    await s.hass.async_block_till_done()
    assert len(tasks) == 1
    await tasks[0]
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
    assert projected == [(Decimal(1010) / Decimal(3600)).quantize(Decimal("0.000001"))]
    assert (
        "invalidated" in next(iter(s.entry.subentries.values())).data["source_identity"]
    )
    assert manager._data == _disk(manager)


async def test_stale_projection_after_parent_removal_cannot_update_removed_entry(
    scenario,
):
    s = scenario
    await s.start_checkpoint()
    old_writer = s.writer
    await s.hass.config_entries.async_remove(s.entry.entry_id)
    assert old_writer._removing
    assert s.hass.config_entries.async_get_entry(s.entry.entry_id) is None
    s.registry.async_remove(s.source.entity_id)
    await s.hass.async_block_till_done()
    assert old_writer.native_value == (Decimal(1010) / Decimal(3600)).quantize(
        Decimal("0.000001")
    )


@pytest.mark.parametrize(
    "drain",
    [
        "checkpoint",
        "periodic",
        "prepare-unload",
        "remove",
        "retire",
        "shutdown",
        "actual-unload",
    ],
)
async def test_nested_presentation_change_retains_valid_elapsed_on_every_drain(
    scenario, drain
):
    s = scenario
    await s.start_checkpoint()
    manager = s.manager
    subentry = next(iter(s.entry.subentries.values()))
    before = dict(subentry.data)
    tasks = []
    projected = []

    @callback
    def nested_presentation(_event):
        row = s.registry.async_update_entity(
            s.source.entity_id, name="Display name only"
        )
        assert row.id == s.source.id and row.unique_id == s.source.unique_id
        s.clock.value = 40
        projected.append(s.writer.native_value)
        operations = {
            "checkpoint": s.writer.async_checkpoint_runtime,
            "periodic": lambda: s.writer._handle_periodic_checkpoint(None),
            "prepare-unload": s.writer.async_prepare_runtime_unload,
            "remove": s.writer.async_will_remove_from_hass,
            "retire": s.writer.async_retire_runtime,
            "shutdown": lambda: s.writer._handle_shutdown(None),
            "actual-unload": lambda: s.hass.config_entries.async_unload(
                s.entry.entry_id
            ),
        }
        tasks.append(s.hass.async_create_task(operations[drain]()))

    s.hass.bus.async_listen_once("bl04_nested_presentation_change", nested_presentation)
    s.hass.bus.async_fire("bl04_nested_presentation_change")
    await s.hass.async_block_till_done()
    assert len(tasks) == 1
    await tasks[0]
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1040)
    assert projected == [(Decimal(1010) / Decimal(3600)).quantize(Decimal("0.000001"))]
    assert dict(subentry.data) == before
    assert manager._data == _disk(manager)


async def test_cancelled_reauthorization_preserves_approval_pending_and_retry(scenario):
    s = scenario
    await s.state(s.active)
    s.clock.value = 10
    writer = s.writer
    with patch.object(
        ha_storage,
        "write_utf8_file_atomic",
        side_effect=WriteError("before reauthorization"),
    ):
        await writer._handle_periodic_checkpoint(None)
        replacement = s.replace_now()
        await s.hass.async_block_till_done()
    subentry = next(iter(s.entry.subentries.values()))
    old_data = deepcopy(dict(subentry.data))
    assert "invalidated" in old_data["source_identity"]
    raw = {CONF_SOURCE_ENTITY_ID: replacement.entity_id, "confirm_source": True}
    if s.mode == RUNTIME_MODE_POWER:
        raw.update({CONF_POWER_THRESHOLD: 10, CONF_POWER_HYSTERESIS: 2})
    initial = await s.entry.start_subentry_reconfigure_flow(
        s.hass, subentry.subentry_id
    )
    source_form = await s.hass.config_entries.subentries.async_configure(
        initial["flow_id"], {CONF_RUNTIME_MODE: s.mode}
    )
    gate = _Gate(s.hass)
    with patch.object(ha_storage, "write_utf8_file_atomic", side_effect=gate.write):
        confirm = asyncio.create_task(
            s.hass.config_entries.subentries.async_configure(
                source_form["flow_id"], raw
            )
        )
        try:
            await asyncio.wait_for(gate.started.wait(), 5)
            confirm.cancel()
            await _blocked(confirm)
        finally:
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await confirm
    assert dict(subentry.data) == old_data
    assert writer._active_since is None
    assert s.manager.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
    assert s.manager._data == _disk(s.manager)
    s.clock.value = 40
    _, result = await _reconfigure_runtime_source(
        s.hass, s.entry, subentry.subentry_id, runtime_mode=s.mode, source_input=raw
    )
    assert result["reason"] == "reconfigure_successful"
    await s.hass.async_block_till_done()
    assert writer._pending == []
    assert subentry.data["source_identity"]["registry_id"] == replacement.id
    assert (
        subentry.data["source_identity"]["authorization_id"]
        != old_data["source_identity"]["authorization_id"]
    )
    s.clock.value = 60
    assert await s.writer.async_checkpoint_runtime() == Decimal(1030)
    assert s.manager._data == _disk(s.manager)


async def test_observer_process_exits_before_replacement_then_explicit_authorization(
    scenario, tmp_path
):
    """Three sequential child OS processes; no observing process spans replacement."""
    import json
    import os
    import shutil
    import sys

    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers.storage import Store

    from tests.test_runtime import _ORIGINAL_ASYNC_WRITE_DATA

    s = scenario
    await s.start_checkpoint()
    assert await s.hass.config_entries.async_unload(s.entry.entry_id)
    with patch.object(Store, "_async_write_data", _ORIGINAL_ASYNC_WRITE_DATA):
        for owner in (s.hass.config_entries, s.registry, dr.async_get(s.hass)):
            real = Store(
                s.hass,
                owner._store.version,
                owner._store.key,
                minor_version=owner._store.minor_version,
            )
            await real.async_save(owner._data_to_save())
    config = tmp_path / "sequential-process-config"
    shutil.copytree(Path(s.hass.config.config_dir) / ".storage", config / ".storage")
    worker = Path(__file__).with_name("runtime_restart_worker.py")
    process_ids = []
    for phase, case, starting_total in (
        ("original", "valid", "1010"),
        ("replace-only", "replacement", "1040"),
        ("verify-reauthorize", "replacement", "1040"),
    ):
        env = {
            **os.environ,
            "DL_BL04_RESTART_CONFIG": str(config),
            "DL_BL04_RESTART_TEST_ROOT": str(tmp_path),
            "DL_BL04_RESTART_CASE": case,
            "DL_BL04_RESTART_PHASE": phase,
            "DL_BL04_RESTART_TOTAL": starting_total,
        }
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-ra",
            str(worker),
            "--basetemp=" + str(tmp_path / ("worker-temp-" + phase)),
            "--override-ini=cache_dir=" + str(tmp_path / ("worker-cache-" + phase)),
            "--junitxml=" + str(tmp_path / ("worker-" + phase + ".xml")),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await asyncio.wait_for(proc.communicate(), 30)
        (tmp_path / ("worker-" + phase + ".log")).write_bytes(output)
        assert proc.returncode == 0, output.decode()
        # This exited process is awaited before the next process is created.
        observation = json.loads((config / f"restart-{phase}.json").read_text())
        assert observation["pid"] == proc.pid and proc.pid != os.getpid()
        process_ids.append(proc.pid)
    assert len(set(process_ids)) == 3
    first = json.loads((config / "restart-original.json").read_text())
    replaced = json.loads((config / "restart-replace-only.json").read_text())
    final = json.loads((config / "restart-verify-reauthorize.json").read_text())
    assert Decimal(first["final_total"]) == Decimal(1040)
    assert replaced["old_registry_id"] == first["registry_id"]
    assert replaced["new_registry_id"] != first["registry_id"]
    assert Decimal(final["paused_total"]) == Decimal(1040)
    assert Decimal(final["final_total"]) == Decimal(1060)
    assert final["authorization"]["registry_id"] == replaced["new_registry_id"]
    assert final["asset_uuid"] == ASSET_UUID and final["asset_id"] == "DL0007"
    saved = json.loads((config / ".storage/core.config_entries").read_text())
    dl = next(entry for entry in saved["data"]["entries"] if entry["domain"] == DOMAIN)
    assert dl["subentries"][0]["data"]["source_identity"] == final["authorization"]


@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("wait", ["runtime-lock", "atomic-write"])
@pytest.mark.parametrize("change", ["registry-aba", "presentation"])
async def test_reconfigure_preflight_never_restores_a_concurrently_revoked_binding(
    scenario, confirmed, wait, change
):
    """Consent/settings captured before awaited I/O cannot undo a detected gap."""
    from homeassistant.helpers import device_registry as dr

    s = scenario
    await s.start_checkpoint()
    subentry = next(iter(s.entry.subentries.values()))
    original_binding = deepcopy(subentry.data["source_identity"])
    if s.mode == RUNTIME_MODE_ON:
        dr.async_get(s.hass).async_update_device(
            s.device.id, name_by_user="Updated Runtime title"
        )
        await s.hass.async_block_till_done()
    raw = {CONF_SOURCE_ENTITY_ID: s.source.entity_id, "confirm_source": confirmed}
    if s.mode == RUNTIME_MODE_POWER:
        raw.update({CONF_POWER_THRESHOLD: 20, CONF_POWER_HYSTERESIS: 2})
    initial = await s.entry.start_subentry_reconfigure_flow(
        s.hass, subentry.subentry_id
    )
    form = await s.hass.config_entries.subentries.async_configure(
        initial["flow_id"], {CONF_RUNTIME_MODE: s.mode}
    )
    writer = s.writer
    gate = _Gate(s.hass)
    if wait == "runtime-lock":
        await writer._runtime_lock.acquire()
    else:
        s.clock.value = 20
    with patch.object(ha_storage, "write_utf8_file_atomic", new=gate.write):
        submit = asyncio.create_task(
            s.hass.config_entries.subentries.async_configure(form["flow_id"], raw)
        )
        try:
            if wait == "atomic-write":
                await asyncio.wait_for(gate.started.wait(), 5)
                assert [(p.expected_total, p.delta) for p in writer._pending] == [
                    (Decimal(1010), Decimal(10))
                ]
            else:
                await _blocked(submit)
            if change == "registry-aba":
                s.registry.async_remove(s.source.entity_id)
                s.hass.states.async_remove(s.source.entity_id)
                restored = s.registry.async_get_or_create(
                    s.source.domain,
                    s.source.platform,
                    s.source.unique_id,
                    config_entry=s.owner,
                    device_id=s.device.id,
                    suggested_object_id="bl04_source",
                    original_device_class=(
                        "power" if s.mode == RUNTIME_MODE_POWER else None
                    ),
                )
                assert restored.id == s.source.id
                assert restored.entity_id == s.source.entity_id
                s.hass.states.async_set(restored.entity_id, s.active, s.attrs)
                live_binding = deepcopy(subentry.data["source_identity"])
                assert "invalidated" in live_binding
                assert writer._active_since is None
            else:
                s.registry.async_update_entity(s.source.entity_id, name="Cosmetic")
                assert subentry.data["source_identity"] == original_binding
            s.clock.value = 40
        finally:
            # Never block the executor on cleanup or on the control's reload.
            gate.release.set()
            if wait == "runtime-lock":
                writer._runtime_lock.release()
            result = await submit
    await s.hass.async_block_till_done()
    s.clock.value = 60
    actual = await s.manager.async_checkpoint_runtime(ASSET_UUID)
    assert s.manager._data == _disk(s.manager)
    if change == "presentation":
        assert actual == Decimal(1060)
        assert result["reason"] == "reconfigure_successful"
        assert "invalidated" not in subentry.data["source_identity"]
        if not confirmed:
            assert subentry.data["source_identity"] == original_binding
        return
    safe_total = Decimal(1020 if wait == "atomic-write" else 1010)
    assert actual == safe_total
    assert subentry.data["source_identity"] == live_binding
    assert result["type"] == "form"
    assert result["errors"] == {"base": "source_confirmation_required"}
    assert not writer._pending
    # A refused earlier confirmation is not reused: resubmit an explicit one.
    retry = await s.hass.config_entries.subentries.async_configure(
        form["flow_id"], {**raw, "confirm_source": True}
    )
    assert retry["reason"] == "reconfigure_successful"
    await s.hass.async_block_till_done()
    assert (
        subentry.data["source_identity"]["authorization_id"]
        != original_binding["authorization_id"]
    )
    s.clock.value = 80
    assert await s.manager.async_checkpoint_runtime(ASSET_UUID) == safe_total + 20
    assert s.manager._data == _disk(s.manager)


@pytest.mark.parametrize("flow_kind", ["create", "legacy-confirmation"])
@pytest.mark.parametrize("change", ["registry-aba", "presentation"])
async def test_initial_authorization_does_not_outlive_a_selection_gap(
    scenario, flow_kind, change
):
    """A tracker without any old binding still needs uninterrupted consent."""
    s = scenario
    subentry = next(iter(s.entry.subentries.values()))
    if flow_kind == "create":
        s.hass.config_entries.async_remove_subentry(s.entry, subentry.subentry_id)
        await s.hass.async_block_till_done()
        assert not s.entry.subentries and not s.manager._runtime_writers
        initial = await s.hass.config_entries.subentries.async_init(
            (s.entry.entry_id, SUBENTRY_TYPE_RUNTIME), context={"source": SOURCE_USER}
        )
        form = await s.hass.config_entries.subentries.async_configure(
            initial["flow_id"], {CONF_DEVICE_ID: s.device.id, CONF_RUNTIME_MODE: s.mode}
        )
        wait_lock = s.manager._mutation_lock
    else:
        assert await s.hass.config_entries.async_unload(s.entry.entry_id)
        data = dict(subentry.data)
        data.pop("source_identity")
        s.hass.config_entries.async_update_subentry(s.entry, subentry, data=data)
        assert await s.hass.config_entries.async_setup(s.entry.entry_id)
        await s.hass.async_block_till_done()
        initial = await s.entry.start_subentry_reconfigure_flow(
            s.hass, subentry.subentry_id
        )
        form = await s.hass.config_entries.subentries.async_configure(
            initial["flow_id"], {CONF_RUNTIME_MODE: s.mode}
        )
        wait_lock = s.writer._runtime_lock
    await s.state(s.active)
    raw = {CONF_SOURCE_ENTITY_ID: s.source.entity_id}
    if flow_kind != "create":
        raw["confirm_source"] = True
    if s.mode == RUNTIME_MODE_POWER:
        raw.update({CONF_POWER_THRESHOLD: 10, CONF_POWER_HYSTERESIS: 2})
    await wait_lock.acquire()
    submit = asyncio.create_task(
        s.hass.config_entries.subentries.async_configure(form["flow_id"], raw)
    )
    try:
        await _blocked(submit)
        if change == "registry-aba":
            s.registry.async_remove(s.source.entity_id)
            s.hass.states.async_remove(s.source.entity_id)
            restored = s.registry.async_get_or_create(
                s.source.domain,
                s.source.platform,
                s.source.unique_id,
                config_entry=s.owner,
                device_id=s.device.id,
                suggested_object_id="bl04_source",
                original_device_class="power" if s.mode == RUNTIME_MODE_POWER else None,
            )
            assert restored.id == s.source.id and restored.entity_id == s.source.entity_id
            s.hass.states.async_set(restored.entity_id, s.active, s.attrs)
        else:
            s.registry.async_update_entity(s.source.entity_id, name="Cosmetic")
        s.clock.value = 40
    finally:
        wait_lock.release()
    result = await submit
    await s.hass.async_block_till_done()
    s.clock.value = 60
    actual = await s.manager.async_checkpoint_runtime(ASSET_UUID)
    assert s.manager._data == _disk(s.manager)
    if change == "presentation":
        assert actual == Decimal(1020)
        assert result["type"] == ("create_entry" if flow_kind == "create" else "abort")
        return
    assert actual == Decimal(1000)
    assert result["type"] == "form"
    assert result["errors"] == {
        "base": "source_changed_during_save"
        if flow_kind == "create"
        else "source_confirmation_required"
    }
    if flow_kind == "create":
        assert not s.entry.subentries and not s.manager._runtime_writers
    else:
        assert "source_identity" not in subentry.data
    retry = await s.hass.config_entries.subentries.async_configure(form["flow_id"], raw)
    assert retry["type"] == ("create_entry" if flow_kind == "create" else "abort")
    await s.hass.async_block_till_done()
    s.clock.value = 80
    assert await s.manager.async_checkpoint_runtime(ASSET_UUID) == Decimal(1020)
    assert s.manager._data == _disk(s.manager)


async def test_cancelled_create_releases_reservation_and_selection_observer(scenario):
    s = scenario
    subentry = next(iter(s.entry.subentries.values()))
    s.hass.config_entries.async_remove_subentry(s.entry, subentry.subentry_id)
    await s.hass.async_block_till_done()
    initial = await s.hass.config_entries.subentries.async_init(
        (s.entry.entry_id, SUBENTRY_TYPE_RUNTIME), context={"source": SOURCE_USER}
    )
    form = await s.hass.config_entries.subentries.async_configure(
        initial["flow_id"], {CONF_DEVICE_ID: s.device.id, CONF_RUNTIME_MODE: s.mode}
    )
    raw = {CONF_SOURCE_ENTITY_ID: s.source.entity_id}
    if s.mode == RUNTIME_MODE_POWER:
        raw.update({CONF_POWER_THRESHOLD: 10, CONF_POWER_HYSTERESIS: 2})
    manager = s.manager
    original_reserve = manager.async_reserve_runtime_binding
    listeners = s.hass.bus.async_listeners().get(er.EVENT_ENTITY_REGISTRY_UPDATED, 0)

    async def cancel_after_reserving(entry, asset_uuid):
        release = await original_reserve(entry, asset_uuid)
        asyncio.current_task().cancel()
        return release

    with patch.object(manager, "async_reserve_runtime_binding", new=cancel_after_reserving):
        submit = asyncio.create_task(
            s.hass.config_entries.subentries.async_configure(form["flow_id"], raw)
        )
        with pytest.raises(asyncio.CancelledError):
            await submit
    assert not manager._runtime_binding_reservations()
    assert not s.entry.subentries
    assert s.hass.bus.async_listeners().get(er.EVENT_ENTITY_REGISTRY_UPDATED, 0) == listeners
    s.hass.config_entries.subentries.async_abort(form["flow_id"])
