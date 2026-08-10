# Device Lifecycle architecture

This document defines the Asset Core invariants through Device Lifecycle 0.5.6. Future releases must extend the model through explicit migrations instead of replacing Asset identity.

## Core concepts

An **Asset** is the canonical identity of one real-world physical item.

The model deliberately separates:

- **Asset**: physical identity and lifecycle metadata
- **Purchase**: an acquisition event shared by zero or more Assets
- **Home Assistant relationship**: an instance-specific reference to an external Home Assistant device
- **Runtime configuration**: an active measurement rule stored in a config subentry in 0.5.6
- **Entity**: Home Assistant presentation of Asset data, never the sole source of Asset identity

A Purchase is not the Asset identity. A Home Assistant Device Registry entry is not the Asset identity. Asset identity remains stable when either relationship changes.

## Identity invariants

Every Asset has two permanent identifiers:

- `asset_uuid`: random UUID and immutable technical primary key
- `asset_id`: monotonic human-readable ID `DL0001` ... `DL9999`

Rules:

1. `asset_uuid` never depends on name, serial number, Purchase, Home Assistant device ID, deployment, or location.
2. `asset_id` is unique and never recycled.
3. `next_asset_number` is always greater than every allocated Asset number.
4. Creating a Purchase does not allocate Asset identity.
5. A missing, linked, unlinked, replaced, or recreated Home Assistant device does not redefine Asset identity.
6. Storage corruption must not silently restart Asset numbering; setup fails instead of risking ID reuse.

## Asset Store schema 1.2

Asset Core uses one private, atomic, versioned Home Assistant Store:

```text
device_lifecycle.assets
```

The Store major version remains `1`; Device Lifecycle 0.5.6 continues to use minor version `2`.

Conceptual payload:

```text
next_asset_number
purchases
  purchase_uuid -> Purchase
assets
  asset_uuid -> Asset
```

The Store wrapper owns the major/minor version. Every payload change requires an explicit migration. Store data is validated as one complete structure before it is published in memory.

### Purchase

A Purchase is an acquisition transaction containing:

- immutable `purchase_uuid`
- optional current `config_subentry_id`
- `configured`, indicating whether an active Purchase subentry currently represents it
- name
- Purchase date
- seller
- total price
- currency
- receipt/order reference
- receipt/invoice URL
- notes
- ordered `asset_uuids` membership

Money is persisted as a decimal string plus currency, not binary floating point.

One Purchase can contain many Assets. A Purchase with no Assets is also valid:

```text
asset_uuids: []
```

No Asset UUID, Asset ID, or `next_asset_number` increment is allocated merely because a Purchase exists.

An Asset can point to at most one acquisition Purchase in Store 1.2.

### Asset

An Asset contains:

- immutable `asset_uuid`
- permanent `asset_id`
- display name
- category
- optional acquisition `purchase_uuid`
- `deployment_state`
- `installed_date`
- `ha_area_id`
- warranty object
- manufacturer
- model and model ID
- serial number
- software and hardware versions
- notes
- field provenance
- zero or more `ha_device_refs`

Dates are stored as ISO calendar dates (`YYYY-MM-DD`).

Allowed Deployment status values are:

```text
unknown
not_deployed
deployed
```

Store 1.2 explicitly supports a relationship-free physical Asset:

```text
purchase_uuid: null
ha_device_refs: []
deployment_state: not_deployed
installed_date: null
ha_area_id: null
```

This is the normal initial relationship state of a manually created Asset.

## Metadata and relationship provenance

Asset fields discovered or projected from another source record provenance in `field_sources`:

- `home_assistant`
- `purchase`
- `user`

Automatic metadata refresh never overwrites a field whose source is `user`. This includes user-cleared fields: an explicit clear remains user-owned and is not repopulated from Home Assistant.

Purchase relationship ownership is explicit:

```text
field_sources["purchase_uuid"] = "purchase" | "user"
```

- `purchase` means legacy device-selected Purchase reconciliation controls the relationship.
- `user` means the user explicitly assigned or cleared the Asset's Purchase.

Reconciliation must not silently replace a user-managed Purchase relationship.

Existing warranty data and Purchase projection behavior remain unchanged in 0.5.6. There is no general Asset-level warranty editor.

## Serialized mutation model

`AssetStoreManager` owns one `asyncio.Lock` used by every Store mutation, including reconciliation. A successful mutation follows this contract:

1. Acquire the mutation lock.
2. Deep-copy the current Store snapshot.
3. Apply the mutation to the copy.
4. Validate the complete resulting Store.
5. Atomically save the complete Store.
6. Publish the new in-memory snapshot.
7. Release the lock.

If mutation, validation, or save fails, the published Store remains unchanged. A failed write must not:

- advance `next_asset_number`
- consume an Asset UUID or `DLxxxx` ID
- leave a half-updated Purchase-to-Asset relationship
- leave a half-replaced Home Assistant device relationship
- expose a partially changed in-memory snapshot

Returned Asset, Purchase, and Store-derived values are detached snapshots and cannot mutate the manager's internal state.

## Purchase creation and reconciliation

Purchase creation accepts zero Home Assistant devices and produces a valid Purchase with `asset_uuids: []`. Assets can later be added by:

- creating a manual Asset and assigning it to the configured Purchase
- assigning an existing eligible Asset
- selecting Home Assistant devices through the existing Purchase flow

Purchase reconciliation is provenance-aware:

- `purchase` relationships follow legacy device-selected Purchase membership.
- `user` relationships are explicitly managed through Asset management.
- a reconciliation pass may refresh the former but must not silently overwrite the latter.
- removing legacy membership does not recycle or replace Asset identity.

Reconciliation updates both `asset.purchase_uuid` and `purchase.asset_uuids` atomically. Historical or unconfigured Purchases and their existing memberships remain stored; only currently configured Purchases are offered as new user-managed targets.

## Deployment semantics

Deployment data is Asset metadata, never Asset identity.

- `unknown` is assigned to Assets migrated from Store 1.1 because their 0.5.3 deployment status is not known.
- `not_deployed` means the Asset exists but is not currently deployed or in use.
- `deployed` means the Asset is currently deployed or in use.

No Deployment status is inferred from:

- Home Assistant device presence
- Purchase membership
- `installed_date`
- `ha_area_id`
- Runtime configuration

`installed_date` remains the existing optional Asset field. It can be set, changed, or cleared independently and is not automatically cleared when status changes.

`ha_area_id` is the optional current deployment Area. It is not identity and does not change the Area of a linked external Home Assistant device. New selections must identify an existing Area. A stale stored Area ID is preserved for display and explicit repair; Device Lifecycle never remaps it by name.

Changing an Asset with an Area to `not_deployed` requires explicit confirmation before `ha_area_id` is cleared. `installed_date` remains unchanged unless the user explicitly edits it.

Store 1.2 contains only current Deployment status, Installation date, and Area. There is no deployment history in 0.5.6.

## Home Assistant relationships

`ha_device_refs` stores relationships from an Asset to Home Assistant Device Registry devices. In 0.5.6, related relationships are explicitly user-managed. Primary relationships may be established through Asset management or the existing Purchase and Runtime reconciliation paths. The persisted Store 1.2 representation is unchanged:

```text
ha_device_refs:
  - device_id: "..."
    role: primary | related
```

The supported roles are:

- `primary`: the one operational external device currently linked to the Asset
- `related`: a non-exclusive reference to another external device associated with the Asset

Relationship invariants:

1. An Asset has zero or one primary device and zero or more related devices.
2. A Home Assistant device is primary for at most one Asset.
3. A Home Assistant device may be related to multiple Assets.
4. A device may be primary for one Asset and related to other Assets.
5. The same device cannot occur twice within one Asset, including once as primary and once as related.
6. Linking the same primary or related device to the same Asset is idempotent.
7. A primary collision with another Asset is rejected; Assets are never merged automatically.
8. Missing primary and related device IDs remain stored until the user explicitly removes, unlinks, or replaces them.

Device Lifecycle does not match or merge Assets by name, manufacturer, model, serial number, identifiers, connections, network address, or Area.

Primary is the only operational Home Assistant relationship used by current compatibility behavior. Purchase reconciliation, Runtime reconciliation, Runtime targets, lifecycle entity placement, and Runtime entity placement resolve the primary device only. Related devices never become a fallback when the primary is absent or stale and never cause entities to be copied or moved.

A successful primary link, replacement, or promotion may refresh eligible non-user-owned Asset metadata through the existing Home Assistant metadata rules. Adding a related relationship never refreshes or aggregates metadata. Neither role changes Asset UUID, Asset ID, Purchase, deployment information, or Runtime configuration.

Promoting an existing related device to primary removes its related reference in the same atomic mutation. The old primary is removed and is not automatically converted into a related reference. All dependency and primary-conflict checks apply normally.

Device Lifecycle never mutates an external Device Registry entry and does not:

- attach its config entry to an external device
- create a Device Lifecycle-owned Device Registry entry
- change external identifiers or connections
- rename or move the external device
- change the external Area
- change external config-entry ownership

Home Assistant 2026.8 restricts a Device Registry device to one owning config entry. Eligibility validation uses the device's `config_entry_id` and its current owner config entry. Device Lifecycle rejects its own devices, service devices, conservative software/system exclusions, devices with no valid owner, and missing device IDs without using deprecated multi-config-entry ownership assumptions.

Related add/remove operations do not mutate Purchase or Runtime config subentries and are not blocked merely because the device is used elsewhere. Removing a related reference uses the stored relationship list, so it remains possible when the Device Registry device no longer exists.

Unlink and replacement are allowed only when the current primary device is not referenced by an active:

- Purchase subentry through `device_ids`, or
- Runtime subentry through `device_id`

Dependency conflicts are reported to the user. Device Lifecycle 0.5.6 does not rewrite dependent subentries automatically. Replacement removes the old primary and adds the validated new primary in one serialized Store mutation and one atomic save. Failed validation or save preserves the old relationship.

## Runtime compatibility

Runtime configuration remains a config subentry in 0.5.6. Reconciliation stores an `asset_uuid` reference in the Runtime subentry and resolves an existing matching primary relationship before considering a new Asset. Related relationships are ignored for Runtime reconciliation and entity setup.

Runtime entity unique IDs are Asset-owned:

```text
<asset_uuid>_runtime_hours
```

Lifecycle entity unique IDs use:

```text
<asset_uuid>_lifecycle
```

Entity Registry migration preserves the existing `entity_id`, recorder identity, and restore continuity. The cumulative Runtime total remains RestoreSensor-owned in 0.5.6; it is not persisted in Asset Store.

## Store 1.1 to 1.2 migration

The explicit migration preserves exactly:

- Asset UUIDs
- Asset IDs
- `next_asset_number`
- Purchase UUIDs, ordering, and membership
- Home Assistant device references
- config-subentry references
- Runtime relationships
- entity unique IDs and restored totals

For every existing 0.5.3 Asset, migration adds:

```text
deployment_state: unknown
ha_area_id: null
```

If `purchase_uuid` is non-null and `field_sources["purchase_uuid"]` is absent, the migration records `purchase` provenance. The migration is idempotent and does not infer deployment from any existing relationship or metadata.

Downgrading Store 1.2 to Device Lifecycle 0.5.3 should not be treated as safe without restoring a backup made before the upgrade.

## Reconciliation and persistence ordering

Current setup and legacy normalization follow this order:

1. Load and validate Asset Store, applying an explicit migration when required.
2. Reconcile Purchase subentries into Purchase and Asset records.
3. Reconcile Runtime subentries to existing or new Assets.
4. Validate all identity, provenance, membership, and relationship invariants.
5. Atomically save Asset Store.
6. Only after Store persistence succeeds, add generated stable references back to config subentries.
7. Migrate Entity Registry unique IDs while preserving `entity_id`.
8. Set up entity platforms.

This ordering makes repeated setup idempotent and avoids allocating a second Asset merely because a previous run stopped between Store and config-entry writes.

## Extension rules

Future functionality attaches to `asset_uuid`; it must not introduce another physical-device identity.

Examples from the existing roadmap include:

- maintenance schedules and history -> Asset UUID
- Asset-owned Runtime totals and corrections -> Asset UUID
- lifecycle, replacement, and RMA relationships -> Asset UUIDs
- documents -> Purchase UUID or Asset UUID according to scope
- export/import -> preserve Asset UUID and Asset ID; treat Home Assistant device/entity IDs as instance-specific references

Growing histories do not belong in ConfigSubentries. ConfigSubentries remain suitable for active user configuration; persistent history belongs in explicitly versioned Device Lifecycle storage.

## 0.5.6 schema and migration impact

Device Lifecycle 0.5.6 changes management behavior, validation, and UI only. It does not change the persisted `ha_device_refs` shape. Therefore:

- Store major/minor remains `1.2`
- config entry version remains `4`
- no Store migration is added
- no config-entry migration is added
- existing valid Store 1.2 related references load directly

## Explicit non-goals for 0.5.6

Device Lifecycle 0.5.6 does not add:

- Asset archive or delete
- Asset merge
- Device Lifecycle-owned Home Assistant Device Registry devices
- automatic device discovery or metadata matching
- automatic stale-device rematching
- relationship history or subtype taxonomy
- metadata aggregation from related devices
- Asset status sensors
- deployment history
- maintenance schedules or history
- a general Asset-level warranty editor
- Runtime total persistence in Asset Store
- Runtime migration or history
- Asset-to-Asset or replacement relationships
- export/import

The 0.5.6 acceptance contract is: one optional primary and any number of related Home Assistant relationships per Asset; primary alone drives current operational compatibility; related references are non-exclusive and have no identity, metadata, Purchase, Runtime, entity-placement, deployment, or Device Registry ownership semantics; stale references persist until explicit repair; and every mutation remains validated and atomic.
