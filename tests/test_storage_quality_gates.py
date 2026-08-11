"""Focused Store 3.1 validation and history failure quality gates."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
import pytest

from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import (
    AssetStoreError,
    AssetStoreManager,
    DeviceLifecycleStore,
    STORAGE_KEY,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    _migrate_v1_to_v2_1,
    _normalize_price,
    _runtime_seconds,
    _validate_store_data,
    _warranty_type,
)

from .conftest import ASSET_UUID
from .test_options_flow import _manager

EVENT_ONE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
EVENT_TWO = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"
REPLACEMENT_ONE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
REPLACEMENT_TWO = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"
SECOND_ASSET = "cccccccc-cccc-4ccc-8ccc-ccccccccccc2"
THIRD_ASSET = "cccccccc-cccc-4ccc-8ccc-ccccccccccc3"
RECORDED_ONE = "2026-08-09T10:00:00+00:00"
RECORDED_TWO = "2026-08-09T11:00:00+00:00"


def _add_asset(data: AssetStoreData, asset_uuid: str, asset_id: str) -> None:
    """Add a detached valid Asset without inventing Purchase membership."""
    asset = deepcopy(next(iter(data["assets"].values())))
    asset["asset_uuid"] = asset_uuid
    asset["asset_id"] = asset_id
    asset["name"] = f"Asset {asset_id}"
    asset["purchase_uuid"] = None
    asset["installed_date"] = None
    asset["ha_device_refs"] = []
    asset["lifecycle"] = {"status": "unknown", "current_event_uuid": None}
    asset["field_sources"].pop("purchase_uuid", None)
    data["assets"][asset_uuid] = asset
    data["next_asset_number"] = int(asset_id[2:]) + 1


def _event(
    event_uuid: str,
    *,
    asset_uuid: str = ASSET_UUID,
    previous_event_uuid: str | None = None,
    from_status: str = "unknown",
    to_status: str = "active",
    effective_date: str | None = "2026-08-09",
    recorded_at: str = RECORDED_ONE,
) -> dict[str, Any]:
    return {
        "event_uuid": event_uuid,
        "asset_uuid": asset_uuid,
        "previous_event_uuid": previous_event_uuid,
        "from_status": from_status,
        "to_status": to_status,
        "effective_date": effective_date,
        "recorded_at": recorded_at,
        "notes": None,
    }


def _record(
    replacement_uuid: str,
    *,
    predecessor: str = ASSET_UUID,
    successor: str = SECOND_ASSET,
    effective_date: str | None = "2026-08-09",
) -> dict[str, Any]:
    return {
        "replacement_uuid": replacement_uuid,
        "predecessor_asset_uuid": predecessor,
        "successor_asset_uuid": successor,
        "reason": "failure",
        "effective_date": effective_date,
        "recorded_at": RECORDED_ONE,
        "notes": None,
        "voided_at": None,
        "void_reason": None,
    }


def _valid_lifecycle(data: AssetStoreData) -> AssetStoreData:
    result = deepcopy(data)
    result["lifecycle_events"] = {EVENT_ONE: _event(EVENT_ONE)}
    result["assets"][ASSET_UUID]["lifecycle"] = {
        "status": "active",
        "current_event_uuid": EVENT_ONE,
    }
    return result


def _valid_replacement(data: AssetStoreData) -> AssetStoreData:
    result = deepcopy(data)
    _add_asset(result, SECOND_ASSET, "DL0008")
    result["replacement_records"] = {
        REPLACEMENT_ONE: _record(REPLACEMENT_ONE)
    }
    return result


def _set(data: AssetStoreData, path: tuple[str, ...], value: Any) -> None:
    target: Any = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        (("lifecycle_events", EVENT_ONE), "not-a-mapping", "lifecycle_chain_invalid"),
        (("lifecycle_events", EVENT_ONE, "event_uuid"), EVENT_TWO, "lifecycle_chain_invalid"),
        (("lifecycle_events", EVENT_ONE, "asset_uuid"), SECOND_ASSET, "lifecycle_chain_invalid"),
        (("lifecycle_events", EVENT_ONE, "from_status"), "stored", "invalid_lifecycle_status"),
        (("lifecycle_events", EVENT_ONE, "to_status"), "unknown", "lifecycle_chain_invalid"),
        (("lifecycle_events", EVENT_ONE, "previous_event_uuid"), "bad", "lifecycle_chain_invalid"),
        (("lifecycle_events", EVENT_ONE, "effective_date"), 123, "invalid_lifecycle_effective_date"),
        (("lifecycle_events", EVENT_ONE, "effective_date"), "20260809", "invalid_lifecycle_effective_date"),
        (("lifecycle_events", EVENT_ONE, "effective_date"), "2099-01-01", "lifecycle_date_in_future"),
        (("lifecycle_events", EVENT_ONE, "recorded_at"), 123, "lifecycle_chain_invalid"),
        (("lifecycle_events", EVENT_ONE, "recorded_at"), "bad", "lifecycle_chain_invalid"),
        (("lifecycle_events", EVENT_ONE, "recorded_at"), "2026-08-09T10:00:00", "lifecycle_chain_invalid"),
        (("lifecycle_events", EVENT_ONE, "recorded_at"), "2026-08-09T13:00:00+03:00", "lifecycle_chain_invalid"),
        (("lifecycle_events", EVENT_ONE, "notes"), 123, "lifecycle_chain_invalid"),
        (("assets", ASSET_UUID, "lifecycle"), "invalid", "lifecycle_chain_invalid"),
        (("assets", ASSET_UUID, "lifecycle", "status"), "stored", "invalid_lifecycle_status"),
        (("assets", ASSET_UUID, "lifecycle", "current_event_uuid"), EVENT_TWO, "lifecycle_chain_invalid"),
    ],
)
def test_lifecycle_event_validation_rejects_each_corrupt_field(
    asset_store_data: AssetStoreData,
    path: tuple[str, ...],
    value: Any,
    code: str,
) -> None:
    """Each immutable event field and current-state pointer fails closed."""
    data = _valid_lifecycle(asset_store_data)
    _set(data, path, value)

    with pytest.raises(AssetStoreError) as raised:
        _validate_store_data(data)

    assert raised.value.code == code


def test_lifecycle_event_structure_key_and_no_history_state_are_validated(
    asset_store_data: AssetStoreData,
) -> None:
    """Store validation rejects wrong keys, invalid UUID keys, and fake current state."""
    wrong_structure = _valid_lifecycle(asset_store_data)
    wrong_structure["lifecycle_events"][EVENT_ONE].pop("notes")
    invalid_key = _valid_lifecycle(asset_store_data)
    invalid_key["lifecycle_events"]["bad"] = invalid_key["lifecycle_events"].pop(
        EVENT_ONE
    )
    invalid_key["assets"][ASSET_UUID]["lifecycle"]["current_event_uuid"] = "bad"
    no_history = deepcopy(asset_store_data)
    no_history["assets"][ASSET_UUID]["lifecycle"]["status"] = "active"

    for corrupt in (wrong_structure, invalid_key, no_history):
        with pytest.raises(AssetStoreError) as raised:
            _validate_store_data(corrupt)
        assert raised.value.code == "lifecycle_chain_invalid"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("second_root", "lifecycle_chain_invalid"),
        ("wrong_root_status", "lifecycle_chain_invalid"),
        ("missing_previous", "lifecycle_chain_invalid"),
        ("discontinuous", "lifecycle_chain_invalid"),
        ("branch", "lifecycle_chain_invalid"),
        ("decreasing_date", "lifecycle_chain_invalid"),
        ("decreasing_recorded", "lifecycle_chain_invalid"),
        ("current_status", "lifecycle_chain_invalid"),
    ],
)
def test_complete_lifecycle_chain_validation(
    asset_store_data: AssetStoreData,
    mutation: str,
    code: str,
) -> None:
    """Whole-chain validation catches roots, links, branches, ordering, and head drift."""
    data = _valid_lifecycle(asset_store_data)
    second = _event(
        EVENT_TWO,
        previous_event_uuid=EVENT_ONE,
        from_status="active",
        to_status="retired",
        effective_date="2026-08-10",
        recorded_at=RECORDED_TWO,
    )
    data["lifecycle_events"][EVENT_TWO] = second
    data["assets"][ASSET_UUID]["lifecycle"] = {
        "status": "retired",
        "current_event_uuid": EVENT_TWO,
    }

    if mutation == "second_root":
        second["previous_event_uuid"] = None
        second["from_status"] = "unknown"
    elif mutation == "wrong_root_status":
        data["lifecycle_events"][EVENT_ONE]["from_status"] = "lost"
    elif mutation == "missing_previous":
        second["previous_event_uuid"] = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    elif mutation == "discontinuous":
        second["from_status"] = "lost"
    elif mutation == "branch":
        third_uuid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa3"
        data["lifecycle_events"][third_uuid] = _event(
            third_uuid,
            previous_event_uuid=EVENT_ONE,
            from_status="active",
            to_status="lost",
            effective_date="2026-08-10",
            recorded_at=RECORDED_TWO,
        )
    elif mutation == "decreasing_date":
        second["effective_date"] = "2026-08-08"
    elif mutation == "decreasing_recorded":
        second["recorded_at"] = "2026-08-09T09:00:00+00:00"
    elif mutation == "current_status":
        data["assets"][ASSET_UUID]["lifecycle"]["status"] = "lost"

    with pytest.raises(AssetStoreError) as raised:
        _validate_store_data(data)
    assert raised.value.code == code


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        (("replacement_records", REPLACEMENT_ONE), "not-a-mapping", "replacement_missing"),
        (("replacement_records", REPLACEMENT_ONE, "replacement_uuid"), REPLACEMENT_TWO, "replacement_missing"),
        (("replacement_records", REPLACEMENT_ONE, "predecessor_asset_uuid"), THIRD_ASSET, "asset_missing"),
        (("replacement_records", REPLACEMENT_ONE, "successor_asset_uuid"), THIRD_ASSET, "asset_missing"),
        (("replacement_records", REPLACEMENT_ONE, "successor_asset_uuid"), ASSET_UUID, "replacement_self_reference"),
        (("replacement_records", REPLACEMENT_ONE, "reason"), "returned", "invalid_replacement_reason"),
        (("replacement_records", REPLACEMENT_ONE, "effective_date"), 123, "invalid_replacement_effective_date"),
        (("replacement_records", REPLACEMENT_ONE, "effective_date"), "20260809", "invalid_replacement_effective_date"),
        (("replacement_records", REPLACEMENT_ONE, "effective_date"), "2099-01-01", "replacement_date_in_future"),
        (("replacement_records", REPLACEMENT_ONE, "recorded_at"), "bad", "replacement_graph_invalid"),
        (("replacement_records", REPLACEMENT_ONE, "notes"), 123, "replacement_graph_invalid"),
        (("replacement_records", REPLACEMENT_ONE, "void_reason"), "should be null", "replacement_graph_invalid"),
    ],
)
def test_replacement_record_validation_rejects_each_corrupt_field(
    asset_store_data: AssetStoreData,
    path: tuple[str, ...],
    value: Any,
    code: str,
) -> None:
    """Each permanent record field is checked before graph construction."""
    data = _valid_replacement(asset_store_data)
    _set(data, path, value)

    with pytest.raises(AssetStoreError) as raised:
        _validate_store_data(data)

    assert raised.value.code == code


@pytest.mark.parametrize(
    ("history_kind", "code"),
    [
        ("lifecycle", "invalid_lifecycle_effective_date"),
        ("replacement", "invalid_replacement_effective_date"),
    ],
)
def test_store_rejects_parseable_noncanonical_history_effective_dates(
    asset_store_data: AssetStoreData,
    history_kind: str,
    code: str,
) -> None:
    """Compact ISO input accepted by fromisoformat is not canonical Store data."""
    noncanonical = "20260809"
    assert date.fromisoformat(noncanonical).isoformat() == "2026-08-09"
    if history_kind == "lifecycle":
        data = _valid_lifecycle(asset_store_data)
        data["lifecycle_events"][EVENT_ONE]["effective_date"] = noncanonical
    else:
        data = _valid_replacement(asset_store_data)
        data["replacement_records"][REPLACEMENT_ONE][
            "effective_date"
        ] = noncanonical

    with pytest.raises(AssetStoreError) as raised:
        _validate_store_data(data)

    assert raised.value.code == code


def test_replacement_structure_uuid_and_void_metadata_are_validated(
    asset_store_data: AssetStoreData,
) -> None:
    """Wrong schemas, UUID keys, and malformed permanent void data fail closed."""
    wrong_structure = _valid_replacement(asset_store_data)
    wrong_structure["replacement_records"][REPLACEMENT_ONE].pop("notes")
    invalid_key = _valid_replacement(asset_store_data)
    invalid_key["replacement_records"]["bad"] = invalid_key[
        "replacement_records"
    ].pop(REPLACEMENT_ONE)
    void_before_record = _valid_replacement(asset_store_data)
    void_before_record["replacement_records"][REPLACEMENT_ONE].update(
        {"voided_at": "2026-08-09T09:00:00+00:00", "void_reason": "Correction"}
    )
    void_without_reason = _valid_replacement(asset_store_data)
    void_without_reason["replacement_records"][REPLACEMENT_ONE].update(
        {"voided_at": RECORDED_TWO, "void_reason": " "}
    )
    invalid_void_timestamp = _valid_replacement(asset_store_data)
    invalid_void_timestamp["replacement_records"][REPLACEMENT_ONE].update(
        {"voided_at": "bad", "void_reason": "Correction"}
    )

    expected_codes = (
        "replacement_graph_invalid",
        "replacement_missing",
        "replacement_graph_invalid",
        "replacement_graph_invalid",
        "replacement_graph_invalid",
    )
    for corrupt, code in zip(
        (
            wrong_structure,
            invalid_key,
            void_before_record,
            void_without_reason,
            invalid_void_timestamp,
        ),
        expected_codes,
        strict=True,
    ):
        with pytest.raises(AssetStoreError) as raised:
            _validate_store_data(corrupt)
        assert raised.value.code == code


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("duplicate", "replacement_predecessor_conflict"),
        ("outgoing", "replacement_predecessor_conflict"),
        ("incoming", "replacement_successor_conflict"),
        ("cycle", "replacement_cycle"),
        ("date_inversion", "replacement_graph_invalid"),
    ],
)
def test_replacement_whole_graph_conflicts_and_ordering(
    asset_store_data: AssetStoreData,
    mutation: str,
    code: str,
) -> None:
    """The active graph enforces 1:1 cardinality, acyclicity, and date order."""
    data = _valid_replacement(asset_store_data)
    _add_asset(data, THIRD_ASSET, "DL0009")
    if mutation == "duplicate":
        second = _record(REPLACEMENT_TWO)
    elif mutation == "outgoing":
        second = _record(REPLACEMENT_TWO, successor=THIRD_ASSET)
    elif mutation == "incoming":
        second = _record(
            REPLACEMENT_TWO, predecessor=THIRD_ASSET, successor=SECOND_ASSET
        )
    elif mutation == "cycle":
        second = _record(
            REPLACEMENT_TWO, predecessor=SECOND_ASSET, successor=ASSET_UUID
        )
    else:
        data["replacement_records"][REPLACEMENT_ONE]["effective_date"] = "2026-08-10"
        second = _record(
            REPLACEMENT_TWO,
            predecessor=SECOND_ASSET,
            successor=THIRD_ASSET,
            effective_date="2026-08-09",
        )
    data["replacement_records"][REPLACEMENT_TWO] = second

    with pytest.raises(AssetStoreError) as raised:
        _validate_store_data(data)
    assert raised.value.code == code


async def test_history_input_normalization_uuid_collision_and_missing_queries(
    hass: HomeAssistant,
) -> None:
    """Manager APIs reject malformed inputs and never guess missing identities."""
    manager = _manager(hass)
    first = await manager.async_create_manual_asset(name="First")
    second = await manager.async_create_manual_asset(name="Second")
    missing_uuid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"

    bad_lifecycle_inputs = (
        ("2026-8-9", None),
        (123, None),
        (None, 123),
    )
    for effective_date, notes in bad_lifecycle_inputs:
        with pytest.raises(AssetStoreError):
            await manager.async_set_asset_lifecycle(
                first["asset_uuid"],
                "retired",
                effective_date=effective_date,  # type: ignore[arg-type]
                notes=notes,  # type: ignore[arg-type]
            )

    with pytest.raises(AssetStoreError, match="does not exist"):
        manager.lifecycle_events_for_asset(missing_uuid)
    with pytest.raises(AssetStoreError, match="does not exist"):
        manager.replacement_records_for_asset(missing_uuid)
    with pytest.raises(AssetStoreError, match="does not exist"):
        manager.active_replacement_predecessor(missing_uuid)
    with pytest.raises(AssetStoreError, match="does not exist"):
        manager.active_replacement_successor(missing_uuid)
    assert manager.lifecycle_event(None) is None
    assert manager.replacement_record(None) is None

    existing_event = first["lifecycle"]["current_event_uuid"]
    with patch(
        "custom_components.device_lifecycle.storage.uuid.uuid4",
        return_value=existing_event,
    ), pytest.raises(AssetStoreError) as lifecycle_collision:
        await manager.async_set_asset_lifecycle(
            first["asset_uuid"], "retired", effective_date=None, notes=None
        )
    assert lifecycle_collision.value.code == "lifecycle_chain_invalid"

    record = await manager.async_create_asset_replacement(
        first["asset_uuid"],
        second["asset_uuid"],
        reason="failure",
        effective_date=None,
        notes="",
    )
    await manager.async_void_asset_replacement(
        record["replacement_uuid"], void_reason="Free active graph"
    )
    with patch(
        "custom_components.device_lifecycle.storage.uuid.uuid4",
        return_value=record["replacement_uuid"],
    ), pytest.raises(AssetStoreError) as replacement_collision:
        await manager.async_create_asset_replacement(
            first["asset_uuid"],
            second["asset_uuid"],
            reason="failure",
            effective_date=None,
            notes=None,
        )
    assert replacement_collision.value.code == "replacement_graph_invalid"


async def test_void_and_correction_manager_error_contracts(
    hass: HomeAssistant,
) -> None:
    """Void/correction failures are deterministic and leave history active."""
    manager = _manager(hass)
    old = await manager.async_create_manual_asset(name="Old")
    new = await manager.async_create_manual_asset(name="New")
    other = await manager.async_create_manual_asset(name="Other")
    record = await manager.async_create_asset_replacement(
        old["asset_uuid"], new["asset_uuid"], reason="failure", effective_date=None, notes=None
    )

    for replacement_uuid in ("bad", "dddddddd-dddd-4ddd-8ddd-dddddddddddd"):
        with pytest.raises(AssetStoreError) as raised:
            await manager.async_void_asset_replacement(
                replacement_uuid, void_reason="Correction"
            )
        assert raised.value.code == "replacement_missing"

    with pytest.raises(AssetStoreError) as bad_reason:
        await manager.async_void_asset_replacement(
            record["replacement_uuid"], void_reason=123  # type: ignore[arg-type]
        )
    assert bad_reason.value.code == "replacement_void_reason_required"

    for replacement_uuid in ("bad", "dddddddd-dddd-4ddd-8ddd-dddddddddddd"):
        with pytest.raises(AssetStoreError) as raised:
            await manager.async_correct_asset_replacement(
                replacement_uuid,
                predecessor_asset_uuid=old["asset_uuid"],
                successor_asset_uuid=other["asset_uuid"],
                reason="other",
                effective_date=None,
                notes=None,
                void_reason="Correction",
            )
        assert raised.value.code == "replacement_missing"

    with pytest.raises(AssetStoreError) as correction_reason:
        await manager.async_correct_asset_replacement(
            record["replacement_uuid"],
            predecessor_asset_uuid=old["asset_uuid"],
            successor_asset_uuid=other["asset_uuid"],
            reason="other",
            effective_date=None,
            notes=None,
            void_reason=123,  # type: ignore[arg-type]
        )
    assert correction_reason.value.code == "replacement_void_reason_required"
    assert manager.replacement_record(record["replacement_uuid"])["voided_at"] is None

    await manager.async_void_asset_replacement(
        record["replacement_uuid"], void_reason="Actually void"
    )
    for mutation in ("void", "correct"):
        with pytest.raises(AssetStoreError) as raised:
            if mutation == "void":
                await manager.async_void_asset_replacement(
                    record["replacement_uuid"], void_reason="Again"
                )
            else:
                await manager.async_correct_asset_replacement(
                    record["replacement_uuid"],
                    predecessor_asset_uuid=old["asset_uuid"],
                    successor_asset_uuid=other["asset_uuid"],
                    reason="other",
                    effective_date=None,
                    notes=None,
                    void_reason="Again",
                )
        assert raised.value.code == "replacement_missing"


async def test_history_oserror_is_structured_and_does_not_publish(
    hass: HomeAssistant,
) -> None:
    """An unstructured Store write error becomes persistence_error atomically."""
    manager: AssetStoreManager = _manager(hass)
    asset = await manager.async_create_manual_asset(name="Atomic")
    before = deepcopy(manager._data)
    manager._store.async_save = AsyncMock(side_effect=OSError("disk unavailable"))

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_set_asset_lifecycle(
            asset["asset_uuid"], "retired", effective_date=None, notes=None
        )

    assert raised.value.code == "persistence_error"
    assert manager._data == before
    assert manager._persistence_uncertain is False


async def test_future_history_date_uses_ha_local_calendar(
    hass: HomeAssistant,
) -> None:
    """Both history APIs reject tomorrow according to Home Assistant local time."""
    manager = _manager(hass)
    old = await manager.async_create_manual_asset(name="Old")
    new = await manager.async_create_manual_asset(name="New")
    tomorrow = (dt_util.now().date() + timedelta(days=1)).isoformat()

    with pytest.raises(AssetStoreError) as lifecycle:
        await manager.async_set_asset_lifecycle(
            old["asset_uuid"], "retired", effective_date=tomorrow, notes=None
        )
    with pytest.raises(AssetStoreError) as replacement:
        await manager.async_create_asset_replacement(
            old["asset_uuid"],
            new["asset_uuid"],
            reason="failure",
            effective_date=tomorrow,
            notes=None,
        )

    assert lifecycle.value.code == "lifecycle_date_in_future"
    assert replacement.value.code == "replacement_date_in_future"


def test_low_level_normalizers_reject_unsafe_values_and_preserve_legacy_warranty() -> None:
    """Decimal/Runtime/date-era warranty helpers fail closed without float guessing."""
    for value in ("bad", -1):
        with pytest.raises(AssetStoreError):
            _normalize_price(value)
    for value in (Decimal("NaN"), Decimal("-1")):
        with pytest.raises(AssetStoreError):
            _runtime_seconds(value)
    assert _warranty_type({"warranty_until": "2027-01-01"}) == "manual"

    malformed_assets = {"assets": []}
    assert _migrate_v1_to_v2_1(malformed_assets, 1) == malformed_assets
    nonmapping_asset = {"assets": {ASSET_UUID: "invalid"}}
    assert _migrate_v1_to_v2_1(nonmapping_asset, 1) == nonmapping_asset


@pytest.mark.parametrize(
    "case",
    [
        "top_level",
        "asset_mapping",
        "asset_uuid_key",
        "asset_uuid_mismatch",
        "asset_id_format",
        "asset_id_duplicate",
        "asset_name",
        "asset_purchase_uuid",
        "installed_date",
        "warranty_mapping",
        "warranty_type",
        "field_sources_mapping",
        "field_source_value",
        "ha_refs_mapping",
        "ha_ref_value",
        "next_number_recycles",
        "purchase_mapping",
        "purchase_uuid_key",
        "purchase_uuid_mismatch",
        "purchase_configured",
        "purchase_subentry_id",
        "purchase_subentry_duplicate",
        "purchase_currency",
        "purchase_price_type",
        "purchase_price_decimal",
        "purchase_price_negative",
        "purchase_membership_type",
        "purchase_missing_asset",
        "purchase_member_mismatch",
        "asset_purchase_inconsistent",
    ],
)
def test_complete_store_validation_corruption_matrix(
    asset_store_data: AssetStoreData,
    case: str,
) -> None:
    """Every canonical Asset/Purchase structure is validated before publication."""
    data = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    purchase_uuid = next(iter(data["purchases"]))
    purchase = data["purchases"][purchase_uuid]

    if case == "top_level":
        data["lifecycle_events"] = []  # type: ignore[assignment]
    elif case == "asset_mapping":
        data["assets"][ASSET_UUID] = "invalid"  # type: ignore[assignment]
    elif case == "asset_uuid_key":
        data["assets"]["bad"] = data["assets"].pop(ASSET_UUID)
    elif case == "asset_uuid_mismatch":
        asset["asset_uuid"] = SECOND_ASSET
    elif case == "asset_id_format":
        asset["asset_id"] = "ASSET-7"
    elif case == "asset_id_duplicate":
        _add_asset(data, SECOND_ASSET, "DL0007")
    elif case == "asset_name":
        asset["name"] = " "
    elif case == "asset_purchase_uuid":
        asset["purchase_uuid"] = "bad"
    elif case == "installed_date":
        asset["installed_date"] = "bad"
    elif case == "warranty_mapping":
        asset["warranty"] = "invalid"  # type: ignore[assignment]
    elif case == "warranty_type":
        asset["warranty"]["type"] = "lifetime"
    elif case == "field_sources_mapping":
        asset["field_sources"] = "invalid"  # type: ignore[assignment]
    elif case == "field_source_value":
        asset["field_sources"]["name"] = "guessed"
    elif case == "ha_refs_mapping":
        asset["ha_device_refs"] = "invalid"  # type: ignore[assignment]
    elif case == "ha_ref_value":
        asset["ha_device_refs"] = ["invalid"]  # type: ignore[list-item]
    elif case == "next_number_recycles":
        data["next_asset_number"] = 7
    elif case == "purchase_mapping":
        data["purchases"][purchase_uuid] = "invalid"  # type: ignore[assignment]
    elif case == "purchase_uuid_key":
        data["purchases"]["bad"] = data["purchases"].pop(purchase_uuid)
    elif case == "purchase_uuid_mismatch":
        purchase["purchase_uuid"] = SECOND_ASSET
    elif case == "purchase_configured":
        purchase["configured"] = "yes"  # type: ignore[typeddict-item]
    elif case == "purchase_subentry_id":
        purchase["config_subentry_id"] = ""
    elif case == "purchase_subentry_duplicate":
        duplicate_uuid = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
        duplicate = deepcopy(purchase)
        duplicate["purchase_uuid"] = duplicate_uuid
        duplicate["asset_uuids"] = []
        data["purchases"][duplicate_uuid] = duplicate
    elif case == "purchase_currency":
        purchase["currency"] = " "
    elif case == "purchase_price_type":
        purchase["total_price"] = 12  # type: ignore[typeddict-item]
    elif case == "purchase_price_decimal":
        purchase["total_price"] = "bad"
    elif case == "purchase_price_negative":
        purchase["total_price"] = "-1"
    elif case == "purchase_membership_type":
        purchase["asset_uuids"] = [ASSET_UUID, ASSET_UUID]
    elif case == "purchase_missing_asset":
        purchase["asset_uuids"] = [SECOND_ASSET]
    elif case == "purchase_member_mismatch":
        asset["purchase_uuid"] = None
        asset["field_sources"].pop("purchase_uuid", None)
    elif case == "asset_purchase_inconsistent":
        asset["purchase_uuid"] = SECOND_ASSET

    with pytest.raises(AssetStoreError):
        _validate_store_data(data)


async def test_direct_persisted_snapshot_envelope_recovery_matrix(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """Recovery bypasses caches and rejects unreadable, malformed, or changed envelopes."""
    store = DeviceLifecycleStore(hass)

    with patch.object(
        hass,
        "async_add_executor_job",
        AsyncMock(side_effect=HomeAssistantError("unreadable")),
    ), pytest.raises(AssetStoreError) as unreadable:
        await store.async_load_persisted_snapshot()
    assert unreadable.value.code == "persistence_error"
    assert unreadable.value.ambiguous is True

    with patch.object(
        hass,
        "async_add_executor_job",
        AsyncMock(side_effect=[{}, False]),
    ):
        assert await store.async_load_persisted_snapshot() is None

    with patch.object(
        hass, "async_add_executor_job", AsyncMock(return_value=[])
    ), pytest.raises(AssetStoreError, match="envelope is invalid"):
        await store.async_load_persisted_snapshot()

    changed = {
        "version": STORAGE_VERSION,
        "minor_version": STORAGE_MINOR_VERSION + 1,
        "key": STORAGE_KEY,
        "data": asset_store_data,
    }
    with patch.object(
        hass, "async_add_executor_job", AsyncMock(return_value=changed)
    ), pytest.raises(AssetStoreError, match="changed during persistence recovery"):
        await store.async_load_persisted_snapshot()

    envelope = {
        "version": STORAGE_VERSION,
        "minor_version": STORAGE_MINOR_VERSION,
        "key": STORAGE_KEY,
        "data": asset_store_data,
    }
    with patch.object(
        hass, "async_add_executor_job", AsyncMock(return_value=envelope)
    ):
        recovered = await store.async_load_persisted_snapshot()
    assert recovered == asset_store_data
    assert recovered is not asset_store_data


async def test_ambiguous_recovery_refuses_disappeared_nonempty_store(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
) -> None:
    """A previously uncertain write cannot silently recover as an empty Store."""
    manager = _manager(hass, asset_store_data)
    manager._persistence_uncertain = True
    manager._store.async_load_persisted_snapshot = AsyncMock(return_value=None)

    with pytest.raises(AssetStoreError, match="disappeared during recovery"):
        await manager.async_set_asset_lifecycle(
            ASSET_UUID, "active", effective_date=None, notes=None
        )

    assert manager._persistence_uncertain is True
    manager._store.async_save.assert_not_awaited()
