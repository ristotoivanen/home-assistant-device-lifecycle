"""Exposure preflight collision and rollback quality gates."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
import logging
from types import SimpleNamespace
from unittest.mock import Mock

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_device_registry,
)

from custom_components.device_lifecycle.const import (
    CONF_ASSET_UUID,
    CONF_DEVICE_ID,
    DOMAIN,
    SUBENTRY_TYPE_RUNTIME,
)
from custom_components.device_lifecycle.exposure import (
    AssetDevicePlan,
    EntityRegistryUpdatePlan,
    ExposureMigrationPlan,
    _discover_attempt_devices,
    _primary_device_id,
    _registry_device_entries,
    _rollback_exposure_registry,
    _runtime_subentries_by_asset,
    asset_device_entry,
    asset_device_identifier,
    build_exposure_migration_plan,
)
from custom_components.device_lifecycle.migration import (
    lifecycle_unique_id,
    runtime_unique_id,
)
from custom_components.device_lifecycle.models import AssetStoreData
from custom_components.device_lifecycle.storage import AssetStoreError

from .conftest import ASSET_UUID, device_registry_entries
from .test_exposure import _entry

PresentRegistry = Callable[[dr.DeviceRegistry], dr.DeviceRegistry]


class _DeviceEntryIteration:
    """HA 2026.9-style `devices` over an HA 2026.8 device-ID mapping.

    Iteration yields DeviceEntry values; every other attribute is delegated
    to the real mapping, as Home Assistant 2026.9's own view does, so the
    registry's internal mapping use keeps working.
    """

    def __init__(self, devices: Mapping[str, dr.DeviceEntry]) -> None:
        self._devices = devices

    def __iter__(self):
        return iter(self._devices.values())

    def __len__(self) -> int:
        return len(self._devices)

    def __getattr__(self, name: str):
        return getattr(self._devices, name)


@pytest.fixture(params=["device_entries", "device_ids"])
def present_registry(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> PresentRegistry:
    """Present a registry's devices through one Home Assistant iteration model.

    - `device_entries` (HA 2026.9+): iterating `devices` yields DeviceEntry
      values.
    - `device_ids` (HA 2026.8): `devices` is a device-ID -> DeviceEntry
      mapping, so iterating it yields device IDs.

    The installed Home Assistant's real registry is used unchanged for its
    own model; only the other model is emulated. Apply it after the test has
    registered its devices, so each lookup case runs against both models on
    every supported Home Assistant version.
    """
    model = request.param

    def _present(registry: dr.DeviceRegistry) -> dr.DeviceRegistry:
        native_device_ids = isinstance(registry.devices, Mapping)
        if model == "device_ids":
            if not native_device_ids:
                monkeypatch.setattr(
                    registry,
                    "devices",
                    {
                        device.id: device
                        for device in device_registry_entries(registry)
                    },
                )
            assert all(isinstance(item, str) for item in registry.devices)
        else:
            if native_device_ids:
                monkeypatch.setattr(
                    registry,
                    "devices",
                    _DeviceEntryIteration(registry.devices),
                )
            assert all(
                isinstance(item, dr.DeviceEntry) for item in registry.devices
            )
        return registry

    return _present


def test_primary_reference_with_empty_device_id_is_not_guessed(
    asset_store_data: AssetStoreData,
) -> None:
    """An empty stored primary reference remains absent instead of becoming identity."""
    asset = deepcopy(asset_store_data["assets"][ASSET_UUID])
    asset["ha_device_refs"] = [{"device_id": "", "role": "primary"}]
    assert _primary_device_id(asset) is None


def test_runtime_subentry_preflight_rejects_missing_wrong_and_duplicate_assets(
    asset_store_data: AssetStoreData,
) -> None:
    """Runtime ownership must resolve to one Asset and its exact primary device."""
    asset = deepcopy(asset_store_data["assets"][ASSET_UUID])
    assets = {ASSET_UUID: asset}
    incomplete = SimpleNamespace(
        subentry_id="incomplete",
        subentry_type=SUBENTRY_TYPE_RUNTIME,
        data={CONF_ASSET_UUID: "", CONF_DEVICE_ID: ""},
    )
    assert _runtime_subentries_by_asset(
        SimpleNamespace(subentries={"incomplete": incomplete}), assets
    ) == {}

    missing = SimpleNamespace(
        subentry_id="missing",
        subentry_type=SUBENTRY_TYPE_RUNTIME,
        data={
            CONF_ASSET_UUID: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            CONF_DEVICE_ID: "device",
        },
    )
    with pytest.raises(AssetStoreError, match="missing Asset"):
        _runtime_subentries_by_asset(
            SimpleNamespace(subentries={"missing": missing}), assets
        )

    wrong = SimpleNamespace(
        subentry_id="wrong",
        subentry_type=SUBENTRY_TYPE_RUNTIME,
        data={CONF_ASSET_UUID: ASSET_UUID, CONF_DEVICE_ID: "wrong-device"},
    )
    with pytest.raises(AssetStoreError, match="exact primary"):
        _runtime_subentries_by_asset(
            SimpleNamespace(subentries={"wrong": wrong}), assets
        )

    first = SimpleNamespace(
        subentry_id="a",
        subentry_type=SUBENTRY_TYPE_RUNTIME,
        data={CONF_ASSET_UUID: ASSET_UUID, CONF_DEVICE_ID: "existing-ha-device-id"},
    )
    second = SimpleNamespace(
        subentry_id="b",
        subentry_type=SUBENTRY_TYPE_RUNTIME,
        data=dict(first.data),
    )
    with pytest.raises(AssetStoreError, match="multiple Runtime subentries"):
        _runtime_subentries_by_asset(
            SimpleNamespace(subentries={"a": first, "b": second}), assets
        )


def test_duplicate_asset_snapshots_fail_exposure_preflight(
    asset_store_data: AssetStoreData,
) -> None:
    """A duplicate canonical UUID is rejected before any registry mutation."""
    asset = asset_store_data["assets"][ASSET_UUID]
    with pytest.raises(AssetStoreError, match="duplicate canonical UUIDs"):
        build_exposure_migration_plan(
            entry=SimpleNamespace(entry_id="entry", subentries={}),
            assets=[asset, deepcopy(asset)],
            device_registry=SimpleNamespace(devices={}),
            entity_registry=SimpleNamespace(entities={}),
        )


async def test_foreign_asset_device_projection_fails_closed(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """The deterministic Asset identifier cannot be adopted from a subentry device."""
    entry = _entry(hass, device_id="external-device")
    subentry_id = next(iter(entry.subentries))
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=subentry_id,
        identifiers={asset_device_identifier(ASSET_UUID)},
        name="Ambiguous Asset Device",
    )

    with pytest.raises(AssetStoreError, match="externally owned"):
        asset_device_entry(
            device_registry,
            config_entry_id=entry.entry_id,
            asset_uuid=ASSET_UUID,
        )


def _lookup(
    registry: dr.DeviceRegistry,
    entry: MockConfigEntry,
    *,
    require_parent: bool = True,
) -> dr.DeviceEntry | None:
    """Resolve the canonical Asset Device for ASSET_UUID."""
    return asset_device_entry(
        registry,
        config_entry_id=entry.entry_id,
        asset_uuid=ASSET_UUID,
        require_parent=require_parent,
    )


def _foreign_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return another integration's config entry."""
    foreign = MockConfigEntry(domain="hue")
    foreign.add_to_hass(hass)
    return foreign


async def test_asset_device_lookup_without_canonical_holder_is_none(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    present_registry: PresentRegistry,
) -> None:
    """Zero exact identifier holders means no Asset Device, never a guess."""
    entry = _entry(hass)
    device_registry.async_get_or_create(
        config_entry_id=_foreign_entry(hass).entry_id,
        identifiers={("hue", ASSET_UUID)},
        name="Same UUID, other domain",
    )
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={asset_device_identifier("another-asset-uuid")},
        name="Another Asset Device",
    )

    assert _lookup(present_registry(device_registry), entry) is None


async def test_asset_device_lookup_returns_the_single_exact_holder(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    present_registry: PresentRegistry,
) -> None:
    """Exactly one exactly owned holder is the Asset Device."""
    entry = _entry(hass)
    created = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={asset_device_identifier(ASSET_UUID)},
        name="Exact Asset Device",
    )

    found = _lookup(present_registry(device_registry), entry)

    assert found is not None
    assert found.id == created.id


async def test_same_entry_duplicate_holders_fail_closed(
    hass: HomeAssistant,
    present_registry: PresentRegistry,
) -> None:
    """Two exactly owned holders are ambiguous; neither is picked."""
    entry = _entry(hass)
    identifiers = {asset_device_identifier(ASSET_UUID)}
    first = dr.DeviceEntry(
        config_entry_id=entry.entry_id,
        identifiers=identifiers,
        name="First",
    )
    second = dr.DeviceEntry(
        config_entry_id=entry.entry_id,
        identifiers=identifiers,
        name="Second",
    )
    # The public API refuses a same-entry identifier collision, but an older
    # registry store can still hold one. Pre-load that state directly.
    registry = mock_device_registry(hass, {first.id: first, second.id: second})

    with pytest.raises(AssetStoreError, match="2 Device Registry devices"):
        _lookup(present_registry(registry), entry)


@pytest.mark.parametrize("foreign_first", [True, False])
async def test_cross_entry_duplicate_holders_fail_closed(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    present_registry: PresentRegistry,
    foreign_first: bool,
) -> None:
    """Every holder is counted, so an exact owned match is never picked first.

    Another config entry can register its own device with Device Lifecycle's
    canonical identifier through the public API. Lookup must see both, in
    either registry order.
    """
    entry = _entry(hass)
    identifiers = {asset_device_identifier(ASSET_UUID)}
    owners = [entry.entry_id, _foreign_entry(hass).entry_id]
    if foreign_first:
        owners.reverse()
    for owner in owners:
        device_registry.async_get_or_create(
            config_entry_id=owner,
            identifiers=identifiers,
            name=f"Holder {owner}",
        )
    registry = present_registry(device_registry)
    assert (
        len(
            [
                device
                for device in device_registry_entries(registry)
                if device.identifiers == identifiers
            ]
        )
        == 2
    )

    with pytest.raises(AssetStoreError, match="2 Device Registry devices"):
        _lookup(registry, entry)


async def test_sole_foreign_canonical_holder_fails_closed(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    present_registry: PresentRegistry,
) -> None:
    """A device owned by another config entry is never adopted."""
    entry = _entry(hass)
    device_registry.async_get_or_create(
        config_entry_id=_foreign_entry(hass).entry_id,
        identifiers={asset_device_identifier(ASSET_UUID)},
        name="Foreign holder",
    )

    with pytest.raises(AssetStoreError, match="externally owned"):
        _lookup(present_registry(device_registry), entry)


@pytest.mark.parametrize(
    ("extra_identifiers", "connections"),
    [
        ({(DOMAIN, "extra-identifier")}, set()),
        (set(), {(dr.CONNECTION_NETWORK_MAC, "00:11:22:33:44:66")}),
    ],
    ids=["extra_identifier", "connection"],
)
async def test_owned_holder_with_extra_identity_fails_closed(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    present_registry: PresentRegistry,
    extra_identifiers: set[tuple[str, str]],
    connections: set[tuple[str, str]],
) -> None:
    """An owned holder with any extra identity is ambiguous."""
    entry = _entry(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={asset_device_identifier(ASSET_UUID)} | extra_identifiers,
        connections=connections,
        name="Extra identity",
    )

    with pytest.raises(AssetStoreError, match="externally owned"):
        _lookup(present_registry(device_registry), entry)


async def test_owned_holder_on_subentry_needs_parent_unless_preflight_repairs(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    present_registry: PresentRegistry,
) -> None:
    """A subentry-attached holder fails closed, except for preflight repair."""
    entry = _entry(hass, device_id="external-device")
    created = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=next(iter(entry.subentries)),
        identifiers={asset_device_identifier(ASSET_UUID)},
        name="Subentry holder",
    )
    registry = present_registry(device_registry)

    with pytest.raises(AssetStoreError, match="externally owned"):
        _lookup(registry, entry)
    repairable = _lookup(registry, entry, require_parent=False)
    assert repairable is not None
    assert repairable.id == created.id


@pytest.mark.parametrize(
    "devices",
    [[object()], {"device-id": object()}],
    ids=["entry_collection", "device_id_mapping"],
)
def test_unexpected_registry_item_fails_closed(devices: object) -> None:
    """A registry item that is not a DeviceEntry is never skipped."""
    with pytest.raises(AssetStoreError, match="unexpected device item"):
        list(_registry_device_entries(SimpleNamespace(devices=devices)))


async def test_asset_device_lookup_uses_no_deprecated_registry_api(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Lookup on the installed real registry reports no Device Registry deprecation.

    Home Assistant 2026.9+ reports custom-integration mapping use of
    `device_registry.devices` through its frame helper logger, not through
    the integration's own logger.
    """
    caplog.set_level(logging.WARNING)
    entry = _entry(hass)
    created = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={asset_device_identifier(ASSET_UUID)},
        name="Exact Asset Device",
    )

    assert _lookup(device_registry, entry) == created
    assert not [
        record
        for record in caplog.records
        if "device_registry.devices" in record.getMessage()
    ]


@pytest.mark.parametrize("unique_id", ["orphan_lifecycle", "orphan_runtime_hours"])
async def test_orphan_legacy_entity_ids_fail_preflight(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
    unique_id: str,
) -> None:
    """Noncanonical lifecycle/runtime IDs are never remapped from metadata."""
    entry = _entry(hass)
    entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        unique_id,
        config_entry=entry,
    )
    with pytest.raises(AssetStoreError, match="canonical Asset"):
        build_exposure_migration_plan(
            entry=entry,
            assets=[asset_store_data["assets"][ASSET_UUID]],
            device_registry=device_registry,
            entity_registry=entity_registry,
        )


async def test_stale_canonical_runtime_entity_is_left_for_platform_cleanup(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    asset_store_data: AssetStoreData,
) -> None:
    """No Runtime subentry means preflight must not infer new subentry ownership."""
    entry = _entry(hass)
    entity_registry.async_get_or_create(
        Platform.SENSOR,
        DOMAIN,
        runtime_unique_id(ASSET_UUID),
        config_entry=entry,
    )
    plan = build_exposure_migration_plan(
        entry=entry,
        assets=[asset_store_data["assets"][ASSET_UUID]],
        device_registry=device_registry,
        entity_registry=entity_registry,
    )
    assert not [update for update in plan.entity_updates if update.kind == "runtime"]


def test_entity_disappearing_between_lookup_and_read_fails_preflight(
    asset_store_data: AssetStoreData,
) -> None:
    """A registry race after unique-ID resolution aborts the complete plan."""
    registry = Mock()
    registry.entities = {}
    registry.async_get_entity_id.side_effect = lambda platform, domain, unique_id: (
        "sensor.disappeared"
        if unique_id == lifecycle_unique_id(ASSET_UUID)
        else None
    )
    registry.async_get.return_value = None
    with pytest.raises(AssetStoreError, match="disappeared during exposure preflight"):
        build_exposure_migration_plan(
            entry=SimpleNamespace(entry_id="entry", subentries={}),
            assets=[asset_store_data["assets"][ASSET_UUID]],
            device_registry=SimpleNamespace(devices={}),
            entity_registry=registry,
        )


async def test_attempt_discovery_and_rollback_report_identity_races(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Compensating rollback records disappeared/changed rows and exact new devices."""
    entry = _entry(hass)
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={asset_device_identifier(ASSET_UUID)},
        name="Attempted Asset",
    )
    plan = ExposureMigrationPlan(
        config_entry_id=entry.entry_id,
        devices=(
            AssetDevicePlan(
                asset_uuid=ASSET_UUID,
                asset_id="DL0007",
                name="Attempted Asset",
                manufacturer=None,
                model=None,
                model_id=None,
                serial_number=None,
                sw_version=None,
                hw_version=None,
                existing_device_id=None,
            ),
        ),
        entity_updates=(),
    )
    created: list[str] = []
    _discover_attempt_devices(
        plan=plan,
        registry=device_registry,
        attempted_missing_assets={ASSET_UUID},
        created_device_ids=created,
    )
    assert created == [device.id]

    updates = [
        EntityRegistryUpdatePlan(
            kind="lifecycle",
            asset_uuid=ASSET_UUID,
            entity_id="sensor.disappeared",
            unique_id=lifecycle_unique_id(ASSET_UUID),
            original_device_id=None,
            original_config_subentry_id=None,
            desired_config_subentry_id=None,
        ),
        EntityRegistryUpdatePlan(
            kind="runtime",
            asset_uuid=ASSET_UUID,
            entity_id="sensor.changed",
            unique_id=runtime_unique_id(ASSET_UUID),
            original_device_id=None,
            original_config_subentry_id=None,
            desired_config_subentry_id=None,
        ),
    ]
    entity_registry = Mock()
    entity_registry.async_get.side_effect = [
        SimpleNamespace(unique_id="different"),
        None,
    ]
    failures = _rollback_exposure_registry(
        plan=plan,
        device_registry=device_registry,
        entity_registry=entity_registry,
        attempted_updates=updates,
        created_device_ids=[],
    )
    assert failures == [
        "entity sensor.changed changed unique ID during rollback",
        "entity sensor.disappeared disappeared during rollback",
    ]

    assert _rollback_exposure_registry(
        plan=plan,
        device_registry=device_registry,
        entity_registry=Mock(),
        attempted_updates=[],
        created_device_ids=["already-disappeared"],
    ) == []
