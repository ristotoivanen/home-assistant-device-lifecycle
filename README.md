# Device Lifecycle

Home Assistant custom integration for tracking physical Assets, Purchases, deployment information, warranty metadata, optional Home Assistant device relationships, and runtime hours.

The integration UI is available in English and Finnish. The product name is **Device Lifecycle** in both languages, and the Finnish name of the human-readable Asset ID is **Elinkaaritunnus**.

## Asset Core

An **Asset** is the canonical record of one real-world physical item. A Purchase records how an Asset was acquired, while a Home Assistant device is an optional relationship to that Asset. Neither one defines Asset identity.

Every Asset has two permanent identifiers:

- an immutable internal UUID used for technical relationships and stable entity unique IDs
- a human-readable Asset ID such as `DL0014`

Asset IDs are allocated monotonically and never recycled. The UUID and Asset ID remain unchanged when the Purchase, deployment information, or linked Home Assistant device changes.

Asset Core uses Home Assistant's private, atomic, versioned storage. Its invariants and Store 3.1 schema are documented in [`ARCHITECTURE.md`](ARCHITECTURE.md).

## What's new in 0.7.1

Device Lifecycle 0.7.1 adds **Add device**, a guided Quick Add workflow for creating one physical Asset from an eligible Home Assistant device or by manual entry. Identity, metadata and provenance, an optional existing Purchase, warranty, Lifecycle, Deployment, Area, and an optional replacement are reviewed before anything is stored. Confirmation performs one atomic, verified Store transaction; the permanent `DLxxxx` ID is allocated only inside that transaction.

Home Assistant-device metadata is suggested and remains editable or clearable. Unchanged suggestions retain Home Assistant provenance; edited or explicitly cleared values become user-owned. A new Asset with no selected Purchase stores no false “user chose no Purchase” provenance. Quick Add can select an existing configured Purchase but cannot create one.

Warranty can be not specified, manual, or calculated as one or two calendar years from the selected Purchase date. Calculated warranties revalidate the Purchase and reviewed date at commit. Lifecycle starts with exactly one optional `unknown` → selected-state event, except that an initial `unknown` state creates no event. Lifecycle effective date, Installation date, and Asset Area are always explicit; no date or Area is inferred.

Quick Add can atomically record that the new physical Asset replaces a predecessor. The user may explicitly retire or undeploy that predecessor; an actual undeploy clears its Asset Area while preserving Installation date. Disposed or lost predecessors are never automatically changed to retired. Replacement never transfers Runtime, Purchase, warranty, external Home Assistant relationships, or other canonical domains, and it never creates a Purchase. The diagnostic Replacement entity retains its unique ID but is now enabled by default when an active replacement exists; user- or config-entry-disabled registry entries are never overridden.

First-time setup continues into Quick Add after creating and loading the single parent ConfigEntry. Existing installations using the exact old Finnish default title are normalized to **Device Lifecycle**; custom titles are preserved. Store remains **3.1** and ConfigEntry remains version **4**, with no migration in this release.

## What's new in 0.7.0

Device Lifecycle 0.7.0, **Lifecycle & Replacement**, extends the canonical Asset Core with two independent physical-Asset domains:

- current Lifecycle status plus immutable lifecycle transition history
- physical predecessor/successor replacement relationships plus permanent void/correction history

Every Asset now has an enabled **Lifecycle Status** enum sensor (`<asset_uuid>_lifecycle_status`) and a disabled-by-default diagnostic **Replacement** enum sensor (`<asset_uuid>_replacement`). Both belong to the parent ConfigEntry and the same deterministic Asset Device as the existing entities. The existing warranty-oriented **Lifecycle** sensor (`<asset_uuid>_lifecycle`) is unchanged.

Store migrates explicitly from 1.1, 1.2, or 2.1 to **3.1**. Existing Assets start with Lifecycle `unknown`, no current event, and no synthetic history. Assets created after the upgrade normally start `active` with one initial `unknown` → `active` event. ConfigEntry remains version 4.

## What's new in 0.6.1

Device Lifecycle 0.6.1 exposes the canonical Asset `installed_date`, already stored and reconciled in 0.6.0, as an enabled native Home Assistant **Installation Date** sensor. Its unique ID is `<asset_uuid>_installed_date`, and it belongs to the parent ConfigEntry and the deterministic Asset Device.

The sensor reports the canonical date without inferring it from Purchase date, Deployment, Area, external devices, Runtime, or entity history. Deployment status remains independent: an Asset may have an Installation Date while its Deployment state is still `unknown` or `not_deployed`. Store remains 2.1, ConfigEntry remains version 4, and 0.6.1 adds no persistence or config-entry migration.

## What's new in 0.6.0

Device Lifecycle 0.6.0, **Asset Exposure Core**, gives every existing canonical Asset a clean Home Assistant representation:

- one Device Lifecycle-owned **Asset Device** per Asset
- one **Lifecycle** entity for every Asset, even without a Purchase or external device
- a new normal **Deployment** enum entity
- a new enabled-by-default **Relationships** diagnostic entity
- a new enabled-by-default **Asset ID** diagnostic entity
- existing Runtime entities appear under the Asset Device while their configuration, unique ID, entity ID, calculations, and canonical totals remain unchanged
- existing Lifecycle entities keep their unique ID, entity ID, user customization, and Recorder continuity while moving from Purchase-subentry ownership to parent/Asset ownership

There is no new canonical data model. `asset_uuid` remains the only canonical technical Asset identity and `DLxxxx` remains its permanent human-facing identity. Store stays at 2.1 and the ConfigEntry stays at version 4.

## Asset Device

The Asset Device is a derived Home Assistant Device Registry projection. Its complete identity is based only on the immutable Asset UUID:

```text
identifiers = {("device_lifecycle", asset_uuid)}
```

It belongs to the parent Device Lifecycle ConfigEntry, never a Purchase or Runtime subentry. It uses the Asset's canonical name and supported physical metadata. It does not use Asset ID, Purchase, name, serial number, Area, an external device ID, identifiers, or connections as identity.

The Asset Device's Home Assistant registry ID is not stored. If the projection is removed, a later setup can recreate it deterministically without changing or recreating the Asset, allocating a new `DLxxxx` value, or touching Purchase and Runtime history. Home Assistant device-name overrides remain registry customizations and are not written back to Asset Store.

The Asset deployment Area is intentionally not synchronized to the Asset Device's Device Registry Area. The two concepts remain independent in 0.6.0.

## Exposure entities

Every valid Asset now has these seven parent-owned entities on its Asset Device:

- **Lifecycle** (`<asset_uuid>_lifecycle`) preserves the existing warranty/state behavior and compatibility attributes. No Purchase or warranty information is required; the entity uses the existing not-specified state in that case.
- **Deployment** (`<asset_uuid>_deployment`) reports `unknown`, `not_deployed`, or `deployed`. Its attributes continue to expose Installation date and the exact stored Asset Area as `not_set`, `present`, or `missing`. A stale Area ID is preserved and never repaired by name.
- **Installation Date** (`<asset_uuid>_installed_date`) exposes canonical `asset["installed_date"]` as a native Home Assistant date. It has no independent state or persistence.
- **Relationships** (`<asset_uuid>_relationships`) reports `none`, `present`, or `missing` from exact stored external Device Registry IDs. It shows primary and related states and current display names without persisting those names or treating registry presence as operational availability.
- **Asset ID** (`<asset_uuid>_asset_id`) reports the permanent `DLxxxx` value. In Finnish its name is **Elinkaaritunnus**.
- **Lifecycle Status** (`<asset_uuid>_lifecycle_status`) reports `unknown`, `active`, `retired`, `disposed`, or `lost` from canonical Asset lifecycle state. It is enabled by default and exposes only the current transition's optional effective date.
- **Replacement** (`<asset_uuid>_replacement`) reports `none`, `replaces`, `replaced_by`, or `chain_member`. It is diagnostic, disabled by default when no active relationship exists, and enabled by default when active replacement context exists. Its attributes contain only current predecessor/successor Asset IDs as lists.

Relationships remain read-only references. A missing external device stays linked by its stored ID and is never automatically remapped. Related devices never refresh Asset metadata, alter Purchase or Deployment, or become Runtime targets or fallbacks.

## Purchase-first workflow

A Purchase is an acquisition event, so it can be recorded before the physical Assets arrive or before any device exists in Home Assistant.

```text
Purchase
  -> Asset created or assigned later
  -> Deployment information
  -> optional Home Assistant device relationship
```

A Purchase with zero Assets is valid and remains editable. Creating it does not allocate an Asset UUID or Asset ID. Asset identity is allocated only when an Asset is actually created.

Purchase metadata includes:

- name
- Purchase date
- seller
- total price and currency
- receipt or order reference
- receipt or invoice URL
- notes
- optional warranty information used by existing Purchase workflows

The Purchase price is the total transaction price, not a per-device price. The configured Home Assistant currency is captured when the Purchase is created and retained when it is edited later.

## Manual Assets

A physical item can be inventoried even when it has no Home Assistant device. Examples include:

- a spare smart bulb stored on a shelf
- network equipment not represented in Home Assistant
- a device waiting for installation
- physical equipment that may never appear in Home Assistant

Choosing **Add device > Manually** opens Quick Add with **Active** Lifecycle and **Not deployed** Deployment defaults, with no Purchase, Installation date, Home Assistant Area, or Home Assistant device relationship. These are visible, editable values. Confirmation assigns the immutable internal identity and next permanent Asset ID atomically.

The user-facing Asset ID remains unchanged when:

- the Purchase is assigned, changed, or cleared
- Deployment status, Installation date, or Area changes
- a Home Assistant device is linked
- the linked Home Assistant device is unlinked or replaced

## Asset management

Open the existing Device Lifecycle integration and choose **Configure** to access Asset management.

Available actions are:

- **Add device**: create one physical Asset from an eligible Home Assistant device or by manual entry, with a mandatory final review
- **Manage Asset**: select any existing Asset by its permanent Asset ID and display name
- **Edit metadata**: change physical details such as name, category, manufacturer, model, serial number, and notes
- **Change Purchase**: assign a configured Purchase or clear the current Purchase
- **Deployment status**: edit status, Installation date, and Home Assistant Area
- **Lifecycle status**: record an explicit current-state transition with optional effective date and notes
- **Asset replacement**: create, correct, or void a physical predecessor/successor relationship
- **Home Assistant relationships**: inspect the primary and all related devices
- **Manage primary device**: link, unlink, replace, or promote a related device
- **Add related device**: add one non-exclusive relationship
- **Remove related device**: remove one stored relationship, including a missing device ID

Assets originally created through Purchase or Runtime reconciliation are managed through the same UI. Device Lifecycle does not introduce a separate manual-device model.

## Deployment

Deployment information belongs to the Asset and is independent of whether a Home Assistant device is linked.

Supported Deployment statuses are:

- **Unknown**: the current status is not known; this is the migration value for existing 0.5.3 Assets
- **Not deployed**: the Asset exists but is not currently deployed or in use
- **Deployed**: the Asset is currently deployed or in use

The **Installation date** is an explicit lifecycle value. It can be set, changed, or cleared, and a Deployment status change does not infer or automatically replace it.

The optional **Home Assistant Area** represents the Asset's current deployment location. It is Asset metadata: Device Lifecycle does not move an external Home Assistant device, copy the external device's Area, or assign it to the Device Lifecycle Asset Device.

Changing an Asset with an Area to **Not deployed** opens a separate confirmation step. The Area is cleared only after confirmation, while the Installation date is preserved unless the user explicitly changed or cleared it.

If a stored Area has been deleted, setup and Asset management continue normally. The unavailable Area ID is displayed and preserved until the user explicitly clears it or selects an existing Area. Device Lifecycle never guesses a replacement Area by name.

0.7.0 does not store deployment history.

## Lifecycle status

Lifecycle status describes whether the physical Asset belongs to actively managed inventory. The canonical values are:

- **Unknown**: the current lifecycle state is not known
- **Active**: actively managed inventory, whether deployed or not
- **Retired**: no longer in normal use, but the physical Asset and history remain
- **Disposed**: permanently left managed inventory
- **Lost**: physical possession or control is lost

Lifecycle and Deployment are independent. For example, an active Asset may be not deployed, and a retired Asset may retain its Installation Date and Area history. Changing Lifecycle never changes Deployment, Installation Date, Area, Purchase, warranty, Home Assistant relationships, Runtime, or replacement relationships.

Each actual status change appends an immutable event containing the old and new status, optional effective date and notes, an integration-recorded UTC timestamp, and an explicit pointer to the previous event. Same-state changes are no-ops with no event, Store write, or reload. Statuses remain explicitly reversible: corrections such as `lost` → `active` or `disposed` → `active` create a new transition rather than editing history. Selecting `disposed` requires a separate confirmation.

## Physical Asset replacement

A replacement means one physical Asset replaces another physical Asset. An active relationship is directed from predecessor to successor, for example `DL0001 → DL0008`. A valid chain may continue through later replacements. In 0.7.0 each Asset can have at most one active predecessor and one active successor, and the complete active graph must be acyclic.

Replacement reasons are Unknown, Planned refresh, Upgrade, Failure, Warranty RMA, and Other. **Warranty RMA is only a reason label in 0.7.0**; it does not create an RMA case or transfer the predecessor's Purchase or warranty. A successor never automatically inherits Purchase, warranty, Deployment, Installation Date, Area, Home Assistant relationships, Runtime, maintenance data, or documents.

Incorrect records are never deleted. Voiding requires confirmation and a reason, and the record remains in history. Correction atomically voids the old record and creates a new active record in one verified Store save. If validation or persistence fails, the old relationship remains active.

## Home Assistant device relationships

A Home Assistant device reference is a relationship, not Asset identity. Each Asset can have zero or one primary external Home Assistant device and zero or more related devices.

The relationship rules are:

- a Home Assistant device can be primary for at most one Asset
- a related device can be linked to multiple Assets
- a device can be primary for one Asset and related to other Assets
- the same device cannot be both primary and related on one Asset
- the same device cannot appear twice on one Asset
- stale primary and related IDs remain stored until explicit repair

Linking or replacing the primary device:

- does not change the Asset UUID or Asset ID
- does not attach the Device Lifecycle config entry to the external device
- does not change the external device's identifiers or connections
- does not rename or move the external device
- does not change the external device's Area or config-entry ownership
- may refresh non-user-owned Asset metadata from available device information
- never overwrites metadata explicitly owned or cleared by the user

Related relationships are reference-only. Adding or removing one:

- does not refresh or aggregate Asset metadata
- does not change the Asset UUID or Asset ID
- does not affect Purchase membership or reconciliation
- does not affect Runtime configuration, reconciliation, or totals
- does not affect Deployment information
- does not place or duplicate entities on the related device
- does not modify or take ownership of the Home Assistant device

When a related device is promoted to primary, its related reference is removed in the same atomic change. The old primary is removed rather than automatically becoming related. Existing dependency checks and primary-conflict validation still apply.

Device Lifecycle does not automatically match devices by name, model, serial number, manufacturer, network address, or Area. It never automatically merges Assets.

A missing stored device is shown as unavailable with its stored ID and is not silently replaced. A stale primary can be unlinked or replaced when dependency checks allow it. A stale related reference remains available in the Remove related device list.

Unlinking or replacing the primary device is blocked while an active:

- Purchase configuration still includes that device, or
- Runtime configuration still tracks that device

Remove the active dependency first. Device Lifecycle does not rewrite dependent configurations automatically in 0.6.0. Related add/remove operations do not rewrite or depend on Purchase and Runtime subentries.

Device Lifecycle validates new relationship targets using the Home Assistant 2026.8 single-config-entry Device Registry model. It never attaches its config entry to an external device or changes external identifiers, connections, names, Area, config-entry ownership, or device topology. Its separately owned Asset Device is only a projection of canonical Asset data.

## Purchase relationships

An Asset may have no Purchase. A manual or existing eligible Asset can later be assigned to any currently configured Purchase, and the relationship can be cleared without recreating the Asset.

Existing relationships to historical or no-longer-configured Purchases are preserved and displayed safely. Historical Purchases are not offered as targets for new relationships.

## Storage and migration impact

0.7.1 continues to use Store 3.1 and ConfigEntry version 4, with no schema migration. Store 3.1 contains `asset.lifecycle`, top-level `lifecycle_events`, and top-level `replacement_records`. It does not persist Asset Device IDs, Entity Registry IDs, exposure state, workflow drafts, or alternate identities.

## Warranty

Existing Purchase workflows support these warranty modes:

- Not specified
- 1 year
- 2 years
- Manual

For 1- and 2-year warranties, the warranty end date is calculated from the Purchase date with calendar-year and leap-day handling. Quick Add can apply those modes only when a configured Purchase with a valid Purchase date is selected, or use a manual warranty date without a Purchase. It revalidates the Purchase date immediately before commit. Existing management behavior remains unchanged; 0.7.1 does not add a general Asset-level warranty editor.

## Runtime tracking

Runtime tracking is optional and configured separately for each physical Asset represented by a Home Assistant device.

Available tracking methods are:

- **Entity state is on**: counts time while a supported source entity is `on`
- **Power above threshold**: counts time while a power sensor exceeds the configured watt threshold

Runtime setup first selects the target device and tracking method, then a suitable source entity. Power threshold and hysteresis are shown only in power mode. Supported power units such as `W` and `kW` are normalized to watts; energy, current, voltage, and frequency sensors are not valid power sources.

The cumulative **Runtime hours** sensor is localized as **Käyttötunnit** in Finnish. Asset Store owns its cumulative seconds. The sensor projects canonical committed time plus any sealed pending delta and current monotonic active interval. Time while Home Assistant is stopped is not observed or added.

While active, Runtime checkpoints to Asset Store every five minutes. It also checkpoints when the source stops or becomes unavailable/unknown and during normal unload or shutdown. Under healthy Store operation, a hard crash therefore loses only time since the last successful checkpoint, normally less than five minutes. No crash-loss bound is claimed while persistence is failing. Runtime reconciliation and entity setup remain primary-only; related devices are never Runtime targets or fallbacks.

Runtime configuration remains owned by its Runtime subentry, and the external primary relationship remains its configured target. In 0.6.0 only the entity's Device Registry placement changes to the owned Asset Device. Runtime unique ID, entity ID, subentry ID, total, source behavior, initialization, restore import, thresholds, hysteresis, units, precision, state class, checkpointing, and CAS behavior are unchanged.

## Upgrade notes

### Upgrading from 0.7.0 to 0.7.1

No Store or ConfigEntry migration runs. Store remains 3.1 and ConfigEntry remains version 4. Existing Assets, history, entity unique IDs, entity IDs, and Recorder continuity remain unchanged. The normal creation UI becomes Quick Add, and Replacement entity visibility is reconciled from active canonical relationships without overriding entries disabled by the user or ConfigEntry.

### Upgrading from 0.6.1 to 0.7.0

Create a Home Assistant backup before upgrading. Store 1.1, 1.2, and 2.1 are migrated explicitly to Store 3.1. Every existing Asset receives Lifecycle `unknown` with `current_event_uuid: null`; lifecycle and replacement history dictionaries start empty, with no inferred or synthetic events. All existing Asset/Purchase identities, membership/order, ConfigSubentry references, Deployment, Installation Date, Area, warranty, Runtime, metadata, provenance, and Home Assistant device references are preserved.

Downgrading Store 3.1 to Device Lifecycle 0.6.1 is unsupported. To roll back, restore the complete Home Assistant backup made before upgrading; do not copy a 3.1 Store file into a 0.6.1 installation.

### Upgrading from 0.6.0 to 0.6.1

No Store or ConfigEntry migration runs. Setup preflights the new immutable Asset UUID-derived Installation Date identity before platform setup, then Home Assistant creates or reuses the parent-owned entity on the existing deterministic Asset Device.

### Upgrading from 0.5.7 to 0.6.0

No Store or ConfigEntry schema migration runs. Setup first completes the existing legacy UUID-based entity migration, then validates the entire exposure registry plan before creating an Asset Device or moving an entity. Ambiguous Device or Entity Registry identity fails setup closed.

Existing Lifecycle and Runtime entities keep their exact entity IDs and unique IDs. Lifecycle becomes parent-owned and Runtime keeps its Runtime subentry. Both move to the deterministic Asset Device. If a registry step fails, completed entity moves are rolled back in reverse order and only unreferenced Asset Devices proven new in that setup attempt are removed. A partial derived projection left by a rollback failure or crash is reconciled on the next reload from unchanged canonical Asset Store data.

### Upgrading from 0.5.6 to 0.5.7

Store 1.2 migrates to Store 2.1 with every Asset Runtime total initially uninitialized, never zero. An existing Runtime subentry is recognized by the absence of `runtime_data_version`; its exact native restored hours and hour unit are validated and atomically imported before counting starts. A new 0.5.7 Runtime carries `runtime_data_version: 1`, which permits safe zero initialization. Invalid or missing legacy restore data leaves migration pending and retryable instead of resetting history.

The Store major-version change is intentional. After Store 2.1 migration, downgrading to Device Lifecycle 0.5.6 is unsupported. Make a Home Assistant backup before upgrading and restore that backup if rollback is required.

### Upgrading from 0.5.3 to 0.5.4

Asset Store is migrated explicitly from schema 1.1 to 1.2. The migration preserves:

- every Asset UUID
- every permanent `DLxxxx` Asset ID
- `next_asset_number`
- Purchase UUIDs, ordering, and Asset membership
- Home Assistant device relationships
- Purchase and Runtime config-subentry references
- Runtime totals and existing entity unique IDs

Existing Assets receive:

```text
deployment_state: unknown
ha_area_id: null
```

If an existing Asset has a Purchase relationship without relationship provenance, it is marked as Purchase-controlled during migration. No Deployment status is inferred from Home Assistant device presence, Purchase membership, Installation date, Home Assistant Area, or Runtime configuration.

Downgrading a Store 1.2 installation to Device Lifecycle 0.5.3 should not be assumed safe. Create a Home Assistant backup before upgrading and restore that backup if rollback is required.

### Upgrading from 0.4.x

The existing 0.5.0 migration normalizes Purchase and Runtime subentries into Asset Core. Home Assistant entity IDs are preserved while Device Lifecycle entity unique IDs move to immutable Asset UUID-based IDs, keeping Runtime restore history available.

Downgrading from Asset Core to 0.4.x is not supported. Restore a backup instead.

## Other features

- Permanent Asset IDs (`DL0001` ... `DL9999`) that are never recycled
- Versioned private Asset Core storage with explicit migrations
- One Home Assistant integration with multiple Purchase and Runtime subentries
- Receipt/order references and optional receipt or invoice URLs
- Metadata refresh from Home Assistant with user-override protection
- Conservative filtering of obvious system/software devices
- Rejection of service-type Home Assistant devices as physical targets
- Runtime source validation and configurable power hysteresis
- Stable Asset-based Lifecycle and Runtime entity unique IDs
- Deterministic Asset Devices and parent-owned Deployment, Relationships, and Asset ID entities

Example Lifecycle sensor attributes:

```text
asset_id: DL0001
asset_uuid: 9b5a...
```

Example Runtime sensor:

```text
Runtime hours: 1284.53 h
```

## Requirements

- Home Assistant 2026.8.0 or newer

The integration uses Home Assistant's device-linking model while keeping Device Lifecycle entities and external device ownership separate.

## Installation with HACS

Until this repository is included as a HACS default repository, add it as a custom repository:

1. Open HACS.
2. Open the menu and choose **Custom repositories**.
3. Add `https://github.com/ristotoivanen/home-assistant-device-lifecycle`.
4. Select category **Integration**.
5. Install **Device Lifecycle**.
6. Restart Home Assistant.
7. Go to **Settings > Devices & services > Add integration** and search for **Device Lifecycle**.

## Manual installation

Copy:

```text
custom_components/device_lifecycle/
```

to:

```text
/config/custom_components/device_lifecycle/
```

Restart Home Assistant.

## Editing and deletion behavior

Existing Purchases remain editable, including Purchases with zero Assets. Removing a device from a Purchase removes the active Purchase projection while preserving the Asset identity and permanent Asset ID.

Removing a Runtime tracking entry removes only that Runtime sensor and active configuration. The canonical Asset Runtime total remains available if tracking is recreated later. Other Purchases, Assets, Runtime configurations, and integrations are left untouched.

Removing a Device Lifecycle Asset Device from Home Assistant does not delete or purge its canonical Asset. The projection can be recreated on reload.

Device Lifecycle 0.7.1 does not provide Asset deletion/purge/merge, Runtime reset/manual editing, bulk Asset creation, automatic discovery or stale-device rematching, Purchase creation inside Quick Add, Maintenance, RMA cases, Documents, export/import, future replacement scheduling, automatic inheritance/transfer between replacement Assets, a lifecycle-history UI, or full replacement-history attributes. Lifecycle and replacement history remain canonical in Store 3.1 even though Home Assistant exposes only current state.

## Roadmap

- **0.5.4 — Manual Assets & Deployment**
- **0.5.5 — Purchase Asset membership follow-up**
- **0.5.6 — HA Relationships**
- **0.5.7 — Asset Runtime**
- **0.6.x — Asset Exposure / UI**
- **0.7.0 — Lifecycle & Replacement**
- **0.7.1 — Quick Asset Entry & UX**
- **Possible 0.7.2 — Lifecycle UX/history improvements, if justified**
- **0.8.x — Maintenance**
- **0.9.x — Portability & Hardening**
- **Future — Documents**
- **1.0 — Stable**

## License

MIT
