"""0.7.7: the legacy entity migration moves identities, never placement.

Setup runs reconciliation, then the legacy unique-ID migration, then the
exposure reconciliation that places every entity on its Asset Device. Before
0.7.7 the migration also re-attached Lifecycle and Runtime entities to the
external Home Assistant device and to the Purchase or Runtime subentry a
configuration lists, only for exposure to move them straight back. That
happened on every setup and reload, and when exposure then failed, the
temporary placement stayed behind.

These tests watch the Entity Registry events themselves, so a placement that
is changed and changed back within one setup is caught even though the final
state would look the same.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import attr
import pytest
from homeassistant.components.sensor import SensorExtraStoredData
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform, UnitOfTime
from homeassistant.core import Event, HomeAssistant, State
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache_with_extra_data,
)

from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_CURRENCY,
    CONF_PURCHASE_NAME,
    CONF_PURCHASE_UUID,
    CONF_RECEIPT_URL,
    CONF_RUNTIME_DATA_VERSION,
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
)
from custom_components.device_lifecycle.exposure import (
    asset_device_entry,
    replacement_unique_id,
)
from custom_components.device_lifecycle.migration import (
    async_migrate_entity_registry,
    lifecycle_unique_id,
    runtime_unique_id,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import STORAGE_KEY

from .conftest import ASSET_UUID, PURCHASE_UUID, SOURCE_ENTITY_ID
from .test_exposure import _manager
from .test_exposure_options_reload import _verified_store_readback
from .test_stale_device_registry_references import (
    _device,
    _load,
    _purchase,
    _refs,
    _reload,
    _runtime,
)
from .test_stale_reference_repairs import _device_container, _owned

pytestmark = pytest.mark.real_reload

SECOND_PURCHASE_UUID = "44444444-4444-4444-8444-444444444444"
LIFECYCLE_UID = lifecycle_unique_id(ASSET_UUID)
RUNTIME_UID = runtime_unique_id(ASSET_UUID)
STALE_WARNING = (
    "The Home Assistant device that {source} lists for Asset DL0007 is no "
    "longer in the Device Registry; keeping the stored reference and "
    "continuing setup"
)

Placement = tuple[str, str | None, str | None]


# --- Watching the Entity Registry ----------------------------------------------


@contextmanager
def _placement_changes(hass: HomeAssistant) -> Iterator[list[dict[str, Any]]]:
    """Record every device or config-subentry change of a Device Lifecycle entity.

    Home Assistant fires one `update` event per registry write and names the
    changed fields with their previous values, so a move that is undone later
    in the same setup still appears here twice.
    """
    registry = er.async_get(hass)
    changes: list[dict[str, Any]] = []

    def _listener(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        data = event.data
        if data["action"] != "update":
            return
        registry_entry = registry.async_get(data["entity_id"])
        if registry_entry is not None and registry_entry.platform != DOMAIN:
            return
        moved = set(data["changes"]) & {"device_id", "config_subentry_id"}
        if moved:
            changes.append(
                {"entity_id": data["entity_id"], "changes": data["changes"]}
            )

    unsubscribe = hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _listener)
    try:
        yield changes
    finally:
        unsubscribe()


def _placement(hass: HomeAssistant, unique_id: str) -> Placement:
    """Return entity ID, device, and config subentry of one sensor identity."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(Platform.SENSOR, DOMAIN, unique_id)
    assert entity_id is not None, f"{unique_id} is not in the Entity Registry"
    registry_entry = registry.async_get(entity_id)
    return entity_id, registry_entry.device_id, registry_entry.config_subentry_id


def _placements(hass: HomeAssistant) -> dict[str, Placement]:
    """Return the Lifecycle and Runtime placements of the test Asset."""
    return {uid: _placement(hass, uid) for uid in (LIFECYCLE_UID, RUNTIME_UID)}


def _subentry_id(entry: MockConfigEntry, subentry_type: str) -> str:
    """Return the ID of the entry's only subentry of one type."""
    return next(
        subentry.subentry_id
        for subentry in entry.subentries.values()
        if subentry.subentry_type == subentry_type
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return every Device Lifecycle warning, in order."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
        and record.name.startswith("custom_components.device_lifecycle")
    ]


# --- A canonical installation ---------------------------------------------------


async def _install(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
    *,
    relinked: bool = False,
) -> tuple[MockConfigEntry, dr.DeviceEntry]:
    """Load an Asset tracked by a Purchase and a Runtime configuration.

    With `relinked`, the person has since moved the Asset to a second
    Purchase while the first Purchase configuration still lists its device:
    the 0.7.4 G3 steady state.
    """
    data = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    asset["runtime"]["total_seconds"] = "3600"
    device = _device(hass, device_registry, "placement-primary", asset)
    asset["ha_device_refs"] = _refs(device.id)
    subentries = [
        _purchase(purchase_subentry_data, device.id),
        _runtime(runtime_subentry_data, device.id),
    ]
    if relinked:
        second = deepcopy(data["purchases"][PURCHASE_UUID])
        second.update(
            {
                "purchase_uuid": SECOND_PURCHASE_UUID,
                "config_subentry_id": None,
                "name": "Second purchase",
                "asset_uuids": [ASSET_UUID],
            }
        )
        data["purchases"][SECOND_PURCHASE_UUID] = second
        data["purchases"][PURCHASE_UUID]["asset_uuids"] = []
        asset["purchase_uuid"] = SECOND_PURCHASE_UUID
        asset["field_sources"]["purchase_uuid"] = "user"
        second_subentry = _purchase(purchase_subentry_data)
        second_subentry["data"][CONF_PURCHASE_UUID] = SECOND_PURCHASE_UUID
        second_subentry["data"][CONF_PURCHASE_NAME] = "Second purchase"
        second_subentry["title"] = "Second purchase"
        subentries.append(second_subentry)
    hass.states.async_set(SOURCE_ENTITY_ID, "0", {"unit_of_measurement": "W"})
    entry = await _load(hass, hass_storage, data, *subentries)
    return entry, device


# --- A / H: a converged setup moves nothing -------------------------------------


@pytest.mark.parametrize("device_state", ["present", "removed"])
async def test_repeated_reloads_move_no_entity(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
    caplog: pytest.LogCaptureFixture,
    device_state: str,
) -> None:
    """Two real reloads of a canonical installation write no placement.

    With the listed device removed, the stale-device warning still appears
    once per configuration and setup, exactly as before.
    """
    entry, device = await _install(
        hass,
        hass_storage,
        asset_store_data,
        purchase_subentry_data,
        runtime_subentry_data,
        device_registry,
    )
    if device_state == "removed":
        device_registry.async_remove_device(device.id)
        await hass.async_block_till_done()
    before = _placements(hass)
    caplog.clear()
    caplog.set_level(logging.INFO, logger="custom_components.device_lifecycle")

    with _placement_changes(hass) as changes:
        await _reload(hass, hass_storage, entry)
        await _reload(hass, hass_storage, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert changes == []
    assert _placements(hass) == before
    # The migration log line now means a unique ID actually moved.
    assert "Migrated Device Lifecycle entity" not in caplog.text
    if device_state == "removed":
        per_setup = [
            STALE_WARNING.format(source="a Purchase configuration"),
            STALE_WARNING.format(source="a Runtime configuration"),
        ]
        assert _warnings(caplog) == per_setup * 2
    else:
        assert _warnings(caplog) == []


# --- B: G3 relinked Purchase ------------------------------------------------------


async def test_a_relinked_asset_never_borrows_the_stale_purchase_subentry(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    """The legacy Purchase still lists the device; the entity stays put."""
    entry, _device_entry = await _install(
        hass,
        hass_storage,
        asset_store_data,
        purchase_subentry_data,
        runtime_subentry_data,
        device_registry,
        relinked=True,
    )
    before = _placements(hass)

    with _placement_changes(hass) as changes:
        await _reload(hass, hass_storage, entry)
        await _reload(hass, hass_storage, entry)

    assert changes == []
    assert _placements(hass) == before
    assert before[LIFECYCLE_UID][2] is None
    asset = entry.runtime_data.asset(ASSET_UUID)
    assert asset["purchase_uuid"] == SECOND_PURCHASE_UUID
    assert asset["field_sources"]["purchase_uuid"] == "user"
    assert ASSET_UUID not in entry.runtime_data.purchase(PURCHASE_UUID)["asset_uuids"]


# --- C / D: a failed setup keeps placement, Purchase removal keeps Lifecycle ------


async def test_a_failed_preflight_leaves_placement_and_purchase_removal_keeps_lifecycle(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Before 0.7.7 this left Lifecycle in the Purchase subentry, and removing
    that Purchase then deleted the Lifecycle entity from the Entity Registry.
    """
    entry, _device_entry = await _install(
        hass,
        hass_storage,
        asset_store_data,
        purchase_subentry_data,
        runtime_subentry_data,
        device_registry,
    )
    before = _placements(hass)
    # Exposure fails closed on a foreign claim to one of the Asset's identities.
    own = entity_registry.async_get_entity_id(
        Platform.SENSOR, DOMAIN, replacement_unique_id(ASSET_UUID)
    )
    entity_registry.async_remove(own)
    foreign = MockConfigEntry(domain="test", title="Foreign", data={})
    foreign.add_to_hass(hass)
    collision = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        replacement_unique_id(ASSET_UUID),
        config_entry=foreign,
        suggested_object_id="foreign_collision",
    )

    with _placement_changes(hass) as changes:
        await _reload(hass, hass_storage, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert changes == []
    assert _placements(hass) == before

    hass.config_entries.async_remove_subentry(
        entry, _subentry_id(entry, SUBENTRY_TYPE_PURCHASE)
    )
    await hass.async_block_till_done()

    # Same registry row: same unique ID resolves to the same entity ID.
    assert _placement(hass, LIFECYCLE_UID) == before[LIFECYCLE_UID]

    # Once the collision is resolved the entry loads with the same identities.
    entity_registry.async_remove(collision.entity_id)
    await _reload(hass, hass_storage, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert _placements(hass) == before


# --- E: exposure execution failure and rollback -----------------------------------


async def test_an_exposure_failure_before_any_entity_move_keeps_placement(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exposure fails while projecting the Asset Device: nothing moved first."""
    entry, _device_entry = await _install(
        hass,
        hass_storage,
        asset_store_data,
        purchase_subentry_data,
        runtime_subentry_data,
        device_registry,
    )
    before = _placements(hass)

    with (
        patch.object(
            device_registry,
            "async_update_device",
            side_effect=RuntimeError("injected Asset Device failure"),
        ),
        _placement_changes(hass) as changes,
    ):
        await _reload(hass, hass_storage, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert "was rolled back" in caplog.text
    assert changes == []
    assert _placements(hass) == before


async def test_rollback_after_a_partial_projection_restores_setup_entry_placement(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Exposure's rollback now restores what setup started from.

    Runtime is left on the external device, as an interrupted projection can
    leave it, and exposure's move of it fails. Before 0.7.7 the rollback
    restored the migration's temporary placement of Lifecycle instead.
    """
    entry, device = await _install(
        hass,
        hass_storage,
        asset_store_data,
        purchase_subentry_data,
        runtime_subentry_data,
        device_registry,
    )
    runtime_entity_id = _placement(hass, RUNTIME_UID)[0]
    entity_registry.async_update_entity(runtime_entity_id, device_id=device.id)
    before = _placements(hass)
    original_update = entity_registry.async_update_entity
    attempted: list[dict[str, Any]] = []

    def _fail_runtime_move(entity_id: str, **kwargs: Any) -> er.RegistryEntry:
        if (
            entity_id == runtime_entity_id
            and kwargs.get("device_id") not in (None, device.id)
        ):
            attempted.append(kwargs)
            if len(attempted) == 1:
                raise RuntimeError("injected Runtime move failure")
        return original_update(entity_id, **kwargs)

    with patch.object(
        entity_registry,
        "async_update_entity",
        side_effect=_fail_runtime_move,
    ):
        await _reload(hass, hass_storage, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert attempted, "exposure never attempted the Runtime move"
    assert _placements(hass) == before


# --- Historical registry shapes ----------------------------------------------------
#
# The shapes below were verified against the real 0.4.4, 0.5.0, 0.5.6, 0.5.7,
# 0.6.1, 0.7.0, and 0.7.3 integrations: until 0.6.0 every Lifecycle entity sat
# in its Purchase subentry and every Runtime entity in its Runtime subentry,
# both on the external device. 0.4.x additionally used subentry- and
# device-derived unique IDs. 0.6.0 and later are already canonical, which the
# reload tests above cover.


def _seed_store(
    hass_storage: dict,
    version: int,
    minor_version: int,
    data: AssetStoreData,
) -> None:
    """Persist one Asset Store payload with an explicit schema version."""
    hass_storage[STORAGE_KEY] = {
        "version": version,
        "minor_version": minor_version,
        "key": STORAGE_KEY,
        "data": deepcopy(data),
    }


def _historical_entry(
    hass: HomeAssistant,
    version: int,
    subentries: list[dict[str, Any]],
) -> MockConfigEntry:
    """Add the parent entry as a past release left it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Device Lifecycle",
        unique_id="device_lifecycle_main",
        version=version,
        data={},
        options={},
        subentries_data=tuple(subentries),
    )
    entry.add_to_hass(hass)
    return entry


def _historical_rows(
    hass: HomeAssistant,
    entity_registry: er.EntityRegistry,
    entry: MockConfigEntry,
    device: dr.DeviceEntry,
    *,
    legacy_unique_ids: bool,
) -> tuple[str, str]:
    """Register the pre-0.6 Lifecycle and Runtime rows with restore data."""
    purchase_id = _subentry_id(entry, SUBENTRY_TYPE_PURCHASE)
    runtime_id = _subentry_id(entry, SUBENTRY_TYPE_RUNTIME)
    lifecycle = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        f"{purchase_id}_{device.id}_lifecycle"
        if legacy_unique_ids
        else LIFECYCLE_UID,
        suggested_object_id="historic_lifecycle",
        config_entry=entry,
        config_subentry_id=purchase_id,
        device_id=device.id,
    )
    runtime = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        f"{runtime_id}_{device.id}_runtime_hours"
        if legacy_unique_ids
        else RUNTIME_UID,
        suggested_object_id="historic_runtime_hours",
        config_entry=entry,
        config_subentry_id=runtime_id,
        device_id=device.id,
    )
    entity_registry.async_update_entity(lifecycle.entity_id, name="Kept name")
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(
                    runtime.entity_id,
                    "12.5",
                    {"unit_of_measurement": UnitOfTime.HOURS},
                ),
                SensorExtraStoredData(Decimal("12.5"), UnitOfTime.HOURS).as_dict(),
            )
        ],
    )
    return lifecycle.entity_id, runtime.entity_id


@pytest.mark.parametrize("era", ["0.4.x", "0.5.0-0.5.6", "0.5.7"])
async def test_historical_installations_upgrade_to_canonical_placement(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    asset_store_data_v1_1: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    era: str,
) -> None:
    """Every supported past registry shape converges, then stays still.

    0.4.x has no Store, ConfigEntry version 3, and subentry-derived unique
    IDs; its Runtime hours are imported from RestoreSensor data. 0.5.0-0.5.6
    have Store 1.x without Runtime totals, so they import too. 0.5.7 has a
    Store 2.1 Runtime total, which wins over the restore data.
    """
    device = _device(
        hass, device_registry, "historic", asset_store_data["assets"][ASSET_UUID]
    )
    hass.states.async_set(SOURCE_ENTITY_ID, "0", {"unit_of_measurement": "W"})
    purchase = _purchase(purchase_subentry_data, device.id)
    runtime = _runtime(runtime_subentry_data, device.id)
    if era == "0.4.x":
        for key in (CONF_PURCHASE_UUID, CONF_CURRENCY, CONF_RECEIPT_URL):
            purchase["data"].pop(key, None)
        del runtime["data"][CONF_ASSET_UUID]
        del runtime["data"][CONF_RUNTIME_DATA_VERSION]
        entry = _historical_entry(hass, 3, [purchase, runtime])
        expected_total = Decimal(45000)
    elif era == "0.5.0-0.5.6":
        data = deepcopy(asset_store_data_v1_1)
        data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(device.id)
        del runtime["data"][CONF_RUNTIME_DATA_VERSION]
        _seed_store(hass_storage, 1, 1, data)
        entry = _historical_entry(hass, CONFIG_ENTRY_VERSION, [purchase, runtime])
        expected_total = Decimal(45000)
    else:
        data = deepcopy(asset_store_data)
        data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(device.id)
        data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "1234.5"
        for asset in data["assets"].values():
            del asset["lifecycle"]
        del data["lifecycle_events"]
        del data["replacement_records"]
        _seed_store(hass_storage, 2, 1, data)
        entry = _historical_entry(hass, CONFIG_ENTRY_VERSION, [purchase, runtime])
        expected_total = Decimal("1234.5")
    lifecycle_entity_id, runtime_entity_id = _historical_rows(
        hass,
        entity_registry,
        entry,
        device,
        legacy_unique_ids=era == "0.4.x",
    )

    with _verified_store_readback(hass_storage):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    manager = entry.runtime_data
    asset_uuid = next(asset["asset_uuid"] for asset in manager.assets())
    asset_device = asset_device_entry(
        device_registry, config_entry_id=entry.entry_id, asset_uuid=asset_uuid
    )
    assert asset_device is not None
    assert _placement(hass, lifecycle_unique_id(asset_uuid)) == (
        lifecycle_entity_id,
        asset_device.id,
        None,
    )
    assert _placement(hass, runtime_unique_id(asset_uuid)) == (
        runtime_entity_id,
        asset_device.id,
        _subentry_id(entry, SUBENTRY_TYPE_RUNTIME),
    )
    assert entity_registry.async_get(lifecycle_entity_id).name == "Kept name"
    assert manager.runtime_total_seconds(asset_uuid) == expected_total

    with _placement_changes(hass) as changes:
        await _reload(hass, hass_storage, entry)
        await _reload(hass, hass_storage, entry)

    assert changes == []


async def test_rollback_restores_a_historical_setup_entry_placement(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """A failed first projection of a pre-0.6 shape leaves that shape intact."""
    device = _device(
        hass, device_registry, "rollback", asset_store_data["assets"][ASSET_UUID]
    )
    hass.states.async_set(SOURCE_ENTITY_ID, "0", {"unit_of_measurement": "W"})
    data = deepcopy(asset_store_data)
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(device.id)
    data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "3600"
    _seed_store(hass_storage, 3, 1, data)
    entry = _historical_entry(
        hass,
        CONFIG_ENTRY_VERSION,
        [
            _purchase(purchase_subentry_data, device.id),
            _runtime(runtime_subentry_data, device.id),
        ],
    )
    lifecycle_entity_id, runtime_entity_id = _historical_rows(
        hass, entity_registry, entry, device, legacy_unique_ids=False
    )
    before = _placements(hass)
    original_update = entity_registry.async_update_entity

    def _fail_runtime_move(entity_id: str, **kwargs: Any) -> er.RegistryEntry:
        if entity_id == runtime_entity_id and "device_id" in kwargs:
            raise RuntimeError("injected Runtime move failure")
        return original_update(entity_id, **kwargs)

    with (
        _verified_store_readback(hass_storage),
        patch.object(
            entity_registry,
            "async_update_entity",
            side_effect=_fail_runtime_move,
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert before[LIFECYCLE_UID][0] == lifecycle_entity_id
    assert _placements(hass) == before


# --- F: the migration on its own ---------------------------------------------------


async def test_the_migration_moves_only_the_unique_id(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """A legacy row is renamed in place; no row is attached anywhere.

    Before 0.7.7 both rows would have been moved to the listed device and to
    the Purchase or Runtime subentry.
    """
    device = _device(
        hass, device_registry, "unit", asset_store_data["assets"][ASSET_UUID]
    )
    asset_store_data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(device.id)
    manager = _manager(hass, asset_store_data)
    entry = _historical_entry(
        hass,
        CONFIG_ENTRY_VERSION,
        [
            _purchase(purchase_subentry_data, device.id),
            _runtime(runtime_subentry_data, device.id),
        ],
    )
    purchase_id = _subentry_id(entry, SUBENTRY_TYPE_PURCHASE)
    runtime_id = _subentry_id(entry, SUBENTRY_TYPE_RUNTIME)
    legacy = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        f"{purchase_id}_{device.id}_lifecycle",
        config_entry=entry,
    )
    canonical = entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        RUNTIME_UID,
        config_entry=entry,
        config_subentry_id=runtime_id,
    )

    with _placement_changes(hass) as changes:
        await async_migrate_entity_registry(hass, entry, manager)
        await hass.async_block_till_done()

    assert changes == []
    assert _placement(hass, LIFECYCLE_UID) == (legacy.entity_id, None, None)
    assert _placement(hass, RUNTIME_UID) == (canonical.entity_id, None, runtime_id)


# --- I: composite Device Registry IDs ------------------------------------------------


async def test_a_composite_purchase_device_keeps_its_warning_and_moves_nothing(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pre-migration composite ID still resolves for Repairs, but Home
    Assistant would not attach an entity to it, so the migration warning
    keeps firing exactly as in 0.7.5 and 0.7.6.
    """
    data = deepcopy(asset_store_data)
    split = _device(hass, device_registry, "split", data["assets"][ASSET_UUID])
    composite_id = "0123456789abcdef0123456789abcdef"
    _device_container(device_registry)[split.id] = attr.evolve(
        split, composite_device_id=composite_id
    )
    assert device_registry.async_get(composite_id) is not None
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(composite_id)
    entry = await _load(
        hass, hass_storage, data, _purchase(purchase_subentry_data, composite_id)
    )
    assert _owned(hass) == {}
    caplog.clear()

    with _placement_changes(hass) as changes:
        await _reload(hass, hass_storage, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert changes == []
    assert _warnings(caplog) == [
        STALE_WARNING.format(source="a Purchase configuration")
    ]
    assert _owned(hass) == {}
    asset_device = asset_device_entry(
        device_registry, config_entry_id=entry.entry_id, asset_uuid=ASSET_UUID
    )
    assert _placement(hass, LIFECYCLE_UID)[1:] == (asset_device.id, None)
