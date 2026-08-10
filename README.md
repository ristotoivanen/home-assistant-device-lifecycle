# Device Lifecycle

Home Assistant custom integration for tracking physical Assets, Purchases, deployment information, warranty metadata, optional Home Assistant device relationships, and runtime hours.

The integration UI is available in English and Finnish. In Finnish Home Assistant it is shown as **Laitteen elinkaari**, and the human-readable Asset ID is called **Elinkaaritunnus**.

## Asset Core

An **Asset** is the canonical record of one real-world physical item. A Purchase records how an Asset was acquired, while a Home Assistant device is an optional relationship to that Asset. Neither one defines Asset identity.

Every Asset has two permanent identifiers:

- an immutable internal UUID used for technical relationships and stable entity unique IDs
- a human-readable Asset ID such as `DL0014`

Asset IDs are allocated monotonically and never recycled. The UUID and Asset ID remain unchanged when the Purchase, deployment information, or linked Home Assistant device changes.

Asset Core uses Home Assistant's private, atomic, versioned storage. Its invariants and Store 2.1 schema are documented in [`ARCHITECTURE.md`](ARCHITECTURE.md).

## What's new in 0.5.7

Device Lifecycle 0.5.7, **Asset Runtime**, moves cumulative Runtime ownership into canonical Asset Store data:

- existing 0.5.6 native RestoreSensor totals import exactly once without changing entity IDs or unique IDs
- cumulative seconds live on the immutable Asset as a decimal string
- active Runtime is durably checkpointed every five minutes and on stop/unload/shutdown
- monotonic elapsed measurement avoids wall-clock corrections
- sealed elapsed remains visible and retryable during save failures
- expected-total compare-and-set commits prevent duplicate counting
- Store writes are directly read back before an in-memory mutation is published
- Store schema moves to major version 2.1; config entry version remains 4

Asset identity, Purchase behavior, deployment behavior, Home Assistant relationship semantics, Runtime entity identity, and detection behavior remain unchanged.

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

Creating a manual Asset assigns its immutable internal identity and the next permanent Asset ID. A new manual Asset starts as **Not deployed**, with no Purchase, Installation date, Home Assistant Area, or Home Assistant device relationship.

The user-facing Asset ID remains unchanged when:

- the Purchase is assigned, changed, or cleared
- Deployment status, Installation date, or Area changes
- a Home Assistant device is linked
- the linked Home Assistant device is unlinked or replaced

## Asset management

Open the existing Device Lifecycle integration and choose **Configure** to access Asset management.

Available actions are:

- **Create Asset**: inventory a physical item without requiring a Purchase or Home Assistant device
- **Manage Asset**: select any existing Asset by its permanent Asset ID and display name
- **Edit metadata**: change physical details such as name, category, manufacturer, model, serial number, and notes
- **Change Purchase**: assign a configured Purchase or clear the current Purchase
- **Deployment status**: edit status, Installation date, and Home Assistant Area
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

The optional **Home Assistant Area** represents the Asset's current deployment location. It is Asset metadata: Device Lifecycle does not move an external Home Assistant device or copy the external device's Area automatically.

Changing an Asset with an Area to **Not deployed** opens a separate confirmation step. The Area is cleared only after confirmation, while the Installation date is preserved unless the user explicitly changed or cleared it.

If a stored Area has been deleted, setup and Asset management continue normally. The unavailable Area ID is displayed and preserved until the user explicitly clears it or selects an existing Area. Device Lifecycle never guesses a replacement Area by name.

0.5.7 does not store deployment history.

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

Remove the active dependency first. Device Lifecycle does not rewrite dependent configurations automatically in 0.5.7. Related add/remove operations do not rewrite or depend on Purchase and Runtime subentries.

Device Lifecycle validates new relationship targets using the Home Assistant 2026.8 single-config-entry Device Registry model. It never attaches its config entry to an external device or changes identifiers, connections, names, Area, config-entry ownership, or device topology.

## Purchase relationships

An Asset may have no Purchase. A manual or existing eligible Asset can later be assigned to any currently configured Purchase, and the relationship can be cleared without recreating the Asset.

Existing relationships to historical or no-longer-configured Purchases are preserved and displayed safely. Historical Purchases are not offered as targets for new relationships.

## Storage and migration impact

0.5.7 retains the existing `ha_device_refs` structure in Store 2.1, where each reference contains only `device_id` and `role` (`primary` or `related`). Store migration is limited to adding canonical Runtime data; there is no config-entry version migration.

## Warranty

Existing Purchase workflows support these warranty modes:

- Not specified
- 1 year
- 2 years
- Manual

For 1- and 2-year warranties, the warranty end date is calculated from the Purchase date. Manual mode allows an arbitrary end date. Device Lifecycle 0.5.7 preserves existing warranty projection behavior and does not add a general Asset-level warranty editor.

## Runtime tracking

Runtime tracking is optional and configured separately for each physical Asset represented by a Home Assistant device.

Available tracking methods are:

- **Entity state is on**: counts time while a supported source entity is `on`
- **Power above threshold**: counts time while a power sensor exceeds the configured watt threshold

Runtime setup first selects the target device and tracking method, then a suitable source entity. Power threshold and hysteresis are shown only in power mode. Supported power units such as `W` and `kW` are normalized to watts; energy, current, voltage, and frequency sensors are not valid power sources.

The cumulative **Runtime hours** sensor is localized as **Käyttötunnit** in Finnish. Asset Store owns its cumulative seconds in 0.5.7. The sensor projects canonical committed time plus any sealed pending delta and current monotonic active interval. Time while Home Assistant is stopped is not observed or added.

While active, Runtime checkpoints to Asset Store every five minutes. It also checkpoints when the source stops or becomes unavailable/unknown and during normal unload or shutdown. Under healthy Store operation, a hard crash therefore loses only time since the last successful checkpoint, normally less than five minutes. No crash-loss bound is claimed while persistence is failing. Runtime reconciliation and entity setup remain primary-only; related devices are never Runtime targets or fallbacks.

## Upgrade notes

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
7. Go to **Settings > Devices & services > Add integration** and search for **Device Lifecycle** or **Laitteen elinkaari**.

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

Device Lifecycle 0.5.7 does not provide Asset archive/delete, restore, purge, merge, Runtime reset/manual editing, relationship history, automatic discovery, or stale-device rematching actions.

## Roadmap

- **0.5.4 — Manual Assets & Deployment**
- **0.5.5 — Purchase Asset membership follow-up**
- **0.5.6 — HA Relationships**
- **0.5.7 — Asset Runtime**
- **0.6.x — Maintenance**
- **0.7.x — Home Assistant Exposure / UI**
- **0.8.x — Lifecycle & Replacement**
- **0.9.x — Portability & Hardening**
- **1.0 — Stable**

## License

MIT
