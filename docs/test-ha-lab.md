# Persistent Test HA lab

This document describes `ha-ai-lab`, the persistent Home Assistant environment used for Device Lifecycle regression and UX testing. It is a test fixture, not part of the integration: nothing here is a Device Lifecycle feature, schema, or API contract.

The lab is separate from release history. Release validation results are recorded per release in the README's [Release validation](../README.md#release-validation) section. Building the original lab did not perform the 0.7.6 destructive stale-reference Repairs v1 validation. That validation was completed later in this lab, on 2026-09-26. It is recorded in the README's [0.7.6 post-release validation](../README.md#076-post-release-validation--repairs-v1-in-test-ha--complete) and in [Repairs v1 destructive Test HA validation](repairs-v1-test-ha-validation.md). It added the [DL0009 Repairs fixture](#dl0009--dl-repairs-fixture--destructive-test).

## Baseline

The current baseline:

| Item | Value |
|---|---|
| Home Assistant | 2026.9.3 |
| Device Lifecycle | 0.7.7, loaded |
| Assets | 9 |
| Device Lifecycle entities | 65 (58 enabled, 7 disabled Replacement entities) |
| Config subentries | 6 |
| Device Lifecycle Repairs issues | 0 |

History of this baseline:

- **Original baseline.** The original lab baseline was established after the 0.7.7 release validation had already completed. It had 8 Assets (DL0001–DL0008) and 58 Device Lifecycle entities (52 enabled, 6 disabled Replacement entities).
- **Checking reload.** A final reload after that lab was built kept all 58 entity identities and placements and produced 0 `entity_registry_updated` placement events. That reload checked the original baseline. It was not another 0.7.7 release smoke; the formal two-reload 0.7.7 smoke had already completed separately.
- **DL0009 added.** DL0009 was added on purpose for the Repairs v1 destructive validation that was completed on 2026-09-26. It contributes 7 entities, one of them its disabled Replacement entity, and no config subentry.
- **DL0001–DL0008 unchanged.** They stayed unchanged throughout that validation.
- **Store 4.1 gates.** The table above records the lab before the Store 4.1 gates, with the released 0.7.7. Gates 5.3 and 5.4 then installed branch builds, and Gate 5.4 migrated the lab's Store to 4.1 and added one Maintenance schedule to DL0009; see [Gate 5.4 — Store 4.1 upgrade and smoke](#gate-54--store-41-upgrade-and-smoke).

## Asset scenarios

Each Asset exists for a specific scenario. Keep that purpose when changing lab data.

| Asset | Name | Scenario |
|---|---|---|
| DL0001 | Testidevice 1 | pre-existing legacy/manual Asset |
| DL0002 | DL Lab Device 01 | pre-existing primary Runtime Asset, power-based |
| DL0003 | DL Lab Device 02 — New | happy path with live Runtime accumulation |
| DL0004 | DL Lab Device 03 — Warranty expired | expired warranty |
| DL0005 | DL Lab Device 04 — Spare | not deployed; Purchase vs warranty separation |
| DL0006 | DL Lab Device 06 — Minimal | minimal/default Asset |
| DL0007 | DL Lab Device 05 Old — Replaced | replacement predecessor |
| DL0008 | DL Lab Device 05 — Replacement | replacement successor |
| DL0009 | DL Repairs Fixture — destructive test | dedicated Repairs regression fixture: stale primary and related device references |

### DL0001 — Testidevice 1

- Pre-existing manual Asset with no Home Assistant device and no Purchase.
- Warranty not specified. Deployment `not_deployed`.
- Keep it as existing historical test data. Its Finnish entity IDs are intentional existing state and are not renamed for consistency.

### DL0002 — DL Lab Device 01

- Pre-existing primary Runtime Asset. Its Home Assistant device, DL Lab Device 01, is MQTT-backed.
- Purchase: Test Purchase 1, purchased 2026-08-01, 2-year warranty.
- Deployed, installed 2026-08-10, area DL Lab.
- Runtime: power-based, source `sensor.dl_lab_device_01_power`.
- The accumulated value was 0.011743 h while the lab was built, and the source was idle during the 0.7.7 smoke. That value is historical evidence, not a future invariant: future tests check continuity and that the value does not reset, not this number.

### DL0003 — DL Lab Device 02 — New

- Normal happy-path Asset with active Runtime accumulation. Its Home Assistant device, DL Lab Device 02, is a synthetic retained MQTT discovery device (TEST DATA).
- Purchase: Test Purchase — Device 02, 2026-09-10, `2_years`.
- Deployed, installed 2026-09-12, area DL Lab.
- Runtime: `on_state`, source `switch.dl_lab_device_02_switch`, retained ON. Runtime is intentionally live and accumulating.
- Never compare this Runtime entity against a fixed number. Instead verify that:
  - the entity ID is the same
  - the unique ID is the same
  - the Asset still owns it
  - the value does not reset or decrease unexpectedly
  - accumulation continues after a reload
- How often its state updates while accumulating is UNKNOWN.

### DL0004 — DL Lab Device 03 — Warranty expired

- Purchase: Test Purchase — Device 03, purchased 2023-03-15, `2_years`, so the warranty expired on 2025-03-15.
- Deployed, installed 2023-03-20, area DL Lab.

### DL0005 — DL Lab Device 04 — Spare

- Lifecycle `active`, Deployment `not_deployed`: a spare.
- Purchase: Test Purchase — Device 04, purchased 2026-02-01, with the Purchase warranty field deliberately `none`.
- Asset warranty: `manual`, valid until 2027-02-01.
- This shows that Purchase warranty data and the Asset's own warranty stay separate.
- Device Lifecycle rejected an area while Deployment was `not_deployed`, with `invalid_area`. There is no "spare" Deployment value. This is intended domain behavior and useful invariant evidence, not a defect.

### DL0006 — DL Lab Device 06 — Minimal

- Home Assistant device: DL Lab Device 06.
- No Purchase, no warranty specified, no installation date, no area.
- Keep it deliberately sparse: it checks that optional data stays optional.

### DL0007 — DL Lab Device 05 Old — Replaced

- Manual Asset with no Home Assistant device.
- Lifecycle `retired`, Deployment `not_deployed`, installed 2022-05-01.
- Replaced by DL0008.

### DL0008 — DL Lab Device 05 — Replacement

- Home Assistant device: DL Lab Device 05.
- Warranty `manual`, until 2029-06-30.
- Deployed, installed 2026-06-20, area DL Lab.
- Replaces DL0007, reason `planned_refresh`.

When the replacement was recorded, only the two Replacement entities changed. Lifecycle, Deployment, Purchase, warranty, Runtime, and Home Assistant device relationships did not change automatically. The predecessor's Lifecycle and Deployment were changed afterwards as separate, explicit steps. This is the regression evidence for Replacement ≠ Lifecycle ≠ Deployment.

### DL0009 — DL Repairs Fixture — destructive test

- **Purpose.** DL0009 is the persistent, dedicated regression fixture for stale-reference Repairs. It was added for the completed [Repairs v1 destructive validation](repairs-v1-test-ha-validation.md).
- **Linked devices.**
  - Primary Home Assistant device: DL Repairs Fixture Primary.
  - Related Home Assistant device: DL Repairs Fixture Related.
  - Both are synthetic retained-MQTT devices; see [Synthetic Home Assistant devices](#synthetic-home-assistant-devices).
- **No other configuration.** It has no Purchase, no Runtime, and no Replacement configuration. Its Replacement diagnostic entity stays disabled by the integration.
- **Known-good state.** The known-good baseline has both linked external devices present. In that state:
  - Relationships is `present`.
  - There are 0 Device Lifecycle Repairs issues.
- **How it is used.** It is used for controlled stale-reference and recovery tests:
  - A reference is made stale by clearing the device's retained discovery config with an empty retained payload.
  - It is recovered by republishing the exact original discovery payload. Home Assistant then restores the same Device Registry ID.
  - Keep the original payloads, and verify them, before clearing anything.
- **Afterwards.** Return the fixture to its known-good state after every test.
- **Other Assets.** Do not use DL0001–DL0008 for destructive Repairs tests.

## Synthetic Home Assistant devices

DL Lab Device 02–06 are synthetic test devices, not physical devices. They use retained MQTT discovery on the lab's existing Mosquitto broker:

- manufacturer `Device Lifecycle Lab`
- model `MQTT Test Device (TEST DATA — <scenario>)`
- each device provides a Switch, a Power, and a Temperature entity
- discovery, state, and availability messages are retained, so the devices persist across restarts

These devices remain unchanged.

The DL0009 Repairs fixture adds two more synthetic retained-MQTT discovery devices on the same broker:

- DL Repairs Fixture Primary, the primary device of DL0009
- DL Repairs Fixture Related, a related device of DL0009
- manufacturer `Device Lifecycle Lab`
- models `MQTT Repairs Fixture Primary (TEST DATA — disposable)` and `MQTT Repairs Fixture Related (TEST DATA — disposable)`
- each device provides one Temperature entity
- their discovery config is retained, so they persist across restarts

A separate unlinked MQTT recovery probe was used to verify same-device restoration before the destructive Asset tests. It is not part of DL0009 or the Device Lifecycle regression inventory.

This is Test HA fixture infrastructure only. It is not part of Device Lifecycle's architecture, which does not depend on MQTT.

## Values observed in 0.7.7 flows

These are the values the 0.7.7 flows offered in the lab. They are observations, not a stability promise; [ARCHITECTURE.md](../ARCHITECTURE.md) defines the canonical model.

| Domain | Values |
|---|---|
| Lifecycle | `unknown`, `active`, `retired`, `disposed`, `lost` |
| Deployment | `unknown`, `not_deployed`, `deployed` |
| Warranty | `none`, `1_year`, `2_years`, `manual` |
| Runtime | `on_state`, `power` |
| Replacement reason | `unknown`, `planned_refresh`, `upgrade`, `failure`, `warranty_rma`, `other` |

`3_years` is planned only. It does not exist in 0.7.7 and was not used.

## Dashboards

The lab has two dashboards with different purposes. Keep them separate.

### Device Lifecycle Lab (`/dl-lab`)

Technical regression and diagnostics dashboard for release testing and investigation. It has five views: Overview, Assets, Runtime, Lifecycle / Warranty, and Test / Diagnostics.

It covers:

- the regression inventory and an Asset state overview
- warranty, lifecycle, and deployment compared side by side
- Runtime visibility, history, and statistics
- links to the Device Lifecycle devices
- a release-test checklist
- navigation to Events, Repairs, logs, Entities, and History
- live entity and state diagnostics

It may show technical information that helps testing. It is not the reference end-user experience.

### Device Lifecycle (`/device-lifecycle`)

Reference end-user dashboard, built only from native Home Assistant cards. It tests whether the entities the integration exposes support a useful everyday experience. It has four views:

- **Overview** is organized around user questions rather than integration internals: Needs attention, a warranty summary, a Runtime summary, and My devices.
  - Needs attention currently covers expired warranty, retired/lost/disposed Lifecycle, replacement state, a missing linked device, and unavailable relevant data.
  - `not_deployed` on its own is intentionally not an attention condition.
- **Devices** has readable sections for the baseline Assets represented on the dashboard (DL0001–DL0008). It shows only relevant data:
  - Lifecycle, Deployment, and installation/location
  - warranty and Purchase
  - Runtime and Replacement where present
  - the Asset ID as secondary information
  - links to the Device Lifecycle device and to a linked Home Assistant device where one exists

  Empty optional fields are not shown just for symmetry.
- **Warranty** separates Needs attention / expired, Active warranties, and Warranty not specified. Not specified does not mean expired. The integration's warranty state stays authoritative, and Purchase is not warranty.
- **Runtime & history** shows only Runtime-tracked Assets (currently DL0002 and DL0003), with readable accumulated Runtime and useful history. Configuration internals stay on the Lab dashboard.

Replacement relationships are shown with readable Asset names: the old Asset shows "Replaced by" its successor, and the successor shows "Replaces" its predecessor. No raw relationship IDs are shown.

After this dashboard was built, the lab was checked and was otherwise unchanged:

- the stored Device Lifecycle Lab dashboard and the Device Lifecycle configuration were unchanged
- the config entry `modified_at`, the subentries, and the Device Registry were unchanged
- Repairs stayed at 0
- no Device Lifecycle reload was performed, and Runtime was not reset
- the dashboard references 44 entities, with 0 missing and 0 disabled

DL0003 Runtime kept accumulating while the dashboard was being built.

## Store 4.1 pre-activation external gates — 2026-09-27

Owner-run checks of the external gates in the [Store 4.1 implementation plan](store-4-1-implementation-plan.md#5-external-test-ha-gates), sections 5.2 and 5.3. They were run on this lab on 2026-09-27 with the production Store still at 3.1. Store 4.1 was not activated, and no Archive or Maintenance behavior was involved. Following the plan, only key names, counts, Asset IDs, and outcomes are recorded here, not field values, UUIDs, subentry IDs, or file hashes.

### Lab inventory at the time of the gates

The original [baseline](#baseline) had 8 Assets after the 0.7.7 release validation. On 2026-09-27 the lab held 9 Assets and 4 Purchases. The additional Asset is DL0009, the [DL0009 Repairs fixture](#dl0009--dl-repairs-fixture--destructive-test) added for the completed Repairs v1 destructive validation. The difference is a change in lab data, not a Store-shape finding.

### Gate 5.2 — Store 3.1 source shape

Because the Test HA core-ssh environment did not provide Python, the owner ran an equivalent read-only `jq` inspection implementing the section 5.2 shape checks against a copy of `/config/.storage/device_lifecycle.assets` in `/tmp`.

| Check | Observed |
|---|---|
| Envelope | key `device_lifecycle.assets`, version 3, minor version 1 |
| Top-level keys | exactly `assets`, `lifecycle_events`, `next_asset_number`, `purchases`, `replacement_records`; none missing, none unexpected |
| Records | 9 Assets, 4 Purchases |
| Asset record shapes | every Asset had exactly the 20 Store 3.1 Asset keys |
| Purchase record shapes | every Purchase had exactly the 12 Purchase keys |
| Read-only | the live file and the inspection copy had equal SHA-256 before the inspection, and the live file still matched afterwards |
| Result | `RESULT: COMPATIBLE` |

Gate 5.2: **PASS**.

### Gate 5.3 — Runtime identity continuity

The branch build `claude/admiring-turing-yqbbb9` at `0a30142` (WP7) was installed, with the Store still at 3.1.

**Ownership move.** Before the build was installed, the two Runtime entities were owned by their Runtime ConfigSubentries in the Entity Registry: DL0002 (DL Lab Device 01) and DL0003 (DL Lab Device 02 — New). After the install and a restart, both:

- kept the same `entity_id` and unique ID
- had `config_subentry_id` null, the intended parent-owned placement
- kept `disabled_by` unchanged
- had no duplicate Runtime entity

**Remove, restart, and re-add (DL0002 only).**

1. The DL0002 Runtime configuration was deleted in the Home Assistant UI. Its ConfigSubentry was gone from the config entries. The Runtime Entity Registry entry remained, with the same `entity_id` and unique ID and `config_subentry_id` null, and the canonical Runtime total was unchanged.
2. Home Assistant was restarted with no Runtime tracking for DL0002. The same registry entry, `entity_id`, and unique ID remained, `config_subentry_id` stayed null, and the canonical Runtime total was unchanged.
3. Runtime tracking was added again for the same device. The same `entity_id` and unique ID were reused, `config_subentry_id` stayed null, and there was exactly one matching Runtime registry entry, with no `_2` or other duplicate. The canonical Runtime total resumed from the existing Asset-owned total, and no Runtime was added for the time without tracking.

**History.** In Home Assistant History, "DL Lab Device 01 Runtime hours" showed one continuous series across the install, removal, restart, and re-add, with no new entity or series.

The DL0002 total was carried through unchanged at every step. Its value in this one-time test is not a baseline; the lab still does not require Runtime values to match historical numbers.

Gate 5.3: **PASS**.

### Gate status after these checks

| Gate | Status |
|---|---|
| S1 — migration-source safety | CLOSED: repository evidence (WP1, WP3) and Gate 5.2 |
| S2 — pure Store 4.1 model | CLOSED earlier by repository evidence (WP1–WP6) |
| S3 — Runtime identity continuity | CLOSED: repository evidence (WP7) and Gate 5.3 |
| S4 — Store 4.1 activation | NOT STARTED; its preconditions S1–S3 are now met |
| Gate 5.4 — Store 4.1 upgrade and smoke | PENDING; required before the branch is merged or released (Gate S7) |

## Gate 5.4 — Store 4.1 upgrade and smoke

Owner-run check of section 5.4 and the S7 smoke of the [Store 4.1 implementation plan](store-4-1-implementation-plan.md#54-store-41-upgrade-branch-build-with-wp9-or-later), on 2026-09-27, after a Home Assistant backup. As for the earlier gates, only key names, counts, Asset IDs, states, and outcomes are recorded. The one Runtime identity below is synthetic lab evidence named by the gate itself.

### Environment

| | |
|---|---|
| Home Assistant Core | 2026.9.3 |
| Home Assistant OS | 18.3 |
| Release candidate | `ff8652ec29e5b90433604cd4d40601f63a617176` on `claude/admiring-turing-yqbbb9` |
| Pre-upgrade Store | version 3, minor version 1; 9 Assets, 4 Purchases |

### Pre-upgrade source inspection (5.2 repeated)

- exact Store 3.1 top-level shape: PASS
- exact Asset source shape: PASS
- exact Purchase source shape: PASS
- the inspection was read-only, and the live Store's SHA-256 was unchanged across it

### Migration

- the Store reported version 4, minor version 1
- exactly the seven Store 4.1 top-level keys: PASS
- every Asset gained `archived_at: null`
- both Maintenance collections were initially empty
- all 9 Assets and 4 Purchases were preserved
- the pre-existing data compared equal to the pre-upgrade copy after projecting the 4.1 additions (`archived_at` and the Maintenance collections) away, except for the canonical Runtime total of DL0003, which kept accumulating live while tracking ran between the two reads. With canonical Runtime totals excluded from the comparison, the data was byte-for-byte identical. The DL0003 difference is live Runtime accumulation, not a change made by the migration.

### Archive and Restore

- **DL0008.** The Archive preview correctly blocked Archive because the Asset was installed (`deployed`). No automatic undeploy was performed.
- **DL0005.** Archive succeeded. The Asset kept Asset ID DL0005, Lifecycle stayed `active`, and Deployment stayed `not_deployed`. The restricted archived view appeared. Restore succeeded: `archived_at` returned to `null`, the Asset returned to active management, and no Repairs issue remained.

### Runtime identity continuity (DL0002)

| | |
|---|---|
| Runtime entity | `sensor.dl_lab_device_01_runtime_hours` |
| Unique ID | `eaa208df-88f9-4ec3-b165-2f6058faad39_runtime_hours` |
| Canonical `runtime.total_seconds` before and after remove and re-add | `42.2740369420207573874` |

- Runtime tracking was removed, and the Runtime Entity Registry entry remained.
- The canonical total did not reset.
- Runtime tracking was added again: the same `entity_id` and unique ID were reused, `config_subentry_id` stayed `null`, and there was exactly one matching registry entry, with no `_2` duplicate.
- No Runtime was invented for the time without tracking.

### Maintenance (DL0009)

Schedule "Gate 5.4 daily maintenance": calendar interval 1 day, initial anchor 2026-09-25, preparation reminder 1 day.

| Step | Result | Status | Next due |
|---|---|---|---|
| Create | schedule created | OVERDUE | 2026-09-26 |
| Mark done, Just now | the starting-point lock notice was shown and needed explicit confirmation before saving | OK | 2026-09-28 |
| Correct | the original 2026-09-27 Event was kept as Corrected; the new 2026-09-26 Event is Active with Runtime left unknown | DUE | 2026-09-27 |
| Select the Corrected Event again | rejected safely | — | — |
| Void the active corrected Event | the 2026-09-26 Event became Voided; the projection fell back to the initial anchor | OVERDUE | 2026-09-26 |
| Disable | the schedule showed Disabled; history was kept | Disabled | — |

After the final restart the Schedule was still `enabled: false`, with the 1-day calendar interval and the 2026-09-25 initial anchor. Both Maintenance Events were preserved and voided (the original by the correction, the correction manually), and the correction Event still referenced the original through `corrects_event_uuid`.

### Final restart

| | |
|---|---|
| Store | 4.1; 9 Assets, 4 Purchases, 1 Maintenance schedule, 2 Maintenance events |
| DL0005 | active, `archived_at: null`, Lifecycle `active`, Deployment `not_deployed` |
| DL0002 | Runtime total preserved; same `entity_id` and unique ID; `config_subentry_id` null |
| Maintenance | schedule disabled; history persisted |
| Repairs | **Settings → System → Repairs**: "There are currently no repairs pending." No Device Lifecycle Repairs issue remained after the Gate 5.4 sequence. |

Gate 5.4: **PASS**.

## External gate summary

| Gate | Build | Home Assistant | Result |
|---|---|---|---|
| 5.2 — Store 3.1 source shape | production Store 3.1, before activation | Test HA lab | PASS (2026-09-27), repeated before the 5.4 upgrade: PASS |
| 5.3 — Runtime identity continuity | `0a30142` (WP7), Store 3.1 | Test HA lab | PASS (2026-09-27) |
| 5.4 — Store 4.1 upgrade and smoke | `ff8652e` (release candidate, WP1–WP17) | Core 2026.9.3, OS 18.3 | PASS (2026-09-27) |

The [gate table recorded after 5.2 and 5.3](#gate-status-after-these-checks) is kept as it was at that time. Current status:

| Gate | Status |
|---|---|
| S1 — migration-source safety | CLOSED |
| S2 — pure Store 4.1 model | CLOSED |
| S3 — Runtime identity continuity | CLOSED |
| S4 — Store 4.1 activation | CLOSED (WP9) |
| S5 — production mutation persistence | CLOSED (WP10, WP11, WP13) |
| S6 — Runtime/Archive Home Assistant safety | CLOSED (WP12, WP13) |
| Gate 5.4 — Store 4.1 upgrade and smoke | CLOSED: PASS above |
| S7 — Home Assistant surface and release readiness | CLOSED: WP14–WP17, Gate 5.4, and the WP18 release documentation |

Closing S7 means the branch has met every defined implementation and external validation gate. It does not mean that a tag exists, a release is published, or the branch is merged.

## Observations for later review

These are observations, not current defects.

- **Localized attribute keys.** The end-user dashboard reads Finnish entity attribute keys such as `takuu_tila`, `takuu_paattyy`, and `ostos`, so its templates depend on localized internal names. The 0.7.7 validation also noted `takuun_tyyppi`. This needs a later entity/API contract review.
- **Expiring soon.** Device Lifecycle has no "expiring soon" status. The end-user dashboard highlights warranties with fewer than 90 days left. That threshold is dashboard presentation only, not a Device Lifecycle domain rule.
- **Partly static presentation.** The attention and warranty lists discover Assets dynamically, but parts of the Devices view and its order must be updated by hand when Assets are added.
- **Runtime formatting.** One Runtime tile in the Devices view may show decimal hours while the Overview and Runtime views use a readable duration.
- **Two kinds of "Active".** Lifecycle and warranty can both show "Active". Icons and context tell them apart. This is useful UX evidence, not a reason to change integration semantics.
- **Runtime update cadence.** How often DL0003's Runtime state updates while accumulating is UNKNOWN.
- **Historical deprecation warning.** A log entry from 2026-09-20 reported a Home Assistant deprecation warning for `registry.devices` use in `custom_components/device_lifecycle/exposure.py` (line 133), saying it will stop working in Home Assistant 2027.9.0. It did not recur during the 0.7.7 smoke or the lab work, it is not a 0.7.7 release failure, and it is not yet known which installed version logged it.

## Persistent-baseline policy

- The lab is persistent. Release validation starts from this known baseline instead of rebuilding it.
- Do not reset or recreate the lab between releases unless there is a deliberate test-environment migration.
- Before a release smoke:
  - record the current inventory
  - record representative entity identities
  - record Runtime continuity points
  - keep existing relationships
- After the smoke, compare against the baseline, and document intentional changes separately from regressions.
- Do not require mutable Runtime values to match historical numbers.
- Synthetic test data may change deliberately, but document each change here.
- Run destructive Repairs tests only against DL0009. Afterwards, return it to its known-good state: both linked devices present and 0 Device Lifecycle Repairs issues.
