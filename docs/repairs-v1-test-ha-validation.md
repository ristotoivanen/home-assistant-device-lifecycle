# Repairs v1 destructive Test HA validation

This is the record of the destructive end-to-end validation of stale-reference Repairs v1, the feature released in 0.7.6. The README summarizes it in [0.7.6 post-release validation — Repairs v1 in Test HA — complete](../README.md#076-post-release-validation--repairs-v1-in-test-ha--complete). The expected behavior is the released contract in [Stale external device references in Repairs](../ARCHITECTURE.md#stale-external-device-references-in-repairs).

## Scope

- **Result.** 0.7.6 Repairs v1 destructive Test HA validation: **COMPLETE**, completed 2026-09-26.
- **Validation debt.** This closes the 0.7.6 post-release validation that was deferred because production has no naturally occurring stale device reference and the scenario is destructive.
- **What it is not.**
  - It is not a 0.7.7 release smoke. The [0.7.7 Test HA release validation](../README.md#077-test-ha-release-validation--complete) is a separate, earlier record and is unchanged.
  - It is not a 0.8.x feature.
  - No integration code, tests, Store schema, ConfigEntry version, manifest version, release, or tag was changed.

## Environment

| Item | Value |
|---|---|
| Test environment | Dedicated persistent Test HA, [`ha-ai-lab`](test-ha-lab.md) |
| Home Assistant | 2026.9.3 |
| Device Lifecycle | released 0.7.7, loaded |

0.7.7 did not change `stale_references.py`, so the released 0.7.7 implementation is the 0.7.6 Repairs v1 implementation.

## Fixture

The destructive steps used only the dedicated regression Asset **DL0009 — DL Repairs Fixture — destructive test**. It is described in [the lab document](test-ha-lab.md#dl0009--dl-repairs-fixture--destructive-test).

- **Primary.** "DL Repairs Fixture Primary", a synthetic device created from retained MQTT discovery.
- **Related.** "DL Repairs Fixture Related", a second synthetic retained-MQTT device.
- **No other configuration.** The fixture has no Purchase, no Runtime, and no Replacement configuration. Its Replacement diagnostic entity stays disabled by the integration.

Devices were made stale and recovered with two operations:

- **Making a device stale.** An empty retained payload on the device's MQTT discovery topic removed the external Home Assistant device.
- **Recovering a device.** Republishing the exact original discovery payload brought the device back.

DL0001–DL0008 were never used as destructive fixtures.

## Results matrix

| Check | Result |
|---|---|
| Primary stale | PASS |
| Ordinary reload while stale: issue persists | PASS |
| Home Assistant restart while stale: issue re-derived | PASS |
| Primary recovery (same device returns) | PASS |
| Related stale | PASS |
| Related recovery (same device returns) | PASS |
| Multiple stale: primary-only stage | PASS |
| Multiple stale: exact two-issue set | PASS |
| Multiple stale: canonical relationships preserved | PASS |
| Sibling issue isolation when the primary recovers | PASS |
| Multiple stale: related final recovery | PASS |
| Multiple stale overall | PASS |
| Issue contract: warning, not fixable, not persistent | PASS (`is_persistent` by source verification; see below) |
| User-facing Repairs text (English): names the Asset, no raw IDs | PASS |

## Primary stale and recovery

When the external primary device disappeared:

- The stored primary relationship stayed. Nothing was cleaned up automatically and nothing was relinked.
- The Relationships entity changed from `present` to `missing`.
- Exactly one deterministic primary issue appeared.
  - The issue is a warning and is not fixable.
  - It names the Asset by name and Asset ID (DL0009).
  - It shows no raw Device Registry ID.
- No Device Lifecycle reload was needed.

Recovery used the exact original discovery payload:

- Home Assistant restored the device under the same Device Registry ID.
- The issue cleared by itself, through the Device Registry `create` handling.
- There was no Asset edit, no relink, and no manual reload. The stored relationship simply resolved again.

**`is_persistent`.** Home Assistant 2026.9.3 `repairs/list_issues` does not expose `is_persistent`, so it cannot be observed at runtime. The released source settles it instead: `custom_components/device_lifecycle/stale_references.py` at release commit `c0b9f12d39fb653fa00e79bc00f133c9d48d9d10` calls `async_create_issue` with `is_fixable=False`, `is_persistent=False` and `severity=WARNING`. The automated tests also assert all three values.

## Reload and restart

These checks ran while the primary was stale.

- **Ordinary reload.**
  - An ordinary Device Lifecycle unload and reload did not delete the issue.
  - The issue ID and its creation time stayed the same.
  - The stored relationship was unchanged.
- **Home Assistant restart.**
  - After the restart and Device Lifecycle setup, the same deterministic issue existed again.
  - The stored stale relationship remained, and the related relationship stayed valid.
  - There was no Runtime or entity identity regression, and no Device Lifecycle error or traceback.
  - The startup event sequence itself was not observed, because the observing websocket was disconnected during the restart. Only the state before and after the restart is evidence.

## Related stale and recovery

When the external related device disappeared:

- The primary relationship stayed valid.
- The stored related relationship stayed.
- Exactly one related-specific issue appeared, and no primary issue.
- The Repairs UI showed no raw IDs.

When the same device returned, it came back under the same Device Registry ID and the related issue cleared by itself.

## Multiple stale / sibling isolation

This test checks two issues for the same Asset at once. It makes both the primary and the related device stale, then recovers them one at a time.

| Stage | Repairs total | Device Lifecycle issues | Relationships |
|---|---|---|---|
| Baseline | 0 | none | `present` |
| Primary stale | 1 | primary | `missing`; primary `missing`, related `present` |
| Related also stale | 2 | primary + related | `missing`; primary `missing`, 1 of 1 related `missing` |
| Primary recovered | 1 | related | `missing`; primary `present`, 1 of 1 related `missing` |
| Related recovered | 0 | none | `present` |

What this shows:

- **Two distinct issues.** With both devices stale there were exactly two issues. Each had its own deterministic, role-specific issue ID, so neither collided with or overwrote the other.
- **Relationships preserved.** Both stored relationships stayed unchanged at every stage, including while both devices were stale.
- **Sibling isolation.** Recovering the primary removed only the primary issue.
  - The related issue was not deleted and recreated. It kept the same issue ID, the same creation time, the same ignored/dismissed state, and the same translation data.
  - The observed Repairs issue registry events were exactly: create primary, create related, remove primary, remove related. During the observer gap (see Limitations) nothing was mutated, and both issues were identical afterwards, including their creation times.
- **Final recovery.** Recovering the related device removed the last issue.
- **Payload check.** Before each recovery in this run, the republished discovery payload was verified against its SHA-256 in the fixture baseline.

**English Repairs UI with two issues.** Both issues were listed separately. The titles distinguish the primary device from the related device, and both name the Asset and DL0009. Both issue dialogs link to Device Lifecycle. A scan of the rendered page found none of these in either the list or the dialogs:

- a raw Device Registry ID
- an issue ID
- a device digest
- the Asset UUID

Neither issue was ignored.

## Preserved invariants

- **Other Assets unchanged.** DL0001–DL0008 were unchanged throughout the run.
- **No reload during multiple stale.** The config entry stayed loaded, and its `modified_at` did not change.
- **Runtime continuity.**
  - The Runtime entities of DL0002 and DL0003 kept their identities.
  - Neither total ever decreased or reset, and DL0003's active Runtime kept accumulating.
  - The observed totals are historical observations, not new baseline invariants.
- **No errors.** No Device Lifecycle error or traceback was logged.

**Environment fingerprints.** These are environment-specific test evidence for `ha-ai-lab`, not product or API contracts. Each fingerprint is the SHA-256 of newline-joined canonical rows with no trailing newline.

- **Row format.** Each Entity Registry row is `entity_id|unique_id|registry_id|device_id|config_subentry_id|disabled_by`, with null written as empty. Rows are sorted lexicographically.
- **Original baseline.** The 58 Entity Registry rows of DL0001–DL0008.
  - Fingerprint: `8ea3da1fd696b39bbc345002671bb9801b12c88e65d3c7b79347fe4578f5c68c`.
  - It was unchanged at every multiple-stale checkpoint.
- **DL0009 fixture.** The fixture's 7 Entity Registry rows, followed by three rows: an `ASSET` row (Asset ID, Asset UUID, Asset Device ID), a `PRIMARY` row, and a `RELATED` row with the stored device IDs.
  - Fingerprint: `bafeeff9c247f49295c7bb3d4dc3c81c474f4e6b6541eb14a787ad522cf86d4b`.
  - It was unchanged at every multiple-stale checkpoint.
  - It does not include whether the external devices currently resolve, so a stale device does not change it.

## Automated-only coverage

The released test suite, `tests/test_stale_reference_repairs.py`, covers these cases. They were not performed live in this run:

- **Explicit repair clears only when the relationship is repaired.** Deselecting the device from a Purchase alone keeps the issue. Replacing the primary relationship clears it (`test_the_explicit_repair_clears_the_issue_only_when_it_is_done`).
- **Foreign issues are untouched.** Issues Device Lifecycle does not own are never touched, including look-alike issue IDs and other domains (`test_issues_it_does_not_own_are_never_touched`).
- **Unload keeps, removal clears only owned issues.** Unloading keeps the owned issues. Permanently removing the config entry clears only owned issues, and foreign issues survive (`test_unloading_keeps_the_issues_and_removal_clears_only_its_own`).
- **Silent restore.** A silent restore is picked up by the next setup (`test_a_silent_restore_is_picked_up_by_the_next_setup`).

The live same-device recovery above is also covered automatically (`test_the_same_device_returning_clears_its_issue`).

## Limitations

1. **Home Assistant 2026.8 silent restore was not live-tested.** The live validation used Home Assistant 2026.9.3. For the documented [2026.8 limitation](../README.md#home-assistant-device-relationships), only the next-setup recovery path is covered, by the automated test `test_a_silent_restore_is_picked_up_by_the_next_setup`.
2. **Finnish Repairs rendering was not live-tested separately.** The English user-visible Repairs text and its navigation were checked live.
3. **The Store file was not read directly.** The live session did not read the `.storage` file. Preservation of the stored relationships was observed through two things:
   - the Device Lifecycle Relationships projection, which exposes the stored device IDs
   - the stable DL0009 fixture fingerprint
4. **There was one observer gap during the multiple-stale run.** While a hidden frontend tab was suspended, the observing websocket was briefly disconnected.
   - No mutation happened during the gap, and the state after it was identical.
   - A separate websocket observed the decisive stages: primary recovery with sibling isolation, and the related final recovery.
5. **Recorder database integrity was not examined directly.**

## Final lab state

- Both fixture devices present, Relationships `present`.
- Repairs issues: 0 (Device Lifecycle: 0).
- Device Lifecycle loaded.
- 9 Assets, 65 Device Lifecycle entities (58 enabled, 7 disabled Replacement entities), 6 config subentries.

The raw evidence files contain environment identifiers, so they are working evidence kept outside the repository.
