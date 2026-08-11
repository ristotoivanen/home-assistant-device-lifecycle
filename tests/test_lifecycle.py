"""Canonical lifecycle state, history validation, and mutation tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    AssetStoreError,
    AssetStoreManager,
    AssetStorePersistenceError,
    _validate_store_data,
)

from .conftest import ASSET_UUID


def _manager(
    hass: HomeAssistant,
    data: AssetStoreData | None = None,
) -> AssetStoreManager:
    manager = AssetStoreManager(hass)
    if data is not None:
        manager._data = deepcopy(data)
    manager._store.async_save = AsyncMock()
    return manager


async def test_new_asset_starts_active_with_one_initial_event(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)

    asset = await manager.async_create_manual_asset(name="New Asset")
    events = manager.lifecycle_events_for_asset(asset["asset_uuid"])

    assert asset["lifecycle"] == {
        "status": "active",
        "current_event_uuid": events[0]["event_uuid"],
    }
    assert len(events) == 1
    assert events[0]["from_status"] == "unknown"
    assert events[0]["to_status"] == "active"
    assert events[0]["previous_event_uuid"] is None
    assert events[0]["effective_date"] is None


async def test_explicit_unknown_new_asset_has_no_fake_event(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)

    asset = await manager.async_create_manual_asset(
        name="Unknown Asset",
        initial_lifecycle_status="unknown",
    )

    assert asset["lifecycle"] == {
        "status": "unknown",
        "current_event_uuid": None,
    }
    assert manager.lifecycle_events_for_asset(asset["asset_uuid"]) == []


def test_migrated_asset_is_unknown_without_event(asset_store_data) -> None:
    asset = asset_store_data["assets"][ASSET_UUID]
    assert asset["lifecycle"] == {
        "status": "unknown",
        "current_event_uuid": None,
    }
    assert asset_store_data["lifecycle_events"] == {}
    _validate_store_data(asset_store_data)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("retired", "active"),
        ("disposed", "active"),
        ("lost", "active"),
    ],
)
async def test_lifecycle_transitions_are_explicit_and_reversible(
    hass: HomeAssistant,
    first: str,
    second: str,
) -> None:
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Transition Asset")
    manager._store.async_save.reset_mock()
    today = dt_util.now().date().isoformat()

    changed = await manager.async_set_asset_lifecycle(
        asset["asset_uuid"],
        first,
        effective_date=today,
        notes="First transition",
    )
    corrected = await manager.async_set_asset_lifecycle(
        asset["asset_uuid"],
        second,
        effective_date=today,
        notes="Correction",
    )
    events = manager.lifecycle_events_for_asset(asset["asset_uuid"])

    assert changed["lifecycle"]["status"] == first
    assert corrected["lifecycle"]["status"] == second
    assert [event["to_status"] for event in events] == ["active", first, second]
    assert events[-1]["previous_event_uuid"] == events[-2]["event_uuid"]
    assert manager._store.async_save.await_count == 2


async def test_migrated_unknown_can_transition_to_active(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    manager = _manager(hass, asset_store_data)

    asset = await manager.async_set_asset_lifecycle(
        ASSET_UUID,
        "active",
        effective_date=None,
        notes=None,
    )

    events = manager.lifecycle_events_for_asset(ASSET_UUID)
    assert asset["lifecycle"]["status"] == "active"
    assert len(events) == 1
    assert events[0]["from_status"] == "unknown"


async def test_same_state_transition_is_a_store_noop(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="No-op Asset")
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    returned = await manager.async_set_asset_lifecycle(
        asset["asset_uuid"],
        "active",
        effective_date=None,
        notes="Ignored because no state change",
    )

    assert returned == asset
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()


@pytest.mark.parametrize(
    "effective_date",
    [123, "not-a-date", "20260810", "2099-01-01"],
)
async def test_same_state_manager_noop_ignores_transition_only_fields(
    hass: HomeAssistant,
    effective_date: object,
) -> None:
    """Malformed/future transition fields are irrelevant when no event is created."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Strict no-op Asset")
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with patch("custom_components.device_lifecycle.storage.uuid.uuid4") as event_uuid:
        returned = await manager.async_set_asset_lifecycle(
            asset["asset_uuid"],
            "active",
            effective_date=effective_date,  # type: ignore[arg-type]
            notes=object(),  # type: ignore[arg-type]
        )

    assert returned == asset
    assert returned is not manager._data["assets"][asset["asset_uuid"]]
    assert manager._data == before
    assert len(manager.lifecycle_events_for_asset(asset["asset_uuid"])) == 1
    event_uuid.assert_not_called()
    manager._store.async_save.assert_not_awaited()


@pytest.mark.parametrize("status", ["stored", "replaced", "returned", ""])
async def test_invalid_lifecycle_status_is_structured_and_not_saved(
    hass: HomeAssistant,
    status: str,
) -> None:
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Invalid status")
    manager._store.async_save.reset_mock()

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_set_asset_lifecycle(
            asset["asset_uuid"],
            status,
            effective_date=None,
            notes=None,
        )

    assert raised.value.code == "invalid_lifecycle_status"
    manager._store.async_save.assert_not_awaited()


async def test_lifecycle_dates_accept_past_today_and_null_but_reject_future(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Dated Asset")
    today = dt_util.now().date()

    await manager.async_set_asset_lifecycle(
        asset["asset_uuid"],
        "retired",
        effective_date=(today - timedelta(days=1)).isoformat(),
        notes=None,
    )
    await manager.async_set_asset_lifecycle(
        asset["asset_uuid"],
        "active",
        effective_date=today.isoformat(),
        notes=None,
    )
    await manager.async_set_asset_lifecycle(
        asset["asset_uuid"],
        "lost",
        effective_date=None,
        notes=None,
    )
    before = deepcopy(manager._data)

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_set_asset_lifecycle(
            asset["asset_uuid"],
            "active",
            effective_date=(today + timedelta(days=1)).isoformat(),
            notes=None,
        )

    assert raised.value.code == "lifecycle_date_in_future"
    assert manager._data == before


@pytest.mark.parametrize("effective_date", [123, "not-a-date", "20260810"])
async def test_malformed_lifecycle_effective_date_has_distinct_code(
    hass: HomeAssistant,
    effective_date: object,
) -> None:
    """Only valid canonical dates can be attached to real lifecycle transitions."""
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Malformed lifecycle date")
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_set_asset_lifecycle(
            asset["asset_uuid"],
            "retired",
            effective_date=effective_date,  # type: ignore[arg-type]
            notes=None,
        )

    assert raised.value.code == "invalid_lifecycle_effective_date"
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()


async def test_lifecycle_date_cannot_move_back_between_known_events(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Ordered dates")
    today = dt_util.now().date()
    await manager.async_set_asset_lifecycle(
        asset["asset_uuid"],
        "retired",
        effective_date=today.isoformat(),
        notes=None,
    )
    before = deepcopy(manager._data)

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_set_asset_lifecycle(
            asset["asset_uuid"],
            "active",
            effective_date=(today - timedelta(days=1)).isoformat(),
            notes=None,
        )

    assert raised.value.code == "lifecycle_chain_invalid"
    assert manager._data == before


async def test_missing_and_invalid_asset_uuid_are_structured(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)

    for asset_uuid in ("not-a-uuid", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"):
        with pytest.raises(AssetStoreError) as raised:
            await manager.async_set_asset_lifecycle(
                asset_uuid,
                "active",
                effective_date=None,
                notes=None,
            )
        assert raised.value.code == "asset_missing"


async def test_lifecycle_save_failure_keeps_published_snapshot(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Save failure")
    before = deepcopy(manager._data)
    manager._store.async_save = AsyncMock(side_effect=OSError("save failed"))

    with pytest.raises(AssetStorePersistenceError) as raised:
        await manager.async_set_asset_lifecycle(
            asset["asset_uuid"],
            "retired",
            effective_date=None,
            notes=None,
        )

    assert raised.value.code == "persistence_error"
    assert manager._data == before


async def test_ambiguous_lifecycle_persistence_recovers_before_retry(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Recovery")
    before = deepcopy(manager._data)
    manager._store.async_save = AsyncMock(
        side_effect=AssetStorePersistenceError("unknown", ambiguous=True)
    )

    with pytest.raises(AssetStorePersistenceError):
        await manager.async_set_asset_lifecycle(
            asset["asset_uuid"],
            "retired",
            effective_date=None,
            notes=None,
        )
    assert manager._data == before
    assert manager._persistence_uncertain is True

    manager._store.async_load_persisted_snapshot = AsyncMock(return_value=before)
    manager._store.async_save = AsyncMock()
    changed = await manager.async_set_asset_lifecycle(
        asset["asset_uuid"],
        "retired",
        effective_date=None,
        notes=None,
    )

    assert changed["lifecycle"]["status"] == "retired"
    manager._store.async_load_persisted_snapshot.assert_awaited_once()


@pytest.mark.parametrize(
    "corruption",
    [
        "broken_previous",
        "key_mismatch",
        "from_mismatch",
        "orphan",
        "cycle",
        "current_status_mismatch",
        "invalid_timestamp",
        "same_state_event",
        "missing_field",
    ],
)
async def test_complete_lifecycle_graph_corruption_is_rejected(
    hass: HomeAssistant,
    corruption: str,
) -> None:
    manager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Corrupt graph")
    data = deepcopy(manager._data)
    current_uuid = asset["lifecycle"]["current_event_uuid"]
    event = data["lifecycle_events"][current_uuid]

    if corruption == "broken_previous":
        event["previous_event_uuid"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    elif corruption == "key_mismatch":
        event["event_uuid"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    elif corruption == "from_mismatch":
        event["from_status"] = "retired"
    elif corruption == "orphan":
        orphan_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        data["lifecycle_events"][orphan_uuid] = {
            **event,
            "event_uuid": orphan_uuid,
            "previous_event_uuid": None,
        }
    elif corruption == "cycle":
        event["previous_event_uuid"] = current_uuid
    elif corruption == "current_status_mismatch":
        data["assets"][asset["asset_uuid"]]["lifecycle"]["status"] = "retired"
    elif corruption == "invalid_timestamp":
        event["recorded_at"] = "2026-08-10T10:00:00"
    elif corruption == "same_state_event":
        event["from_status"] = event["to_status"]
    elif corruption == "missing_field":
        del event["notes"]

    with pytest.raises(AssetStoreError) as raised:
        _validate_store_data(data)

    assert raised.value.code in {
        "invalid_lifecycle_status",
        "lifecycle_chain_invalid",
    }


async def test_previous_event_from_another_asset_is_rejected(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    first = await manager.async_create_manual_asset(name="First")
    second = await manager.async_create_manual_asset(name="Second")
    data = deepcopy(manager._data)
    first_event = first["lifecycle"]["current_event_uuid"]
    second_event = second["lifecycle"]["current_event_uuid"]
    data["lifecycle_events"][second_event]["previous_event_uuid"] = first_event

    with pytest.raises(AssetStoreError) as raised:
        _validate_store_data(data)

    assert raised.value.code == "lifecycle_chain_invalid"
