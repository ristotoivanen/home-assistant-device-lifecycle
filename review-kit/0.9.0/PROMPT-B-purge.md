# Reviewer B — Purge, tombstones, historical integrity and Store schema

You are Reviewer B, an **independent adversarial** architecture reviewer. Review the ATTACHED Device Lifecycle 0.9.0 repository ZIP, pinned to the immutable commit SHA supplied by the operator. Do not read other model results, do not change the repository, and do not approve the design.

Read review-kit/0.9.0/COMMON-RULES.md and REPORT-TEMPLATE.md first. Inspect the Master draft, ARCHITECTURE.md identity/Purge rules, frozen Store 4.1 and Maintenance contracts, storage.py validators, models.py, store_shape.py, archive.py, maintenance modules, Quick Create/replacement logic, and tests.

Try to break these contracts with **concrete minimal object graphs**:

1. Enumerate every reference in/out of an Asset: Purchase membership and ConfigSubentry, Lifecycle events/chains, Replacement records including voided/corrected links, Runtime totals and writer bindings, Maintenance schedules/history and corrected/voided references, HA links and generated entities.
2. Purge only after Archive and with no active Deployment, Runtime, Maintenance or unresolved relationships. Show cases where silent cleanup would break surviving history.
3. Tombstone minimalism versus retaining interpretable Purchase/Replacement history; collision between tombstone/active identity; orphaned histories; UUID and DLxxxx ID never-reuse; next_asset_number correctness.
4. Quick Create replay, aborted Purge/retry, Store migration and restoring a backup taken *before* Purge. Separate live-dataset deletion from old HA backups and Recorder copies.
5. Exact-shape Store 4.1 migration; version negotiation, unknown newer versions, deterministic transform, validation and rollback after interrupted write.

Return RB-01.. findings with example data graphs/payload fragments, line evidence, violated invariants, OD-05/06/08 implications and a deletion-dependency matrix. Every blocker needs a falsifying test, not merely a concern. Mark unrun tests NOT TESTED. Do not implement changes.

Finish with **G0: BLOCKED** or **G0: READY FOR OWNER ADJUDICATION**, not APPROVED.