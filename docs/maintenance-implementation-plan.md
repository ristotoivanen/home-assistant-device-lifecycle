# Device Lifecycle 0.8.x Maintenance — implementation plan

| | |
|---|---|
| Status | **APPROVED IMPLEMENTATION PLAN** |
| Schema baseline | `73234167ec88723879df92dc4ddc3e3e7b54281c` |
| Production implementation | NOT STARTED |
| Store 4 activation | BLOCKED BY ARCHIVE/VERSION GATE |

The authoritative schema is [Maintenance Store 4.x frozen schema](maintenance-store-v4-schema.md). This plan does not restate it: every record shape, canonical representation, invariant, mutation rule, and projection rule referenced here is defined there. Where this plan and the schema disagree, the schema wins and the plan is corrected.

## 1. Implementation boundary

The central rule of this plan is the difference between **implemented code** and a **reachable production feature**.

Before Store 4 activation (Gate 5 and the activation commit), the following may be implemented, reviewed, and tested on the branch:

- canonical helpers
- Maintenance record validators and the correction graph, as standalone functions
- pure projection functions
- snapshot-level mutation functions, request normalization, and replay helpers
- the Runtime-domain checkpoint interface
- Maintenance entity and flow classes that nothing registers or routes to
- unit tests for all of the above, using synthetic Maintenance payloads

Before activation, none of the following may exist on a production path:

- a Maintenance row in the Asset hub or any OptionsFlow Maintenance step reachable from it
- registered Maintenance entities, including a preparation `binary_sensor`
- `Platform.BINARY_SENSOR` added to `PLATFORMS` for Maintenance
- `AssetStoreManager` methods that write Maintenance data
- any code that writes `maintenance_schedules` or `maintenance_events` into a Store
- any wiring of the Maintenance validator into `_validate_store_data`

**Store 3.1 stays unaware of the Maintenance shape until activation.** Before activation, a Store 3.1 payload that contains `maintenance_schedules` or `maintenance_events` is invalid top-level data and fails closed. `_validate_store_data` never validates Maintenance collections in a 3.1 envelope, and there is no "validate Maintenance if the key exists" branch.

**Manager wiring decision.** Maintenance manager integration is deferred to the activation commit. There is no pre-activation manager Maintenance API, so no production code can write Maintenance data before Store 4 exists, and no runtime hard gate has to be trusted. The domain layer (snapshot mutations and replay) is complete and tested before activation; the manager only wraps it in the existing verified persistence pipeline.

## 2. Current implementation (baseline)

Paths and symbols as of the schema baseline.

- `custom_components/device_lifecycle/storage.py`
  - `DeviceLifecycleStore(Store)`: `STORAGE_VERSION = 3`, `STORAGE_MINOR_VERSION = 1`, private, atomic writes, `serialize_in_event_loop=False`. `_async_migrate_func` rejects a wrong same-major minor with `AssetStoreError` and chains `_migrate_v1_to_v2_1` → `_migrate_v2_1_to_v3_1` → `_validate_store_data`. `async_save` forces the pending write and verifies the exact envelope by reading the file directly; an unreadable readback raises `AssetStorePersistenceError(ambiguous=True)`. `async_load_persisted_snapshot` bypasses Store caches.
  - `AssetStoreManager`: `async_setup` fails closed on a corrupt file and checks that the known top-level keys are present. `_async_mutate_reporting` takes `_mutation_lock`, recovers uncertain persistence, deep-copies, runs the mutator, validates, saves only when data changed, and publishes after verification. `async_quick_create_asset` is the idempotency precedent: request normalization (`QuickAssetCreateRequest`), replay check inside the lock (`_quick_replay_result`), and ambiguous-write resolution from the persisted snapshot. `async_commit_runtime_delta` is the Runtime CAS write. `runtime_total_seconds` reads canonical Runtime.
  - Helpers: `_validate_history_effective_date` (civil-date round-trip), `_reject_future_effective_date` (`dt_util.now().date()`), `_parse_utc_timestamp` (aware UTC, no round-trip), `_runtime_seconds`, `_valid_uuid`, `_optional_text` (no trimming), `normalize_asset_metadata_text`, `add_calendar_years`.
- `custom_components/device_lifecycle/sensor.py`: Asset exposure entities and `DeviceRuntimeHoursSensor`, which keeps a monotonic active interval and pending deltas, checkpoints every five minutes, and flushes with `_async_flush_pending`. The warranty Lifecycle sensor recomputes at local midnight with `async_track_time_change(hour=0, minute=0, second=5)`.
- `custom_components/device_lifecycle/config_flow.py`: `DeviceLifecycleOptionsFlow` Asset hub (`manage_asset_menu`), read-only section menus, the `NOT_SELECTED` placeholder, confirmation steps such as `confirm_disposed`, and `_finish_asset_action` / `_async_apply_reload`.
- `custom_components/device_lifecycle/__init__.py`: `PLATFORMS = [Platform.SENSOR]`.
- There are no services, WebSocket commands, or custom frontend.

**Finding: the 3.1 top-level shape is not exact.** `_validate_store_data` and `async_setup` require the five known top-level keys but do not reject unknown ones, and the 1.x → 3.1 migrations carry unknown keys through. A 3.1 payload with `maintenance_*` keys is therefore accepted today and would be preserved on save. Commit 1 closes this before any Maintenance code lands.

## 3. Architecture

| Module | Content | Writes the Store |
|---|---|---|
| `canonical.py` (new) | Shared canonical helpers | Never |
| `maintenance.py` (new) | Record validators, correction graph, derived helpers (baseline lock, hard-delete eligibility), request dataclasses, replay equality, snapshot mutation functions `fn(data, request) -> result` | Only through the manager after activation |
| `maintenance_projection.py` (new) | Pure effective-source, Runtime-safety, due, calendar, and preparation functions | Never |
| `storage.py` | Store 3.1 hardening now; Maintenance manager wrappers, Store 4 migration, and validator wiring at activation | — |
| `sensor.py`, `binary_sensor.py`, `config_flow.py` | Maintenance classes and steps; registered and routed only at activation | — |

## 4. Canonical helpers (`canonical.py`)

- `require_text(value)` and `optional_text(value)`: load-time checks (`str`, `value == value.strip()`, non-empty; optional also allows `None`).
- `normalize_required_text(value)` and `normalize_optional_text(value)`: mutation-time trimming; an empty optional value becomes `None`.
- `parse_civil_date(value)`: the round-trip rule already implemented by `_validate_history_effective_date`.
- `parse_canonical_utc(value)`: aware, UTC offset zero, `parsed.isoformat() == value`. The existing `_parse_utc_timestamp` used by Lifecycle and Replacement is not tightened.
- `parse_canonical_decimal(value, *, positive=False)`: finite, `format(Decimal(value), "f") == value`, non-negative (positive for intervals).
- `decimal_from_input(value)`: `Decimal(str(value))`, never `Decimal(float)`; rejects negative values; normalizes `-0` to zero.
- `format_decimal(value)`: `format(value, "f")`.
- `canonical_uuid(value)`: `_valid_uuid(value) == value`.
- `today()`: `dt_util.now().date()`. `utc_now_iso()`: `datetime.now(UTC).isoformat()`, the existing timestamp convention.

Existing domains keep their current behavior; the Asset Runtime validator is not tightened.

## 5. Validation (`maintenance.py`)

- `validate_maintenance_collections(schedules, events, assets)` implements every Maintenance load invariant in the schema: exact key sets, canonical values, references, intervals, anchor components, reminder, `void_reason`, and the absence of any `recorded_at`/`voided_at` ordering rule.
- The correction graph builds a corrector map (a duplicate target is a violation), checks existence, self-reference, same Asset, and voided target, and detects cycles by following each chain with a visited set, O(E).
- Derived helpers: `referenced_schedule_uuids(events)` (including voided Events), `is_baseline_locked`, and `can_hard_delete`. Everything is O(S + E) with no persisted index.

Before activation this validator is called only by tests. At activation, `_validate_store_data` requires the complete Store 4 top-level shape and calls it.

## 6. Mutations (`maintenance.py`, snapshot level)

Every operation is a function over a detached snapshot. At activation the manager runs it inside the existing lock-copy-validate-save-readback-publish pipeline. The common order is: replay check → state preconditions → normalization and validation → candidate changes. A canonical no-op returns `changed = False`, so no save and no reload happen.

| Operation | Request | Order and rules | Replay and conflict |
|---|---|---|---|
| Create Schedule | `ScheduleCreateRequest` with pre-generated `schedule_uuid` | Replay → Asset exists → shape → M-9 and future-date rule for the baseline | Same UUID and identical complete Schedule: no-op. Same UUID, different content: conflict. |
| Edit config | Schedule UUID and new `name`, `enabled`, interval values, or reminder | Schedule exists → shape | Unchanged values: no-op |
| Edit unused baseline | Schedule UUID and anchor | Baseline lock → M-9 → future-date rule | Unchanged: no-op |
| Add interval type | Schedule UUID, dimension, value, optional baseline component | A baseline component only when unlocked | — |
| Remove interval type | Schedule UUID, dimension, destroy confirmation | Last interval → reject. Cleanup of the matching anchor component and collapse to `null`. Calendar removal also clears the reminder. If a locked component would be destroyed without confirmation: reject with `confirmation_required`. | — |
| Enable / disable | Schedule UUID, bool | — | Unchanged: no-op |
| Hard delete | Schedule UUID | `can_hard_delete` | A missing Schedule fails closed. A no-op replay only when the caller can prove its own earlier attempt without persisted metadata. |
| Record Event | `EventRecordRequest` with pre-generated `event_uuid` | Replay → Asset and Schedules exist on the same Asset (disabled allowed) → future-date rule → M-8 against the current canonical Runtime read from the same snapshot → normalization → `recorded_at` observed | Replay only while the Event is active with no corrector; otherwise conflict |
| Void Event | `EventVoidRequest(event_uuid, void_reason)` | Replay → target active → `voided_at` observed, never clamped | Replay: target voided, no corrector, same normalized reason. A correction-void is a conflict. |
| Correct Event | `EventCorrectRequest(new_event_uuid, target_uuid, fields, void_reason)` | **Replay before the active-target check** → target active → Runtime comparison (below) → one snapshot change: void target and create the complete corrected Event | Later void or correction of either Event: conflict |

**Runtime in a correction.** The mutation layer decides whether the Runtime changed; the request carries no "unchanged" flag and no caller claim can skip M-8.

```text
old = target.runtime_seconds, new = request.runtime_seconds
both null                         -> unchanged
exactly one null                  -> changed
Decimal(old) == Decimal(new)      -> unchanged
otherwise                         -> changed

unchanged -> M-8 not applied; the corrected Event keeps the target's exact string
changed   -> M-8 applied; the new value is persisted with format_decimal
```

A flow may remember that a field was not edited, but only to prefill and preserve input. It never passes that as a business decision.

**Replay equality.** `event_business_key(event)` is the tuple of `asset_uuid`, `frozenset(schedule_uuids)`, `title`, `performed_date`, `Decimal(runtime_seconds)` or `None`, `notes`, and `corrects_event_uuid`. Text is compared after normalization and UUIDs as canonical strings. Generated timestamp values are excluded; active, voided, and corrected state is checked separately and is always part of the decision. Nothing new is persisted.

## 7. Projection (`maintenance_projection.py`)

Pure functions taking `today` and `current_runtime` from the caller:

- `relevant_events`, `latest_group`, and `effective_source`: dated baseline, date-less Runtime baseline, and Event group exactly as in the schema, with the same-source rule and no tie-breaker.
- `apply_runtime_safety`: Rules A, B, and B′; the calendar component stays known.
- `add_interval(date, value, unit)`: `days`, `months` with end-of-month clamp, `years` as 12·N months. An overflow yields UNKNOWN.
- `condition_state` and `combined_state`: `OK < UNKNOWN < DUE < OVERDUE`; a disabled Schedule has no active projection.
- `preparation_state(schedule, calendar_due, combined_state, today)` returns one of `ACTIVE`, `INACTIVE`, or `UNKNOWN`:

  ```text
  no reminder configured          -> not applicable
  calendar due date UNKNOWN       -> UNKNOWN
  state is DUE or OVERDUE         -> INACTIVE
  otherwise                       -> ACTIVE if due - lead_days <= today < due, else INACTIVE
  ```

  UNKNOWN is never turned into `INACTIVE`.

## 8. Runtime checkpoint interface

Runtime writes stay owned by the Runtime domain. Maintenance requests a checkpoint and reads canonical Runtime; it never calls `async_commit_runtime_delta`.

- The Runtime domain gains an Asset-scoped interface, for example `AssetStoreManager.async_request_runtime_checkpoint(asset_uuid)`, backed by a callback that each `DeviceRuntimeHoursSensor` registers for its Asset while it is added to Home Assistant. The callback seals the active interval and awaits the sensor's existing flush path, which commits through the Runtime CAS write. With no registered sensor, the call returns immediately: canonical Runtime is already all that is known.
- "Just now" sequence:

  ```text
  request Runtime-domain checkpoint (no Maintenance lock held)
  -> await the durable Runtime commit
  -> read canonical persisted Runtime (runtime_total_seconds)
  -> show the captured value in the confirmation step
  -> send that exact canonical value in the Event request
  -> the Maintenance mutation validates M-8 against the current canonical Runtime
  ```

- **Lock ordering.** The checkpoint takes the sensor's entity-level lock and then the manager `_mutation_lock` inside the Runtime commit. The flow awaits the checkpoint to completion before it starts the Maintenance mutation, and the Maintenance mutation takes only `_mutation_lock`. The two are sequential, so no path holds `_mutation_lock` while waiting for a Runtime checkpoint, and no deadlock is possible.
- Runtime is monotonic, so the captured value can only be less than or equal to the value M-8 reads under the lock. A checkpoint failure leaves the flow on the confirmation step with an error; no stale value is shown as captured.
- A backdated Event never copies the current Runtime; its Runtime is entered by the person or left unknown.

## 9. Home Assistant surfaces (classes before activation, wiring at activation)

**OptionsFlow.**

- The Asset hub gets a Maintenance row that opens a read-only Maintenance view with a summary and the actions Add schedule, Open schedule (selector starting at `NOT_SELECTED`), Record maintenance (multi-select of this Asset's Schedules with nothing preselected; empty is ad-hoc), History, and Back.
- The Schedule view offers Mark done, Edit, Intervals, Enable/Disable, Delete (only while unused), and Back. Mark done preselects only that Schedule.
- Create: name; an optional time interval (number and a `days`/`months`/`years` unit); an optional Runtime interval in hours; at least one is required. There is no Calendar/Runtime/Combined type selector.
- Starting point: an explicit choice between "Not known" and "I know it", with no default, followed by date and hours fields only for the active dimensions.
- Mark done: "Just now" (today and the captured Runtime, read-only) or "Earlier date" (date and optional Runtime; empty means unknown).
- The preparation reminder is its own field group, shown only with a calendar interval.
- Guards: a first-Event lock notice in the form description; a mandatory confirmation step that names the information lost before a locked baseline component is destroyed; a non-blocking warning step when an Event makes the latest date ambiguous for a Runtime Schedule.
- Hours input from `NumberSelector` is converted with `decimal_from_input` and multiplied as `Decimal`.

**History: the summary display limit is not an actionability limit.**

- The History view shows a summary of the latest N Events.
- Correct and Void first open a date-range filter step (from and to, defaulting to all), then a selector listing every matching Event of the Asset, newest first, labelled by date, title, and state.
- An Event older than the summary limit is still selectable, including voided Events for display. Correct and Void accept only active targets and report otherwise.
- At household scale the unfiltered selector lists all Events; the filter keeps it usable. No custom frontend.

**Entities** (one current projection per Schedule, on the Asset Device, parent-owned; never one entity per Event; no history in attributes):

| Entity | State | `unique_id` | 0.8.0 |
|---|---|---|---|
| Maintenance status (`sensor`, `ENUM`) | `ok`, `unknown`, `due`, `overdue`, or `disabled` (presentation value) | `<schedule_uuid>_maintenance_status` | Yes |
| Next maintenance (`sensor`, `DATE`) | Calendar due date, or unknown | `<schedule_uuid>_maintenance_due_date` | Yes |
| Maintenance preparation (`binary_sensor`) | `on` for ACTIVE, `off` for INACTIVE, HA unknown (`is_on = None`) for UNKNOWN; only for Schedules with a reminder | `<schedule_uuid>_maintenance_preparation` | Yes, if approved at Gate 4 |
| Runtime remaining | — | — | Rejected for 0.8.0; can be added later without a schema change |

Updates come from the reload after each Store change, a local-midnight `async_track_time_change` trigger, and a manager publish listener added at activation so a Runtime commit refreshes Runtime-dependent projections. Sensor setup removes Maintenance entities whose Schedule was hard-deleted. The `_maintenance_*` suffixes do not end in `_lifecycle` or `_runtime_hours`, so the exposure preflight is unaffected.

## 10. Store 4 migration boundary

- Before activation: a composable helper `_add_maintenance_collections(candidate)` adds empty `maintenance_schedules` and `maintenance_events` mappings. Tests prove that it adds only those two empty mappings, changes no existing 3.1 data, and is deterministic. It is not called by `_async_migrate_func`.
- `_migrate_v3_1_to_v4` and the migration dispatch do not exist before Gate 5, because the complete Store 4 target shape, including Archive, and its version number are not yet known.
- At activation: `_migrate_v3_1_to_v4` composes `_add_maintenance_collections` with the Archive transformation, the dispatch is wired, and the result is validated as a complete Store before it is saved and read back. A wrong same-major minor keeps failing with the project's own `AssetStoreError`, never `NotImplementedError`.

## 11. Commit sequence

Every commit keeps the full test suite green on the minimum and baseline Home Assistant versions. Nothing is merged to `main` or released before Gate 6.

| # | Purpose | Likely files | Behavior | Tests | Depends on | Rollback |
|---|---|---|---|---|---|---|
| 1 | Store 3.1 exact top-level shape | `storage.py`, `tests/test_storage_quality_gates.py` | A 3.1 payload with any unknown top-level key, including `maintenance_*`, fails closed at load and before save. This hardens released behavior: stores written by released versions contain exactly the five keys. | Unknown-key rejection, `maintenance_*` rejection, existing migrations still valid | — | Revert |
| 2 | Canonical helpers | `canonical.py`, `storage.py` imports, `tests/test_canonical_helpers.py` | None visible | Helper matrix; unchanged behavior of existing domains | — | Revert |
| 3 | Maintenance models and standalone validators | `models.py`, `maintenance.py`, `tests/test_maintenance_validation.py`, `tests/test_maintenance_baseline.py` (derived helpers) | None; not wired into `_validate_store_data` | Validation matrix | 2 | Revert |
| 4 | Pure projection | `maintenance_projection.py`, `tests/test_maintenance_projection.py`, `tests/test_maintenance_calendar.py` | None | Projection, calendar, and preparation matrix | 2–3 | Revert |
| 5 | Snapshot mutations, replay, migration helper | `maintenance.py`, `tests/test_maintenance_mutations.py`, `tests/test_maintenance_idempotency.py`, `tests/test_maintenance_migration.py` (helper only) | None; functions over detached snapshots | Mutation, replay, Runtime-correction, and helper matrix | 3 | Revert |
| 6 | Runtime checkpoint interface | `sensor.py`, `storage.py`, `tests/test_runtime.py` | Runtime domain can checkpoint on request; no Maintenance caller | Flush-before-read, CAS regression, no registered sensor | — | Revert |
| 7 | Maintenance entity and flow classes, unregistered | `sensor.py`, new `binary_sensor.py` (not in `PLATFORMS`), `config_flow.py` (steps not reachable from any menu), `translations/*.json`, `tests/test_maintenance_entities.py`, `tests/test_maintenance_options_flow.py` | None reachable. Tests instantiate classes and drive steps directly with synthetic data. | Entity and flow matrix; reachability test proves no hub row, menu option, or platform registration exists | 4–6 | Revert |
| — | **Gate 5**: Archive schema and Store 4 version coordination | — | — | — | — | — |
| 8 | Atomic activation | `storage.py` (version constants, `_migrate_v3_1_to_v4`, dispatch, mandatory Store 4 top-level shape including Archive, `_validate_store_data` wiring, `_empty_store_data`, `async_setup` keys, Maintenance manager API, publish listener), `__init__.py` (`PLATFORMS`), `config_flow.py` (hub row), `tests/conftest.py` and fixtures, integration tests | Store 4 and Maintenance become reachable together | Migration integration, full suite on both Home Assistant versions | Gate 5, 1–7 | Downgrade requires a backup |
| 9 | Documentation and release notes | `README.md`, `ARCHITECTURE.md`, upgrade notes | — | `git diff --check` | 8 | Revert |

**Activation strategy.** Commit 8 is one atomic boundary so that no intermediate state exposes a half-active feature. If it has to be split for review, the split is only into commits that each still fail closed: first the Store 4 shape, migration, and validator wiring together with `_empty_store_data` and the fixtures, with no Maintenance manager API, hub row, or platform registration; then the manager API, platform registration, and hub row together. No commit may register Maintenance entities or UI before the Store 4 validator is active, and no commit may activate Store 4 without the complete Maintenance and Archive shape.

## 12. Test matrix

- `tests/test_storage_quality_gates.py` (commit 1): a 3.1 payload with `maintenance_schedules` or `maintenance_events` is rejected as the wrong exact Store shape; any other unknown top-level key is rejected; existing 1.x → 3.1 migrations still validate.
- `tests/test_canonical_helpers.py`: text trimming, empty and whitespace; `2026-02-30`; non-canonical dates; timestamps with `Z`, `-00:00`, `.000000`, naive; Decimal exponent, `NaN`, `Infinity`, `-0`, leading zeros, `"3600"` versus `"3600.0"`, float input through `Decimal(str(x))`; existing Asset Runtime validation unchanged.
- `tests/test_maintenance_validation.py`: exact shapes including nested objects; missing and extra keys; UUID/key mismatch; orphan Asset and Schedule; cross-Asset and duplicate references; empty `schedule_uuids` and disabled references allowed; anchor without its interval; `{null, null}` anchor; reminder without calendar interval; `void_reason` without `voided_at`; `voided_at < recorded_at` accepted; correction self-reference, cross-Asset, active target, two correctors, cycle.
- `tests/test_maintenance_baseline.py`: unlocked edit; first Event locks; void and correction do not unlock; interval-removal cleanup and `null` collapse; remove and re-add a dimension without resurrection or new component; hard delete blocked by a voided reference; unused hard delete; last interval; reminder cleanup; destroying a locked component without confirmation is rejected.
- `tests/test_maintenance_projection.py`: no baseline and no Event; dated baseline only; Event newer, same date, older; date-less baseline with no Events and with Event Runtime greater than, equal to, less than, and null; several latest Events; void fallback to the baseline; T-3 source; Rules A, B, B′ with calendar kept; due OK, DUE at the exact threshold, OVERDUE, UNKNOWN, OK+UNKNOWN, DUE+UNKNOWN, OVERDUE+UNKNOWN, disabled; preparation known outside the lead window is INACTIVE, inside is ACTIVE, DUE is INACTIVE, OVERDUE is INACTIVE, unknown calendar due is UNKNOWN.
- `tests/test_maintenance_calendar.py`: `2026-01-31` + 1 month; `2028-02-29` + 1 and + 4 years; overflow is UNKNOWN; `years` equals 12·N months; a DST transition day does not change civil results; a local date that differs from the UTC date (frozen time at 23:30 UTC).
- `tests/test_maintenance_mutations.py`: every operation and its no-op; M-8 and M-9 with known and null current Runtime; correction with `"3600"` against `"3600.0"` is unchanged and keeps the original string; a numerically changed value runs M-8; no request field or caller claim can bypass M-8; observed timestamps earlier than the target's `recorded_at` are accepted and not clamped.
- `tests/test_maintenance_idempotency.py`: lost acknowledgement on Record Event replays; a later void followed by the old retry conflicts; lost acknowledgement on a correction replays; a corrected Event corrected again makes the old retry conflict; manual void replay; a correction-void is not accepted as a manual void replay; same UUID with different content conflicts; Schedule create replay and conflict after an edit; correction replay is checked before the active-target rule.
- `tests/test_maintenance_migration.py`: `_add_maintenance_collections` adds only two empty mappings, preserves 3.1 data byte for byte, and is deterministic. At activation: 3.1 → 4 integration, wrong minor rejected with the project error, Store 4 rejected by a simulated 0.7.x reader.
- `tests/test_maintenance_persistence.py` (activation): success; save failure; readback mismatch; corrupted Store; ambiguous result resolved by replay against the persisted snapshot; no publish before verified readback.
- `tests/test_runtime.py` (commit 6): checkpoint seals and commits pending time before the read; CAS regression; no registered sensor; no lock held across checkpoint and Maintenance mutation.
- `tests/test_maintenance_entities.py`: unique IDs and Asset Device placement; midnight and publish-listener refresh; orphan cleanup after hard delete; preparation `binary_sensor` reports unknown, not off, for UNKNOWN; no history in attributes; exposure preflight unaffected.
- `tests/test_maintenance_options_flow.py`: every step; `NOT_SELECTED`; no other Schedule preselected; "Just now" captures after the checkpoint; an earlier date never copies current Runtime; the three guards; the mandatory destroy confirmation; an Event older than the History summary limit N is selectable for Correct and Void; the form-exit line inventory; EN/FI translation parity; before activation, a reachability test proves no hub row, menu option, or platform registration exists.

## 13. Risks

| Risk | Failure mode | Detection | Mitigation | Residual |
|---|---|---|---|---|
| Store 4 activated before Archive is ready | A second Store 4 migration becomes necessary | Gate 5 review | Activation only in commit 8 after Gate 5 | Low |
| 3.1 hardening rejects an existing store | A store with an unexpected top-level key stops loading | Commit 1 tests; Test HA run on the real store before merge | Released versions write exactly the five keys; fail closed with an actionable error | Low |
| Migration overwrite or silent acceptance | Wrong minor loaded, or `NotImplementedError` lets Home Assistant load data unchanged | Migration tests | Project error, whole-Store validation, readback | Low |
| Decimal float contamination | Hours computed as float drift the stored seconds | Decimal tests, load validation | `decimal_from_input`, Decimal arithmetic, mutation-owned Runtime comparison | Low |
| Replay order | Active-target check before replay turns a lost acknowledgement into an error | Idempotency tests | One manager pipeline enforces replay first | Low |
| Runtime lag or race | Canonical Runtime lags the sensor by up to five minutes, causing false M-8 rejections | Flow test with pending delta | Checkpoint before read; M-8 read under the lock | Seconds; never a rejection |
| Checkpoint deadlock | A lock held across checkpoint and mutation | Runtime test | Sequential lock ordering (section 8) | Low |
| Local date boundary | `date.today()` or UTC used as "today" | Frozen-time test | `dt_util.now().date()` only | Low |
| Entity stale state | Due state not refreshed at midnight or after a Runtime commit | Entity tests | Midnight trigger and publish listener | Runtime updates at checkpoint cadence |
| Correction graph cost | Cycle check on every mutation | Test with about 10,000 Events | O(E) with a visited set | Negligible |
| Interval cleanup UX mistake | A locked starting point destroyed unknowingly | Flow test | Mandatory confirmation; mutation rejects without it | Low |
| Unreachability regression | A pre-activation commit exposes Maintenance | Reachability test | Classes only; wiring only in commit 8 | Low |

## 14. Gates

| Gate | Evidence required | Status |
|---|---|---|
| 1. Pure domain and Store helpers ready | Commits 1–4 green; validation and projection matrices cover every schema rule; 3.1 rejects `maintenance_*` | NOT READY |
| 2. Mutation layer ready | Commit 5 green; mutation, replay, and Runtime-correction matrices | NOT READY |
| 3. Projection layer ready | Rules A, B, B′, due, calendar, and preparation UNKNOWN proven | NOT READY |
| 4. Native HA UX ready | Commits 6–7 green; guards; History actionability; EN/FI; owner decisions on the `disabled` status value and the preparation `binary_sensor`; reachability test proves nothing is exposed | NOT READY |
| 5. Archive and version coordination complete | Archive schema frozen; complete Store 4 top-level shape and version number assigned | BLOCKED |
| 6. Migration activation and full integration tests | Commit 8; full suite on Home Assistant 2026.8.0 and the baseline; 3.1 → 4 upgrade in Test HA with real data; downgrade refused | NOT READY |
| 7. Release readiness | Documentation, release notes, backup guidance; Test HA smoke of create, mark done, correct, void, and due transitions | NOT READY |
