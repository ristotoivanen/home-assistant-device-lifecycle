"""Identity versus current management of archived Assets (WP10).

IDENTITY / RECONCILIATION LOOKUP sees every Asset; CURRENT-MANAGEMENT
CANDIDATES are active Assets only. Archive is not deletion: an archived
Asset keeps its UUID, Asset ID, Home Assistant device relationships, Runtime
identity, and history. What it loses is current management, which the Store
refuses inside the locked mutation, whatever a selector showed earlier.

No production path archives an Asset yet, so these tests write the Archive
state straight into a valid Store 4.1 payload.
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.device_lifecycle import storage
from custom_components.device_lifecycle.config_flow import NO_REPLACEMENT_SELECTION
from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_CONFIRM_QUICK_ADD,
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
    CONF_INSTALLED_DATE,
    CONF_PURCHASE_NAME,
    CONF_REPLACEMENT_REASON,
    CONF_REPLACEMENT_TARGET_ASSET_UUID,
    CONF_RETIRE_PREDECESSOR,
    CONF_UNDEPLOY_PREDECESSOR,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DOMAIN,
    LIFECYCLE_STATUS_ACTIVE,
    LIFECYCLE_STATUS_RETIRED,
    SUBENTRY_TYPE_PURCHASE,
    WARRANTY_NONE,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.runtime_identity import (
    resolve_runtime_subentry_asset,
)
from custom_components.device_lifecycle.stale_references import (
    async_sync_stale_reference_issues,
    is_owned_stale_reference_issue,
)
from custom_components.device_lifecycle.storage import (
    STORAGE_KEY,
    AssetStoreError,
    AssetStoreManager,
    DeviceLifecycleStore,
    QuickAssetCreateRequest,
    _validate_store_data,
)

from .conftest import ASSET_UUID, DEVICE_ID, PURCHASE_SUBENTRY_ID, PURCHASE_UUID
from .test_lifecycle import _manager
from .test_quick_add_options_flow import _details, _external_device, _flow

ARCHIVED_AT = "2026-09-26T12:34:56.123456+00:00"
RELATED_DEVICE_ID = "related-ha-device-id"
QUICK_UUID = "77777777-7777-4777-8777-777777777777"
PACKAGE = Path(storage.__file__).parent


# --- Fixture data ------------------------------------------------------------


@dataclass(frozen=True)
class Ids:
    """The Assets and the Replacement of one prepared Store."""

    archived: str
    second: str
    third: str
    replacement: str


def _archive(data: dict[str, Any], asset_uuid: str) -> None:
    """Write an Archive state, as the later Archive mutation will."""
    asset = data["assets"][asset_uuid]
    asset["deployment_state"] = DEPLOYMENT_STATE_NOT_DEPLOYED
    asset["ha_area_id"] = None
    asset["archived_at"] = ARCHIVED_AT


def _restore(data: dict[str, Any], asset_uuid: str) -> None:
    data["assets"][asset_uuid]["archived_at"] = None


async def _prepared(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> tuple[dict[str, Any], Ids]:
    """A valid active Store: three Assets, Runtime, a related device, and an
    active Replacement second -> third. Nothing is archived yet."""
    manager = _manager(hass, asset_store_data)
    await manager.async_initialize_new_runtime(ASSET_UUID)
    await manager.async_add_related_device(ASSET_UUID, RELATED_DEVICE_ID)
    second = await manager.async_create_manual_asset(name="Second device")
    third = await manager.async_create_manual_asset(name="Third device")
    replacement = await manager.async_create_asset_replacement(
        second["asset_uuid"],
        third["asset_uuid"],
        reason="upgrade",
        effective_date=None,
        notes=None,
    )
    data = deepcopy(manager._data)
    return data, Ids(
        archived=ASSET_UUID,
        second=second["asset_uuid"],
        third=third["asset_uuid"],
        replacement=replacement["replacement_uuid"],
    )


def _envelope(data: Any) -> dict[str, Any]:
    return {
        "version": 4,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": deepcopy(data),
    }


def _bytes(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))


@contextmanager
def _readback(hass_storage: dict[str, Any]) -> Iterator[None]:
    with patch.object(
        storage.json_util,
        "load_json",
        side_effect=lambda _path: deepcopy(hass_storage[STORAGE_KEY]),
    ):
        yield


async def _persisted_manager(
    hass: HomeAssistant, hass_storage: dict[str, Any], data: dict[str, Any]
) -> AssetStoreManager:
    """A manager set up from a persisted Store 4.1 file."""
    _validate_store_data(data)
    hass_storage[STORAGE_KEY] = _envelope(data)
    manager = AssetStoreManager(hass)
    with _readback(hass_storage):
        await manager.async_setup()
    return manager


# --- Identity versus management helpers --------------------------------------


async def test_filtering_helpers_split_by_archive_state_only(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data, ids = await _prepared(hass, asset_store_data)
    _archive(data, ids.archived)
    manager = _manager(hass, data)

    every = {asset["asset_uuid"] for asset in manager.assets()}
    active = {asset["asset_uuid"] for asset in manager.active_assets()}
    archived = {asset["asset_uuid"] for asset in manager.archived_assets()}

    assert every == {ids.archived, ids.second, ids.third}
    assert active == {ids.second, ids.third}
    assert archived == {ids.archived}
    # Detached snapshots, like every other read.
    manager.active_assets()[0]["name"] = "mutated"
    manager.archived_assets()[0]["name"] = "mutated"
    assert manager._data == data


async def test_archived_asset_keeps_its_identity(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data, ids = await _prepared(hass, asset_store_data)
    _archive(data, ids.archived)
    manager = _manager(hass, data)

    archived = manager.asset(ids.archived)
    assert archived is not None
    assert archived["archived_at"] == ARCHIVED_AT
    assert archived["asset_id"] == "DL0007"
    assert ids.archived in {asset["asset_uuid"] for asset in manager.assets()}
    owner = manager.asset_for_primary_device_id(DEVICE_ID)
    assert owner is not None
    assert owner["asset_uuid"] == ids.archived
    assert manager.runtime_total_seconds(ids.archived) == Decimal(0)

    # The canonical Runtime resolver is unchanged: it sees the archived owner.
    for hint in ({CONF_ASSET_UUID: ids.archived}, {}):
        subentry_data = {CONF_DEVICE_ID: DEVICE_ID, **hint}
        assert (
            resolve_runtime_subentry_asset(manager._data["assets"], subentry_data)
            == ids.archived
        )


async def test_an_archived_primary_device_is_never_free(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data, ids = await _prepared(hass, asset_store_data)
    _archive(data, ids.archived)
    manager = _manager(hass, data)
    snapshot = deepcopy(manager._data)

    with pytest.raises(AssetStoreError, match="already linked to another Asset"):
        await manager.async_link_asset_device(ids.second, DEVICE_ID)
    assert manager._data == snapshot
    manager._store.async_save.assert_not_awaited()


# --- Guarded mutation matrix --------------------------------------------------


Operation = Callable[[AssetStoreManager, Ids], Awaitable[Any]]

GUARDED_OPERATIONS: dict[str, Operation] = {
    "update_metadata_reporting": lambda m, i: m.async_update_asset_metadata_reporting(
        i.archived, name="Renamed"
    ),
    "update_metadata": lambda m, i: m.async_update_asset_metadata(
        i.archived, notes="New notes"
    ),
    "set_purchase_reporting": lambda m, i: m.async_set_asset_purchase_reporting(
        i.archived, None
    ),
    "set_purchase": lambda m, i: m.async_set_asset_purchase(i.archived, None),
    "set_deployment_reporting": lambda m, i: m.async_set_asset_deployment_reporting(
        i.archived, deployment_state=DEPLOYMENT_STATE_DEPLOYED
    ),
    "set_deployment": lambda m, i: m.async_set_asset_deployment(
        i.archived, installed_date="2026-02-01"
    ),
    "link_device_reporting": lambda m, i: m.async_link_asset_device_reporting(
        i.archived, "another-ha-device-id", replace=True
    ),
    "link_device": lambda m, i: m.async_link_asset_device(i.archived, DEVICE_ID),
    "unlink_device_reporting": lambda m, i: m.async_unlink_asset_device_reporting(
        i.archived
    ),
    "unlink_device": lambda m, i: m.async_unlink_asset_device(i.archived),
    "add_related_device_reporting": lambda m, i: m.async_add_related_device_reporting(
        i.archived, "another-related-id"
    ),
    "add_related_device": lambda m, i: m.async_add_related_device(
        i.archived, RELATED_DEVICE_ID
    ),
    "remove_related_device": lambda m, i: m.async_remove_related_device(
        i.archived, RELATED_DEVICE_ID
    ),
    "set_lifecycle_reporting": lambda m, i: m.async_set_asset_lifecycle_reporting(
        i.archived, LIFECYCLE_STATUS_RETIRED, effective_date=None, notes=None
    ),
    "set_lifecycle": lambda m, i: m.async_set_asset_lifecycle(
        i.archived, LIFECYCLE_STATUS_ACTIVE, effective_date=None, notes=None
    ),
    "initialize_new_runtime": lambda m, i: m.async_initialize_new_runtime(i.archived),
    "import_legacy_runtime": lambda m, i: m.async_import_legacy_runtime(
        i.archived, Decimal(100)
    ),
    "commit_runtime_delta": lambda m, i: m.async_commit_runtime_delta(
        i.archived, expected_total=Decimal(0), delta=Decimal(5)
    ),
    "create_replacement_archived_predecessor": lambda m, i: (
        m.async_create_asset_replacement(
            i.archived, i.second, reason="upgrade", effective_date=None, notes=None
        )
    ),
    "create_replacement_archived_successor": lambda m, i: (
        m.async_create_asset_replacement(
            i.third, i.archived, reason="upgrade", effective_date=None, notes=None
        )
    ),
    "correct_replacement_to_archived_predecessor": lambda m, i: (
        m.async_correct_asset_replacement(
            i.replacement,
            predecessor_asset_uuid=i.archived,
            successor_asset_uuid=i.third,
            reason="failure",
            effective_date=None,
            notes=None,
            void_reason="Wrong predecessor",
        )
    ),
    "correct_replacement_to_archived_successor": lambda m, i: (
        m.async_correct_asset_replacement(
            i.replacement,
            predecessor_asset_uuid=i.second,
            successor_asset_uuid=i.archived,
            reason="failure",
            effective_date=None,
            notes=None,
            void_reason="Wrong successor",
        )
    ),
}


@contextmanager
def _save_spy() -> Iterator[Any]:
    with patch.object(
        DeviceLifecycleStore,
        "async_save",
        autospec=True,
        side_effect=DeviceLifecycleStore.async_save,
    ) as save:
        yield save


@pytest.mark.parametrize("operation", GUARDED_OPERATIONS)
async def test_current_management_of_an_archived_asset_is_refused(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    asset_store_data: AssetStoreData,
    operation: str,
) -> None:
    """Refused inside the Store: nothing saved, persisted, or published."""
    data, ids = await _prepared(hass, asset_store_data)
    _archive(data, ids.archived)
    manager = await _persisted_manager(hass, hass_storage, data)
    persisted = _bytes(hass_storage[STORAGE_KEY])
    published = deepcopy(manager._data)

    with (
        _readback(hass_storage),
        _save_spy() as save,
        pytest.raises(AssetStoreError) as raised,
    ):
        await GUARDED_OPERATIONS[operation](manager, ids)

    assert raised.value.code == "asset_archived"
    save.assert_not_called()
    assert _bytes(hass_storage[STORAGE_KEY]) == persisted
    assert manager._data == published
    assert manager._persistence_uncertain is False


@pytest.mark.parametrize("operation", GUARDED_OPERATIONS)
async def test_the_guard_runs_on_the_mutation_candidate(
    hass: HomeAssistant, asset_store_data: AssetStoreData, operation: str
) -> None:
    """The same operations succeed or fail on their own merits once the
    Asset is active again, so the refusal above is the Archive guard."""
    data, ids = await _prepared(hass, asset_store_data)
    manager = _manager(hass, data)
    try:
        await GUARDED_OPERATIONS[operation](manager, ids)
    except AssetStoreError as err:
        assert err.code != "asset_archived"


async def test_a_missing_asset_still_fails_as_missing(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    manager = _manager(hass, asset_store_data)
    with pytest.raises(AssetStoreError) as raised:
        await manager.async_update_asset_metadata(
            "99999999-9999-4999-8999-999999999999", name="x"
        )
    assert raised.value.code == "asset_missing"


async def test_other_assets_remain_manageable(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data, ids = await _prepared(hass, asset_store_data)
    _archive(data, ids.archived)
    manager = _manager(hass, data)

    updated = await manager.async_update_asset_metadata(ids.second, name="Renamed")

    assert updated["name"] == "Renamed"
    manager._store.async_save.assert_awaited_once()
    assert manager.asset(ids.archived) == data["assets"][ids.archived]


# --- Replacement ---------------------------------------------------------------


@pytest.mark.parametrize("side", ["predecessor", "successor"])
async def test_correction_with_an_archived_side_voids_nothing(
    hass: HomeAssistant, asset_store_data: AssetStoreData, side: str
) -> None:
    data, ids = await _prepared(hass, asset_store_data)
    _archive(data, ids.archived)
    manager = _manager(hass, data)

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_correct_asset_replacement(
            ids.replacement,
            predecessor_asset_uuid=ids.archived
            if side == "predecessor"
            else ids.second,
            successor_asset_uuid=ids.archived if side == "successor" else ids.third,
            reason="failure",
            effective_date=None,
            notes=None,
            void_reason="Corrected",
        )

    assert raised.value.code == "asset_archived"
    record = manager.replacement_record(ids.replacement)
    assert record is not None
    assert record["voided_at"] is None
    assert record["void_reason"] is None
    assert len(manager._data["replacement_records"]) == 1
    manager._store.async_save.assert_not_awaited()


@pytest.mark.parametrize("side", ["predecessor", "successor"])
async def test_voiding_history_with_an_archived_side_is_allowed(
    hass: HomeAssistant, asset_store_data: AssetStoreData, side: str
) -> None:
    """Void is history, not current management."""
    data, ids = await _prepared(hass, asset_store_data)
    _archive(data, ids.second if side == "predecessor" else ids.third)
    manager = _manager(hass, data)

    voided = await manager.async_void_asset_replacement(
        ids.replacement, void_reason="Recorded by mistake"
    )

    assert voided["voided_at"] is not None
    assert voided["void_reason"] == "Recorded by mistake"
    manager._store.async_save.assert_awaited_once()
    _validate_store_data(manager._data)


async def test_correction_between_active_assets_still_works(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data, ids = await _prepared(hass, asset_store_data)
    manager = _manager(hass, data)

    corrected = await manager.async_correct_asset_replacement(
        ids.replacement,
        predecessor_asset_uuid=ids.third,
        successor_asset_uuid=ids.second,
        reason="failure",
        effective_date=None,
        notes=None,
        void_reason="Reversed",
    )

    assert corrected["predecessor_asset_uuid"] == ids.third
    old = manager.replacement_record(ids.replacement)
    assert old is not None
    assert old["voided_at"] is not None


def _quick_request(**fields: Any) -> QuickAssetCreateRequest:
    base: dict[str, Any] = {
        "asset_uuid": QUICK_UUID,
        "primary_device_id": None,
        "metadata": {
            "name": "Quick device",
            "category": None,
            "manufacturer": None,
            "model": None,
            "model_id": None,
            "serial_number": None,
            "sw_version": None,
            "hw_version": None,
            "notes": None,
        },
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
    base.update(fields)
    return QuickAssetCreateRequest(**base)


async def test_quick_create_with_an_archived_predecessor_is_refused(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data, ids = await _prepared(hass, asset_store_data)
    _archive(data, ids.archived)
    manager = _manager(hass, data)
    predecessor = data["assets"][ids.archived]

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_quick_create_asset(
            _quick_request(
                predecessor_asset_uuid=ids.archived,
                expected_predecessor_lifecycle_status=predecessor["lifecycle"][
                    "status"
                ],
                expected_predecessor_current_event_uuid=predecessor["lifecycle"][
                    "current_event_uuid"
                ],
                expected_predecessor_deployment_state=predecessor["deployment_state"],
                expected_predecessor_ha_area_id=None,
                replacement_reason="upgrade",
                retire_predecessor=True,
            )
        )

    assert raised.value.code == "asset_archived"
    assert manager._data == data
    manager._store.async_save.assert_not_awaited()


async def test_quick_create_for_a_device_owned_by_an_archived_asset_is_refused(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data, ids = await _prepared(hass, asset_store_data)
    _archive(data, ids.archived)
    manager = _manager(hass, data)

    with pytest.raises(AssetStoreError) as raised:
        await manager.async_quick_create_asset(
            _quick_request(primary_device_id=DEVICE_ID)
        )

    assert raised.value.code == "device_already_linked"
    assert manager._data == data
    manager._store.async_save.assert_not_awaited()


# --- Options flow: selectors, TOCTOU, and Quick Add from Home Assistant -------


async def test_selectors_offer_only_active_assets(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data, ids = await _prepared(hass, asset_store_data)
    _archive(data, ids.archived)
    flow, _entry = _flow(hass, _manager(hass, data))

    def values(options: list[Any]) -> set[str]:
        return {option["value"] for option in options}

    assert values(flow._asset_choices()) == {ids.second, ids.third}
    assert values(flow._replacement_target_choices(ids.second)) == {ids.third}
    assert values(flow._quick_replacement_choices()) == {
        NO_REPLACEMENT_SELECTION,
        ids.second,
        ids.third,
    }

    _restore(data, ids.archived)
    flow, _entry = _flow(hass, _manager(hass, data))
    assert ids.archived in values(flow._asset_choices())
    assert ids.archived in values(flow._replacement_target_choices(ids.second))
    assert ids.archived in values(flow._quick_replacement_choices())


async def test_quick_labels_stay_unambiguous_against_archived_assets(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    data, ids = await _prepared(hass, asset_store_data)
    data["assets"][ids.second]["name"] = data["assets"][ids.archived]["name"]
    _archive(data, ids.archived)
    flow, _entry = _flow(hass, _manager(hass, data))

    labels = {
        option["value"]: option["label"] for option in flow._quick_replacement_choices()
    }
    second = data["assets"][ids.second]
    assert labels[ids.second] == f"{second['name']} · {second['asset_id']}"
    assert flow._quick_asset_display_label(second) == labels[ids.second]


async def test_quick_add_commit_refuses_a_predecessor_archived_after_review(
    hass: HomeAssistant,
) -> None:
    """TOCTOU: the selector was rendered while the predecessor was active."""
    manager = _manager(hass)
    predecessor = await manager.async_create_manual_asset(name="Old device")
    flow, _entry = _flow(hass, manager)
    await flow.async_step_quick_add_manual()
    details = await flow.async_step_quick_add_details(
        _details(
            deployment_state=DEPLOYMENT_STATE_DEPLOYED,
            predecessor_uuid=predecessor["asset_uuid"],
        )
    )
    assert details["step_id"] == "quick_add_replacement", details.get("errors")
    confirm = await flow.async_step_quick_add_replacement(
        {
            CONF_REPLACEMENT_REASON: "upgrade",
            CONF_RETIRE_PREDECESSOR: True,
            CONF_UNDEPLOY_PREDECESSOR: True,
        }
    )
    assert confirm["step_id"] == "quick_add_confirm", confirm.get("errors")

    # Archived between review and commit.
    _archive(manager._data, predecessor["asset_uuid"])  # type: ignore[arg-type]
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()

    result = await flow.async_step_quick_add_confirm({CONF_CONFIRM_QUICK_ADD: True})

    assert result["errors"] == {"base": "asset_archived"}
    assert result["step_id"] == "quick_add_details"
    assert (
        flow._quick_details_input[CONF_REPLACEMENT_TARGET_ASSET_UUID]
        == NO_REPLACEMENT_SELECTION
    )
    manager._store.async_save.assert_not_awaited()
    assert manager._data == before
    assert QUICK_UUID not in manager._data["assets"]
    assert len(manager._data["assets"]) == 1
    assert manager._data["replacement_records"] == {}


async def test_quick_add_from_home_assistant_rejects_an_archived_owners_device(
    hass: HomeAssistant,
) -> None:
    manager = _manager(hass)
    device = _external_device(hass)
    owner = await manager.async_create_manual_asset(name="Owner")
    await manager.async_link_asset_device(owner["asset_uuid"], device.id)
    _archive(manager._data, owner["asset_uuid"])  # type: ignore[arg-type]
    before = deepcopy(manager._data)
    manager._store.async_save.reset_mock()
    flow, _entry = _flow(hass, manager)

    selected = await flow.async_step_quick_add_from_ha({CONF_DEVICE_ID: device.id})

    assert selected["errors"] == {"base": "device_already_linked"}
    assert manager._data == before
    assert len(manager.assets()) == 1
    manager._store.async_save.assert_not_awaited()


# --- Reconciliation stays source-authoritative --------------------------------


async def test_reconciliation_refreshes_an_archived_asset_without_restoring_it(
    hass: HomeAssistant,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
) -> None:
    owner = MockConfigEntry(domain="test", entry_id="reconcile-owner")
    owner.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("test", "archived-owner-device")},
        name="HA name",
        manufacturer="Refreshed manufacturer",
        model="Refreshed model",
        sw_version="9.9.9",
    )
    data: dict[str, Any] = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    asset["ha_device_refs"] = [{"device_id": device.id, "role": "primary"}]
    # A user-owned field is protected from the Home Assistant projection.
    asset["model"] = "User model"
    asset["field_sources"]["model"] = "user"
    _archive(data, ASSET_UUID)
    manager = _manager(hass, data)

    subentry_data = deepcopy(purchase_subentry_data)
    subentry_data[CONF_DEVICE_IDS] = [device.id]
    subentry_data[CONF_PURCHASE_NAME] = "Renamed purchase"
    subentry_data[CONF_INSTALLED_DATE] = "2026-02-02"
    entry = SimpleNamespace(
        entry_id="device-lifecycle-entry-id",
        subentries={
            PURCHASE_SUBENTRY_ID: SimpleNamespace(
                subentry_id=PURCHASE_SUBENTRY_ID,
                subentry_type=SUBENTRY_TYPE_PURCHASE,
                title="Purchase",
                data=subentry_data,
            )
        },
    )
    with patch.object(hass.config_entries, "async_update_subentry"):
        await manager.async_reconcile_entry(entry)  # type: ignore[arg-type]

    assert set(manager._data["assets"]) == {ASSET_UUID}
    reconciled = manager._data["assets"][ASSET_UUID]
    assert reconciled["archived_at"] == ARCHIVED_AT
    assert reconciled["manufacturer"] == "Refreshed manufacturer"
    assert reconciled["sw_version"] == "9.9.9"
    assert reconciled["model"] == "User model"
    assert reconciled["installed_date"] == "2026-02-02"
    assert reconciled["purchase_uuid"] == PURCHASE_UUID
    assert reconciled["ha_device_refs"][0] == {
        "device_id": device.id,
        "role": "primary",
    }
    assert manager._data["purchases"][PURCHASE_UUID]["name"] == "Renamed purchase"
    assert manager._data["purchases"][PURCHASE_UUID]["asset_uuids"] == [ASSET_UUID]
    assert manager.archived_assets()[0]["asset_uuid"] == ASSET_UUID
    manager._store.async_save.assert_awaited_once()


# --- Stale reference Repairs -------------------------------------------------


def _owned_issue_ids(hass: HomeAssistant) -> set[str]:
    return {
        issue_id
        for domain, issue_id in ir.async_get(hass).issues
        if is_owned_stale_reference_issue(domain, issue_id)
    }


async def test_stale_reference_repairs_follow_the_archive_state(
    hass: HomeAssistant, asset_store_data: AssetStoreData
) -> None:
    """DEVICE_ID is not in the Device Registry, so the primary is stale."""
    data: dict[str, Any] = deepcopy(asset_store_data)
    manager = _manager(hass, data)

    async_sync_stale_reference_issues(hass, manager)
    active_issues = _owned_issue_ids(hass)
    assert len(active_issues) == 1

    _archive(manager._data, ASSET_UUID)  # type: ignore[arg-type]
    async_sync_stale_reference_issues(hass, manager)
    assert _owned_issue_ids(hass) == set()

    _restore(manager._data, ASSET_UUID)  # type: ignore[arg-type]
    async_sync_stale_reference_issues(hass, manager)
    assert _owned_issue_ids(hass) == active_issues
    manager._store.async_save.assert_not_awaited()
    assert ir.async_get(hass).async_get_issue(DOMAIN, next(iter(active_issues)))


# --- Inventories --------------------------------------------------------------

IDENTITY = "identity/reconciliation"
MANAGEMENT = "current management"

# Every production call of the Asset collection and primary-device lookups,
# keyed by (module, scope, method), with how often it is called there and
# why. A new call site fails this test until it is classified here.
CALL_SITE_CLASSIFICATION: dict[tuple[str, str, str], tuple[int, str]] = {
    ("config_flow.py", "DeviceLifecycleOptionsFlow._asset_choices", "active_assets"): (
        1,
        MANAGEMENT,
    ),
    (
        "config_flow.py",
        "DeviceLifecycleOptionsFlow._quick_replacement_choices",
        "active_assets",
    ): (1, MANAGEMENT),
    # Label disambiguation counts every Asset name.
    (
        "config_flow.py",
        "DeviceLifecycleOptionsFlow._quick_replacement_choices",
        "assets",
    ): (1, IDENTITY),
    (
        "config_flow.py",
        "DeviceLifecycleOptionsFlow._quick_asset_display_label",
        "assets",
    ): (1, IDENTITY),
    # An archived owner's device is never free.
    (
        "config_flow.py",
        "DeviceLifecycleOptionsFlow.async_step_manage_primary_device",
        "asset_for_primary_device_id",
    ): (2, IDENTITY),
    (
        "config_flow.py",
        "DeviceLifecycleOptionsFlow.async_step_quick_add_confirm",
        "asset_for_primary_device_id",
    ): (1, IDENTITY),
    (
        "config_flow.py",
        "DeviceLifecycleOptionsFlow.async_step_quick_add_from_ha",
        "asset_for_primary_device_id",
    ): (1, IDENTITY),
    # The exposure projection and Runtime setup follow persistent identity.
    ("exposure.py", "async_reconcile_exposure_registry", "assets"): (2, IDENTITY),
    ("migration.py", "async_migrate_entity_registry", "asset_for_primary_device_id"): (
        2,
        IDENTITY,
    ),
    ("sensor.py", "async_setup_entry", "assets"): (1, IDENTITY),
    ("sensor.py", "async_setup_entry", "asset_for_primary_device_id"): (1, IDENTITY),
    # Repairs are raised for Assets under current management only.
    (
        "stale_references.py",
        "async_sync_stale_reference_issues",
        "active_assets",
    ): (1, MANAGEMENT),
}
INVENTORIED = frozenset(
    {"assets", "active_assets", "archived_assets", "asset_for_primary_device_id"}
)


def _production_trees() -> dict[str, ast.Module]:
    return {
        path.name: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(PACKAGE.glob("*.py"))
    }


def _call_sites(tree: ast.Module, file_name: str) -> Counter[tuple[str, str, str]]:
    found: Counter[tuple[str, str, str]] = Counter()

    def _visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            child_scope = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if isinstance(node, ast.ClassDef):
                    child_scope = f"{node.name}.{child.name}"
                elif scope == "<module>":
                    child_scope = child.name
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in INVENTORIED
            ):
                found[(file_name, child_scope, child.func.attr)] += 1
            _visit(child, child_scope)

    _visit(tree, "<module>")
    return found


def test_every_asset_collection_call_site_is_classified() -> None:
    found: Counter[tuple[str, str, str]] = Counter()
    for name, tree in _production_trees().items():
        found.update(_call_sites(tree, name))

    assert dict(found) == {
        site: count for site, (count, _kind) in CALL_SITE_CLASSIFICATION.items()
    }
    for (_module, _scope, method), (_count, kind) in CALL_SITE_CLASSIFICATION.items():
        assert kind in (IDENTITY, MANAGEMENT)
        # A management call never goes through the archive-blind helpers,
        # and an identity call never through the filtering ones.
        if kind == MANAGEMENT:
            assert method in ("active_assets", "archived_assets")
        else:
            assert method in ("assets", "asset_for_primary_device_id")


def test_archive_state_is_read_only_through_the_archive_authority() -> None:
    """No production module reads ``archived_at`` itself; the manager
    reads Archive state only in its filtering helpers, the guard, the
    Maintenance projection helper, and the Runtime quarantine.

    The field name as a value (a key, a subscript, a ``get`` argument) is
    the only way to read it, so its string constants are inventoried.
    """
    spelled: dict[str, int] = {}
    for name, tree in _production_trees().items():
        count = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value == "archived_at"
        )
        if count:
            spelled[name] = count
    # archive.py owns the field (ARCHIVED_AT), store_shape.py names the
    # frozen record keys, and storage.py creates a new Asset active.
    assert spelled == {"archive.py": 1, "store_shape.py": 1, "storage.py": 1}
    creation = (PACKAGE / "storage.py").read_text(encoding="utf-8")
    assert creation.count('"archived_at": None') == 1

    tree = _production_trees()["storage.py"]
    readers: dict[str, set[str]] = {}

    def _visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            child_scope = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if isinstance(node, ast.ClassDef):
                    child_scope = f"{node.name}.{child.name}"
                elif scope == "<module>":
                    child_scope = child.name
            if isinstance(child, ast.Name) and child.id == "asset_is_archived":
                readers.setdefault(child.id, set()).add(child_scope)
            _visit(child, child_scope)

    _visit(tree, "<module>")
    assert readers == {
        "asset_is_archived": {
            "AssetStoreManager.active_assets",
            "AssetStoreManager.archived_assets",
            "AssetStoreManager._require_active_asset",
            "AssetStoreManager.maintenance_projection",
            "AssetStoreManager.quarantined_runtime_subentries",
            "AssetStoreManager.register_runtime_checkpoint",
            "AssetStoreManager.asset_archived",
        }
    }


def test_the_guard_is_called_inside_every_guarded_mutator() -> None:
    tree = _production_trees()["storage.py"]
    guarded_scopes: set[str] = set()

    def _visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            child_scope = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if isinstance(node, ast.ClassDef):
                    child_scope = f"{node.name}.{child.name}"
                elif scope == "<module>":
                    child_scope = child.name
            if (
                isinstance(child, ast.Attribute)
                and child.attr == "_require_active_asset"
            ):
                guarded_scopes.add(child_scope.removeprefix("AssetStoreManager."))
            _visit(child, child_scope)

    _visit(tree, "<module>")
    assert guarded_scopes == {
        "async_update_asset_metadata_reporting",
        "async_set_asset_purchase_reporting",
        "async_set_asset_deployment_reporting",
        "async_link_asset_device_reporting",
        "async_unlink_asset_device_reporting",
        "async_add_related_device_reporting",
        "async_remove_related_device",
        "async_set_asset_lifecycle_reporting",
        "async_initialize_new_runtime",
        "async_import_legacy_runtime",
        "async_commit_runtime_delta",
        "_create_replacement_record",
        "async_correct_asset_replacement",
        "_quick_create_in_snapshot",
    }


def test_archive_and_restore_exist_only_as_the_store_manager_api() -> None:
    """Since WP13 the WP4 mutations are reached through the Store manager's
    Archive and Restore API; no other production module calls them."""
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name in {"storage.py", "archive.py"}:
            continue
        source = path.read_text(encoding="utf-8")
        for symbol in (
            "apply_archive_request",
            "ArchiveAssetRequest",
            "RestoreAssetRequest",
            "async_archive_asset",
            "async_restore_asset",
        ):
            assert symbol not in source, (path.name, symbol)
    source = (PACKAGE / "storage.py").read_text(encoding="utf-8")
    assert "async_archive_asset" in source
    assert "async_restore_asset" in source
    assert "asset_archived" in source
