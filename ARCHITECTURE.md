# Device Lifecycle architecture

This document defines the Asset Core and Asset Exposure invariants through Device Lifecycle 0.7.1. Future releases must extend the model through explicit migrations instead of replacing Asset identity.

## Core concepts

An **Asset** is the canonical identity of one real-world physical item.

The model deliberately separates:

- **Asset**: physical identity and lifecycle metadata
- **Purchase**: an acquisition event shared by zero or more Assets
- **Home Assistant relationship**: an instance-specific reference to an external Home Assistant device
- **Runtime configuration**: an active measurement rule stored in a config subentry
- **Runtime total**: cumulative Asset-owned history stored in Asset Core
- **Lifecycle state/history**: current Asset state plus an immutable Asset-specific transition chain
- **Replacement**: a historical directed relationship between predecessor and successor physical Assets
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

## Asset Store schema 3.1

Asset Core uses one private, atomic, versioned Home Assistant Store:

```text
device_lifecycle.assets
```

Device Lifecycle 0.7.1 uses Store major version `3`, minor version `1`. Asset Exposure remains derived and adds no stored projection IDs, exposure state, workflow drafts, or alternate Asset identity. Version 0.7.1 requires no Store or ConfigEntry migration.

Conceptual payload:

```text
next_asset_number
purchases
  purchase_uuid -> Purchase
assets
  asset_uuid -> Asset
lifecycle_events
  event_uuid -> LifecycleEvent
replacement_records
  replacement_uuid -> ReplacementRecord
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

An Asset can point to at most one acquisition Purchase in Store 3.1.

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
- Lifecycle object
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

lifecycle:
  status: unknown | active | retired | disposed | lost
  current_event_uuid: UUID | null
```

The value is finite, non-negative cumulative seconds. `null` means canonical Runtime is not initialized yet; it never means zero. Persistent Runtime arithmetic uses `Decimal`, and normal operation never decreases the total.

Dates are stored as ISO calendar dates (`YYYY-MM-DD`).

Allowed Deployment status values are:

```text
unknown
not_deployed
deployed
```

Store 3.1 explicitly supports a relationship-free physical Asset:

```text
purchase_uuid: null
ha_device_refs: []
deployment_state: not_deployed
installed_date: null
ha_area_id: null
runtime:
  total_seconds: null
lifecycle:
  status: active
  current_event_uuid: <initial event UUID>
```

This is the normal initial relationship state of a newly created Asset. A creation path that explicitly requests Lifecycle `unknown` stores `current_event_uuid: null` and creates no same-state event. Assets migrated into 3.1 also use that unknown/no-event state.

## Metadata and relationship provenance

Asset fields discovered or projected from another source record provenance in `field_sources`:

- `home_assistant`
- `purchase`
- `user`

Automatic metadata refresh never overwrites a field whose source is `user`. This includes user-cleared fields: an explicit clear remains user-owned and is not repopulated from Home Assistant.

Quick Add preserves the same rule: an unchanged normalized Home Assistant suggestion remains `home_assistant`, while a changed or explicitly cleared suggestion becomes `user`. Default absence is not an override. In particular, a new Asset with no Purchase stores `purchase_uuid: null` without a `purchase_uuid` provenance entry; selecting an existing configured Purchase records `user` provenance.

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
2. If persistence is uncertain, directly read and validate the persisted 3.1 snapshot first.
3. Deep-copy the current published Store snapshot.
4. Validate requested inputs and apply the mutation to the copy.
5. Validate the complete resulting Store, including every lifecycle chain and the whole replacement graph.
6. Atomically save the complete Store.
7. Read the Store file directly and verify the complete versioned envelope and exact payload.
8. Publish the new in-memory snapshot only after verification.
9. Release the lock.

Home Assistant 2026.8 may catch and log an underlying `WriteError` inside `Store.async_save()`. Device Lifecycle therefore does not treat a normal return as acknowledgement. Failed verification leaves the published snapshot unchanged. If read-back itself fails and persistence is ambiguous, the manager reloads and validates the direct Store snapshot before another mutation; Runtime compare-and-set semantics then recognize an already-landed delta without adding it twice.

If mutation, validation, save, or verification fails, the published Store remains unchanged. A failed write must not:

- advance `next_asset_number`
- consume an Asset UUID or `DLxxxx` ID
- leave a half-updated Purchase-to-Asset relationship
- leave a half-replaced Home Assistant device relationship
- leave a half-appended lifecycle transition
- publish a void without its corrected replacement record
- expose a partially changed in-memory snapshot

Returned Asset, Purchase, and Store-derived values are detached snapshots and cannot mutate the manager's internal state.

The manager passes only its detached, unpublished candidate snapshot to Home Assistant Store and awaits serialization, write, and direct readback before publishing it. Therefore Store 3.1 safely uses `serialize_in_event_loop=False`; the snapshot cannot be concurrently mutated during executor serialization.

### Atomic Quick Create

Quick Add is a flow orchestration layer over one canonical `async_quick_create_asset` mutation. Its immutable request contains the proposed Asset UUID and reviewed canonical outcome; it contains no UI source concept. Within the existing mutation lock, the manager validates references and expected review snapshots, allocates the next permanent `DLxxxx` identity, creates the Asset, applies Purchase membership, metadata/provenance, Lifecycle, Deployment, warranty, and optional replacement changes, validates the complete Store, saves once, verifies direct readback, and only then publishes.

The proposed UUID is the idempotency key and is generated before confirmation, but is not persisted and consumes no Asset number until the transaction. A replay whose persisted final state exactly matches the request returns the existing result without another Asset ID, lifecycle event, replacement record, or Purchase membership. A UUID with a different final state fails with `quick_create_idempotency_conflict`.

If the write outcome is ambiguous, Quick Create performs one direct recovery read. A matching persisted result is returned as a successful replay; definite absence is treated as not committed; an unreadable outcome fails closed with `persistence_error`. It never blindly issues a second write after an ambiguous first write. Deterministic validation and known persistence failures leave the entire published snapshot, including `next_asset_number`, unchanged.

Quick Add may select an existing configured Purchase but never creates a Purchase or ConfigSubentry. One- and two-year warranty dates use calendar-year arithmetic from the reviewed Purchase date, which is revalidated at commit; manual warranty remains possible without a Purchase. Quick Add does not read, initialize, transfer, or otherwise mutate Runtime totals or Runtime configuration.

An optional replacement remains one physical Asset-to-Asset record. The same transaction can apply explicitly reviewed predecessor retirement and undeployment: only `active` or `unknown` Lifecycle becomes `retired`; `retired`, `disposed`, and `lost` are not rewritten. Only `deployed` or `unknown` Deployment becomes `not_deployed`, and an actual undeploy clears Asset Area while preserving Installation date. The replacement and any retirement event use the same optional effective date and common transaction `recorded_at`; replacement notes stay only on the replacement record. Purchase, warranty, external relationships, and Runtime never transfer.

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

Store 3.1 contains only current Deployment status, Installation date, and Area. There is no deployment history in 0.7.0.

## Canonical lifecycle state and event chain

Lifecycle and Deployment are independent domains. Lifecycle status is exactly one of `unknown`, `active`, `retired`, `disposed`, or `lost`. No `stored`, `replaced`, or `returned` lifecycle values exist. Every explicit corrective transition is permitted; terminal-looking states are not irreversible.

`asset.lifecycle` stores only the current state and current event pointer. Growing history lives in top-level `lifecycle_events`, never a ConfigSubentry. Each immutable event contains:

```text
event_uuid
asset_uuid
previous_event_uuid
from_status
to_status
effective_date
recorded_at
notes
```

`effective_date` is an optional non-future Home Assistant local calendar date. `recorded_at` is generated by Device Lifecycle as an aware UTC timestamp. Users cannot edit or delete events. A mistake is corrected by appending another transition. A same-state request appends nothing, writes nothing, and triggers no reload.

Complete Store validation proves that all event and Asset UUIDs are canonical, keys equal embedded IDs, every event's Asset and previous event exist, previous events belong to the same Asset, adjacent statuses and known dates are continuous/non-decreasing, recorded timestamps are valid UTC and non-decreasing, no branch/cycle/orphan exists, and all events for each Asset form one chain ending at `current_event_uuid`. The current event must end in the Asset's current status. An Asset with no events must be `unknown` with a null current pointer.

New ordinary Assets create `unknown` → `active` in the same canonical creation mutation. Migration never fabricates an event: every existing Asset enters 3.1 as unknown with a null pointer.

## Physical replacement graph

Top-level `replacement_records` stores permanent directed records from `predecessor_asset_uuid` to `successor_asset_uuid`. The schema is a dictionary of records and does not encode the 0.7.0 cardinality limit. Each record contains:

```text
replacement_uuid
predecessor_asset_uuid
successor_asset_uuid
reason
effective_date
recorded_at
notes
voided_at
void_reason
```

Reasons are `unknown`, `planned_refresh`, `upgrade`, `failure`, `warranty_rma`, and `other`. `warranty_rma` is only a reason; 0.7.0 has no RMA case model. Both endpoints must be distinct existing Assets. Effective dates are optional and non-future. Integration timestamps are aware UTC.

Only records with `voided_at: null` participate in the active graph. Store 3.1 retains all voided records permanently. Version 0.7.0 whole-graph validation enforces at most one active outgoing and one active incoming edge per Asset, rejects duplicate active edges and self-links, detects cycles of every length, and requires known effective dates to be non-decreasing along consecutive active edges. Unknown dates never imply order.

Voiding requires a non-empty reason. Correction is one serialized mutation: mark the old record void, create the new record, validate the complete graph, perform one verified save, then publish. Any mutation, graph, persistence, or readback failure leaves the old published relationship active.

Replacement never changes Lifecycle, Deployment, Installation Date, Area, Purchase, warranty, external Home Assistant relationships, Runtime, maintenance, or documents. In particular, a `warranty_rma` successor does not inherit the predecessor's acquisition Purchase or warranty.

## Home Assistant relationships

`ha_device_refs` stores relationships from an Asset to Home Assistant Device Registry devices. In 0.5.7, related relationships became explicitly user-managed. Primary relationships may be established through Asset management or the existing Purchase and Runtime reconciliation paths. The relationship representation remains unchanged in Store 3.1:

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

Every valid Asset exposes seven parent-owned entities on its Asset Device, regardless of Purchase, external relationship, Runtime configuration, or deployment state:

- Lifecycle, with stable unique ID `<asset_uuid>_lifecycle`
- Deployment, with stable unique ID `<asset_uuid>_deployment`
- Installation Date, with stable unique ID `<asset_uuid>_installed_date`
- Relationships, with stable unique ID `<asset_uuid>_relationships`
- Asset ID, with stable unique ID `<asset_uuid>_asset_id`
- Lifecycle Status, with stable unique ID `<asset_uuid>_lifecycle_status`
- Replacement, with stable unique ID `<asset_uuid>_replacement`

Lifecycle retains its existing state and warranty behavior, existing attributes, unique ID, entity ID, user naming, enabled state, options, and Recorder identity. It no longer belongs to a Purchase subentry. An Asset with no Purchase or warranty still exposes Lifecycle with the existing not-specified style state.

Deployment is a normal enum sensor with canonical states `unknown`, `not_deployed`, and `deployed`. It resolves only the exact stored `ha_area_id` for display. Its deterministic Area state is `not_set`, `present`, or `missing`; a stale ID remains stored and is never repaired by name.

Installation Date is a normal native date sensor derived only from canonical Asset `installed_date`. It holds no independent canonical state and does not read Purchase subentry data directly. Purchase reconciliation and user provenance determine the canonical Asset value before exposure; both provenance modes are displayed identically. A date never implies or changes Deployment state, and the existing Deployment and Lifecycle compatibility attributes remain unchanged.

Relationships is an enabled-by-default diagnostic enum sensor. Its state is `none` when no references are stored, `present` when every exact stored Device Registry ID resolves, and `missing` when any stored ID does not resolve. `primary_state` uses `not_linked`, `present`, or `missing`; related entries use only `present` or `missing`. Current Device Registry names are derived display metadata and are never persisted. Registry presence means only that a `DeviceEntry` exists, not that the device is operational or available.

Asset ID is an enabled-by-default diagnostic sensor whose state is the permanent `DLxxxx` value. The short ID is not inserted into the Asset Device name merely to expose it.

Lifecycle Status is an enabled-by-default normal enum sensor. Its English machine states exactly match canonical Lifecycle values; Home Assistant translation infrastructure localizes state presentation. It exposes at most the current event's optional effective date, never full history.

Replacement is a diagnostic enum sensor with states `none`, `replaces`, `replaced_by`, and `chain_member`. A new instance defaults disabled when no active relationship exists and enabled when active replacement context exists. During canonical exposure reconciliation, an active relationship enables an existing entry only when `disabled_by` is `INTEGRATION`; user- and ConfigEntry-disabled entries are untouched. Voiding later never automatically disables the entity. Its unique ID remains `<asset_uuid>_replacement`, and it exposes only active predecessor/successor Asset IDs as lists. Historical and voided records are not entity attributes.

Home Assistant 2026.8 restricts a Device Registry device to one owning config entry. Eligibility validation uses the device's `config_entry_id` and its current owner config entry. Device Lifecycle rejects its own devices, service devices, conservative software/system exclusions, devices with no valid owner, and missing device IDs without using deprecated multi-config-entry ownership assumptions.

Related add/remove operations do not mutate Purchase or Runtime config subentries and are not blocked merely because the device is used elsewhere. Removing a related reference uses the stored relationship list, so it remains possible when the Device Registry device no longer exists.

Unlink and primary-device relationship replacement are allowed only when the current primary device is not referenced by an active:

- Purchase subentry through `device_ids`, or
- Runtime subentry through `device_id`

Dependency conflicts are reported to the user. Device Lifecycle does not rewrite dependent subentries automatically. This Home Assistant relationship operation removes the old primary and adds the validated new primary for the same physical Asset in one serialized Store mutation and one verified atomic save. It is unrelated to the physical Asset-to-Asset replacement graph. Failed validation, save, or read-back preserves the published old relationship.

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

1. Load and validate Store 3.1, explicitly migrating Store 1.1, 1.2, or 2.1 when required.
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

The preflight scans all Assets and all desired entity identities, including `<asset_uuid>_installed_date`, `<asset_uuid>_lifecycle_status`, and `<asset_uuid>_replacement`, before the first exposure-related Device or Entity Registry mutation. An exact Asset Device identifier may have zero or one active match. Multiple matches, external ownership, additional identifiers or connections, a foreign entity collision, a noncanonical Lifecycle/Runtime unique ID, a mismatched config entry, or multiple Runtime subentries for one Asset fail setup closed. An otherwise exact Device Lifecycle-owned projection found on one of the same parent entry's subentries is an incomplete derived projection and is safely moved back to the parent; metadata is never used to choose between devices or entities.

Registry reconciliation is idempotent. A valid existing Asset Device is reused; only a missing exact projection is created. Existing entity updates change only `device_id` and `config_subentry_id`; they do not rewrite entity ID, unique ID, name overrides, enabled state, options, categories, labels, or unrelated customization.

Device Registry and Entity Registry are not treated as one atomic transaction. Before each existing entity move, the migration retains its original entity ID, unique ID, device ID, and config-subentry ID. If a later step fails, attempted entity moves are restored in reverse order. Cleanup removes only an unreferenced Asset Device proven absent at preflight and created during the current setup attempt. A pre-existing device is never rollback cleanup.

If rollback itself fails, setup fails with an actionable error and Asset Store remains canonical and unchanged. A partial derived projection may remain. The next setup re-reads Store and both registries and reconciles the same deterministic desired state. It never allocates another Asset UUID or Asset ID, increments `next_asset_number`, rewrites Purchases or external references, remaps a missing external device, or adds a Store marker.

This defines the repair boundary: Device Lifecycle may recreate its own deterministic projection objects, but it never silently repairs canonical or external relationships. A missing external primary or related device stays as the same stored ID and is exposed as missing.

Store 1.1 and 1.2 migrations add `runtime.total_seconds: null` to every existing Asset. Legacy Runtime subentries have no `runtime_data_version`. Before their sensor is added, Device Lifecycle resolves the existing entity ID, reads native RestoreSensor data for that exact identity, requires a finite non-negative native value in hours, converts it with `Decimal` to seconds, and atomically compares-and-sets `null` to that value. A non-null canonical total always wins, so the import is exactly once. Missing, corrupt, unsafe-unit, or unpersistable restore data leaves the value null, preserves the entity-registry identity, adds no misleading zero sensor, and retries on reload.

New Runtime subentries contain `runtime_data_version: 1`. Only that provenance permits a safe atomic `null` to `"0"` initialization. Reconfiguration preserves the marker, and removing or recreating Runtime tracking never removes or resets the Asset total.

Elapsed Runtime uses a monotonic clock and three explicit components: committed canonical seconds, ordered sealed pending deltas, and the current active interval. The displayed value includes all three. Every five minutes while active, the entity seals elapsed time, advances its active baseline, and commits the delta. Stop, unavailable/unknown, unload, and normal shutdown also checkpoint. Inactive periodic callbacks retry pending deltas.

Each delta carries its expected canonical total. Store equality with the expected value applies the delta; equality with expected plus delta is idempotent success; any other value fails closed. An entity-level lock serializes periodic, source, unload, and shutdown callbacks, while the Store manager mutation lock serializes canonical writes. Failed saves retain sealed pending deltas without leaving the active timer running. Under healthy persistence, a hard crash loses only time since the last successful checkpoint, normally less than the five-minute interval. That bound does not apply while Store writes are failing.

## Store 1.1/1.2/2.1 to 3.1 migration

The explicit migration preserves exactly:

- Asset UUIDs
- Asset IDs
- `next_asset_number`
- Purchase UUIDs, ordering, and membership
- Home Assistant device references
- config-subentry references
- Runtime relationships
- entity unique IDs and legacy restored totals pending import
- Deployment state, Installation Date, Area, warranty, Runtime total, metadata, and provenance

For every existing 0.5.3 Asset, migration adds:

```text
deployment_state: unknown
ha_area_id: null
runtime:
  total_seconds: null
```

If `purchase_uuid` is non-null and `field_sources["purchase_uuid"]` is absent, the migration records `purchase` provenance. The migration is idempotent and does not infer deployment from any existing relationship or metadata.

Store 1.2 Assets also receive the null Runtime object directly. The composable migration first produces the established normalized 2.1 model, then adds to every existing Asset:

```text
lifecycle:
  status: unknown
  current_event_uuid: null
```

and adds empty top-level `lifecycle_events` and `replacement_records` dictionaries. It generates no synthetic lifecycle or replacement history. Store 2.1 inputs retain their exact Runtime totals and all other canonical data. Migration works on a detached copy and validates the complete Store 3.1 result.

After Store 3.1 migration, downgrading to Device Lifecycle 0.6.1 is unsupported. The old reader must reject Store major version 3 and must not rewrite it. Restore a complete backup made before the 0.7.0 upgrade for rollback.

## Reconciliation and persistence ordering

Current setup and legacy normalization follow this order:

1. Load and validate Asset Store, applying an explicit migration when required.
2. Reconcile Purchase subentries into Purchase and Asset records.
3. Reconcile Runtime subentries to existing or new Assets.
4. Validate all identity, provenance, membership, lifecycle-chain, and replacement-graph invariants.
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

- Quick Asset Entry -> one composite canonical Store transaction using the same snapshot primitives; no alternate Asset identity or Store
- maintenance schedules and history -> Asset UUID, with growing history in a future explicit Store schema
- Asset-owned Runtime totals and any future corrections -> Asset UUID
- future RMA cases -> replacement UUID, without redefining Purchase or Asset identity
- documents -> Purchase UUID or Asset UUID according to scope
- export/import -> preserve Asset UUID and Asset ID; treat Home Assistant device/entity IDs as instance-specific references

Growing histories do not belong in ConfigSubentries. ConfigSubentries remain suitable for active user configuration; persistent history belongs in explicitly versioned Device Lifecycle storage.

These boundaries reserve 0.8.x for Maintenance, 0.9.x for Portability & Hardening, and a later release for Documents. None is represented by placeholder 3.1 records.

## 0.7.1 workflow and schema impact

Device Lifecycle 0.7.1 adds Quick Add and related projection/UI behavior without changing canonical schemas:

- Store remains `3.1`
- ConfigEntry remains version `4`
- no Store or ConfigEntry migration runs
- first setup creates and loads the single parent entry, then continues through Home Assistant's supported next-flow mechanism into Quick Add
- Quick Add supports a conservatively validated Home Assistant physical-device source or manual entry and always requires final confirmation
- no Store write, `DLxxxx` allocation, lifecycle event, Purchase membership, or replacement record exists before confirmation
- the Home Assistant Device Registry is revalidated immediately before commit but is not transactionally locked with Asset Store
- all existing entity unique IDs, entity IDs, registry customizations, and Recorder continuity remain unchanged
- the exact old Finnish default parent title is normalized to `Device Lifecycle`; custom entry titles and Asset names are untouched

Quick Add drafts are flow-local and never canonical data. UI section payloads are flattened before the manager request; Asset Core has no knowledge of manual versus Home Assistant source. Confirmation uses human-readable names rather than raw UUIDs, and `DLxxxx` appears only when needed to distinguish duplicate predecessor names.

## 0.7.0 schema and migration impact

Device Lifecycle 0.7.0 adds canonical Lifecycle and Replacement domains. Therefore:

- Store major/minor is `3.1`
- config entry version remains `4`
- Store 1.1, 1.2, and 2.1 migrate explicitly to 3.1
- no config-entry migration is added
- Runtime entity unique IDs and entity IDs remain unchanged
- Lifecycle entity unique IDs and entity IDs remain unchanged
- Deployment, Installation Date, Relationships, and Asset ID identities remain unchanged
- new deterministic parent-owned identities are `<asset_uuid>_lifecycle_status` and `<asset_uuid>_replacement`
- no Device Registry ID or exposure status is stored
- lifecycle/replacement mutations preserve every other canonical domain

## Explicit non-goals for 0.7.0

Device Lifecycle 0.7.0 does not add:

- Asset archive or delete
- Asset merge
- automatic device discovery or metadata matching
- automatic stale-device rematching
- Quick Asset Entry or speculative Quick Add refactoring
- metadata aggregation from related devices
- lifecycle-history attributes or a custom history UI
- deployment history
- maintenance schedules or history
- a general Asset-level warranty editor
- Runtime reset or manual Runtime editing
- Recorder/statistics migration
- future replacement scheduling or N:M active replacement support
- export/import

It also does not add Asset deletion/purge/merge, RMA cases, Maintenance, maintenance history or schedules, Runtime reset/manual editing, Runtime-based maintenance, Recorder migration, Device Registry Area synchronization, Repairs, notifications, custom frontend surfaces, Documents, automatic Purchase/warranty/HA-relationship/Runtime/Deployment/Lifecycle transfer, or automatic lifecycle change when a replacement is created.

The 0.7.0 acceptance contract extends the existing Asset without replacing it: `asset_uuid` remains the only canonical technical identity; Store is 3.1 and ConfigEntry stays 4; the Asset Device is a recomputable projection; the old warranty Lifecycle remains continuous; Lifecycle Status and Replacement are new deterministic projections; Runtime configuration stays subentry-owned while history stays Asset-owned; external devices stay untouched references; ambiguous registry identity fails closed; and partial derived projection state remains deterministically recoverable.

## Explicit non-goals for 0.7.1

Version 0.7.1 does not add bulk Asset creation, Purchase creation inside Quick Add, a new warranty model, Runtime transfer or editing, automatic Area inference, automatic external-device matching, Maintenance, Documents, RMA cases, export/import, notifications, or a custom frontend. Its optional repository-level dashboard is a presentation-only side deliverable: it is not Asset Core or canonical persistence, the integration does not depend on it, and it changes neither Store 3.1 nor ConfigEntry version 4 nor Asset identity or Lifecycle semantics. Version 0.7.1 does not change normal standalone Lifecycle/Deployment independence or ordinary replacement side-effect isolation; only the explicit, confirmed composite Quick Add request can bundle the predecessor changes described above.
