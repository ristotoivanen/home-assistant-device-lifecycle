# BL-03: Persistence evidence and offline backup recovery

Status: **Owner decisions approved, 2026-10-15.** Applies to the local 0.8.1 baseline-safety candidate for PR #22. This records OD-BL03-01 and OD-BL03-02; it does not approve a release, close BL-03, change a frozen schema, or define 0.9.0 recovery tooling.

The decision source is the owner request **“Device Lifecycle — Implement Approved BL-03 Owner Decisions”**, received on 2026-10-15. Its exact attachment source and preserved evidence are recorded under [Decision provenance and investigation evidence](#decision-provenance-and-investigation-evidence).

## Context

The independent BL-03 investigation reproduced stale overwrites across ConfigEntry reload, cancellation after durable writes, corruption retries that reset permanent-ID allocation, and disappearance of known or ambiguously written Stores. The candidate protects cooperating managers with one Store-key fence, retirement checks, owned executor completion and verified readback. Missing data still requires an explicit recovery contract.

Two historical tests expected retry from an empty in-memory snapshot after uncertain persistence and a missing file. Their old retry expectations do not prove noncommit: Home Assistant can report `WriteError` after atomic replacement, and a first write can land while the published manager remains empty. Their valid no-publication and notification assertions must be retained while the retry expectations are aligned with the approved contract.

An additional investigation demonstrated a conditional HA corruption-rename race: shutdown cancels HA's registered rename Future while its executor thread can still rename the canonical file. External restoration during that interval permits a later acknowledged canonical snapshot to be moved to a corruption sidecar. The complete acknowledged bytes survived in the demonstrated trace. The owner has defined the supported restoration boundary below; the HA rename mechanism has **not** been fixed by that decision.

## OD-BL03-01: Positive evidence governs persistence recovery

Fail closed when the authoritative durable state is unknown. None of the following proves that a write did not commit or that an installation is new:

- A missing canonical Store file or missing readback result.
- An empty published snapshot, absent manager owner, or newly constructed manager.
- `WriteError`, cancellation, caller timeout, failed readback or failed setup.
- The test harness's private knowledge that an injected exception occurred before I/O, when the production transport does not provide that ordering proof.

Positive evidence is narrower:

| Evidence | Permitted conclusion |
| --- | --- |
| Validation rejects the request before submission | That request did not write; a corrected command may be attempted. |
| A trusted transport positively establishes noncommit through its explicit non-ambiguous outcome contract | That transaction may be retried without treating missing data as proof. HA's exception class alone supplies no such proof. |
| Direct readback supplies the complete valid canonical envelope and payload under the Store-key fence | Recover the verified snapshot and evaluate the command against it, including canonical replay or conflict checks. |

A complete valid **empty Store envelope** is affirmative data; a missing file is not an empty snapshot. Even a structurally valid backup is not proof that acknowledged later changes never existed. Recovery must respect the known identity, history and allocator invariants; it must not silently select an older empty state to discard known records.

### New installation, retry and monotonic evidence

New installation and uncertain retry are separate states. A normal first-install path may bootstrap an empty Store only within an eligible lifecycle context before prior persistence activity, with cooperating I/O fenced, no pending or ambiguous write, and checks of canonical and preserved corruption artifacts completed. A load returning `None` is not by itself bootstrap eligibility. The retained-evidence limit below still applies.

After an ambiguous first write, empty published memory plus an absent file must remain blocked: no additional save, Asset creation, UUID allocation, DL-ID allocation or unverified publication may proceed from that uncertainty. This applies both to immediate Quick Create resolution and subsequent mutations or manager setup.

The Store lifecycle retains positive existence and uncertainty evidence across managers in the same HA instance. Its `store_seen` flag is monotonic for that Store key: observed canonical existence, a predecessor's verified nonempty snapshot, an ambiguous predecessor, or matching preserved corruption cannot be forgotten by owner replacement, failed setup, cancellation, or repeated retry. A valid recovery snapshot can resolve transaction uncertainty; it does not turn the installation back into a new one.

Transaction outcomes must contribute to that shared evidence even before the first successful manager setup: a verified save or validated durable recovery establishes persistence, and an ambiguous or cancelled submission cannot establish its absence. A manager without a registered owner must not bypass this protection. A positively proven noncommit outcome alone does not invent evidence of a prior commit.

When the canonical path is absent, HA's `<key>.corrupt.<timestamp>` files remain durable evidence of a previous Store. Setup must refuse empty initialization while that evidence remains. It may load a complete valid restored canonical Store without deleting the preserved corruption. Other Store keys' sidecars do not establish prior existence for this Store.

This is an evidence-limited guarantee. The in-memory lifecycle does not survive process termination, and a fresh process cannot detect every prior installation after an external actor deletes **all** canonical and preserved persistence artifacts. Absence in that situation cannot establish historical nonexistence. No owner-absence or empty-memory compatibility bypass is approved.

### Identity, allocator and publication invariants

- `asset_uuid` remains the canonical immutable Asset identity; a persisted UUID must not be silently recreated as a new Asset.
- Permanent `DLxxxx` IDs remain associated with their original Assets and must not be reissued. `next_asset_number` must retain its valid allocation state; gaps are not permission to reuse IDs.
- Recover one complete canonical snapshot with mutually consistent Assets, Purchases, Lifecycle events, Replacement records, Maintenance schedules/events and allocator state. Preserve Runtime totals, relationships, metadata and field provenance. Do not reconstruct missing data from partial records or old published memory.
- A successful mutation requires complete validation, atomic persistence and exact envelope/payload readback before acknowledgement. Cancellation still reaches the caller; a successfully verified durable snapshot is published before that cancellation returns. Unverified candidates are not published.
- Uncertain, missing, corrupt, unsupported or unverifiable state blocks subsequent persistence until a complete authoritative snapshot or genuine positive noncommit evidence resolves it. Recovery errors retain uncertainty; cancellation is not disguised as a successful recovery.

### Synchronization and availability

Preserve one lock per HA instance and Store key across setup, migration saves, mutation, readback, recovery, publication and retirement. A replacement must wait for earlier cooperating I/O and reject retired managers' later writes. Runtime locks precede the Store lock; Runtime preparation, platform unload and ConfigSubentry callbacks must not wait while holding the Store lock.

The integration owns completion of its executor writes rather than relying on a cancellable HA task-registration Future as proof that a worker stopped. Cancellation or a timeout must not release the fence while a late write can still land. A stalled operation can therefore block mutation, unload or reload indefinitely. The bounded pending-operation warning explains that wait without authorizing a timeout bypass. This is an intentional availability tradeoff.

## OD-BL03-02: Restore backups only with HA fully stopped

Manual restoration of Device Lifecycle Store data is supported **only while the Home Assistant process is fully stopped and no other process is performing Store I/O**. Stopping or reloading this integration, requesting HA shutdown, reaching a shutdown timeout, or observing a cancelled task is insufficient. The HA process must have exited, including its executor threads. Prevent automatic restart or another HA instance, restore agent, editor or synchronization process from accessing the Store during restoration.

External restoration while HA is running or stopping is unsupported. This boundary excludes the demonstrated late corruption-rename interaction from supported manual restoration; it does not repair HA's rename Future or claim that the cooperative Store lock drains it.

### Offline restoration procedure

1. **Stop HA completely.** Use the installation's normal process-management mechanism, confirm the HA process has exited, and exclude every other process performing Store I/O. Keep HA stopped until the complete replacement and permission checks are finished. Do not infer quiescence from a failed reload or a warning alone.
2. **Preserve the current evidence.** Copy any remaining canonical `.storage/device_lifecycle.assets`, all matching `.corrupt.*` files, relevant backup metadata and failure logs to a separate safe location before replacing anything. Retain exact bytes and hashes when available. A corruption sidecar can contain the latest complete acknowledged snapshot; its filename is not evidence that its entire content is unusable. Do not delete evidence to make setup look new.
3. **Choose and validate one complete authoritative backup.** Prefer a coherent Home Assistant backup with matching ConfigEntry and registry state. Check the complete Store envelope, supported schema, all record shapes/references, UUIDs, permanent DL IDs, Runtime totals, history and `next_asset_number` together against the [Store architecture](../../ARCHITECTURE.md#store-41-asset-archive-and-maintenance-080), [Asset Archive schema](../asset-archive-store-v4.md) and [Maintenance schema](../maintenance-store-v4-schema.md). For current Store 4.1 the envelope is key `device_lifecycle.assets`, major `4`, minor `1`; recovery from a pre-upgrade backup follows the existing [upgrade/downgrade rules](../../README.md#upgrading-from-07x-to-080), never a hand-edited version number. Normal setup's complete validation remains a final gate; JSON parsing alone is insufficient.
4. **Resolve backup consistency before installing it.** A valid but older backup may omit acknowledged changes or lower the allocator. Preserve newer candidate snapshots and sidecars. If the available material cannot establish consistent identity, history and allocation state, keep the integration unavailable and seek recovery review; do not merge fragments, invent records, reset counters or silently accept ID reuse. This procedure does not promise lossless recovery from an incomplete or stale backup.
5. **Restore the complete file while still offline.** Replace the canonical file as one complete snapshot using the backup mechanism or a controlled same-filesystem atomic replacement. Preserve the intended HA service ownership and private file permissions; do not make the Store broadly readable or writable to bypass an access error. Keep the recovery copies and sidecars. Do not copy individual collections or partially overwrite a file that HA could later read.
6. **Start HA and verify recovery.** Let normal setup validate the restored snapshot and any supported migration before using mutation flows. Check the Device Lifecycle logs for setup, persistence/readback or recovery errors; verify the expected UUIDs, DL IDs, allocator, Runtime totals, history and customization. A loaded integration alone does not prove an older backup preserved all previously acknowledged changes.
7. **If validation or recovery fails, remain fail closed.** Retain the errors and files. Do not repeatedly remove files, clear uncertainty, or initialize a blank Store to force availability. Any further manual restoration requires stopping HA completely again before touching the Store. Retry can resume after a complete authoritative snapshot is available; it must not restart permanent-ID allocation.

There is no new import, repair, fragment-merging or automatic sidecar-restoration tool in this decision. No production recovery was performed as part of the local investigation.

## Consequences and evidence limits

### Manager initialization scope

The supported ConfigEntry lifecycle awaits `AssetStoreManager.async_setup()` before publishing the manager to flows, Runtime writers or other integration consumers. That existing production call order supplies the loaded snapshot and single setup owner. Direct mutation of raw internal managers before setup is not a supported integration entry point or a tested multi-owner coherence contract.

The transaction-outcome latch defensively preserves pre-setup evidence for a later setup, but does not make arbitrary uninitialized managers coherent. A final real-file adversarial probe still reproduced a stale overwrite and DL0001 reuse when two separately constructed managers both mutated without either calling setup. No route through the supported ConfigEntry lifecycle was reproduced. The failing witness is retained as `owner-decisions-2026-10-15/final-independent-review/test_unsetup_second_manager_probe.py` and `unsetup-second-manager.log` in the evidence bundle. Extending guarantees to that raw API requires a separately reviewed initialization/load-or-refuse contract and test-fixture audit; claiming ownership on the first mutation alone would still not protect an uninitialized cold-start writer from an existing Store. This limitation is visible for independent review, not a claim that the demonstrated overwrite is fixed.

### Recovery and durability limits

Safety takes precedence over restoring historical retry availability. The two named historical tests may change only where their success expectations inferred noncommit from missing data; their no-publication and identity protections remain substantive assertions. Positive noncommit and complete-snapshot recovery controls remain necessary.

The supported restoration boundary is operational, not cross-process file locking. Noncooperating writers, power loss, forced process termination, deliberate removal of all evidence, and an incorrectly chosen older backup are outside what this in-memory fence can prove. Local tests with isolated real Store files exercise fault injection and restoration controls; their live test-owned file manipulations do not authorize live manual restoration in an operator installation.

The conditional rename trace demonstrated loss of canonical availability with complete bytes preserved in a sidecar. It did not demonstrate irreversible byte loss or DL-ID reuse from that trace. Preserve that distinction when reviewing the remaining risk. A separate recovery design would be needed to support external restoration during HA operation or shutdown.

Implementation verification, three-version suite results and independent review remain separate from these approved policy decisions. This ADR makes no claim that the current candidate is green or approved for release.

## Decision provenance and investigation evidence

Approved source: `/Users/ristotoivanen/.codex/attachments/16b36b18-bd3a-47b4-a48d-e30b4df63eae/Liitetty text.txt`, SHA-256 `89143331332d5f483dddb87f597d459ef7de912a87e8cbc9f4c6e2daa858fb39`. The exact request is preserved in the local review bundle as `owner-decisions-2026-10-15/adr-authoring/OWNER-REQUEST.txt`. Starting candidate: `c98fc70ac3b1dedd60cd01bc06bbc310bcee1a8c`.

The preserved bundle is named `overnight-review-2026-10-14/` and lives outside the repository. The following paths are relative to that bundle; they are evidence artifact names, not portable repository links:

- `FINDINGS.md`, `RED-GREEN-EVIDENCE.md`, `TEST-MATRIX.md`, `REMAINING-RISKS.md` and `SHA256SUMS`: investigation classifications, exact prior results and evidence inventory. The pre-decision summaries record a blocked candidate and must not be read as new acceptance results.
- `continuation-2026-10-15/SAFETY-CONTRACT-CONFLICT.md`: why the two historical retry expectations were unsafe; preserved baseline/current failure logs accompany it.
- `compatibility/writeerror-after-commit-proof.log`: real atomic-writer `WriteError` after canonical replacement.
- `final-independent-review/unowned-evidence-probe.log`: empty/unowned first-write counterexample.
- `continuation-2026-10-15/shutdown-rename-audit/REVIEW.md` and `current-probe-configured.log`/`.xml`: conditional live-restoration rename trace and no-restoration fail-closed control.
- `owner-decisions-2026-10-15/`: approved-decision follow-up, original-test preservation, updated results and final review package. Its results must be assessed on their recorded candidate bytes.

Repository-reviewable implementation and regression references:

- [Store manager and persistence protocol](../../custom_components/device_lifecycle/storage.py); [integration setup/unload](../../custom_components/device_lifecycle/__init__.py).
- [Original BL-03 fault cases](../../tests/test_persistence_safety.py), [lifecycle cases](../../tests/test_persistence_lifecycle.py), [setup/corruption cases](../../tests/test_setup_ownership_safety.py), [shutdown executor cases](../../tests/test_executor_shutdown_safety.py), and [known/missing Store cases](../../tests/test_missing_store_safety.py).
- [Quick Add replay cases](../../tests/test_quick_add_replay_safety.py) and [Quick Create positive noncommit/readback controls](../../tests/test_quick_add_storage.py).
- The two authorized historical expectation updates are in [publish refresh tests](../../tests/test_publish_refresh.py) (`test_recovery_of_a_missing_file_with_an_empty_store_announces_nothing`) and [Runtime tests](../../tests/test_runtime.py) (`test_real_store_writeerror_is_detected_and_not_published`). Their names identify the preserved original cases; see the follow-up evidence for exact changes and results.
