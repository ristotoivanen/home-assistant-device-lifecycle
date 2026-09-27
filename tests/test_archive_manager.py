"""Archive and Restore through the Store manager, and Runtime eligibility (WP13).

Archive is refused while anything can still create Runtime for the Asset:
a deployed Asset, a resolving Runtime subentry, a Runtime binding
reservation, or a writer whose Runtime is not provably durable. Every check
and the change run in one synchronous mutator under ``_mutation_lock``.
Restore has no Runtime precondition. Both change only ``archived_at``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError

from custom_components.device_lifecycle import storage
from custom_components.device_lifecycle.archive import (
    ArchiveMutationError,
    ArchiveOutcome,
)
from custom_components.device_lifecycle.canonical import parse_canonical_utc
from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_DEVICE_ID,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    SUBENTRY_TYPE_RUNTIME,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    RUNTIME_BINDING_RESERVATIONS,
    STORAGE_KEY,
    AssetStoreError,
    AssetStoreManager,
    AssetStorePersistenceError,
    DeviceLifecycleStore,
    RuntimeArchiveEligibility,
    RuntimeWriterDurability,
)

from .conftest import ASSET_UUID, DEVICE_ID, SOURCE_ENTITY_ID
from .test_runtime import _Clock, _manager, _sensor
from .test_runtime_checkpoint import _add

ARCHIVED_AT = "2026-09-26T12:34:56.123456+00:00"
TOTAL = "1000"
ENTRY_ID = "device-lifecycle-entry-id"


# --- Helpers -----------------------------------------------------------------


def _data(asset_store_data: AssetStoreData, *, archived: bool = False) -> dict:
    data: dict[str, Any] = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    asset["deployment_state"] = DEPLOYMENT_STATE_NOT_DEPLOYED
    asset["runtime"]["total_seconds"] = TOTAL
    if archived:
        asset["archived_at"] = ARCHIVED_AT
    return data


def _runtime_subentry(*, legacy: bool = False) -> SimpleNamespace:
    data = {CONF_DEVICE_ID: DEVICE_ID}
    if not legacy:
        data[CONF_ASSET_UUID] = ASSET_UUID
    return SimpleNamespace(
        subentry_id="runtime", subentry_type=SUBENTRY_TYPE_RUNTIME, data=data
    )


def _entry(manager: AssetStoreManager, *subentries: SimpleNamespace) -> Any:
    return SimpleNamespace(
        entry_id=ENTRY_ID,
        runtime_data=manager,
        subentries={item.subentry_id: item for item in subentries},
    )


class _Writer:
    """A registered Runtime writer reporting fixed durability evidence."""

    def __init__(
        self,
        *,
        observing: bool = False,
        pending: bool = False,
        committed: str = TOTAL,
    ) -> None:
        self.state = RuntimeWriterDurability(
            observing=observing, pending=pending, committed_seconds=Decimal(committed)
        )
        self.calls: list[str] = []

    def durability(self) -> RuntimeWriterDurability:
        self.calls.append("durability")
        return self.state

    async def checkpoint(self) -> None:
        self.calls.append("checkpoint")

    async def prepare_unload(self) -> None:
        self.calls.append("prepare_unload")

    async def finalize(self) -> None:
        self.calls.append("finalize")

    async def retire(self) -> None:
        self.calls.append("retire")

    def register(self, manager: AssetStoreManager) -> None:
        manager.register_runtime_checkpoint(
            ASSET_UUID,
            self.checkpoint,
            prepare_unload=self.prepare_unload,
            durability=self.durability,
            finalize=self.finalize,
            retire=self.retire,
        )


def _only_archived_at_changed(before: dict, after: dict) -> None:
    before, after = deepcopy(before), deepcopy(after)
    before["assets"][ASSET_UUID].pop("archived_at")
    after["assets"][ASSET_UUID].pop("archived_at")
    assert after == before


async def _refused(
    manager: AssetStoreManager, entry: Any, code: str
) -> AssetStoreError:
    before = deepcopy(manager._data)
    with pytest.raises(AssetStoreError) as raised:
        await manager.async_archive_asset(entry, ASSET_UUID)
    assert raised.value.code == code
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()
    return raised.value


# --- Archive --------------------------------------------------------------------


async def test_archive_changes_only_archived_at(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data = _data(asset_store_data)
    manager = _manager(hass, data)
    entry = _entry(manager)

    with patch.object(storage, "utc_now_iso", return_value=ARCHIVED_AT):
        outcome = await manager.async_archive_asset(entry, ASSET_UUID)

    assert outcome is ArchiveOutcome.CHANGED
    assert manager._data["assets"][ASSET_UUID]["archived_at"] == ARCHIVED_AT
    parse_canonical_utc(manager._data["assets"][ASSET_UUID]["archived_at"])
    _only_archived_at_changed(data, manager._data)
    manager._store.async_save.assert_awaited_once()
    assert entry.subentries == {}


async def test_archive_of_an_archived_asset_is_no_op_before_any_precondition(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data = _data(asset_store_data, archived=True)
    manager = _manager(hass, data)
    writer = _Writer(observing=True, pending=True)
    manager._runtime_writers[ASSET_UUID] = storage.RuntimeWriter(
        writer.checkpoint,
        writer.prepare_unload,
        durability=writer.durability,
    )
    manager._runtime_unresolved.add(ASSET_UUID)
    entry = _entry(manager, _runtime_subentry())
    manager.hass.data.setdefault(RUNTIME_BINDING_RESERVATIONS, {})[
        (ENTRY_ID, ASSET_UUID)
    ] = {object()}

    with patch.object(storage, "utc_now_iso") as clock:
        outcome = await manager.async_archive_asset(entry, ASSET_UUID)

    assert outcome is ArchiveOutcome.NO_OP
    clock.assert_not_called()
    assert writer.calls == []
    assert manager._data == data
    manager._store.async_save.assert_not_awaited()
    assert manager.archive_blockers(entry, ASSET_UUID) == ()


async def test_archive_refuses_a_deployed_asset(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data = _data(asset_store_data)
    data["assets"][ASSET_UUID]["deployment_state"] = DEPLOYMENT_STATE_DEPLOYED
    manager = _manager(hass, data)
    await _refused(manager, _entry(manager), "archive_asset_deployed")


@pytest.mark.parametrize("legacy", [False, True], ids=["modern", "legacy"])
async def test_archive_refuses_a_resolving_runtime_subentry(
    hass: HomeAssistant, asset_store_data: AssetStoreData, legacy: bool
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    entry = _entry(manager, _runtime_subentry(legacy=legacy))
    await _refused(manager, entry, "archive_runtime_configured")


async def test_archive_refuses_a_runtime_binding_reservation(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    entry = _entry(manager)
    release = await manager.async_reserve_runtime_binding(entry, ASSET_UUID)
    await _refused(manager, entry, "archive_runtime_binding_in_progress")

    release()
    release()  # idempotent
    assert hass.data[RUNTIME_BINDING_RESERVATIONS] == {}
    assert await manager.async_archive_asset(entry, ASSET_UUID) is (
        ArchiveOutcome.CHANGED
    )


@pytest.mark.parametrize(
    ("writer", "code", "eligibility"),
    [
        (_Writer(observing=True), "archive_runtime_writer_active", "active"),
        (_Writer(pending=True), "archive_runtime_undurable", "undurable"),
        (_Writer(committed="999"), "archive_runtime_undurable", "undurable"),
    ],
    ids=["observing", "pending", "total-mismatch"],
)
async def test_archive_refuses_a_writer_without_durable_runtime(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    writer: _Writer,
    code: str,
    eligibility: str,
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    writer.register(manager)
    entry = _entry(manager)
    assert manager._runtime_archive_eligibility(manager._data, ASSET_UUID) == (
        RuntimeArchiveEligibility(eligibility)
    )
    await _refused(manager, entry, code)
    # Archive only observes the writer; it never drives it.
    assert set(writer.calls) == {"durability"}


async def test_archive_refuses_a_writer_that_gives_no_evidence(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    writer = _Writer()
    manager.register_runtime_checkpoint(
        ASSET_UUID, writer.checkpoint, prepare_unload=writer.prepare_unload
    )
    await _refused(manager, _entry(manager), "archive_runtime_writer_active")


async def test_archive_refuses_a_canonical_total_that_is_unknown(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data = _data(asset_store_data)
    data["assets"][ASSET_UUID]["runtime"]["total_seconds"] = None
    manager = _manager(hass, data)
    _Writer(committed="0").register(manager)
    await _refused(manager, _entry(manager), "archive_runtime_undurable")


async def test_unresolved_runtime_blocks_archive_and_is_never_cleared(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    manager.mark_runtime_unresolved(ASSET_UUID)
    entry = _entry(manager)
    await _refused(manager, entry, "archive_runtime_unresolved")

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_finalize_runtime(ASSET_UUID)
    assert raised.value.code == "runtime_checkpoint_unresolved"
    assert ASSET_UUID in manager._runtime_unresolved
    await _refused(manager, entry, "archive_runtime_unresolved")


async def test_a_quiesced_durable_writer_may_be_archived(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    writer = _Writer()
    writer.register(manager)
    entry = _entry(manager)
    assert manager._runtime_archive_eligibility(manager._data, ASSET_UUID) is (
        RuntimeArchiveEligibility.QUIESCED_DURABLE
    )
    assert manager.archive_blockers(entry, ASSET_UUID) == ()

    assert await manager.async_archive_asset(entry, ASSET_UUID) is (
        ArchiveOutcome.CHANGED
    )
    assert ASSET_UUID in manager._runtime_writers
    # Read synchronously by the direct probe, the preview, and the Archive
    # mutator; never finalized, retired, or checkpointed.
    assert writer.calls == ["durability"] * 3


async def test_archive_of_a_missing_asset_fails(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    missing = "99999999-9999-4999-8999-999999999999"
    with pytest.raises(AssetStoreError) as raised:
        await manager.async_archive_asset(_entry(manager), missing)
    assert raised.value.code == "asset_missing"
    with pytest.raises(AssetStoreError):
        manager.archive_blockers(_entry(manager), missing)


async def test_after_archive_runtime_can_no_longer_be_written(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    await manager.async_archive_asset(_entry(manager), ASSET_UUID)
    writer = _Writer()

    with pytest.raises(AssetStoreError) as registration:
        writer.register(manager)
    assert registration.value.code == "runtime_asset_archived"
    with pytest.raises(AssetStoreError) as commit:
        await manager.async_commit_runtime_delta(
            ASSET_UUID, expected_total=Decimal(TOTAL), delta=Decimal(5)
        )
    assert commit.value.code == "asset_archived"
    assert manager._runtime_writers == {}


# --- Preview --------------------------------------------------------------------


async def test_blocker_preview_lists_everything_and_changes_nothing(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data = _data(asset_store_data)
    data["assets"][ASSET_UUID]["deployment_state"] = DEPLOYMENT_STATE_DEPLOYED
    manager = _manager(hass, data)
    writer = _Writer(observing=True)
    writer.register(manager)
    entry = _entry(manager, _runtime_subentry())
    release = await manager.async_reserve_runtime_binding(entry, ASSET_UUID)
    reservations = deepcopy(hass.data[RUNTIME_BINDING_RESERVATIONS])

    assert manager.archive_blockers(entry, ASSET_UUID) == (
        "archive_asset_deployed",
        "archive_runtime_configured",
        "archive_runtime_binding_in_progress",
        "archive_runtime_writer_active",
    )
    assert manager._data == data
    assert writer.calls == ["durability"]
    assert hass.data[RUNTIME_BINDING_RESERVATIONS].keys() == reservations.keys()
    manager._store.async_save.assert_not_awaited()
    release()

    manager.mark_runtime_unresolved(ASSET_UUID)
    assert "archive_runtime_unresolved" in manager.archive_blockers(entry, ASSET_UUID)


async def test_blocker_preview_is_advisory(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """A blocker can appear after the preview; Archive checks again."""
    manager = _manager(hass, _data(asset_store_data))
    entry = _entry(manager)
    assert manager.archive_blockers(entry, ASSET_UUID) == ()

    release = await manager.async_reserve_runtime_binding(entry, ASSET_UUID)
    await _refused(manager, entry, "archive_runtime_binding_in_progress")
    release()
    assert await manager.async_archive_asset(entry, ASSET_UUID) is (
        ArchiveOutcome.CHANGED
    )


# --- Restore --------------------------------------------------------------------


async def test_restore_changes_only_archived_at_and_needs_no_runtime(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data = _data(asset_store_data, archived=True)
    manager = _manager(hass, data)
    manager.mark_runtime_unresolved(ASSET_UUID)

    outcome = await manager.async_restore_asset(ASSET_UUID)

    assert outcome is ArchiveOutcome.CHANGED
    assert manager._data["assets"][ASSET_UUID]["archived_at"] is None
    _only_archived_at_changed(data, manager._data)
    assert manager._runtime_writers == {}
    assert ASSET_UUID in manager._runtime_unresolved
    manager._store.async_save.assert_awaited_once()


async def test_restore_of_an_active_asset_is_no_op(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data = _data(asset_store_data)
    manager = _manager(hass, data)
    assert await manager.async_restore_asset(ASSET_UUID) is ArchiveOutcome.NO_OP
    assert manager._data == data
    manager._store.async_save.assert_not_awaited()


# --- Persistence ------------------------------------------------------------------


def _envelope(data: Any) -> dict[str, Any]:
    return {"version": 4, "minor_version": 1, "key": STORAGE_KEY, "data": data}


@contextmanager
def _readback(hass_storage: dict[str, Any]) -> Iterator[None]:
    with patch.object(
        storage.json_util,
        "load_json",
        side_effect=lambda _path: deepcopy(hass_storage[STORAGE_KEY]),
    ):
        yield


async def _persisted(
    hass: HomeAssistant, hass_storage: dict[str, Any], data: dict
) -> AssetStoreManager:
    hass_storage[STORAGE_KEY] = _envelope(deepcopy(data))
    manager = AssetStoreManager(hass)
    with _readback(hass_storage):
        await manager.async_setup()
    return manager


def _operations() -> list[tuple[str, bool]]:
    return [("archive", False), ("restore", True)]


async def _run(manager: AssetStoreManager, operation: str) -> ArchiveOutcome:
    if operation == "archive":
        return await manager.async_archive_asset(_entry(manager), ASSET_UUID)
    return await manager.async_restore_asset(ASSET_UUID)


@pytest.mark.parametrize(("operation", "archived"), _operations())
async def test_success_is_published_only_after_verified_save(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
    operation: str,
    archived: bool,
) -> None:
    manager = await _persisted(
        hass, hass_storage, _data(asset_store_data, archived=archived)
    )
    with _readback(hass_storage):
        assert await _run(manager, operation) is ArchiveOutcome.CHANGED
    assert manager._data == hass_storage[STORAGE_KEY]["data"]
    stored = hass_storage[STORAGE_KEY]["data"]["assets"][ASSET_UUID]["archived_at"]
    assert (stored is None) is archived


@pytest.mark.parametrize(("operation", "archived"), _operations())
async def test_definite_save_failure_publishes_nothing(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
    operation: str,
    archived: bool,
) -> None:
    manager = await _persisted(
        hass, hass_storage, _data(asset_store_data, archived=archived)
    )
    before = deepcopy(manager._data)
    with (
        patch.object(
            DeviceLifecycleStore,
            "async_save",
            side_effect=AssetStorePersistenceError("failed"),
        ),
        pytest.raises(AssetStorePersistenceError),
    ):
        await _run(manager, operation)
    assert manager._data == before
    assert manager._persistence_uncertain is False


@pytest.mark.parametrize(("operation", "archived"), _operations())
async def test_ambiguous_write_that_landed_is_success(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
    operation: str,
    archived: bool,
) -> None:
    manager = await _persisted(
        hass, hass_storage, _data(asset_store_data, archived=archived)
    )
    reads: list[int] = []

    def _load(_path: str) -> Any:
        reads.append(1)
        if len(reads) == 1:
            raise HomeAssistantError("readback failed")
        return deepcopy(hass_storage[STORAGE_KEY])

    with patch.object(storage.json_util, "load_json", side_effect=_load):
        assert await _run(manager, operation) is ArchiveOutcome.CHANGED
    assert manager._persistence_uncertain is False
    assert manager._data == hass_storage[STORAGE_KEY]["data"]
    stored = manager._data["assets"][ASSET_UUID]["archived_at"]
    assert (stored is None) is archived


@pytest.mark.parametrize(("operation", "archived"), _operations())
async def test_ambiguous_write_that_did_not_land_is_a_definite_failure(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
    operation: str,
    archived: bool,
) -> None:
    data = _data(asset_store_data, archived=archived)
    manager = await _persisted(hass, hass_storage, data)
    with (
        _readback(hass_storage),
        patch.object(
            DeviceLifecycleStore,
            "async_save",
            side_effect=AssetStorePersistenceError("unknown", ambiguous=True),
        ),
        pytest.raises(AssetStorePersistenceError) as raised,
    ):
        await _run(manager, operation)
    assert raised.value.ambiguous is False
    assert manager._persistence_uncertain is False
    assert manager._data == data


async def test_ambiguous_archive_with_an_unreadable_store_stays_uncertain(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    manager = await _persisted(hass, hass_storage, _data(asset_store_data))
    before = deepcopy(manager._data)
    original = AssetStorePersistenceError("unknown", ambiguous=True)
    with (
        patch.object(DeviceLifecycleStore, "async_save", side_effect=original),
        patch.object(
            storage.json_util, "load_json", side_effect=HomeAssistantError("x")
        ),
        pytest.raises(AssetStorePersistenceError) as raised,
    ):
        await manager.async_archive_asset(_entry(manager), ASSET_UUID)
    assert raised.value is original
    assert manager._persistence_uncertain is True
    assert manager._data == before


# --- Runtime binding reservation ---------------------------------------------------


async def test_reservation_is_refused_for_an_archived_missing_or_stale_target(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    archived = _manager(hass, _data(asset_store_data, archived=True))
    with pytest.raises(AssetStoreError) as raised:
        await archived.async_reserve_runtime_binding(_entry(archived), ASSET_UUID)
    assert raised.value.code == "runtime_asset_archived"

    manager = _manager(hass, _data(asset_store_data))
    with pytest.raises(AssetStoreError) as missing:
        await manager.async_reserve_runtime_binding(
            _entry(manager), "99999999-9999-4999-8999-999999999999"
        )
    assert missing.value.code == "asset_missing"

    stale = _entry(manager)
    stale.runtime_data = _manager(hass, _data(asset_store_data))
    with pytest.raises(AssetStoreError) as not_loaded:
        await manager.async_reserve_runtime_binding(stale, ASSET_UUID)
    assert not_loaded.value.code == "entry_not_loaded"
    assert hass.data.get(RUNTIME_BINDING_RESERVATIONS, {}) == {}


async def test_reservation_recovers_uncertain_persistence_first(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
) -> None:
    """The persisted Store, recovered first, shows the Asset archived."""
    manager = await _persisted(hass, hass_storage, _data(asset_store_data))
    hass_storage[STORAGE_KEY] = _envelope(_data(asset_store_data, archived=True))
    manager._persistence_uncertain = True
    with _readback(hass_storage), pytest.raises(AssetStoreError) as raised:
        await manager.async_reserve_runtime_binding(_entry(manager), ASSET_UUID)
    assert raised.value.code == "runtime_asset_archived"
    assert manager._persistence_uncertain is False


async def test_two_reservations_are_released_independently(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    entry = _entry(manager)
    first = await manager.async_reserve_runtime_binding(entry, ASSET_UUID)
    second = await manager.async_reserve_runtime_binding(entry, ASSET_UUID)
    first()
    assert "archive_runtime_binding_in_progress" in manager.archive_blockers(
        entry, ASSET_UUID
    )
    second()
    assert manager.archive_blockers(entry, ASSET_UUID) == ()


# --- Runtime writer lifecycle --------------------------------------------------------


async def _writer(
    hass: HomeAssistant, asset_store_data: AssetStoreData, clock: _Clock
) -> tuple[AssetStoreManager, Any]:
    manager = _manager(hass, _data(asset_store_data))
    sensor = _sensor(hass, manager, clock)
    await _add(hass, sensor)
    return manager, sensor


async def test_writer_durability_reports_in_memory_evidence_only(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    clock = _Clock(0)
    manager, sensor = await _writer(hass, asset_store_data, clock)
    assert sensor.runtime_durability() == RuntimeWriterDurability(
        observing=True, pending=False, committed_seconds=Decimal(TOTAL)
    )
    sensor._active_since = 0.0
    clock.value = 10
    manager._store.async_save = AsyncMock(side_effect=OSError("disk"))
    await sensor._handle_periodic_checkpoint(None)
    assert sensor.runtime_durability() == RuntimeWriterDurability(
        observing=True, pending=True, committed_seconds=Decimal(TOTAL)
    )
    manager._store.async_save.reset_mock()
    sensor.runtime_durability()
    manager._store.async_save.assert_not_awaited()


async def test_finalize_commits_pending_only_and_keeps_it_on_failure(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    clock = _Clock(0)
    manager, sensor = await _writer(hass, asset_store_data, clock)
    sensor._active_since = 0.0
    clock.value = 10
    manager._store.async_save = AsyncMock(side_effect=OSError("disk"))
    await sensor._handle_periodic_checkpoint(None)  # 10 s pending, still active

    clock.value = 40
    with pytest.raises(AssetStoreError):
        await manager.async_finalize_runtime(ASSET_UUID)
    assert [item.delta for item in sensor._pending] == [Decimal("10.0")]

    manager._store.async_save = AsyncMock()
    assert await manager.async_finalize_runtime(ASSET_UUID) == Decimal(1010)
    assert sensor._pending == []
    # Finalize sealed nothing: the active interval continues from t=10.
    assert sensor._active_since == 10
    assert sensor.runtime_durability().observing is True


async def test_finalize_without_a_writer_returns_the_canonical_total(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    assert await manager.async_finalize_runtime(ASSET_UUID) == Decimal(TOTAL)
    writer = _Writer()
    manager.register_runtime_checkpoint(
        ASSET_UUID, writer.checkpoint, prepare_unload=writer.prepare_unload
    )
    with pytest.raises(AssetStoreError):
        await manager.async_finalize_runtime(ASSET_UUID)


async def test_retire_quiesces_even_when_its_flush_fails(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    clock = _Clock(0)
    manager, sensor = await _writer(hass, asset_store_data, clock)
    sensor._active_since = 0.0
    clock.value = 10
    manager._store.async_save = AsyncMock(side_effect=OSError("disk"))

    with pytest.raises(AssetStoreError):
        await sensor.async_retire_runtime()

    assert sensor.runtime_durability() == RuntimeWriterDurability(
        observing=False, pending=True, committed_seconds=Decimal(TOTAL)
    )
    assert sensor._unsub_source is None
    assert sensor._active_since is None
    # A later source transition adds nothing.
    hass.states.async_set(SOURCE_ENTITY_ID, "on")
    await sensor._handle_source_change(
        SimpleNamespace(
            data={
                "old_state": State(SOURCE_ENTITY_ID, "off"),
                "new_state": State(SOURCE_ENTITY_ID, "on"),
            }
        )
    )
    assert sensor._active_since is None
    assert [item.delta for item in sensor._pending] == [Decimal("10.0")]


async def test_periodic_interval_retries_a_quiesced_writer_without_sealing(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    clock = _Clock(0)
    manager, sensor = await _writer(hass, asset_store_data, clock)
    sensor._active_since = 0.0
    clock.value = 10
    manager._store.async_save = AsyncMock(side_effect=OSError("disk"))
    with pytest.raises(AssetStoreError):
        await sensor.async_retire_runtime()

    clock.value = 500
    await sensor._handle_periodic_checkpoint(None)  # still failing: kept
    assert [item.delta for item in sensor._pending] == [Decimal("10.0")]

    manager._store.async_save = AsyncMock()
    await sensor._handle_periodic_checkpoint(None)
    assert sensor._pending == []
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
    assert manager._runtime_archive_eligibility(manager._data, ASSET_UUID) is (
        RuntimeArchiveEligibility.QUIESCED_DURABLE
    )


async def test_unload_gate_flushes_a_quiesced_writer_strictly(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    clock = _Clock(0)
    manager, sensor = await _writer(hass, asset_store_data, clock)
    sensor._active_since = 0.0
    clock.value = 10
    manager._store.async_save = AsyncMock(side_effect=OSError("disk"))
    with pytest.raises(AssetStoreError):
        await sensor.async_retire_runtime()

    with pytest.raises(AssetStoreError) as refused:
        await manager.async_prepare_runtime_unload()
    assert refused.value.code == "runtime_unload_checkpoint_failed"
    assert ASSET_UUID in manager._runtime_writers
    assert sensor.runtime_durability().pending is True
    assert sensor.runtime_durability().observing is False

    manager._store.async_save = AsyncMock()
    await manager.async_prepare_runtime_unload()
    assert sensor._pending == []
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1010)


async def test_active_writer_unload_behaves_as_before(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    clock = _Clock(0)
    manager, sensor = await _writer(hass, asset_store_data, clock)
    sensor._active_since = 0.0
    clock.value = 30
    await manager.async_prepare_runtime_unload()
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1030)
    assert sensor.runtime_durability().observing is False
    await manager.async_prepare_runtime_unload()  # again: nothing pending
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1030)


async def test_orphaned_writers_are_retired_and_failures_do_not_raise(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    clock = _Clock(0)
    manager, sensor = await _writer(hass, asset_store_data, clock)
    with_subentry = _entry(manager, _runtime_subentry())
    await manager.async_retire_orphaned_runtime_writers(with_subentry)
    assert sensor.runtime_durability().observing is True

    sensor._active_since = 0.0
    clock.value = 10
    manager._store.async_save = AsyncMock(side_effect=OSError("disk"))
    await manager.async_retire_orphaned_runtime_writers(_entry(manager))
    assert sensor.runtime_durability().observing is False
    assert sensor.runtime_durability().pending is True
    assert ASSET_UUID in manager._runtime_writers


async def test_a_writer_without_retire_is_left_in_place(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    writer = _Writer()
    manager.register_runtime_checkpoint(
        ASSET_UUID, writer.checkpoint, prepare_unload=writer.prepare_unload
    )
    await manager.async_retire_orphaned_runtime_writers(_entry(manager))
    assert writer.calls == []
    assert ASSET_UUID in manager._runtime_writers


def test_store_stays_4_1_and_quarantine_is_kept() -> None:
    assert (storage.STORAGE_VERSION, storage.STORAGE_MINOR_VERSION) == (4, 1)
    assert hasattr(AssetStoreManager, "apply_runtime_quarantine")


async def test_archive_mutation_errors_keep_their_stable_code(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    with (
        patch.object(
            storage,
            "apply_archive_request",
            side_effect=ArchiveMutationError(
                "bad time", code="archive_observed_utc_invalid"
            ),
        ),
        pytest.raises(AssetStoreError) as raised,
    ):
        await manager.async_archive_asset(_entry(manager), ASSET_UUID)
    assert raised.value.code == "archive_observed_utc_invalid"
    manager._store.async_save.assert_not_awaited()


async def test_retiring_again_seals_nothing_new(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    clock = _Clock(0)
    manager, sensor = await _writer(hass, asset_store_data, clock)
    sensor._active_since = 0.0
    clock.value = 10
    await sensor.async_retire_runtime()
    clock.value = 90
    await sensor.async_retire_runtime()
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(1010)
    assert sensor.runtime_durability().observing is False


async def test_a_writer_unregistered_during_retirement_is_skipped(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, _data(asset_store_data))
    second_uuid = "44444444-4444-4444-8444-444444444444"
    retired: list[str] = []

    async def _noop() -> None:
        return None

    async def _retire_first() -> None:
        retired.append("first")
        manager._runtime_writers.pop(second_uuid)

    async def _retire_second() -> None:
        retired.append("second")

    manager._runtime_writers[ASSET_UUID] = storage.RuntimeWriter(
        _noop, _noop, retire=_retire_first
    )
    manager._runtime_writers[second_uuid] = storage.RuntimeWriter(
        _noop, _noop, retire=_retire_second
    )
    await manager.async_retire_orphaned_runtime_writers(_entry(manager))
    assert retired == ["first"]
