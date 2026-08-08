# Laitteen elinkaari

Home Assistant custom integration for tracking device purchases, warranty information and lifecycle metadata.

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
- Finnish and English UI translations

Example sensor state:

```text
Voimassa · 164 pv · 19.1.2027
```

## Requirements

- Home Assistant 2026.8.0 or newer

The integration uses the Home Assistant 2026.8 device-linking model where lifecycle entities are linked to an existing physical device without taking ownership of that device.

## Installation with HACS

Until this repository is included as a HACS default repository, add it as a custom repository:

1. Open HACS.
2. Open the menu and choose **Custom repositories**.
3. Add:
   `https://github.com/ristotoivanen/home-assistant-device-lifecycle`
4. Select category **Integration**.
5. Install **Laitteen elinkaari**.
6. Restart Home Assistant.
7. Go to **Settings > Devices & services > Add integration** and search for **Laitteen elinkaari**.

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

Add the integration once. Purchases are created underneath the single integration entry.

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

## Editing and deletion safety

Existing purchases can be edited without recreating them.

Removing a device from a purchase removes only that device's lifecycle entity for the purchase.

Removing an entire purchase removes only lifecycle entities belonging to that purchase. Other purchases and integrations are left untouched.

## License

MIT
