# Device Lifecycle

Home Assistant custom integration for tracking device purchases, warranty information, lifecycle metadata and optional per-device runtime hours.

The integration UI is localized. In Finnish Home Assistant it is shown as **Laitteen elinkaari**.

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
- Runtime can be detected from an entity being `on` or from a numeric power sensor exceeding a configurable threshold
- Runtime totals are restored across Home Assistant restarts
- Finnish and English UI translations

Example lifecycle sensor state:

```text
Voimassa · 164 pv · 19.1.2027
```

Example runtime sensor:

```text
Käyttötunnit: 1284.53 h
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

- **Entity state is on**: counts time while the selected source entity state is `on`. This is suitable for lights, switches and similar entities.
- **Power above threshold**: counts time while a numeric source entity is above the configured watt threshold. This is suitable for devices whose actual operation is best detected from measured power.

The integration creates a cumulative **Käyttötunnit** sensor on the selected physical device.

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
