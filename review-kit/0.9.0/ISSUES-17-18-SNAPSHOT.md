# Frozen issue snapshots for the Device Lifecycle 0.9.0 review bundle

Source repository: https://github.com/ristotoivanen/home-assistant-device-lifecycle

These are read-only copies of issue descriptions at review-bundle preparation on 2026-10-13. The GitHub issue threads may later change. They are context, not a replacement for approved architecture contracts.

---

## Issue #undefined: 0.9.x: Verified backup and restore before Marketplace default listing

Source: https://github.com/ristotoivanen/home-assistant-device-lifecycle/issues/17
Status: open

## Goal
Deliver **working, user-accessible Device Lifecycle backup/export and restore/import in 0.9.x**, before requesting inclusion in the default HACS / Home Assistant Marketplace catalog. The built-in Home Assistant backup remains supported, but **is not a substitute** for this feature.

This is a **release gate**, not permission to rush or silently weaken the data model. Current baseline: Device Lifecycle 0.8.0 / Store 4.1 / ConfigEntry 4.

## Required scope
- Export a **consistent, versioned** snapshot of all Device Lifecycle canonical data: Asset identities (UUID and human-readable ID), Purchases, Lifecycle and Replacement history, Archive state, Runtime totals, Maintenance schedules and history, and every owned relationship/provenance field.
- Define and implement the boundary with HA-managed **ConfigEntry/ConfigSubentries, Device Registry and Entity Registry**. The backup must support a coherent documented end-to-end restore, not just write back `device_lifecycle.assets` while configuration disagrees. Retain canonical identities; preserve existing HA entity identities where safely possible.
- Treat external HA device/entity IDs as **instance-local references**. During cross-instance restore do not silently rebind Assets to unrelated devices; unresolved links require explicit, visible resolution.
- Downloadable backup and user-accessible restore entrypoint, with version/schema and integrity validation, clear preview of impact and explicit confirmation before mutation.
- Define the supported MVP restore modes first. Prefer a safe restore to a clean target; **do not add destructive overwrite or merge** until collision, transactional rollback and identity semantics are specified and tested.
- Quiesce or checkpoint Runtime writers so an exported snapshot is internally consistent. Do not reset or invent Runtime totals.
- Restore must be all-or-nothing: validate *before* modifying persistent state; reject unsupported versions, partial/corrupt backups and identity collisions without partial writes. Specify rollback and failure recovery for every HA-owned side effect.
- No secrets or unrelated Home Assistant configuration in the export. State explicitly that the feature is not a full Home Assistant backup.

## Acceptance / proof
- [ ] Reviewed design contract covers backup format, scope, version compatibility, HA-managed configuration/registry boundary, reference rebinding, restore modes, and interruption behavior.
- [ ] Automated round-trip tests: export -> restore -> same canonical content and identities (including Asset ID allocation).
- [ ] Negative tests: truncated/invalid/unsupported backup, collision, missing external devices, interrupted/failed restore; originals remain unchanged on failure.
- [ ] Restoration to a clean **Test HA** instance and recovery scenario on the existing `ha-ai-lab`, with before/after evidence for Asset UUID/ID, Purchases, histories, Runtime totals, Maintenance, entity identity and no spurious duplicates.
- [ ] Both Finnish and English UI and instructions explain limitations, security and the distinction from full HA backup.
- [ ] Backward-compatibility gates and CI pass; release notes give safe upgrade/restore instructions.
- [ ] Only after accepted end-to-end restore tests may the catalog-submission gate be considered.

## Sequencing
1. Design and freeze restore contract.
2. Implement snapshot/export and validation.
3. Implement safe restore with recovery/transaction semantics.
4. CI + actual `ha-ai-lab` restore test.
5. Release 0.9.x with proven backup/restore.
6. Then pursue Marketplace default-catalog inclusion in its own tracking issue.

This prioritizes the existing 0.9.x **Portability, Data Safety & Hardening** roadmap before permanent purge or additional data-destructive behavior.

---

## Issue #undefined: Marketplace default-catalog inclusion (blocked by backup/restore #17)

Source: https://github.com/ristotoivanen/home-assistant-device-lifecycle/issues/18
Status: open

## Goal
Make Device Lifecycle discoverable/installable through the built-in Home Assistant Marketplace **default catalog** (not merely a custom repository). Do **not** submit a catalog PR or announce catalog-readiness until the backup/restore release gate has been met.

## Hard dependency
**Blocked by #17:** verified, user-accessible backup/export and restore/import, including real Test HA restore evidence, released in 0.9.x.

Marketplace becoming part of HA Core (planned for 2026.11) is **not** a deadline for Device Lifecycle. Existing custom-repository use and HACS-to-Marketplace migration can continue independently.

## Readiness work
- [ ] Confirm the final Marketplace/default-catalog eligibility requirements when the feature ships; check existing `hacs.json`, `manifest.json`, brand icon and GitHub release metadata.
- [ ] Ensure HACS action, Hassfest, supported HA compatibility CI and real HA regression tests pass with the **released** backup/restore build.
- [ ] Exercise 2026.10 -> 2026.11 HACS-to-Marketplace migration on `ha-ai-lab` (current custom repository, installed integration, upgrade path, maintained Device Lifecycle data). Test separately from the backup/restore round-trip.
- [ ] Update README install/upgrade instructions, marketplace link/badge only when verified; retain compatible installation methods where appropriate.
- [ ] Verify required repository metadata (issues enabled, topics, current release); submit PR to `hacs/default` following the then-current contributor rules.
- [ ] Verify post-inclusion catalog listing and update discovery in a test installation.

## Release gate
- #17 completed with restore evidence, no known data-loss/blocking defect.
- Version containing backup/restore released with release notes.
- No untested destructive/automatic migration introduced as part of Marketplace listing.

References: https://github.com/home-assistant/core/pull/183502 and https://hacs.dev/docs/publish/include/ .
