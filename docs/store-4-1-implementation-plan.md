# Device Lifecycle 0.8.x — Store 4.1 activation, Asset Archive, and Maintenance integration plan

| | |
|---|---|
| Status | **APPROVED FOR IMPLEMENTATION** (planning document) |
| Code baseline | `5494942` (`docs: freeze coordinated Store 4.1 archive design`) |
| Production Store | 3.1 (`STORAGE_VERSION = 3`, `STORAGE_MINOR_VERSION = 1`) |
| Target Store | 4.1, frozen in [Asset Archive and Store 4.1 frozen architecture](asset-archive-store-v4.md) |
| Maintenance schema | [Maintenance Store 4.x frozen schema](maintenance-store-v4-schema.md) |

This is the single coordinated implementation plan for everything that remains between the implemented pre-activation core and a releasable 0.8.x: Store 4.1 activation, Asset Archive and Restore, and Maintenance production integration. It replaces the remaining production phases (commits 7–9 and Gates 4–7) of the [Maintenance implementation plan](maintenance-implementation-plan.md), which stays the record of the implemented Maintenance core and of the Maintenance user-interface requirements it lists.

This plan does not restate or change frozen semantics. Every record shape, invariant, and operation rule referenced here is defined in the two frozen documents above; where this plan and a frozen document disagree, the frozen document wins and this plan is corrected.

## 1. Starting point at `5494942`

Everything named here exists in the repository today.

### Store and persistence (`storage.py`)

- `STORAGE_VERSION = 3`, `STORAGE_MINOR_VERSION = 1`, `STORAGE_KEY = "device_lifecycle.assets"`, `STORE_TOP_LEVEL_KEYS` (the five Store 3.1 keys).
- `DeviceLifecycleStore._async_migrate_func(old_major, old_minor, old_data)`: deep-copies, rejects a wrong same-major minor with `AssetStoreError`, chains `_migrate_v1_to_v2_1` → `_migrate_v2_1_to_v3_1` → `_validate_store_data`.
- `DeviceLifecycleStore.async_save`: `super().async_save`, forces `_async_handle_write_data`, reads the file with `json_util.load_json`, compares the exact envelope; an unreadable readback raises `AssetStorePersistenceError(ambiguous=True)`, a mismatch raises it with `ambiguous=False`.
- `DeviceLifecycleStore.async_load_persisted_snapshot`: direct disk read that checks the envelope against the current constants.
- `_empty_store_data`, `_validate_store_data` (exact top-level set, then Asset, Purchase, membership, `_validate_lifecycle_graph`, `_validate_replacement_graph`). Asset and Purchase records are not exact-key validated.
- `AssetStoreManager`: `async_setup` (fails closed on a corrupt file, checks `STORE_TOP_LEVEL_KEYS.issubset`, validates), `_async_recover_uncertain_persistence`, `_async_mutate` / `_async_mutate_reporting` (lock → recover → deepcopy → mutator → validate → save if changed → publish), `_async_mutate_history(_reporting)`.
- Runtime: `async_initialize_new_runtime`, `async_import_legacy_runtime`, `async_commit_runtime_delta` (CAS), `register_runtime_checkpoint`, `mark_runtime_unresolved`, `async_checkpoint_runtime`, `async_prepare_runtime_unload`, `runtime_total_seconds`; in-memory `_runtime_writers` and `_runtime_unresolved`.
- Asset creation: `_new_asset` → `_create_asset_in_snapshot` (the 20 Store 3.1 Asset keys). Purchase creation: `_new_purchase` (the 12 Purchase keys); reconciliation writes the same keys with `purchase.update`.
- Identity: `asset`, `assets`, `asset_for_primary_device_id`, `_find_asset_by_primary_device`, `purchase`, `purchases`.
- Current-management mutations: `async_update_asset_metadata(_reporting)`, `async_set_asset_purchase(_reporting)`, `async_set_asset_deployment(_reporting)`, `async_link_asset_device(_reporting)`, `async_unlink_asset_device(_reporting)`, `async_add_related_device(_reporting)`, `async_remove_related_device`, `async_set_asset_lifecycle(_reporting)`, `async_create_asset_replacement`, `async_correct_asset_replacement` (voids one record and creates a new one), `async_void_asset_replacement`, `async_create_manual_asset`, `async_quick_create_asset` (may create a Replacement).
- Reconciliation: `_reconcile_entry_data` (Purchase subentries, then Runtime subentries in `subentry_id` order; a Runtime subentry resolves to the Asset its `asset_uuid` names when that Asset's primary device is unset or equals the subentry's `device_id`, otherwise to the owner of the primary device, otherwise a new Asset is created) and `async_reconcile_entry` (writes the Store first, then rewrites subentry data). Source-owned updates go through `_ensure_primary_reference`, `_refresh_home_assistant_metadata` (skips `field_sources == "user"`), `_assign_reconciled_purchase`, and `_apply_purchase_asset_fields` (skips user-owned `installed_date` and `warranty`).

### Pure libraries (inactive)

- `canonical.py`: `parse_canonical_utc`, `utc_now_iso`, `require_canonical_uuid`, text, date, and Decimal helpers.
- `maintenance.py`: `validate_maintenance_collections(assets, maintenance_schedules, maintenance_events)`, `MAINTENANCE_COLLECTION_KEYS`, `add_maintenance_collections`, `MaintenanceCompositionError`, baseline-lock helpers.
- `maintenance_projection.py`: `project_schedule(schedule, events, *, today, current_runtime)` returning `MaintenanceProjection(active, anchor, calendar, runtime, combined_state, preparation)`; `active=False` currently means only "disabled".
- `maintenance_mutations.py`: `MaintenanceSnapshot(assets, schedules, events)` (full Asset records are read, never changed), `MaintenanceMutationContext(today, observed_utc, current_runtime)`, `mutate_maintenance`, request classes, `MutationOutcome` (`CHANGED`, `NO_OP`, `REPLAY`).

### Home Assistant surfaces

- `__init__.py` `async_setup_entry`: `AssetStoreManager.async_setup` → `async_reconcile_entry` → `async_migrate_entity_registry` → `async_reconcile_exposure_registry` → `async_sync_stale_reference_issues` and its Device Registry listener → `async_forward_entry_setups(PLATFORMS)` → update listener that schedules a reload on any subentry change. `async_unload_entry` runs the Runtime unload gate first. `PLATFORMS = [Platform.SENSOR]`.
- `sensor.py` `async_setup_entry`: Asset entities built from detached snapshots at platform setup (`DeviceLifecycleSensor`, `DeviceDeploymentSensor`, `DeviceInstallationDateSensor`, `DeviceAssetIdSensor`, `DeviceLifecycleStatusSensor`, `DeviceReplacementSensor`, `DeviceRelationshipsSensor`). `DeviceRuntimeHoursSensor` is added with `config_subentry_id=<Runtime subentry>` and unique ID `runtime_unique_id(asset_uuid)` (`<asset_uuid>_runtime_hours`). Entities of removed subentries are removed from the Entity Registry.
- `exposure.py`: `_runtime_subentries_by_asset` (strict; raises on an inconsistent Runtime subentry), `async_reconcile_exposure_registry` with an update plan whose Runtime entry is desired in its Runtime subentry, and compensating rollback.
- `stale_references.py`: deterministic IDs `stale_device_<role>_<asset_uuid>_<digest>`, `async_sync_stale_reference_issues` creates desired issues and deletes every other owned ID, `async_delete_stale_reference_issues` on entry removal.
- `config_flow.py`: OptionsFlow `_finish_asset_action` → `_async_apply_reload` (awaited reload after a changed mutation); selectors `_asset_choices`, `_replacement_target_choices`, `_quick_replacement_choices`; identity checks through `asset_for_primary_device_id` (Quick Add, primary device). `RuntimeSubentryFlow` (`async_step_user`, `async_step_runtime_source` → `async_create_entry`; `async_step_reconfigure`, `async_step_reconfigure_source` → `async_update_and_abort`; the device cannot be changed by reconfigure).

### Verified Home Assistant behavior this plan relies on

Checked against the Home Assistant releases pinned by `requirements_test.txt` and `requirements_test_minimum.txt`:

- `Store._async_load_data` calls `_async_migrate_func` when the stored version differs, then calls `self.async_save(stored)` before returning. The migrated payload therefore passes through `DeviceLifecycleStore.async_save`, including the forced write and the direct readback, before `async_load` returns. `AssetStoreManager` publishes nothing until `async_load` has returned.
- A stored major version above the reader's version raises `UnsupportedStorageVersionError` without writing the file. A Store 3 reader therefore refuses a Store 4 file unchanged.
- `ConfigEntries.async_remove_subentry` calls `EntityRegistry.async_clear_config_subentry`, which removes every entity whose `config_subentry_id` is the removed subentry. Deleted entities are kept for `ORPHANED_ENTITY_KEEP_SECONDS` (30 days). This is why Runtime entity identity is currently lost after a long Archive.
- `ConfigSubentryFlowManager.async_finish_flow` calls `async_add_subentry` before `FlowManager` removes the flow and calls `flow.async_remove()`. A flow that is closed or aborted also ends in `flow.async_remove()`.

## 2. Concern dependency graph

| Concern | Depends on | Store 3.1 (pre-activation) or 4.1 live |
|---|---|---|
| A. Store 3.1 migration-source compatibility gate | — (repository part); real Test HA Store (external part) | Pre-activation |
| B. Pure Store 4.1 shape and validation helpers | A | Pre-activation, unreachable |
| C. Archive Store transform | — | Pre-activation, unreachable |
| D. Archive/Restore snapshot mutations | C (field helpers) | Pre-activation, unreachable |
| E. Archived-aware Maintenance pure domain | C (`asset_is_archived`) | Pre-activation, unreachable |
| F. Runtime Entity Registry ownership | canonical Runtime identity resolver (K1) | Pre-activation, **reachable** (it changes released behavior on Store 3.1) |
| G. Store 4.1 activation and migration dispatch | A (both parts), B, C, F | Crosses the boundary |
| H. Manager mutation and persistence APIs | G, D, E | 4.1 live |
| I. Runtime ↔ Archive eligibility and serialization | G, H, K | 4.1 live |
| J. Archived + Runtime setup quarantine and Repairs | G, K1 | 4.1 live |
| K. Identity versus management filtering | K1 resolver (pre-activation); management guards need G | K1 pre-activation; K2 4.1 live |
| L. Entity snapshot refresh | G | 4.1 live |
| M. Archive/Restore UI | H, I, J, K, L | 4.1 live |
| N. Maintenance entities and UI | H, L; Archive guards (K2) | 4.1 live |

```text
A ──► B ──┐
C ──► D ──┼──────────────► G ──► K2 ──► H ──► J ──► I ──► L ──► M ──► N-UI
C ──► E ──┘                ▲                                  └──► N-entities
K1 ──► F ─────────────────┘
A(external Test HA) ───────┘
```

Why this order:

- **F before G.** Runtime identity ownership is independent of the Store and must be proven on real installations before anything can make an Asset archived for a long time. It changes released Entity Registry placement, so it is proven and validated in Test HA while rollback is still an ordinary code rollback.
- **K2 before H.** Current-management guards on existing domains land before any API can produce an archived Asset, so there is never a branch state in which an archived Asset exists and an existing mutation ignores it.
- **J before I.** Quarantine needs only the resolver and 4.1 data (tests craft archived Assets directly), and it is the setup backstop that I relies on for backups and inconsistent older configuration.
- **L before M and N.** Archive and Restore must not need a parent reload, so the refresh path exists before any user interface exposes them.

Nothing between G and the release gate is released: the branch is not merged or released until Gate S7 closes, so intermediate branch states (for example 4.1 live without the Archive user interface) never reach users.

## 3. Work packages and commit sequence

Each work package is one commit. Every commit keeps the full test suite green on Home Assistant 2026.8.0 (minimum) and the supported baseline, passes the Ruff baseline gate, the dashboard check, and `git diff --check`. Nothing is merged, tagged, or released before Gate S7.

### WP1 — Exact record shapes and migration-source preflight

- **Commit message:** `store: add exact record shapes and migration-source preflight`
- **Goal:** the repository states the exact Store 3.1 Asset and Purchase record key sets and can prove any payload against them, fail closed, without changing anything.
- **Files and symbols:** new `custom_components/device_lifecycle/store_shape.py` (pure, stdlib only, no Home Assistant and no `storage` import):
  - `STORE_3_1_TOP_LEVEL_KEYS`, `STORE_4_1_TOP_LEVEL_KEYS` (`frozenset`)
  - `ASSET_KEYS_3_1` (20 keys), `ASSET_KEYS_4_1` (`ASSET_KEYS_3_1 | {"archived_at"}`), `PURCHASE_KEYS` (12 keys)
  - `StoreShapeError(ValueError)` carrying `kind`, `record_id`, `missing`, `unexpected` (key names only, never values)
  - `require_exact_record_keys(records, expected, *, kind) -> None`
  - `preflight_store_3_1_record_shapes(data) -> None`: every Asset has exactly `ASSET_KEYS_3_1`, every Purchase exactly `PURCHASE_KEYS`; read-only.
  - New `tests/test_store_shape.py`.
- **Preconditions:** none.
- **Invariants:** the preflight never mutates, copies back, strips, whitelists, or rebuilds; unknown data is reported as evidence. `STORE_3_1_TOP_LEVEL_KEYS == storage.STORE_TOP_LEVEL_KEYS`. The Store 3.1 validator is untouched.
- **Deliberately unreachable:** nothing in production imports `store_shape`.
- **Proof tests (unit):** the constants equal the key sets actually produced by `_create_asset_in_snapshot`, `async_quick_create_asset`, reconciliation of a Purchase subentry (`_new_purchase` and `purchase.update`), and the 1.1 / 1.2 / 2.1 → 3.1 migrations of the existing fixtures; an unexpected and a missing key on an Asset and on a Purchase each fail with the record and key named; the rejected input is unchanged both structurally (`==` a deep copy) and byte for byte (`json.dumps(..., sort_keys=True)` before and after); a non-mapping record fails closed; an AST test proves no production module imports `store_shape`.
- **Rollback:** revert; no persisted or registry effect.
- **Activation impact:** none.
- **External gate:** none. The Test HA inspection (section 5) can be run as soon as this commit exists; it is mandatory before WP9.

### WP2 — Archive Store transform and state validation

- **Commit message:** `archive: add Archive Store transform and state validation`
- **Goal:** the pure Archive component of the migration and the Archive load invariants.
- **Files and symbols:** new `custom_components/device_lifecycle/archive.py` (pure; imports only `canonical` and `const`):
  - `ARCHIVED_AT = "archived_at"`
  - `ArchiveCompositionError(ValueError)`, `ArchiveValidationError(ValueError)`
  - `add_asset_archive_state(candidate) -> dict`: deep copy; every Asset gains `archived_at = None`; fails closed if any Asset already has the key, if `assets` is missing or not a mapping, or if an Asset is not a mapping.
  - `asset_is_archived(asset) -> bool`: `asset["archived_at"] is not None` (strict key access; a missing key is a programming error, never "active").
  - `validate_asset_archive_state(assets) -> None`: `archived_at` is `None` or `parse_canonical_utc`-canonical; archived → `deployment_state != DEPLOYMENT_STATE_DEPLOYED`.
  - New `tests/test_archive_transform.py`.
- **Invariants:** no inference, no overwrite, no persistence, no clock read, input untouched. `add_asset_archive_state(add_maintenance_collections(v)) == add_maintenance_collections(add_asset_archive_state(v))` for every valid Store 3.1 payload.
- **Deliberately unreachable:** no production import.
- **Proof tests (unit):** only `archived_at` is added, to every Asset, as `None`; input unchanged and result not aliased; existing key → `ArchiveCompositionError` with input unchanged; commutation on a rich Store 3.1 payload (Purchases, Runtime, Deployment, Lifecycle events, Replacement records); `Z`, `-00:00`, `.000000+00:00`, naive, and non-string timestamps rejected; a future timestamp accepted; archived + `deployed` rejected; archived + `not_deployed` and `unknown` accepted.
- **Rollback:** revert.
- **Activation impact:** none.

### WP3 — Inactive Store 4.1 validator and migration step

- **Commit message:** `storage: add inactive Store 4.1 validator and migration step`
- **Goal:** the complete, separate Store 4.1 validator and the pure 3.1 → 4.1 step, provable while production stays 3.1.
- **Files and symbols (`storage.py`):**
  - Split `_validate_store_data` without changing its behavior: the exact 3.1 top-level check stays in `_validate_store_data`; everything after it moves to `_validate_store_payload(data)`, which `_validate_store_data` calls.
  - `_validate_store_v4_1_data(data)`: exact `STORE_4_1_TOP_LEVEL_KEYS`; `require_exact_record_keys` for `ASSET_KEYS_4_1` and `PURCHASE_KEYS`; `_validate_store_payload`; `validate_asset_archive_state`; `validate_maintenance_collections(data["assets"], data["maintenance_schedules"], data["maintenance_events"])`. `StoreShapeError`, `ArchiveValidationError`, and `MaintenanceValidationError` are re-raised as `AssetStoreError` with key names only.
  - `_migrate_v3_1_to_v4_1(data)`: `_validate_store_data` (3.1) → `preflight_store_3_1_record_shapes` → `add_asset_archive_state` → `add_maintenance_collections` → `_validate_store_v4_1_data`; `StoreShapeError` becomes `AssetStoreError(code="store_migration_source_incompatible")`.
  - `tests/test_maintenance_composition.py`: the pre-Gate-5 assertion that no source contains `_migrate_v3_1_to_v4` is replaced by an assertion that `_async_migrate_func` does not call `_migrate_v3_1_to_v4_1`.
  - New `tests/test_store_v4_1_validation.py`.
- **Invariants:** `_validate_store_data` accepts and rejects exactly what it did before, so the whole existing suite is the regression proof. The 3.1 validator never accepts 4.1, and the 4.1 validator never accepts 3.1. No final-shape validation runs between the two transforms. Runtime ConfigSubentry state is not validated.
- **Deliberately unreachable:** `_async_migrate_func`, `async_setup`, recovery, and `_async_mutate_reporting` still call only `_validate_store_data`.
- **Proof tests (unit):** every Store 4.1 invariant (exact 7 keys, 21-key Asset, 12-key Purchase, existing Asset invariants, canonical `archived_at`, archived + deployed, Lifecycle graph, Replacement graph, Maintenance collections and references); the migration step on 1.1, 1.2, 2.1 (via the existing chain), and 3.1 fixtures yields every Asset with `archived_at = None`, empty Maintenance, and otherwise identical data; a source-shape violation fails before any transform with the input unchanged; an AST test proves no production call path reaches `_validate_store_v4_1_data` or `_migrate_v3_1_to_v4_1`; `(STORAGE_VERSION, STORAGE_MINOR_VERSION) == (3, 1)`.
- **Rollback:** revert; the split is behavior-preserving.
- **Activation impact:** none.

### WP4 — Archive and Restore snapshot mutations

- **Commit message:** `archive: add Archive and Restore snapshot mutations`
- **Goal:** the Store-domain part of Archive and Restore as pure functions.
- **Files and symbols (`archive.py`):**
  - `ArchiveOutcome(StrEnum)`: `CHANGED`, `NO_OP`
  - `ArchiveAssetRequest(asset_uuid)`, `RestoreAssetRequest(asset_uuid)` (frozen dataclasses; canonical UUID validated at construction)
  - `ArchiveMutationError(ValueError)` with `code`: `archive_asset_not_found`, `archive_asset_deployed`
  - `apply_archive_request(assets, request, *, observed_utc) -> ArchiveOutcome`: mutates the given detached `assets` mapping in place (the manager's mutator pattern). Order: Asset exists → requested state already holds → `NO_OP` → (Archive only) not `deployed` → set `archived_at`. `observed_utc` is validated with `parse_canonical_utc` and injected by the caller.
  - `archive_state_matches(asset, request) -> bool`: the ambiguous-persistence acceptance rule (Archive: `archived_at is not None`; Restore: `archived_at is None`); the timestamp value is never compared.
  - New `tests/test_archive_mutations.py`.
- **Invariants:** only `assets[uuid]["archived_at"]` changes; no UUID, request ID, history, or clock read; `NO_OP` precedes every operational precondition (an archived and somehow deployed Asset cannot exist in a valid Store, and Restore of an archived Asset never checks Deployment). The Runtime preconditions are deliberately not here: they need Home Assistant facts and live in WP13.
- **Deliberately unreachable:** no production import.
- **Proof tests (unit):** the whole Store before and after differs only in that one field (compared with a deep copy of every top-level collection); NO_OP for Archive-of-archived and Restore-of-active with the input unchanged; a missing Asset fails; `deployed` fails for Archive; Restore of an archived Asset whose Deployment is `not_deployed` succeeds and sets `None`; a non-canonical `observed_utc` is rejected before any change; `archive_state_matches` ignores the timestamp value.
- **Rollback:** revert.
- **Activation impact:** none.

### WP5 — Archived-aware Maintenance pure domain

- **Commit message:** `maintenance: suppress projection and guard mutations for archived Assets`
- **Goal:** implement the frozen [Archived Assets](maintenance-store-v4-schema.md#archived-assets) rules in the pure layers.
- **Files and symbols:**
  - `maintenance_projection.py`: `InactiveReason(StrEnum)` with `DISABLED` and `ASSET_ARCHIVED`; `MaintenanceProjection` gains `inactive_reason: InactiveReason | None`; `project_schedule(schedule, events, *, today, current_runtime, asset_archived: bool)` with `asset_archived` keyword-only and required. When `asset_archived` is true the result is `active=False`, `inactive_reason=ASSET_ARCHIVED`, and no condition, combined state, or preparation, regardless of `enabled`; archived takes precedence over disabled. The effective anchor is still derived, so the projection has no hidden state.
  - `maintenance_mutations.py`: `MaintenanceArchivedAssetError(MaintenanceMutationError)` with code `maintenance_asset_archived`; `_reject_archived(snapshot, asset_uuid)` reads `snapshot.assets[asset_uuid]` through `archive.asset_is_archived`. It is applied after the replay check and before every other precondition in `create_schedule`, `edit_schedule`, `set_initial_anchor`, `add_interval`, `remove_interval`, `set_enabled`, `delete_schedule`, and `record_event`. `void_event` and `correct_event` never call it. The Maintenance persisted shape is unchanged: the archived state comes only from the Asset record the snapshot already carries.
  - Tests: `tests/test_maintenance_projection.py`, `tests/test_maintenance_mutations.py`, and `tests/test_maintenance_idempotency.py` fixtures gain `archived_at` on their Assets; new `tests/test_maintenance_archive.py`.
- **Invariants:** replay before the archive guard; no Maintenance record gains a field; `enabled` is never changed by the projection or by Archive.
- **Deliberately unreachable:** unchanged (the pure layers are still not called by production).
- **Proof tests (unit):** archived + enabled → inactive with `ASSET_ARCHIVED`; archived + disabled → `ASSET_ARCHIVED`; the same inputs with `asset_archived=False` → the normal projection, including an immediate `OVERDUE` and an active preparation reminder; each blocked operation rejected for an archived Asset with the snapshot unchanged; Record Event and Create Schedule replays of already-persisted requests on an archived Asset return `REPLAY`; Void and Correct succeed on an archived Asset, and their replays return `REPLAY`; a snapshot Asset without `archived_at` raises `KeyError` (fail closed, never "active").
- **Rollback:** revert.
- **Activation impact:** none.

### WP6 — Canonical Runtime subentry identity resolver

- **Commit message:** `runtime: extract canonical Runtime subentry identity resolution`
- **Goal:** one resolver that every later consumer shares (Archive eligibility, Runtime flow, setup quarantine), with exactly the reconciliation semantics.
- **Files and symbols:** new `custom_components/device_lifecycle/runtime_identity.py` (pure; `const` and `models` only):
  - `resolve_runtime_subentry_asset(assets, subentry_data) -> str | None`: no `device_id` → `None`; `asset_uuid` names an Asset whose primary device is unset or equals `device_id` → that Asset; otherwise the Asset whose primary device is `device_id`; otherwise `None` (reconciliation would create an Asset).
  - `runtime_subentries_resolving_to(subentries, assets, asset_uuid) -> tuple[str, ...]` over Runtime-type subentries in `subentry_id` order.
  - `storage.py`: `_reconcile_entry_data` uses the resolver for its lookup step and keeps its own create/refresh/rewrite behavior.
  - New `tests/test_runtime_identity.py`.
- **Invariants:** the resolver looks at all Assets (archive-blind). Reconciliation behavior is unchanged (the existing reconciliation, migration, and Runtime suites pass unmodified).
- **Deliberately unreachable:** no new behavior.
- **Proof tests (unit):** subentry with a matching `asset_uuid`; with an `asset_uuid` whose Asset has a different primary device (resolves by device); legacy subentry without `asset_uuid` (resolves by device); unknown device (`None`); missing `device_id` (`None`); an equivalence test that runs `_reconcile_entry_data` and the resolver on the same fixtures and compares the targets.
- **Rollback:** revert.
- **Activation impact:** none.

### WP7 — Parent-owned Runtime Entity Registry identity

- **Commit message:** `runtime: keep Runtime entity identity when tracking is removed`
- **Goal:** the Runtime sensor's Entity Registry entry becomes Asset/parent-owned (`config_subentry_id = None`) while the Runtime ConfigSubentry still decides whether a writer exists and how it is configured.
- **Files and symbols:**
  - `exposure.py`: the Runtime update plan's `desired_config_subentry_id` becomes `None` for every Runtime entity, including one whose Asset currently has no Runtime subentry; the skip for "Runtime entity with no active Runtime subentry" is removed. `_runtime_subentries_by_asset` keeps its consistency checks for writer setup only. The existing compensating rollback covers the move.
  - `sensor.py`: Runtime entities are added with `async_add_entities(runtime_entities)` (no `config_subentry_id`). The deleted-subentry cleanup and `_remove_unexpected_subentry_entities` keep removing only entities that are still subentry-owned, so a parent-owned Runtime entry survives. An Asset with a parent-owned Runtime entry and no Runtime subentry gets no entity object; Home Assistant keeps the registry entry and shows it as not provided.
  - `migration.py`: unchanged unique IDs (`runtime_unique_id`).
  - `README.md` and `ARCHITECTURE.md`: the released statements that Runtime "keeps its Runtime subentry" are updated to the new ownership.
  - Tests: `tests/test_exposure.py`, `tests/test_legacy_migration_placement.py`, `tests/test_runtime.py`, `tests/test_reload_isolation.py` updated; new `tests/test_runtime_entity_ownership.py`.
- **Preconditions:** WP6.
- **Invariants:** unique ID, `entity_id`, user name and icon overrides, `disabled_by`, `hidden_by`, area, and Asset Device are preserved; no Store field is added; ConfigEntry stays version 4; the Runtime writer, unload gate, and checkpoint behavior are unchanged; a second reload produces no Entity Registry update.
- **Deliberately unreachable:** nothing else changes. This commit is reachable on Store 3.1 on purpose.
- **Proof tests (integration):** an installed subentry-owned Runtime entity (with a custom name and `disabled_by=USER` variants) moves to the parent with every identity field preserved; removing the Runtime subentry keeps the registry entry and its `entity_id`; re-adding tracking for the same Home Assistant device reuses the same `entity_id` and continues from the canonical total without inferring elapsed time; rollback on an injected registry failure restores the original placement; a legacy Runtime import (`_legacy_restore_runtime_seconds`) still finds its entity; the unload gate and Runtime checkpoint suites pass.
- **Rollback:** revert. A 0.7.7 build moves the entity back into its Runtime subentry through its own exposure plan, so code rollback before WP9 needs no data repair.
- **Activation impact:** none on the Store; this is the Gate S3 evidence.
- **External gate:** Test HA Runtime identity check (section 5.3) before WP9.

### WP8 — Record the external Test HA gate evidence

- **Commit message:** `docs: record Test HA Store 4.1 source-shape and Runtime identity gates`
- **Goal:** record the owner-run results of section 5 in `docs/test-ha-lab.md` (key names and counts only, no personal values).
- **Preconditions:** WP1 and WP7 on the Test HA branch build; the owner has run sections 5.2 and 5.3.
- **Rule:** if section 5.2 reports `RESULT: INCOMPATIBLE`, this commit records the finding and WP9 is blocked until the provenance is investigated and a reviewed decision exists. Fields are never deleted to pass the gate.
- **Rollback:** documentation only.

### WP9 — Store 4.1 activation

- **Commit message:** `storage: activate Store 4.1`
- **Goal:** the only commit that changes the persisted version from 3.1 to 4.1.
- **Files and symbols:**
  - `storage.py`: `STORAGE_VERSION = 4`, `STORAGE_MINOR_VERSION = 1`; `STORE_TOP_LEVEL_KEYS = STORE_4_1_TOP_LEVEL_KEYS`; the former Store 3.1 validator is renamed `_validate_store_v3_1_data` and is called only by migration; `_validate_store_data` becomes the Store 4.1 validator used by `async_setup`, `_async_recover_uncertain_persistence`, and `_async_mutate_reporting`; `_empty_store_data` returns the 4.1 shape; `_create_asset_in_snapshot` writes `"archived_at": None`.
  - `_async_migrate_func`:

    ```text
    data = deepcopy(old_data)
    (4, 1)                       -> _validate_store_data (4.1); return
    (4, other minor)             -> AssetStoreError (never NotImplementedError)
    (3, 1)                       -> _migrate_v3_1_to_v4_1(data)
    (2, 1) / (1, 1) / (1, 2)     -> existing chain to 3.1 -> _migrate_v3_1_to_v4_1(data)
    anything else                -> AssetStoreError naming 1.1, 1.2, 2.1, 3.1, 4.1
    ```

    `_migrate_v3_1_to_v4_1` runs the 3.1 whole-Store validation, the source preflight, `add_asset_archive_state`, `add_maintenance_collections`, and the 4.1 validation, in that order.
  - `models.py`: `AssetData.archived_at: str | None`; `AssetStoreData` gains `maintenance_schedules` and `maintenance_events`.
  - `tests/conftest.py`: `asset_store_data` becomes 4.1; the 1.1 / 1.2 fixtures stay as migration sources; a 3.1 fixture is added; every test that seeds `hass_storage` states which version it seeds.
  - Reachability tests in `tests/test_maintenance_composition.py` and `tests/test_store_v4_1_validation.py` are updated to the activated state.
  - New `tests/test_store_v4_1_migration.py`.
- **Preconditions:** Gates S1, S2, and S3 closed.
- **Invariants:** the frozen pipeline in [Migration](asset-archive-store-v4.md#migration); no Archive, Maintenance, or Runtime-guard behavior is added here. With no writer of a non-null `archived_at` yet, only a restored or hand-edited Store can contain an archived Asset; WP10–WP13 add the handling before anything is released.
- **Home Assistant `Store` interaction:** Home Assistant calls `_async_migrate_func` only when the stored version differs from the constants, then saves the result through `DeviceLifecycleStore.async_save`, which forces the write and verifies the exact envelope by direct readback. Any exception propagates out of `async_load`, so `AssetStoreManager.async_setup` never publishes an unverified 4.1 payload. See section 7 for the failure cases.
- **Proof tests (integration):** migration from 1.1, 1.2, 2.1, and 3.1 to the exact 4.1 payload, verified from the raw file; an unexpected or missing Asset or Purchase key fails setup and leaves the file byte-identical; an unreadable readback during migration fails setup without publishing, and the next setup succeeds from either the old 3.1 file or the new 4.1 file; a mismatching readback fails the same way; 4.0 and 4.2 are rejected with `AssetStoreError`; 5.x raises `UnsupportedStorageVersionError`; a simulated 0.7.x reader (`Store` with version 3) refuses the 4.1 file and leaves it unchanged; a new installation writes 4.1; new Assets have `archived_at = None`; an archived + deployed 4.1 payload fails setup; a future `archived_at` loads.
- **Rollback:** revert only before any installation has run it. After a 4.1 file exists, older code refuses it; restore the pre-upgrade backup (section 8).
- **Activation impact:** Store 4.1 is live; no user-visible Archive or Maintenance feature yet.

### WP10 — Identity versus management helpers and current-management guards

- **Commit message:** `storage: block current-management mutations of archived Assets`
- **Goal:** the named invariant IDENTITY/RECONCILIATION LOOKUP → all Assets, CURRENT-MANAGEMENT CANDIDATES → active Assets only, enforced at the authoritative mutation boundary.
- **Files and symbols:**
  - `storage.py`:
    - `AssetStoreManager.active_assets()` and `archived_assets()`: the only filtering helpers for current management.
    - `AssetStoreManager._require_active_asset(data, asset_uuid) -> AssetData`, raising `AssetStoreError(code="asset_archived")`, called inside the mutators of `async_update_asset_metadata_reporting`, `async_set_asset_purchase_reporting`, `async_set_asset_deployment_reporting`, `async_link_asset_device_reporting`, `async_unlink_asset_device_reporting`, `async_add_related_device_reporting`, `async_remove_related_device`, `async_set_asset_lifecycle_reporting`, `async_initialize_new_runtime`, `async_import_legacy_runtime`, and `async_commit_runtime_delta`.
    - `_create_replacement_record` requires both Assets active, which covers `async_create_asset_replacement`, the new record of `async_correct_asset_replacement`, and a Quick Create replacement predecessor.
    - `async_void_asset_replacement` has no archive guard.
    - `asset`, `assets`, `asset_for_primary_device_id`, `_find_asset_by_primary_device`, and `runtime_identity` stay archive-blind.
  - `config_flow.py`: `_asset_choices`, `_replacement_target_choices`, and `_quick_replacement_choices` use `active_assets()`; `_quick_asset_display_label` keeps counting all Assets for unambiguous labels; the `asset_for_primary_device_id` checks in Quick Add and primary-device management stay archive-blind, so an archived primary device is never "free". `asset_archived` errors get EN/FI copy.
  - A test inventory lists every `assets()`, `active_assets()`, `archived_assets()`, and `asset_for_primary_device_id()` call in production code with its classification (identity or management), so a new call site cannot appear unclassified. No `archived_at` comparison appears outside `archive.py` and the two manager helpers.
  - Stale reference Repairs: `async_sync_stale_reference_issues` collects references from `active_assets()`, which deletes an archived Asset's issues through the existing owned-issue cleanup and recreates them after Restore on the next sync.
  - Reconciliation: no archive guard. Purchase-owned projection, `_refresh_home_assistant_metadata`, and `_ensure_primary_reference` keep following their sources on archived Assets.
- **Preconditions:** WP9.
- **Proof tests:** each blocked mutation on an archived Asset fails with the Store unchanged and no save (unit on mutators plus integration through the manager); Replacement void of a record with an archived side succeeds; Replacement create and correct with an archived side fail; Quick Create with an archived predecessor fails at commit even when the selector was stale (TOCTOU: the Asset is archived between selector render and submit); selectors omit archived Assets; Quick Add from Home Assistant rejects a device whose primary owner is archived; reconciliation refreshes Home Assistant-owned and Purchase-owned fields of an archived Asset, leaves user-owned fields untouched, finds the archived owner of a Purchase device, and creates no duplicate Asset; stale reference issues disappear for an archived Asset and reappear after its `archived_at` returns to `None`.
- **Rollback:** revert to WP9 (the Store is unchanged by this commit).
- **Activation impact:** guards in place before any API can archive an Asset.

### WP11 — Ambiguous-persistence resolution and the Maintenance manager API

- **Commit message:** `storage: add Maintenance manager API with replay recovery`
- **Goal:** Maintenance becomes writable through the verified persistence pipeline, and ambiguous writes resolve immediately from the persisted snapshot.
- **Files and symbols (`storage.py`):**
  - `_async_mutate_reporting(mutator, *, resolve_ambiguous=None)`: when `async_save` raises `AssetStorePersistenceError(ambiguous=True)` and a resolver is given, still holding `_mutation_lock` it reads `async_load_persisted_snapshot`, validates it, and calls `resolve_ambiguous(persisted)`. A non-`None` result publishes the persisted snapshot as `_data`, clears `_persistence_uncertain`, and is returned. A `None` result also publishes the successfully read persisted snapshot and clears the flag, then raises a non-ambiguous `AssetStorePersistenceError`, because the outcome is now known. If the persisted snapshot cannot be read or validated, the flag stays set and the original ambiguous error is raised. The existing callers pass nothing and are unchanged.
  - `AssetStoreManager.async_mutate_maintenance(request) -> MaintenanceMutationResult`: inside the mutator, build `MaintenanceSnapshot(data["assets"], data["maintenance_schedules"], data["maintenance_events"])` and `MaintenanceMutationContext(today=dt_util.now().date(), observed_utc=utc_now_iso(), current_runtime=data["assets"][uuid]["runtime"]["total_seconds"])` from the same locked candidate, call `mutate_maintenance`, and write back only the two Maintenance collections when `CHANGED`. The ambiguous resolver re-runs the same request on the persisted snapshot: `REPLAY` or `NO_OP` means the write landed and is reported as success; `CHANGED` or an error means it did not land, and the caller receives a definite failure against the now-known persisted state. Every Maintenance create request carries its pre-generated UUID, so a user retry replays.
  - Read helpers: `maintenance_schedules_for_asset(asset_uuid)`, `maintenance_events_for_asset(asset_uuid)`, `maintenance_projection(schedule_uuid)` (calls `project_schedule` with `asset_archived=asset_is_archived(asset)` and the canonical Runtime).
  - Maintenance errors map to `AssetStoreError` codes the flows can translate.
  - New `tests/test_maintenance_persistence.py`.
- **Invariants:** "Just now" is `await manager.async_checkpoint_runtime(asset_uuid)` (existing, takes `_runtime_lock` then `_mutation_lock` per delta) → the flow shows the value → `await manager.async_mutate_maintenance(...)`. `async_mutate_maintenance` never awaits a Runtime checkpoint and is never called while a Runtime lock is held. No Maintenance operation writes Asset Runtime.
- **Proof tests:** success; save failure (non-ambiguous) leaves `_data` unchanged; readback mismatch; unreadable readback resolved as REPLAY when the write landed and raised when it did not; a corrupted file during recovery fails closed; no publish before verified readback; `current_runtime` is read from the locked candidate (a concurrent Runtime commit queued behind the lock is not seen half-way); the lock-order test proves `_mutation_lock` is free while a checkpoint runs; archived-Asset guard reachable through the API.
- **Rollback:** revert to WP10.
- **Activation impact:** Maintenance data writable programmatically; no entity or UI.

### WP12 — Archived Runtime subentry quarantine and conflict Repair

- **Commit message:** `runtime: quarantine Runtime tracking of archived Assets at setup`
- **Goal:** the frozen [Setup conflict: quarantine](asset-archive-store-v4.md#setup-conflict-quarantine).
- **Files and symbols:**
  - `storage.py`: `AssetStoreManager.quarantined_runtime_subentries(entry) -> frozenset[str]`, which resolves every Runtime subentry with `resolve_runtime_subentry_asset` over all Assets and returns those whose target is archived. `_reconcile_entry_data` and `async_reconcile_entry` take `quarantined` and skip those subentries entirely: no `_ensure_primary_reference`, no Home Assistant metadata refresh through the Runtime path, no new Asset, no subentry rewrite.
  - `__init__.py`: compute the quarantine set after `async_setup` and before `async_reconcile_entry`, pass it to reconciliation, exposure, and the platform (stored on the manager for this setup), then sync the conflict issues.
  - `exposure.py`: `_runtime_subentries_by_asset` ignores quarantined subentries.
  - `sensor.py`: no writer, no Runtime initialization or legacy import, no listener, and no checkpoint loop for a quarantined subentry; the parent-owned Runtime registry entry is left untouched.
  - New `runtime_conflicts.py`: issue ID `runtime_archived_asset_<config_subentry_id>` (a pattern that never matches the stale-reference pattern); `async_sync_runtime_conflict_issues(hass, manager, quarantined)` creates the desired issues (not fixable; translation placeholders are the Asset name and ID) and deletes every owned conflict issue not desired; `async_delete_runtime_conflict_issues` is called from `async_remove_entry`. Unloading keeps the issues, like stale-reference issues.
  - Translations (EN/FI) for the issue.
  - New `tests/test_runtime_quarantine.py`.
- **Invariants:** setup never fails the whole entry for this conflict and never schedules a reload. The frozen Repair exits are Restore, or removal or reconfiguration of the Runtime subentry. A reconfiguration resolves the conflict only when the subentry no longer resolves to the archived Asset. The current reconfigure flow cannot change the device, so a reconfiguration that keeps the archived target is a rebind to an archived Asset and is rejected (WP13); with today's flow the subentry-side exit is removal. The user's Ignore state is never consulted.
- **Proof tests (integration):** a restored backup with an archived Asset and a resolving Runtime subentry (by `asset_uuid` and legacy by device only) loads; the subentry data is byte-identical; no Runtime writer registers; the Runtime total and the Store file are unchanged; exactly one issue exists after three reloads and a restart; other Assets and their Runtime work; restoring the Asset (Store edited in the test) plus a reload starts Runtime and deletes the issue; removing the subentry deletes the issue; removing the entry deletes it.
- **Rollback:** revert to WP11.
- **Activation impact:** safe setup for any archived state before any API creates one.

### WP13 — Archive and Restore manager API and Runtime serialization

- **Commit message:** `archive: add Archive and Restore with Runtime serialization`
- **Goal:** the authoritative cross-system Archive boundary and the Runtime create/rebind guard, reviewed together because they are one protocol.
- **Files and symbols:**
  - `storage.py`:
    - `AssetStoreManager.async_archive_asset(entry, asset_uuid) -> ArchiveOutcome`
    - `AssetStoreManager.async_restore_asset(asset_uuid) -> ArchiveOutcome`
    - `AssetStoreManager.async_reserve_runtime_binding(entry, asset_uuid) -> Callable[[], None]`
    - `AssetStoreManager.archive_blockers(entry, asset_uuid) -> tuple[str, ...]`, a read-only preview for the user interface that is never authoritative
    - Runtime binding reservations are kept per ConfigEntry in `hass.data` under a `HassKey` (in memory only), so they survive a manager replacement by reload.
    - `RuntimeWriter` gains three capabilities next to `checkpoint` and `prepare_unload`: `durability`, a synchronous probe returning `RuntimeWriterDurability(observing: bool, pending: bool, committed_seconds: Decimal)`; `finalize`, a strict flush of already-pending deltas that never seals new time; and `retire`, described below. `register_runtime_checkpoint` takes them as keyword arguments and rejects registration for an archived Asset.
    - `AssetStoreManager._runtime_archive_eligibility(data, asset_uuid) -> RuntimeArchiveEligibility`, a manager-private synchronous helper (see [Runtime Archive eligibility](#runtime-archive-eligibility)).
    - `AssetStoreManager.async_finalize_runtime(asset_uuid)`: calls the writer's `finalize`; never holds `_mutation_lock` around it; raises and keeps every pending delta on failure.
    - `AssetStoreManager.async_retire_orphaned_runtime_writers(entry)`: for every registered writer whose Asset no longer has a resolving Runtime subentry (`runtime_subentries_resolving_to`), calls the writer's `retire`. Failures are logged and leave the writer quiesced with its pending deltas; they never block the reload that follows.
  - `sensor.py` (`DeviceRuntimeHoursSensor`), all under the existing `_runtime_lock`:
    - `retire`: seals observed time through now, stops observing (sets `_removing`, removes the source listener), then attempts a strict flush. Unlike `async_prepare_runtime_unload`, it quiesces even when the flush fails, because the tracking configuration is gone.
    - `finalize`: strict flush of the pending deltas only.
    - The periodic interval retries a quiesced writer's pending deltas best effort without sealing.
    - `async_prepare_runtime_unload` on an already quiesced writer strictly flushes any remaining pending deltas instead of returning immediately, so a quiesced writer with pending Runtime still blocks the unload gate.
    - No path adds time observed after the quiesce; pending deltas are never discarded.
  - `__init__.py`: `_async_update_listener` awaits `async_retire_orphaned_runtime_writers(entry)` before it schedules the reload.
  - `config_flow.py`: `RuntimeSubentryFlow` resolves the selected device to its canonical Asset through `resolve_runtime_subentry_asset` over all Assets (early UX rejection of an archived owner in `async_step_user`). In `async_step_runtime_source` it takes the reservation immediately before returning `async_create_entry`, with no other `await` in between, and releases it in `async_remove()`. `async_step_reconfigure` rejects a subentry whose target is archived. The flow aborts with `entry_not_loaded` when the parent has no loaded manager.
  - Translations for `runtime_asset_archived`, `archive_runtime_configured`, `archive_runtime_binding_in_progress`, `archive_runtime_writer_active`, `archive_runtime_undurable`, `archive_runtime_unresolved`, and `archive_asset_deployed`.
  - New `tests/test_archive_manager.py` and `tests/test_archive_runtime_race.py`.
- **Archive sequence (all inside `_async_mutate_reporting`, holding `_mutation_lock`):**

  ```text
  mutator(data):                       # synchronous; no await between the checks and the change
    Asset exists
    already archived                   -> NO_OP (no save)
    deployment_state != deployed
    runtime_subentries_resolving_to(entry.subentries, data["assets"], uuid) == ()
    no Runtime binding reservation for (entry_id, uuid)
    _runtime_archive_eligibility(data, uuid) in {ABSENT, QUIESCED_DURABLE}
    apply_archive_request(..., observed_utc=utc_now_iso())
  -> 4.1 validation -> save -> direct readback -> publish
  ambiguous save -> resolve_ambiguous: persisted archived_at != null -> success
  ```

<a id="runtime-archive-eligibility"></a>
- **Runtime Archive eligibility.** The frozen condition is "no surviving Runtime writer for this Asset holds undurable Runtime state". A missing Runtime ConfigSubentry does not by itself prove that Runtime is durable, and a registered writer does not by itself prove that it is not. Archive therefore needs both: no resolving Runtime subentry and no binding reservation (configuration relationship absent), and a durability-safe writer state. `_runtime_archive_eligibility` derives that state synchronously from in-memory evidence only, in this order:

  | State | Evidence | Archive |
  |---|---|---|
  | `UNRESOLVED` | `asset_uuid in _runtime_unresolved` (a removed writer left pending Runtime) | blocked: `archive_runtime_unresolved` |
  | `ABSENT` | no registered writer | writer condition passes |
  | `ACTIVE` | `durability().observing` | blocked: `archive_runtime_writer_active` |
  | `UNDURABLE` | not observing, and `durability().pending`, or `committed_seconds` differs from the canonical total in the locked candidate | blocked: `archive_runtime_undurable` |
  | `QUIESCED_DURABLE` | not observing, no pending delta, and `committed_seconds` equals the canonical total in the locked candidate | writer condition passes |

  - A quiesced writer alone is never sufficient; `QUIESCED_DURABLE` requires the completed strict flush (empty pending) and the verified canonical total.
  - `_runtime_unresolved` is never cleared to enable Archive. It ends only with the manager, as today.
  - No Runtime metadata is persisted, and no elapsed time is inferred.
  - A successful parent reload is not required: a writer that quiesced after its Runtime subentry was removed and later finalized durably stays registered and is `QUIESCED_DURABLE`.
  - The deployment and Runtime-subentry preconditions still apply.

- **Runtime create/rebind sequence:**

  ```text
  async_reserve_runtime_binding(entry, uuid):
    async with _mutation_lock:          # waits for any in-flight Archive or Restore to publish
      recover uncertain persistence
      entry.runtime_data is self        # a replaced manager fails closed
      Asset exists and is not archived
      add reservation (entry_id, uuid)
  flow returns CREATE_ENTRY -> async_add_subentry -> flow.async_remove() releases the reservation
  ```

- **Why the forbidden combination cannot occur:** Archive and the reservation both run under the same `_mutation_lock`, and Archive's checks are synchronous with its change, so they are totally ordered.
  - If Archive publishes first, the reservation sees `archived_at != null` and fails.
  - If the reservation comes first, it exists from before the Archive check until after the subentry has been added. Archive then sees either the reservation or the subentry, and fails.
  - A reload between reservation and subentry creation cannot hide the reservation, because it lives in `hass.data` per entry. A flow abandoned without finishing releases the reservation in `async_remove()`.
  - Setup quarantine (WP12) remains the backstop for backups and older configuration.
- **Lock ordering and race safety:** `_runtime_lock` → `_mutation_lock` stays the only nested order.
  - Archive never acquires `_runtime_lock` and never awaits a writer. Its mutator reads `durability()` synchronously while holding `_mutation_lock`, in the same synchronous segment as the change, so the observation is authoritative for that commit.
  - Making Runtime durable happens before Archive and outside `_mutation_lock`: the Archive flow (WP15) awaits `async_finalize_runtime(asset_uuid)` (`_runtime_lock` → `_mutation_lock` per delta, as in every Runtime commit) and only then calls `async_archive_asset`.
  - The observation cannot become unsafe after it is taken:
    - A quiesced writer can no longer create Runtime, because every Runtime-creating path already rechecks `_removing` under `_runtime_lock`. `finalize` and `prepare_unload` only reduce pending.
    - A pending delta whose commit is waiting for `_mutation_lock` is still in `pending` and makes the state `UNDURABLE`.
    - `committed_seconds` changes only after a commit has been published, which cannot interleave with the locked mutator.
    - A new writer can only register at platform setup for a resolving Runtime subentry, which Archive has just proven absent; subentry creation goes through the binding reservation. After Archive publishes, `register_runtime_checkpoint` refuses an archived Asset and `async_commit_runtime_delta` refuses it too (WP10), so no Runtime can be written for it.
- **Runtime subentry removal without a required reload:** removing the Runtime subentry runs `async_retire_orphaned_runtime_writers` from the update listener, so the writer stops observing at once, whether or not its strict flush succeeds, and the reload is scheduled. If the flush or the reload's unload gate fails, the entry stays loaded, and the writer stays registered, quiesced, and `UNDURABLE` with its pending deltas. Durability later becomes provable by `finalize`, either the periodic best-effort retry or the Archive flow's explicit call. The writer is then `QUIESCED_DURABLE`, and Archive succeeds without any parent reload.
- **Restore:** `apply_archive_request` with `RestoreAssetRequest`, no Runtime check, no subentry recreation, no Home Assistant device or reference repair; ambiguous save resolved by `archived_at is None` in the persisted snapshot.
- **Proof tests:** unit and integration for every precondition, NO_OP without a save, ambiguous success for both operations, and ambiguous non-matching state raising. Deterministic interleavings with a blocking `async_save`:
  - Archive in flight vs Runtime create: the reservation waits for the lock and fails.
  - Reservation held vs Archive: Archive fails with `archive_runtime_binding_in_progress`.
  - Subentry added vs Archive: `archive_runtime_configured`.
  - Flow abandoned: Archive succeeds.
  - Reload between reservation and add: Archive still fails.
  - Rebind by primary-device change: the device of an archived owner cannot be claimed.
  - Reconfigure of a quarantined subentry is rejected.
  - A legacy subentry resolving by device blocks Archive.
  - Active writer: a registered, observing writer → Archive rejected with `archive_runtime_writer_active`.
  - Pending writer: a quiesced, non-observing writer with a pending delta → Archive rejected with `archive_runtime_undurable`.
  - Unresolved: `_runtime_unresolved` contains the Asset → Archive rejected with `archive_runtime_unresolved`; `async_finalize_runtime` does not clear it.
  - Durable quiesced writer: Runtime subentry absent, writer still registered, quiesced, strict flush completed, pending empty, `committed_seconds` equal to the canonical total → Archive succeeds, and no parent reload is called (asserted with `capture_reloads`).
  - Failed checkpoint then recovery (fake monotonic clock):
    - Canonical total 1000; the source is active from t=50.
    - The Runtime subentry is removed and `retire` runs at t=60 with the commit failing: 10 seconds stay pending and the writer is quiesced. Archive is rejected (`archive_runtime_undurable`). The reload's unload gate also fails and the entry stays loaded.
    - At t=100, with the source still on, `async_finalize_runtime` succeeds: the canonical total is 1010, not 1050, and pending is empty.
    - The writer is still registered and quiesced, and Archive succeeds without a reload.
  - Committed-total mismatch: a quiesced writer with empty pending whose `committed_seconds` differs from the canonical total → `archive_runtime_undurable`.
  - Race: a writer's delta commit queued behind an Archive that holds `_mutation_lock` → Archive is rejected, because the delta is still pending. After an Archive has published, `register_runtime_checkpoint` and `async_commit_runtime_delta` for that Asset fail closed. No interleaving ends with `archived_at != null` and a pending or unresolved Runtime for the Asset.
  - Lock order: `async_archive_asset` never acquires `_runtime_lock` (asserted by instrumenting the lock). `async_finalize_runtime` is never awaited while `_mutation_lock` is held.
  - Unload gate: a quiesced writer with pending deltas still makes `async_prepare_runtime_unload` fail. When the flush succeeds, the unload proceeds.
  - After Restore, a new Runtime subentry for the same device resumes from the canonical total.

  The ordering assumption about `async_add_subentry` before `flow.async_remove()` is asserted by a test on both supported Home Assistant versions.
- **Rollback:** revert to WP12.
- **Activation impact:** Archive and Restore reachable programmatically; not yet in any user interface.

### WP14 — Store publish refresh without reload

- **Commit message:** `refresh: update entities and Repairs after Store commits`
- **Goal:** the frozen [Refresh without reload](asset-archive-store-v4.md#refresh-without-reload).
- **Files and symbols:**
  - `storage.py`: `AssetStoreManager.async_add_publish_listener(listener: Callable[[frozenset[str]], None]) -> Callable[[], None]`. After every publish of a changed snapshot, each listener is called synchronously with the Asset UUIDs whose Asset record changed or whose Maintenance records changed. A listener exception is logged and never affects the commit or other listeners. This is the "manager publish listener" the Maintenance plan planned; there is no generic event bus or coordinator.
  - `sensor.py`: a small `_AssetSnapshotEntity` mixin for the Asset entities. It subscribes in `async_added_to_hass`, and for its own UUID re-reads `manager.asset(uuid)` (and the related Purchase, Lifecycle event, and Replacement snapshots it renders) before `async_write_ha_state()`. `DeviceRuntimeHoursSensor` re-reads its Asset snapshot the same way; its Runtime accounting is unchanged.
  - `DeviceAssetIdSensor` gains the attribute `archived: true | false`. States of informational sensors do not change and never become `unavailable` because of Archive.
  - `__init__.py`: a publish listener runs `async_sync_stale_reference_issues`.
  - Existing mutation flows keep their reload behavior. Archive and Restore, and later Maintenance mutations, rely on the listener.
  - New `tests/test_publish_refresh.py`.
- **Invariants:** the Store commit is authoritative; a failed refresh never rolls it back; no parent reload for Archive or Restore; entity and device registries are not written by Archive or Restore (no `disabled_by`, no Device Registry update).
- **Proof tests (integration):**
  - Archive and Restore update the Asset ID attribute and every Asset entity snapshot without an `async_reload` call (reloads are captured with the existing `capture_reloads` fixture).
  - A listener that raises does not undo the commit.
  - Stale reference issues disappear on Archive and reappear on Restore within the same loaded entry, with the same deterministic ID.
  - Entity Registry and Device Registry entries are unchanged across Archive and Restore, including user-disabled entities.
  - The OptionsFlow's next step reads the new state.
- **Rollback:** revert to WP13.

### WP15 — Archive and Restore user interface

- **Commit message:** `config_flow: add Archive and Restore to Asset management`
- **Goal:** Home Assistant-native Archive and Restore in the existing OptionsFlow.
- **Files and symbols:** `config_flow.py`:
  - An Archive action in the Asset hub opens `confirm_archive_asset`. It previews the consequences from `archive_blockers`: it lists a `deployed` Deployment and Runtime tracking (configured, being set up, still observing, not yet durable, or unresolved) as blockers with the steps that resolve them. Before submitting, it awaits `async_finalize_runtime` for a quiesced writer with pending Runtime (WP13), which never guesses or discards Runtime. It never undoes Deployment or removes Runtime itself, and it records no history.
  - The init menu gains an archived-Assets entry opening a selector over `archived_assets()` and a restricted archived-Asset view: facts, Restore (`confirm_restore_asset`), and the existing Replacement void. Maintenance history correction is added to this view in WP17.
  - Restore explains that the identity is unchanged, that stale device references may show in Repairs, that Runtime stays off until configured, and that Maintenance may be overdue at once.
  - Both finish with `_finish_asset_action(..., reload=False)` and rely on WP14.
  - Translations EN/FI; the form-exit, submit-label, and step-classification inventories extended.
  - New `tests/test_archive_options_flow.py`.
- **Invariants:** the Archive user interface uses labels distinct from the dashboard's Lifecycle "Archived"/"Arkistoidut" group, enforced by a translation test. No new ConfigEntry, subentry type, or dashboard change.
- **Owner decision resolved in this work package's review:** the exact EN/FI wording of the Archive labels, within the distinct-label rule.
- **Proof tests (integration):** every step and exit, NOT_SELECTED handling, blocker rendering, a blocker appearing between preview and confirm (the manager's error is shown, nothing changes), NO_OP messages, no reload, and archived Assets absent from every current-management selector.
- **Rollback:** revert to WP14.

### WP16 — Maintenance entities

- **Commit message:** `maintenance: add Maintenance entities`
- **Goal:** the entities of [Maintenance implementation plan section 9](maintenance-implementation-plan.md#9-home-assistant-surfaces-classes-before-activation-wiring-at-activation), registered with 4.1 live.
- **Files and symbols:**
  - `sensor.py`: Maintenance status and next-maintenance sensors.
  - New `binary_sensor.py`: the preparation sensor.
  - `__init__.py`: `PLATFORMS` gains `Platform.BINARY_SENSOR`.
  - Unique IDs `<schedule_uuid>_maintenance_status`, `<schedule_uuid>_maintenance_due_date`, `<schedule_uuid>_maintenance_preparation`. Entities sit on the Asset Device and are parent-owned (`config_subentry_id = None`).
  - Refresh comes from the publish listener, a local-midnight `async_track_time_change`, and Runtime commits (which already publish).
  - While the Asset is archived: `available = False`. Restore recomputes immediately.
  - `DISABLED` presentation follows the owner decision below. UNKNOWN stays a real state and is never merged with unavailable. Hard-deleted Schedules' entities are removed at setup; nothing else removes or recreates Maintenance entities, and `disabled_by` is never written.
- **Owner decisions resolved before this work package starts** (carried over from the Maintenance plan's Gate 4): whether the status sensor uses the `disabled` presentation value, and whether the preparation `binary_sensor` ships in 0.8.0.
- **Proof tests (integration):** identity and placement; archived → unavailable, never `off`; Restore → immediate recomputation including `OVERDUE`; UNKNOWN distinct from unavailable; midnight refresh; Runtime commit refresh; exposure preflight unaffected by the `_maintenance_*` suffixes; reload continuity.
- **Rollback:** revert to WP15.

### WP17 — Maintenance user interface

- **Commit message:** `config_flow: add Maintenance management`
- **Goal:** the Maintenance OptionsFlow of the Maintenance plan section 9: schedule management, History with the date-range filter and full actionability, Mark done with "Just now" (checkpoint first, then mutation), Correct and Void (also in the archived-Asset view), the three guards, and the destroy confirmation.
- **Files:** `config_flow.py`, translations, `tests/test_maintenance_options_flow.py`.
- **Invariants:** no frozen semantics re-planned; Maintenance mutations use `async_mutate_maintenance` and the publish refresh (no parent reload); an archived Asset offers only History with Correct and Void.
- **Proof tests:** the Maintenance plan's options-flow matrix plus archived-Asset restrictions.
- **Rollback:** revert to WP16.

### WP18 — Release documentation

- **Commit message:** `docs: prepare Store 4.1 release documentation`
- **Goal:** README (features, upgrade and backup warning, downgrade statement), ARCHITECTURE (implemented state replacing "not implemented"), upgrade notes, the Test HA release validation checklist and its results (Gate S7).
- **Rollback:** documentation only.

### Deviations from the suggested conceptual order

| Suggested | This plan | Reason |
|---|---|---|
| 1. Store 4.1 shape/validation | WP1–WP3 | Split so the source preflight (WP1) exists early enough for the owner to run the Test HA inspection long before activation, and so the behavior-preserving validator split (WP3) is reviewed on its own. |
| 3. Archived-aware Maintenance | WP5 | Unchanged position. |
| 4. Runtime identity hardening | WP6 + WP7 | The shared resolver is extracted first because WP7's tests, WP12, and WP13 all depend on exactly one resolution rule. |
| 7. Manager APIs | WP10, WP11, WP13 | Current-management guards (WP10) come first so no branch state has an archived Asset without guards. The ambiguous-resolution hook is introduced with its first user (Maintenance, WP11) and reused by Archive (WP13). |
| 8. Runtime/Archive guards + quarantine | WP12 then WP13 | Quarantine is the backstop and needs no Archive API; the Runtime flow guard ships with the Archive API because the reservation protocol has two halves that must be reviewed together. |
| 11. Maintenance HA integration | WP16 + WP17 | Entities and flows are split for reviewability. The Maintenance plan's pre-activation "unregistered classes" commit is dropped: with Store 4.1 live before the user interface, unregistered classes would be dead code with no added safety. |

## 4. Gate model

| Gate | Closed by | Evidence |
|---|---|---|
| **S1 — migration-source safety** | WP1, WP3, WP8 | `tests/test_store_shape.py` and the migration-step tests green on both Home Assistant versions; the section 5.2 inspection of the real Test HA Store printed `RESULT: COMPATIBLE` with the source unchanged (equal SHA-256 before and after), recorded in WP8. |
| **S2 — pure Store 4.1 model** | WP1–WP6 | Validator, transforms (including commutation), Archive mutations, and archived-aware Maintenance tests green; the AST and constant tests prove production is still Store 3.1 and nothing reaches the new code. |
| **S3 — Runtime identity continuity** | WP7, WP8 | Integration tests of WP7 green; the section 5.3 Test HA check shows every Runtime entity with unchanged `entity_id` and unique ID, `config_subentry_id` null, continuous history, and the registry entry surviving removal and re-adding of a Runtime subentry. |
| **S4 — Store 4.1 activation** | WP9 | S1–S3 closed before the commit; the WP9 migration matrix green on both Home Assistant versions; the refusal by a simulated 3.1 reader proven. |
| **S5 — production mutation persistence** | WP10, WP11, WP13 | Guard matrix; Maintenance and Archive/Restore persistence including ambiguous resolution, no publish before verified readback, and NO_OP without a save. |
| **S6 — Runtime/Archive HA safety** | WP12, WP13 | Quarantine across repeated reloads and a restart; the deterministic race matrix; the flow-manager ordering assertion on both Home Assistant versions. |
| **S7 — Home Assistant surface and release readiness** | WP14–WP18 | Refresh without reload; Archive/Restore and Maintenance flows and entities; full suite and coverage on both Home Assistant versions; Test HA upgrade of the real Store from 3.1 to 4.1 (section 5.4) and a smoke of Archive, Restore, Runtime removal and re-add, Maintenance create, mark done, correct, void, and due transitions; release notes with the backup and downgrade warning. |

## 5. External Test HA gates

These gates use the real Test HA installation (`ha-ai-lab`, see [Persistent Test HA lab](test-ha-lab.md)). Repository fixtures cannot prove the shape of that Store; only the owner can run these steps and report the output.

### 5.1 When they are mandatory

| Check | Mandatory before |
|---|---|
| 5.2 Store source shape | WP9 is committed (it can and should be run as soon as WP1 exists) |
| 5.3 Runtime identity continuity | WP9 is committed (it requires a branch build containing WP7) |
| 5.4 Store 4.1 upgrade | the branch is merged or released (Gate S7) |

### 5.2 Store 3.1 source-shape inspection (read-only)

Run in any shell that can read `/config` and has Python 3.8 or later, for example the Home Assistant container (`docker exec -it homeassistant sh`) or an SSH add-on that includes Python. If no such shell exists, download `/config/.storage/device_lifecycle.assets` through a backup or Samba and run the same script on a workstation against the downloaded copy. The script reads only a copy, uses the standard library only, and prints key names, counts, and Asset IDs, never field values.

```sh
SRC=/config/.storage/device_lifecycle.assets
COPY=/tmp/device_lifecycle.assets.inspect.json
sha256sum "$SRC"
cp "$SRC" "$COPY"
sha256sum "$COPY"

python3 - "$COPY" <<'PY'
import json
import sys

EXPECTED_TOP = {
    "next_asset_number", "purchases", "assets",
    "lifecycle_events", "replacement_records",
}
EXPECTED_ASSET = {
    "asset_uuid", "asset_id", "name", "category", "purchase_uuid",
    "deployment_state", "installed_date", "ha_area_id", "warranty",
    "runtime", "lifecycle", "manufacturer", "model", "model_id",
    "serial_number", "sw_version", "hw_version", "notes",
    "field_sources", "ha_device_refs",
}
EXPECTED_PURCHASE = {
    "purchase_uuid", "config_subentry_id", "configured", "name",
    "purchase_date", "seller", "total_price", "currency",
    "receipt_reference", "receipt_url", "notes", "asset_uuids",
}

with open(sys.argv[1], encoding="utf-8") as handle:
    envelope = json.load(handle)

problems = 0
print("envelope keys:", sorted(envelope))
print("key:", envelope.get("key"))
print("version:", envelope.get("version"), "minor_version:", envelope.get("minor_version"))
if (envelope.get("key"), envelope.get("version"), envelope.get("minor_version")) != (
    "device_lifecycle.assets", 3, 1,
):
    print("PROBLEM: envelope is not device_lifecycle.assets Store 3.1")
    problems += 1

data = envelope.get("data")
if not isinstance(data, dict):
    print("PROBLEM: data is not a mapping")
    sys.exit(1)

top = set(data)
print("top-level keys:", sorted(top))
print("  missing:", sorted(EXPECTED_TOP - top), "unexpected:", sorted(top - EXPECTED_TOP))
problems += bool(top ^ EXPECTED_TOP)


def inspect_records(kind, records, expected, label):
    count = 0
    if not isinstance(records, dict):
        print(f"PROBLEM: {kind} is not a mapping")
        return 1
    print(f"\n{kind}: {len(records)} records")
    union = set()
    for record in records.values():
        if isinstance(record, dict):
            union.update(record)
    for key in sorted(union | expected):
        present = sum(1 for r in records.values() if isinstance(r, dict) and key in r)
        status = "expected" if key in expected else "UNEXPECTED"
        print(f"  {key:<20} {present}/{len(records)}  {status}")
    for record_key, record in sorted(records.items()):
        if not isinstance(record, dict):
            print(f"  PROBLEM: {kind} record {str(record_key)[:8]} is not a mapping")
            count += 1
            continue
        keys = set(record)
        missing = sorted(expected - keys)
        unexpected = sorted(keys - expected)
        if missing or unexpected:
            print(f"  PROBLEM: {label(record_key, record)} missing={missing} unexpected={unexpected}")
            count += 1
    return count


problems += inspect_records(
    "assets", data.get("assets"), EXPECTED_ASSET,
    lambda k, r: f"Asset {r.get('asset_id', '?')} ({str(k)[:8]})",
)
problems += inspect_records(
    "purchases", data.get("purchases"), EXPECTED_PURCHASE,
    lambda k, r: f"Purchase {str(k)[:8]}",
)
print("\nRESULT:", "COMPATIBLE" if problems == 0 else f"INCOMPATIBLE ({problems} problem(s))")
sys.exit(0 if problems == 0 else 1)
PY

sha256sum "$SRC"
rm "$COPY"
```

Report the complete output. The two `sha256sum "$SRC"` lines must be identical. `RESULT: INCOMPATIBLE` stops activation: the provenance of every unexpected or missing key is investigated first, and no field is deleted to pass the gate.

### 5.3 Runtime identity continuity (branch build with WP7, Store still 3.1)

Take a Home Assistant backup first. Before installing the build, record each Device Lifecycle Runtime entity's `entity_id`, unique ID, name override, and `disabled_by` from **Settings → Entities** (filter the integration), or with the same read-only copy approach on `/config/.storage/core.entity_registry`. After installing and restarting:

1. Every Runtime entity keeps its `entity_id` and unique ID and now has no ConfigSubentry (`config_subentry_id` null in a copy of `core.entity_registry`).
2. Its history continues in the history view across the restart.
3. Remove the Runtime tracking of the lab's power-based Runtime Asset (DL0002), reload, and confirm that the entity still exists in the Entity Registry, reported as not provided.
4. Add Runtime tracking for the same device again and confirm the same `entity_id` and a Runtime value that continues from the previous total.

### 5.4 Store 4.1 upgrade (branch build with WP9 or later)

Take a Home Assistant backup. Run 5.2 once more (it must still pass), install the build, restart, and confirm:

- the Store file reports version 4, minor 1
- every Asset has `archived_at: null`, and both Maintenance collections are empty
- all 8 lab Assets, Purchases, Runtime totals, Lifecycle, and Replacement records are unchanged
- no Device Lifecycle Repairs issue appears

Then run the S7 smoke.

## 6. Store activation boundary

The persisted version changes from 3.1 to 4.1 in **WP9 (`storage: activate Store 4.1`)** and in no other commit. Before WP9 every commit can be reverted as ordinary code. From WP9 on, an installation that has started once has a 4.1 file that older code refuses.

## 7. Migration and persistence failure behavior

| Failure | Behavior |
|---|---|
| Unsupported older version, or a wrong minor of 3 or 4 | `AssetStoreError` from `_async_migrate_func`; setup fails; the file is not written. |
| Source preflight or 3.1 validation fails | `AssetStoreError` before any transform; setup fails; the file is not written (byte-identical). |
| 4.1 validation of the migrated candidate fails | `AssetStoreError`; nothing is saved. |
| Save raises before the atomic replace | The original 3.1 file remains; setup fails; the next setup migrates again deterministically. |
| Readback unreadable (`ambiguous=True`) or mismatching | The exception leaves `async_load`; `AssetStoreManager.async_setup` never publishes. The file is atomically either the old 3.1 file or the new 4.1 file: the next setup either migrates again or loads 4.1 and validates it. |
| Stored major newer than the code | `UnsupportedStorageVersionError`; the file is not written. |

There is no transaction journal. Atomic writes plus deterministic migration make a retried setup converge. After activation, ambiguous mutation writes are resolved inside the same locked mutation from the persisted snapshot (WP11 hook) for Maintenance and Archive, and at the next mutation (existing `_async_recover_uncertain_persistence`) for every other operation.

## 8. Rollback, downgrade, and backup

- **Before WP9:** rollback is a normal code rollback. The only persisted side effect is WP7's Entity Registry move, which a 0.7.7 build reverses through its own exposure plan.
- **After WP9:** a Store 4.1 file is refused by every 0.7.x release (`UnsupportedStorageVersionError`); Device Lifecycle then does not set up, and the file is left unchanged. There is no automatic 4.1 → 3.1 downgrade, and none is promised. Returning to 0.7.x requires restoring the Home Assistant backup made before the upgrade, or a copy of the pre-upgrade `device_lifecycle.assets` together with the matching Entity Registry.
- **Backup:** no automatic Store copy is created. The project's rule is one canonical Store; an automatic `.bak` file would be an unmanaged second copy of personal data that nothing reads, cleans up, or restores, and restoring it needs the same manual steps as restoring a Home Assistant backup. Instead:
  - the migration never writes before the preflight and 4.1 validation pass, and the atomic write keeps the original file until the verified replacement
  - the release notes (WP18) require a Home Assistant backup before upgrading and state that downgrade needs that backup
  - Test HA proves the upgrade on real data (section 5.4) before release

## 9. Master test matrix

U = unit, I = integration (pytest with Home Assistant), X = external Test HA. Every U and I test runs on Home Assistant 2026.8.0 and on the supported baseline in CI.

| Area | Tests | Type | WP |
|---|---|---|---|
| Store shape | exact 3.1/4.1 key constants equal real creation paths | U | 1 |
| Migration-source compatibility | missing/unexpected Asset and Purchase keys fail closed; input byte-identical | U | 1 |
| Migration-source compatibility | real Test HA Store inspection | X | 1 → 8 |
| Migration purity | transforms deep-copy, infer nothing, reject existing keys | U | 2, 3 |
| Commuting transforms | `A(M(v)) == M(A(v))` on rich payloads | U | 2 |
| Store 4.1 validation | every load invariant incl. archived + deployed and future `archived_at` | U | 3 |
| Store 3.1 regression | 3.1 validator behavior unchanged; existing suite | U, I | 3 |
| Archive/Restore domain | only `archived_at` changes; NO_OP first; deployed blocks | U | 4 |
| Archived Maintenance projection | archived vs disabled; Restore resumes incl. OVERDUE | U | 5 |
| Historical corrections | Maintenance void/correct allowed, others blocked, replay first | U | 5 |
| Historical corrections | Replacement void allowed; create/correct and Lifecycle blocked | I | 10 |
| Runtime identity resolution | resolver equals reconciliation | U | 6 |
| Runtime identity ownership | parent-owned move preserves identity; survives subentry removal; re-add reuses `entity_id` | I | 7 |
| Runtime identity ownership | Test HA continuity | X | 7 → 8 |
| Activation | 1.1/1.2/2.1/3.1 → 4.1 from raw file; wrong/newer versions; 3.1 reader refusal | I | 9 |
| Ambiguous persistence | migration readback failure; mutation replay/state resolution | I | 9, 11, 13 |
| Selectors TOCTOU | archived between render and submit; final-submit rejection | I | 10, 13, 15 |
| Reconciliation duplicate prevention | archived primary device never free; no duplicate Asset | I | 10 |
| Source-authoritative updates | Purchase-owned and HA-owned fields refresh; user-owned untouched | I | 10 |
| Maintenance persistence | lock-protected Runtime context; checkpoint-then-mutate ordering | I | 11 |
| Runtime conflict quarantine | no writer, no writes, no rewrite, one Repair, repeated reloads and restart | I | 12 |
| Repairs lifecycle | stale issues suppressed/rederived; conflict issue until resolved; entry removal deletes | I | 10, 12, 14 |
| Archive vs Runtime race | create, rebind, reservation, reload window, abandoned flow | I | 13 |
| Runtime Archive eligibility | active, pending, unresolved, durable quiesced, failed-then-finalized timeline, committed-total mismatch, no reload required | U, I | 13 |
| HA reload/unload | unload gate unchanged by WP7 and still blocking a quiesced writer with pending Runtime (WP13); archived Asset setup; quarantine without reload loops | I | 7, 12, 13 |
| Dispatcher refresh | entities and Repairs update without reload; failed listener keeps commit | I | 14 |
| Entity identity continuity | no `disabled_by`, no registry writes across Archive/Restore; Maintenance entities stay registered | I | 14, 16 |
| Archive/Restore UI | preview, blockers, restricted archived view, distinct labels | I | 15 |
| Maintenance entities/UI | Maintenance plan matrices plus archived behavior | I | 16, 17 |
| Upgrade and smoke | 3.1 → 4.1 on real data; Archive/Restore/Maintenance smoke | X | 18 |

## 10. Deliberately not built

No generic domain event bus, migration framework, archive engine, cross-store transaction manager, or entity projection framework. The additions are exactly:

- two small pure modules (`store_shape.py`, `archive.py`)
- one resolver module (`runtime_identity.py`)
- one conflict-Repairs module (`runtime_conflicts.py`)
- one optional resolver argument on the existing mutation pipeline
- one in-memory reservation set
- three capabilities on the existing `RuntimeWriter` (`durability`, `finalize`, `retire`) and one manager-private eligibility helper, with no persisted Runtime metadata and no general state machine
- one publish-listener list on the existing manager

## 11. Remaining external dependencies and owner decisions

| Item | Resolved by | When | Blocks |
|---|---|---|---|
| Real Test HA Store source shape (5.2) | Owner runs the script and reports the output | Before WP9 | WP9 and everything after it |
| Test HA Runtime identity continuity (5.3) | Owner, on a branch build with WP7 | Before WP9 | WP9 |
| Test HA upgrade and smoke (5.4, S7) | Owner, on the release candidate | Before merge or release | Release |
| Archive user-interface wording within the distinct-label rule | Owner, in the WP15 review | During WP15 | WP15 merge into the branch |
| Maintenance `disabled` presentation value and preparation `binary_sensor` in 0.8.0 | Owner | Before WP16 starts | WP16, WP17 |

Everything else in this plan is decided.
