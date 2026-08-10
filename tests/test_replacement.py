"""Physical Asset replacement graph and atomic correction tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.device_lifecycle.storage import (
    AssetStoreError,
    AssetStoreManager,
    AssetStorePersistenceError,
    _validate_store_data,
)


def _manager(hass: HomeAssistant) -> AssetStoreManager:
    manager = AssetStoreManager(hass)
    manager._store.async_save = AsyncMock()
    return manager


async def _assets(
    manager: AssetStoreManager,
    count: int = 4,
) -> list[dict]:
    return [
        await manager.async_create_manual_asset(name=f"Asset {index}")
        for index in range(count)
    ]


async def test_valid_long_replacement_chain_and_detached_queries(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    assets = await _assets(manager)
    manager._store.async_save.reset_mock()

    records = []
    for predecessor, successor in zip(assets[:-1], assets[1:], strict=True):
        records.append(
            await manager.async_create_asset_replacement(
                predecessor["asset_uuid"],
                successor["asset_uuid"],
                reason="planned_refresh",
                effective_date=None,
                notes=None,
            )
        )

    assert manager._store.async_save.await_count == 3
    assert manager.active_replacement_successor(assets[0]["asset_uuid"])[
        "asset_uuid"
    ] == assets[1]["asset_uuid"]
    assert manager.active_replacement_predecessor(assets[-1]["asset_uuid"])[
        "asset_uuid"
    ] == assets[-2]["asset_uuid"]
    middle_records = manager.replacement_records_for_asset(assets[1]["asset_uuid"])
    assert len(middle_records) == 2
    records[0]["reason"] = "failure"
    assert manager.replacement_record(records[0]["replacement_uuid"])["reason"] == (
        "planned_refresh"
    )


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("self", "replacement_self_reference"),
        ("outgoing", "replacement_predecessor_conflict"),
        ("incoming", "replacement_successor_conflict"),
        ("two_cycle", "replacement_cycle"),
        ("long_cycle", "replacement_cycle"),
    ],
)
async def test_invalid_replacement_graph_mutations_are_atomic(
    hass: HomeAssistant,
    mode: str,
    code: str,
) -> None:
    manager = _manager(hass)
    a, b, c, *_ = await _assets(manager)
    if mode != "self":
        await manager.async_create_asset_replacement(
            a["asset_uuid"],
            b["asset_uuid"],
            reason="unknown",
            effective_date=None,
            notes=None,
        )
    if mode == "long_cycle":
        await manager.async_create_asset_replacement(
            b["asset_uuid"],
            c["asset_uuid"],
            reason="upgrade",
            effective_date=None,
            notes=None,
        )
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()
    endpoints = {
        "self": (a, a),
        "outgoing": (a, c),
        "incoming": (c, b),
        "two_cycle": (b, a),
        "long_cycle": (c, a),
    }
    predecessor, successor = endpoints[mode]

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_create_asset_replacement(
            predecessor["asset_uuid"],
            successor["asset_uuid"],
            reason="failure",
            effective_date=None,
            notes=None,
        )

    assert raised.value.code == code
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()


async def test_duplicate_active_relation_is_rejected(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    a, b, *_ = await _assets(manager)
    await manager.async_create_asset_replacement(
        a["asset_uuid"],
        b["asset_uuid"],
        reason="failure",
        effective_date=None,
        notes=None,
    )

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_create_asset_replacement(
            a["asset_uuid"],
            b["asset_uuid"],
            reason="failure",
            effective_date=None,
            notes=None,
        )

    assert raised.value.code == "replacement_predecessor_conflict"


@pytest.mark.parametrize("reason", ["rma", "refresh", ""])
async def test_invalid_replacement_reason_is_structured(
    hass: HomeAssistant,
    reason: str,
) -> None:
    manager = _manager(hass)
    a, b, *_ = await _assets(manager)

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_create_asset_replacement(
            a["asset_uuid"],
            b["asset_uuid"],
            reason=reason,
            effective_date=None,
            notes=None,
        )

    assert raised.value.code == "invalid_replacement_reason"


async def test_missing_assets_and_invalid_replacement_uuid_fail_closed(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    a, *_ = await _assets(manager)

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_create_asset_replacement(
            a["asset_uuid"],
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            reason="unknown",
            effective_date=None,
            notes=None,
        )
    assert raised.value.code == "asset_missing"

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_void_asset_replacement(
            "not-a-uuid",
            void_reason="Incorrect",
        )
    assert raised.value.code == "replacement_missing"


async def test_replacement_dates_and_chain_ordering(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    a, b, c, *_ = await _assets(manager)
    today = dt_util.now().date()
    await manager.async_create_asset_replacement(
        a["asset_uuid"],
        b["asset_uuid"],
        reason="upgrade",
        effective_date=today.isoformat(),
        notes=None,
    )
    before = deepcopy(manager._data)

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_create_asset_replacement(
            b["asset_uuid"],
            c["asset_uuid"],
            reason="upgrade",
            effective_date=(today - timedelta(days=1)).isoformat(),
            notes=None,
        )
    assert raised.value.code == "replacement_graph_invalid"
    assert manager._data == before

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_create_asset_replacement(
            b["asset_uuid"],
            c["asset_uuid"],
            reason="upgrade",
            effective_date=(today + timedelta(days=1)).isoformat(),
            notes=None,
        )
    assert raised.value.code == "replacement_date_in_future"


async def test_unknown_date_does_not_infer_chain_order(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    a, b, c, *_ = await _assets(manager)
    today = dt_util.now().date()

    await manager.async_create_asset_replacement(
        a["asset_uuid"],
        b["asset_uuid"],
        reason="unknown",
        effective_date=None,
        notes=None,
    )
    await manager.async_create_asset_replacement(
        b["asset_uuid"],
        c["asset_uuid"],
        reason="unknown",
        effective_date=(today - timedelta(days=100)).isoformat(),
        notes=None,
    )

    _validate_store_data(manager._data)


async def test_void_requires_reason_retains_history_and_frees_graph(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    a, b, c, *_ = await _assets(manager)
    record = await manager.async_create_asset_replacement(
        a["asset_uuid"],
        b["asset_uuid"],
        reason="failure",
        effective_date=None,
        notes="Original",
    )
    before = deepcopy(manager._data)

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_void_asset_replacement(
            record["replacement_uuid"],
            void_reason="  ",
        )
    assert raised.value.code == "replacement_void_reason_required"
    assert manager._data == before

    voided = await manager.async_void_asset_replacement(
        record["replacement_uuid"],
        void_reason="Wrong physical mapping",
    )
    assert voided["voided_at"] is not None
    assert voided["void_reason"] == "Wrong physical mapping"
    assert manager.replacement_records_for_asset(a["asset_uuid"]) == []
    assert len(
        manager.replacement_records_for_asset(
            a["asset_uuid"], include_voided=True
        )
    ) == 1

    await manager.async_create_asset_replacement(
        a["asset_uuid"],
        c["asset_uuid"],
        reason="other",
        effective_date=None,
        notes=None,
    )
    with pytest.raises(AssetStoreError) as raised:
        await manager.async_void_asset_replacement(
            record["replacement_uuid"],
            void_reason="Again",
        )
    assert raised.value.code == "replacement_missing"


async def test_correction_voids_old_and_creates_new_in_one_save(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    a, b, c, *_ = await _assets(manager)
    old = await manager.async_create_asset_replacement(
        a["asset_uuid"],
        b["asset_uuid"],
        reason="failure",
        effective_date=None,
        notes=None,
    )
    manager._store.async_save.reset_mock()

    new = await manager.async_correct_asset_replacement(
        old["replacement_uuid"],
        predecessor_asset_uuid=a["asset_uuid"],
        successor_asset_uuid=c["asset_uuid"],
        reason="warranty_rma",
        effective_date=None,
        notes="Correct successor",
        void_reason="Selected the wrong replacement",
    )

    manager._store.async_save.assert_awaited_once()
    retained = manager.replacement_record(old["replacement_uuid"])
    assert retained["voided_at"] is not None
    assert retained["void_reason"] == "Selected the wrong replacement"
    assert new["voided_at"] is None
    assert manager.active_replacement_successor(a["asset_uuid"])["asset_uuid"] == (
        c["asset_uuid"]
    )


async def test_failed_correction_leaves_old_relationship_active(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    a, b, c, d = await _assets(manager)
    old = await manager.async_create_asset_replacement(
        a["asset_uuid"],
        b["asset_uuid"],
        reason="failure",
        effective_date=None,
        notes=None,
    )
    await manager.async_create_asset_replacement(
        c["asset_uuid"],
        d["asset_uuid"],
        reason="upgrade",
        effective_date=None,
        notes=None,
    )
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_correct_asset_replacement(
            old["replacement_uuid"],
            predecessor_asset_uuid=a["asset_uuid"],
            successor_asset_uuid=d["asset_uuid"],
            reason="other",
            effective_date=None,
            notes=None,
            void_reason="Correction",
        )

    assert raised.value.code == "replacement_successor_conflict"
    assert manager._data == before
    assert manager.replacement_record(old["replacement_uuid"])["voided_at"] is None
    manager._store.async_save.assert_not_awaited()


async def test_failed_correction_save_publishes_no_partial_void(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    a, b, c, *_ = await _assets(manager)
    old = await manager.async_create_asset_replacement(
        a["asset_uuid"],
        b["asset_uuid"],
        reason="failure",
        effective_date=None,
        notes=None,
    )
    before = deepcopy(manager._data)
    manager._store.async_save = AsyncMock(side_effect=OSError("save failed"))

    with pytest.raises(AssetStorePersistenceError) as raised:
        await manager.async_correct_asset_replacement(
            old["replacement_uuid"],
            predecessor_asset_uuid=a["asset_uuid"],
            successor_asset_uuid=c["asset_uuid"],
            reason="other",
            effective_date=None,
            notes=None,
            void_reason="Correction",
        )

    assert raised.value.code == "persistence_error"
    assert manager._data == before
    assert manager.replacement_record(old["replacement_uuid"])["voided_at"] is None


async def test_replacement_does_not_mutate_independent_asset_domains(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    a, b, *_ = await _assets(manager)
    before_a = deepcopy(manager.asset(a["asset_uuid"]))
    before_b = deepcopy(manager.asset(b["asset_uuid"]))

    await manager.async_create_asset_replacement(
        a["asset_uuid"],
        b["asset_uuid"],
        reason="warranty_rma",
        effective_date=None,
        notes=None,
    )

    assert manager.asset(a["asset_uuid"]) == before_a
    assert manager.asset(b["asset_uuid"]) == before_b


@pytest.mark.parametrize(
    "corruption",
    ["key", "self", "reason", "timestamp", "missing_predecessor", "missing_field"],
)
async def test_corrupt_replacement_record_is_rejected(
    hass: HomeAssistant,
    corruption: str,
) -> None:
    manager = _manager(hass)
    a, b, *_ = await _assets(manager)
    record = await manager.async_create_asset_replacement(
        a["asset_uuid"],
        b["asset_uuid"],
        reason="failure",
        effective_date=None,
        notes=None,
    )
    data = deepcopy(manager._data)
    stored = data["replacement_records"][record["replacement_uuid"]]
    if corruption == "key":
        stored["replacement_uuid"] = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    elif corruption == "self":
        stored["successor_asset_uuid"] = stored["predecessor_asset_uuid"]
    elif corruption == "reason":
        stored["reason"] = "rma"
    elif corruption == "timestamp":
        stored["recorded_at"] = "2026-08-10T12:00:00"
    elif corruption == "missing_predecessor":
        stored["predecessor_asset_uuid"] = (
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        )
    elif corruption == "missing_field":
        del stored["void_reason"]

    with pytest.raises(AssetStoreError):
        _validate_store_data(data)
