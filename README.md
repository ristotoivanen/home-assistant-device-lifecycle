# Device Lifecycle

Home Assistant custom integration for tracking device purchases, warranty information, lifecycle metadata and optional per-device runtime hours.

The integration UI is localized. In Finnish Home Assistant it is shown as **Laitteen elinkaari**.

Lifecycle status text follows the Home Assistant system language. English and Finnish are currently included.

## Features

- One Home Assistant integration with multiple purchase subentries
- Multiple physical devices can belong to the same purchase
- Purchase details can be edited later
- Devices can be added to or removed from an existing purchase
- Warranty presets: 1 year, 2 years, manual end date, or not specified
- Warranty end date is calculated automatically for 1- and 2-year warranties
- Lifecycle sensor is linked to the existing physical Home Assistant device
- Warranty status is visible directly on the device page
- Manufacturer, model, model ID, serial number, firmware and hardware version are read from Home Assistant when available
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
- notes

The purchase price is the total price of the purchase, not a per-device price.

Physical-device pickers deliberately filter only integrations that are clearly system/software-only. Ambiguous devices remain visible rather than risking the accidental removal of a real physical device. Home Assistant device-registry entries marked as `service` are rejected when saving even if the native device picker still displays one.

## Warranty

Available warranty modes:

- Not specified
- 1 year
- 2 years
- Manual

For 1- and 2-year warranties, the warranty end date is calculated from the purchase date. Manual mode allows an arbitrary end date.

## Runtime tracking

Runtime tracking is optional and configured separately for each physical device.

Available tracking methods:

- **Entity state is on**: counts time while the selected source entity state is `on`. The source picker is limited to sensible on/off domains such as switches, lights, binary sensors, input booleans and fans.
- **Power above threshold**: counts time while a source sensor with the Home Assistant `power` device class is above the configured watt threshold. Supported power units such as `W` and `kW` are normalized to watts before comparison. Energy (`kWh`), current (`A`), voltage (`V`) and frequency (`Hz`) sensors are not valid power sources.

Runtime setup uses two short steps. First select the target device and tracking method, then select the source entity. Power threshold and hysteresis are shown only for **Power above threshold** mode.

Power hysteresis is optional and defaults to `0 W` to preserve the behavior of existing runtime trackers. For example, a `10 W` threshold with `2 W` hysteresis starts runtime above `10 W` and stops it below `8 W`; values between those limits retain the current running state.

The selected source is validated again when the configuration is saved. Power sources must use a Home Assistant-supported power unit and are normalized to watts before threshold and hysteresis evaluation. This prevents a manually supplied or stale selector value from bypassing the source rules.

The integration creates a cumulative **Runtime hours** sensor on the selected physical device. The entity name is localized as **Käyttötunnit** in Finnish.

Runtime totals are restored after Home Assistant restarts. While a device is continuously active, the runtime sensor is refreshed every five minutes to avoid unnecessary recorder writes. Source state transitions are processed immediately.

Time while Home Assistant is stopped cannot be observed and is therefore not added to the runtime total. If the source entity becomes unavailable, the last accumulated runtime remains visible and tracking resumes when the source becomes usable again.

## Editing and deletion safety

Existing purchases can be edited without recreating them.

Removing a device from a purchase removes only that device's lifecycle entity for the purchase.

Removing an entire purchase removes only lifecycle entities belonging to that purchase.

Removing a runtime tracking entry removes only that device's runtime sensor.

Other purchases, runtime tracking entries and integrations are left untouched.

## License

MIT
