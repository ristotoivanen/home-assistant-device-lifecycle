"""Versioned persistent Asset Core storage for Device Lifecycle."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import os
import re
from typing import Any, Callable, TypeVar, cast
from uuid import UUID, uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store

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
    DEPLOYMENT_STATES,
    DEPLOYMENT_STATE_NOT_DEPLOYED,
    DEPLOYMENT_STATE_UNKNOWN,
    DOMAIN,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
    WARRANTY_MANUAL,
    WARRANTY_NONE,
    WARRANTY_TYPES,
)
from .models import AssetData, AssetStoreData, HADeviceReference, PurchaseData

STORAGE_VERSION = 1
STORAGE_MINOR_VERSION = 2
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


class AssetStoreError(HomeAssistantError):
    """Raised when Asset Core storage cannot be safely reconciled."""


class DeviceLifecycleStore(Store[AssetStoreData]):
    """Home Assistant Store with an explicit migration hook from version one."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize private, atomic Asset Core storage."""
        super().__init__(
            hass,
            STORAGE_VERSION,
            STORAGE_KEY,
            private=True,
            atomic_writes=True,
            minor_version=STORAGE_MINOR_VERSION,
        )

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: AssetStoreData,
    ) -> AssetStoreData:
        """Migrate Asset Core storage without changing persistent identity."""
        if old_major_version != STORAGE_VERSION:
            raise NotImplementedError

        if old_minor_version == STORAGE_MINOR_VERSION:
            return old_data

        if old_minor_version == 1:
            data = deepcopy(old_data)
            assets = data.get("assets")
            if not isinstance(assets, dict):
                return data

            for asset in assets.values():
                if not isinstance(asset, dict):
                    continue

                asset.setdefault(CONF_DEPLOYMENT_STATE, DEPLOYMENT_STATE_UNKNOWN)
                asset.setdefault(CONF_HA_AREA_ID, None)

                if asset.get("purchase_uuid") is None:
                    continue
                sources = asset.get("field_sources")
                if isinstance(sources, dict):
                    sources.setdefault("purchase_uuid", FIELD_SOURCE_PURCHASE)

            return data

        raise NotImplementedError


def _empty_store_data() -> AssetStoreData:
    """Return an empty version-one Asset Core payload."""
    return {
        "next_asset_number": 1,
        "purchases": {},
        "assets": {},
    }


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


def _validate_store_data(data: AssetStoreData) -> None:
    """Validate invariants which must remain true across all future releases."""
    if not isinstance(data.get("purchases"), dict) or not isinstance(
        data.get("assets"), dict
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


class AssetStoreManager:
    """Own the normalized persistent Asset/Purchase model."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the manager."""
        self.hass = hass
        self._store = DeviceLifecycleStore(hass)
        self._data: AssetStoreData = _empty_store_data()
        self._mutation_lock = asyncio.Lock()

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
        if not all(key in loaded for key in ("next_asset_number", "purchases", "assets")):
            raise AssetStoreError("Asset Core storage payload is incomplete")

        data = cast(AssetStoreData, deepcopy(loaded))
        _validate_store_data(data)
        self._data = data

    async def _async_mutate(
        self,
        mutator: Callable[[AssetStoreData], _MutationResultT],
    ) -> _MutationResultT:
        """Apply one all-or-nothing mutation to a detached Store snapshot."""
        async with self._mutation_lock:
            data = deepcopy(self._data)
            result = mutator(data)
            _validate_store_data(data)

            if data != self._data:
                await self._store.async_save(data)

            # Publish only after the complete snapshot has been validated and
            # durably saved. A mutation or save exception leaves _data untouched.
            self._data = data
            return deepcopy(result)

    def _require_asset(
        self,
        data: AssetStoreData,
        asset_uuid: str,
    ) -> AssetData:
        """Return an Asset from a transaction snapshot or raise a stable error."""
        asset = data["assets"].get(asset_uuid)
        if asset is None:
            raise AssetStoreError(f"Asset {asset_uuid} does not exist")
        return asset

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
        asset: AssetData = {
            "asset_uuid": asset_uuid,
            "asset_id": self._allocate_asset_id(data),
            "name": _device_name(device, device_id),
            "category": None,
            "purchase_uuid": None,
            CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_UNKNOWN,
            "installed_date": None,
            CONF_HA_AREA_ID: None,
            "warranty": {"type": WARRANTY_NONE, "until": None},
            "manufacturer": None,
            "model": None,
            "model_id": None,
            "serial_number": None,
            "sw_version": None,
            "hw_version": None,
            "notes": None,
            "field_sources": {"name": FIELD_SOURCE_HOME_ASSISTANT},
            "ha_device_refs": [
                {"device_id": device_id, "role": DEVICE_ROLE_PRIMARY}
            ],
        }
        self._refresh_home_assistant_metadata(asset, device, device_id)
        data["assets"][asset_uuid] = asset
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
            if asset_uuid in data["assets"]:
                raise AssetStoreError("Generated Asset UUID is already in use")
            sources = {"name": FIELD_SOURCE_USER}
            sources.update(
                {
                    field: FIELD_SOURCE_USER
                    for field, value in metadata.items()
                    if value is not None
                }
            )
            asset: AssetData = {
                "asset_uuid": asset_uuid,
                "asset_id": self._allocate_asset_id(data),
                "name": cast(str, normalized_name),
                "category": metadata["category"],
                "purchase_uuid": None,
                CONF_DEPLOYMENT_STATE: DEPLOYMENT_STATE_NOT_DEPLOYED,
                "installed_date": None,
                CONF_HA_AREA_ID: None,
                "warranty": {"type": WARRANTY_NONE, "until": None},
                "manufacturer": metadata["manufacturer"],
                "model": metadata["model"],
                "model_id": metadata["model_id"],
                "serial_number": metadata["serial_number"],
                "sw_version": metadata["sw_version"],
                "hw_version": metadata["hw_version"],
                "notes": metadata["notes"],
                "field_sources": sources,
                "ha_device_refs": [],
            }
            data["assets"][asset_uuid] = asset
            return asset

        return await self._async_mutate(_create)

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

        return await self._async_mutate(_set_purchase)

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
            sources = asset.setdefault("field_sources", {})
            for field, value in updates.items():
                if value is _UNSET:
                    continue
                asset[field] = value  # type: ignore[literal-required]
                sources[field] = FIELD_SOURCE_USER
            return asset

        return await self._async_mutate(_set_deployment)

    async def async_link_asset_device(
        self,
        asset_uuid: str,
        device_id: str,
        *,
        replace: bool = False,
    ) -> AssetData:
        """Set a stored primary HA reference without touching the HA registry."""
        if not isinstance(device_id, str) or not device_id.strip():
            raise AssetStoreError("Home Assistant device ID is required")
        device_id = device_id.strip()

        def _link(data: AssetStoreData) -> AssetData:
            asset = self._require_asset(data, asset_uuid)
            current = _primary_device_id(asset)
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
            return asset

        return await self._async_mutate(_link)

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

        discovered: dict[str, Any] = {
            "name": _device_name(device, fallback_name),
            "manufacturer": getattr(device, "manufacturer", None),
            "model": getattr(device, "model", None),
            "model_id": getattr(device, "model_id", None),
            "serial_number": getattr(device, "serial_number", None),
            "sw_version": getattr(device, "sw_version", None),
            "hw_version": getattr(device, "hw_version", None),
        }
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

            old_members = set(purchase.get("asset_uuids", []))
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

                self._remove_asset_from_previous_purchase(
                    data,
                    asset,
                    purchase_uuid,
                )
                asset["purchase_uuid"] = purchase_uuid
                asset.setdefault("field_sources", {})[
                    "purchase_uuid"
                ] = FIELD_SOURCE_PURCHASE
                self._apply_purchase_asset_fields(asset, raw)
                new_members.append(asset["asset_uuid"])

            for asset_uuid in old_members.difference(new_members):
                old_asset = data["assets"].get(asset_uuid)
                if old_asset is not None and old_asset.get("purchase_uuid") == purchase_uuid:
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

    def asset_for_device_id(self, device_id: str) -> AssetData | None:
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

    @property
    def asset_count(self) -> int:
        """Return number of persistent Assets."""
        return len(self._data["assets"])
