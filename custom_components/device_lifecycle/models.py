"""Persistent Asset Core data model for Device Lifecycle."""

from __future__ import annotations

from typing import TypedDict


class HADeviceReference(TypedDict):
    """Relationship from one Asset to one Home Assistant device."""

    device_id: str
    role: str


class WarrantyData(TypedDict):
    """Asset warranty data."""

    type: str
    until: str | None


class AssetData(TypedDict):
    """One real-world physical Asset.

    ``asset_uuid`` is the immutable technical primary key. ``asset_id`` is the
    permanent short human-facing identifier (DL0001 ... DL9999).
    """

    asset_uuid: str
    asset_id: str
    name: str
    category: str | None
    purchase_uuid: str | None
    installed_date: str | None
    warranty: WarrantyData
    manufacturer: str | None
    model: str | None
    model_id: str | None
    serial_number: str | None
    sw_version: str | None
    hw_version: str | None
    notes: str | None
    field_sources: dict[str, str]
    ha_device_refs: list[HADeviceReference]


class PurchaseData(TypedDict):
    """One purchase transaction which may contain multiple Assets."""

    purchase_uuid: str
    config_subentry_id: str | None
    configured: bool
    name: str | None
    purchase_date: str | None
    seller: str | None
    total_price: str | None
    currency: str
    receipt_reference: str | None
    receipt_url: str | None
    notes: str | None
    asset_uuids: list[str]


class AssetStoreData(TypedDict):
    """Top-level normalized Device Lifecycle storage payload."""

    next_asset_number: int
    purchases: dict[str, PurchaseData]
    assets: dict[str, AssetData]
