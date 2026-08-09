"""Versioned persistent Asset Core storage for Device Lifecycle."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import os
import re
from typing import Any, cast
from uuid import UUID, uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store

from .const import (
    CONF_ASSET_UUID,
    CONF_CURRENCY,
    CONF_DEVICE_ID,
    CONF_DEVICE_IDS,
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
    DOMAIN,
    SUBENTRY_TYPE_PURCHASE,
    SUBENTRY_TYPE_RUNTIME,
    WARRANTY_MANUAL,
    WARRANTY_NONE,
    WARRANTY_TYPES,
)
from .models import AssetData, AssetStoreData, HADeviceReference, PurchaseData

STORAGE_VERSION = 1
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
        """Migrate future Asset Core storage versions explicitly."""
        if old_major_version == STORAGE_VERSION:
            # Version 1.1 is the first public Asset Core schema. Keeping the
            # migration hook from day one makes later minor migrations explicit.
            if old_minor_version <= STORAGE_MINOR_VERSION:
                return old_data
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

        _validate_optional_date(asset.get("installed_date"), "installed_date")

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
            "installed_date": None,
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

    def _ensure_primary_reference(self, asset: AssetData, device_id: str) -> None:
        """Set a primary HA relationship while preserving future related links."""
        related: list[HADeviceReference] = [
            reference
            for reference in asset.get("ha_device_refs", [])
            if reference.get("role") != DEVICE_ROLE_PRIMARY
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

    async def async_reconcile_entry(self, entry: ConfigEntry) -> None:
        """Normalize current 0.4.x/0.5.x subentries into Asset Core storage.

        Storage is written before adding the generated UUID references back to
        config subentries. If a later config-entry write fails, the next setup can
        recover the same objects by subentry/device relationship instead of
        allocating new Asset IDs.
        """
        data = deepcopy(self._data)
        device_registry = dr.async_get(self.hass)
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

        _validate_store_data(data)

        if data != self._data:
            await self._store.async_save(data)
        self._data = data

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
