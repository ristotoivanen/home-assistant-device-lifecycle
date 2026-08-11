"""Atomic Quick Asset Entry Store contract tests."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
import pytest

from custom_components.device_lifecycle.const import (
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DEPLOYMENT_STATE_UNKNOWN,
    LIFECYCLE_STATUS_ACTIVE,
    LIFECYCLE_STATUS_DISPOSED,
    LIFECYCLE_STATUS_LOST,
    LIFECYCLE_STATUS_RETIRED,
    LIFECYCLE_STATUS_UNKNOWN,
    WARRANTY_MANUAL,
    WARRANTY_NONE,
    WARRANTY_ONE_YEAR,
    WARRANTY_TWO_YEARS,
)
from custom_components.device_lifecycle.models import AssetStoreData, PurchaseData
from custom_components.device_lifecycle.storage import (
    AssetStoreError,
    AssetStoreManager,
    AssetStorePersistenceError,
    QuickAssetCreateRequest,
    home_assistant_asset_metadata,
)

QUICK_UUID = "44444444-4444-4444-8444-444444444444"
SECOND_QUICK_UUID = "55555555-5555-4555-8555-555555555555"
PURCHASE_UUID = "66666666-6666-4666-8666-666666666666"


def _manager(hass: HomeAssistant) -> AssetStoreManager:
    """Return an isolated manager with mocked durable persistence."""
    manager = AssetStoreManager(hass)
    manager._store.async_save = AsyncMock()
    return manager


def _metadata(**overrides: str | None) -> dict[str, str | None]:
    """Return every canonical editable metadata field."""
    values: dict[str, str | None] = {
        "name": "Quick Asset",
        "category": None,
        "manufacturer": None,
        "model": None,
        "model_id": None,
        "serial_number": None,
        "sw_version": None,
        "hw_version": None,
        "notes": None,
    }
    values.update(overrides)
    return values


def _request(**overrides: Any) -> QuickAssetCreateRequest:
    """Return one normalized manual Quick Create command."""
    values: dict[str, Any] = {
        "asset_uuid": QUICK_UUID,
        "primary_device_id": None,
        "metadata": _metadata(),
        "field_sources": {"name": "user"},
        "initial_lifecycle_status": LIFECYCLE_STATUS_ACTIVE,
        "initial_lifecycle_effective_date": None,
        "deployment_state": DEPLOYMENT_STATE_NOT_DEPLOYED,
        "installed_date": None,
        "ha_area_id": None,
        "warranty_type": WARRANTY_NONE,
        "warranty_until": None,
        "purchase_uuid": None,
    }
    values.update(overrides)
    return QuickAssetCreateRequest(**values)


def _purchase(
    *,
    configured: bool = True,
    purchase_date: str | None = "2026-08-07",
) -> PurchaseData:
    """Return one configured canonical Purchase without Asset members."""
    return {
        "purchase_uuid": PURCHASE_UUID,
        "config_subentry_id": "quick-purchase-subentry",
        "configured": configured,
        "name": "Quick Add Purchase",
        "purchase_date": purchase_date,
        "seller": "Example seller",
        "total_price": "100",
        "currency": "EUR",
        "receipt_reference": None,
        "receipt_url": None,
        "notes": None,
        "asset_uuids": [],
    }


async def _predecessor_request(
    manager: AssetStoreManager,
    *,
    lifecycle_status: str = LIFECYCLE_STATUS_ACTIVE,
    deployment_state: str = DEPLOYMENT_STATE_DEPLOYED,
    area_id: str | None = None,
    retire: bool = True,
    undeploy: bool = True,
) -> tuple[dict[str, Any], QuickAssetCreateRequest]:
    """Create a predecessor and return its exact reviewed Quick Create command."""
    predecessor = await manager.async_create_manual_asset(
        name="Old Asset",
        initial_lifecycle_status=lifecycle_status,
    )
    if deployment_state != DEPLOYMENT_STATE_NOT_DEPLOYED or area_id is not None:
        predecessor = await manager.async_set_asset_deployment(
            predecessor["asset_uuid"],
            deployment_state=deployment_state,
            installed_date="2025-01-10",
            ha_area_id=area_id,
        )
    request = _request(
        deployment_state=DEPLOYMENT_STATE_DEPLOYED,
        predecessor_asset_uuid=predecessor["asset_uuid"],
        expected_predecessor_lifecycle_status=predecessor["lifecycle"]["status"],
        expected_predecessor_current_event_uuid=predecessor["lifecycle"][
            "current_event_uuid"
        ],
        expected_predecessor_deployment_state=predecessor["deployment_state"],
        expected_predecessor_ha_area_id=predecessor["ha_area_id"],
        replacement_reason="failure",
        replacement_effective_date="2026-08-01",
        replacement_notes="Physical replacement",
        retire_predecessor=retire,
        undeploy_predecessor=undeploy,
    )
    return predecessor, request


async def test_manual_quick_create_allocates_requested_identity_once(
    hass: HomeAssistant,
) -> None:
    """Manual Quick Add creates one active Asset in one Store save."""
    manager = _manager(hass)

    result = await manager.async_quick_create_asset(_request())

    assert result.replayed is False
    assert result.asset["asset_uuid"] == QUICK_UUID
    assert result.asset["asset_id"] == "DL0001"
    assert result.asset["runtime"] == {"total_seconds": None}
    assert result.asset["purchase_uuid"] is None
    assert "purchase_uuid" not in result.asset["field_sources"]
    assert "installed_date" not in result.asset["field_sources"]
    assert "ha_area_id" not in result.asset["field_sources"]
    assert "warranty" not in result.asset["field_sources"]
    assert result.asset["lifecycle"]["status"] == LIFECYCLE_STATUS_ACTIVE
    assert len(manager.lifecycle_events_for_asset(QUICK_UUID)) == 1
    assert manager._data["next_asset_number"] == 2
    manager._store.async_save.assert_awaited_once()


async def test_ha_metadata_provenance_and_explicit_clear_survive_refresh(
    hass: HomeAssistant,
) -> None:
    """Accepted, edited, and cleared HA prefills retain distinct provenance."""
    manager = _manager(hass)
    request = _request(
        primary_device_id="ha-device",
        metadata=_metadata(
            name="HA Device",
            manufacturer="User manufacturer",
            model=None,
        ),
        field_sources={
            "name": "home_assistant",
            "manufacturer": "user",
            "model": "user",
        },
        deployment_state=DEPLOYMENT_STATE_DEPLOYED,
    )

    result = await manager.async_quick_create_asset(request)

    assert result.asset["ha_device_refs"] == [
        {"device_id": "ha-device", "role": "primary"}
    ]
    assert result.asset["field_sources"]["name"] == "home_assistant"
    assert result.asset["field_sources"]["manufacturer"] == "user"
    assert result.asset["field_sources"]["model"] == "user"
    stored = manager._data["assets"][QUICK_UUID]
    manager._refresh_home_assistant_metadata(
        stored,
        SimpleNamespace(
            name_by_user=None,
            name="HA Device",
            manufacturer="HA manufacturer",
            model="HA model",
            model_id=None,
            serial_number=None,
            sw_version=None,
            hw_version=None,
        ),
        "ha-device",
    )
    assert stored["manufacturer"] == "User manufacturer"
    assert stored["model"] is None


def test_quick_create_request_detaches_and_freezes_input_mappings() -> None:
    """Caller mutation cannot change an already constructed command."""
    metadata = _metadata()
    sources = {"name": "user"}
    request = _request(metadata=metadata, field_sources=sources)
    metadata["name"] = "Changed"
    sources["model"] = "user"

    assert request.metadata["name"] == "Quick Asset"
    assert "model" not in request.field_sources
    with pytest.raises(TypeError):
        request.metadata["name"] = "Nope"  # type: ignore[index]


@pytest.mark.parametrize(
    ("status", "event_count"),
    [
        (LIFECYCLE_STATUS_UNKNOWN, 0),
        (LIFECYCLE_STATUS_ACTIVE, 1),
        (LIFECYCLE_STATUS_RETIRED, 1),
        (LIFECYCLE_STATUS_DISPOSED, 1),
        (LIFECYCLE_STATUS_LOST, 1),
    ],
)
async def test_quick_create_initial_lifecycle_states(
    hass: HomeAssistant,
    status: str,
    event_count: int,
) -> None:
    """Unknown creates no event and every known initial state creates exactly one."""
    manager = _manager(hass)
    request = _request(
        initial_lifecycle_status=status,
        initial_lifecycle_effective_date=(
            None if status == LIFECYCLE_STATUS_UNKNOWN else "2026-08-01"
        ),
    )

    result = await manager.async_quick_create_asset(request)

    assert result.asset["lifecycle"]["status"] == status
    events = manager.lifecycle_events_for_asset(QUICK_UUID)
    assert len(events) == event_count
    if events:
        assert events[0]["from_status"] == LIFECYCLE_STATUS_UNKNOWN
        assert events[0]["to_status"] == status
        assert events[0]["effective_date"] == "2026-08-01"


async def test_quick_create_purchase_and_two_year_warranty_are_atomic(
    hass: HomeAssistant,
) -> None:
    """Selected Purchase membership and calculated warranty commit together."""
    manager = _manager(hass)
    manager._data["purchases"][PURCHASE_UUID] = _purchase()
    request = _request(
        purchase_uuid=PURCHASE_UUID,
        expected_purchase_date="2026-08-07",
        warranty_type=WARRANTY_TWO_YEARS,
        warranty_until="2028-08-07",
    )

    result = await manager.async_quick_create_asset(request)

    assert result.asset["purchase_uuid"] == PURCHASE_UUID
    assert result.asset["field_sources"]["purchase_uuid"] == "user"
    assert result.asset["field_sources"]["warranty"] == "user"
    assert result.asset["warranty"] == {
        "type": WARRANTY_TWO_YEARS,
        "until": "2028-08-07",
    }
    assert manager._data["purchases"][PURCHASE_UUID]["asset_uuids"] == [QUICK_UUID]
    manager._store.async_save.assert_awaited_once()


async def test_quick_create_manual_warranty_without_purchase(
    hass: HomeAssistant,
) -> None:
    """Manual warranty is independent from Purchase."""
    manager = _manager(hass)

    result = await manager.async_quick_create_asset(
        _request(warranty_type=WARRANTY_MANUAL, warranty_until="2027-12-31")
    )

    assert result.asset["purchase_uuid"] is None
    assert result.asset["warranty"] == {
        "type": WARRANTY_MANUAL,
        "until": "2027-12-31",
    }


async def test_quick_create_leap_day_warranty_uses_calendar_year(
    hass: HomeAssistant,
) -> None:
    """Calculated warranty retains existing leap-day-safe calendar behavior."""
    manager = _manager(hass)
    manager._data["purchases"][PURCHASE_UUID] = _purchase(
        purchase_date="2024-02-29"
    )

    result = await manager.async_quick_create_asset(
        _request(
            purchase_uuid=PURCHASE_UUID,
            expected_purchase_date="2024-02-29",
            warranty_type=WARRANTY_ONE_YEAR,
            warranty_until="2025-02-28",
        )
    )

    assert result.asset["warranty"]["until"] == "2025-02-28"


@pytest.mark.parametrize(
    ("purchase", "request_updates", "code"),
    [
        (None, {"purchase_uuid": PURCHASE_UUID}, "purchase_missing"),
        (
            _purchase(configured=False),
            {"purchase_uuid": PURCHASE_UUID},
            "purchase_not_configured",
        ),
        (
            None,
            {
                "warranty_type": WARRANTY_ONE_YEAR,
                "warranty_until": "2027-08-07",
            },
            "purchase_date_required_for_warranty",
        ),
        (
            _purchase(purchase_date=None),
            {
                "purchase_uuid": PURCHASE_UUID,
                "warranty_type": WARRANTY_ONE_YEAR,
                "warranty_until": "2027-08-07",
            },
            "purchase_date_required_for_warranty",
        ),
        (
            _purchase(purchase_date="2026-08-08"),
            {
                "purchase_uuid": PURCHASE_UUID,
                "expected_purchase_date": "2026-08-07",
                "warranty_type": WARRANTY_ONE_YEAR,
                "warranty_until": "2027-08-07",
            },
            "purchase_changed",
        ),
    ],
)
async def test_quick_create_purchase_warranty_failures_are_atomic(
    hass: HomeAssistant,
    purchase: PurchaseData | None,
    request_updates: dict[str, Any],
    code: str,
) -> None:
    """Missing, unconfigured, undated, and stale Purchases consume no identity."""
    manager = _manager(hass)
    if purchase is not None:
        manager._data["purchases"][PURCHASE_UUID] = purchase
    before = deepcopy(manager._data)

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_quick_create_asset(_request(**request_updates))

    assert raised.value.code == code
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()


async def test_quick_create_deployment_installed_date_and_area(
    hass: HomeAssistant,
) -> None:
    """Explicit Deployment metadata uses the selected valid Area only."""
    manager = _manager(hass)
    area = ar.async_get(hass).async_create("Kitchen")

    result = await manager.async_quick_create_asset(
        _request(
            deployment_state=DEPLOYMENT_STATE_DEPLOYED,
            installed_date="2026-07-01",
            ha_area_id=area.id,
        )
    )

    assert result.asset["deployment_state"] == DEPLOYMENT_STATE_DEPLOYED
    assert result.asset["installed_date"] == "2026-07-01"
    assert result.asset["ha_area_id"] == area.id
    assert result.asset["field_sources"]["deployment_state"] == "user"
    assert result.asset["field_sources"]["installed_date"] == "user"
    assert result.asset["field_sources"]["ha_area_id"] == "user"


@pytest.mark.parametrize(
    "quick_request",
    [
        _request(
            deployment_state=DEPLOYMENT_STATE_NOT_DEPLOYED,
            ha_area_id="missing-area",
        ),
        _request(
            deployment_state=DEPLOYMENT_STATE_DEPLOYED,
            ha_area_id="missing-area",
        ),
    ],
)
async def test_quick_create_invalid_area_is_atomic(
    hass: HomeAssistant,
    quick_request: QuickAssetCreateRequest,
) -> None:
    """Invalid Area combinations never allocate an Asset identity."""
    manager = _manager(hass)
    before = deepcopy(manager._data)

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_quick_create_asset(quick_request)

    assert raised.value.code == "invalid_area"
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()


async def test_quick_replacement_applies_requested_side_effects_once(
    hass: HomeAssistant,
) -> None:
    """Replacement, retirement, undeploy, and Area clearing share one timestamp."""
    manager = _manager(hass)
    area = ar.async_get(hass).async_create("Kitchen")
    predecessor, request = await _predecessor_request(manager, area_id=area.id)
    manager._store.async_save.reset_mock()

    result = await manager.async_quick_create_asset(request)

    assert result.predecessor_lifecycle_changed is True
    assert result.predecessor_deployment_changed is True
    assert result.predecessor_area_cleared is True
    assert result.predecessor["lifecycle"]["status"] == LIFECYCLE_STATUS_RETIRED
    assert result.predecessor["deployment_state"] == DEPLOYMENT_STATE_NOT_DEPLOYED
    assert result.predecessor["ha_area_id"] is None
    assert result.predecessor["installed_date"] == predecessor["installed_date"]
    assert result.replacement["reason"] == "failure"
    assert result.replacement["effective_date"] == "2026-08-01"
    assert result.replacement["notes"] == "Physical replacement"
    predecessor_event = manager.lifecycle_event(
        result.predecessor["lifecycle"]["current_event_uuid"]
    )
    new_event = manager.lifecycle_event(result.asset["lifecycle"]["current_event_uuid"])
    assert predecessor_event["effective_date"] == result.replacement["effective_date"]
    assert predecessor_event["notes"] is None
    assert {
        predecessor_event["recorded_at"],
        new_event["recorded_at"],
        result.replacement["recorded_at"],
    } == {result.replacement["recorded_at"]}
    assert len(manager.replacement_records_for_asset(QUICK_UUID)) == 1
    manager._store.async_save.assert_awaited_once()


@pytest.mark.parametrize(
    ("status", "changed", "final_status"),
    [
        (LIFECYCLE_STATUS_ACTIVE, True, LIFECYCLE_STATUS_RETIRED),
        (LIFECYCLE_STATUS_UNKNOWN, True, LIFECYCLE_STATUS_RETIRED),
        (LIFECYCLE_STATUS_RETIRED, False, LIFECYCLE_STATUS_RETIRED),
        (LIFECYCLE_STATUS_DISPOSED, False, LIFECYCLE_STATUS_DISPOSED),
        (LIFECYCLE_STATUS_LOST, False, LIFECYCLE_STATUS_LOST),
    ],
)
async def test_quick_replacement_predecessor_lifecycle_rules(
    hass: HomeAssistant,
    status: str,
    changed: bool,
    final_status: str,
) -> None:
    """Disposed/lost are never resurrected while active/unknown can retire."""
    manager = _manager(hass)
    predecessor, request = await _predecessor_request(
        manager,
        lifecycle_status=status,
        deployment_state=DEPLOYMENT_STATE_NOT_DEPLOYED,
        undeploy=False,
    )
    before_events = len(
        manager.lifecycle_events_for_asset(predecessor["asset_uuid"])
    )

    result = await manager.async_quick_create_asset(request)

    assert result.predecessor_lifecycle_changed is changed
    assert result.predecessor["lifecycle"]["status"] == final_status
    assert len(manager.lifecycle_events_for_asset(predecessor["asset_uuid"])) == (
        before_events + int(changed)
    )


@pytest.mark.parametrize(
    ("state", "changed"),
    [
        (DEPLOYMENT_STATE_DEPLOYED, True),
        (DEPLOYMENT_STATE_UNKNOWN, True),
        (DEPLOYMENT_STATE_NOT_DEPLOYED, False),
    ],
)
async def test_quick_replacement_predecessor_deployment_rules(
    hass: HomeAssistant,
    state: str,
    changed: bool,
) -> None:
    """Only deployed/unknown predecessors transition to not deployed."""
    manager = _manager(hass)
    _predecessor, request = await _predecessor_request(
        manager,
        lifecycle_status=LIFECYCLE_STATUS_RETIRED,
        deployment_state=state,
        retire=False,
    )

    result = await manager.async_quick_create_asset(request)

    assert result.predecessor_deployment_changed is changed
    assert result.predecessor["deployment_state"] == DEPLOYMENT_STATE_NOT_DEPLOYED


@pytest.mark.parametrize(
    "changed_field",
    ["lifecycle_status", "lifecycle_event", "deployment", "area"],
)
async def test_quick_replacement_rejects_stale_predecessor_snapshot(
    hass: HomeAssistant,
    changed_field: str,
) -> None:
    """Status, event pointer, Deployment, and Area are all TOCTOU guards."""
    manager = _manager(hass)
    area = ar.async_get(hass).async_create("Kitchen")
    predecessor, request = await _predecessor_request(manager, area_id=area.id)
    if changed_field == "lifecycle_status":
        request = replace(
            request,
            expected_predecessor_lifecycle_status=LIFECYCLE_STATUS_UNKNOWN,
        )
    elif changed_field == "lifecycle_event":
        request = replace(request, expected_predecessor_current_event_uuid=None)
    elif changed_field == "deployment":
        request = replace(
            request,
            expected_predecessor_deployment_state=DEPLOYMENT_STATE_UNKNOWN,
        )
    else:
        request = replace(request, expected_predecessor_ha_area_id=None)
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_quick_create_asset(request)

    assert raised.value.code == "predecessor_changed"
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()
    assert manager.asset(predecessor["asset_uuid"]) == predecessor


async def test_quick_replacement_detects_same_status_new_event_pointer(
    hass: HomeAssistant,
) -> None:
    """Active -> lost -> active after review is detected by event UUID."""
    manager = _manager(hass)
    predecessor, request = await _predecessor_request(manager)
    await manager.async_set_asset_lifecycle(
        predecessor["asset_uuid"],
        LIFECYCLE_STATUS_LOST,
        effective_date=None,
        notes=None,
    )
    await manager.async_set_asset_lifecycle(
        predecessor["asset_uuid"],
        LIFECYCLE_STATUS_ACTIVE,
        effective_date=None,
        notes=None,
    )
    manager._store.async_save.reset_mock()

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_quick_create_asset(request)

    assert raised.value.code == "predecessor_changed"
    manager._store.async_save.assert_not_awaited()


async def test_quick_create_graph_conflict_is_fully_atomic(
    hass: HomeAssistant,
) -> None:
    """An existing outgoing edge prevents every part of Quick Create."""
    manager = _manager(hass)
    predecessor, request = await _predecessor_request(manager)
    other = await manager.async_create_manual_asset(name="Other successor")
    await manager.async_create_asset_replacement(
        predecessor["asset_uuid"],
        other["asset_uuid"],
        reason="upgrade",
        effective_date=None,
        notes=None,
    )
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_quick_create_asset(request)

    assert raised.value.code == "replacement_predecessor_conflict"
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()


async def test_quick_create_idempotent_replay_allocates_nothing(
    hass: HomeAssistant,
) -> None:
    """Repeating an identical successful command returns the same canonical result."""
    manager = _manager(hass)
    manager._data["purchases"][PURCHASE_UUID] = _purchase()
    request = _request(purchase_uuid=PURCHASE_UUID)

    first = await manager.async_quick_create_asset(request)
    after_first = deepcopy(manager._data)
    manager._store.async_save.reset_mock()
    replay = await manager.async_quick_create_asset(request)

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.asset == first.asset
    assert manager._data == after_first
    assert manager._data["next_asset_number"] == 2
    assert len(manager.lifecycle_events_for_asset(QUICK_UUID)) == 1
    assert manager._data["purchases"][PURCHASE_UUID]["asset_uuids"] == [QUICK_UUID]
    manager._store.async_save.assert_not_awaited()


async def test_quick_replacement_idempotent_replay_creates_no_history(
    hass: HomeAssistant,
) -> None:
    """Replacement replay compares requested final state, not original predecessor state."""
    manager = _manager(hass)
    _predecessor, request = await _predecessor_request(manager)
    first = await manager.async_quick_create_asset(request)
    after_first = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    replay = await manager.async_quick_create_asset(request)

    assert replay.replayed is True
    assert replay.replacement == first.replacement
    assert replay.predecessor == first.predecessor
    assert manager._data == after_first
    assert len(manager.replacement_records_for_asset(QUICK_UUID)) == 1
    manager._store.async_save.assert_not_awaited()


async def test_quick_create_same_uuid_different_result_conflicts(
    hass: HomeAssistant,
) -> None:
    """A proposed UUID cannot be reused for a different canonical command."""
    manager = _manager(hass)
    await manager.async_quick_create_asset(_request())
    manager._store.async_save.reset_mock()

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_quick_create_asset(
            _request(metadata=_metadata(name="Different"))
        )

    assert raised.value.code == "quick_create_idempotency_conflict"
    manager._store.async_save.assert_not_awaited()


async def test_quick_create_known_persistence_failure_mutates_nothing(
    hass: HomeAssistant,
) -> None:
    """Known failure preserves every canonical collection and the DL allocator."""
    manager = _manager(hass)
    before = deepcopy(manager._data)
    manager._store.async_save = AsyncMock(side_effect=OSError("write failed"))

    with pytest.raises(AssetStorePersistenceError) as raised:
        await manager.async_quick_create_asset(_request())

    assert raised.value.code == "persistence_error"
    assert manager._data == before
    assert manager._data["next_asset_number"] == 1


async def test_quick_create_ambiguous_persisted_write_recovers_as_replay(
    hass: HomeAssistant,
) -> None:
    """A persisted write with failed acknowledgement is recovered without rewriting."""
    manager = _manager(hass)
    persisted: AssetStoreData | None = None

    async def _ambiguous_save(data: AssetStoreData) -> None:
        nonlocal persisted
        persisted = deepcopy(data)
        raise AssetStorePersistenceError("readback failed", ambiguous=True)

    manager._store.async_save = AsyncMock(side_effect=_ambiguous_save)
    manager._store.async_load_persisted_snapshot = AsyncMock(
        side_effect=lambda: deepcopy(persisted)
    )

    result = await manager.async_quick_create_asset(_request())

    assert result.replayed is True
    assert result.asset["asset_uuid"] == QUICK_UUID
    assert manager.asset(QUICK_UUID) == result.asset
    assert manager._persistence_uncertain is False
    manager._store.async_save.assert_awaited_once()
    manager._store.async_load_persisted_snapshot.assert_awaited_once()


async def test_quick_replacement_ambiguous_write_replays_canonical_notes(
    hass: HomeAssistant,
) -> None:
    """Canonical replacement notes survive an ambiguous write and exact replay."""
    manager = _manager(hass)
    _predecessor, request = await _predecessor_request(manager)
    persisted: AssetStoreData | None = None

    async def _ambiguous_save(data: AssetStoreData) -> None:
        nonlocal persisted
        persisted = deepcopy(data)
        raise AssetStorePersistenceError("readback failed", ambiguous=True)

    manager._store.async_save = AsyncMock(side_effect=_ambiguous_save)
    manager._store.async_load_persisted_snapshot = AsyncMock(
        side_effect=lambda: deepcopy(persisted)
    )

    result = await manager.async_quick_create_asset(request)

    assert result.replayed is True
    assert result.replacement is not None
    assert result.replacement["notes"] == request.replacement_notes
    assert manager.asset(QUICK_UUID) == result.asset
    assert manager._data["next_asset_number"] == 3
    manager._store.async_save.assert_awaited_once()
    manager._store.async_load_persisted_snapshot.assert_awaited_once()


async def test_quick_create_ambiguous_nonpersisted_write_allows_same_uuid_retry(
    hass: HomeAssistant,
) -> None:
    """A confirmed absent write creates no phantom and retry safely uses the same UUID."""
    manager = _manager(hass)
    manager._store.async_save = AsyncMock(
        side_effect=AssetStorePersistenceError("unknown", ambiguous=True)
    )
    manager._store.async_load_persisted_snapshot = AsyncMock(
        return_value=deepcopy(manager._data)
    )

    with pytest.raises(AssetStorePersistenceError) as raised:
        await manager.async_quick_create_asset(_request())

    assert raised.value.code == "persistence_error"
    assert manager.asset(QUICK_UUID) is None
    assert manager._data["next_asset_number"] == 1
    assert manager._persistence_uncertain is False

    manager._store.async_save = AsyncMock()
    result = await manager.async_quick_create_asset(_request())
    assert result.asset["asset_uuid"] == QUICK_UUID
    assert result.asset["asset_id"] == "DL0001"


async def test_quick_create_ambiguous_unreadable_outcome_remains_uncertain(
    hass: HomeAssistant,
) -> None:
    """An unreadable outcome publishes nothing and forces recovery before retry."""
    manager = _manager(hass)
    before = deepcopy(manager._data)
    manager._store.async_save = AsyncMock(
        side_effect=AssetStorePersistenceError("unknown", ambiguous=True)
    )
    manager._store.async_load_persisted_snapshot = AsyncMock(
        side_effect=AssetStorePersistenceError("still unreadable", ambiguous=True)
    )

    with pytest.raises(AssetStorePersistenceError) as raised:
        await manager.async_quick_create_asset(_request())

    assert raised.value.code == "persistence_error"
    assert manager._data == before
    assert manager._persistence_uncertain is True


def _invalid_request_cases() -> list[tuple[QuickAssetCreateRequest, str]]:
    """Return malformed commands covering every Quick Create trust boundary."""
    predecessor_fields = {
        "predecessor_asset_uuid": SECOND_QUICK_UUID,
        "expected_predecessor_lifecycle_status": LIFECYCLE_STATUS_ACTIVE,
        "expected_predecessor_current_event_uuid": None,
        "expected_predecessor_deployment_state": DEPLOYMENT_STATE_DEPLOYED,
        "expected_predecessor_ha_area_id": None,
        "replacement_reason": "failure",
        "replacement_effective_date": None,
        "replacement_notes": None,
    }
    return [
        (replace(_request(), asset_uuid="bad"), "invalid_quick_create_request"),
        (
            replace(_request(), metadata={"name": "Incomplete"}),
            "invalid_quick_create_request",
        ),
        (
            replace(_request(), metadata=_metadata(model=123)),  # type: ignore[arg-type]
            "invalid_quick_create_request",
        ),
        (
            replace(_request(), metadata=_metadata(name=" ")),
            "invalid_quick_create_request",
        ),
        (
            replace(_request(), metadata=_metadata(name=" Padded ")),
            "invalid_quick_create_request",
        ),
        (
            replace(_request(), metadata=_metadata(model=""), field_sources={"name": "user"}),
            "invalid_quick_create_request",
        ),
        (
            replace(_request(), field_sources={"unknown": "user"}),
            "invalid_quick_create_request",
        ),
        (
            replace(_request(), field_sources={"name": "purchase"}),
            "invalid_quick_create_request",
        ),
        (
            replace(
                _request(),
                metadata=_metadata(category="Tool"),
                field_sources={"name": "user", "category": "home_assistant"},
                primary_device_id="ha-device",
            ),
            "invalid_quick_create_request",
        ),
        (
            replace(
                _request(),
                metadata=_metadata(model="Model"),
                field_sources={"name": "user"},
            ),
            "invalid_quick_create_request",
        ),
        (
            replace(_request(), primary_device_id=" "),
            "invalid_quick_create_request",
        ),
        (
            replace(
                _request(),
                field_sources={"name": "home_assistant"},
            ),
            "invalid_quick_create_request",
        ),
        (replace(_request(), initial_lifecycle_status="bad"), "invalid_lifecycle_status"),
        (
            replace(_request(), initial_lifecycle_effective_date="bad"),
            "invalid_lifecycle_effective_date",
        ),
        (
            replace(_request(), initial_lifecycle_effective_date="2999-01-01"),
            "lifecycle_date_in_future",
        ),
        (replace(_request(), deployment_state="bad"), "invalid_deployment_state"),
        (replace(_request(), installed_date=7), "invalid_installed_date"),  # type: ignore[arg-type]
        (replace(_request(), installed_date="bad"), "invalid_installed_date"),
        (replace(_request(), installed_date="2026-8-1"), "invalid_installed_date"),
        (replace(_request(), ha_area_id=7), "invalid_area"),  # type: ignore[arg-type]
        (replace(_request(), warranty_type="bad"), "invalid_warranty_type"),
        (
            replace(_request(), warranty_until="2028-01-01"),
            "invalid_warranty_date",
        ),
        (
            replace(_request(), warranty_type=WARRANTY_MANUAL),
            "manual_warranty_date_required",
        ),
        (
            replace(
                _request(),
                warranty_type=WARRANTY_MANUAL,
                warranty_until="bad",
            ),
            "invalid_warranty_date",
        ),
        (
            replace(_request(), warranty_type=WARRANTY_ONE_YEAR),
            "purchase_date_required_for_warranty",
        ),
        (replace(_request(), purchase_uuid="bad"), "invalid_purchase"),
        (
            replace(_request(), expected_purchase_date=7),  # type: ignore[arg-type]
            "invalid_quick_create_request",
        ),
        (
            replace(_request(), retire_predecessor=1),  # type: ignore[arg-type]
            "invalid_quick_create_request",
        ),
        (
            replace(_request(), replacement_reason="failure"),
            "invalid_quick_create_request",
        ),
        (
            replace(
                _request(),
                **(predecessor_fields | {"predecessor_asset_uuid": "bad"}),
            ),
            "asset_missing",
        ),
        (
            replace(
                _request(),
                **(predecessor_fields | {"predecessor_asset_uuid": QUICK_UUID}),
            ),
            "replacement_self_reference",
        ),
        (
            replace(
                _request(),
                **(
                    predecessor_fields
                    | {"expected_predecessor_lifecycle_status": "bad"}
                ),
            ),
            "invalid_quick_create_request",
        ),
        (
            replace(
                _request(),
                **(
                    predecessor_fields
                    | {"expected_predecessor_current_event_uuid": "bad"}
                ),
            ),
            "invalid_quick_create_request",
        ),
        (
            replace(
                _request(),
                **(
                    predecessor_fields
                    | {"expected_predecessor_deployment_state": "bad"}
                ),
            ),
            "invalid_quick_create_request",
        ),
        (
            replace(
                _request(),
                **(
                    predecessor_fields
                    | {"expected_predecessor_ha_area_id": ""}
                ),
            ),
            "invalid_quick_create_request",
        ),
        (
            replace(
                _request(),
                **(predecessor_fields | {"replacement_reason": "bad"}),
            ),
            "invalid_replacement_reason",
        ),
        (
            replace(
                _request(),
                **(
                    predecessor_fields
                    | {"replacement_effective_date": "bad"}
                ),
            ),
            "invalid_replacement_effective_date",
        ),
        (
            replace(
                _request(),
                **(
                    predecessor_fields
                    | {"replacement_effective_date": "2999-01-01"}
                ),
            ),
            "replacement_date_in_future",
        ),
        (
            replace(
                _request(),
                **(predecessor_fields | {"replacement_notes": 7}),
            ),  # type: ignore[arg-type]
            "invalid_quick_create_request",
        ),
    ]


@pytest.mark.parametrize(("quick_request", "code"), _invalid_request_cases())
async def test_quick_create_rejects_malformed_commands_without_mutation(
    hass: HomeAssistant,
    quick_request: QuickAssetCreateRequest,
    code: str,
) -> None:
    """The immutable manager command validates every untrusted field first."""
    manager = _manager(hass)
    before = deepcopy(manager._data)

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_quick_create_asset(quick_request)

    assert raised.value.code == code
    assert manager._data == before
    manager._store.async_save.assert_not_awaited()


@pytest.mark.parametrize("replacement_notes", ["", 7])
async def test_quick_create_rejects_noncanonical_replacement_notes_before_save(
    hass: HomeAssistant,
    replacement_notes: object,
) -> None:
    """The immutable command rejects notes that cannot replay byte-for-byte."""
    manager = _manager(hass)
    predecessor, request = await _predecessor_request(manager)
    request = replace(
        request,
        replacement_notes=replacement_notes,  # type: ignore[arg-type]
    )
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_quick_create_asset(request)

    assert raised.value.code == "invalid_quick_create_request"
    assert manager._data == before
    assert manager._data["next_asset_number"] == 2
    assert manager.asset(predecessor["asset_uuid"]) == predecessor
    assert manager.asset(QUICK_UUID) is None
    manager._store.async_save.assert_not_awaited()


async def test_quick_create_rejects_non_command_object(
    hass: HomeAssistant,
) -> None:
    """The public manager API rejects arbitrary caller objects safely."""
    manager = _manager(hass)

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_quick_create_asset({})  # type: ignore[arg-type]

    assert raised.value.code == "invalid_quick_create_request"
    manager._store.async_save.assert_not_awaited()


async def test_quick_replacement_rejects_date_before_predecessor_lifecycle(
    hass: HomeAssistant,
) -> None:
    """An earlier retirement date fails before Asset allocation or Store save."""
    manager = _manager(hass)
    predecessor = await manager.async_create_manual_asset(
        name="Dated predecessor",
        initial_lifecycle_status=LIFECYCLE_STATUS_UNKNOWN,
    )
    predecessor = await manager.async_set_asset_lifecycle(
        predecessor["asset_uuid"],
        LIFECYCLE_STATUS_ACTIVE,
        effective_date="2026-08-05",
        notes=None,
    )
    request = _request(
        deployment_state=DEPLOYMENT_STATE_DEPLOYED,
        predecessor_asset_uuid=predecessor["asset_uuid"],
        expected_predecessor_lifecycle_status=LIFECYCLE_STATUS_ACTIVE,
        expected_predecessor_current_event_uuid=predecessor["lifecycle"][
            "current_event_uuid"
        ],
        expected_predecessor_deployment_state=predecessor["deployment_state"],
        expected_predecessor_ha_area_id=predecessor["ha_area_id"],
        replacement_reason="failure",
        replacement_effective_date="2026-08-04",
        replacement_notes=None,
        retire_predecessor=True,
        undeploy_predecessor=False,
    )
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_quick_create_asset(request)

    assert raised.value.code == "replacement_date_before_predecessor_lifecycle"
    assert manager._data == before
    assert manager._data["next_asset_number"] == 2
    assert manager.asset(QUICK_UUID) is None
    manager._store.async_save.assert_not_awaited()


@pytest.mark.parametrize("replacement_date", ["2026-08-05", "2026-08-06", None])
async def test_quick_replacement_allows_non_decreasing_or_unknown_retirement_date(
    hass: HomeAssistant,
    replacement_date: str | None,
) -> None:
    """Equal, later, and unknown dates retain the existing lifecycle semantics."""
    manager = _manager(hass)
    predecessor = await manager.async_create_manual_asset(
        name="Dated predecessor",
        initial_lifecycle_status=LIFECYCLE_STATUS_UNKNOWN,
    )
    predecessor = await manager.async_set_asset_lifecycle(
        predecessor["asset_uuid"],
        LIFECYCLE_STATUS_ACTIVE,
        effective_date="2026-08-05",
        notes=None,
    )
    manager._store.async_save.reset_mock()

    result = await manager.async_quick_create_asset(
        _request(
            deployment_state=DEPLOYMENT_STATE_DEPLOYED,
            predecessor_asset_uuid=predecessor["asset_uuid"],
            expected_predecessor_lifecycle_status=LIFECYCLE_STATUS_ACTIVE,
            expected_predecessor_current_event_uuid=predecessor["lifecycle"][
                "current_event_uuid"
            ],
            expected_predecessor_deployment_state=predecessor["deployment_state"],
            expected_predecessor_ha_area_id=predecessor["ha_area_id"],
            replacement_reason="failure",
            replacement_effective_date=replacement_date,
            replacement_notes=None,
            retire_predecessor=True,
            undeploy_predecessor=False,
        )
    )

    assert result.predecessor_lifecycle_changed is True
    assert result.predecessor["lifecycle"]["status"] == LIFECYCLE_STATUS_RETIRED
    assert result.replacement["effective_date"] == replacement_date
    manager._store.async_save.assert_awaited_once()


def test_home_assistant_metadata_has_stable_missing_device_fallback() -> None:
    """Quick Add and reconciliation share the same absent-device interpretation."""
    assert home_assistant_asset_metadata(None, "fallback") == {
        "name": "fallback",
        "manufacturer": None,
        "model": None,
        "model_id": None,
        "serial_number": None,
        "sw_version": None,
        "hw_version": None,
    }


@pytest.mark.parametrize(
    "corruption",
    [
        "unknown_has_history",
        "initial_event_missing",
        "initial_event_changed",
        "purchase_membership",
        "purchase_review_date",
        "calculated_warranty",
        "unexpected_replacement",
        "replacement_history_count",
        "replacement_record",
        "predecessor_missing",
        "predecessor_lifecycle",
        "predecessor_event",
        "predecessor_event_pointer",
        "predecessor_deployment",
        "transaction_timestamp",
    ],
)
async def test_quick_replay_rejects_inconsistent_persisted_outcomes(
    hass: HomeAssistant,
    corruption: str,
) -> None:
    """Idempotent replay fails closed for every composite-result boundary."""
    manager = _manager(hass)
    request = _request()

    if corruption == "unknown_has_history":
        request = _request(initial_lifecycle_status=LIFECYCLE_STATUS_UNKNOWN)
        await manager.async_quick_create_asset(request)
        foreign = await manager.async_create_manual_asset(name="Foreign")
        foreign_event = manager.lifecycle_events_for_asset(foreign["asset_uuid"])[0]
        manager._data["lifecycle_events"][foreign_event["event_uuid"]][
            "asset_uuid"
        ] = QUICK_UUID
    elif corruption in {"initial_event_missing", "initial_event_changed"}:
        result = await manager.async_quick_create_asset(request)
        event_uuid = result.asset["lifecycle"]["current_event_uuid"]
        if corruption == "initial_event_missing":
            manager._data["lifecycle_events"].pop(event_uuid)
        else:
            manager._data["lifecycle_events"][event_uuid]["effective_date"] = (
                "2026-08-01"
            )
    elif corruption in {
        "purchase_membership",
        "purchase_review_date",
        "calculated_warranty",
    }:
        manager._data["purchases"][PURCHASE_UUID] = _purchase()
        request = _request(
            purchase_uuid=PURCHASE_UUID,
            expected_purchase_date="2026-08-07",
            warranty_type=WARRANTY_TWO_YEARS,
            warranty_until="2028-08-07",
        )
        await manager.async_quick_create_asset(request)
        if corruption == "purchase_membership":
            manager._data["purchases"][PURCHASE_UUID]["asset_uuids"] = []
        elif corruption == "purchase_review_date":
            manager._data["purchases"][PURCHASE_UUID]["purchase_date"] = (
                "2026-08-08"
            )
        else:
            manager._data["purchases"][PURCHASE_UUID]["purchase_date"] = (
                "2026-08-08"
            )
            request = replace(request, expected_purchase_date="2026-08-08")
    elif corruption == "unexpected_replacement":
        await manager.async_quick_create_asset(request)
        other = await manager.async_create_manual_asset(name="Other")
        await manager.async_create_asset_replacement(
            QUICK_UUID,
            other["asset_uuid"],
            reason="upgrade",
            effective_date=None,
            notes=None,
        )
    else:
        lifecycle_status = (
            LIFECYCLE_STATUS_RETIRED
            if corruption == "predecessor_event_pointer"
            else LIFECYCLE_STATUS_ACTIVE
        )
        _predecessor, request = await _predecessor_request(
            manager,
            lifecycle_status=lifecycle_status,
            deployment_state=DEPLOYMENT_STATE_DEPLOYED,
        )
        result = await manager.async_quick_create_asset(request)
        replacement = result.replacement
        predecessor = result.predecessor
        assert replacement is not None
        assert predecessor is not None
        if corruption == "replacement_history_count":
            duplicate = deepcopy(replacement)
            duplicate_uuid = "77777777-7777-4777-8777-777777777777"
            duplicate["replacement_uuid"] = duplicate_uuid
            manager._data["replacement_records"][duplicate_uuid] = duplicate
        elif corruption == "replacement_record":
            manager._data["replacement_records"][replacement["replacement_uuid"]][
                "reason"
            ] = "upgrade"
        elif corruption == "predecessor_missing":
            manager._data["assets"].pop(predecessor["asset_uuid"])
        elif corruption == "predecessor_lifecycle":
            manager._data["assets"][predecessor["asset_uuid"]]["lifecycle"][
                "status"
            ] = LIFECYCLE_STATUS_LOST
        elif corruption == "predecessor_event":
            event_uuid = predecessor["lifecycle"]["current_event_uuid"]
            manager._data["lifecycle_events"].pop(event_uuid)
        elif corruption == "predecessor_event_pointer":
            manager._data["assets"][predecessor["asset_uuid"]]["lifecycle"][
                "current_event_uuid"
            ] = None
        elif corruption == "predecessor_deployment":
            manager._data["assets"][predecessor["asset_uuid"]][
                "deployment_state"
            ] = DEPLOYMENT_STATE_DEPLOYED
        else:
            manager._data["replacement_records"][replacement["replacement_uuid"]][
                "recorded_at"
            ] = "2026-08-10T10:00:00+00:00"

    manager._store.async_save.reset_mock()
    with pytest.raises(AssetStoreError) as raised:
        await manager.async_quick_create_asset(request)

    assert raised.value.code == "quick_create_idempotency_conflict"
    manager._store.async_save.assert_not_awaited()


def test_quick_replay_requires_existing_asset(hass: HomeAssistant) -> None:
    """Recovery verification cannot fabricate a replay result without its Asset."""
    manager = _manager(hass)

    with pytest.raises(AssetStoreError) as raised:
        manager._quick_replay_result(manager._data, _request())

    assert raised.value.code == "persistence_error"


async def test_quick_create_rejects_compact_noncanonical_date(
    hass: HomeAssistant,
) -> None:
    """Calendar dates that parse but are not YYYY-MM-DD remain invalid."""
    manager = _manager(hass)

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_quick_create_asset(
            _request(installed_date="20260807")
        )

    assert raised.value.code == "invalid_installed_date"


@pytest.mark.parametrize(
    ("purchase_date", "warranty_until"),
    [
        ("bad", "2027-08-07"),
        ("2026-08-07", "2027-08-08"),
    ],
)
async def test_quick_create_rejects_unreviewable_calculated_warranty(
    hass: HomeAssistant,
    purchase_date: str,
    warranty_until: str,
) -> None:
    """Malformed Purchase dates and mismatched reviewed results fail atomically."""
    manager = _manager(hass)
    manager._data["purchases"][PURCHASE_UUID] = _purchase(
        purchase_date=purchase_date
    )

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_quick_create_asset(
            _request(
                purchase_uuid=PURCHASE_UUID,
                expected_purchase_date=purchase_date,
                warranty_type=WARRANTY_ONE_YEAR,
                warranty_until=warranty_until,
            )
        )

    assert raised.value.code == "invalid_warranty_date"
    manager._store.async_save.assert_not_awaited()


async def test_quick_create_manager_rechecks_primary_ownership(
    hass: HomeAssistant,
) -> None:
    """The atomic Store transaction remains authoritative for primary uniqueness."""
    manager = _manager(hass)
    owner = await manager.async_create_manual_asset(name="Owner")
    await manager.async_link_asset_device(owner["asset_uuid"], "shared-device")
    manager._store.async_save.reset_mock()

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_quick_create_asset(
            _request(
                primary_device_id="shared-device",
                field_sources={"name": "home_assistant"},
                deployment_state=DEPLOYMENT_STATE_DEPLOYED,
            )
        )

    assert raised.value.code == "device_already_linked"
    manager._store.async_save.assert_not_awaited()


async def test_quick_create_known_store_persistence_error_is_not_retried(
    hass: HomeAssistant,
) -> None:
    """A known failed write does not trigger ambiguous recovery or a second write."""
    manager = _manager(hass)
    manager._store.async_save = AsyncMock(
        side_effect=AssetStorePersistenceError("known", ambiguous=False)
    )
    manager._store.async_load_persisted_snapshot = AsyncMock()

    with pytest.raises(AssetStorePersistenceError):
        await manager.async_quick_create_asset(_request())

    manager._store.async_save.assert_awaited_once()
    manager._store.async_load_persisted_snapshot.assert_not_awaited()


@pytest.mark.parametrize("has_existing_data", [False, True])
async def test_quick_create_ambiguous_missing_store_envelope_fails_closed(
    hass: HomeAssistant,
    has_existing_data: bool,
) -> None:
    """A missing recovery envelope never publishes the detached Quick snapshot."""
    manager = _manager(hass)
    if has_existing_data:
        await manager.async_create_manual_asset(name="Existing")
    before = deepcopy(manager._data)
    manager._store.async_save = AsyncMock(
        side_effect=AssetStorePersistenceError("unknown", ambiguous=True)
    )
    manager._store.async_load_persisted_snapshot = AsyncMock(return_value=None)

    with pytest.raises(AssetStorePersistenceError) as raised:
        await manager.async_quick_create_asset(_request())

    assert raised.value.code == "persistence_error"
    assert manager._data == before
    assert manager.asset(QUICK_UUID) is None
