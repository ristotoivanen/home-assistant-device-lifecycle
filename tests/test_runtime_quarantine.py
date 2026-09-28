"""Runtime tracking that resolves to an archived Asset is quarantined (WP12).

A restored backup can hold an archived Asset and a Runtime ConfigSubentry
that still resolves to it. Setup must not fail and must not write: the
subentry is kept exactly, no Runtime writer, initialization, or import runs
for it, the Asset's parent-owned Runtime entity keeps its identity, one
Repairs issue reports the conflict, and every other Asset keeps working.

No production path archives an Asset yet, so the Archive state (and the
Restore exit) is written straight into the persisted Store 4.1 here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from types import MappingProxyType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.device_lifecycle import sensor
from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_SOURCE_ENTITY_ID,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DOMAIN,
    SUBENTRY_TYPE_RUNTIME,
)
from custom_components.device_lifecycle.exposure import _runtime_subentries_by_asset
from custom_components.device_lifecycle.migration import runtime_unique_id
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.runtime_conflicts import (
    TRANSLATION_KEY,
    async_sync_runtime_conflict_issues,
    is_owned_runtime_conflict_issue,
    runtime_conflict_issue_id,
)
from custom_components.device_lifecycle.stale_references import (
    is_owned_stale_reference_issue,
    stale_reference_issue_id,
)
from custom_components.device_lifecycle.storage import (
    STORAGE_KEY,
    AssetStoreError,
    AssetStoreManager,
)

from .conftest import ASSET_UUID
from .test_exposure_options_reload import _verified_store_readback
from .test_stale_device_registry_references import (
    SECOND_ASSET_UUID,
    _device,
    _load,
    _purchase,
    _recorded_saves,
    _refs,
    _reload,
    _runtime,
    _stored,
    _with_second_asset,
)

pytestmark = pytest.mark.real_reload

ARCHIVED_AT = "2026-09-26T12:34:56.123456+00:00"
ARCHIVED_TOTAL = "3600"
ACTIVE_TOTAL = "100"
SECOND_SOURCE = "sensor.garage_heater_power"


# --- Building a restored-backup installation ---------------------------------


def _installation(
    hass: HomeAssistant,
    registry: dr.DeviceRegistry,
    asset_store_data: AssetStoreData,
) -> tuple[dict[str, Any], dr.DeviceEntry, dr.DeviceEntry]:
    """Two Assets with their own devices and initialized Runtime totals."""
    data: dict[str, Any] = _with_second_asset(deepcopy(asset_store_data))
    first = _device(hass, registry, "archived-device", data["assets"][ASSET_UUID])
    second = _device(hass, registry, "active-device", data["assets"][SECOND_ASSET_UUID])
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(first.id)
    data["assets"][SECOND_ASSET_UUID]["ha_device_refs"] = _refs(second.id)
    data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = ARCHIVED_TOTAL
    data["assets"][SECOND_ASSET_UUID]["runtime"]["total_seconds"] = ACTIVE_TOTAL
    return data, first, second


def _archive(data: dict[str, Any]) -> None:
    asset = data["assets"][ASSET_UUID]
    asset["deployment_state"] = DEPLOYMENT_STATE_NOT_DEPLOYED
    asset["ha_area_id"] = None
    asset["archived_at"] = ARCHIVED_AT


def _runtime_subentries(
    runtime_subentry_data: dict[str, Any],
    first: dr.DeviceEntry,
    second: dr.DeviceEntry,
    *,
    legacy: bool,
) -> tuple[dict, dict]:
    archived = _runtime(runtime_subentry_data, first.id)
    if legacy:
        # A device-only binding from before the Asset UUID was written back.
        del archived["data"][CONF_ASSET_UUID]
    active = _runtime(runtime_subentry_data, second.id)
    active["data"][CONF_ASSET_UUID] = SECOND_ASSET_UUID
    active["data"][CONF_SOURCE_ENTITY_ID] = SECOND_SOURCE
    active["title"] = "Garage heater"
    return archived, active


def _subentry_id(entry: Any, asset_device_id: str) -> str:
    return next(
        subentry.subentry_id
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE_RUNTIME
        and subentry.data.get("device_id") == asset_device_id
    )


def _owned_conflicts(hass: HomeAssistant) -> dict[str, ir.IssueEntry]:
    return {
        issue_id: issue
        for (domain, issue_id), issue in ir.async_get(hass).issues.items()
        if is_owned_runtime_conflict_issue(domain, issue_id)
    }


def _runtime_registry_entry(hass: HomeAssistant, asset_uuid: str) -> Any:
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, runtime_unique_id(asset_uuid)
    )
    return None if entity_id is None else registry.async_get(entity_id)


@contextmanager
def _runtime_calls() -> Iterator[list[tuple[str, str]]]:
    """Record every Runtime initialization, import, and sensor construction."""
    calls: list[tuple[str, str]] = []
    initialize = AssetStoreManager.async_initialize_new_runtime
    import_legacy = AssetStoreManager.async_import_legacy_runtime
    runtime_sensor = sensor.DeviceRuntimeHoursSensor.__init__

    async def _initialize(self: AssetStoreManager, asset_uuid: str) -> Any:
        calls.append(("initialize", asset_uuid))
        return await initialize(self, asset_uuid)

    async def _import(self: AssetStoreManager, asset_uuid: str, total: Any) -> Any:
        calls.append(("import", asset_uuid))
        return await import_legacy(self, asset_uuid, total)

    def _sensor(self: Any, **kwargs: Any) -> None:
        calls.append(("sensor", kwargs["asset"]["asset_uuid"]))
        runtime_sensor(self, **kwargs)

    with (
        patch.object(AssetStoreManager, "async_initialize_new_runtime", _initialize),
        patch.object(AssetStoreManager, "async_import_legacy_runtime", _import),
        patch.object(sensor.DeviceRuntimeHoursSensor, "__init__", _sensor),
    ):
        yield calls


async def _archived_installation(
    hass: HomeAssistant,
    hass_storage: dict,
    registry: dr.DeviceRegistry,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
    *,
    legacy: bool,
) -> tuple[Any, dr.DeviceEntry, dr.DeviceEntry]:
    """Load both Assets active first (as the installation once ran), then
    return to it as a restored backup with the first Asset archived."""
    data, first, second = _installation(hass, registry, asset_store_data)
    archived_runtime, active_runtime = _runtime_subentries(
        runtime_subentry_data, first, second, legacy=legacy
    )
    if legacy:
        _archive(data)
    entry = await _load(
        hass,
        hass_storage,
        data,  # type: ignore[arg-type]
        _purchase(purchase_subentry_data, first.id, second.id),
        archived_runtime,
        active_runtime,
    )
    if not legacy:
        assert _runtime_registry_entry(hass, ASSET_UUID) is not None
        _archive(hass_storage[STORAGE_KEY]["data"])
        await _reload(hass, hass_storage, entry)
    assert entry.state is ConfigEntryState.LOADED
    return entry, first, second


# --- The canonical resolver decides ----------------------------------------------


def _manager(hass: HomeAssistant, data: dict[str, Any]) -> AssetStoreManager:
    manager = AssetStoreManager(hass)
    manager._data = deepcopy(data)  # type: ignore[assignment]
    return manager


def _entry(*subentries: tuple[str, dict[str, Any], str]) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id="entry",
        subentries={
            subentry_id: SimpleNamespace(
                subentry_id=subentry_id,
                subentry_type=subentry_type,
                data=MappingProxyType(data),
            )
            for subentry_id, data, subentry_type in subentries
        },
    )


async def test_modern_and_legacy_bindings_are_quarantined_alike(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    data, first, second = _installation(hass, device_registry, asset_store_data)
    _archive(data)
    manager = _manager(hass, data)
    entry = _entry(
        (
            "modern",
            {"device_id": first.id, CONF_ASSET_UUID: ASSET_UUID},
            SUBENTRY_TYPE_RUNTIME,
        ),
        ("legacy", {"device_id": first.id}, SUBENTRY_TYPE_RUNTIME),
        ("active", {"device_id": second.id}, SUBENTRY_TYPE_RUNTIME),
        ("unresolved", {"device_id": "unknown-device"}, SUBENTRY_TYPE_RUNTIME),
        ("no-device", {}, SUBENTRY_TYPE_RUNTIME),
        ("purchase", {"device_id": first.id}, "purchase"),
    )

    assert manager.quarantined_runtime_subentries(entry) == {"modern", "legacy"}  # type: ignore[arg-type]
    assert manager.apply_runtime_quarantine(entry) == {"modern", "legacy"}  # type: ignore[arg-type]
    assert manager.runtime_quarantine == {"modern", "legacy"}
    assert manager.quarantined_runtime_asset("modern")["asset_uuid"] == ASSET_UUID  # type: ignore[index]
    assert manager.quarantined_runtime_asset("active") is None

    data["assets"][ASSET_UUID]["archived_at"] = None
    assert _manager(hass, data).quarantined_runtime_subentries(entry) == frozenset()  # type: ignore[arg-type]


async def test_reconciliation_skips_a_quarantined_subentry_entirely(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    data, first, _second = _installation(hass, device_registry, asset_store_data)
    _archive(data)
    # The archived Asset's primary is unset: a normal reconciliation of the
    # device-only subentry would link it or create another Asset.
    data["assets"][ASSET_UUID]["ha_device_refs"] = []
    manager = _manager(hass, data)
    manager._store.async_save = AsyncMock()  # type: ignore[method-assign]
    entry = _entry(("legacy", {"device_id": first.id}, SUBENTRY_TYPE_RUNTIME))

    with patch.object(hass.config_entries, "async_update_subentry") as update:
        await manager.async_reconcile_entry(entry, quarantined=frozenset({"legacy"}))  # type: ignore[arg-type]

    update.assert_not_called()
    # The quarantined subentry links, refreshes, and creates nothing.
    assert manager._data["assets"] == data["assets"]
    assert manager._data["next_asset_number"] == data["next_asset_number"]

    # Without the quarantine the same subentry would link the archived
    # Asset's device and write its UUID back into the subentry.
    unguarded = _manager(hass, data)
    unguarded._store.async_save = AsyncMock()  # type: ignore[method-assign]
    with patch.object(hass.config_entries, "async_update_subentry") as update:
        await unguarded.async_reconcile_entry(entry, quarantined=frozenset())  # type: ignore[arg-type]
    update.assert_called_once()
    assert unguarded._data["assets"] != data["assets"]


# --- Setup with the conflict ---------------------------------------------------------


@pytest.mark.parametrize("legacy", [False, True], ids=["modern", "legacy"])
async def test_conflict_is_quarantined_and_reported_across_reloads_and_restart(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
    legacy: bool,
) -> None:
    entry, first, _second = await _archived_installation(
        hass,
        hass_storage,
        device_registry,
        asset_store_data,
        purchase_subentry_data,
        runtime_subentry_data,
        legacy=legacy,
    )
    quarantined_id = _subentry_id(entry, first.id)
    subentry_data = dict(entry.subentries[quarantined_id].data)
    assert (CONF_ASSET_UUID in subentry_data) is not legacy
    stored = _stored(hass_storage)
    registry_entry = _runtime_registry_entry(hass, ASSET_UUID)
    issue_id = runtime_conflict_issue_id(quarantined_id)

    def _assert_quarantined() -> None:
        manager: AssetStoreManager = entry.runtime_data
        assert entry.state is ConfigEntryState.LOADED
        assert manager.runtime_quarantine == {quarantined_id}
        assert dict(entry.subentries[quarantined_id].data) == subentry_data
        assert _stored(hass_storage) == stored
        asset = manager.asset(ASSET_UUID)
        assert asset is not None
        assert asset["archived_at"] == ARCHIVED_AT
        assert asset["runtime"]["total_seconds"] == ARCHIVED_TOTAL
        assert set(manager._runtime_writers) == {SECOND_ASSET_UUID}
        assert len(manager.assets()) == 2
        conflicts = _owned_conflicts(hass)
        assert list(conflicts) == [issue_id]
        issue = conflicts[issue_id]
        assert issue.is_fixable is False
        assert issue.translation_key == TRANSLATION_KEY
        assert issue.translation_placeholders == {
            "asset_name": asset["name"],
            "asset_id": asset["asset_id"],
        }
        current = _runtime_registry_entry(hass, ASSET_UUID)
        if registry_entry is None:
            assert current is None
        else:
            assert current is not None
            assert current.id == registry_entry.id
            assert current.entity_id == registry_entry.entity_id
            assert current.unique_id == runtime_unique_id(ASSET_UUID)
            assert current.config_subentry_id is None
            assert current.disabled_by is None
        entity_ids = [
            item.entity_id
            for item in er.async_entries_for_config_entry(
                er.async_get(hass), entry.entry_id
            )
        ]
        assert len(entity_ids) == len(set(entity_ids))
        assert not [entity_id for entity_id in entity_ids if entity_id.endswith("_2")]

    _assert_quarantined()
    # Dismissing the issue is not a resolution; it is never consulted.
    ir.async_ignore_issue(hass, DOMAIN, issue_id, True)

    with (
        _recorded_saves() as saves,
        _runtime_calls() as calls,
        patch.object(
            hass.config_entries,
            "async_update_subentry",
            wraps=hass.config_entries.async_update_subentry,
        ) as update_subentry,
        patch.object(
            hass.config_entries,
            "async_schedule_reload",
            wraps=hass.config_entries.async_schedule_reload,
        ) as schedule_reload,
    ):
        for _cycle in range(3):
            await _reload(hass, hass_storage, entry)
            _assert_quarantined()

        # A restart: unload, the non-persistent issue is gone, set up again.
        with _verified_store_readback(hass_storage):
            assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
        assert list(_owned_conflicts(hass)) == [issue_id]  # unload keeps it
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        with _verified_store_readback(hass_storage):
            assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
        _assert_quarantined()

    assert saves == []
    assert ("initialize", ASSET_UUID) not in calls
    assert ("import", ASSET_UUID) not in calls
    assert ("sensor", ASSET_UUID) not in calls
    assert ("sensor", SECOND_ASSET_UUID) in calls
    update_subentry.assert_not_called()
    schedule_reload.assert_not_called()


async def test_the_active_asset_keeps_tracking_runtime(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    entry, _first, _second = await _archived_installation(
        hass,
        hass_storage,
        device_registry,
        asset_store_data,
        purchase_subentry_data,
        runtime_subentry_data,
        legacy=False,
    )
    manager: AssetStoreManager = entry.runtime_data
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, runtime_unique_id(SECOND_ASSET_UUID)
    )
    assert entity_id is not None
    assert hass.states.get(entity_id) is not None
    assert SECOND_ASSET_UUID in manager._runtime_writers

    with _verified_store_readback(hass_storage):
        total = await manager.async_checkpoint_runtime(SECOND_ASSET_UUID)
        with pytest.raises(Exception, match="archived"):
            await manager.async_commit_runtime_delta(
                ASSET_UUID, expected_total=total, delta=total + 1
            )
    assert total is not None
    assert manager.runtime_total_seconds(ASSET_UUID) is not None
    assert str(manager.runtime_total_seconds(ASSET_UUID)) == ARCHIVED_TOTAL


# --- The exits --------------------------------------------------------------------------


async def test_restoring_the_asset_resumes_runtime_and_clears_the_issue(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    entry, first, _second = await _archived_installation(
        hass,
        hass_storage,
        device_registry,
        asset_store_data,
        purchase_subentry_data,
        runtime_subentry_data,
        legacy=False,
    )
    issue_id = runtime_conflict_issue_id(_subentry_id(entry, first.id))
    ir.async_ignore_issue(hass, DOMAIN, issue_id, True)
    assert list(_owned_conflicts(hass)) == [issue_id]

    # Restore: the Store is edited, as a later Restore mutation will do.
    hass_storage[STORAGE_KEY]["data"]["assets"][ASSET_UUID]["archived_at"] = None
    await _reload(hass, hass_storage, entry)

    manager: AssetStoreManager = entry.runtime_data
    assert entry.state is ConfigEntryState.LOADED
    assert manager.runtime_quarantine == frozenset()
    assert _owned_conflicts(hass) == {}
    assert set(manager._runtime_writers) == {ASSET_UUID, SECOND_ASSET_UUID}
    assert str(manager.runtime_total_seconds(ASSET_UUID)) == ARCHIVED_TOTAL


async def test_removing_the_runtime_subentry_clears_the_issue(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    entry, first, _second = await _archived_installation(
        hass,
        hass_storage,
        device_registry,
        asset_store_data,
        purchase_subentry_data,
        runtime_subentry_data,
        legacy=False,
    )
    subentry_id = _subentry_id(entry, first.id)
    registry_entry = _runtime_registry_entry(hass, ASSET_UUID)
    assert registry_entry is not None

    with _verified_store_readback(hass_storage):
        assert hass.config_entries.async_remove_subentry(entry, subentry_id)
        await hass.async_block_till_done()

    manager: AssetStoreManager = entry.runtime_data
    assert entry.state is ConfigEntryState.LOADED
    assert subentry_id not in entry.subentries
    assert _owned_conflicts(hass) == {}
    asset = manager.asset(ASSET_UUID)
    assert asset is not None
    assert asset["archived_at"] == ARCHIVED_AT
    assert asset["runtime"]["total_seconds"] == ARCHIVED_TOTAL
    current = _runtime_registry_entry(hass, ASSET_UUID)
    assert current is not None and current.entity_id == registry_entry.entity_id


async def test_a_reconfiguration_that_keeps_the_archived_target_stays_quarantined(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    entry, first, _second = await _archived_installation(
        hass,
        hass_storage,
        device_registry,
        asset_store_data,
        purchase_subentry_data,
        runtime_subentry_data,
        legacy=False,
    )
    subentry_id = _subentry_id(entry, first.id)
    subentry = entry.subentries[subentry_id]
    changed = {**subentry.data, "power_threshold": 25.0}

    with _verified_store_readback(hass_storage):
        hass.config_entries.async_update_subentry(entry, subentry, data=changed)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.runtime_quarantine == {subentry_id}
    assert dict(entry.subentries[subentry_id].data) == changed
    assert list(_owned_conflicts(hass)) == [runtime_conflict_issue_id(subentry_id)]


async def test_removing_the_entry_deletes_only_owned_conflict_issues(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    entry, _first, _second = await _archived_installation(
        hass,
        hass_storage,
        device_registry,
        asset_store_data,
        purchase_subentry_data,
        runtime_subentry_data,
        legacy=False,
    )
    for domain, issue_id in (
        ("other_integration", "runtime_archived_asset_x"),
        (DOMAIN, "unrelated"),
    ):
        ir.async_create_issue(
            hass,
            domain,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="unrelated",
        )
    assert len(_owned_conflicts(hass)) == 1

    with _verified_store_readback(hass_storage):
        await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()

    issues = ir.async_get(hass).issues
    assert _owned_conflicts(hass) == {}
    assert ("other_integration", "runtime_archived_asset_x") in issues
    assert (DOMAIN, "unrelated") in issues


# --- Repairs namespace ------------------------------------------------------------------


async def test_conflict_sync_is_idempotent_and_touches_only_its_own_issues(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    data, first, _second = _installation(hass, device_registry, asset_store_data)
    _archive(data)
    manager = _manager(hass, data)
    entry = _entry(("conflict", {"device_id": first.id}, SUBENTRY_TYPE_RUNTIME))
    quarantined = manager.apply_runtime_quarantine(entry)  # type: ignore[arg-type]
    stale_id = stale_reference_issue_id(ASSET_UUID, "primary", "gone-device")
    for domain, issue_id in (
        (DOMAIN, stale_id),
        (DOMAIN, "unrelated"),
        ("other_integration", runtime_conflict_issue_id("elsewhere")),
    ):
        ir.async_create_issue(
            hass,
            domain,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="unrelated",
        )

    for _ in range(2):
        async_sync_runtime_conflict_issues(hass, manager, quarantined)
    assert list(_owned_conflicts(hass)) == [runtime_conflict_issue_id("conflict")]

    # A subentry this setup did not quarantine names no Asset: no issue.
    async_sync_runtime_conflict_issues(hass, manager, {"conflict", "unknown"})
    assert list(_owned_conflicts(hass)) == [runtime_conflict_issue_id("conflict")]

    async_sync_runtime_conflict_issues(hass, manager, frozenset())
    issues = ir.async_get(hass).issues
    assert _owned_conflicts(hass) == {}
    assert (DOMAIN, stale_id) in issues
    assert (DOMAIN, "unrelated") in issues
    assert ("other_integration", runtime_conflict_issue_id("elsewhere")) in issues

    # Neither namespace claims the other's IDs.
    assert not is_owned_runtime_conflict_issue(DOMAIN, stale_id)
    assert not is_owned_stale_reference_issue(DOMAIN, runtime_conflict_issue_id("x"))
    assert not is_owned_runtime_conflict_issue("other", runtime_conflict_issue_id("x"))


async def test_exposure_ignores_a_quarantined_subentry_as_a_tracking_source(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    """An archived Asset whose primary was unlinked still owns the modern
    binding; its mismatching device must not fail the exposure preflight."""
    data, first, _second = _installation(hass, device_registry, asset_store_data)
    _archive(data)
    data["assets"][ASSET_UUID]["ha_device_refs"] = []
    manager = _manager(hass, data)
    entry = _entry(
        (
            "conflict",
            {"device_id": first.id, CONF_ASSET_UUID: ASSET_UUID},
            SUBENTRY_TYPE_RUNTIME,
        )
    )
    quarantined = manager.apply_runtime_quarantine(entry)  # type: ignore[arg-type]
    assert quarantined == {"conflict"}
    assets = {asset["asset_uuid"]: asset for asset in manager.assets()}

    with pytest.raises(AssetStoreError, match="exact primary device"):
        _runtime_subentries_by_asset(entry, assets)  # type: ignore[arg-type]
    assert _runtime_subentries_by_asset(entry, assets, quarantined) == {}  # type: ignore[arg-type]


async def test_setup_loads_when_the_archived_primary_was_unlinked(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    data, first, second = _installation(hass, device_registry, asset_store_data)
    _archive(data)
    data["assets"][ASSET_UUID]["ha_device_refs"] = []
    archived_runtime, active_runtime = _runtime_subentries(
        runtime_subentry_data, first, second, legacy=False
    )

    with _recorded_saves() as saves:
        entry = await _load(
            hass,
            hass_storage,
            data,  # type: ignore[arg-type]
            _purchase(purchase_subentry_data, second.id),
            archived_runtime,
            active_runtime,
        )

    subentry_id = _subentry_id(entry, first.id)
    manager: AssetStoreManager = entry.runtime_data
    assert manager.runtime_quarantine == {subentry_id}
    assert dict(entry.subentries[subentry_id].data) == archived_runtime["data"]
    assert manager.asset(ASSET_UUID)["ha_device_refs"] == []  # type: ignore[index]
    assert manager.asset_for_primary_device_id(first.id) is None
    assert len(manager.assets()) == 2
    assert list(_owned_conflicts(hass)) == [runtime_conflict_issue_id(subentry_id)]
    # The Purchase no longer lists the archived Asset's device, so the Purchase
    # projection is reconciled; the quarantine itself writes nothing.
    for saved in saves:
        archived = saved["assets"][ASSET_UUID]
        assert archived["archived_at"] == ARCHIVED_AT
        assert archived["runtime"]["total_seconds"] == ARCHIVED_TOTAL
        assert archived["ha_device_refs"] == []
