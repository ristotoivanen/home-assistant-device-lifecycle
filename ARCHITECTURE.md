# Device Lifecycle architecture

This document defines the core invariants introduced by Device Lifecycle 0.5.0. Future releases should extend this model through explicit storage migrations instead of replacing Asset identity.

## Core concept

An **Asset** is the real-world physical item. A Home Assistant Device Registry entry is only one possible relationship to that Asset.

The model deliberately separates:

- Asset: physical identity and lifecycle metadata
- Purchase: acquisition transaction shared by one or more Assets
- Home Assistant relationship: instance-specific link to a HA device
- Runtime configuration: current measurement rule, still stored in its config subentry in 0.5.0
- Entity: Home Assistant presentation of Asset data, never the sole persistent source of Asset identity

## Identity invariants

Every Asset has two permanent identifiers:

- `asset_uuid`: random UUID, immutable technical primary key
- `asset_id`: monotonic human-facing ID `DL0001` ... `DL9999`

Rules:

1. `asset_uuid` never depends on name, serial number, Home Assistant device ID or location.
2. `asset_id` is unique and never recycled, even if the Asset is detached from active configuration.
3. `next_asset_number` must always be greater than every allocated Asset number.
4. A missing or recreated Home Assistant device must not redefine Asset identity.
5. Storage corruption must not silently restart Asset numbering. Setup fails instead of risking ID reuse.

## Storage

Asset Core uses one fixed Home Assistant Store key:

```text
device_lifecycle.assets
```

The Store is private, atomic and versioned. Version 1.1 is the first public Asset Core schema.

Conceptual payload:

```text
next_asset_number
purchases
  purchase_uuid -> Purchase
assets
  asset_uuid -> Asset
```

The Home Assistant Store wrapper owns the storage major/minor version. Changes to the payload require an explicit storage migration.

## Purchase

A Purchase is a transaction, not a physical identity.

A Purchase contains:

- immutable `purchase_uuid`
- optional current `config_subentry_id`
- whether the purchase is currently represented by an active config subentry
- name
- purchase date
- seller
- total price
- currency
- receipt/order reference
- receipt/invoice URL
- purchase notes
- ordered Asset UUID membership

Money is persisted as a decimal string plus currency, not binary floating point. The Home Assistant form may use a numeric selector, but the normalized persistent representation is decimal-safe.

One Purchase can contain many Assets. An Asset can point to at most one acquisition Purchase in the current schema.

## Asset

An Asset contains:

- immutable `asset_uuid`
- permanent `asset_id`
- display name
- category
- optional acquisition `purchase_uuid`
- installation date
- warranty object
- manufacturer
- model and model ID
- serial number
- software and hardware versions
- Asset notes
- field provenance
- zero or more Home Assistant device references

Dates are stored as ISO calendar dates (`YYYY-MM-DD`).

## Home Assistant relationships

`ha_device_refs` is a list from the first Asset Core version. This avoids a future schema redesign when one physical Asset needs to relate to more than one Home Assistant device.

Relationship roles currently reserved by the schema:

- `primary`: the HA device used to present Asset entities today
- `related`: another HA representation of the same or related physical Asset

An Asset can have at most one `primary` HA device. A HA device can be primary for at most one Asset. A related device may later participate in multiple Asset relationships where the product model requires it.

Device Lifecycle never merges or takes ownership of device-registry entries created by other integrations.

## Metadata provenance

Asset fields discovered from Home Assistant record their source. Current sources are:

- `home_assistant`
- `purchase`
- `user`

Automatic refresh must never overwrite a field whose source is `user`.

In 0.5.0 installation date and warranty are still entered through the purchase UI for compatibility, then projected onto each Asset with source `purchase`. This lets a later Asset-level editor take ownership of an individual value by changing its provenance to `user` without replacing the schema.

## Runtime compatibility

Runtime configuration remains a config subentry in 0.5.0. The physical target receives an `asset_uuid` reference during reconciliation.

Runtime entity unique IDs change from config-subentry/device-derived IDs to:

```text
<asset_uuid>_runtime_hours
```

Lifecycle entity unique IDs use:

```text
<asset_uuid>_lifecycle
```

During migration Device Lifecycle updates the Entity Registry unique ID while preserving the existing `entity_id`. This is required so Home Assistant restore state and recorder identity remain continuous.

The cumulative runtime total itself remains RestoreSensor-owned in 0.5.0. Moving that total into Asset-owned persistent storage is a separate runtime-data migration, not a reason to change Asset identity.

## Reconciliation and migration ordering

0.4.x configuration is normalized in this order:

1. Load and validate existing Asset Core storage, if present.
2. Reconcile Purchase subentries into Purchase and Asset records.
3. Reconcile Runtime subentries to existing or new Assets.
4. Validate all identity and relationship invariants.
5. Atomically save Asset Core storage.
6. Only after the store write succeeds, add generated `purchase_uuid`, `asset_uuid` and currency references back to config subentries.
7. Migrate Entity Registry unique IDs while keeping `entity_id` unchanged.
8. Set up the sensor platform.

This ordering makes repeated setup idempotent and avoids allocating a new Asset merely because a previous run stopped between the storage and config-entry writes.

## Extension rules for later releases

Future functionality should attach to `asset_uuid` instead of introducing another physical-device identity.

Examples:

- deployment and location history -> Asset UUID
- maintenance schedules/events -> Asset UUID
- Asset-owned runtime totals and corrections -> Asset UUID
- lifecycle/replacement/RMA relationships -> Asset UUIDs
- documents -> Purchase UUID or Asset UUID according to document scope
- export/import -> preserve Asset UUID and Asset ID; treat HA device/entity IDs as instance-specific references

Growing histories such as maintenance events or deployment events should not become ConfigSubentries. ConfigSubentries remain suitable for user-configurable active features; persistent history belongs in versioned Device Lifecycle storage.

## Non-goals of Asset Core

Asset Core does not infer that two Home Assistant devices are the same physical item. It does not automatically merge devices by name, model, serial number, network address or integration. Ambiguous relationships require explicit user control in later relationship-management UI.
