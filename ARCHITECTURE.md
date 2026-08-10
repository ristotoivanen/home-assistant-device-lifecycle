# Device Lifecycle architecture

This document defines the Asset Core and Asset Exposure invariants through Device Lifecycle 0.6.1. Future releases must extend the model through explicit migrations instead of replacing Asset identity.

## Core concepts

An **Asset** is the canonical identity of one real-world physical item.

The model deliberately separates:

- **Asset**: physical identity and lifecycle metadata
- **Purchase**: an acquisition event shared by zero or more Assets
- **Home Assistant relationship**: an instance-specific reference to an external Home Assistant device
- **Runtime configuration**: an active measurement rule stored in a config subentry
- **Runtime total**: cumulative Asset-owned history stored in Asset Core
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

## Asset Store schema 2.1

Asset Core uses one private, atomic, versioned Home Assistant Store:

```text
device_lifecycle.assets
```

Device Lifecycle 0.6.1 uses Store major version `2`, minor version `1`. Asset Exposure is derived entirely from this existing payload and adds no stored projection IDs, exposure state, migration markers, or persistent relationship fields.

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

An Asset can point to at most one acquisition Purchase in Store 2.1.

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
- Runtime object
- manufacturer
- model and model ID
- serial number
- software and hardware versions
- notes
- field provenance
- zero or more `ha_device_refs`

Runtime is represented as:

```text
runtime:
  total_seconds: decimal string | null
```

The value is finite, non-negative cumulative seconds. `null` means canonical Runtime is not initialized yet; it never means zero. Persistent Runtime arithmetic uses `Decimal`, and normal operation never decreases the total.

Dates are stored as ISO calendar dates (`YYYY-MM-DD`).

Allowed Deployment status values are:

```text
unknown
not_deployed
deployed
```

Store 2.1 explicitly supports a relationship-free physical Asset:

```text
purchase_uuid: null
ha_device_refs: []
deployment_state: not_deployed
installed_date: null
ha_area_id: null
runtime:
  total_seconds: null
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

Existing warranty data and Purchase projection behavior remain unchanged in 0.6.0. There is no general Asset-level warranty editor.

## Serialized mutation model

`AssetStoreManager` owns one `asyncio.Lock` used by every Store mutation, including reconciliation. A successful mutation follows this contract:

1. Acquire the mutation lock.
2. Deep-copy the current Store snapshot.
3. Apply the mutation to the copy.
4. Validate the complete resulting Store.
5. Atomically save the complete Store.
6. Read the Store file directly and verify the complete versioned envelope and payload.
7. Publish the new in-memory snapshot only after verification.
8. Release the lock.

Home Assistant 2026.8 may catch and log an underlying `WriteError` inside `Store.async_save()`. Device Lifecycle therefore does not treat a normal return as acknowledgement. Failed verification leaves the published snapshot unchanged. If read-back itself fails and persistence is ambiguous, the manager reloads and validates the direct Store snapshot before another mutation; Runtime compare-and-set semantics then recognize an already-landed delta without adding it twice.

If mutation, validation, save, or verification fails, the published Store remains unchanged. A failed write must not:

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

Store 2.1 contains only current Deployment status, Installation date, and Area. There is no deployment history in 0.6.0.

## Home Assistant relationships

`ha_device_refs` stores relationships from an Asset to Home Assistant Device Registry devices. In 0.5.7, related relationships are explicitly user-managed. Primary relationships may be established through Asset management or the existing Purchase and Runtime reconciliation paths. The relationship representation in Store 2.1 remains:

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

Primary is the only operational Home Assistant relationship used by current compatibility behavior. Purchase reconciliation, Runtime reconciliation, and Runtime targets resolve the primary device only. Entity placement is now on the derived Asset Device and does not turn that projection into a Runtime source or external relationship. Related devices never become a fallback when the primary is absent or stale and never cause entities to be copied or moved.

A successful primary link, replacement, or promotion may refresh eligible non-user-owned Asset metadata through the existing Home Assistant metadata rules. Adding a related relationship never refreshes or aggregates metadata. Neither role changes Asset UUID, Asset ID, Purchase, deployment information, or Runtime configuration.

Promoting an existing related device to primary removes its related reference in the same atomic mutation. The old primary is removed and is not automatically converted into a related reference. All dependency and primary-conflict checks apply normally.

Device Lifecycle never mutates an external Device Registry entry and does not:

- attach its config entry to an external device
- change external identifiers or connections
- rename or move the external device
- change the external Area
- change external config-entry ownership

The Device Lifecycle-owned Asset Device described below is a separate derived projection. Creating or repairing that owned projection does not attach Device Lifecycle to an external device and does not change the stored primary or related references.

## Asset Device projection

Device Lifecycle 0.6.0 creates exactly one owned Home Assistant Device Registry projection for every valid canonical Asset. It is a presentation object, not a new Asset identity and not canonical storage.

Its complete identity is:

```text
identifiers = {("device_lifecycle", asset_uuid)}
config_entry_id = Device Lifecycle parent ConfigEntry ID
config_subentry_id = null
connections = {}
```

Only immutable `asset_uuid` participates in this identity. `asset_id`, name, manufacturer, model, model ID, serial number, Purchase UUID, external device IDs, Area, and connection data never participate. The Device Registry ID is deliberately not persisted in Asset Store because the projection can always be found or recreated from `asset_uuid`.

The Asset Device projects only the canonical Asset's supported physical metadata: name, manufacturer, model, model ID, serial number, software version, and hardware version. It does not dynamically copy metadata from the current external primary device. Existing Asset provenance rules may first update canonical Store metadata; exposure then reads that canonical snapshot. A Home Assistant `name_by_user` override remains a Registry customization and is never written back to Asset Store.

Asset `ha_area_id` is not assigned to the Asset Device's Device Registry Area. Asset deployment Area and the Device Registry Area are independent concepts in 0.6.0. Loss or user removal of the derived Asset Device never deletes, archives, reallocates, or otherwise changes the Asset. A later setup may recreate the missing projection with the same identifier.

Device Lifecycle owns only these deterministic projection devices. External Home Assistant devices remain independently owned references and are never mutated, renamed, moved, deleted, or heuristically replaced.

## Asset exposure entities

Every valid Asset exposes five parent-owned entities on its Asset Device, regardless of Purchase, external relationship, Runtime configuration, or deployment state:

- Lifecycle, with stable unique ID `<asset_uuid>_lifecycle`
- Deployment, with stable unique ID `<asset_uuid>_deployment`
- Installation Date, with stable unique ID `<asset_uuid>_installed_date`
- Relationships, with stable unique ID `<asset_uuid>_relationships`
- Asset ID, with stable unique ID `<asset_uuid>_asset_id`

Lifecycle retains its existing state and warranty behavior, existing attributes, unique ID, entity ID, user naming, enabled state, options, and Recorder identity. It no longer belongs to a Purchase subentry. An Asset with no Purchase or warranty still exposes Lifecycle with the existing not-specified style state.

Deployment is a normal enum sensor with canonical states `unknown`, `not_deployed`, and `deployed`. It resolves only the exact stored `ha_area_id` for display. Its deterministic Area state is `not_set`, `present`, or `missing`; a stale ID remains stored and is never repaired by name.

Installation Date is a normal native date sensor derived only from canonical Asset `installed_date`. It holds no independent canonical state and does not read Purchase subentry data directly. Purchase reconciliation and user provenance determine the canonical Asset value before exposure; both provenance modes are displayed identically. A date never implies or changes Deployment state, and the existing Deployment and Lifecycle compatibility attributes remain unchanged.

Relationships is an enabled-by-default diagnostic enum sensor. Its state is `none` when no references are stored, `present` when every exact stored Device Registry ID resolves, and `missing` when any stored ID does not resolve. `primary_state` uses `not_linked`, `present`, or `missing`; related entries use only `present` or `missing`. Current Device Registry names are derived display metadata and are never persisted. Registry presence means only that a `DeviceEntry` exists, not that the device is operational or available.

Asset ID is an enabled-by-default diagnostic sensor whose state is the permanent `DLxxxx` value. The short ID is not inserted into the Asset Device name merely to expose it.

Home Assistant 2026.8 restricts a Device Registry device to one owning config entry. Eligibility validation uses the device's `config_entry_id` and its current owner config entry. Device Lifecycle rejects its own devices, service devices, conservative software/system exclusions, devices with no valid owner, and missing device IDs without using deprecated multi-config-entry ownership assumptions.

Related add/remove operations do not mutate Purchase or Runtime config subentries and are not blocked merely because the device is used elsewhere. Removing a related reference uses the stored relationship list, so it remains possible when the Device Registry device no longer exists.

Unlink and replacement are allowed only when the current primary device is not referenced by an active:

- Purchase subentry through `device_ids`, or
- Runtime subentry through `device_id`

Dependency conflicts are reported to the user. Device Lifecycle 0.6.0 does not rewrite dependent subentries automatically. Replacement removes the old primary and adds the validated new primary in one serialized Store mutation and one verified atomic save. Failed validation, save, or read-back preserves the published old relationship.

## Canonical Runtime ownership

Runtime configuration remains a config subentry in 0.6.0. Reconciliation stores an `asset_uuid` reference and resolves the primary relationship only. Related relationships have no Runtime semantics.

Runtime entity unique IDs are Asset-owned:

```text
<asset_uuid>_runtime_hours
```

Lifecycle entity unique IDs use:

```text
<asset_uuid>_lifecycle
```

Entity Registry migration preserves the existing `entity_id`, recorder identity, and Recorder continuity. Asset Store is the canonical Runtime truth; the sensor is a projection and Recorder remains the displayed timeline.

Runtime configuration remains owned by its existing Runtime ConfigSubentry, so the Runtime Entity Registry `config_subentry_id` is unchanged. In 0.6.0 its `device_id` points to the owned Asset Device rather than the external primary device. This placement change does not alter canonical total ownership, RestoreSensor import, source validation, monotonic timing, pending deltas, checkpoint frequency, CAS semantics, power thresholds, hysteresis, state class, units, precision, or shutdown/unload behavior. Related devices never become Runtime targets or fallbacks.

## Exposure registry migration and recovery

The 0.6.0 registry migration is deliberately separate from Asset Store migration and from the existing 0.4.x entity unique-ID migration. Setup order is:

1. Load and validate Store 2.1, applying only the already-supported Store 1.1/1.2 migration when required.
2. Reconcile Purchase subentries.
3. Reconcile Runtime subentries.
4. Validate the complete canonical Store snapshot and persist reconciliation before publishing it.
5. Complete the logically separate legacy entity unique-ID migration.
6. Build the complete read-only 0.6.0 exposure plan.
7. Ensure deterministic Asset Devices.
8. Reparent existing Lifecycle entities to the parent ConfigEntry and Asset Device.
9. Relink existing Runtime entities to the Asset Device while preserving their Runtime subentry.
10. Publish `runtime_data` and forward sensor platform setup.
11. Create any missing parent-owned exposure entities and register the centralized relationship listener.

The preflight scans all Assets and all desired entity identities, including `<asset_uuid>_installed_date`, before the first exposure-related Device or Entity Registry mutation. An exact Asset Device identifier may have zero or one active match. Multiple matches, external ownership, additional identifiers or connections, a foreign entity collision, a noncanonical Lifecycle/Runtime unique ID, a mismatched config entry, or multiple Runtime subentries for one Asset fail setup closed. An otherwise exact Device Lifecycle-owned projection found on one of the same parent entry's subentries is an incomplete derived projection and is safely moved back to the parent; metadata is never used to choose between devices or entities.

Registry reconciliation is idempotent. A valid existing Asset Device is reused; only a missing exact projection is created. Existing entity updates change only `device_id` and `config_subentry_id`; they do not rewrite entity ID, unique ID, name overrides, enabled state, options, categories, labels, or unrelated customization.

Device Registry and Entity Registry are not treated as one atomic transaction. Before each existing entity move, the migration retains its original entity ID, unique ID, device ID, and config-subentry ID. If a later step fails, attempted entity moves are restored in reverse order. Cleanup removes only an unreferenced Asset Device proven absent at preflight and created during the current setup attempt. A pre-existing device is never rollback cleanup.

If rollback itself fails, setup fails with an actionable error and Asset Store remains canonical and unchanged. A partial derived projection may remain. The next setup re-reads Store and both registries and reconciles the same deterministic desired state. It never allocates another Asset UUID or Asset ID, increments `next_asset_number`, rewrites Purchases or external references, remaps a missing external device, or adds a Store marker.

This defines the repair boundary: Device Lifecycle may recreate its own deterministic projection objects, but it never silently repairs canonical or external relationships. A missing external primary or related device stays as the same stored ID and is exposed as missing.

Store 1.1 and 1.2 migrations add `runtime.total_seconds: null` to every existing Asset. Legacy Runtime subentries have no `runtime_data_version`. Before their sensor is added, Device Lifecycle resolves the existing entity ID, reads native RestoreSensor data for that exact identity, requires a finite non-negative native value in hours, converts it with `Decimal` to seconds, and atomically compares-and-sets `null` to that value. A non-null canonical total always wins, so the import is exactly once. Missing, corrupt, unsafe-unit, or unpersistable restore data leaves the value null, preserves the entity-registry identity, adds no misleading zero sensor, and retries on reload.

New Runtime subentries contain `runtime_data_version: 1`. Only that provenance permits a safe atomic `null` to `"0"` initialization. Reconfiguration preserves the marker, and removing or recreating Runtime tracking never removes or resets the Asset total.

Elapsed Runtime uses a monotonic clock and three explicit components: committed canonical seconds, ordered sealed pending deltas, and the current active interval. The displayed value includes all three. Every five minutes while active, the entity seals elapsed time, advances its active baseline, and commits the delta. Stop, unavailable/unknown, unload, and normal shutdown also checkpoint. Inactive periodic callbacks retry pending deltas.

Each delta carries its expected canonical total. Store equality with the expected value applies the delta; equality with expected plus delta is idempotent success; any other value fails closed. An entity-level lock serializes periodic, source, unload, and shutdown callbacks, while the Store manager mutation lock serializes canonical writes. Failed saves retain sealed pending deltas without leaving the active timer running. Under healthy persistence, a hard crash loses only time since the last successful checkpoint, normally less than the five-minute interval. That bound does not apply while Store writes are failing.

## Store 1.1/1.2 to 2.1 migration

The explicit migration preserves exactly:

- Asset UUIDs
- Asset IDs
- `next_asset_number`
- Purchase UUIDs, ordering, and membership
- Home Assistant device references
- config-subentry references
- Runtime relationships
- entity unique IDs and legacy restored totals pending import

For every existing 0.5.3 Asset, migration adds:

```text
deployment_state: unknown
ha_area_id: null
runtime:
  total_seconds: null
```

If `purchase_uuid` is non-null and `field_sources["purchase_uuid"]` is absent, the migration records `purchase` provenance. The migration is idempotent and does not infer deployment from any existing relationship or metadata.

Store 1.2 Assets also receive the null Runtime object directly. Migration works on a detached copy and validates the complete Store 2.1 result.

After Store migration, downgrading 0.5.7 to 0.5.6 is unsupported. The 0.5.6 Store v1 reader rejects Store major version 2 and must not rewrite it. Restore a backup made before the upgrade for rollback.

## Reconciliation and persistence ordering

Current setup and legacy normalization follow this order:

1. Load and validate Asset Store, applying an explicit migration when required.
2. Reconcile Purchase subentries into Purchase and Asset records.
3. Reconcile Runtime subentries to existing or new Assets.
4. Validate all identity, provenance, membership, and relationship invariants.
5. Atomically save Asset Store.
6. Only after Store persistence succeeds, add generated stable references back to config subentries.
7. Migrate Entity Registry unique IDs while preserving `entity_id`.
8. Build and validate the complete exposure migration plan without mutation.
9. Ensure/reconcile owned Asset Devices and existing entity placement with compensating rollback.
10. Set `runtime_data` and set up entity platforms.

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

## 0.6.1 schema and migration impact

Device Lifecycle 0.6.1 adds only a native Home Assistant date projection of the already-canonical Asset `installed_date`. Therefore:

- Store major/minor is `2.1`
- config entry version remains `4`
- Store 1.1 and 1.2 migrate explicitly to 2.1
- no config-entry migration is added
- Runtime entity unique IDs and entity IDs remain unchanged
- Lifecycle entity unique IDs and entity IDs remain unchanged
- no Device Registry ID or exposure status is stored
- no installation-date field, provenance rule, Store migration, or ConfigEntry migration is added

## Explicit non-goals for 0.6.0

Device Lifecycle 0.6.0 does not add:

- Asset archive or delete
- Asset merge
- automatic device discovery or metadata matching
- automatic stale-device rematching
- relationship history or subtype taxonomy
- metadata aggregation from related devices
- Asset status sensors
- deployment history
- maintenance schedules or history
- a general Asset-level warranty editor
- Runtime reset or manual Runtime editing
- Recorder/statistics migration
- Asset-to-Asset or replacement relationships
- export/import

It also does not add Asset deletion/archive/merge, replacement or RMA, Maintenance, maintenance history or schedules, Runtime reset/manual editing, Runtime-based maintenance, Recorder migration, Device Registry Area synchronization, Repairs, notifications, custom frontend surfaces, Documents, or Asset-to-Asset relationships.

The 0.6.0 acceptance contract exposes the existing Asset without replacing it: `asset_uuid` remains the only canonical technical identity; Store stays 2.1 and ConfigEntry stays 4; the Asset Device is a recomputable projection; Lifecycle is present for every Asset; Runtime configuration stays subentry-owned while history stays Asset-owned; existing Lifecycle/Runtime identities remain continuous; external devices stay untouched references; ambiguous registry identity fails closed; and partial derived projection state remains deterministically recoverable.
