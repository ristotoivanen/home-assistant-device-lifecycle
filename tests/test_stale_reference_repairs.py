"""Home Assistant Repairs reports stale external device references.

A Home Assistant device an Asset still names may leave the Device Registry
at any time; 0.7.5 keeps that reference for the person to repair. These tests
pin how it is reported: one advisory, non-fixable Repairs issue per stored
Asset relationship that no longer resolves, derived from canonical Asset data
only, kept in step with the Device Registry, and never a reason to write the
Store, a config subentry or any registry.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from itertools import permutations
from types import SimpleNamespace
from typing import Any

import attr
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from custom_components.device_lifecycle.const import (
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
    CONF_HA_RELATIONSHIP_ACTION,
    DOMAIN,
    HA_RELATIONSHIP_ACTION_REPLACE,
)
from custom_components.device_lifecycle.exposure import relationships_unique_id
from custom_components.device_lifecycle.migration import _device_can_be_linked
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.stale_references import (
    StoredDeviceReference,
    collect_asset_device_references,
    desired_stale_reference_issues,
    device_reference_resolves,
    is_owned_stale_reference_issue,
    parse_stale_reference_issue_id,
    stale_reference_issue_id,
)

from .conftest import ASSET_UUID
from .test_exposure_options_reload import (
    _purchase_edit_input,
    _verified_store_readback,
)
from .test_ha_relationship_options_flow import _external_device
from .test_stale_device_registry_references import (
    SECOND_ASSET_UUID,
    _device,
    _entities,
    _load,
    _open_primary_device,
    _purchase,
    _recorded_saves,
    _refs,
    _reload,
    _runtime,
    _state,
    _stored,
    _with_second_asset,
)

pytestmark = pytest.mark.real_reload


# --- Reading the issue registry ----------------------------------------------


def _owned(hass: HomeAssistant) -> dict[str, ir.IssueEntry]:
    """Return every stale-reference issue this feature owns."""
    return {
        issue_id: issue
        for (domain, issue_id), issue in ir.async_get(hass).issues.items()
        if is_owned_stale_reference_issue(domain, issue_id)
    }


def _issue_id(asset_uuid: str, role: str, device_id: str) -> str:
    return stale_reference_issue_id(asset_uuid, role, device_id)  # type: ignore[arg-type]


@contextmanager
def _issue_events(hass: HomeAssistant) -> Iterator[list[dict[str, Any]]]:
    """Record every issue registry announcement while the block runs."""
    events: list[dict[str, Any]] = []

    def _record(event: Event) -> None:
        events.append(dict(event.data))

    unsubscribe = hass.bus.async_listen(
        ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED, _record
    )
    try:
        yield events
    finally:
        unsubscribe()


def _subentries(entry) -> dict[str, dict[str, Any]]:
    return {key: deepcopy(dict(item.data)) for key, item in entry.subentries.items()}


def _asset_uuids(entry) -> list[str]:
    return sorted(asset["asset_uuid"] for asset in entry.runtime_data.assets())


def _snapshot(hass: HomeAssistant, hass_storage: dict, entry) -> dict[str, Any]:
    """Everything the issue reconciliation must never change."""
    return {
        "store": _stored(hass_storage),
        "subentries": _subentries(entry),
        "asset_uuids": _asset_uuids(entry),
        "entities": _entities(hass, entry),
    }


def _missing_from_relationships(hass: HomeAssistant) -> set[tuple[str, str]]:
    """Return (role, device ID) pairs the Relationships entity calls missing."""
    attributes = _state(hass, relationships_unique_id(ASSET_UUID)).attributes
    missing: set[tuple[str, str]] = set()
    if attributes["primary_state"] == "missing":
        missing.add(("primary", attributes["primary_device_id"]))
    missing.update(
        ("related", related["device_id"])
        for related in attributes["related_devices"]
        if related["state"] == "missing"
    )
    return missing


# --- Collecting, resolving and naming: no Home Assistant needed --------------


def _asset(asset_uuid: str, asset_id: str, *refs: tuple[str, str]) -> dict:
    return {
        "asset_uuid": asset_uuid,
        "asset_id": asset_id,
        "name": f"Asset {asset_id}",
        "ha_device_refs": [
            {"device_id": device_id, "role": role} for role, device_id in refs
        ],
    }


A_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
B_UUID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def test_the_collector_orders_references_the_same_way_every_time() -> None:
    """Asset, then primary before related, then device ID, whatever the input."""
    assets = [
        _asset(B_UUID, "DL0002", ("related", "zeta"), ("primary", "omega")),
        _asset(A_UUID, "DL0001", ("related", "beta"), ("related", "alpha")),
        _asset(A_UUID.replace("a", "c"), "DL0003"),
    ]
    expected = (
        StoredDeviceReference(A_UUID, "DL0001", "Asset DL0001", "related", "alpha"),
        StoredDeviceReference(A_UUID, "DL0001", "Asset DL0001", "related", "beta"),
        StoredDeviceReference(B_UUID, "DL0002", "Asset DL0002", "primary", "omega"),
        StoredDeviceReference(B_UUID, "DL0002", "Asset DL0002", "related", "zeta"),
    )
    for order in permutations(assets):
        assert collect_asset_device_references(order) == expected


def test_the_issue_id_is_a_fixed_hash_of_the_stored_device_id() -> None:
    """The same relationship always has the same ID; the raw ID is not in it."""
    issue_id = _issue_id(A_UUID, "primary", "gone-device")
    # A literal, so a change of hash, encoding or length cannot pass unseen.
    assert issue_id == (
        "stale_device_primary_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa_"
        "46286c1d6b68c49b0561c127a592b5e5"
    )
    assert issue_id == _issue_id(A_UUID, "primary", "gone-device")
    assert "gone-device" not in issue_id
    # Arbitrary stored strings still land inside the same closed grammar.
    odd = _issue_id(A_UUID, "related", "Weird ID / with spaces ä")
    assert parse_stale_reference_issue_id(odd) is not None


def test_role_is_part_of_the_issue_identity() -> None:
    primary = _issue_id(A_UUID, "primary", "device")
    related = _issue_id(A_UUID, "related", "device")
    assert primary != related
    assert parse_stale_reference_issue_id(primary).role == "primary"
    assert parse_stale_reference_issue_id(related).role == "related"
    parsed = parse_stale_reference_issue_id(related)
    assert parsed.asset_uuid == A_UUID
    assert parsed.device_digest == hashlib.sha256(b"device").hexdigest()[:32]


@pytest.mark.parametrize(
    "issue_id",
    [
        "",
        "stale_device",
        "some_other_issue",
        f"stale_device_owner_{A_UUID}_{'0' * 32}",
        f"stale_device_primary_{A_UUID}_{'0' * 31}",
        f"stale_device_primary_{A_UUID}_{'0' * 33}",
        f"stale_device_primary_{A_UUID}_{'A' * 32}",
        f"stale_device_primary_{A_UUID.upper()}_{'0' * 32}",
        f"stale_device_primary_{A_UUID.replace('-', '')}_{'0' * 32}",
        f"xstale_device_primary_{A_UUID}_{'0' * 32}",
        f"stale_device_primary_{A_UUID}_{'0' * 32}_extra",
        f"stale_device_primary_{A_UUID}_{'0' * 32}\n",
    ],
)
def test_only_the_exact_grammar_is_owned(issue_id: str) -> None:
    assert parse_stale_reference_issue_id(issue_id) is None
    assert not is_owned_stale_reference_issue(DOMAIN, issue_id)


def test_ownership_needs_both_the_domain_and_the_grammar() -> None:
    issue_id = _issue_id(A_UUID, "primary", "device")
    assert is_owned_stale_reference_issue(DOMAIN, issue_id)
    assert not is_owned_stale_reference_issue("hue", issue_id)


def test_an_issue_id_is_never_built_outside_the_grammar() -> None:
    with pytest.raises(ValueError):
        _issue_id(A_UUID.upper(), "primary", "device")
    with pytest.raises(ValueError):
        _issue_id(A_UUID, "owner", "device")


def test_resolution_asks_the_registry_exactly_what_the_ui_asks() -> None:
    """Plain `async_get`, no keyword: composites and children resolve."""
    asked: list[tuple[str, dict[str, Any]]] = []

    def _async_get(device_id: str, **kwargs: Any) -> object | None:
        asked.append((device_id, kwargs))
        return object() if device_id == "kept" else None

    registry = SimpleNamespace(async_get=_async_get)
    assert device_reference_resolves(registry, "kept")
    assert not device_reference_resolves(registry, "gone")
    assert asked == [("kept", {}), ("gone", {})]


def test_desired_issues_name_the_asset_and_never_the_device() -> None:
    registry = SimpleNamespace(
        async_get=lambda device_id: object() if device_id == "kept" else None
    )
    references = collect_asset_device_references(
        [_asset(A_UUID, "DL0001", ("primary", "gone"), ("related", "kept"))]
    )
    desired = desired_stale_reference_issues(references, registry)
    issue_id = _issue_id(A_UUID, "primary", "gone")
    assert list(desired) == [issue_id]
    issue = desired[issue_id]
    assert issue.translation_key == "stale_primary_device"
    assert issue.translation_placeholders == {
        "asset_id": "DL0001",
        "asset_name": "Asset DL0001",
    }


# --- One issue per stored Asset relationship ----------------------------------


def _assert_advisory(issue: ir.IssueEntry, translation_key: str) -> None:
    assert issue.domain == DOMAIN
    assert issue.is_fixable is False
    assert issue.is_persistent is False
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == translation_key
    assert issue.issue_domain is None
    assert issue.data is None
    assert issue.learn_more_url is None


async def test_a_stale_primary_device_is_one_advisory_issue(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    data = deepcopy(asset_store_data)
    device = _device(hass, device_registry, "gone-primary", data["assets"][ASSET_UUID])
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(device.id)
    entry = await _load(hass, hass_storage, data)
    assert _owned(hass) == {}
    before = _snapshot(hass, hass_storage, entry)

    device_registry.async_remove_device(device.id)
    await hass.async_block_till_done()
    with _recorded_saves() as saves:
        await _reload(hass, hass_storage, entry)

    issue_id = _issue_id(ASSET_UUID, "primary", device.id)
    issues = _owned(hass)
    assert list(issues) == [issue_id]
    _assert_advisory(issues[issue_id], "stale_primary_device")
    assert issues[issue_id].translation_placeholders == {
        "asset_id": "DL0007",
        "asset_name": "Workshop device",
    }
    assert all(
        device.id not in value
        for value in issues[issue_id].translation_placeholders.values()
    )
    assert saves == []
    assert _snapshot(hass, hass_storage, entry) == before
    assert _missing_from_relationships(hass) == {("primary", device.id)}


async def test_a_stale_related_device_is_its_own_issue(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    data = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    primary = _device(hass, device_registry, "kept-primary", asset)
    _owner, kept = _external_device(hass, device_registry, key="kept-related")
    _owner, gone = _external_device(hass, device_registry, key="gone-related")
    asset["ha_device_refs"] = _refs(primary.id, kept.id, gone.id)
    entry = await _load(hass, hass_storage, data)
    before = _snapshot(hass, hass_storage, entry)

    with _recorded_saves() as saves:
        device_registry.async_remove_device(gone.id)
        await hass.async_block_till_done()

    issue_id = _issue_id(ASSET_UUID, "related", gone.id)
    issues = _owned(hass)
    assert list(issues) == [issue_id]
    _assert_advisory(issues[issue_id], "stale_related_device")
    assert saves == []
    assert _snapshot(hass, hass_storage, entry) == before
    assert _missing_from_relationships(hass) == {("related", gone.id)}


@pytest.mark.parametrize("configuration", ["purchase", "runtime", "both"])
async def test_purchase_and_runtime_listings_add_no_issue_of_their_own(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    runtime_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
    configuration: str,
) -> None:
    """A device a Purchase and Runtime also list is still one primary issue."""
    data = deepcopy(asset_store_data)
    device = _device(hass, device_registry, "gone-listed", data["assets"][ASSET_UUID])
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(device.id)
    subentries = {
        "purchase": (_purchase(purchase_subentry_data, device.id),),
        "runtime": (_runtime(runtime_subentry_data, device.id),),
        "both": (
            _purchase(purchase_subentry_data, device.id),
            _runtime(runtime_subentry_data, device.id),
        ),
    }[configuration]
    entry = await _load(hass, hass_storage, data, *subentries)
    before = _snapshot(hass, hass_storage, entry)

    device_registry.async_remove_device(device.id)
    await hass.async_block_till_done()
    with _recorded_saves() as saves:
        await _reload(hass, hass_storage, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert list(_owned(hass)) == [_issue_id(ASSET_UUID, "primary", device.id)]
    assert saves == []
    assert _snapshot(hass, hass_storage, entry) == before
    listed = [
        str(value)
        for item in entry.subentries.values()
        for value in (
            item.data.get(CONF_DEVICE_IDS, []) or [item.data.get(CONF_DEVICE_ID)]
        )
    ]
    assert listed and set(listed) == {device.id}


async def test_one_device_in_two_asset_relationships_is_two_issues(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Primary of one Asset, related of another: each is repaired on its own."""
    data = _with_second_asset(deepcopy(asset_store_data))
    shared = _device(hass, device_registry, "shared", data["assets"][ASSET_UUID])
    _owner, second_primary = _external_device(
        hass, device_registry, key="second-primary"
    )
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(shared.id)
    data["assets"][SECOND_ASSET_UUID]["ha_device_refs"] = _refs(
        second_primary.id, shared.id
    )
    entry = await _load(hass, hass_storage, data)

    device_registry.async_remove_device(shared.id)
    await hass.async_block_till_done()

    primary_issue = _issue_id(ASSET_UUID, "primary", shared.id)
    related_issue = _issue_id(SECOND_ASSET_UUID, "related", shared.id)
    assert sorted(_owned(hass)) == sorted([primary_issue, related_issue])
    assert _owned(hass)[related_issue].translation_placeholders == {
        "asset_id": "DL0008",
        "asset_name": "Garage heater",
    }

    # Removing the related relationship through the existing UI clears only
    # that Asset's issue.
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow_id = flow["flow_id"]
    for user_input in (
        {"next_step_id": "manage_asset"},
        {"asset_uuid": SECOND_ASSET_UUID},
        {"next_step_id": "ha_relationship"},
        {"next_step_id": "remove_related_device"},
    ):
        result = await hass.config_entries.options.async_configure(
            flow_id, user_input
        )
    assert result["step_id"] == "remove_related_device"
    with _verified_store_readback(hass_storage):
        result = await hass.config_entries.options.async_configure(
            flow_id, {CONF_DEVICE_ID: shared.id}
        )
        await hass.async_block_till_done()
    assert result["step_id"] == "ha_relationship"
    hass.config_entries.options.async_abort(flow_id)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.asset(SECOND_ASSET_UUID)["ha_device_refs"] == _refs(
        second_primary.id
    )
    assert list(_owned(hass)) == [primary_issue]


# --- Lifecycle ----------------------------------------------------------------


async def test_repeated_setup_changes_nothing_in_the_issue_registry(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    """Reloads, unload + setup and a kept dismissal: no churn at all."""
    data = deepcopy(asset_store_data)
    device = _device(hass, device_registry, "gone-repeat", data["assets"][ASSET_UUID])
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(device.id)
    entry = await _load(
        hass, hass_storage, data, _purchase(purchase_subentry_data, device.id)
    )
    device_registry.async_remove_device(device.id)
    await hass.async_block_till_done()
    issue_id = _issue_id(ASSET_UUID, "primary", device.id)
    first = _owned(hass)[issue_id]
    ir.async_ignore_issue(hass, DOMAIN, issue_id, True)
    ignored = _owned(hass)[issue_id]
    assert ignored.dismissed_version is not None
    before = _snapshot(hass, hass_storage, entry)

    with _issue_events(hass) as events, _recorded_saves() as saves:
        for _round in range(3):
            await _reload(hass, hass_storage, entry)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        await _reload(hass, hass_storage, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert events == []
    assert saves == []
    assert _owned(hass) == {issue_id: ignored}
    assert _owned(hass)[issue_id].created == first.created
    assert _snapshot(hass, hass_storage, entry) == before


async def test_a_device_removed_while_loaded_is_reported_without_a_restart(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    data = deepcopy(asset_store_data)
    device = _device(hass, device_registry, "gone-live", data["assets"][ASSET_UUID])
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(device.id)
    entry = await _load(hass, hass_storage, data)
    before = _snapshot(hass, hass_storage, entry)

    with _recorded_saves() as saves:
        device_registry.async_remove_device(device.id)
        await hass.async_block_till_done()

    assert list(_owned(hass)) == [_issue_id(ASSET_UUID, "primary", device.id)]
    assert saves == []
    assert _snapshot(hass, hass_storage, entry) == before


async def test_the_same_device_returning_clears_its_issue(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Home Assistant restores a removed device under its old ID.

    Its integration re-adds it with its device information, as integrations
    do. (On 2026.8 a restore that carries no device information at all
    announces nothing; the issue then clears on the next setup.)
    """
    data = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    owner, device = _external_device(
        hass,
        device_registry,
        key="returning",
        name=asset["name"],
        manufacturer=asset["manufacturer"],
        model=asset["model"],
        model_id=asset["model_id"],
        serial_number=asset["serial_number"],
        sw_version=asset["sw_version"],
        hw_version=asset["hw_version"],
    )
    asset["ha_device_refs"] = _refs(device.id)
    entry = await _load(hass, hass_storage, data)
    device_registry.async_remove_device(device.id)
    await hass.async_block_till_done()
    assert list(_owned(hass)) == [_issue_id(ASSET_UUID, "primary", device.id)]
    before = _snapshot(hass, hass_storage, entry)

    with _recorded_saves() as saves:
        returned = device_registry.async_get_or_create(
            config_entry_id=owner.entry_id,
            identifiers={("hue", "returning")},
            connections={("test_connection", "returning")},
            name=asset["name"],
            manufacturer=asset["manufacturer"],
            model=asset["model"],
        )
        await hass.async_block_till_done()

    assert returned.id == device.id
    assert _owned(hass) == {}
    assert saves == []
    assert _snapshot(hass, hass_storage, entry) == before


async def test_a_silent_restore_is_picked_up_by_the_next_setup(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Whatever the registry announces, setup derives the set from scratch."""
    data = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    owner, device = _external_device(hass, device_registry, key="silent")
    asset["ha_device_refs"] = _refs(device.id)
    entry = await _load(hass, hass_storage, data)
    device_registry.async_remove_device(device.id)
    await hass.async_block_till_done()
    assert list(_owned(hass)) == [_issue_id(ASSET_UUID, "primary", device.id)]

    returned = device_registry.async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("hue", "silent")},
        connections={("test_connection", "silent")},
    )
    await hass.async_block_till_done()
    assert returned.id == device.id
    await _reload(hass, hass_storage, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert _owned(hass) == {}


async def test_the_explicit_repair_clears_the_issue_only_when_it_is_done(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    purchase_subentry_data: dict[str, Any],
    device_registry: dr.DeviceRegistry,
) -> None:
    """Deselecting from the Purchase is not enough; replacing the primary is."""
    data = deepcopy(asset_store_data)
    gone = _device(hass, device_registry, "repair-gone", data["assets"][ASSET_UUID])
    _owner, new = _external_device(
        hass, device_registry, key="repair-new", name="New workshop device"
    )
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(gone.id)
    entry = await _load(
        hass, hass_storage, data, _purchase(purchase_subentry_data, gone.id)
    )
    device_registry.async_remove_device(gone.id)
    await hass.async_block_till_done()
    issue_id = _issue_id(ASSET_UUID, "primary", gone.id)
    assert list(_owned(hass)) == [issue_id]

    purchase = next(iter(entry.subentries.values()))
    edit = _purchase_edit_input(
        purchase_subentry_data, device_id=gone.id, installed_date="2026-01-20"
    )
    flow = await entry.start_subentry_reconfigure_flow(hass, purchase.subentry_id)
    with _verified_store_readback(hass_storage):
        done = await hass.config_entries.subentries.async_configure(
            flow["flow_id"], {**edit, CONF_DEVICE_IDS: []}
        )
        await hass.async_block_till_done()
    assert done["reason"] == "reconfigure_successful"
    assert entry.state is ConfigEntryState.LOADED
    assert list(_owned(hass)) == [issue_id]

    flow_id = await _open_primary_device(hass, entry)
    with _verified_store_readback(hass_storage):
        saved = await hass.config_entries.options.async_configure(
            flow_id,
            {
                CONF_HA_RELATIONSHIP_ACTION: HA_RELATIONSHIP_ACTION_REPLACE,
                CONF_DEVICE_ID: new.id,
            },
        )
        await hass.async_block_till_done()
    assert saved["step_id"] == "ha_relationship"
    hass.config_entries.options.async_abort(flow_id)
    assert entry.runtime_data.asset(ASSET_UUID)["ha_device_refs"] == _refs(new.id)
    assert _owned(hass) == {}


def _foreign_issues(hass: HomeAssistant) -> dict[tuple[str, str], str]:
    """Create issues that are not this feature's, however alike they look."""
    lookalike = _issue_id(ASSET_UUID, "primary", "not-a-stored-device")
    foreign = {
        ("hue", lookalike): "hue_issue",
        (DOMAIN, "some_other_issue"): "other_issue",
        (DOMAIN, lookalike + "_extra"): "other_issue",
    }
    for (domain, issue_id), key in foreign.items():
        ir.async_create_issue(
            hass,
            domain,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=key,
        )
    return foreign


async def test_issues_it_does_not_own_are_never_touched(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    data = deepcopy(asset_store_data)
    device = _device(hass, device_registry, "kept", data["assets"][ASSET_UUID])
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(device.id)
    foreign = _foreign_issues(hass)
    before = {key: ir.async_get(hass).issues[key] for key in foreign}

    entry = await _load(hass, hass_storage, data)
    await _reload(hass, hass_storage, entry)
    _owner, other = _external_device(hass, device_registry, key="unrelated")
    device_registry.async_remove_device(other.id)
    await hass.async_block_till_done()

    assert _owned(hass) == {}
    assert {key: ir.async_get(hass).issues[key] for key in foreign} == before


async def test_unloading_keeps_the_issues_and_removal_clears_only_its_own(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    data = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    gone = _device(hass, device_registry, "gone-remove", asset)
    _owner, related = _external_device(hass, device_registry, key="gone-related")
    asset["ha_device_refs"] = _refs(gone.id, related.id)
    entry = await _load(hass, hass_storage, data)
    device_registry.async_remove_device(gone.id)
    device_registry.async_remove_device(related.id)
    await hass.async_block_till_done()
    foreign = _foreign_issues(hass)
    owned = _owned(hass)
    assert sorted(owned) == sorted(
        [
            _issue_id(ASSET_UUID, "primary", gone.id),
            _issue_id(ASSET_UUID, "related", related.id),
        ]
    )

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert _owned(hass) == owned

    assert (await hass.config_entries.async_remove(entry.entry_id))[
        "require_restart"
    ] is False
    await hass.async_block_till_done()
    assert _owned(hass) == {}
    assert set(foreign) <= set(ir.async_get(hass).issues)


# --- Registry models ----------------------------------------------------------


def _device_container(registry: dr.DeviceRegistry) -> Any:
    """Return the registry's device container (2026.8 and 2026.9+ differ)."""
    return registry._devices if hasattr(registry, "_devices") else registry.devices


async def test_a_composite_id_resolves_while_a_split_device_remains(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Same answer as the Relationships entity, not the entity-linking test."""
    data = deepcopy(asset_store_data)
    split = _device(hass, device_registry, "split", data["assets"][ASSET_UUID])
    composite_id = "0123456789abcdef0123456789abcdef"
    _device_container(device_registry)[split.id] = attr.evolve(
        split, composite_device_id=composite_id
    )
    assert device_registry.async_get(composite_id) is not None
    assert not _device_can_be_linked(device_registry, composite_id)
    data["assets"][ASSET_UUID]["ha_device_refs"] = _refs(composite_id)
    entry = await _load(hass, hass_storage, data)

    assert _owned(hass) == {}
    assert _state(hass, relationships_unique_id(ASSET_UUID)).state == "present"
    before = _snapshot(hass, hass_storage, entry)

    device_registry.async_remove_device(split.id)
    await hass.async_block_till_done()

    assert device_registry.async_get(composite_id) is None
    assert list(_owned(hass)) == [_issue_id(ASSET_UUID, "primary", composite_id)]
    assert _snapshot(hass, hass_storage, entry) == before


async def test_a_child_device_resolves_where_home_assistant_has_them(
    hass: HomeAssistant,
    hass_storage: dict,
    asset_store_data: AssetStoreData,
    device_registry: dr.DeviceRegistry,
) -> None:
    if not hasattr(device_registry, "async_get_or_create_child"):
        pytest.skip("Home Assistant 2026.8 has no child devices")
    data = deepcopy(asset_store_data)
    asset = data["assets"][ASSET_UUID]
    parent = _device(hass, device_registry, "parent", asset)
    owner = hass.config_entries.async_get_entry(parent.config_entry_id)
    child = device_registry.async_get_or_create_child(
        config_entry_id=owner.entry_id,
        identifiers={("hue", "child")},
        name="Child device",
        parent_device_id=parent.id,
    )
    asset["ha_device_refs"] = _refs(parent.id, child.id)
    await _load(hass, hass_storage, data)
    assert _owned(hass) == {}

    device_registry.async_remove_device(child.id)
    await hass.async_block_till_done()

    assert list(_owned(hass)) == [_issue_id(ASSET_UUID, "related", child.id)]
