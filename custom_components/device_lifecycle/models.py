"""Persistent Asset Core data model for Device Lifecycle."""

from __future__ import annotations

from typing import Literal, TypedDict

DeploymentState = Literal["unknown", "not_deployed", "deployed"]
HADeviceRole = Literal["primary", "related"]
LifecycleStatus = Literal["unknown", "active", "retired", "disposed", "lost"]
ReplacementReason = Literal[
    "unknown",
    "planned_refresh",
    "upgrade",
    "failure",
    "warranty_rma",
    "other",
]


class HADeviceReference(TypedDict):
    """Relationship from one Asset to one Home Assistant device."""

    device_id: str
    role: HADeviceRole


class WarrantyData(TypedDict):
    """Asset warranty data."""

    type: str
    until: str | None


class RuntimeData(TypedDict):
    """Asset-owned cumulative Runtime data."""

    total_seconds: str | None


class AssetLifecycleData(TypedDict):
    """Current canonical lifecycle state for one Asset."""

    status: LifecycleStatus
    current_event_uuid: str | None


class LifecycleEventData(TypedDict):
    """One immutable canonical Asset lifecycle transition."""

    event_uuid: str
    asset_uuid: str
    previous_event_uuid: str | None
    from_status: LifecycleStatus
    to_status: LifecycleStatus
    effective_date: str | None
    recorded_at: str
    notes: str | None


class ReplacementRecordData(TypedDict):
    """One historical physical Asset-to-Asset replacement record."""

    replacement_uuid: str
    predecessor_asset_uuid: str
    successor_asset_uuid: str
    reason: ReplacementReason
    effective_date: str | None
    recorded_at: str
    notes: str | None
    voided_at: str | None
    void_reason: str | None


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
    deployment_state: DeploymentState
    installed_date: str | None
    ha_area_id: str | None
    warranty: WarrantyData
    runtime: RuntimeData
    lifecycle: AssetLifecycleData
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


MaintenanceIntervalUnit = Literal["days", "months", "years"]


class MaintenanceCalendarIntervalData(TypedDict):
    """A Maintenance Schedule calendar interval with its semantic unit."""

    value: int
    unit: MaintenanceIntervalUnit


class MaintenanceInitialAnchorData(TypedDict):
    """The explicit calculation baseline that precedes maintenance history."""

    date: str | None
    runtime_seconds: str | None


class MaintenancePreparationReminderData(TypedDict):
    """A calendar-based preparation reminder for one Maintenance Schedule."""

    lead_days: int
    message: str | None


class MaintenanceScheduleData(TypedDict):
    """Current planning configuration for one Asset's maintenance.

    Frozen in docs/maintenance-store-v4-schema.md. Every key is always
    present; a nullable field is stored as None, never omitted. Not part of
    the Store 3.1 payload.
    """

    schedule_uuid: str
    asset_uuid: str
    name: str
    enabled: bool
    calendar_interval: MaintenanceCalendarIntervalData | None
    runtime_interval_seconds: str | None
    initial_anchor: MaintenanceInitialAnchorData | None
    preparation_reminder: MaintenancePreparationReminderData | None


class MaintenanceEventData(TypedDict):
    """One historical fact: maintenance performed on one Asset.

    Frozen in docs/maintenance-store-v4-schema.md. Every key is always
    present; a nullable field is stored as None, never omitted. Not part of
    the Store 3.1 payload.
    """

    event_uuid: str
    asset_uuid: str
    schedule_uuids: list[str]
    title: str
    performed_date: str
    runtime_seconds: str | None
    recorded_at: str
    notes: str | None
    voided_at: str | None
    void_reason: str | None
    corrects_event_uuid: str | None


class AssetStoreData(TypedDict):
    """Top-level normalized Device Lifecycle storage payload."""

    next_asset_number: int
    purchases: dict[str, PurchaseData]
    assets: dict[str, AssetData]
    lifecycle_events: dict[str, LifecycleEventData]
    replacement_records: dict[str, ReplacementRecordData]
