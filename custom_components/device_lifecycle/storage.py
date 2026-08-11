"""Versioned persistent Asset Core storage for Device Lifecycle."""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping, TypeVar, cast
from uuid import UUID, uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.util import json as json_util
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ASSET_UUID,
    CONF_CURRENCY,
    CONF_DEPLOYMENT_STATE,
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
    CONF_HA_AREA_ID,
    CONF_INSTALLED_DATE,
    CONF_NOTES,
    CONF_PURCHASE_DATE,
    CONF_PURCHASE_NAME,
    CONF_PURCHASE_PRICE,
    CONF_PURCHASE_UUID,
    CONF_RECEIPT_REFERENCE,
    CONF_RECEIPT_URL,
    CONF_SELLER,
    CONF_WARRANTY_TYPE,
    CONF_WARRANTY_UNTIL,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DEPLOYMENT_STATE_DEPLOYED,
    DEPLOYMENT_STATE_UNKNOWN,
    DEPLOYMENT_STATES,
    DOMAIN,
    LIFECYCLE_STATUSES,
    LIFECYCLE_STATUS_ACTIVE,
    LIFECYCLE_STATUS_RETIRED,
    LIFECYCLE_STATUS_UNKNOWN,
    REPLACEMENT_REASONS,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
    WARRANTY_MANUAL,
    WARRANTY_NONE,
    WARRANTY_ONE_YEAR,
    WARRANTY_TWO_YEARS,
    WARRANTY_TYPES,
)
from .models import (
    AssetData,
    AssetStoreData,
    HADeviceReference,
    LifecycleEventData,
    LifecycleStatus,
    PurchaseData,
    ReplacementReason,
    ReplacementRecordData,
)

STORAGE_VERSION = 3
STORAGE_MINOR_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.assets"

ASSET_ID_PATTERN = re.compile(r"^DL([0-9]{4})$")
MAX_ASSET_NUMBER = 9999

DEVICE_ROLE_PRIMARY = "primary"
DEVICE_ROLE_RELATED = "related"
DEVICE_ROLES = frozenset({DEVICE_ROLE_PRIMARY, DEVICE_ROLE_RELATED})

FIELD_SOURCE_HOME_ASSISTANT = "home_assistant"
FIELD_SOURCE_PURCHASE = "purchase"
FIELD_SOURCE_USER = "user"
FIELD_SOURCES = frozenset(
    {FIELD_SOURCE_HOME_ASSISTANT, FIELD_SOURCE_PURCHASE, FIELD_SOURCE_USER}
)

_MutationResultT = TypeVar("_MutationResultT")
_UNSET = object()
_USER_EDITABLE_ASSET_FIELDS = (
    "name",
    "category",
    "manufacturer",
    "model",
    "model_id",
    "serial_number",
    "sw_version",
    "hw_version",
    "notes",
)
_HOME_ASSISTANT_METADATA_FIELDS = frozenset(
    {
        "name",
        "manufacturer",
        "model",
        "model_id",
        "serial_number",
        "sw_version",
        "hw_version",
    }
)


@dataclass(frozen=True, slots=True)
class QuickAssetCreateRequest:
    """Immutable canonical command for one atomic Quick Asset creation."""

    asset_uuid: str
    primary_device_id: str | None
    metadata: Mapping[str, str | None]
    field_sources: Mapping[str, str]
    initial_lifecycle_status: LifecycleStatus
    initial_lifecycle_effective_date: str | None
    deployment_state: str
    installed_date: str | None
    ha_area_id: str | None
    warranty_type: str
    warranty_until: str | None
    purchase_uuid: str | None
    expected_purchase_date: str | None = None
    predecessor_asset_uuid: str | None = None
    expected_predecessor_lifecycle_status: LifecycleStatus | None = None
    expected_predecessor_current_event_uuid: str | None = None
    expected_predecessor_deployment_state: str | None = None
    expected_predecessor_ha_area_id: str | None = None
    replacement_reason: ReplacementReason | None = None
    replacement_effective_date: str | None = None
    replacement_notes: str | None = None
    retire_predecessor: bool = False
    undeploy_predecessor: bool = False

    def __post_init__(self) -> None:
        """Detach and freeze caller-owned command mappings."""
        object.__setattr__(
            self,
            "metadata",
            MappingProxyType(dict(self.metadata)),
        )
        object.__setattr__(
            self,
            "field_sources",
            MappingProxyType(dict(self.field_sources)),
        )


@dataclass(frozen=True, slots=True)
class QuickAssetCreateResult:
    """Detached result of one atomic or safely replayed Quick Create."""

    asset: AssetData
    replacement: ReplacementRecordData | None
    predecessor: AssetData | None
    predecessor_lifecycle_changed: bool
    predecessor_deployment_changed: bool
    predecessor_area_cleared: bool
    replayed: bool


class AssetStoreError(HomeAssistantError):
    """Raised when Asset Core storage cannot be safely reconciled."""

    def __init__(self, message: str, *, code: str = "asset_store_error") -> None:
        """Initialize an error with a stable machine-readable code."""
        super().__init__(message)
        self.code = code


class AssetStorePersistenceError(AssetStoreError):
    """Raised when Store persistence cannot be verified."""

    def __init__(self, message: str, *, ambiguous: bool = False) -> None:
        """Initialize a persistence error with acknowledgement certainty."""
        super().__init__(message, code="persistence_error")
        self.ambiguous = ambiguous


class DeviceLifecycleStore(Store[AssetStoreData]):
    """Home Assistant Store with explicit migration and verified persistence."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize private, atomic Asset Core storage."""
        super().__init__(
            hass,
            STORAGE_VERSION,
            STORAGE_KEY,
            private=True,
            atomic_writes=True,
            minor_version=STORAGE_MINOR_VERSION,
            serialize_in_event_loop=False,
        )

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: AssetStoreData,
    ) -> AssetStoreData:
        """Migrate Asset Core storage without changing persistent identity."""
        data = deepcopy(old_data)

        if old_major_version == STORAGE_VERSION:
            if old_minor_version != STORAGE_MINOR_VERSION:
                raise AssetStoreError(
                    "Unsupported Asset Core Store version "
                    f"{old_major_version}.{old_minor_version}; "
                    f"expected {STORAGE_VERSION}.{STORAGE_MINOR_VERSION}"
                )
            _validate_store_data(data)
            return data

        if old_major_version == 1 and old_minor_version in (1, 2):
            data = _migrate_v1_to_v2_1(data, old_minor_version)
        elif old_major_version == 2 and old_minor_version == 1:
            pass
        else:
            raise AssetStoreError(
                "Unsupported Asset Core Store version "
                f"{old_major_version}.{old_minor_version}; expected one of "
                "1.1, 1.2, 2.1, or 3.1"
            )

        data = _migrate_v2_1_to_v3_1(data)
        _validate_store_data(data)
        return data

    async def async_save(self, data: AssetStoreData) -> None:
        """Save and verify the exact Store envelope from the persisted file."""
        await super().async_save(data)

        # Store defers writes once Home Assistant is stopping. Device Lifecycle
        # must know whether a mutation reached disk before publishing it, so force
        # a pending final write through the same Store write path now.
        if self._data is not None:
            await self._async_handle_write_data()

        expected = {
            "version": self.version,
            "minor_version": self.minor_version,
            "key": self.key,
            "data": data,
        }
        try:
            persisted = await self.hass.async_add_executor_job(
                json_util.load_json,
                self.path,
            )
        except HomeAssistantError as err:
            raise AssetStorePersistenceError(
                "Asset Core persistence could not be read back; the write "
                "result is unknown",
                ambiguous=True,
            ) from err

        if persisted != expected:
            raise AssetStorePersistenceError(
                "Asset Core persistence verification did not match the "
                "requested snapshot"
            )

    async def async_load_persisted_snapshot(self) -> AssetStoreData | None:
        """Read the current v3.1 payload directly, bypassing Store caches."""
        try:
            persisted = await self.hass.async_add_executor_job(
                json_util.load_json,
                self.path,
            )
        except HomeAssistantError as err:
            raise AssetStorePersistenceError(
                "Asset Core persistence could not be read for recovery",
                ambiguous=True,
            ) from err

        if persisted == {} and not await self.hass.async_add_executor_job(
            os.path.exists,
            self.path,
        ):
            return None

        if not isinstance(persisted, dict):
            raise AssetStoreError("Asset Core Store envelope is invalid")
        if (
            persisted.get("version") != STORAGE_VERSION
            or persisted.get("minor_version") != STORAGE_MINOR_VERSION
            or persisted.get("key") != STORAGE_KEY
            or not isinstance(persisted.get("data"), dict)
        ):
            raise AssetStoreError(
                "Asset Core Store envelope changed during persistence recovery"
            )
        return cast(AssetStoreData, deepcopy(persisted["data"]))


def _empty_store_data() -> AssetStoreData:
    """Return an empty Store 3.1 Asset Core payload."""
    return {
        "next_asset_number": 1,
        "purchases": {},
        "assets": {},
        "lifecycle_events": {},
        "replacement_records": {},
    }


def _migrate_v1_to_v2_1(
    old_data: AssetStoreData,
    old_minor_version: int,
) -> AssetStoreData:
    """Normalize supported Store 1.x data into the existing 2.1 model."""
    data = deepcopy(old_data)
    assets = data.get("assets")
    if not isinstance(assets, dict):
        return data

    for asset in assets.values():
        if not isinstance(asset, dict):
            continue
        if old_minor_version == 1:
            asset.setdefault(CONF_DEPLOYMENT_STATE, DEPLOYMENT_STATE_UNKNOWN)
            asset.setdefault(CONF_HA_AREA_ID, None)
            if asset.get("purchase_uuid") is not None:
                sources = asset.get("field_sources")
                if isinstance(sources, dict):
                    sources.setdefault("purchase_uuid", FIELD_SOURCE_PURCHASE)
        asset["runtime"] = {"total_seconds": None}
    return data


def _migrate_v2_1_to_v3_1(old_data: AssetStoreData) -> AssetStoreData:
    """Add lifecycle and replacement structures without inventing history."""
    data = deepcopy(old_data)
    assets = data.get("assets")
    if isinstance(assets, dict):
        for asset in assets.values():
            if isinstance(asset, dict):
                asset["lifecycle"] = {
                    "status": LIFECYCLE_STATUS_UNKNOWN,
                    "current_event_uuid": None,
                }
    data["lifecycle_events"] = {}
    data["replacement_records"] = {}
    return data


def _optional_text(value: Any) -> str | None:
    """Normalize an optional text value."""
    if value in (None, ""):
        return None
    return str(value)



def _normalize_price(value: Any) -> str | None:
    """Normalize money to a decimal string instead of binary floating point."""
    if value in (None, ""):
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as err:
        raise AssetStoreError("Purchase total price is not a valid decimal") from err
    if not amount.is_finite() or amount < 0:
        raise AssetStoreError("Purchase total price is invalid")
    return format(amount, "f")


def _runtime_seconds(value: Decimal, *, allow_zero: bool = True) -> Decimal:
    """Validate a Runtime seconds value without binary-float coercion."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise AssetStoreError("Runtime seconds must be a finite Decimal")
    if value < 0 or (not allow_zero and value == 0):
        raise AssetStoreError("Runtime seconds must be positive")
    return value


def _valid_uuid(value: Any) -> str | None:
    """Return a canonical UUID string or None."""
    if not value:
        return None
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _device_name(device: dr.DeviceEntry | None, fallback: str) -> str:
    """Return the best current display name for a Home Assistant device."""
    if device is None:
        return fallback
    value = (
        getattr(device, "name_by_user", None)
        or getattr(device, "name", None)
        or getattr(device, "model", None)
    )
    return str(value) if value not in (None, "") else fallback


def home_assistant_asset_metadata(
    device: dr.DeviceEntry | None,
    fallback_name: str,
) -> dict[str, str | None]:
    """Return canonical Asset metadata discovered from one HA device."""
    if device is None:
        return {
            "name": fallback_name,
            "manufacturer": None,
            "model": None,
            "model_id": None,
            "serial_number": None,
            "sw_version": None,
            "hw_version": None,
        }
    return {
        "name": _device_name(device, fallback_name),
        "manufacturer": _optional_text(getattr(device, "manufacturer", None)),
        "model": _optional_text(getattr(device, "model", None)),
        "model_id": _optional_text(getattr(device, "model_id", None)),
        "serial_number": _optional_text(getattr(device, "serial_number", None)),
        "sw_version": _optional_text(getattr(device, "sw_version", None)),
        "hw_version": _optional_text(getattr(device, "hw_version", None)),
    }


def _warranty_type(data: dict[str, Any]) -> str:
    """Return an explicit warranty type or infer old pre-0.3.4 data."""
    if value := data.get(CONF_WARRANTY_TYPE):
        return str(value)
    if data.get(CONF_WARRANTY_UNTIL):
        return WARRANTY_MANUAL
    return WARRANTY_NONE


def _primary_device_id(asset: AssetData) -> str | None:
    """Return an Asset's primary Home Assistant device reference."""
    for reference in asset.get("ha_device_refs", []):
        if reference.get("role") == DEVICE_ROLE_PRIMARY:
            return str(reference.get("device_id") or "") or None
    return None



def _validate_optional_date(value: Any, field: str) -> None:
    """Validate an optional ISO calendar date in persistent storage."""
    if value is None:
        return
    if not isinstance(value, str):
        raise AssetStoreError(f"Asset Core {field} must be an ISO date string")
    try:
        date.fromisoformat(value)
    except ValueError as err:
        raise AssetStoreError(f"Asset Core {field} is not a valid ISO date") from err


def add_calendar_years(value: str, years: int) -> str | None:
    """Add calendar years to one canonical date with leap-day safety."""
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.isoformat() != value:
        return None
    try:
        result = parsed.replace(year=parsed.year + years)
    except ValueError:
        result = parsed.replace(year=parsed.year + years, month=2, day=28)
    return result.isoformat()


def _validate_history_effective_date(
    value: Any,
    field: str,
    *,
    invalid_code: str,
    future_code: str,
) -> date | None:
    """Validate one canonical, non-future 0.7 history effective date."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise AssetStoreError(
            f"Asset Core {field} must use canonical YYYY-MM-DD",
            code=invalid_code,
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as err:
        raise AssetStoreError(
            f"Asset Core {field} is not a valid calendar date",
            code=invalid_code,
        ) from err
    if parsed.isoformat() != value:
        raise AssetStoreError(
            f"Asset Core {field} must use canonical YYYY-MM-DD",
            code=invalid_code,
        )
    if parsed > dt_util.now().date():
        raise AssetStoreError(
            f"Asset Core {field} cannot be in the future",
            code=future_code,
        )
    return parsed


def _parse_utc_timestamp(value: Any, field: str, *, code: str) -> datetime:
    """Return a valid aware UTC timestamp or fail closed."""
    if not isinstance(value, str):
        raise AssetStoreError(
            f"Asset Core {field} must be an aware UTC timestamp",
            code=code,
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise AssetStoreError(
            f"Asset Core {field} is not a valid timestamp",
            code=code,
        ) from err
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise AssetStoreError(
            f"Asset Core {field} must be an aware UTC timestamp",
            code=code,
        )
    return parsed


def _validate_lifecycle_graph(data: AssetStoreData) -> None:
    """Validate every immutable event and complete per-Asset event chain."""
    events = data["lifecycle_events"]
    events_by_asset: dict[str, dict[str, LifecycleEventData]] = {
        asset_uuid: {} for asset_uuid in data["assets"]
    }

    for event_uuid, event in events.items():
        if not isinstance(event, dict):
            raise AssetStoreError(
                f"Lifecycle event {event_uuid} is not a mapping",
                code="lifecycle_chain_invalid",
            )
        if set(event) != {
            "event_uuid",
            "asset_uuid",
            "previous_event_uuid",
            "from_status",
            "to_status",
            "effective_date",
            "recorded_at",
            "notes",
        }:
            raise AssetStoreError(
                f"Lifecycle event {event_uuid} has an invalid structure",
                code="lifecycle_chain_invalid",
            )
        if _valid_uuid(event_uuid) != event_uuid:
            raise AssetStoreError(
                f"Invalid lifecycle event UUID: {event_uuid}",
                code="lifecycle_chain_invalid",
            )
        if event.get("event_uuid") != event_uuid:
            raise AssetStoreError(
                f"Lifecycle event UUID/key mismatch: {event_uuid}",
                code="lifecycle_chain_invalid",
            )
        asset_uuid = event.get("asset_uuid")
        if _valid_uuid(asset_uuid) != asset_uuid or asset_uuid not in data["assets"]:
            raise AssetStoreError(
                f"Lifecycle event {event_uuid} references a missing Asset",
                code="lifecycle_chain_invalid",
            )
        if event.get("from_status") not in LIFECYCLE_STATUSES or event.get(
            "to_status"
        ) not in LIFECYCLE_STATUSES:
            raise AssetStoreError(
                f"Lifecycle event {event_uuid} has an invalid status",
                code="invalid_lifecycle_status",
            )
        if event["from_status"] == event["to_status"]:
            raise AssetStoreError(
                f"Lifecycle event {event_uuid} does not change status",
                code="lifecycle_chain_invalid",
            )
        previous_uuid = event.get("previous_event_uuid")
        if previous_uuid is not None and _valid_uuid(previous_uuid) != previous_uuid:
            raise AssetStoreError(
                f"Lifecycle event {event_uuid} has an invalid previous UUID",
                code="lifecycle_chain_invalid",
            )
        _validate_history_effective_date(
            event.get("effective_date"),
            "lifecycle effective_date",
            invalid_code="invalid_lifecycle_effective_date",
            future_code="lifecycle_date_in_future",
        )
        _parse_utc_timestamp(
            event.get("recorded_at"),
            "lifecycle recorded_at",
            code="lifecycle_chain_invalid",
        )
        notes = event.get("notes")
        if notes is not None and not isinstance(notes, str):
            raise AssetStoreError(
                f"Lifecycle event {event_uuid} has invalid notes",
                code="lifecycle_chain_invalid",
            )
        events_by_asset[asset_uuid][event_uuid] = cast(LifecycleEventData, event)

    for asset_uuid, asset in data["assets"].items():
        lifecycle = asset.get("lifecycle")
        if not isinstance(lifecycle, dict) or set(lifecycle) != {
            "status",
            "current_event_uuid",
        }:
            raise AssetStoreError(
                f"Asset {asset_uuid} has invalid lifecycle data",
                code="lifecycle_chain_invalid",
            )
        status = lifecycle.get("status")
        if status not in LIFECYCLE_STATUSES:
            raise AssetStoreError(
                f"Asset {asset_uuid} has an invalid lifecycle status",
                code="invalid_lifecycle_status",
            )
        current_uuid = lifecycle.get("current_event_uuid")
        asset_events = events_by_asset[asset_uuid]
        if not asset_events:
            if current_uuid is not None or status != LIFECYCLE_STATUS_UNKNOWN:
                raise AssetStoreError(
                    f"Asset {asset_uuid} lifecycle state has no event chain",
                    code="lifecycle_chain_invalid",
                )
            continue
        if _valid_uuid(current_uuid) != current_uuid or current_uuid not in asset_events:
            raise AssetStoreError(
                f"Asset {asset_uuid} has an invalid current lifecycle event",
                code="lifecycle_chain_invalid",
            )

        children: dict[str, str] = {}
        roots: list[str] = []
        for event_uuid, event in asset_events.items():
            previous_uuid = event["previous_event_uuid"]
            if previous_uuid is None:
                roots.append(event_uuid)
                if event["from_status"] != LIFECYCLE_STATUS_UNKNOWN:
                    raise AssetStoreError(
                        f"Asset {asset_uuid} lifecycle chain does not start unknown",
                        code="lifecycle_chain_invalid",
                    )
                continue
            previous = events.get(previous_uuid)
            if previous is None or previous.get("asset_uuid") != asset_uuid:
                raise AssetStoreError(
                    f"Lifecycle event {event_uuid} has an invalid previous event",
                    code="lifecycle_chain_invalid",
                )
            if previous.get("to_status") != event["from_status"]:
                raise AssetStoreError(
                    f"Lifecycle event {event_uuid} has discontinuous statuses",
                    code="lifecycle_chain_invalid",
                )
            if previous_uuid in children:
                raise AssetStoreError(
                    f"Asset {asset_uuid} lifecycle history branches",
                    code="lifecycle_chain_invalid",
                )
            children[previous_uuid] = event_uuid

        if len(roots) != 1:
            raise AssetStoreError(
                f"Asset {asset_uuid} lifecycle events do not form one chain",
                code="lifecycle_chain_invalid",
            )

        visited: set[str] = set()
        event_uuid: str | None = roots[0]
        previous_date: date | None = None
        previous_recorded: datetime | None = None
        last_uuid: str | None = None
        while event_uuid is not None:
            if event_uuid in visited:
                raise AssetStoreError(
                    f"Asset {asset_uuid} lifecycle chain contains a cycle",
                    code="lifecycle_chain_invalid",
                )
            visited.add(event_uuid)
            event = asset_events[event_uuid]
            effective = (
                date.fromisoformat(event["effective_date"])
                if event["effective_date"] is not None
                else None
            )
            recorded = _parse_utc_timestamp(
                event["recorded_at"],
                "lifecycle recorded_at",
                code="lifecycle_chain_invalid",
            )
            if (
                previous_date is not None
                and effective is not None
                and effective < previous_date
            ):
                raise AssetStoreError(
                    f"Asset {asset_uuid} lifecycle effective dates decrease",
                    code="lifecycle_chain_invalid",
                )
            if previous_recorded is not None and recorded < previous_recorded:
                raise AssetStoreError(
                    f"Asset {asset_uuid} lifecycle recorded timestamps decrease",
                    code="lifecycle_chain_invalid",
                )
            previous_date = effective
            previous_recorded = recorded
            last_uuid = event_uuid
            event_uuid = children.get(event_uuid)

        if visited != set(asset_events) or last_uuid != current_uuid:
            raise AssetStoreError(
                f"Asset {asset_uuid} lifecycle events contain orphans",
                code="lifecycle_chain_invalid",
            )
        if asset_events[current_uuid]["to_status"] != status:
            raise AssetStoreError(
                f"Asset {asset_uuid} current lifecycle status does not match its event",
                code="lifecycle_chain_invalid",
            )


def _validate_replacement_graph(data: AssetStoreData) -> None:
    """Validate permanent records and the complete active 1:1 replacement graph."""
    records = data["replacement_records"]
    outgoing: dict[str, ReplacementRecordData] = {}
    incoming: dict[str, ReplacementRecordData] = {}
    active_pairs: set[tuple[str, str]] = set()

    for replacement_uuid, record in records.items():
        if not isinstance(record, dict):
            raise AssetStoreError(
                f"Replacement record {replacement_uuid} is not a mapping",
                code="replacement_missing",
            )
        if set(record) != {
            "replacement_uuid",
            "predecessor_asset_uuid",
            "successor_asset_uuid",
            "reason",
            "effective_date",
            "recorded_at",
            "notes",
            "voided_at",
            "void_reason",
        }:
            raise AssetStoreError(
                f"Replacement record {replacement_uuid} has an invalid structure",
                code="replacement_graph_invalid",
            )
        if _valid_uuid(replacement_uuid) != replacement_uuid:
            raise AssetStoreError(
                f"Invalid replacement UUID: {replacement_uuid}",
                code="replacement_missing",
            )
        if record.get("replacement_uuid") != replacement_uuid:
            raise AssetStoreError(
                f"Replacement UUID/key mismatch: {replacement_uuid}",
                code="replacement_missing",
            )
        predecessor = record.get("predecessor_asset_uuid")
        successor = record.get("successor_asset_uuid")
        if _valid_uuid(predecessor) != predecessor or predecessor not in data["assets"]:
            raise AssetStoreError(
                f"Replacement {replacement_uuid} references a missing predecessor",
                code="asset_missing",
            )
        if _valid_uuid(successor) != successor or successor not in data["assets"]:
            raise AssetStoreError(
                f"Replacement {replacement_uuid} references a missing successor",
                code="asset_missing",
            )
        if predecessor == successor:
            raise AssetStoreError(
                f"Replacement {replacement_uuid} references the same Asset twice",
                code="replacement_self_reference",
            )
        if record.get("reason") not in REPLACEMENT_REASONS:
            raise AssetStoreError(
                f"Replacement {replacement_uuid} has an invalid reason",
                code="invalid_replacement_reason",
            )
        _validate_history_effective_date(
            record.get("effective_date"),
            "replacement effective_date",
            invalid_code="invalid_replacement_effective_date",
            future_code="replacement_date_in_future",
        )
        recorded_at = _parse_utc_timestamp(
            record.get("recorded_at"),
            "replacement recorded_at",
            code="replacement_graph_invalid",
        )
        notes = record.get("notes")
        if notes is not None and not isinstance(notes, str):
            raise AssetStoreError(
                f"Replacement {replacement_uuid} has invalid notes",
                code="replacement_graph_invalid",
            )
        voided_at = record.get("voided_at")
        void_reason = record.get("void_reason")
        if voided_at is None:
            if void_reason is not None:
                raise AssetStoreError(
                    f"Active replacement {replacement_uuid} has a void reason",
                    code="replacement_graph_invalid",
                )
        else:
            parsed_voided = _parse_utc_timestamp(
                voided_at,
                "replacement voided_at",
                code="replacement_graph_invalid",
            )
            if parsed_voided < recorded_at or not isinstance(
                void_reason, str
            ) or not void_reason.strip():
                raise AssetStoreError(
                    f"Voided replacement {replacement_uuid} has invalid void data",
                    code="replacement_graph_invalid",
                )
            continue

        typed_record = cast(ReplacementRecordData, record)
        pair = (predecessor, successor)
        if pair in active_pairs:
            raise AssetStoreError(
                f"Duplicate active replacement {predecessor} -> {successor}",
                code="replacement_predecessor_conflict",
            )
        active_pairs.add(pair)
        if predecessor in outgoing:
            raise AssetStoreError(
                f"Asset {predecessor} has more than one active successor",
                code="replacement_predecessor_conflict",
            )
        if successor in incoming:
            raise AssetStoreError(
                f"Asset {successor} has more than one active predecessor",
                code="replacement_successor_conflict",
            )
        outgoing[predecessor] = typed_record
        incoming[successor] = typed_record

    visited: set[str] = set()
    for start_asset_uuid in data["assets"]:
        if start_asset_uuid in visited:
            continue

        path: set[str] = set()
        asset_uuid = start_asset_uuid
        while asset_uuid not in visited:
            if asset_uuid in path:
                raise AssetStoreError(
                    "The active replacement graph contains a cycle",
                    code="replacement_cycle",
                )
            path.add(asset_uuid)

            record = outgoing.get(asset_uuid)
            if record is None:
                break
            successor = record["successor_asset_uuid"]
            next_record = outgoing.get(successor)
            if (
                next_record is not None
                and record["effective_date"] is not None
                and next_record["effective_date"] is not None
                and date.fromisoformat(record["effective_date"])
                > date.fromisoformat(next_record["effective_date"])
            ):
                raise AssetStoreError(
                    "Replacement effective dates decrease along the active chain",
                    code="replacement_graph_invalid",
                )
            asset_uuid = successor

        visited.update(path)


def _validate_store_data(data: AssetStoreData) -> None:
    """Validate invariants which must remain true across all future releases."""
    if not all(
        isinstance(data.get(key), dict)
        for key in (
            "purchases",
            "assets",
            "lifecycle_events",
            "replacement_records",
        )
    ):
        raise AssetStoreError("Asset Core storage has an invalid top-level structure")

    next_number = data.get("next_asset_number")
    if (
        not isinstance(next_number, int)
        or isinstance(next_number, bool)
        or not 1 <= next_number <= MAX_ASSET_NUMBER + 1
    ):
        raise AssetStoreError("Asset Core next_asset_number is invalid")

    asset_ids: set[str] = set()
    primary_device_ids: set[str] = set()
    max_asset_number = 0

    for asset_uuid, asset in data["assets"].items():
        if not isinstance(asset, dict):
            raise AssetStoreError(f"Asset {asset_uuid} is not a mapping")
        if _valid_uuid(asset_uuid) != asset_uuid:
            raise AssetStoreError(f"Invalid Asset UUID: {asset_uuid}")
        if asset.get("asset_uuid") != asset_uuid:
            raise AssetStoreError(f"Asset UUID/key mismatch: {asset_uuid}")

        asset_id = str(asset.get("asset_id") or "")
        match = ASSET_ID_PATTERN.fullmatch(asset_id)
        if match is None:
            raise AssetStoreError(f"Invalid Asset ID: {asset_id}")
        if asset_id in asset_ids:
            raise AssetStoreError(f"Duplicate Asset ID: {asset_id}")
        asset_ids.add(asset_id)
        max_asset_number = max(max_asset_number, int(match.group(1)))

        if not isinstance(asset.get("name"), str) or not asset["name"].strip():
            raise AssetStoreError(f"Asset {asset_id} has no stable display name")

        purchase_uuid = asset.get("purchase_uuid")
        if purchase_uuid is not None and _valid_uuid(purchase_uuid) != purchase_uuid:
            raise AssetStoreError(f"Asset {asset_id} has an invalid Purchase UUID")

        deployment_state = asset.get(CONF_DEPLOYMENT_STATE)
        if deployment_state not in DEPLOYMENT_STATES:
            raise AssetStoreError(f"Asset {asset_id} has an invalid deployment state")

        _validate_optional_date(asset.get("installed_date"), "installed_date")

        ha_area_id = asset.get(CONF_HA_AREA_ID)
        if ha_area_id is not None and (
            not isinstance(ha_area_id, str) or not ha_area_id.strip()
        ):
            raise AssetStoreError(f"Asset {asset_id} has an invalid HA Area ID")

        warranty = asset.get("warranty")
        if not isinstance(warranty, dict):
            raise AssetStoreError(f"Asset {asset_id} has invalid warranty data")
        if warranty.get("type") not in WARRANTY_TYPES:
            raise AssetStoreError(f"Asset {asset_id} has an invalid warranty type")
        _validate_optional_date(warranty.get("until"), "warranty.until")

        runtime = asset.get("runtime")
        if not isinstance(runtime, dict) or set(runtime) != {"total_seconds"}:
            raise AssetStoreError(f"Asset {asset_id} has invalid Runtime data")
        runtime_total = runtime.get("total_seconds")
        if runtime_total is not None:
            if not isinstance(runtime_total, str):
                raise AssetStoreError(
                    f"Asset {asset_id} has an invalid Runtime total"
                )
            try:
                runtime_seconds = Decimal(runtime_total)
            except InvalidOperation as err:
                raise AssetStoreError(
                    f"Asset {asset_id} has an invalid Runtime total"
                ) from err
            if not runtime_seconds.is_finite() or runtime_seconds < 0:
                raise AssetStoreError(
                    f"Asset {asset_id} has an invalid Runtime total"
                )

        sources = asset.get("field_sources")
        if not isinstance(sources, dict):
            raise AssetStoreError(f"Asset {asset_id} has invalid field provenance")
        for field, source in sources.items():
            if not isinstance(field, str) or source not in FIELD_SOURCES:
                raise AssetStoreError(f"Asset {asset_id} has invalid field provenance")

        purchase_source = sources.get("purchase_uuid")
        if purchase_source is not None and purchase_source not in (
            FIELD_SOURCE_PURCHASE,
            FIELD_SOURCE_USER,
        ):
            raise AssetStoreError(
                f"Asset {asset_id} has invalid Purchase relationship provenance"
            )
        if purchase_uuid is not None and purchase_source is None:
            raise AssetStoreError(
                f"Asset {asset_id} has no Purchase relationship provenance"
            )

        refs = asset.get("ha_device_refs")
        if not isinstance(refs, list):
            raise AssetStoreError(f"Asset {asset_id} has invalid HA device references")

        primary_count = 0
        local_device_ids: set[str] = set()
        for reference in refs:
            if not isinstance(reference, dict):
                raise AssetStoreError(f"Asset {asset_id} has an invalid HA reference")
            device_id = str(reference.get("device_id") or "")
            role = str(reference.get("role") or "")
            if not device_id or role not in DEVICE_ROLES:
                raise AssetStoreError(f"Asset {asset_id} has an invalid HA reference")
            if device_id in local_device_ids:
                raise AssetStoreError(
                    f"Asset {asset_id} references HA device {device_id} more than once"
                )
            local_device_ids.add(device_id)

            if role == DEVICE_ROLE_PRIMARY:
                primary_count += 1
                if device_id in primary_device_ids:
                    raise AssetStoreError(
                        f"HA device {device_id} is primary for more than one Asset"
                    )
                primary_device_ids.add(device_id)

        if primary_count > 1:
            raise AssetStoreError(f"Asset {asset_id} has more than one primary HA device")

    if max_asset_number >= next_number:
        raise AssetStoreError(
            "Asset Core next_asset_number would recycle an existing Asset ID"
        )

    subentry_ids: set[str] = set()
    for purchase_uuid, purchase in data["purchases"].items():
        if not isinstance(purchase, dict):
            raise AssetStoreError(f"Purchase {purchase_uuid} is not a mapping")
        if _valid_uuid(purchase_uuid) != purchase_uuid:
            raise AssetStoreError(f"Invalid Purchase UUID: {purchase_uuid}")
        if purchase.get("purchase_uuid") != purchase_uuid:
            raise AssetStoreError(f"Purchase UUID/key mismatch: {purchase_uuid}")

        if not isinstance(purchase.get("configured"), bool):
            raise AssetStoreError(f"Purchase {purchase_uuid} has invalid configured state")

        subentry_id = purchase.get("config_subentry_id")
        if subentry_id is not None:
            if not isinstance(subentry_id, str) or not subentry_id:
                raise AssetStoreError(
                    f"Purchase {purchase_uuid} has an invalid config subentry ID"
                )
            if subentry_id in subentry_ids:
                raise AssetStoreError(
                    f"Config subentry {subentry_id} maps to more than one Purchase"
                )
            subentry_ids.add(subentry_id)

        currency = purchase.get("currency")
        if not isinstance(currency, str) or not currency.strip():
            raise AssetStoreError(f"Purchase {purchase_uuid} has an invalid currency")

        price = purchase.get("total_price")
        if price is not None:
            if not isinstance(price, str):
                raise AssetStoreError(
                    f"Purchase {purchase_uuid} has an invalid total price"
                )
            try:
                amount = Decimal(price)
            except InvalidOperation as err:
                raise AssetStoreError(
                    f"Purchase {purchase_uuid} has an invalid total price"
                ) from err
            if not amount.is_finite() or amount < 0:
                raise AssetStoreError(
                    f"Purchase {purchase_uuid} has an invalid total price"
                )

        _validate_optional_date(purchase.get("purchase_date"), "purchase_date")

        member_ids = purchase.get("asset_uuids")
        if not isinstance(member_ids, list) or len(member_ids) != len(set(member_ids)):
            raise AssetStoreError(f"Purchase {purchase_uuid} has invalid Asset membership")

        for asset_uuid in member_ids:
            asset = data["assets"].get(asset_uuid)
            if asset is None:
                raise AssetStoreError(
                    f"Purchase {purchase_uuid} references missing Asset {asset_uuid}"
                )
            if asset.get("purchase_uuid") != purchase_uuid:
                raise AssetStoreError(
                    f"Purchase/Asset membership mismatch for Asset {asset_uuid}"
                )

    for asset_uuid, asset in data["assets"].items():
        purchase_uuid = asset.get("purchase_uuid")
        if purchase_uuid is None:
            continue
        purchase = data["purchases"].get(purchase_uuid)
        if purchase is None or asset_uuid not in purchase.get("asset_uuids", []):
            raise AssetStoreError(
                f"Asset {asset_uuid} references an inconsistent Purchase"
            )

    _validate_lifecycle_graph(data)
    _validate_replacement_graph(data)


class AssetStoreManager:
    """Own the normalized persistent Asset/Purchase model."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the manager."""
        self.hass = hass
        self._store = DeviceLifecycleStore(hass)
        self._data: AssetStoreData = _empty_store_data()
        self._mutation_lock = asyncio.Lock()
        self._persistence_uncertain = False

    async def async_setup(self) -> None:
        """Load and validate persistent Asset Core data."""
        storage_existed = await self.hass.async_add_executor_job(
            os.path.exists,
            self._store.path,
        )
        loaded = await self._store.async_load()
        if loaded is None:
            if storage_existed:
                # Home Assistant's Store preserves a corrupt file under a renamed
                # path and returns None. Starting from DL0001 again would violate
                # the permanent-ID invariant, so fail setup and require recovery.
                raise AssetStoreError(
                    "Existing Asset Core storage could not be loaded safely; "
                    "restore the preserved storage file from backup"
                )
            self._data = _empty_store_data()
            return

        if not isinstance(loaded, dict):
            raise AssetStoreError("Asset Core storage payload is not a mapping")

        # Store is versioned, so a version-one payload is expected to contain all
        # top-level keys. Refuse to guess if the private store is malformed.
        if not all(
            key in loaded
            for key in (
                "next_asset_number",
                "purchases",
                "assets",
                "lifecycle_events",
                "replacement_records",
            )
        ):
            raise AssetStoreError("Asset Core storage payload is incomplete")

        data = cast(AssetStoreData, deepcopy(loaded))
        _validate_store_data(data)
        self._data = data

    async def _async_recover_uncertain_persistence(self) -> None:
        """Recover the direct persisted snapshot before another mutation."""
        if not self._persistence_uncertain:
            return
        persisted = await self._store.async_load_persisted_snapshot()
        if persisted is None:
            if self._data != _empty_store_data():
                raise AssetStoreError(
                    "Asset Core persistence disappeared during recovery"
                )
        else:
            _validate_store_data(persisted)
            self._data = persisted
        self._persistence_uncertain = False

    async def _async_mutate(
        self,
        mutator: Callable[[AssetStoreData], _MutationResultT],
    ) -> _MutationResultT:
        """Apply one all-or-nothing mutation to a detached Store snapshot."""
        async with self._mutation_lock:
            await self._async_recover_uncertain_persistence()

            data = deepcopy(self._data)
            result = mutator(data)
            _validate_store_data(data)

            if data != self._data:
                try:
                    await self._store.async_save(data)
                except AssetStorePersistenceError as err:
                    self._persistence_uncertain = err.ambiguous
                    raise

            # Publish only after the complete snapshot has been validated and
            # durably saved. A mutation or save exception leaves _data untouched.
            self._data = data
            return deepcopy(result)

    async def _async_mutate_history(
        self,
        mutator: Callable[[AssetStoreData], _MutationResultT],
    ) -> _MutationResultT:
        """Apply a lifecycle/replacement mutation with structured persistence errors."""
        try:
            return await self._async_mutate(mutator)
        except AssetStoreError:
            raise
        except OSError as err:
            raise AssetStorePersistenceError(
                "Asset Core history mutation could not be persisted"
            ) from err

    async def async_initialize_new_runtime(self, asset_uuid: str) -> Decimal:
        """Atomically initialize a new marked Runtime from null to zero."""

        def _initialize(data: AssetStoreData) -> Decimal:
            asset = self._require_asset(data, asset_uuid)
            total = asset["runtime"]["total_seconds"]
            if total is None:
                asset["runtime"]["total_seconds"] = "0"
                return Decimal(0)
            return Decimal(total)

        return await self._async_mutate(_initialize)

    async def async_import_legacy_runtime(
        self,
        asset_uuid: str,
        total_seconds: Decimal,
    ) -> Decimal:
        """Compare-and-set a validated legacy RestoreSensor total exactly once."""
        normalized = _runtime_seconds(total_seconds)

        def _initialize(data: AssetStoreData) -> Decimal:
            asset = self._require_asset(data, asset_uuid)
            current = asset["runtime"]["total_seconds"]
            if current is not None:
                return Decimal(current)
            asset["runtime"]["total_seconds"] = format(normalized, "f")
            return normalized

        return await self._async_mutate(_initialize)

    async def async_commit_runtime_delta(
        self,
        asset_uuid: str,
        *,
        expected_total: Decimal,
        delta: Decimal,
    ) -> Decimal:
        """Commit one Runtime delta with compare-and-set idempotency."""
        expected = _runtime_seconds(expected_total)
        increment = _runtime_seconds(delta, allow_zero=False)
        committed = expected + increment

        def _commit(data: AssetStoreData) -> Decimal:
            asset = self._require_asset(data, asset_uuid)
            stored = asset["runtime"]["total_seconds"]
            if stored is None:
                raise AssetStoreError(
                    f"Asset {asset_uuid} Runtime is not initialized"
                )
            current = Decimal(stored)
            if current == expected:
                asset["runtime"]["total_seconds"] = format(committed, "f")
                return committed
            if current == committed:
                return committed
            raise AssetStoreError(
                f"Asset {asset_uuid} Runtime total changed unexpectedly"
            )

        return await self._async_mutate(_commit)

    def runtime_total_seconds(self, asset_uuid: str) -> Decimal | None:
        """Return one Asset's detached canonical Runtime total."""
        asset = self.asset(asset_uuid)
        if asset is None:
            raise AssetStoreError(f"Asset {asset_uuid} does not exist")
        total = asset["runtime"]["total_seconds"]
        return None if total is None else Decimal(total)

    def _require_asset(
        self,
        data: AssetStoreData,
        asset_uuid: str,
    ) -> AssetData:
        """Return an Asset from a transaction snapshot or raise a stable error."""
        if _valid_uuid(asset_uuid) != asset_uuid:
            raise AssetStoreError(
                f"Asset {asset_uuid} does not exist",
                code="asset_missing",
            )
        asset = data["assets"].get(asset_uuid)
        if asset is None:
            raise AssetStoreError(
                f"Asset {asset_uuid} does not exist",
                code="asset_missing",
            )
        return asset

    def _initialize_asset_lifecycle(
        self,
        data: AssetStoreData,
        asset: AssetData,
        status: LifecycleStatus,
        *,
        effective_date: str | None = None,
        recorded_at: str | None = None,
    ) -> None:
        """Initialize one new Asset without fabricating same-state history."""
        if status not in LIFECYCLE_STATUSES:
            raise AssetStoreError(
                f"Invalid initial lifecycle status: {status}",
                code="invalid_lifecycle_status",
            )
        asset["lifecycle"] = {
            "status": LIFECYCLE_STATUS_UNKNOWN,
            "current_event_uuid": None,
        }
        if status == LIFECYCLE_STATUS_UNKNOWN:
            return
        self._append_lifecycle_transition(
            data,
            asset,
            status,
            effective_date=effective_date,
            notes=None,
            recorded_at=recorded_at,
        )

    def _append_lifecycle_transition(
        self,
        data: AssetStoreData,
        asset: AssetData,
        status: LifecycleStatus,
        *,
        effective_date: str | None,
        notes: str | None,
        recorded_at: str | None = None,
    ) -> bool:
        """Append one transition inside an existing atomic Store snapshot."""
        if status not in LIFECYCLE_STATUSES:
            raise AssetStoreError(
                f"Invalid lifecycle status: {status}",
                code="invalid_lifecycle_status",
            )
        lifecycle = asset["lifecycle"]
        if lifecycle["status"] == status:
            return False
        normalized_date = self._normalize_effective_date(
            effective_date,
            invalid_code="invalid_lifecycle_effective_date",
            future_code="lifecycle_date_in_future",
        )
        normalized_notes = self._normalize_history_notes(notes)
        event_uuid = str(uuid.uuid4())
        if event_uuid in data["lifecycle_events"]:
            raise AssetStoreError(
                "Generated lifecycle event UUID is already in use",
                code="lifecycle_chain_invalid",
            )
        event: LifecycleEventData = {
            "event_uuid": event_uuid,
            "asset_uuid": asset["asset_uuid"],
            "previous_event_uuid": lifecycle["current_event_uuid"],
            "from_status": lifecycle["status"],
            "to_status": status,
            "effective_date": normalized_date,
            "recorded_at": recorded_at or datetime.now(UTC).isoformat(),
            "notes": normalized_notes,
        }
        data["lifecycle_events"][event_uuid] = event
        lifecycle["status"] = status
        lifecycle["current_event_uuid"] = event_uuid
        return True

    def _normalize_user_asset_text(
        self,
        value: Any,
        field: str,
        *,
        required: bool = False,
    ) -> str | None:
        """Normalize user-owned Asset text without accepting implicit coercion."""
        if value is None:
            if required:
                raise AssetStoreError(f"Asset {field} is required")
            return None
        if not isinstance(value, str):
            raise AssetStoreError(f"Asset {field} must be text")
        if required:
            value = value.strip()
            if not value:
                raise AssetStoreError(f"Asset {field} is required")
            return value
        return value if value else None

    def _allocate_asset_id(self, data: AssetStoreData) -> str:
        """Allocate a permanent short Asset ID without ever recycling it."""
        number = data["next_asset_number"]
        if number > MAX_ASSET_NUMBER:
            raise AssetStoreError(
                "Device Lifecycle has exhausted the DL0001-DL9999 Asset ID range"
            )
        data["next_asset_number"] = number + 1
        return f"DL{number:04d}"

    def _find_asset_by_primary_device(
        self,
        data: AssetStoreData,
        device_id: str,
    ) -> AssetData | None:
        """Find the Asset whose primary HA device is device_id."""
        for asset in data["assets"].values():
            if _primary_device_id(asset) == device_id:
                return asset
        return None

    def _new_asset(
        self,
        data: AssetStoreData,
        device_id: str,
        device: dr.DeviceEntry | None,
    ) -> AssetData:
        """Create one stable Asset for a physical Home Assistant device."""
        asset_uuid = str(uuid4())
        asset = self._create_asset_in_snapshot(
            data,
            asset_uuid=asset_uuid,
            name=_device_name(device, device_id),
            metadata={},
            field_sources={"name": FIELD_SOURCE_HOME_ASSISTANT},
            primary_device_id=device_id,
            deployment_state=DEPLOYMENT_STATE_UNKNOWN,
            installed_date=None,
            ha_area_id=None,
            warranty_type=WARRANTY_NONE,
            warranty_until=None,
            initial_lifecycle_status=LIFECYCLE_STATUS_ACTIVE,
        )
        self._refresh_home_assistant_metadata(asset, device, device_id)
        return asset

    def _create_asset_in_snapshot(
        self,
        data: AssetStoreData,
        *,
        asset_uuid: str,
        name: str,
        metadata: dict[str, str | None],
        field_sources: dict[str, str],
        primary_device_id: str | None,
        deployment_state: str,
        installed_date: str | None,
        ha_area_id: str | None,
        warranty_type: str,
        warranty_until: str | None,
        initial_lifecycle_status: LifecycleStatus,
        lifecycle_effective_date: str | None = None,
        recorded_at: str | None = None,
    ) -> AssetData:
        """Create one Asset inside an existing atomic Store snapshot."""
        if asset_uuid in data["assets"]:
            raise AssetStoreError("Generated Asset UUID is already in use")
        asset: AssetData = {
            "asset_uuid": asset_uuid,
            "asset_id": self._allocate_asset_id(data),
            "name": name,
            "category": metadata.get("category"),
            "purchase_uuid": None,
            CONF_DEPLOYMENT_STATE: deployment_state,
            "installed_date": installed_date,
            CONF_HA_AREA_ID: ha_area_id,
            "warranty": {"type": warranty_type, "until": warranty_until},
            "runtime": {"total_seconds": None},
            "lifecycle": {
                "status": LIFECYCLE_STATUS_UNKNOWN,
                "current_event_uuid": None,
            },
            "manufacturer": metadata.get("manufacturer"),
            "model": metadata.get("model"),
            "model_id": metadata.get("model_id"),
            "serial_number": metadata.get("serial_number"),
            "sw_version": metadata.get("sw_version"),
            "hw_version": metadata.get("hw_version"),
            "notes": metadata.get("notes"),
            "field_sources": dict(field_sources),
            "ha_device_refs": (
                []
                if primary_device_id is None
                else [
                    {
                        "device_id": primary_device_id,
                        "role": DEVICE_ROLE_PRIMARY,
                    }
                ]
            ),
        }
        data["assets"][asset_uuid] = asset
        self._initialize_asset_lifecycle(
            data,
            asset,
            initial_lifecycle_status,
            effective_date=lifecycle_effective_date,
            recorded_at=recorded_at,
        )
        return asset

    async def async_create_manual_asset(
        self,
        *,
        name: str,
        category: str | None = None,
        manufacturer: str | None = None,
        model: str | None = None,
        model_id: str | None = None,
        serial_number: str | None = None,
        sw_version: str | None = None,
        hw_version: str | None = None,
        notes: str | None = None,
        initial_lifecycle_status: LifecycleStatus = LIFECYCLE_STATUS_ACTIVE,
    ) -> AssetData:
        """Create a persistent physical Asset without Purchase or HA identity."""
        normalized_name = self._normalize_user_asset_text(
            name,
            "name",
            required=True,
        )
        metadata = {
            "category": self._normalize_user_asset_text(category, "category"),
            "manufacturer": self._normalize_user_asset_text(
                manufacturer,
                "manufacturer",
            ),
            "model": self._normalize_user_asset_text(model, "model"),
            "model_id": self._normalize_user_asset_text(model_id, "model_id"),
            "serial_number": self._normalize_user_asset_text(
                serial_number,
                "serial_number",
            ),
            "sw_version": self._normalize_user_asset_text(
                sw_version,
                "sw_version",
            ),
            "hw_version": self._normalize_user_asset_text(
                hw_version,
                "hw_version",
            ),
            "notes": self._normalize_user_asset_text(notes, "notes"),
        }

        def _create(data: AssetStoreData) -> AssetData:
            asset_uuid = str(uuid4())
            sources = {"name": FIELD_SOURCE_USER}
            sources.update(
                {
                    field: FIELD_SOURCE_USER
                    for field, value in metadata.items()
                    if value is not None
                }
            )
            return self._create_asset_in_snapshot(
                data,
                asset_uuid=asset_uuid,
                name=cast(str, normalized_name),
                metadata=metadata,
                field_sources=sources,
                primary_device_id=None,
                deployment_state=DEPLOYMENT_STATE_NOT_DEPLOYED,
                installed_date=None,
                ha_area_id=None,
                warranty_type=WARRANTY_NONE,
                warranty_until=None,
                initial_lifecycle_status=initial_lifecycle_status,
            )

        return await self._async_mutate(_create)

    def _normalize_effective_date(
        self,
        value: str | None,
        *,
        invalid_code: str,
        future_code: str,
    ) -> str | None:
        """Validate a user-entered canonical date against HA local today."""
        _validate_history_effective_date(
            value,
            "effective_date",
            invalid_code=invalid_code,
            future_code=future_code,
        )
        return value

    def _normalize_history_notes(self, value: str | None) -> str | None:
        """Normalize optional history notes without implicit coercion."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise AssetStoreError("History notes must be text")
        return value if value else None

    def _canonical_quick_date(
        self,
        value: str | None,
        *,
        code: str,
        required_code: str | None = None,
    ) -> str | None:
        """Validate one canonical Quick Create calendar date."""
        if value is None:
            if required_code is not None:
                raise AssetStoreError("A date is required", code=required_code)
            return None
        if not isinstance(value, str):
            raise AssetStoreError("Date must use YYYY-MM-DD", code=code)
        try:
            parsed = date.fromisoformat(value)
        except ValueError as err:
            raise AssetStoreError("Date must use YYYY-MM-DD", code=code) from err
        if parsed.isoformat() != value:
            raise AssetStoreError("Date must use YYYY-MM-DD", code=code)
        return value

    def _validate_quick_create_request(
        self,
        request: QuickAssetCreateRequest,
    ) -> None:
        """Validate one immutable command without resolving Store references."""
        if not isinstance(request, QuickAssetCreateRequest):
            raise AssetStoreError(
                "Quick Create request has an invalid type",
                code="invalid_quick_create_request",
            )
        if _valid_uuid(request.asset_uuid) != request.asset_uuid:
            raise AssetStoreError(
                "Quick Create Asset UUID is invalid",
                code="invalid_quick_create_request",
            )
        if set(request.metadata) != set(_USER_EDITABLE_ASSET_FIELDS):
            raise AssetStoreError(
                "Quick Create metadata is incomplete",
                code="invalid_quick_create_request",
            )
        for field, value in request.metadata.items():
            if value is not None and not isinstance(value, str):
                raise AssetStoreError(
                    f"Quick Create metadata {field} is invalid",
                    code="invalid_quick_create_request",
                )
            if field == "name":
                if not isinstance(value, str) or not value.strip():
                    raise AssetStoreError(
                        "Quick Create name is required",
                        code="invalid_quick_create_request",
                    )
                if value != value.strip():
                    raise AssetStoreError(
                        "Quick Create name is not normalized",
                        code="invalid_quick_create_request",
                    )
            elif value == "":
                raise AssetStoreError(
                    f"Quick Create metadata {field} is not normalized",
                    code="invalid_quick_create_request",
                )

        if not set(request.field_sources).issubset(_USER_EDITABLE_ASSET_FIELDS):
            raise AssetStoreError(
                "Quick Create metadata provenance is invalid",
                code="invalid_quick_create_request",
            )
        for field, source in request.field_sources.items():
            if source not in (FIELD_SOURCE_HOME_ASSISTANT, FIELD_SOURCE_USER):
                raise AssetStoreError(
                    "Quick Create metadata provenance is invalid",
                    code="invalid_quick_create_request",
                )
            if source == FIELD_SOURCE_HOME_ASSISTANT and (
                field not in _HOME_ASSISTANT_METADATA_FIELDS
                or request.metadata[field] is None
            ):
                raise AssetStoreError(
                    "Quick Create Home Assistant provenance is invalid",
                    code="invalid_quick_create_request",
                )
        for field, value in request.metadata.items():
            if value is not None and field not in request.field_sources:
                raise AssetStoreError(
                    f"Quick Create metadata {field} has no provenance",
                    code="invalid_quick_create_request",
                )

        if request.primary_device_id is not None and (
            not isinstance(request.primary_device_id, str)
            or not request.primary_device_id.strip()
            or request.primary_device_id != request.primary_device_id.strip()
        ):
            raise AssetStoreError(
                "Quick Create primary device is invalid",
                code="invalid_quick_create_request",
            )
        if request.primary_device_id is None and FIELD_SOURCE_HOME_ASSISTANT in (
            request.field_sources.values()
        ):
            raise AssetStoreError(
                "Quick Create HA provenance requires a primary device",
                code="invalid_quick_create_request",
            )
        if request.initial_lifecycle_status not in LIFECYCLE_STATUSES:
            raise AssetStoreError(
                "Quick Create Lifecycle status is invalid",
                code="invalid_lifecycle_status",
            )
        if (
            request.initial_lifecycle_status == LIFECYCLE_STATUS_UNKNOWN
            and request.initial_lifecycle_effective_date is not None
        ):
            raise AssetStoreError(
                "Initial unknown Lifecycle cannot have an effective date",
                code="lifecycle_date_not_applicable",
            )
        self._normalize_effective_date(
            request.initial_lifecycle_effective_date,
            invalid_code="invalid_lifecycle_effective_date",
            future_code="lifecycle_date_in_future",
        )
        if request.deployment_state not in DEPLOYMENT_STATES:
            raise AssetStoreError(
                "Quick Create deployment state is invalid",
                code="invalid_deployment_state",
            )
        self._canonical_quick_date(
            request.installed_date,
            code="invalid_installed_date",
        )
        if request.ha_area_id is not None and (
            not isinstance(request.ha_area_id, str)
            or not request.ha_area_id.strip()
        ):
            raise AssetStoreError("Quick Create Area is invalid", code="invalid_area")
        if (
            request.deployment_state == DEPLOYMENT_STATE_NOT_DEPLOYED
            and request.ha_area_id is not None
        ):
            raise AssetStoreError(
                "A not-deployed Asset cannot have an Area",
                code="invalid_area",
            )
        if request.warranty_type not in WARRANTY_TYPES:
            raise AssetStoreError(
                "Quick Create warranty type is invalid",
                code="invalid_warranty_type",
            )
        if request.warranty_type == WARRANTY_NONE:
            if request.warranty_until is not None:
                raise AssetStoreError(
                    "No-warranty selection cannot have an end date",
                    code="invalid_warranty_date",
                )
        elif request.warranty_type == WARRANTY_MANUAL:
            self._canonical_quick_date(
                request.warranty_until,
                code="invalid_warranty_date",
                required_code="manual_warranty_date_required",
            )
        else:
            self._canonical_quick_date(
                request.warranty_until,
                code="invalid_warranty_date",
                required_code="purchase_date_required_for_warranty",
            )

        if request.purchase_uuid is not None and (
            _valid_uuid(request.purchase_uuid) != request.purchase_uuid
        ):
            raise AssetStoreError(
                "Quick Create Purchase UUID is invalid",
                code="invalid_purchase",
            )
        if request.expected_purchase_date is not None and not isinstance(
            request.expected_purchase_date, str
        ):
            raise AssetStoreError(
                "Quick Create expected Purchase date is invalid",
                code="invalid_quick_create_request",
            )
        if not isinstance(request.retire_predecessor, bool) or not isinstance(
            request.undeploy_predecessor, bool
        ):
            raise AssetStoreError(
                "Quick Create predecessor actions are invalid",
                code="invalid_quick_create_request",
            )

        if request.predecessor_asset_uuid is None:
            replacement_values = (
                request.expected_predecessor_lifecycle_status,
                request.expected_predecessor_current_event_uuid,
                request.expected_predecessor_deployment_state,
                request.expected_predecessor_ha_area_id,
                request.replacement_reason,
                request.replacement_effective_date,
                request.replacement_notes,
            )
            if any(value is not None for value in replacement_values) or (
                request.retire_predecessor or request.undeploy_predecessor
            ):
                raise AssetStoreError(
                    "Quick Create replacement data has no predecessor",
                    code="invalid_quick_create_request",
                )
            return

        if _valid_uuid(request.predecessor_asset_uuid) != (
            request.predecessor_asset_uuid
        ):
            raise AssetStoreError(
                "Quick Create predecessor UUID is invalid",
                code="asset_missing",
            )
        if request.predecessor_asset_uuid == request.asset_uuid:
            raise AssetStoreError(
                "An Asset cannot replace itself",
                code="replacement_self_reference",
            )
        if request.expected_predecessor_lifecycle_status not in LIFECYCLE_STATUSES:
            raise AssetStoreError(
                "Quick Create expected predecessor Lifecycle is invalid",
                code="invalid_quick_create_request",
            )
        if request.expected_predecessor_current_event_uuid is not None and (
            _valid_uuid(request.expected_predecessor_current_event_uuid)
            != request.expected_predecessor_current_event_uuid
        ):
            raise AssetStoreError(
                "Quick Create expected predecessor event is invalid",
                code="invalid_quick_create_request",
            )
        if request.expected_predecessor_deployment_state not in DEPLOYMENT_STATES:
            raise AssetStoreError(
                "Quick Create expected predecessor Deployment is invalid",
                code="invalid_quick_create_request",
            )
        if request.expected_predecessor_ha_area_id is not None and (
            not isinstance(request.expected_predecessor_ha_area_id, str)
            or not request.expected_predecessor_ha_area_id.strip()
        ):
            raise AssetStoreError(
                "Quick Create expected predecessor Area is invalid",
                code="invalid_quick_create_request",
            )
        if request.replacement_reason not in REPLACEMENT_REASONS:
            raise AssetStoreError(
                "Quick Create replacement reason is invalid",
                code="invalid_replacement_reason",
            )
        self._normalize_effective_date(
            request.replacement_effective_date,
            invalid_code="invalid_replacement_effective_date",
            future_code="replacement_date_in_future",
        )
        if request.replacement_notes is not None and (
            not isinstance(request.replacement_notes, str)
            or request.replacement_notes == ""
        ):
            raise AssetStoreError(
                "Quick Create replacement notes are not canonical",
                code="invalid_quick_create_request",
            )

    def _quick_create_sources(
        self,
        request: QuickAssetCreateRequest,
    ) -> dict[str, str]:
        """Return exact initial field provenance without owning absent defaults."""
        sources = dict(request.field_sources)
        sources[CONF_DEPLOYMENT_STATE] = FIELD_SOURCE_USER
        if request.installed_date is not None:
            sources[CONF_INSTALLED_DATE] = FIELD_SOURCE_USER
        if request.ha_area_id is not None:
            sources[CONF_HA_AREA_ID] = FIELD_SOURCE_USER
        if request.warranty_type != WARRANTY_NONE:
            sources["warranty"] = FIELD_SOURCE_USER
        if request.purchase_uuid is not None:
            sources["purchase_uuid"] = FIELD_SOURCE_USER
        return sources

    def _resolve_quick_purchase_and_warranty(
        self,
        data: AssetStoreData,
        request: QuickAssetCreateRequest,
    ) -> tuple[PurchaseData | None, str | None]:
        """Resolve one current Purchase and final warranty date atomically."""
        purchase: PurchaseData | None = None
        if request.purchase_uuid is not None:
            purchase = data["purchases"].get(request.purchase_uuid)
            if purchase is None:
                raise AssetStoreError(
                    "The selected Purchase no longer exists",
                    code="purchase_missing",
                )
            if not purchase.get("configured"):
                raise AssetStoreError(
                    "The selected Purchase is not configured",
                    code="purchase_not_configured",
                )

        if request.warranty_type == WARRANTY_NONE:
            return purchase, None
        if request.warranty_type == WARRANTY_MANUAL:
            return purchase, request.warranty_until
        if purchase is None:
            raise AssetStoreError(
                "A Purchase is required for calculated warranty",
                code="purchase_date_required_for_warranty",
            )
        purchase_date = purchase.get("purchase_date")
        if purchase_date is None:
            raise AssetStoreError(
                "The Purchase has no purchase date",
                code="purchase_date_required_for_warranty",
            )
        if purchase_date != request.expected_purchase_date:
            raise AssetStoreError(
                "The Purchase date changed after review",
                code="purchase_changed",
            )
        years = 1 if request.warranty_type == WARRANTY_ONE_YEAR else 2
        warranty_until = add_calendar_years(purchase_date, years)
        if warranty_until is None:
            raise AssetStoreError(
                "The Purchase date cannot calculate warranty",
                code="invalid_warranty_date",
            )
        if request.warranty_until != warranty_until:
            raise AssetStoreError(
                "The reviewed warranty date is inconsistent",
                code="invalid_warranty_date",
            )
        return purchase, warranty_until

    def _quick_create_conflict(self, detail: str) -> None:
        """Raise the stable idempotency conflict for a mismatched replay."""
        raise AssetStoreError(
            f"Quick Create replay does not match persisted state: {detail}",
            code="quick_create_idempotency_conflict",
        )

    def _quick_initial_lifecycle_event(
        self,
        data: AssetStoreData,
        asset: AssetData,
        request: QuickAssetCreateRequest,
    ) -> LifecycleEventData | None:
        """Return the matching initial event or reject a mismatched replay."""
        asset_events = [
            event
            for event in data["lifecycle_events"].values()
            if event["asset_uuid"] == request.asset_uuid
        ]
        lifecycle = asset["lifecycle"]
        if request.initial_lifecycle_status == LIFECYCLE_STATUS_UNKNOWN:
            if lifecycle != {
                "status": LIFECYCLE_STATUS_UNKNOWN,
                "current_event_uuid": None,
            } or asset_events:
                self._quick_create_conflict("initial Lifecycle")
            return None
        if len(asset_events) != 1:
            self._quick_create_conflict("initial Lifecycle history")
        event = asset_events[0]
        if lifecycle != {
            "status": request.initial_lifecycle_status,
            "current_event_uuid": event["event_uuid"],
        } or any(
            (
                event["previous_event_uuid"] is not None,
                event["from_status"] != LIFECYCLE_STATUS_UNKNOWN,
                event["to_status"] != request.initial_lifecycle_status,
                event["effective_date"]
                != request.initial_lifecycle_effective_date,
                event["notes"] is not None,
            )
        ):
            self._quick_create_conflict("initial Lifecycle event")
        return event

    def _quick_replay_result(
        self,
        data: AssetStoreData,
        request: QuickAssetCreateRequest,
    ) -> QuickAssetCreateResult:
        """Verify and return an already persisted Quick Create final state."""
        asset = data["assets"].get(request.asset_uuid)
        if asset is None:
            raise AssetStoreError(
                "Quick Create replay Asset is missing",
                code="persistence_error",
            )
        expected_metadata = dict(request.metadata)
        expected_values: dict[str, Any] = {
            "name": expected_metadata["name"],
            "category": expected_metadata["category"],
            "manufacturer": expected_metadata["manufacturer"],
            "model": expected_metadata["model"],
            "model_id": expected_metadata["model_id"],
            "serial_number": expected_metadata["serial_number"],
            "sw_version": expected_metadata["sw_version"],
            "hw_version": expected_metadata["hw_version"],
            "notes": expected_metadata["notes"],
            "purchase_uuid": request.purchase_uuid,
            CONF_DEPLOYMENT_STATE: request.deployment_state,
            CONF_INSTALLED_DATE: request.installed_date,
            CONF_HA_AREA_ID: request.ha_area_id,
            "warranty": {
                "type": request.warranty_type,
                "until": request.warranty_until,
            },
            "runtime": {"total_seconds": None},
            "field_sources": self._quick_create_sources(request),
            "ha_device_refs": (
                []
                if request.primary_device_id is None
                else [
                    {
                        "device_id": request.primary_device_id,
                        "role": DEVICE_ROLE_PRIMARY,
                    }
                ]
            ),
        }
        if any(asset.get(field) != value for field, value in expected_values.items()):
            self._quick_create_conflict("Asset fields")

        purchase: PurchaseData | None = None
        if request.purchase_uuid is not None:
            purchase = data["purchases"].get(request.purchase_uuid)
            if (
                purchase is None
                or not purchase.get("configured")
                or purchase["asset_uuids"].count(request.asset_uuid) != 1
            ):
                self._quick_create_conflict("Purchase relationship")
        if request.warranty_type in (WARRANTY_ONE_YEAR, WARRANTY_TWO_YEARS):
            if (
                purchase is None
                or purchase.get("purchase_date") != request.expected_purchase_date
            ):
                self._quick_create_conflict("Purchase review snapshot")
            years = 1 if request.warranty_type == WARRANTY_ONE_YEAR else 2
            if add_calendar_years(request.expected_purchase_date or "", years) != (
                request.warranty_until
            ):
                self._quick_create_conflict("calculated warranty")

        initial_event = self._quick_initial_lifecycle_event(data, asset, request)
        related_records = [
            record
            for record in data["replacement_records"].values()
            if request.asset_uuid
            in (
                record["predecessor_asset_uuid"],
                record["successor_asset_uuid"],
            )
        ]
        if request.predecessor_asset_uuid is None:
            if related_records:
                self._quick_create_conflict("unexpected replacement history")
            return QuickAssetCreateResult(
                asset=asset,
                replacement=None,
                predecessor=None,
                predecessor_lifecycle_changed=False,
                predecessor_deployment_changed=False,
                predecessor_area_cleared=False,
                replayed=True,
            )

        if len(related_records) != 1:
            self._quick_create_conflict("replacement history")
        replacement = related_records[0]
        if any(
            (
                replacement["predecessor_asset_uuid"]
                != request.predecessor_asset_uuid,
                replacement["successor_asset_uuid"] != request.asset_uuid,
                replacement["reason"] != request.replacement_reason,
                replacement["effective_date"]
                != request.replacement_effective_date,
                replacement["notes"] != request.replacement_notes,
                replacement["voided_at"] is not None,
                replacement["void_reason"] is not None,
            )
        ):
            self._quick_create_conflict("replacement record")

        predecessor = data["assets"].get(request.predecessor_asset_uuid)
        if predecessor is None:
            self._quick_create_conflict("predecessor")
        lifecycle_changed = request.retire_predecessor and (
            request.expected_predecessor_lifecycle_status
            in (LIFECYCLE_STATUS_ACTIVE, LIFECYCLE_STATUS_UNKNOWN)
        )
        expected_status = (
            LIFECYCLE_STATUS_RETIRED
            if lifecycle_changed
            else request.expected_predecessor_lifecycle_status
        )
        predecessor_event: LifecycleEventData | None = None
        if predecessor["lifecycle"]["status"] != expected_status:
            self._quick_create_conflict("predecessor Lifecycle")
        if lifecycle_changed:
            predecessor_event = data["lifecycle_events"].get(
                predecessor["lifecycle"]["current_event_uuid"] or ""
            )
            if predecessor_event is None or any(
                (
                    predecessor_event["previous_event_uuid"]
                    != request.expected_predecessor_current_event_uuid,
                    predecessor_event["from_status"]
                    != request.expected_predecessor_lifecycle_status,
                    predecessor_event["to_status"] != LIFECYCLE_STATUS_RETIRED,
                    predecessor_event["effective_date"]
                    != request.replacement_effective_date,
                    predecessor_event["notes"] is not None,
                )
            ):
                self._quick_create_conflict("predecessor Lifecycle event")
        elif predecessor["lifecycle"]["current_event_uuid"] != (
            request.expected_predecessor_current_event_uuid
        ):
            self._quick_create_conflict("predecessor Lifecycle event pointer")

        deployment_changed = request.undeploy_predecessor and (
            request.expected_predecessor_deployment_state
            in (DEPLOYMENT_STATE_DEPLOYED, DEPLOYMENT_STATE_UNKNOWN)
        )
        expected_deployment = (
            DEPLOYMENT_STATE_NOT_DEPLOYED
            if deployment_changed
            else request.expected_predecessor_deployment_state
        )
        expected_area = (
            None
            if deployment_changed
            else request.expected_predecessor_ha_area_id
        )
        if (
            predecessor[CONF_DEPLOYMENT_STATE] != expected_deployment
            or predecessor[CONF_HA_AREA_ID] != expected_area
        ):
            self._quick_create_conflict("predecessor Deployment or Area")
        area_cleared = deployment_changed and (
            request.expected_predecessor_ha_area_id is not None
        )
        timestamps = [replacement["recorded_at"]]
        if initial_event is not None:
            timestamps.append(initial_event["recorded_at"])
        if predecessor_event is not None:
            timestamps.append(predecessor_event["recorded_at"])
        if len(set(timestamps)) != 1:
            self._quick_create_conflict("transaction timestamps")
        return QuickAssetCreateResult(
            asset=asset,
            replacement=replacement,
            predecessor=predecessor,
            predecessor_lifecycle_changed=lifecycle_changed,
            predecessor_deployment_changed=deployment_changed,
            predecessor_area_cleared=area_cleared,
            replayed=True,
        )

    def _quick_create_in_snapshot(
        self,
        data: AssetStoreData,
        request: QuickAssetCreateRequest,
    ) -> QuickAssetCreateResult:
        """Apply one new Quick Create command to a detached Store snapshot."""
        _purchase, warranty_until = self._resolve_quick_purchase_and_warranty(
            data,
            request,
        )
        if request.ha_area_id is not None and (
            ar.async_get(self.hass).async_get_area(request.ha_area_id) is None
        ):
            raise AssetStoreError(
                "The selected Home Assistant Area no longer exists",
                code="invalid_area",
            )
        if request.primary_device_id is not None and (
            self._find_asset_by_primary_device(data, request.primary_device_id)
            is not None
        ):
            raise AssetStoreError(
                "The Home Assistant device is already linked",
                code="device_already_linked",
            )

        predecessor: AssetData | None = None
        if request.predecessor_asset_uuid is not None:
            predecessor = self._require_asset(data, request.predecessor_asset_uuid)
            lifecycle = predecessor["lifecycle"]
            if (
                lifecycle["status"]
                != request.expected_predecessor_lifecycle_status
                or lifecycle["current_event_uuid"]
                != request.expected_predecessor_current_event_uuid
                or predecessor[CONF_DEPLOYMENT_STATE]
                != request.expected_predecessor_deployment_state
                or predecessor[CONF_HA_AREA_ID]
                != request.expected_predecessor_ha_area_id
            ):
                raise AssetStoreError(
                    "The predecessor changed after review",
                    code="predecessor_changed",
                )
            current_event = data["lifecycle_events"].get(
                lifecycle["current_event_uuid"] or ""
            )
            if (
                request.retire_predecessor
                and lifecycle["status"]
                in (LIFECYCLE_STATUS_ACTIVE, LIFECYCLE_STATUS_UNKNOWN)
                and request.replacement_effective_date is not None
                and current_event is not None
                and current_event["effective_date"] is not None
                and request.replacement_effective_date
                < current_event["effective_date"]
            ):
                raise AssetStoreError(
                    "Replacement date precedes the predecessor Lifecycle date",
                    code="replacement_date_before_predecessor_lifecycle",
                )

        recorded_at = datetime.now(UTC).isoformat()
        asset = self._create_asset_in_snapshot(
            data,
            asset_uuid=request.asset_uuid,
            name=cast(str, request.metadata["name"]),
            metadata=dict(request.metadata),
            field_sources=self._quick_create_sources(request),
            primary_device_id=request.primary_device_id,
            deployment_state=request.deployment_state,
            installed_date=request.installed_date,
            ha_area_id=request.ha_area_id,
            warranty_type=request.warranty_type,
            warranty_until=warranty_until,
            initial_lifecycle_status=request.initial_lifecycle_status,
            lifecycle_effective_date=request.initial_lifecycle_effective_date,
            recorded_at=recorded_at,
        )
        if request.purchase_uuid is not None:
            self._assign_asset_purchase_in_snapshot(
                data,
                asset,
                request.purchase_uuid,
            )

        lifecycle_changed = False
        deployment_changed = False
        area_cleared = False
        replacement: ReplacementRecordData | None = None
        if predecessor is not None:
            if request.retire_predecessor and predecessor["lifecycle"]["status"] in (
                LIFECYCLE_STATUS_ACTIVE,
                LIFECYCLE_STATUS_UNKNOWN,
            ):
                lifecycle_changed = self._append_lifecycle_transition(
                    data,
                    predecessor,
                    LIFECYCLE_STATUS_RETIRED,
                    effective_date=request.replacement_effective_date,
                    notes=None,
                    recorded_at=recorded_at,
                )
            if request.undeploy_predecessor and predecessor[
                CONF_DEPLOYMENT_STATE
            ] in (DEPLOYMENT_STATE_DEPLOYED, DEPLOYMENT_STATE_UNKNOWN):
                updates: dict[str, str | None | object] = {
                    CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED,
                }
                if predecessor[CONF_HA_AREA_ID] is not None:
                    updates[CONF_HA_AREA_ID] = None
                    area_cleared = True
                self._set_asset_deployment_in_snapshot(predecessor, updates)
                deployment_changed = True
            replacement = self._create_replacement_record(
                data,
                predecessor_asset_uuid=predecessor["asset_uuid"],
                successor_asset_uuid=asset["asset_uuid"],
                reason=cast(ReplacementReason, request.replacement_reason),
                effective_date=request.replacement_effective_date,
                notes=request.replacement_notes,
                recorded_at=recorded_at,
            )
        return QuickAssetCreateResult(
            asset=asset,
            replacement=replacement,
            predecessor=predecessor,
            predecessor_lifecycle_changed=lifecycle_changed,
            predecessor_deployment_changed=deployment_changed,
            predecessor_area_cleared=area_cleared,
            replayed=False,
        )

    async def async_quick_create_asset(
        self,
        request: QuickAssetCreateRequest,
    ) -> QuickAssetCreateResult:
        """Create one complete Asset outcome in exactly one durable transaction."""
        async with self._mutation_lock:
            await self._async_recover_uncertain_persistence()
            self._validate_quick_create_request(request)
            data = deepcopy(self._data)
            if request.asset_uuid in data["assets"]:
                return deepcopy(self._quick_replay_result(data, request))

            result = self._quick_create_in_snapshot(data, request)
            _validate_store_data(data)
            try:
                await self._store.async_save(data)
            except AssetStorePersistenceError as err:
                if not err.ambiguous:
                    raise
                self._persistence_uncertain = True
                try:
                    persisted = await self._store.async_load_persisted_snapshot()
                    if persisted is None:
                        if self._data != _empty_store_data():
                            raise AssetStorePersistenceError(
                                "Asset Core persistence disappeared during recovery",
                                ambiguous=True,
                            )
                        self._persistence_uncertain = False
                        raise AssetStorePersistenceError(
                            "Quick Create was not persisted"
                        )
                    _validate_store_data(persisted)
                except AssetStorePersistenceError:
                    raise
                self._data = persisted
                self._persistence_uncertain = False
                if request.asset_uuid not in persisted["assets"]:
                    raise AssetStorePersistenceError(
                        "Quick Create was not persisted"
                    )
                return deepcopy(self._quick_replay_result(persisted, request))
            except OSError as err:
                raise AssetStorePersistenceError(
                    "Quick Create could not be persisted"
                ) from err

            self._data = data
            return deepcopy(result)

    async def async_set_asset_lifecycle(
        self,
        asset_uuid: str,
        status: LifecycleStatus,
        *,
        effective_date: str | None,
        notes: str | None,
    ) -> AssetData:
        """Append one canonical lifecycle transition and update current state."""

        def _set_lifecycle(data: AssetStoreData) -> AssetData:
            asset = self._require_asset(data, asset_uuid)
            self._append_lifecycle_transition(
                data,
                asset,
                status,
                effective_date=effective_date,
                notes=notes,
            )
            return asset

        return await self._async_mutate_history(_set_lifecycle)

    def lifecycle_event(self, event_uuid: str | None) -> LifecycleEventData | None:
        """Return one detached immutable lifecycle event snapshot."""
        if not event_uuid:
            return None
        event = self._data["lifecycle_events"].get(event_uuid)
        return deepcopy(event) if event is not None else None

    def lifecycle_events_for_asset(
        self,
        asset_uuid: str,
    ) -> list[LifecycleEventData]:
        """Return one Asset's detached event chain in recorded chain order."""
        asset = self.asset(asset_uuid)
        if asset is None:
            raise AssetStoreError(
                f"Asset {asset_uuid} does not exist",
                code="asset_missing",
            )
        result: list[LifecycleEventData] = []
        current_uuid = asset["lifecycle"]["current_event_uuid"]
        while current_uuid is not None:
            event = self._data["lifecycle_events"][current_uuid]
            result.append(event)
            current_uuid = event["previous_event_uuid"]
        result.reverse()
        return deepcopy(result)

    def replacement_record(
        self,
        replacement_uuid: str | None,
    ) -> ReplacementRecordData | None:
        """Return one detached historical replacement record."""
        if not replacement_uuid:
            return None
        record = self._data["replacement_records"].get(replacement_uuid)
        return deepcopy(record) if record is not None else None

    def replacement_records_for_asset(
        self,
        asset_uuid: str,
        *,
        include_voided: bool = False,
    ) -> list[ReplacementRecordData]:
        """Return detached replacement records mentioning one Asset."""
        if self.asset(asset_uuid) is None:
            raise AssetStoreError(
                f"Asset {asset_uuid} does not exist",
                code="asset_missing",
            )
        records = [
            record
            for record in self._data["replacement_records"].values()
            if (
                record["predecessor_asset_uuid"] == asset_uuid
                or record["successor_asset_uuid"] == asset_uuid
            )
            and (include_voided or record["voided_at"] is None)
        ]
        records.sort(key=lambda item: (item["recorded_at"], item["replacement_uuid"]))
        return deepcopy(records)

    def active_replacement_predecessor(
        self,
        asset_uuid: str,
    ) -> AssetData | None:
        """Return the detached Asset actively replaced by this Asset."""
        if self.asset(asset_uuid) is None:
            raise AssetStoreError(
                f"Asset {asset_uuid} does not exist",
                code="asset_missing",
            )
        for record in self._data["replacement_records"].values():
            if (
                record["voided_at"] is None
                and record["successor_asset_uuid"] == asset_uuid
            ):
                return self.asset(record["predecessor_asset_uuid"])
        return None

    def active_replacement_successor(
        self,
        asset_uuid: str,
    ) -> AssetData | None:
        """Return the detached Asset actively replacing this Asset."""
        if self.asset(asset_uuid) is None:
            raise AssetStoreError(
                f"Asset {asset_uuid} does not exist",
                code="asset_missing",
            )
        for record in self._data["replacement_records"].values():
            if (
                record["voided_at"] is None
                and record["predecessor_asset_uuid"] == asset_uuid
            ):
                return self.asset(record["successor_asset_uuid"])
        return None

    def _create_replacement_record(
        self,
        data: AssetStoreData,
        *,
        predecessor_asset_uuid: str,
        successor_asset_uuid: str,
        reason: ReplacementReason,
        effective_date: str | None,
        notes: str | None,
        recorded_at: str | None = None,
    ) -> ReplacementRecordData:
        """Create one active record inside an existing atomic mutation."""
        self._require_asset(data, predecessor_asset_uuid)
        self._require_asset(data, successor_asset_uuid)
        if predecessor_asset_uuid == successor_asset_uuid:
            raise AssetStoreError(
                "An Asset cannot replace itself",
                code="replacement_self_reference",
            )
        if reason not in REPLACEMENT_REASONS:
            raise AssetStoreError(
                f"Invalid replacement reason: {reason}",
                code="invalid_replacement_reason",
            )
        normalized_date = self._normalize_effective_date(
            effective_date,
            invalid_code="invalid_replacement_effective_date",
            future_code="replacement_date_in_future",
        )
        normalized_notes = self._normalize_history_notes(notes)
        replacement_uuid = str(uuid.uuid4())
        if replacement_uuid in data["replacement_records"]:
            raise AssetStoreError(
                "Generated replacement UUID is already in use",
                code="replacement_graph_invalid",
            )
        record: ReplacementRecordData = {
            "replacement_uuid": replacement_uuid,
            "predecessor_asset_uuid": predecessor_asset_uuid,
            "successor_asset_uuid": successor_asset_uuid,
            "reason": reason,
            "effective_date": normalized_date,
            "recorded_at": recorded_at or datetime.now(UTC).isoformat(),
            "notes": normalized_notes,
            "voided_at": None,
            "void_reason": None,
        }
        data["replacement_records"][replacement_uuid] = record
        return record

    async def async_create_asset_replacement(
        self,
        predecessor_asset_uuid: str,
        successor_asset_uuid: str,
        *,
        reason: ReplacementReason,
        effective_date: str | None,
        notes: str | None,
    ) -> ReplacementRecordData:
        """Atomically create one physical Asset replacement relationship."""
        return await self._async_mutate_history(
            lambda data: self._create_replacement_record(
                data,
                predecessor_asset_uuid=predecessor_asset_uuid,
                successor_asset_uuid=successor_asset_uuid,
                reason=reason,
                effective_date=effective_date,
                notes=notes,
            )
        )

    async def async_void_asset_replacement(
        self,
        replacement_uuid: str,
        *,
        void_reason: str,
    ) -> ReplacementRecordData:
        """Atomically void an active relationship without deleting history."""

        def _void(data: AssetStoreData) -> ReplacementRecordData:
            if _valid_uuid(replacement_uuid) != replacement_uuid:
                raise AssetStoreError(
                    f"Replacement {replacement_uuid} does not exist",
                    code="replacement_missing",
                )
            record = data["replacement_records"].get(replacement_uuid)
            if record is None:
                raise AssetStoreError(
                    f"Replacement {replacement_uuid} does not exist",
                    code="replacement_missing",
                )
            if record["voided_at"] is not None:
                raise AssetStoreError(
                    f"Active replacement {replacement_uuid} does not exist",
                    code="replacement_missing",
                )
            if not isinstance(void_reason, str) or not void_reason.strip():
                raise AssetStoreError(
                    "A non-empty void reason is required",
                    code="replacement_void_reason_required",
                )
            record["voided_at"] = datetime.now(UTC).isoformat()
            record["void_reason"] = void_reason.strip()
            return record

        return await self._async_mutate_history(_void)

    async def async_correct_asset_replacement(
        self,
        replacement_uuid: str,
        *,
        predecessor_asset_uuid: str,
        successor_asset_uuid: str,
        reason: ReplacementReason,
        effective_date: str | None,
        notes: str | None,
        void_reason: str,
    ) -> ReplacementRecordData:
        """Atomically void one active record and create its correction."""

        def _correct(data: AssetStoreData) -> ReplacementRecordData:
            if _valid_uuid(replacement_uuid) != replacement_uuid:
                raise AssetStoreError(
                    f"Replacement {replacement_uuid} does not exist",
                    code="replacement_missing",
                )
            old_record = data["replacement_records"].get(replacement_uuid)
            if old_record is None or old_record["voided_at"] is not None:
                raise AssetStoreError(
                    f"Active replacement {replacement_uuid} does not exist",
                    code="replacement_missing",
                )
            if not isinstance(void_reason, str) or not void_reason.strip():
                raise AssetStoreError(
                    "A non-empty void reason is required",
                    code="replacement_void_reason_required",
                )
            old_record["voided_at"] = datetime.now(UTC).isoformat()
            old_record["void_reason"] = void_reason.strip()
            return self._create_replacement_record(
                data,
                predecessor_asset_uuid=predecessor_asset_uuid,
                successor_asset_uuid=successor_asset_uuid,
                reason=reason,
                effective_date=effective_date,
                notes=notes,
            )

        return await self._async_mutate_history(_correct)

    async def async_update_asset_metadata(
        self,
        asset_uuid: str,
        *,
        name: str | object = _UNSET,
        category: str | None | object = _UNSET,
        manufacturer: str | None | object = _UNSET,
        model: str | None | object = _UNSET,
        model_id: str | None | object = _UNSET,
        serial_number: str | None | object = _UNSET,
        sw_version: str | None | object = _UNSET,
        hw_version: str | None | object = _UNSET,
        notes: str | None | object = _UNSET,
    ) -> AssetData:
        """Apply user-owned physical metadata without changing Asset identity."""
        values = {
            "name": name,
            "category": category,
            "manufacturer": manufacturer,
            "model": model,
            "model_id": model_id,
            "serial_number": serial_number,
            "sw_version": sw_version,
            "hw_version": hw_version,
            "notes": notes,
        }
        normalized: dict[str, str | None] = {}
        for field in _USER_EDITABLE_ASSET_FIELDS:
            value = values[field]
            if value is _UNSET:
                continue
            normalized[field] = self._normalize_user_asset_text(
                value,
                field,
                required=field == "name",
            )

        def _update(data: AssetStoreData) -> AssetData:
            asset = self._require_asset(data, asset_uuid)
            sources = asset.setdefault("field_sources", {})
            for field, value in normalized.items():
                asset[field] = value  # type: ignore[literal-required]
                sources[field] = FIELD_SOURCE_USER
            return asset

        return await self._async_mutate(_update)

    async def async_set_asset_purchase(
        self,
        asset_uuid: str,
        purchase_uuid: str | None,
    ) -> AssetData:
        """Atomically assign or clear both sides of a Purchase relationship."""

        def _set_purchase(data: AssetStoreData) -> AssetData:
            asset = self._require_asset(data, asset_uuid)
            return self._assign_asset_purchase_in_snapshot(
                data,
                asset,
                purchase_uuid,
            )

        return await self._async_mutate(_set_purchase)

    def _assign_asset_purchase_in_snapshot(
        self,
        data: AssetStoreData,
        asset: AssetData,
        purchase_uuid: str | None,
    ) -> AssetData:
        """Assign both sides of a Purchase link inside one Store snapshot."""
        asset_uuid = asset["asset_uuid"]
        old_purchase_uuid = asset.get("purchase_uuid")
        target: PurchaseData | None = None

        if purchase_uuid is not None:
            target = data["purchases"].get(purchase_uuid)
            if target is None:
                raise AssetStoreError(f"Purchase {purchase_uuid} does not exist")
            if not target.get("configured") and old_purchase_uuid != purchase_uuid:
                raise AssetStoreError(
                    f"Purchase {purchase_uuid} is not currently configured"
                )

        if old_purchase_uuid is not None and old_purchase_uuid != purchase_uuid:
            old_purchase = data["purchases"].get(old_purchase_uuid)
            if old_purchase is not None:
                old_purchase["asset_uuids"] = [
                    member_uuid
                    for member_uuid in old_purchase.get("asset_uuids", [])
                    if member_uuid != asset_uuid
                ]

        asset["purchase_uuid"] = purchase_uuid
        asset.setdefault("field_sources", {})[
            "purchase_uuid"
        ] = FIELD_SOURCE_USER

        if target is not None and asset_uuid not in target["asset_uuids"]:
            target["asset_uuids"].append(asset_uuid)

        return asset

    async def async_set_asset_deployment(
        self,
        asset_uuid: str,
        *,
        deployment_state: str | object = _UNSET,
        installed_date: str | None | object = _UNSET,
        ha_area_id: str | None | object = _UNSET,
    ) -> AssetData:
        """Set explicit deployment metadata without inference or HA writes."""
        updates = {
            CONF_DEPLOYMENT_STATE: deployment_state,
            CONF_INSTALLED_DATE: installed_date,
            CONF_HA_AREA_ID: ha_area_id,
        }

        def _set_deployment(data: AssetStoreData) -> AssetData:
            asset = self._require_asset(data, asset_uuid)
            return self._set_asset_deployment_in_snapshot(asset, updates)

        return await self._async_mutate(_set_deployment)

    def _set_asset_deployment_in_snapshot(
        self,
        asset: AssetData,
        updates: dict[str, str | None | object],
    ) -> AssetData:
        """Set user-owned deployment fields inside one Store snapshot."""
        sources = asset.setdefault("field_sources", {})
        for field, value in updates.items():
            if value is _UNSET:
                continue
            asset[field] = value  # type: ignore[literal-required]
            sources[field] = FIELD_SOURCE_USER
        return asset

    async def async_link_asset_device(
        self,
        asset_uuid: str,
        device_id: str,
        *,
        replace: bool = False,
        device: dr.DeviceEntry | None = None,
        expected_current_device_id: str | None | object = _UNSET,
    ) -> AssetData:
        """Atomically set a primary HA reference without mutating the registry."""
        if not isinstance(device_id, str) or not device_id.strip():
            raise AssetStoreError("Home Assistant device ID is required")
        device_id = device_id.strip()

        def _link(data: AssetStoreData) -> AssetData:
            asset = self._require_asset(data, asset_uuid)
            current = _primary_device_id(asset)
            if (
                expected_current_device_id is not _UNSET
                and current != expected_current_device_id
            ):
                raise AssetStoreError(
                    f"Asset {asset_uuid} primary HA relationship changed"
                )
            if current not in (None, device_id) and not replace:
                raise AssetStoreError(
                    f"Asset {asset_uuid} is already linked to HA device {current}"
                )

            existing = self._find_asset_by_primary_device(data, device_id)
            if existing is not None and existing["asset_uuid"] != asset_uuid:
                raise AssetStoreError(
                    f"HA device {device_id} is already linked to another Asset"
                )

            self._ensure_primary_reference(asset, device_id)
            if device is not None:
                self._refresh_home_assistant_metadata(asset, device, device_id)
            return asset

        return await self._async_mutate(_link)

    async def async_unlink_asset_device(
        self,
        asset_uuid: str,
        *,
        expected_device_id: str | None | object = _UNSET,
    ) -> AssetData:
        """Atomically remove only the primary stored HA relationship."""

        def _unlink(data: AssetStoreData) -> AssetData:
            asset = self._require_asset(data, asset_uuid)
            current = _primary_device_id(asset)
            if expected_device_id is not _UNSET and current != expected_device_id:
                raise AssetStoreError(
                    f"Asset {asset_uuid} primary HA relationship changed"
                )
            asset["ha_device_refs"] = [
                reference
                for reference in asset.get("ha_device_refs", [])
                if reference.get("role") != DEVICE_ROLE_PRIMARY
            ]
            return asset

        return await self._async_mutate(_unlink)

    async def async_add_related_device(
        self,
        asset_uuid: str,
        device_id: str,
    ) -> AssetData:
        """Atomically add one non-exclusive related HA device reference."""
        if not isinstance(device_id, str) or not device_id.strip():
            raise AssetStoreError("Home Assistant device ID is required")
        device_id = device_id.strip()

        def _add_related(data: AssetStoreData) -> AssetData:
            asset = self._require_asset(data, asset_uuid)
            for reference in asset.get("ha_device_refs", []):
                if reference.get("device_id") != device_id:
                    continue
                if reference.get("role") == DEVICE_ROLE_PRIMARY:
                    raise AssetStoreError(
                        f"HA device {device_id} is already primary for Asset "
                        f"{asset['asset_id']}"
                    )
                return asset

            asset["ha_device_refs"].append(
                {"device_id": device_id, "role": DEVICE_ROLE_RELATED}
            )
            return asset

        return await self._async_mutate(_add_related)

    async def async_remove_related_device(
        self,
        asset_uuid: str,
        device_id: str,
    ) -> AssetData:
        """Atomically remove only one exact related HA device reference."""
        if not isinstance(device_id, str) or not device_id.strip():
            raise AssetStoreError("Home Assistant device ID is required")
        device_id = device_id.strip()

        def _remove_related(data: AssetStoreData) -> AssetData:
            asset = self._require_asset(data, asset_uuid)
            asset["ha_device_refs"] = [
                reference
                for reference in asset.get("ha_device_refs", [])
                if not (
                    reference.get("role") == DEVICE_ROLE_RELATED
                    and reference.get("device_id") == device_id
                )
            ]
            return asset

        return await self._async_mutate(_remove_related)

    def _ensure_primary_reference(self, asset: AssetData, device_id: str) -> None:
        """Set a primary HA relationship while preserving future related links."""
        related: list[HADeviceReference] = [
            reference
            for reference in asset.get("ha_device_refs", [])
            if reference.get("role") != DEVICE_ROLE_PRIMARY
            and reference.get("device_id") != device_id
        ]
        asset["ha_device_refs"] = [
            {"device_id": device_id, "role": DEVICE_ROLE_PRIMARY},
            *related,
        ]

    def _refresh_home_assistant_metadata(
        self,
        asset: AssetData,
        device: dr.DeviceEntry | None,
        fallback_name: str,
    ) -> None:
        """Refresh HA-owned metadata without overwriting future user overrides."""
        if device is None:
            if not asset.get("name"):
                asset["name"] = fallback_name
            return

        discovered = home_assistant_asset_metadata(device, fallback_name)
        sources = asset.setdefault("field_sources", {})

        for field, value in discovered.items():
            if value in (None, ""):
                continue
            if sources.get(field) == FIELD_SOURCE_USER:
                continue
            asset[field] = str(value)  # type: ignore[literal-required]
            sources[field] = FIELD_SOURCE_HOME_ASSISTANT

    def _apply_purchase_asset_fields(
        self,
        asset: AssetData,
        subentry_data: dict[str, Any],
    ) -> None:
        """Normalize legacy shared purchase fields onto each Asset.

        Future Asset-level user edits can mark these fields as ``user`` and are
        then protected from the legacy purchase projection during reconciliation.
        """
        sources = asset.setdefault("field_sources", {})

        if sources.get("installed_date") != FIELD_SOURCE_USER:
            asset["installed_date"] = _optional_text(
                subentry_data.get(CONF_INSTALLED_DATE)
            )
            sources["installed_date"] = FIELD_SOURCE_PURCHASE

        if sources.get("warranty") != FIELD_SOURCE_USER:
            warranty_type = _warranty_type(subentry_data)
            asset["warranty"] = {
                "type": warranty_type,
                "until": (
                    None
                    if warranty_type == WARRANTY_NONE
                    else _optional_text(subentry_data.get(CONF_WARRANTY_UNTIL))
                ),
            }
            sources["warranty"] = FIELD_SOURCE_PURCHASE

    def _new_purchase(
        self,
        purchase_uuid: str,
        subentry_id: str,
        currency: str,
    ) -> PurchaseData:
        """Create an empty normalized purchase transaction."""
        return {
            "purchase_uuid": purchase_uuid,
            "config_subentry_id": subentry_id,
            "configured": True,
            "name": None,
            "purchase_date": None,
            "seller": None,
            "total_price": None,
            "currency": currency,
            "receipt_reference": None,
            "receipt_url": None,
            "notes": None,
            "asset_uuids": [],
        }

    def _find_purchase_by_subentry(
        self,
        data: AssetStoreData,
        subentry_id: str,
    ) -> PurchaseData | None:
        """Find a normalized purchase by its HA configuration subentry."""
        for purchase in data["purchases"].values():
            if purchase.get("config_subentry_id") == subentry_id:
                return purchase
        return None

    def _purchase_from_subentry(
        self,
        data: AssetStoreData,
        subentry_id: str,
        subentry_data: dict[str, Any],
    ) -> PurchaseData:
        """Resolve/create the stable Purchase represented by a subentry."""
        referenced_uuid = _valid_uuid(subentry_data.get(CONF_PURCHASE_UUID))
        purchase: PurchaseData | None = None

        if referenced_uuid is not None:
            purchase = data["purchases"].get(referenced_uuid)

        if purchase is None:
            purchase = self._find_purchase_by_subentry(data, subentry_id)

        if purchase is None:
            purchase_uuid = referenced_uuid or str(uuid4())
            purchase = self._new_purchase(
                purchase_uuid,
                subentry_id,
                str(subentry_data.get(CONF_CURRENCY) or self.hass.config.currency),
            )
            data["purchases"][purchase_uuid] = purchase

        return purchase

    def _remove_asset_from_previous_purchase(
        self,
        data: AssetStoreData,
        asset: AssetData,
        new_purchase_uuid: str,
    ) -> None:
        """Move an Asset out of its previous Purchase without deleting history."""
        old_purchase_uuid = asset.get("purchase_uuid")
        if old_purchase_uuid is None or old_purchase_uuid == new_purchase_uuid:
            return
        old_purchase = data["purchases"].get(old_purchase_uuid)
        if old_purchase is not None:
            old_purchase["asset_uuids"] = [
                asset_uuid
                for asset_uuid in old_purchase.get("asset_uuids", [])
                if asset_uuid != asset["asset_uuid"]
            ]

    def _assign_reconciled_purchase(
        self,
        data: AssetStoreData,
        asset: AssetData,
        purchase_uuid: str,
        device_id: str,
    ) -> None:
        """Apply legacy device membership without overriding user ownership."""
        old_purchase_uuid = asset.get("purchase_uuid")
        purchase_source = asset.get("field_sources", {}).get("purchase_uuid")

        if purchase_source == FIELD_SOURCE_USER:
            if old_purchase_uuid != purchase_uuid:
                relationship = old_purchase_uuid or "no Purchase"
                raise AssetStoreError(
                    f"HA device {device_id} belongs to Asset {asset['asset_id']}, "
                    f"which has a user-managed relationship to {relationship}; "
                    f"it cannot be assigned to Purchase {purchase_uuid}"
                )
            return

        self._remove_asset_from_previous_purchase(
            data,
            asset,
            purchase_uuid,
        )
        asset["purchase_uuid"] = purchase_uuid
        asset.setdefault("field_sources", {})[
            "purchase_uuid"
        ] = FIELD_SOURCE_PURCHASE

    def _reconcile_entry_data(
        self,
        data: AssetStoreData,
        entry: ConfigEntry,
        device_registry: dr.DeviceRegistry,
    ) -> dict[str, dict[str, Any]]:
        """Reconcile config subentries into one transaction snapshot."""
        subentry_updates: dict[str, dict[str, Any]] = {}
        touched_purchase_uuids: set[str] = set()

        for purchase in data["purchases"].values():
            purchase["configured"] = False

        purchase_subentries = sorted(
            (
                subentry
                for subentry in entry.subentries.values()
                if subentry.subentry_type == SUBENTRY_TYPE_PURCHASE
            ),
            key=lambda subentry: subentry.subentry_id,
        )

        for subentry in purchase_subentries:
            raw = dict(subentry.data)
            purchase = self._purchase_from_subentry(
                data,
                subentry.subentry_id,
                raw,
            )
            purchase_uuid = purchase["purchase_uuid"]

            if purchase_uuid in touched_purchase_uuids:
                raise AssetStoreError(
                    f"Purchase {purchase_uuid} is referenced by multiple subentries"
                )
            touched_purchase_uuids.add(purchase_uuid)

            old_member_list = list(purchase.get("asset_uuids", []))
            old_members = set(old_member_list)
            preserved_user_members: list[str] = []
            for asset_uuid in old_member_list:
                member = data["assets"].get(asset_uuid)
                if (
                    member is not None
                    and member.get("purchase_uuid") == purchase_uuid
                    and member.get("field_sources", {}).get("purchase_uuid")
                    == FIELD_SOURCE_USER
                ):
                    preserved_user_members.append(asset_uuid)
            new_members: list[str] = []

            for device_id_value in raw.get(CONF_DEVICE_IDS, []):
                device_id = str(device_id_value)
                device = device_registry.async_get(device_id)
                asset = self._find_asset_by_primary_device(data, device_id)
                if asset is None:
                    asset = self._new_asset(data, device_id, device)
                else:
                    self._ensure_primary_reference(asset, device_id)
                    self._refresh_home_assistant_metadata(asset, device, device_id)

                self._assign_reconciled_purchase(
                    data,
                    asset,
                    purchase_uuid,
                    device_id,
                )
                self._apply_purchase_asset_fields(asset, raw)
                new_members.append(asset["asset_uuid"])

            for asset_uuid in preserved_user_members:
                if asset_uuid not in new_members:
                    new_members.append(asset_uuid)

            for asset_uuid in old_members.difference(new_members):
                old_asset = data["assets"].get(asset_uuid)
                if (
                    old_asset is not None
                    and old_asset.get("purchase_uuid") == purchase_uuid
                    and old_asset.get("field_sources", {}).get("purchase_uuid")
                    == FIELD_SOURCE_PURCHASE
                ):
                    old_asset["purchase_uuid"] = None

            purchase.update(
                {
                    "config_subentry_id": subentry.subentry_id,
                    "configured": True,
                    "name": _optional_text(raw.get(CONF_PURCHASE_NAME)),
                    "purchase_date": _optional_text(raw.get(CONF_PURCHASE_DATE)),
                    "seller": _optional_text(raw.get(CONF_SELLER)),
                    "total_price": _normalize_price(
                        raw.get(CONF_PURCHASE_PRICE)
                    ),
                    "currency": str(
                        raw.get(CONF_CURRENCY) or self.hass.config.currency
                    ),
                    "receipt_reference": _optional_text(
                        raw.get(CONF_RECEIPT_REFERENCE)
                    ),
                    "receipt_url": _optional_text(raw.get(CONF_RECEIPT_URL)),
                    "notes": _optional_text(raw.get(CONF_NOTES)),
                    "asset_uuids": new_members,
                }
            )

            updated_raw = dict(raw)
            updated_raw[CONF_PURCHASE_UUID] = purchase_uuid
            updated_raw[CONF_CURRENCY] = purchase["currency"]
            if updated_raw != raw:
                subentry_updates[subentry.subentry_id] = updated_raw

        runtime_subentries = sorted(
            (
                subentry
                for subentry in entry.subentries.values()
                if subentry.subentry_type == SUBENTRY_TYPE_RUNTIME
            ),
            key=lambda subentry: subentry.subentry_id,
        )

        for subentry in runtime_subentries:
            raw = dict(subentry.data)
            device_id = str(raw.get(CONF_DEVICE_ID) or "")
            if not device_id:
                # Preserve malformed legacy configuration for the UI to repair;
                # do not fabricate an Asset without an identity relationship.
                continue

            asset: AssetData | None = None
            referenced_asset_uuid = _valid_uuid(raw.get(CONF_ASSET_UUID))
            if referenced_asset_uuid is not None:
                candidate = data["assets"].get(referenced_asset_uuid)
                if candidate is not None and _primary_device_id(candidate) in (
                    None,
                    device_id,
                ):
                    asset = candidate

            if asset is None:
                asset = self._find_asset_by_primary_device(data, device_id)

            device = device_registry.async_get(device_id)
            if asset is None:
                asset = self._new_asset(data, device_id, device)
            else:
                self._ensure_primary_reference(asset, device_id)
                self._refresh_home_assistant_metadata(asset, device, device_id)

            updated_raw = dict(raw)
            updated_raw[CONF_ASSET_UUID] = asset["asset_uuid"]
            if updated_raw != raw:
                subentry_updates[subentry.subentry_id] = updated_raw

        return subentry_updates

    async def async_reconcile_entry(self, entry: ConfigEntry) -> None:
        """Normalize current 0.4.x/0.5.x subentries into Asset Core storage.

        Storage is written before adding the generated UUID references back to
        config subentries. If a later config-entry write fails, the next setup can
        recover the same objects by subentry/device relationship instead of
        allocating new Asset IDs.
        """
        device_registry = dr.async_get(self.hass)
        subentry_updates = await self._async_mutate(
            lambda data: self._reconcile_entry_data(
                data,
                entry,
                device_registry,
            )
        )

        # Only after the normalized private store is safely written do we persist
        # the generated stable references into HA config subentries.
        for subentry_id, updated_data in subentry_updates.items():
            current_subentry = entry.subentries.get(subentry_id)
            if current_subentry is None:
                continue
            self.hass.config_entries.async_update_subentry(
                entry,
                current_subentry,
                data=updated_data,
            )

    def asset(self, asset_uuid: str | None) -> AssetData | None:
        """Return a detached snapshot of one Asset."""
        if not asset_uuid:
            return None
        asset = self._data["assets"].get(asset_uuid)
        return deepcopy(asset) if asset is not None else None

    def asset_for_primary_device_id(self, device_id: str) -> AssetData | None:
        """Return a detached Asset snapshot for a primary HA device."""
        asset = self._find_asset_by_primary_device(self._data, device_id)
        return deepcopy(asset) if asset is not None else None

    def purchase(self, purchase_uuid: str | None) -> PurchaseData | None:
        """Return a detached snapshot of one Purchase."""
        if not purchase_uuid:
            return None
        purchase = self._data["purchases"].get(purchase_uuid)
        return deepcopy(purchase) if purchase is not None else None

    def purchase_for_subentry(self, subentry_id: str) -> PurchaseData | None:
        """Return the configured Purchase represented by a subentry."""
        purchase = self._find_purchase_by_subentry(self._data, subentry_id)
        if purchase is None or not purchase.get("configured"):
            return None
        return deepcopy(purchase)

    def assets(self) -> list[AssetData]:
        """Return detached snapshots of all persistent Assets."""
        return deepcopy(list(self._data["assets"].values()))

    def purchases(self) -> list[PurchaseData]:
        """Return detached snapshots of all persistent Purchases."""
        return deepcopy(list(self._data["purchases"].values()))

    @property
    def asset_count(self) -> int:
        """Return number of persistent Assets."""
        return len(self._data["assets"])
