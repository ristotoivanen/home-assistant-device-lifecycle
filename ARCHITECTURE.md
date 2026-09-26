# Device Lifecycle architecture

This document defines the Asset Core and Asset Exposure invariants through Device Lifecycle 0.7.7. Future releases must extend the model through explicit migrations instead of replacing Asset identity.

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

Device Lifecycle 0.7.7 uses Store major version `3`, minor version `1`, and parent ConfigEntry version `4`. Asset Exposure remains derived and adds no stored projection IDs, exposure state, workflow drafts, or alternate Asset identity. Versions 0.7.1 through 0.7.7 require no Store or ConfigEntry migration.

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

A `user` relationship is authoritative, and that includes an explicit choice of no Purchase (`purchase_uuid: null` with `user` provenance). No flow rewrites a Purchase subentry's legacy `device_ids`, so after a user relink or clear the subentry normally keeps listing the Asset's primary device. Every later reconciliation replays that projection. For each listed device whose Asset is user-managed, reconciliation therefore decides only whether this subentry's projection owns the Asset: it does exactly when the user-chosen `purchase_uuid` equals this subentry's Purchase. When it does not, reconciliation skips both the Purchase field projection (for example Installation date and warranty) and canonical membership for that Asset. It neither moves the link back, restores a cleared link, nor raises. The mismatch is the normal steady state after a user choice, not a conflict, so it cannot fail `async_setup_entry`. Before 0.7.4 this case raised `AssetStoreError` inside setup and left the whole entry in `SETUP_ERROR`. The provenance model itself is unchanged.

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

`ha_area_id` is the optional current deployment Area. It is not identity and does not change the Area of a linked external Home Assistant device. New selections must identify an existing Area. A stale stored Area ID is preserved for explicit repair, and Device Lifecycle never remaps it by name. Since 0.7.4 the Asset management UI names it as an unavailable Area instead of displaying the ID. The Deployment entity's attributes still report it as `missing`.

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

`effective_date` is an optional Home Assistant local calendar date that must not be in the future when it is submitted; see [persisted effective dates](#persisted-effective-dates-and-clock-rollback). `recorded_at` is generated by Device Lifecycle as an aware UTC timestamp. Users cannot edit or delete events. A mistake is corrected by appending another transition. A same-state request appends nothing, writes nothing, and triggers no reload.

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

Reasons are `unknown`, `planned_refresh`, `upgrade`, `failure`, `warranty_rma`, and `other`. `warranty_rma` is only a reason; 0.7.0 has no RMA case model. Both endpoints must be distinct existing Assets. Effective dates are optional and must not be in the future when submitted. Integration timestamps are aware UTC.

Only records with `voided_at: null` participate in the active graph. Store 3.1 retains all voided records permanently. Version 0.7.0 whole-graph validation enforces at most one active outgoing and one active incoming edge per Asset, rejects duplicate active edges and self-links, detects cycles of every length, and requires known effective dates to be non-decreasing along consecutive active edges. Unknown dates never imply order.

Voiding requires a non-empty reason. Correction is one serialized mutation: mark the old record void, create the new record, validate the complete graph, perform one verified save, then publish. Any mutation, graph, persistence, or readback failure leaves the old published relationship active.

Replacement never changes Lifecycle, Deployment, Installation Date, Area, Purchase, warranty, external Home Assistant relationships, Runtime, maintenance, or documents. In particular, a `warranty_rma` successor does not inherit the predecessor's acquisition Purchase or warranty.

Since 0.7.5 the Asset management UI creates a relationship only from the successor: the selected Asset is always the new Asset and the chosen target is always its predecessor (`replacement_replaces`). The predecessor shows the relationship read-only, and there is no user-facing action that creates it from the predecessor. This is a UI rule, not a schema change: the manager API, the record fields, the predecessor → successor direction, graph validation, and every record created before 0.7.5, including chains, are unchanged. Correction and voiding remain available from either endpoint.

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
9. A Device Registry ID is an external, non-authoritative reference. Its absence from the registry never invalidates Device Lifecycle's own stored data and never fails setup (see [Stale external device references at setup](#stale-external-device-references-at-setup)). A reference that no longer resolves is reported as an advisory Home Assistant Repairs issue that never changes it (see [Stale external device references in Repairs](#stale-external-device-references-in-repairs)).

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

Asset Device lookup collects every Device Registry device carrying the canonical identifier and fails closed:

```text
0 holders  -> no Asset Device yet (it may be created)
1 holder   -> accepted only when owned by the parent entry with exactly the
              canonical identifier and no connections; otherwise AssetStoreError
>1 holders -> AssetStoreError, even if one of them is exactly owned
```

Exposure preflight additionally accepts a single owned holder that is attached to a ConfigSubentry and repairs it to `config_subentry_id = null`; every other lookup also requires the parent attachment.

The lookup iterates the complete Device Registry. It never uses a single-result identifier lookup such as `async_get_device()`: another config entry can register its own device with the same identifier, and an older registry store can hold a same-entry duplicate that Home Assistant keeps behind one index slot, so a single-result lookup could hide exactly the ambiguity this rule must refuse.

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
5. Complete the logically separate legacy entity unique-ID migration. It changes unique IDs only; since 0.7.7 it moves no entity to a device or config subentry. It still logs one warning for each Purchase- or Runtime-listed device that Home Assistant would not attach an entity to.
6. Build the complete read-only 0.6.0 exposure plan.
7. Ensure deterministic Asset Devices.
8. Reparent existing Lifecycle entities to the parent ConfigEntry and Asset Device.
9. Relink existing Runtime entities to the Asset Device while preserving their Runtime subentry.
10. Publish `runtime_data` and forward sensor platform setup.
11. Create any missing parent-owned exposure entities and register the centralized relationship listener.

The preflight scans all Assets and all desired entity identities, including `<asset_uuid>_installed_date`, `<asset_uuid>_lifecycle_status`, and `<asset_uuid>_replacement`, before the first exposure-related Device or Entity Registry mutation. An exact Asset Device identifier may have zero or one active match. Multiple matches, external ownership, additional identifiers or connections, a foreign entity collision, a noncanonical Lifecycle/Runtime unique ID, a mismatched config entry, or multiple Runtime subentries for one Asset fail setup closed. An otherwise exact Device Lifecycle-owned projection found on one of the same parent entry's subentries is an incomplete derived projection and is safely moved back to the parent; metadata is never used to choose between devices or entities.

Registry reconciliation is idempotent. A valid existing Asset Device is reused; only a missing exact projection is created. Existing entity updates change only `device_id` and `config_subentry_id`; they do not rewrite entity ID, unique ID, name overrides, enabled state, options, categories, labels, or unrelated customization. Since 0.7.7 a setup or reload whose projection is already canonical makes no Entity Registry placement update at all.

Device Registry and Entity Registry are not treated as one atomic transaction. Before each existing entity move, the migration retains its original entity ID, unique ID, device ID, and config-subentry ID. Because the legacy unique-ID migration no longer moves entities (since 0.7.7), that retained placement is the placement the entity had when setup started. If a later step fails, attempted entity moves are restored in reverse order. Cleanup removes only an unreferenced Asset Device proven absent at preflight and created during the current setup attempt. A pre-existing device is never rollback cleanup.

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
7. Migrate Entity Registry unique IDs only, preserving `entity_id`. Device and config-subentry placement are left untouched (since 0.7.7).
8. Build and validate the complete exposure migration plan without mutation.
9. Ensure/reconcile owned Asset Devices and existing entity placement with compensating rollback.
10. Set `runtime_data` and set up entity platforms.

This ordering makes repeated setup idempotent and avoids allocating a second Asset merely because a previous run stopped between Store and config-entry writes.

## 0.7.7 Entity Registry placement ownership

Device Lifecycle 0.7.7 changes one setup rule. It changes no canonical schema, identity, or invariant: Store remains 3.1, ConfigEntry remains version 4, no migration runs, the Home Assistant 2026.8.0 minimum is unchanged, and Asset UUIDs, `DLxxxx` Asset IDs, Asset Device identity, entity unique IDs, entity IDs, Runtime totals, and Recorder continuity are unchanged. Only `migration.py` changes; `exposure.py`, including its rollback, `storage.py`, `sensor.py`, and `stale_references.py` are untouched.

Responsibilities are now separate:

| Step | Owns | Does not do |
|---|---|---|
| Legacy entity migration (`migration.py`) | legacy entity discovery, the 0.4.x unique-ID move to Asset UUID-based unique IDs, the ambiguity check that refuses to merge a legacy and an Asset Core entity, and the stale-device warning | change `device_id` or `config_subentry_id` of any entity |
| Exposure reconciliation (`exposure.py`) | canonical placement: every entity on its Asset Device, Lifecycle and the other Asset exposure entities parent-owned, Runtime in its Runtime subentry, with compensating rollback | change unique IDs |
| Home Assistant entity platform | the same device and config subentry when the sensor platform adds each entity | |

Before 0.7.7 the migration also attached each Lifecycle entity to the external Home Assistant device and the Purchase subentry that a configuration lists, and each Runtime entity to that external device, even though exposure then moved them straight back. Every setup and reload therefore wrote those placements twice. If exposure then failed, the temporary placement stayed: removing that Purchase in this state made Home Assistant delete the Lifecycle entity from the Entity Registry, and exposure's rollback restored the temporary placement instead of the one setup started from. Since 0.7.7:

- an already canonical setup or reload writes no Entity Registry placement update
- a setup that fails leaves every entity where it was when setup started, and exposure's unchanged rollback restores exactly that placement
- a Purchase configuration that still lists the device of an Asset the person moved to another Purchase no longer attaches that Asset's Lifecycle entity to it, even temporarily
- supported upgrades from 0.4.x, 0.5.x, 0.6.x, and earlier 0.7.x keep converging to the same canonical placement, because the entities those releases stored never needed the migration to place them: exposure establishes the placement from their unique IDs alone

The stale-device warning is unchanged: the migration still logs it once for each Purchase- or Runtime-listed device that Home Assistant would not attach an entity to, per setup, with the same text, now as its own step rather than as a side effect of computing a device link. The migration's `Migrated Device Lifecycle entity` informational log line now appears only when a unique ID actually moves.

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

These boundaries reserve 0.8.x for Maintenance, including reversible Asset archive and restore, 0.9.x for Portability, Data Safety & Hardening, including controlled permanent Asset deletion, and a later release for Documents. None is represented by placeholder 3.1 records. See [Planned: Asset archive and permanent deletion](#planned-asset-archive-and-permanent-deletion).

## Planned: 0.8.x Maintenance usability constraints

Status: **planning only**. Nothing in this section is implemented, and it defines no user interface, OptionsFlow structure, entity, dashboard, or Store field. It records design constraints that every 0.8.x Maintenance domain, storage, and workflow decision must satisfy.

The canonical Maintenance Store schema is [Maintenance Store 4.x frozen schema](docs/maintenance-store-v4-schema.md). Status: **FROZEN** (approved 2026-09-26), not implemented, Store version number not yet assigned. The implementation order and activation boundary are in [Maintenance implementation plan](docs/maintenance-implementation-plan.md).

**Usability is a first-class 0.8.x design constraint.** Maintenance adds growing, date- and Runtime-related history, which is internally more complex than any earlier Asset domain. That complexity belongs in the data model and the implementation, not in the person's everyday workflow. The constraints below extend the existing management rules that user-facing copy never shows internal identifiers (see [Summaries and identifier safety](#summaries-and-identifier-safety)) and that a mutation target is never chosen implicitly.

### Minimal normal path

Common Maintenance workflows must stay very simple. Creating an ordinary maintenance schedule, marking maintenance as done, and recording an ordinary maintenance event must require as few steps and as few mandatory fields as the data model allows. A normal workflow asks only for information the person can reasonably be expected to know.

This is a design principle, not a user-interface contract. The number of steps, the controls, the field names, and the flow structure are decided in the Maintenance UX v0.1 checkpoint below.

### Internal concepts must not leak into normal workflows

Internal architecture concepts may appear in this document and in the implementation, but a person must not need to understand them to create an ordinary schedule, mark maintenance done, or record an ordinary maintenance event. Examples of such internal concepts:

- UUIDs and other internal identifiers
- the Store structure
- schedule anchors such as an `initial_anchor` or an `effective_anchor`
- Runtime snapshot mechanics
- derived-state and due-calculation mechanics

### Progressive disclosure

Rare or advanced inputs are shown only when the person needs them, not in the normal path. Examples:

- a backdated maintenance event
- a historical Runtime reading
- one maintenance event that satisfies several schedules
- correcting or voiding a maintenance event
- other comparable exceptional cases

`OPEN DESIGN — deferred to Maintenance UX v0.1`: how and where each advanced input is offered.

### Safe defaults without guessing

The system should derive or prefill information when it can do so safely, without guessing. Examples of what this can mean for Maintenance:

- today's date as the default maintenance date
- a snapshot of the current canonical Runtime total when maintenance is recorded as done now and that total is known
- the schedule name as the default title of a maintenance event

**Convenience must never override data correctness.** A default is only offered when it follows from known data. Device Lifecycle must not invent unknown historical data, such as a past maintenance date or the Runtime total at a past maintenance, to make a workflow easier. A known current value is never presented as a historical one.

### UNKNOWN is a valid user outcome

Unknown starting information is not a validation error. The person must be able to state, for example:

- that the previous maintenance is unknown
- that the Runtime total at a historical maintenance is unknown
- that the maintenance baseline is unknown

without being forced to guess a date or a Runtime value. UNKNOWN is then a deliberate and safe state, consistent with how Lifecycle `unknown`, Deployment `unknown`, and an uninitialized Runtime total (`null`, never zero) are already treated. How a schedule with an unknown baseline reports its due state is part of the due and calendar semantics.

### Schedule baseline lock

A maintenance schedule's explicit calculation baseline, the anchor its due calculation starts from (`initial_anchor` in the internal examples above), may be changed only while the schedule has never been referenced by any maintenance event, including a voided event.

- A new schedule with no history may still change where its counting starts.
- Once any maintenance event has referenced the schedule, the baseline is locked.
- A voided event still counts as a historical reference. Voiding an event never makes a schedule unused again and never unlocks the baseline.
- Changing the baseline must not become a hidden way to reset a schedule or restart its countdown. Once maintenance history exists, a new cycle starts from a maintenance event, never from moving the baseline.

For the person, this means the starting point of a schedule can be corrected only before the schedule's first maintenance event. After that, no baseline reset is offered; correcting and voiding events remain the historical mechanisms, and voiding does not make the baseline editable again. How this is presented is decided in the Maintenance UX v0.1 checkpoint below.

Store boundary: the Store 4.x design must keep enough canonical maintenance history to determine whether a schedule has ever been referenced by any maintenance event, including voided events. The lock is derived from that canonical history. It must not be a separately stored flag that has to be kept in sync, and no field is added for it when the event-to-schedule references already answer the question. The frozen Store 4.x structure is in [Maintenance Store 4.x frozen schema](docs/maintenance-store-v4-schema.md).

### Design review rule

Every new 0.8.x domain and storage decision is also judged by one question:

> Can the common user workflow remain simple without exposing the internal data model?

If the answer is no, the design must be revised before internal complexity reaches the person.

### Maintenance UX v0.1 checkpoint

The concrete normal Maintenance user experience, **Maintenance UX v0.1**, is designed later, as an explicit design checkpoint: after the Maintenance due and calendar semantics are sufficiently settled, and before the Store 4.x design is finally frozen, so that the storage design can still change if the workflow requires it.

Superseded order: the Maintenance Store 4.x schema was frozen on 2026-09-26, before this checkpoint took place (see [Maintenance Store 4.x frozen schema](docs/maintenance-store-v4-schema.md)). Maintenance UX v0.1 is therefore designed within the frozen schema. A workflow need that the frozen schema cannot meet requires an explicit schema revision and review.

### Maintenance preparation reminder

A maintenance schedule can need an optional advance reminder that helps the person prepare for upcoming maintenance. For example, when a ventilation filter change is due in about a month, the person can be reminded to order new filters. This is a generic Maintenance use case, not a feature for one kind of device: other examples are buying consumables, obtaining a spare part, or preparing other material or tools the maintenance needs.

For example, a schedule "Ventilation filter change" every 6 months can have a preparation reminder 30 days before its due date with the message "Remember to order new filters". The same model applies to any preparation, such as obtaining a spare part, a service kit, a filter, a lubricant, or other maintenance supplies.

**0.8.x uses a calendar-based preparation reminder only.** A preparation reminder:

- is optional, and in ordinary use very simple
- is expressed to the person in calendar days before the due date, for example 30 days before
- is based only on a known calendar due date. When no calendar due date is known, no preparation reminder is active
- has no Runtime-relative lead in 0.8.x, such as a number of Runtime hours before due
- never changes the schedule's due state, such as `OK`, `UNKNOWN`, `DUE`, or `OVERDUE`, and never changes its due calculation
- is a derived live projection, not Maintenance history: its activation is never stored as a historical fact, and notification delivery is not part of canonical Maintenance history
- must not suggest that time remains before maintenance once the schedule is `DUE` or `OVERDUE`
- is not a product catalogue, inventory, web shop, or automatic ordering system

Store boundary: 0.8.x needs persistent preparation reminder configuration for the calendar lead and an optional message. The frozen Store 4.x field structure is in [Maintenance Store 4.x frozen schema](docs/maintenance-store-v4-schema.md).

Later extension, not implemented in 0.8.x: Runtime schedules may later derive an estimated calendar due date from observed Runtime consumption rate, for example "approximately 30 days remaining". Such a date is a projection or forecast, never canonical maintenance data: it is not a maintenance event, not a baseline, and not a canonical due date, and it never changes historical data. If the estimate cannot be derived reliably, for example because observed Runtime data is missing or unreliable, it remains unknown rather than being guessed from an assumed rate.

`OPEN DESIGN — deferred to Maintenance UX v0.1`: how the reminder is presented, notification delivery, and any entities. It is designed together with the rest of the normal Maintenance path and is not part of any current release.

## Planned: Asset archive and permanent deletion

Status: **planning only**. Nothing in this section is implemented. Device Lifecycle 0.7.7 provides neither Asset archive nor Asset deletion, Store 3.1 contains no archive or deletion state, and ConfigEntry remains version 4. This section records the semantics and release boundary that later designs must follow. Where a detail is not yet decided, it is marked `OPEN DESIGN` with the release whose design owns it.

Two different operations are planned, in two different releases:

| | Archive Asset | Permanent deletion (purge) |
|---|---|---|
| Purpose | take an Asset out of active use while keeping it | remove a record that must not be kept even archived |
| Reversible | yes, through restore | never |
| Identity | preserved | retired permanently, never reused |
| History | preserved | removed, except the minimal historical reference below |
| Release | 0.8.x — Maintenance | 0.9.x — Portability, Data Safety & Hardening |

Neither operation changes the [identity invariants](#identity-invariants): an Asset is one physical item, `asset_uuid` is its canonical technical identity, `DLxxxx` is its permanent human-facing identity, Asset IDs are allocated monotonically, and neither identifier is ever recycled. A Home Assistant device, Purchase, Runtime configuration, Deployment, Lifecycle, Replacement, or Maintenance record never defines or redefines Asset identity, and archive or purge must not introduce such a rule.

### Archive Asset (0.8.x)

Archive is a normal user operation. It removes an Asset from active use without destroying its identity or history. Maintenance history, planned for 0.8.x, is growing Asset-owned history, so 0.8.x needs a way to retire Assets that keeps that history intact rather than a way to destroy it.

Locked semantics:

1. Archive preserves `asset_uuid`.
2. Archive preserves the `DLxxxx` Asset ID and never releases it for reuse. `next_asset_number` is unchanged.
3. Archive preserves all historical data: Purchase membership and relationship provenance, the Lifecycle event chain, every Replacement record including voided records, the canonical Runtime total, and future Maintenance history.
4. Archive is reversible. Restoring an archived Asset returns the same Asset: it keeps the same `asset_uuid` and `DLxxxx`, allocates nothing, and does not behave as a newly created Asset.
5. Entity `unique_id` values must remain stable across archive and restore. Archive and restore must not silently break Recorder continuity: any case in which the chosen design cannot keep an entity's history continuous must be explicit and documented. This rule does not claim that Recorder continuity can always be guaranteed; the guarantees themselves are open design below.
6. Archive is not a Lifecycle status. Lifecycle `disposed`, `retired`, and `lost` describe the physical item; archive describes whether Device Lifecycle keeps the record in active use. Archiving is neither `disposed` nor a replacement for it, and recording a Lifecycle status never archives an Asset.
7. Archive is not permanent deletion. An archived Asset remains a complete canonical Asset.
8. Archive and restore are explicit user mutations. Device Lifecycle never archives or restores an Asset automatically, for example because its Home Assistant device is missing, its Lifecycle status changed, or it has been replaced.
9. Archive state is canonical Store data and therefore requires an explicit, versioned Store migration. The Store 3.1 schema does not represent it.

Archive must not succeed in a state where it would leave the Asset or its relationships inconsistent. At minimum, an Asset with an active dependency must not be archivable until that dependency is resolved, for example:

- a Deployment that still records the Asset as installed (`deployed`)
- active Runtime tracking by a Runtime configuration
- any other active relationship whose archiving would leave that relationship inconsistent

`OPEN DESIGN — deferred to 0.8.x design`:

- the exact dependency matrix, including how primary and related Home Assistant device relationships, Purchase configurations, active replacement relationships, and Maintenance schedules are treated
- whether any Lifecycle status is required before archive, given that archive itself never changes Lifecycle
- the Store representation of archive state, its migration, and any archive or restore history
- how an archived Asset is exposed in Home Assistant: its Asset Device and its entities
- the Entity Registry lifecycle during archive: whether an archived Asset's entities are removed, disabled, or retained
- which `entity_id` preservation guarantees archive and restore give
- which Recorder and history continuity guarantees archive and restore give. If archive removes an Entity Registry entry, whether restore gets the same `entity_id` back can depend on Home Assistant's retention of deleted entities, which is time-limited. This section does not assume that retention for any archive design
- how archived Assets appear in Asset management, Quick Add targets, replacement targets, and Repairs
- how the optional dashboard's existing "Archived" inventory group relates to archive state. Today that group is only a presentation of Lifecycle `retired`, `disposed`, and `lost` and has no connection to this planned operation

### Permanent deletion / purge (0.9.x)

Permanent deletion is out of scope for 0.8.x. It belongs to **0.9.x — Portability, Data Safety & Hardening**, because irreversible data destruction must not be introduced before export, recovery, and historical-reference semantics exist.

Purge is an exceptional, explicit, and irreversible operation for records that must not be kept even archived, for example an Asset created by mistake or a test Asset.

Locked principles:

1. An Asset must be archived before it can be purged.
2. An Asset must not be purged while its Deployment records it as installed.
3. Active Runtime tracking must be resolved before purge.
4. Active Maintenance schedule dependencies must be resolved before purge.
5. Home Assistant device relationships must be removed or explicitly handled before purge. Purge never mutates an external Home Assistant device.
6. Replacement relationships must be resolved in a defined way before purge. History must not become inconsistent (see below).
7. The user must be shown what data will be removed before confirming.
8. Purge requires an explicit confirmation.
9. Purge is irreversible. Restore does not apply to a purged Asset.
10. The purged `DLxxxx` is never reused. `next_asset_number` never decreases, so it stays greater than every Asset number ever allocated, including purged ones.
11. The purged `asset_uuid` is never reused. No later creation, including an idempotent Quick Create replay that carries that UUID, may recreate an Asset under it.

`OPEN DESIGN — deferred to 0.9.x design`: the user flow, the storage implementation and migration, exactly which Asset-owned data is removed and which is kept, how Purchase membership, Lifecycle events, Replacement records, Runtime totals, and Maintenance history that belong to or reference the Asset are handled, what happens to the Asset Device and entities, and how export and recovery relate to purge.

### Historical references to purged Assets

Purge must not make history inconsistent. Records that remain after a purge can still refer to the purged Asset: a Purchase once contained it, and a Replacement record names it as predecessor or successor. The whole-Store validation that today requires every referenced Asset to exist must be able to interpret such a reference, and the identity invariants must remain provable after the Asset record is gone.

The preferred design direction is a minimal historical tombstone, a persistent reference that keeps only the identity needed to interpret history. Semantically:

```text
DeletedAssetReference
├── asset_uuid
├── asset_id
└── deleted_at
```

A tombstone:

- is not an active Asset and is never restored as one
- must not create Device Lifecycle entities
- must not create a Home Assistant Device Registry device
- must not take part in Runtime, Maintenance, Deployment, or normal Asset management logic
- carries no name, metadata, or history beyond the fields needed to interpret a reference

`OPEN DESIGN — deferred to 0.9.x design`: the exact tombstone storage model, its fields beyond the minimal identity above, its Store migration, and how history that references a tombstone is presented.

## 0.7.5 Asset management flow and setup resilience

Device Lifecycle 0.7.5 changes the Asset management OptionsFlow and one setup rule: the legacy entity relink in `migration.py` no longer fails setup on an external device that has left the Device Registry. It changes no canonical schema, identity, or invariant. Store remains 3.1, ConfigEntry remains version 4, no migration runs, and Asset UUIDs, `DLxxxx` Asset IDs, Asset Device identity, entity unique IDs, entity IDs, entity translations, and Recorder continuity are unchanged. `storage.py`, `models.py`, `sensor.py`, `exposure.py`, and `const.py` are untouched.

### Hub, views, and editors

The hub (`manage_asset_menu`) has five rows in this order: Details & warranty, Installation & location, Lifecycle & replacement, Home Assistant devices, Choose another device. Rows 1–4 carry the one-line summaries described under 0.7.4.

Rows 1–3 open read-only section menus (`asset_details_warranty_menu`, `asset_installation_menu`, `asset_lifecycle_replacement_menu`). A section renders the Asset's current facts through `description_placeholders` and lists the editors for its area as menu options, followed by a Back row. Rendering a section, the hub, a submenu, or the selector performs no Store write and no reload. The editors are the existing forms, unchanged in what they write:

| Section | Actions |
|---|---|
| Details & warranty | `edit_asset_metadata`, `change_asset_purchase` |
| Installation & location | `asset_deployment` |
| Lifecycle & replacement | `asset_lifecycle`, the `asset_replacement` submenu |
| Replacement submenu | `replacement_replaces`, `manage_asset_replacement` |
| Home Assistant devices | `manage_primary_device`, `add_related_device`, `remove_related_device` |

Details & warranty renders the Purchase link and the warranty as separate facts, because the warranty is Asset-owned. Lifecycle & replacement presents both domains in one view while they stay separate internally: a lifecycle mutation never creates or changes a replacement record, and a replacement mutation never changes Lifecycle.

### Navigation contract

A successful mutation, including a canonical no-op, returns to the immediate logical parent of the form it was started from:

| Form | Returns to |
|---|---|
| Asset details, Linked purchase | Details & warranty |
| Installation & location, and its location-clearing confirmation | Installation & location |
| Lifecycle, its Disposed confirmation, and a same-status no-op | Lifecycle & replacement |
| This Asset replaces…, correct, void | Replacement submenu |
| Primary, add related, remove related | Home Assistant devices |

Each section's and submenu's Back row goes exactly one level up as real menu navigation: sections and the Home Assistant devices submenu return to the hub, and the Replacement submenu returns to Lifecycle & replacement. Returning is navigation only: the same `asset_uuid` stays selected, and no extra Store write, reload, or flow restart happens. Sections consume a pending result the same way submenus do, so a result appears once where the person lands and never follows Back to the hub.

### Forms: actions, cancellation, and validation

Every Asset management form step has a semantic `submit` label in its translations: Save and return, Add and return, Remove and return, Void and return, Continue for a step that leads to another form, Open for the Asset selector, and Add device for the Quick Add confirmation. The Purchase and Runtime tracking subentry forms name their action the same way: Add purchase and Add runtime tracking where the step creates one, Save changes where it saves an existing one, and Continue for the first Runtime tracking step, which only leads to the source step. Creating or saving ends on Home Assistant's own confirmation, which the person closes, so those labels do not promise an immediate return; a saved edit is confirmed as "Purchase updated." or "Runtime tracking updated.". Home Assistant forms have no Back control; the top-left X closes the form and ends the flow without saving, which the flow manager handles, not the integration. Every edit, mutation, or action form, including the Purchase and Runtime subentry forms, therefore ends its description with one line saying so. Menus, section views, the hub, and the Asset selector have a Back row or nothing to discard and never carry that line. A test inventory requires every form step to be classified, so a new form cannot appear without the decision being made.

A mutation target is never preselected. Select-based targets (the replacement target, the relationship to correct or void, the related device to remove) start on the `NOT_SELECTED` placeholder. The primary and add-related Home Assistant device pickers are optional in the schema only and have no default, so an empty submit reaches the flow instead of being stopped by the frontend. The device picker shows no required marker in either schema form on Home Assistant 2026.8.0 and 2026.9.3. A missing choice, reason, or confirmation is reported on its own field, the form stays open, and the flow performs no Store write, no reload, and no mutation.

Required text fields keep `vol.Required` and their required marker. Home Assistant's frontend rejects an empty required field client-side with its generic message before the flow runs, so the integration cannot word that case; a whitespace-only value reaches the flow and is reported on its field.

Voiding a replacement requires both a non-empty reason and the confirmation on the Void form. An unconfirmed or reasonless submit stays on that form with the reason preserved and the missing field named, instead of returning as if something had been done. The location-clearing and Disposed confirmations keep their 0.7.4 decline semantics.

### Stale external device references at setup

Device Lifecycle stores Device Registry IDs it does not own: primary and related `ha_device_refs`, Purchase subentry `device_ids`, and a Runtime subentry's `device_id`. Another integration can remove any of those devices at any time. Every resolution site treats a missing device as an unresolved reference:

| Site | Missing device |
|---|---|
| Purchase and Runtime reconciliation (`storage.py`) | resolved to `None`; no metadata refresh, no write |
| Exposure preflight and Asset Device projection (`exposure.py`) | compares stored IDs only; never resolves external devices |
| Relationships entity (`sensor.py`) | reports `missing` for that reference only |
| Asset management UI (`config_flow.py`) | named as an unavailable Home Assistant device; new selections must exist |
| Legacy entity migration (`migration.py`) | logs one warning; links no device and moves no subentry (since 0.7.7; in 0.7.5 and 0.7.6 it skipped the device link only) |

Before 0.7.5 the legacy relink asked the Entity Registry to attach the Asset's Lifecycle or Runtime entity to every device a Purchase or Runtime subentry lists. Home Assistant refuses an unknown device with `ValueError`, so one removed device left the whole entry in `SETUP_ERROR` on every later setup. The relink now asks first whether Home Assistant will attach an entity to that ID, using the same test Home Assistant's own validation applies: exact ID membership in the device mapping on 2026.8, and `async_get(device_id, include_composite_devices=False)` on 2026.9+, which also excludes pre-migration composite IDs. If it will not, the relink leaves the entity's device link alone, still performs any unique-ID or subentry move, and logs one warning per stale reference and setup naming the Asset ID and the configuration that lists it. Exposure then places the entity on its Asset Device as before. No broad exception handler is involved, so unrelated errors still fail setup. Since 0.7.7 the migration links no device and moves no subentry at all, so this check decides only the warning; see [0.7.7 Entity Registry placement ownership](#077-entity-registry-placement-ownership).

The stored reference is preserved exactly: no Store write, no subentry rewrite, no migration, no automatic cleanup, and no automatic selection of another device. Entity unique IDs, entity IDs, and registry entries are unchanged. Repair is an explicit user action through the existing flows: remove the device from the Purchase configuration (whose form rejects a missing device until it is deselected) or delete the Runtime configuration, then replace or unlink the primary device. Home Assistant Repairs integration is not part of 0.7.5; the advisory issues that followed are described in [Stale external device references in Repairs](#stale-external-device-references-in-repairs).

## Stale external device references in Repairs

Every stored Asset relationship to a Home Assistant device that no longer resolves is reported as one Home Assistant Repairs issue. The issues are advisory and derived; they are never an alternative way to change data. The implementation is `stale_references.py`, wired into `async_setup_entry` and `async_remove_entry` in `__init__.py`. Store remains 3.1, ConfigEntry remains version 4, no migration runs, and Device Lifecycle keeps no bookkeeping of its own: Home Assistant's issue registry holds the issues.

**Source.** Only the canonical Asset `ha_device_refs` (primary and related) are collected. Migration and exposure output are not used, because they deliberately skip unresolved references. Purchase `device_ids` and a Runtime `device_id` are not read: reconciliation projects every device they list onto an Asset primary relationship, so one missing device that a Purchase, a Runtime configuration, and the Asset primary all name is exactly one primary issue. The steps for removing the Purchase and Runtime dependencies are in that issue's text.

**Resolution.** A reference is unresolved exactly when `device_registry.async_get(device_id)` returns `None`, called without keyword arguments on both 2026.8 and 2026.9+. It is the same test the Relationships entity and the Asset management UI apply, so Repairs never calls a device missing that those surfaces name. On both releases a pre-migration composite device ID resolves to Home Assistant's read-only stand-in while any of its split devices remains, so it is not reported. On 2026.9+ a child device resolves. The legacy entity migration's warning (`migration._device_can_be_linked`) keeps its stricter question, whether Home Assistant would attach an entity to the ID, which excludes composite IDs. The two tests answer different questions and are intentionally not unified here.

**Identity and ownership.** The issue ID is `stale_device_{role}_{asset_uuid}_{digest}`, with `role` `primary` or `related` and `digest` the first 32 hexadecimal characters of the SHA-256 of the stored device ID encoded as UTF-8. A stored device ID is only validated as a non-empty string, so hashing keeps every ID in one closed grammar and keeps the raw ID out of it. Neither names nor entity IDs are part of the identity. An issue is owned by this feature exactly when its domain is `device_lifecycle` and its ID matches that grammar in full. Translation key and `data` are not used for ownership, because an issue that is not persistent returns after a restart without either. Issues use `is_fixable=False`, `is_persistent=False`, severity `warning`, translation keys `stale_primary_device` and `stale_related_device`, and placeholders `asset_name` and `asset_id` only. There is no `RepairsFlow` and no `repairs.py` platform.

**Lifecycle.** One idempotent reconciliation computes the desired set, creates or updates each desired issue, and deletes only owned issues that are no longer desired. It runs:

- during setup, after Store reconciliation, the legacy entity migration, and exposure reconciliation
- on every Device Registry `create` or `remove` event while the entry is loaded, recomputing the whole set; the removal of a composite's last split device carries the split's ID, not the stored one; `update` events keep the device ID and are ignored

Updating in place keeps each issue's creation time and any dismissal, and Home Assistant announces only real changes, so a reload with unchanged state produces no issue events. An explicit repair through the existing flows (Purchase edit, Runtime removal, primary replace or unlink, related removal) changes Store or subentry data, and that change reloads the entry, which reconciles again. A device that Home Assistant restores under its old ID clears its issue. On 2026.8 a restore that carries no device information announces nothing, and the issue then clears on the next setup. Unloading only stops listening and deletes nothing. Removing the config entry deletes every owned issue and nothing else.

**Non-destructive.** Reconciling issues never writes the Store, a config subentry, the Device Registry, or the Entity Registry. It never removes, rewrites, or re-points a stored reference. Asset UUIDs, `DLxxxx` Asset IDs, entity unique IDs, entity IDs, and Recorder continuity are unchanged.

## 0.7.4 Asset management flow

This section describes the 0.7.4 contracts. The hub layout, return destinations, and void confirmation semantics were revised in 0.7.5 (see [0.7.5 Asset management flow and setup resilience](#075-asset-management-flow-and-setup-resilience)); reload continuity, result messages, summaries, and identifier safety still apply as written.

Device Lifecycle 0.7.4 changes the Asset management OptionsFlow and one reconciliation rule (see [Purchase creation and reconciliation](#purchase-creation-and-reconciliation)). It changes no canonical schema, identity, or invariant. Store remains 3.1, ConfigEntry remains version 4, no migration runs, and Asset UUIDs, `DLxxxx` Asset IDs, Asset Device identity, entity unique IDs, entity IDs, entity translations, and Recorder continuity are unchanged. `sensor.py`, `exposure.py`, and `migration.py` are untouched.

### Navigation contract

Selecting an Asset stores only its `asset_uuid` in the flow and opens a seven-row hub menu for it, always in this order: Asset details, Purchase & warranty, Installation & location, Lifecycle, Replacement, Home Assistant devices, Choose another device. Asset selectors use `asset_uuid` as the option value and `Name · DLxxxx` as the label, sorted by case-folded name and then `asset_id`.

- The four direct editors (details, Purchase, installation, Lifecycle) return to the same Asset's hub after a save, including a canonical no-op.
- Replacement and Home Assistant device operations return to their own submenu. Each submenu's last row returns to the hub as real menu navigation, never as a no-op submit.
- Choose another device reopens the selector and replaces the selected `asset_uuid`.
- Rendering the hub, a submenu, or the selector performs no Store write and no reload.

### Reload continuity

Every Asset mutation that changes canonical Store data still reloads the parent ConfigEntry, so that the Asset Device and entities are rebuilt from the new snapshot. Which mutations reload is unchanged from 0.7.3; what changed is *how*. Earlier releases scheduled a fire-and-forget reload and ended the flow with `CREATE_ENTRY`. A flow that tried to continue would race the reload, which tears down `runtime_data` and installs a new `AssetStoreManager`.

Since 0.7.4 a mutation awaits `hass.config_entries.async_reload(entry_id)` and continues only after it has finished:

1. The manager mutation completes and is persisted and verified as before.
2. If canonical data changed, the flow awaits the reload. A canonical no-op skips it.
3. The flow continues only when `entry.state is ConfigEntryState.LOADED` **and** `entry.runtime_data` is an `AssetStoreManager`. The boolean returned by `async_reload` is deliberately not part of this test. Home Assistant deletes `runtime_data` only after a successful unload, so a failed unload returns `False` while a stale manager is still attached. A disabled entry returns `True` without ever being set up again. Every `False`-returning path leaves a non-`LOADED` state, so the boolean would only add false negatives.
4. If that test fails, the flow aborts with `entry_not_loaded`. The mutation is already committed; only the continuation stops.
5. Otherwise the flow records the result (below) and renders its destination.

Nothing but identity crosses the reload. The flow keeps `_selected_asset_uuid` and never caches an Asset dict or a manager instance. Its `_manager` accessor reads `config_entry.runtime_data` on every access, so each step after a reload works against the manager that setup installed. A successful reload therefore never ends the OptionsFlow. A second mutation in the same flow simply goes through the same cycle against the new manager.

The `entry_not_loaded` guard from 0.7.2 on the first Asset management step is unchanged.

### Result messages

A mutation stores one completion key in the flow (`_last_result`), for example `asset_lifecycle_updated`. The next rendered hub or submenu consumes it: the key is resolved through the integration's own `options.create_entry` translations in the Home Assistant language and then cleared, so it is shown exactly once. The same-status Lifecycle no-op uses `asset_lifecycle_unchanged` with the current status label. Consequences:

- the message appears on the operation's destination only, and the next independent render shows none
- switching to another Asset clears a pending message, so it never moves from Asset A to Asset B
- a submenu consumes its own message, so Back to the hub shows none
- declining a confirmation stores no key
- a key without a translation renders as empty text, never as the raw key

### Summaries and identifier safety

Rows 1–6 of the hub carry one-line summaries passed as `description_placeholders` and rendered through Home Assistant's `menu_option_descriptions` translation structure. The frontend substitutes the placeholders. Summaries are computed on every render from canonical data through read-only helpers, which never mutate the Store and never repair stale references. Each summary is capped at 60 characters, and individual names at 24 characters, without leaving a dangling separator. Replacement summaries use only active records, so a voided record is not shown. The Purchase and warranty halves of the Purchase & warranty summary are rendered independently, because a warranty belongs to the Asset and not to the Purchase shown beside it.

User-facing management copy never contains an `asset_uuid`, a Home Assistant device ID, an Area ID, or a `replacement_uuid`. `DLxxxx` is the intended human identifier. Stale references are displayed as unavailable and are never repaired on render:

- a missing device is labelled as an unavailable Home Assistant device, numbered in stored order only when several stale references must be distinguished
- selectors that remove a stale device keep the real stored device ID as the option *value* while the *label* stays generic, so the exact reference is removed
- a deleted Area is labelled as an unavailable Area and remains stored until explicitly cleared or replaced

### Confirmation cancel semantics

Three changes require a separate confirmation step. Declining one, by submitting it without its confirmation box ticked, returns to the step it came from:

| Confirmation | Declining returns to |
|---|---|
| Clear location on Not installed | Installation & location editor |
| Record Disposed | Lifecycle editor |
| Void replacement | Replacement management editor |

Declining performs no Store write, no reload, and stores no result, and it keeps the same selected Asset. The editor is rendered again from current canonical data. Input entered before the confirmation is not preserved; that remains out of scope. A confirmed change still validates normally, for example voiding still requires a non-empty reason.

### Known pre-existing issue

`async_migrate_entity_registry` relinks an Asset's Lifecycle entity to each device listed in a Purchase subentry's `device_ids`. If such a device has since been removed from the Device Registry, `EntityRegistry.async_update_entity(device_id=...)` raises `ValueError` and setup ends in `SETUP_ERROR` on every later setup, once that entity exists in the Entity Registry. The defect exists unchanged in 0.7.3 and is not fixed in 0.7.4, because 0.7.4 does not change migration behavior. Under 0.7.4, an Asset mutation in an affected installation reports `entry_not_loaded`, because its awaited reload cannot return the entry to `LOADED`. The reload continuity contract surfaces the failure instead of hiding it. Fixed in 0.7.5; see [Stale external device references at setup](#stale-external-device-references-at-setup).

## 0.7.3 compatibility impact

Device Lifecycle 0.7.3 changes no canonical schema, identity, or invariant. Store remains 3.1, ConfigEntry remains version 4, and Asset UUIDs, `DLxxxx` Asset IDs, Asset Device identity, entity unique IDs, and Recorder continuity are unchanged.

The minimum supported Home Assistant version remains 2026.8.0. Home Assistant 2026.9 deprecates using `DeviceRegistry.devices` as a mapping (removal announced for 2027.9), and the two versions iterate it differently:

```text
Home Assistant 2026.8   devices is a device-ID -> DeviceEntry mapping; iteration yields device IDs
Home Assistant 2026.9+  iteration yields DeviceEntry values; mapping use is deprecated
```

Asset Device lookup iterates `registry.devices` and handles each item by type. A `DeviceEntry` is used as is. A device ID, which occurs only on 2026.8, is resolved by exact ID in the same mapping being iterated. Any other item fails closed. The lookup compares no Home Assistant versions and uses no deprecated mapping access on 2026.9+. It keeps the complete-scan, fail-closed rule in [Asset Device projection](#asset-device-projection).

## 0.7.2 behavior contracts

Device Lifecycle 0.7.2 added no schema and no user feature. It tightened three existing contracts.

### Canonical no-op

A request that leaves canonical Store data unchanged is a canonical no-op: the manager performs no Store write and publishes nothing, and the OptionsFlow performs no ConfigEntry reload. In 0.7.2 and 0.7.3 the flow then finished; in 0.7.4 it returned to the Asset hub, and since 0.7.5 it returns to the form's parent view (see [0.7.5 Asset management flow and setup resilience](#075-asset-management-flow-and-setup-resilience)). This covers resubmitting unchanged Asset metadata, the current Purchase, the current Deployment state and Area, the current Lifecycle status, and the current primary Home Assistant device, as well as adding an already-related device. Selecting an Asset's current Lifecycle status is a neutral confirmation, not an error. Metadata fields the user did not change keep their existing provenance instead of becoming user-owned. Quick Add is the deliberate exception: a confirmed Quick Add always schedules one reload, so exposure completes even after an ambiguous Store write.

### Persisted effective dates and clock rollback

Submitting a new Lifecycle or Replacement `effective_date` later than the current Home Assistant local date is rejected (`lifecycle_date_in_future`, `replacement_date_in_future`). Complete Store validation checks already-persisted effective dates only for their structure: type, canonical `YYYY-MM-DD` form, and calendar validity. It does not compare them with the current clock, so accepted history stays loadable when Home Assistant's system date later moves behind it. Chain, graph, and date-ordering invariants remain enforced.

### OptionsFlow without a loaded entry

Every manager-dependent Asset management step needs the parent entry's loaded `AssetStoreManager` in `runtime_data`. The OptionsFlow entry menu checks this once. If the entry is not loaded, failed setup, or is mid-reload, the flow aborts with reason `entry_not_loaded` instead of failing inside a later step.

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
