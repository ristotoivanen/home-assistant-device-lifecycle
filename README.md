# Device Lifecycle

Home Assistant custom integration for tracking physical Assets, Purchases, deployment information, warranty metadata, optional Home Assistant device relationships, and runtime hours.

The integration UI is available in English and Finnish. In Finnish Home Assistant it is shown as **Laitteen elinkaari**, and the human-readable Asset ID is called **Elinkaaritunnus**.

## Asset Core

An **Asset** is the canonical record of one real-world physical item. A Purchase records how an Asset was acquired, while a Home Assistant device is an optional relationship to that Asset. Neither one defines Asset identity.

Every Asset has two permanent identifiers:

- an immutable internal UUID used for technical relationships and stable entity unique IDs
- a human-readable Asset ID such as `DL0014`

Asset IDs are allocated monotonically and never recycled. The UUID and Asset ID remain unchanged when the Purchase, deployment information, or linked Home Assistant device changes.

Asset Core uses Home Assistant's private, atomic, versioned storage. Its invariants and Store 1.2 schema are documented in [`ARCHITECTURE.md`](ARCHITECTURE.md).

## What's new in 0.5.4

Device Lifecycle 0.5.4, **Manual Assets & Deployment**, adds:

- Purchases that are valid before any Asset or Home Assistant device exists
- manual Asset creation without a Purchase or Home Assistant device
- management of manual, Purchase-created, and Runtime-created Assets
- editable physical Asset metadata
- explicit Purchase assignment and clearing
- Deployment status, Installation date, and Home Assistant Area management
- explicit linking, unlinking, and replacement of an existing Home Assistant device
- dependency protection for active Purchase and Runtime configurations
- English and Finnish UI text for the complete Asset-management workflow

Existing Purchase-based and Runtime-based workflows continue to work.

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
- **Home Assistant device**: inspect, link, unlink, or replace the primary device relationship

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

0.5.4 does not store deployment history.

## Home Assistant device relationships

A Home Assistant device reference is a relationship, not Asset identity. Each Asset can have at most one primary external Home Assistant device, and one Home Assistant device can be primary for at most one Asset.

Linking a device:

- does not change the Asset UUID or Asset ID
- does not attach the Device Lifecycle config entry to the external device
- does not change the external device's identifiers or connections
- does not rename or move the external device
- does not change the external device's Area or config-entry ownership
- may refresh non-user-owned Asset metadata from available device information
- never overwrites metadata explicitly owned or cleared by the user

Device Lifecycle does not automatically match devices by name, model, serial number, manufacturer, network address, or Area. It never automatically merges Assets.

A missing stored device is shown as unavailable and is not silently replaced. It can be unlinked or replaced when dependency checks allow it.

Unlinking or replacing the primary device is blocked while an active:

- Purchase configuration still includes that device, or
- Runtime configuration still tracks that device

Remove the active dependency first. Device Lifecycle does not rewrite dependent configurations automatically in 0.5.4.

## Purchase relationships

An Asset may have no Purchase. A manual or existing eligible Asset can later be assigned to any currently configured Purchase, and the relationship can be cleared without recreating the Asset.

Existing relationships to historical or no-longer-configured Purchases are preserved and displayed safely. Historical Purchases are not offered as targets for new relationships.

## Warranty

Existing Purchase workflows support these warranty modes:

- Not specified
- 1 year
- 2 years
- Manual

For 1- and 2-year warranties, the warranty end date is calculated from the Purchase date. Manual mode allows an arbitrary end date. Device Lifecycle 0.5.4 preserves existing warranty projection behavior and does not add a general Asset-level warranty editor.

## Runtime tracking

Runtime tracking is optional and configured separately for each physical Asset represented by a Home Assistant device.

Available tracking methods are:

- **Entity state is on**: counts time while a supported source entity is `on`
- **Power above threshold**: counts time while a power sensor exceeds the configured watt threshold

Runtime setup first selects the target device and tracking method, then a suitable source entity. Power threshold and hysteresis are shown only in power mode. Supported power units such as `W` and `kW` are normalized to watts; energy, current, voltage, and frequency sensors are not valid power sources.

The cumulative **Runtime hours** sensor is localized as **Käyttötunnit** in Finnish. Runtime totals continue to use Home Assistant restore state in 0.5.4 and are restored after restarts. Time while Home Assistant is stopped cannot be observed and is not added.

Runtime configuration and accumulated totals are not persisted in Asset Store in 0.5.4.

## Upgrade notes

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

Removing a Runtime tracking entry removes only that Runtime sensor. Other Purchases, Assets, Runtime configurations, and integrations are left untouched.

Device Lifecycle 0.5.4 does not provide Asset archive/delete, restore, purge, or merge actions.

## Roadmap

- **0.5.4 — Manual Assets & Deployment**
- **0.5.5 — HA Relationships follow-up / relationship model evolution**, if needed
- **0.5.6 — Asset Runtime**
- **0.5.7 — Data Safety**
- **0.6.x — Maintenance**
- **0.7.x — Home Assistant Exposure / UI**
- **0.8.x — Lifecycle & Replacement**
- **0.9.x — Portability & Hardening**
- **1.0 — Stable**

## License

MIT
