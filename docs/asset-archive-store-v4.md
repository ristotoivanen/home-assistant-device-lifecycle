# Device Lifecycle 0.8.x Asset Archive and Store 4.1 — frozen architecture

| | |
|---|---|
| Status | **FROZEN** |
| Architecture freeze approved | 2026-09-26 |
| Store target | `STORAGE_VERSION = 4`, `STORAGE_MINOR_VERSION = 1` (Store 4.1) |
| Implementation status | not implemented; the production constants are still Store 3.1 |
| Maintenance schema | [Maintenance Store 4.x frozen schema](maintenance-store-v4-schema.md), persisted shapes unchanged by this document |

This document is the canonical architecture for 0.8.x Asset Archive and for the complete Store 4.1 target that Maintenance and Archive share. It supersedes the `OPEN DESIGN` items for Archive that [ARCHITECTURE.md](../ARCHITECTURE.md#planned-asset-archive-and-permanent-deletion) previously listed, and it assigns the Store version that the Maintenance schema left open. The Maintenance record shapes, invariants, mutation rules, and projection rules remain defined only in the Maintenance schema; this document adds the Archive coordination rules that apply to them.

A change to anything in this document is an architecture revision and needs an explicit review. The freeze authorizes implementation against this contract. It does not claim that any part of it exists; see [Not yet implemented](#not-yet-implemented).

## Concepts

- **Archive** removes an Asset from active/current management while keeping its identity, its history, and its relationships. It is reversible through **Restore**.
- An archived Asset is not an immutable snapshot. Direct user and current-management mutations are blocked; correction or void of already-persisted historical facts and source-authoritative reconciliation continue.
- Archive is not a Lifecycle status, not an end-of-ownership inference, and not permanent deletion. Permanent deletion stays in 0.9.x.
- Archive state is the current state of one Asset field. Store 4.1 keeps no Archive history.

## Store 4.1

### Top-level shape

Store 4.1 has exactly these seven top-level keys. There are no optional extension keys, and Archive adds no top-level collection.

```text
next_asset_number
purchases
assets
lifecycle_events
replacement_records
maintenance_schedules
maintenance_events
```

### Asset record: exactly 21 keys

The 20 existing Store 3.1 Asset keys plus `archived_at`:

```text
asset_uuid
asset_id
name
category
purchase_uuid
deployment_state
installed_date
ha_area_id
warranty
runtime
lifecycle
manufacturer
model
model_id
serial_number
sw_version
hw_version
notes
field_sources
ha_device_refs
archived_at
```

Every key is always present. The nested shapes of `warranty`, `runtime`, `lifecycle`, `field_sources`, and `ha_device_refs` are unchanged.

### Purchase record: exactly 12 keys

The exact current Store 3.1 Purchase key set, derived from the only creation path (`_new_purchase`), the reconciliation update, the migrations (which carry Purchases through unchanged), and the valid test fixtures:

```text
purchase_uuid
config_subentry_id
configured
name
purchase_date
seller
total_price
currency
receipt_reference
receipt_url
notes
asset_uuids
```

The Purchase shape is unchanged in Store 4.1; Store 4.1 only makes it exact. No optional future Purchase field is reserved.

### `archived_at`

```text
"archived_at": null | <canonical UTC timestamp>
```

| Value | Meaning |
|---|---|
| `null` | The Asset is in active/current management. |
| canonical UTC timestamp | The Asset is archived. |

- Non-nullness alone defines the archived state.
- The timestamp uses the same canonical UTC representation as Maintenance `recorded_at` and `voided_at` (see [canonical representations](maintenance-store-v4-schema.md#canonical-representations)).
- It is the observed operation time, never user input, and never an ordering key.
- Load never compares it with the current clock. A future-looking value is structurally valid.
- Restore sets it to `null`. It records current state, not history: clearing it intentionally discards the timestamp of the previous Archive operation.
- There is no Archive reason, no Archive event collection, no tombstone, no archive sequence, no Runtime epoch, and no transaction metadata.

## Load invariants

Store 4.1 is validated by a separate, exact Store 4.1 validator. The Store 3.1 validator is not changed to accept Store 4.1, and Store 4.1 is never validated by a "validate the new keys if they exist" branch.

The Store 4.1 validator checks:

1. The exact seven top-level keys.
2. The exact 21-key Asset shape.
3. The exact 12-key Purchase shape.
4. Every existing Asset invariant, unchanged.
5. `archived_at` is `null` or a canonical UTC timestamp.
6. `archived_at != null` → `deployment_state != "deployed"`.
7. The existing Lifecycle graph validation.
8. The existing Replacement validation.
9. The Maintenance collection validation of the Maintenance schema.
10. Every existing cross-domain reference.

Invariant 6 is both a mutation invariant and a load invariant. A persisted Store 4.1 that says an Asset is archived and deployed at the same time carries no evidence of which fact is wrong, so load fails closed. Load never infers a Restore, never infers an undeploy, and never repairs the record automatically. Migration cannot produce this state, because every migrated Asset receives `archived_at = null` before the final validation.

**Not a load invariant:** the absence of a Runtime ConfigSubentry for an archived Asset. ConfigSubentry data lives outside the Store, so this is an Archive operation invariant and a setup lifecycle invariant instead (see [Runtime](#runtime) and [Setup conflict: quarantine](#setup-conflict-quarantine)).

## Identity lookup and management filtering

Named invariant:

```text
IDENTITY / RECONCILIATION LOOKUP  -> ALL Assets, active and archived
CURRENT-MANAGEMENT CANDIDATES     -> only Assets with archived_at == null
```

- Global primary Home Assistant Device uniqueness includes archived Assets. Reconciliation always finds an archived Asset that owns a primary Home Assistant Device, the device never becomes "free", and no duplicate Asset is created for it.
- Selectors are user experience only. Every final mutation submit that requires active/current management re-checks `archived_at == null` inside the authoritative mutation. This includes Runtime creation that starts from a Home Assistant Device selector rather than an Asset selector.

## Archive

### Preconditions and order

```text
1. Asset exists
2. already archived                                   -> NO_OP
3. deployment_state != "deployed"
4. no Runtime ConfigSubentry resolves to this Asset
5. no surviving Runtime writer for this Asset holds undurable Runtime state
-> mutate
```

Runtime ConfigSubentry resolution in step 4 uses exactly the identity semantics of Runtime reconciliation, not only an `asset_uuid` match. Today that is: a subentry resolves to the Asset its `asset_uuid` names when that Asset's primary device is unset or equals the subentry's `device_id`; otherwise it resolves to the Asset whose primary Home Assistant Device is the subentry's `device_id`. Legacy and recovery subentries that identify the Asset only through the Home Assistant Device therefore block Archive too.

### Effect

Archive changes only `asset.archived_at`. It does not change:

- Lifecycle, and it appends no Lifecycle Event
- Deployment fields, the installation date, or the Home Assistant Area
- Home Assistant device references
- the canonical Runtime total
- Purchase linkage, membership, or provenance
- Replacement records
- Maintenance Schedules or Events

No history is fabricated, and no identity changes.

## Restore

Restore changes only `archived_at` to `null`. It:

- keeps the same `asset_uuid` and the same `DLxxxx`
- does not recreate a Runtime ConfigSubentry
- does not fabricate a missing Home Assistant Device and does not repair stale Home Assistant references
- does not change Lifecycle, Deployment, or Runtime
- does not change any Maintenance `enabled` value

After Restore:

- stale Home Assistant references become actionable again
- the Maintenance projection resumes, and a Schedule may immediately project `OVERDUE`
- Runtime can be configured again. It continues from the canonical Asset Runtime total; no elapsed time is inferred for the period without tracking.

Restore is the only direct mutation that returns an Asset to active management.

## NO_OP and persistence recovery

Archive and Restore are state-setting operations without a generated business identity.

```text
Asset exists
-> already in the requested final state: NO_OP (no save, no refresh)
-> otherwise: preconditions
-> mutate
```

An ambiguous persistence result is resolved inside the same serialized Store mutation by reading the persisted snapshot:

| Request | Persisted state | Result |
|---|---|---|
| Archive | `archived_at != null` | success |
| Restore | `archived_at == null` | success |

The generated `archived_at` value is not compared.

This is not general delayed-request idempotency. A request that arrives after an intervening opposite operation is a new state-setting command: if Archive succeeds, Restore succeeds later, and the old Archive command arrives again, it is a new Archive. No request or transaction identifier is added in 0.8.x.

## Mutation boundary while archived

> Archive blocks direct user/current-management mutations.
> Archive does not block correction or void of already-persisted historical facts.

Blocked while the Asset is archived:

- direct Asset metadata edits
- Deployment changes
- Lifecycle transitions
- Runtime ConfigSubentry create or rebind
- a new Replacement
- a new Maintenance Schedule and every current Schedule configuration change
- an ordinary new Maintenance Event
- current-management relationship changes, such as changing the linked Purchase or the primary or related Home Assistant devices

Allowed while archived:

| Domain | Allowed | Blocked |
|---|---|---|
| Maintenance | Void Event; Correct Event (it may atomically create the corrected Event under the frozen correction model). Replay is checked before the archive guard. | Record Event, Create Schedule, Edit Schedule, Set baseline, Add or remove interval, Enable or disable, Hard-delete Schedule |
| Replacement | The existing void operation on a record that involves an archived Asset. No new correction primitive is introduced. | Creating a new Replacement; it requires both Assets to be active. |
| Lifecycle | — | Every transition. A Lifecycle transition changes the current canonical Lifecycle state and is not a history-only correction. |

## Source-authoritative reconciliation

```text
No direct user/current-management mutation while archived.
Source-authoritative reconciliation may continue updating fields owned by that source.
```

- The canonical Purchase-owned projection continues on archived Assets.
- Home Assistant reconciliation may refresh Home Assistant-owned metadata of an archived Asset according to the existing `field_sources` provenance rules. User-owned fields are never taken back.
- An archived Asset is described as "removed from active/current management", never as an immutable snapshot.

## Purchase

Archive preserves Purchase membership, `purchase_uuid`, the Purchase `asset_uuids` list, provenance, and warranty data. An archived Asset remains part of its Purchase's history and current canonical relationship. Purchase reconciliation continues under the existing ownership rules. Archive is not an end-of-ownership inference.

## Replacement

Archive preserves Replacement records exactly. Archived Assets remain valid historical predecessor and successor references. A new Replacement requires active Assets; the existing void operation may act on a record that involves archived Assets. Nothing is voided automatically, and no relationship is transferred.

## Maintenance

The persisted Maintenance shapes are unchanged; no `archived` field is added to Maintenance records. The coordination rules are in [Maintenance Store 4.x frozen schema: Archived Assets](maintenance-store-v4-schema.md#archived-assets):

- A Schedule of an archived Asset has no active due or preparation projection. This is separate from `enabled == false`, and the persisted `enabled` value is unchanged.
- Restore resumes the normal frozen projection from the unchanged Schedule and Event history.
- The mutation boundary is the table above, with replay checked first.

## Runtime

- Archive never changes `asset.runtime.total_seconds`, never writes zero, never creates an epoch, and never infers elapsed time.
- Archive requires Runtime tracking to be durably absent: no resolving Runtime ConfigSubentry and no surviving writer holding undurable Runtime state (preconditions 4 and 5).
- Restore does not recreate Runtime configuration. When Runtime is added again, the existing canonical total is the starting point, and no time during the absence is inferred.

### Archive and Runtime create/rebind

Cross-system invariant:

```text
A successful operation sequence never ends with
    archived_at != null
AND a Runtime ConfigSubentry that resolves to the Asset.
```

- Runtime create and rebind resolve the target Asset with the canonical identity rules, reject an archived Asset, and revalidate at the final commit or submission.
- Archive revalidates Runtime eligibility at its authoritative mutation boundary.
- The two serialize and revalidate, so both cannot succeed on stale preconditions.
- Runtime ConfigSubentry state is not persisted in the Asset Store to achieve this.

### Setup conflict: quarantine

A backup restore or an inconsistent older configuration can contain an archived Asset and a Runtime ConfigSubentry that resolves to it. Setup quarantines the subentry.

Before Runtime reconciliation, Runtime entity or writer startup, and Runtime initialization, setup resolves every Runtime ConfigSubentry against all canonical Assets. If the target is archived:

- the ConfigSubentry is kept unchanged: it is not deleted and not rewritten
- no other Asset is created or rebound for it
- Runtime is not initialized, and no Runtime writer, listener, or checkpoint loop starts
- one deterministic Repairs issue is created
- the rest of the parent ConfigEntry continues loading

Setup does not fail the whole parent ConfigEntry for this localized conflict and does not create a reload loop. The Repair exits are Restore the Asset, or remove or reconfigure the Runtime ConfigSubentry.

### Runtime entity identity

The Runtime sensor's Entity Registry identity becomes Asset/parent-owned and no longer depends on the removable Runtime ConfigSubentry. The Runtime ConfigSubentry still owns whether Runtime tracking and its writer are configured, and their configuration. Removing the Runtime ConfigSubentry must not destroy the Asset's stable Runtime Entity Registry identity, so that `entity_id` and Recorder continuity survive an Archive that lasts longer than Home Assistant's deleted-entity retention. No Store field is added for this.

This changes the current placement, in which the Runtime entity belongs to its Runtime subentry (see [ARCHITECTURE.md](../ARCHITECTURE.md)). The change is part of the implementation and is not in effect yet.

## Home Assistant representation

### Asset Device and Asset entities

- Asset entities remain registered across Archive and Restore, with stable `unique_id` values and Entity Registry identity.
- Archive does not write `disabled_by`. A user-disabled entity stays user-disabled.
- The Asset Device remains registered, and Device Registry properties are not changed to represent Archive.

### Maintenance entities

- Maintenance entities remain registered: no remove/recreate cycle and no `disabled_by=INTEGRATION`.
- While the Asset is archived, the active projection is unavailable or absent. Archived state is never presented as a false `off`.
- Restore recomputes the projection immediately.

### Repairs

- A stale external Home Assistant reference issue for an archived Asset is suppressed or deleted, and Restore rederives it and recreates it if the reference is still stale. Issue IDs are deterministic. One remove/create cycle per real Archive/Restore cycle is acceptable. The user's Ignore state is never used to represent Archive.
- The Runtime conflict Repair (archived Asset and a resolving Runtime ConfigSubentry) is separate, deterministic, and remains until the conflict is resolved.
- Repair state is never persisted in the Asset Store.

### Refresh without reload

Archive and Restore do not require a parent ConfigEntry reload for correctness:

```text
validate
-> Store mutation
-> durable save and direct readback verification
-> publish the canonical snapshot
-> integration-local dispatcher/coordinator refresh
-> entity snapshots and states update
-> Repairs refresh
```

- The Store commit is authoritative. A failed Home Assistant refresh never rolls back a valid persisted Archive or Restore.
- An entity that holds a copied Asset snapshot receives or re-reads the new canonical snapshot before it calls `async_write_ha_state()`.
- A full parent reload remains appropriate for actual ConfigSubentry lifecycle changes where the architecture already requires it.

## Migration

### Pipeline

```text
supported old Store
-> existing migration to exact Store 3.1
-> existing Store 3.1 whole-Store validation
-> Store 3.1 Asset/Purchase migration-source exact-shape preflight
-> add_asset_archive_state
-> add_maintenance_collections
-> validate the complete Store 4.1
-> durable save
-> direct disk readback
-> exact envelope and payload verification
-> publish
```

Every migrated Asset starts with `archived_at = null`, and Maintenance starts empty. Nothing is inferred.

The existing fail-closed version handling is kept: a wrong minor within the same major is rejected with the project's own error, never with `NotImplementedError`. Downgrading Store 4.1 to a 0.7.x release is unsupported; restore a backup instead.

### Source preflight

Store 3.1 already enforces the exact top-level shape (`set(data) == STORE_TOP_LEVEL_KEYS`) and rejects unknown or missing top-level keys; that validator stays as it is. Store 3.1 has never enforced exact Asset and Purchase record key sets. Before any Store 4.1 transform, migration therefore runs a read-only, fail-closed preflight:

- every Asset has exactly the 20 Store 3.1 Asset keys
- every Purchase has exactly the 12 Purchase keys

An unexpected or missing key is a migration incompatibility: migration fails before saving, and the original Store stays unchanged. Unknown fields are never stripped, whitelisted away, rebuilt, or silently normalized.

### Pure transforms

| Transform | Effect | Fails closed when |
|---|---|---|
| `add_asset_archive_state` | every Asset gains `archived_at = null` | an input Asset already contains `archived_at` |
| `add_maintenance_collections` (exists) | adds `maintenance_schedules = {}` and `maintenance_events = {}` | either key already exists |

Both deep-copy their input, infer nothing, overwrite nothing, and persist nothing. They work on disjoint paths and commute:

```text
A(M(v3.1)) == M(A(v3.1))
```

The final Store 4.1 validation runs only after both transforms; no final-shape validator runs between them.

### Test HA merge gate

Before the activation implementation may merge, the real Test HA Store is inspected read-only, or as a copy, for the exact Store 3.1 top-level keys, the exact Asset keys, and the exact Purchase keys. An unexpected Asset or Purchase key stops activation until its provenance is investigated. Unknown fields are never deleted by hand to make migration pass. This is an activation merge gate, not a reason to weaken the Store 4.1 schema.

## Purge boundary

Archive never deletes the Asset, its UUID, its `DLxxxx`, Purchase membership or history, Lifecycle Events, Replacement records, the canonical Runtime total, Maintenance Schedules or Events, or Home Assistant reference evidence. Permanent deletion remains 0.9.x, and Store 4.1 reserves no tombstone or purge structure.

## Dashboard terminology

The optional dashboard's existing "Archived" group ("Arkistoidut") presents Lifecycle `retired`, `disposed`, and `lost`. 0.8.x Asset Archive is a separate concept. A future user interface must use distinct labels for the two. The dashboard is unchanged by this freeze.

## Not yet implemented

None of the following exists yet:

- Store constants 4.1
- Archive models and mutations
- the `add_asset_archive_state` Store transform
- the Store 4.1 validator and the Store 3.1 migration-source preflight
- migration dispatch to Store 4.1
- Maintenance manager integration
- ambiguous Store 4.1 persistence recovery wiring
- Runtime/Archive cross-store serialization
- Runtime conflict quarantine at setup
- the Runtime Entity Registry ownership change
- the dispatcher/coordinator refresh
- the Archive and Restore user interface
- Maintenance entities and user interface
