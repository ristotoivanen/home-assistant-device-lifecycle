# Device Lifecycle

Home Assistant custom integration for tracking real-world physical assets, purchases, warranty information, lifecycle metadata and optional runtime hours.

The integration UI is localized. In Finnish Home Assistant it is shown as **Laitteen elinkaari**.

Lifecycle status text follows the Home Assistant system language. English and Finnish are currently included.

## Asset Core in v0.5.0

Version 0.5.0 introduces **Asset Core**, a persistent data model that separates the real physical item from the Home Assistant device that happens to represent it today.

Each physical Asset now has two permanent identifiers:

- an internal immutable UUID used for technical relationships and stable entity unique IDs
- a short human-facing Asset ID such as `DL0001`

Asset IDs are allocated monotonically and are never recycled. A Home Assistant device ID is stored only as a relationship to the Asset, not as the Asset's identity. The relationship model is already plural so later releases can attach multiple Home Assistant devices to the same real-world Asset without changing the core schema.

Purchase transactions and physical Assets are also separate. One purchase can contain multiple Assets while shared transaction data is stored only once.

Asset Core uses Home Assistant's private, atomic, versioned storage. The schema includes an explicit migration path from its first release so later versions can evolve without treating entity state as the only source of truth. The core invariants and extension rules are documented in [`ARCHITECTURE.md`](ARCHITECTURE.md).

### v0.4.x migration

Upgrading from v0.4.x automatically normalizes existing purchase and runtime subentries into Asset Core. Existing Home Assistant entity IDs are preserved while Device Lifecycle entity unique IDs are migrated to immutable Asset UUID-based IDs. This keeps the existing Runtime hours restore history available across the upgrade.

The normalized Asset store is written before generated stable UUID references are added back to Home Assistant config subentries. The migration is designed to be repeatable after an interrupted setup without allocating a second Asset ID for the same physical device.

Runtime totals still use Home Assistant's restore mechanism in v0.5.0. Asset Core provides the stable identity foundation for moving the accumulated total fully into Asset-owned persistent data in a later release.

Because v0.5.0 migrates Device Lifecycle entity unique IDs and the parent config-entry schema, downgrading back to v0.4.x is not a supported recovery path. Create a Home Assistant backup before upgrading a production installation and restore that backup if a rollback is required.

## Features

- Persistent Asset identity independent of Home Assistant device IDs
- Permanent human-facing Asset IDs (`DL0001` ... `DL9999`)
- Versioned private Asset Core storage with explicit migration support
- One Home Assistant integration with multiple purchase subentries
- One purchase can contain multiple physical Assets
- Purchase details can be edited later
- Devices can be added to or removed from an existing purchase without recycling their Asset identity
- Purchase currency is captured from the configured Home Assistant currency
- Optional receipt or invoice URL in addition to the receipt/order reference
- Warranty presets: 1 year, 2 years, manual end date, or not specified
- Warranty end date is calculated automatically for 1- and 2-year warranties
- Lifecycle sensor is linked to the existing physical Home Assistant device
- Warranty status is visible directly on the device page
- Manufacturer, model, model ID, serial number, firmware and hardware version are read from Home Assistant when available
- Asset metadata records its source so future user overrides can be protected from automatic Home Assistant refreshes
- Optional per-device cumulative runtime tracking
- Runtime can be detected from an entity being `on` or from a power sensor exceeding a configurable threshold
- Runtime source pickers are filtered for the selected tracking mode
- Power tracking supports configurable hysteresis to avoid threshold chatter
- Runtime sources are validated again when the configuration is saved
- Device Lifecycle's own entities are excluded from runtime source selection
- Obvious system/software integrations are conservatively filtered from physical-device selection
- Home Assistant service-type devices are rejected as purchase/runtime targets even if the native picker still shows them
- Runtime totals are restored across Home Assistant restarts
- Finnish and English UI translations

Example lifecycle sensor state:

```text
Active · 164 days · 19 Jan 2027
```

Lifecycle sensor attributes also expose the stable Asset identity, for example:

```text
asset_id: DL0001
asset_uuid: 9b5a...
```

Example runtime sensor:

```text
Runtime hours: 1284.53 h
```

## Requirements

- Home Assistant 2026.8.0 or newer

The integration uses the Home Assistant 2026.8 device-linking model where lifecycle and runtime entities are linked to an existing physical device without taking ownership of that device.

## Installation with HACS

Until this repository is included as a HACS default repository, add it as a custom repository:

1. Open HACS.
2. Open the menu and choose **Custom repositories**.
3. Add:
   `https://github.com/ristotoivanen/home-assistant-device-lifecycle`
4. Select category **Integration**.
5. Install **Device Lifecycle**.
6. Restart Home Assistant.
7. Go to **Settings > Devices & services > Add integration** and search for **Device Lifecycle** or **Laitteen elinkaari** when using the Finnish UI.

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

## Usage

Add the integration once. Purchases and optional runtime tracking entries are created underneath the single integration entry.

For each purchase you can store:

- devices included in the purchase
- purchase name
- purchase date
- installation date
- warranty
- seller
- total purchase price
- receipt or order reference
- receipt or invoice URL
- notes

The purchase price is the total price of the purchase, not a per-device price. The configured Home Assistant currency is captured with the purchase and retained when the purchase is edited later.

When adding a new purchase, leaving the installation date empty stores the purchase date as the installation date by default. The installation date can be changed or cleared later when editing the purchase.

In v0.5.0 the purchase form still contains installation date and warranty because the UI remains compatible with v0.4.x. Asset Core normalizes those values onto each Asset internally. Later Asset-oriented UI can edit them per physical Asset without changing the storage identity model.

Physical-device pickers deliberately filter only integrations that are clearly system/software-only. Ambiguous devices remain visible rather than risking the accidental removal of a real physical device. Home Assistant device-registry entries marked as `service` are rejected when saving even if the native device picker still displays one.

## Warranty

Available warranty modes:

- Not specified
- 1 year
- 2 years
- Manual

For 1- and 2-year warranties, the warranty end date is calculated from the purchase date. Manual mode allows an arbitrary end date.

## Runtime tracking

Runtime tracking is optional and configured separately for each physical Asset currently represented by a Home Assistant device.

Available tracking methods:

- **Entity state is on**: counts time while the selected source entity state is `on`. The source picker is limited to sensible on/off domains such as switches, lights, binary sensors, input booleans and fans.
- **Power above threshold**: counts time while a source sensor with the Home Assistant `power` device class is above the configured watt threshold. Supported power units such as `W` and `kW` are normalized to watts before comparison. Energy (`kWh`), current (`A`), voltage (`V`) and frequency (`Hz`) sensors are not valid power sources.

Runtime setup uses two short steps. First select the target device and tracking method. The next step selects a source entity suitable for that method. Power threshold and hysteresis are shown only for **Power above threshold** mode.

Power hysteresis defaults to `0 W` to preserve the behavior of existing runtime trackers. For example, a `10 W` threshold with `2 W` hysteresis starts runtime above `10 W` and stops it below `8 W`; values between those limits retain the current running state.

The selected source is validated again when the configuration is saved. Power sources must use a Home Assistant-supported power unit and are normalized to watts before threshold and hysteresis evaluation. This prevents a manually supplied or stale selector value from bypassing the source rules.

The integration creates a cumulative **Runtime hours** sensor on the selected physical Home Assistant device. The entity name is localized as **Käyttötunnit** in Finnish. The sensor also exposes the stable Device Lifecycle Asset ID and UUID as attributes.

Runtime totals are restored after Home Assistant restarts. While a device is continuously active, the runtime sensor is refreshed every five minutes to avoid unnecessary recorder writes. Source state transitions are processed immediately.

Time while Home Assistant is stopped cannot be observed and is therefore not added to the runtime total. If the source entity becomes unavailable, the last accumulated runtime remains visible and tracking resumes when the source becomes usable again.

## Editing and deletion behavior

Existing purchases can be edited without recreating them.

Removing a device from an active purchase removes only that purchase's Lifecycle entity. The Asset Core identity remains reserved so its Asset ID is not recycled.

Removing an entire purchase removes entities belonging to that active configuration subentry. The normalized Asset/Purchase records are retained by Asset Core in v0.5.0 as a foundation for later archive, restore and explicit purge workflows.

Removing a runtime tracking entry removes only that Asset's runtime sensor. Other purchases, Assets, runtime tracking entries and integrations are left untouched.

Device Lifecycle never modifies or merges Home Assistant device-registry entries owned by other integrations.

## License

MIT
