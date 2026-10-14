"""BL-03: recover durable Store state before any subsequent mutation."""

from __future__ import annotations

import asyncio
import json
import threading
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util.file import WriteError

from custom_components.device_lifecycle.archive import ArchiveOutcome
from custom_components.device_lifecycle.maintenance_mutations import (
    CalendarIntervalInput,
    CreateScheduleRequest,
    MutationOutcome,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    AssetStoreError,
    AssetStoreManager,
    AssetStorePersistenceError,
    _validate_store_data,
    json_util,
)

from .conftest import ASSET_UUID
from .test_quick_add_storage import QUICK_UUID, _request

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
    assert final["data"]["assets"][ASSET_UUID]["runtime"] == {"total_seconds": "2000"}
    assert final["data"] == expected
    assert manager._data == expected
    assert reads == 3  # failed verification, recovery, successful verification
    assert manager._persistence_uncertain is False


def _put_newer_snapshot(path: str) -> dict[str, Any]:
    """Model a different, fully valid authoritative snapshot in the real file."""
    persisted = json.loads(Path(path).read_text())
    persisted["data"]["assets"][ASSET_UUID]["runtime"]["total_seconds"] = "2000"
    _validate_store_data(persisted["data"])
    Path(path).write_text(json.dumps(persisted))
    return persisted


async def _mutate(manager: AssetStoreManager, operation: str) -> Any:
    """Exercise each production mutation transport without mocking its save."""
    if operation == "quick":
        return await manager.async_quick_create_asset(_request())
    if operation == "maintenance":
        return await manager.async_mutate_maintenance(
            CreateScheduleRequest(
                schedule_uuid="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1",
                asset_uuid=ASSET_UUID,
                name="Persistence fixture",
                calendar_interval=CalendarIntervalInput(6, "months"),
            )
        )
    if operation == "archive":
        return await manager.async_archive_asset(
            SimpleNamespace(entry_id="safety-parent", subentries={}), ASSET_UUID
        )
    if operation == "runtime":
        return await manager.async_commit_runtime_delta(
            ASSET_UUID, expected_total=Decimal(1000), delta=Decimal(10)
        )
    return await manager.async_update_asset_metadata(ASSET_UUID, name="First edit")


@pytest.mark.parametrize(
    "operation", ["metadata", "quick", "maintenance", "archive", "runtime"]
)
@pytest.mark.parametrize(
    "fault",
    [
        "before-write",
        "ha-write-error",
        "after-write",
        "readback",
        "different-snapshot",
        "unexpected-save-error",
        "unexpected-readback-error",
    ],
)
async def test_save_faults_never_allow_stale_followup_writes(
    persisted_manager: AssetStoreManager,
    operation: str,
    fault: str,
) -> None:
    """Both save transports must reconcile before editing a newer durable state."""
    manager = persisted_manager
    path = manager._store.path
    original_write = manager._store._async_write_data
    original_read = json_util.load_json
    before = deepcopy(manager._data)
    writes = 0
    reads = 0

    async def _write(data: dict[str, Any]) -> None:
        nonlocal writes
        writes += 1
        if writes == 1 and fault == "before-write":
            raise OSError("injected failure before write")
        if writes == 1 and fault == "ha-write-error":
            raise WriteError("injected failure swallowed by Home Assistant Store")
        await original_write(data)
        if writes == 1 and fault in ("after-write", "unexpected-save-error"):
            _put_newer_snapshot(path)
            if fault == "unexpected-save-error":
                raise RuntimeError("injected lost acknowledgement")
            raise OSError("injected failure after write")

    def _read(path: str) -> Any:
        nonlocal reads
        reads += 1
        if reads == 1 and fault in ("readback", "unexpected-readback-error"):
            _put_newer_snapshot(path)
            if fault == "unexpected-readback-error":
                raise RuntimeError("injected unexpected readback failure")
            raise HomeAssistantError("injected unreadable acknowledgement")
        if reads == 1 and fault == "different-snapshot":
            _put_newer_snapshot(path)
        return original_read(path)

    resolved = operation in {"quick", "maintenance", "archive"}
    did_not_write = fault in {"before-write", "ha-write-error"}
    with (
        patch.object(manager._store, "_async_write_data", side_effect=_write),
        patch.object(json_util, "load_json", side_effect=_read),
    ):
        if resolved and not did_not_write:
            result = await _mutate(manager, operation)
            assert manager._persistence_uncertain is False
        else:
            with pytest.raises(AssetStorePersistenceError):
                await _mutate(manager, operation)
            assert manager._persistence_uncertain is (not resolved)
        durable = original_read(path)
        expected_total = "1000" if did_not_write else "2000"
        assert (
            durable["data"]["assets"][ASSET_UUID]["runtime"]["total_seconds"]
            == expected_total
        )
        assert manager._data == (durable["data"] if resolved else before)
        if resolved and not did_not_write:
            if operation == "quick":
                assert result.replayed is True
                assert result.asset == durable["data"]["assets"][QUICK_UUID]
            elif operation == "maintenance":
                assert result.outcome is MutationOutcome.REPLAY
            else:
                assert result is ArchiveOutcome.CHANGED

        expected = deepcopy(durable)
        if durable["data"]["assets"][ASSET_UUID]["archived_at"] is not None:
            # Archived metadata edits are intentionally refused. Restore is
            # the next permitted mutation and must preserve every other field.
            assert (
                await manager.async_restore_asset(ASSET_UUID) is ArchiveOutcome.CHANGED
            )
            expected["data"]["assets"][ASSET_UUID]["archived_at"] = None
        else:
            await manager.async_update_asset_metadata(
                ASSET_UUID, category="Safe followup"
            )
            expected["data"]["assets"][ASSET_UUID]["category"] = "Safe followup"
            expected["data"]["assets"][ASSET_UUID]["field_sources"]["category"] = "user"

    assert original_read(path) == expected
    assert manager._data == expected["data"]
    assert writes == 2  # one failed/ambiguous attempt, one explicit followup
    assert manager._persistence_uncertain is False


@pytest.mark.parametrize("operation", ["metadata", "quick"])
@pytest.mark.parametrize(
    "fault",
    ["unreadable", "corrupt-json", "wrong-version", "invalid-payload", "missing"],
)
async def test_failed_recovery_blocks_every_retry_until_valid_state_returns(
    persisted_manager: AssetStoreManager,
    operation: str,
    fault: str,
) -> None:
    """Recovery retries cannot save, allocate IDs, or publish unvalidated data."""
    manager = persisted_manager
    path = manager._store.path
    before = deepcopy(manager._data)
    original_read = json_util.load_json
    durable: dict[str, Any] | None = None
    calls = 0

    def _read(path: str) -> Any:
        nonlocal calls, durable
        calls += 1
        if calls == 1:
            durable = _put_newer_snapshot(path)
        if fault == "unreadable":
            raise HomeAssistantError("injected recovery read failure")
        if fault == "corrupt-json":
            Path(path).write_text('{"data":broken')
            return original_read(path)
        if fault == "missing":
            Path(path).unlink(missing_ok=True)
            return {}
        invalid = deepcopy(durable)
        if fault == "wrong-version":
            invalid["version"] = 99
        else:
            invalid["data"]["next_asset_number"] = 1
        Path(path).write_text(json.dumps(invalid))
        return invalid

    with (
        patch.object(json_util, "load_json", side_effect=_read),
        patch.object(
            manager._store, "async_save", wraps=manager._store.async_save
        ) as save,
    ):
        with pytest.raises(AssetStoreError):
            await _mutate(manager, operation)
        assert manager._persistence_uncertain is True
        broken_bytes = Path(path).read_bytes() if Path(path).exists() else None
        for _attempt in range(2):
            with pytest.raises(AssetStoreError):
                await _mutate(manager, operation)
            assert manager._persistence_uncertain is True
            assert manager._data == before
            assert save.await_count == 1
            assert (
                Path(path).read_bytes() if Path(path).exists() else None
            ) == broken_bytes

    assert durable is not None
    Path(path).write_text(json.dumps(durable))
    await manager.async_update_asset_metadata(ASSET_UUID, category="Recovered followup")
    final = original_read(path)
    expected = deepcopy(durable)
    expected["data"]["assets"][ASSET_UUID]["category"] = "Recovered followup"
    expected["data"]["assets"][ASSET_UUID]["field_sources"]["category"] = "user"
    assert final == expected
    assert manager._data == expected["data"]
    assert manager._persistence_uncertain is False


async def test_runtime_cas_rejects_stale_delta_after_mismatch(
    persisted_manager: AssetStoreManager,
) -> None:
    """An old writer cannot replace a recovered total or invent a duplicate delta."""
    manager = persisted_manager
    path = manager._store.path
    original_read = json_util.load_json
    first = True

    def _read(path: str) -> Any:
        nonlocal first
        if first:
            first = False
            _put_newer_snapshot(path)
        return original_read(path)

    with patch.object(json_util, "load_json", side_effect=_read):
        with pytest.raises(AssetStorePersistenceError):
            await _mutate(manager, "runtime")
        durable = Path(path).read_bytes()
        with pytest.raises(AssetStoreError):
            await _mutate(manager, "runtime")
    assert Path(path).read_bytes() == durable
    assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(2000)


@pytest.mark.parametrize(
    "stopping", [False, True], ids=["ordinary", "deferred-final-write"]
)
async def test_quick_create_recovery_replays_without_new_ids_or_extra_writes(
    persisted_manager: AssetStoreManager,
    stopping: bool,
) -> None:
    """A landed Quick Create survives mismatch and same-UUID retry exactly once."""
    manager = persisted_manager
    original_read = json_util.load_json
    first = True

    def _read(path: str) -> Any:
        nonlocal first
        if first:
            first = False
            _put_newer_snapshot(path)
        return original_read(path)

    previous_state = manager.hass.state
    if stopping:
        manager.hass.set_state(CoreState.stopping)
    try:
        with (
            patch.object(json_util, "load_json", side_effect=_read),
            patch.object(
                manager._store, "async_save", wraps=manager._store.async_save
            ) as save,
        ):
            result = await manager.async_quick_create_asset(_request())
            before_retry = Path(manager._store.path).read_bytes()
            retry = await manager.async_quick_create_asset(_request())
        assert result.replayed is True and retry.replayed is True
        assert result.asset["asset_uuid"] == retry.asset["asset_uuid"] == QUICK_UUID
        assert result.asset["asset_id"] == retry.asset["asset_id"] == "DL0008"
        assert manager._data["next_asset_number"] == 9
        assert manager.runtime_total_seconds(ASSET_UUID) == Decimal(2000)
        assert save.await_count == 1
        assert Path(manager._store.path).read_bytes() == before_retry
    finally:
        manager.hass.set_state(previous_state)


@pytest.mark.parametrize("operation", ["metadata", "quick"])
@pytest.mark.parametrize("cancel_count", [1, 2], ids=["cancel-once", "cancel-twice"])
@pytest.mark.parametrize(
    "write_error", [False, True], ids=["write-acknowledged", "write-error"]
)
async def test_cancelled_save_cannot_release_lock_with_a_write_still_in_flight(
    persisted_manager: AssetStoreManager,
    operation: str,
    cancel_count: int,
    write_error: bool,
) -> None:
    """Cancellation must not let a late executor write replace a later mutation."""
    from homeassistant.helpers import storage as ha_storage

    manager = persisted_manager
    before = deepcopy(manager._data)
    before_bytes = Path(manager._store.path).read_bytes()
    started = asyncio.Event()
    release = threading.Event()
    original_write = ha_storage.write_utf8_file_atomic
    first_write = True
    requested: dict[str, Any] | None = None
    writes = 0

    def _blocking_write(*args: Any, **kwargs: Any) -> None:
        nonlocal first_write, requested, writes
        writes += 1
        blocked = first_write
        if blocked:
            first_write = False
            requested = json.loads(args[1])
            manager.hass.loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=10), "test did not release the write"
        original_write(*args, **kwargs)
        if blocked and write_error:
            raise OSError("injected failure after cancelled atomic write")

    with patch.object(
        ha_storage, "write_utf8_file_atomic", side_effect=_blocking_write
    ):
        task = asyncio.create_task(_mutate(manager, operation))
        followup: asyncio.Task[Any] | None = None
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            for _cancel in range(cancel_count):
                task.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert not task.done()
                assert manager._mutation_lock.locked()
                assert manager._persistence_uncertain is True
                assert manager._data == before
                assert Path(manager._store.path).read_bytes() == before_bytes
            followup = asyncio.create_task(
                manager.async_update_asset_metadata(
                    ASSET_UUID, category="After cancellation"
                )
            )
            await asyncio.sleep(0)
            assert not followup.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            await followup
        finally:
            release.set()
            if not task.done():
                with pytest.raises(asyncio.CancelledError):
                    await task
            if followup is not None and not followup.done():
                await followup
            await manager.hass.async_block_till_done()
    assert manager._persistence_uncertain is False
    assert requested is not None
    expected = deepcopy(requested)
    expected["data"]["assets"][ASSET_UUID]["category"] = "After cancellation"
    expected["data"]["assets"][ASSET_UUID]["field_sources"]["category"] = "user"
    assert json_util.load_json(manager._store.path) == expected
    assert manager._data == expected["data"]
    assert writes == 2
    final = expected["data"]
    assert final["assets"][ASSET_UUID]["category"] == "After cancellation"
    if operation == "quick":
        assert final["assets"][QUICK_UUID]["asset_id"] == "DL0008"
        assert final["next_asset_number"] == 9
    else:
        assert final["assets"][ASSET_UUID]["name"] == "First edit"


@pytest.mark.parametrize("operation", ["metadata", "quick"])
async def test_known_nonambiguous_failure_does_not_force_recovery(
    persisted_manager: AssetStoreManager,
    operation: str,
) -> None:
    """An explicitly proven non-commit remains distinct from an unknown result."""
    manager = persisted_manager
    before = Path(manager._store.path).read_bytes()
    with (
        patch.object(
            manager._store,
            "async_save",
            new=AsyncMock(
                side_effect=AssetStorePersistenceError("known not committed")
            ),
        ),
        patch.object(
            manager._store, "async_load_persisted_snapshot", new=AsyncMock()
        ) as recover,
        pytest.raises(AssetStorePersistenceError),
    ):
        await _mutate(manager, operation)
    recover.assert_not_awaited()
    assert Path(manager._store.path).read_bytes() == before
    assert manager._persistence_uncertain is False
