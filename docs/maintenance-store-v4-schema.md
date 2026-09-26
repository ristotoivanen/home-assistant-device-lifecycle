# Device Lifecycle 0.8.x Maintenance — Store 4.x frozen schema

| | |
|---|---|
| Status | **FROZEN** |
| Schema freeze approved | 2026-09-26 |
| Implementation status | not implemented |
| Store version number | Store 4.1 (`STORAGE_VERSION = 4`, `STORAGE_MINOR_VERSION = 1`), assigned 2026-09-26 by the coordinated [Asset Archive and Store 4.1 frozen architecture](asset-archive-store-v4.md) |
| Archive coordination 2026-09-26 | Archived-Asset projection and mutation rules added; see [Archived Assets](#archived-assets). No persisted Maintenance record shape or Maintenance load invariant changed. |
| Projection erratum 2026-09-26 | Future effective calendar anchors project UNKNOWN; see [Future calendar anchor](#future-calendar-anchor). No persisted schema fields or load invariants changed. |

This document is the canonical persisted schema for 0.8.x Maintenance. It is implementation-independent. It freezes the persisted record shapes, their canonical representations, the whole-Store load invariants, the mutation rules that protect them, and the derived projection semantics that the persisted data must support. A change to anything in this document is a schema revision and needs an explicit review; it is not an implementation detail.

The design constraints this schema satisfies are in [ARCHITECTURE.md: Planned: 0.8.x Maintenance usability constraints](../ARCHITECTURE.md#planned-08x-maintenance-usability-constraints). The Asset identity, persistence, and migration rules in [ARCHITECTURE.md](../ARCHITECTURE.md) apply unchanged.

## Concepts

- A **Maintenance Schedule** is the current planning configuration for one Asset. It is not history, and its configuration can be edited after maintenance history exists.
- A **Maintenance Event** is a historical fact: maintenance performed on one Asset. It references zero or more Schedules of the same Asset; zero Schedules means ad-hoc maintenance.
- A Schedule belongs to exactly one Asset and is never moved to another Asset. Replacement never transfers Schedules, Events, or baselines.
- Maintenance reads the canonical Asset Runtime (`asset.runtime.total_seconds`) but never writes it.
- Historical Events are never edited in place. A correction voids the old Event and creates a complete new Event in one atomic mutation. Voided Events remain canonical history. Events are never deleted in 0.8.x.
- Due state, effective anchors, and reminder activity are live projections and are never persisted.

## Store payload

Store 4.1 keeps every Store 3.1 top-level collection and adds two required top-level mappings. The complete Store 4.1 shape, including the one Archive field that every Asset gains (`archived_at`), is canonical in [Asset Archive and Store 4.1 frozen architecture](asset-archive-store-v4.md); Maintenance adds no Asset field.

```text
next_asset_number        (unchanged)
purchases                (unchanged shape; exact key set in Store 4.1)
assets                   (Store 3.1 fields plus archived_at; no Maintenance fields)
lifecycle_events         (unchanged)
replacement_records      (unchanged)
maintenance_schedules    schedule_uuid -> MaintenanceSchedule
maintenance_events       event_uuid    -> MaintenanceEvent
```

## Record shapes

Every record uses an exact key set. Every key is always present; a nullable field is stored as `null`, never omitted. A non-null nested object also uses its exact key set.

### MaintenanceSchedule

```text
{
  "schedule_uuid":            <canonical UUID, same as the map key>,
  "asset_uuid":               <canonical UUID>,
  "name":                     <trimmed non-empty string>,
  "enabled":                  <bool>,
  "calendar_interval":        null | {
                                 "value": <int > 0, not bool>,
                                 "unit":  "days" | "months" | "years"
                               },
  "runtime_interval_seconds": null | <canonical Decimal string > 0>,
  "initial_anchor":           null | {
                                 "date":            null | <civil date>,
                                 "runtime_seconds": null | <canonical Decimal string >= 0>
                               },
  "preparation_reminder":     null | {
                                 "lead_days": <int > 0, not bool>,
                                 "message":   null | <trimmed non-empty string>
                               }
}
```

| Field | Meaning |
|---|---|
| `schedule_uuid` | Identity. Generated before the confirmed create request, so it is also the idempotency key. Unique in the current Store. |
| `asset_uuid` | Owning Asset. Immutable. |
| `name` | Current name and the default title of a new Event. An Event keeps its own title snapshot. |
| `enabled` | Affects only the active due projection. It never affects history, the baseline lock, or Event links. |
| `calendar_interval` | Calendar interval with its semantic unit. The unit is never normalized to days. |
| `runtime_interval_seconds` | Canonical Runtime duration. |
| `initial_anchor` | The explicit calculation baseline that precedes maintenance history. It is neither a Maintenance Event nor a claim that maintenance was performed. An unknown baseline is `null`. |
| `preparation_reminder` | Optional calendar-based preparation reminder: calendar days before a known calendar due date and an optional message. There is no Runtime-relative lead in 0.8.x. Its activation is never stored. |

### MaintenanceEvent

```text
{
  "event_uuid":          <canonical UUID, same as the map key>,
  "asset_uuid":          <canonical UUID>,
  "schedule_uuids":      [<canonical UUID>, ...],
  "title":               <trimmed non-empty string>,
  "performed_date":      <civil date>,
  "runtime_seconds":     null | <canonical Decimal string >= 0>,
  "recorded_at":         <canonical UTC timestamp>,
  "notes":               null | <trimmed non-empty string>,
  "voided_at":           null | <canonical UTC timestamp>,
  "void_reason":         null | <trimmed non-empty string>,
  "corrects_event_uuid": null | <canonical UUID>
}
```

| Field | Meaning |
|---|---|
| `event_uuid` | Identity. Generated before the confirmed request, so it is also the idempotency key. |
| `asset_uuid` | Asset the maintenance was performed on. Immutable. |
| `schedule_uuids` | Schedules the maintenance applies to. Unique; order has no meaning; `[]` is ad-hoc maintenance. A disabled Schedule may be referenced. |
| `title` | Historical title snapshot. It does not follow later Schedule name changes. |
| `performed_date` | Civil date the maintenance was performed. Backdated Events are allowed. |
| `runtime_seconds` | Canonical Runtime total at the time the maintenance was performed, or `null` when unknown. The current Runtime is never used as a historical value automatically. |
| `recorded_at` | Observed UTC time the Event was recorded. Audit only; never an ordering key. |
| `notes` | Optional free text. |
| `voided_at` | Observed UTC time the Event was voided. Void is final. |
| `void_reason` | Optional reason why this Event is no longer valid. It applies to both manual voids and corrections. |
| `corrects_event_uuid` | The voided Event that this Event replaces. Set only when the Event is created by a correction. |

## Canonical representations

| Kind | Canonical form | Validation |
|---|---|---|
| UUID | Canonical lowercase hyphenated UUID string | `str(UUID(s)) == s`; a map key equals the record's own UUID field |
| Required text | Trimmed non-empty string | `isinstance(s, str) and s == s.strip() and s != ""` |
| Optional text | `null` or required text | `""` and whitespace-only strings are invalid; `null` is the only empty representation |
| Civil date | `YYYY-MM-DD`, a real Gregorian calendar date | `date.fromisoformat(s).isoformat() == s`; for example `2026-02-30` is invalid |
| UTC timestamp | The repository convention `datetime.now(UTC).isoformat()`, for example `2026-09-26T12:34:56.123456+00:00`, or `2026-09-26T12:34:56+00:00` when the microseconds are zero | `datetime.fromisoformat(s)` is aware, its UTC offset is zero, and `parsed.isoformat() == s`. This rejects `Z`, `-00:00`, and `.000000+00:00` spellings. |
| Runtime Decimal | Plain decimal string | `Decimal(s)` is finite, `format(Decimal(s), "f") == s`, and the value is non-negative. Exponent notation, `NaN`, `Infinity`, negative values, `-0`, and leading zeros are invalid. An interval is additionally greater than zero. |

Runtime values are compared numerically: `Decimal("3600") == Decimal("3600.0")`. Both spellings are canonical because each round-trips through `format(..., "f")`.

A mutation constructs a Decimal from a string, `Decimal(str(value))`, never from a binary float, rejects negative values, normalizes `-0` to `"0"`, and persists with `format(decimal_value, "f")`, never `.normalize()`. When a correction leaves the Runtime unchanged, the original `runtime_seconds` string is carried over exactly.

Load-time validation never compares a civil date or a timestamp with the current clock.

## Load-time whole-Store invariants

These are checked from the Store contents alone whenever the Store is loaded, migrated, or about to be saved. A violation makes the Store invalid and fails closed.

### Schedule

1. The record has the exact key set, and the map key equals `schedule_uuid`. All UUIDs are canonical.
2. `asset_uuid` references an existing Asset.
3. `name` is required text. `enabled` is exactly a `bool`.
4. At least one of `calendar_interval` and `runtime_interval_seconds` is non-null.
5. `calendar_interval` is `null` or has an `int` (not `bool`) `value` greater than zero and a `unit` of `days`, `months`, or `years`.
6. `runtime_interval_seconds` is `null` or a canonical Runtime Decimal greater than zero.
7. `initial_anchor` is `null` or an object with at least one non-null component. `date` is a civil date; `runtime_seconds` is a canonical Runtime Decimal.
8. If `calendar_interval` is `null`, `initial_anchor.date` is `null`. If `runtime_interval_seconds` is `null`, `initial_anchor.runtime_seconds` is `null`.
9. `preparation_reminder` is `null` or has an `int` (not `bool`) `lead_days` greater than zero and a `message` that is optional text.
10. If `preparation_reminder` is non-null, `calendar_interval` is non-null.

### Event

1. The record has the exact key set, and the map key equals `event_uuid`. All UUIDs are canonical.
2. `asset_uuid` references an existing Asset.
3. `schedule_uuids` contains no duplicates. Every entry references an existing Schedule whose `asset_uuid` equals the Event's `asset_uuid`. An empty list is allowed. A reference to a disabled Schedule is allowed.
4. `title` is required text. `notes` is optional text.
5. `performed_date` is a civil date.
6. `runtime_seconds` is `null` or a canonical Runtime Decimal.
7. `recorded_at` is a UTC timestamp. `voided_at` is `null` or a UTC timestamp.
8. `void_reason` is optional text. If `void_reason` is non-null, `voided_at` is non-null. A voided Event may have a `null` `void_reason`.

### Correction graph

1. `corrects_event_uuid` is `null` or references an existing Event.
2. The target is not the Event itself.
3. The target has the same `asset_uuid`.
4. The target is voided.
5. Each Event has at most one correcting Event: `corrects_event_uuid` values are unique across the collection.
6. The correction graph is acyclic.

### Not load invariants

The following are deliberately **not** whole-Store invariants:

- `voided_at >= recorded_at`. Timestamps are observed audit values, not ordering keys; order is proven by the void state, the correction link, and the atomic mutation.
- Any comparison with the current canonical Runtime. Historical Runtime is never judged against the current Runtime at load.
- Monotonic Runtime across historical Events. A contradiction makes the Runtime projection UNKNOWN instead.
- The baseline lock. It is derived from Event references and enforced at mutation time.
- `performed_date <= today` or `initial_anchor.date <= today`. A future effective calendar anchor makes the calendar projection UNKNOWN instead.
- Permanent non-reuse of a hard-deleted Schedule UUID. There is no tombstone; new UUIDs are generated.
- A maximum interval. A due date that cannot be represented is a projection error boundary (UNKNOWN).
- An order for Events on the same `performed_date`, or an order for `schedule_uuids`.

## Mutation rules

These rules apply when data changes. They are separate from the load invariants, and the result of every mutation must still satisfy all load invariants.

1. **Verified persistence.** Every mutation follows the existing Asset Store pipeline: lock, deep copy, mutate, validate the whole Store, save durably, read back directly from disk (not from Home Assistant's in-memory Store cache), verify the exact envelope and payload, then publish. An ambiguous write outcome is never blindly retried: the persisted snapshot is read, validated, and resolved with the replay rules below. Maintenance has no transaction journal of its own.
2. **Pre-generated identity.** A new Schedule or Event UUID is generated before the confirmed request and reused on retry.
3. **Replay first.** Idempotent replay is checked before any state precondition such as "the target Event is active".
4. **Baseline lock.** An `initial_anchor` component may be set or changed to another value only while no Event, including a voided Event, has ever referenced the Schedule. Voiding an Event never unlocks the baseline, and the baseline is never used to reset a schedule or restart its countdown.
5. **Interval-type removal cleanup.** Removing the calendar interval sets `initial_anchor.date` to `null`; removing the Runtime interval sets `initial_anchor.runtime_seconds` to `null`. This cleanup is allowed on a locked Schedule. If no component remains known, `initial_anchor` becomes `null`. A removed component is never restored, and a locked Schedule never receives a new explicit component when an interval type is added again.
6. **Last interval.** Removing the last remaining interval is rejected.
7. **Reminder cleanup.** Removing the calendar interval also sets `preparation_reminder` to `null`. The user interface may require explicit confirmation, but the resulting Store never has a reminder without a calendar interval.
8. **No future dates.** A user-entered `performed_date` or `initial_anchor.date` later than Home Assistant's configured local civil date, `dt_util.now().date()`, is rejected. `date.today()` and the process timezone are not used.
9. **Runtime sanity.** When the current canonical Runtime is known, a user-supplied Event `runtime_seconds` or `initial_anchor.runtime_seconds` greater than it is rejected. When the current Runtime is `null`, no comparison is made. In a correction the rule applies only to a Runtime value that numerically differs from the target's value.
10. **Void and correction targets.** After the replay check, the target of a void or correction must be an existing, non-voided Event. A correction is one atomic mutation that voids the target, optionally sets its `void_reason`, and creates the complete corrected Event with `corrects_event_uuid`. The corrected Event keeps the target's Asset; its Schedule links, date, Runtime, title, and notes may change.
11. **Hard delete.** A Schedule may be hard-deleted only when no Event, including a voided Event, references it. Otherwise it can only be disabled.
12. **Observed timestamps.** `recorded_at` and `voided_at` are the observed UTC operation time, never user input. They are never clamped, for example to `max(now, recorded_at)`, and a clock earlier than a target's `recorded_at` never rejects a void or correction.
13. **Normalization.** Required text is trimmed and rejected when empty. Optional text is trimmed, and an empty result becomes `null`. Decimals are handled as described under canonical representations. `schedule_uuids` is normalized to a deterministic order.
14. **Runtime is read-only.** No Maintenance mutation writes Asset Runtime.
15. **Semantic UX guards.** Before the first Event references a Schedule with an `initial_anchor`, the person is told that saving locks the starting point permanently. Before an interval-type removal destroys a locked baseline component, the person explicitly confirms that the starting point is removed permanently and cannot be set again. When a new or corrected Event lands on a date that already has another relevant Event for the Schedule, the person is told that the Runtime starting point becomes unknown and that a mistaken duplicate can be corrected or voided.

## Idempotent replay

Replay equality compares request-determined business fields after canonical normalization:

- Runtime values are compared numerically as Decimals.
- Text is compared after normalization; `null` equals `null`.
- UUIDs are compared as canonical strings.
- `schedule_uuids` is compared as a set, not in caller order.

Generated timestamp values (`recorded_at`, `voided_at`) never take part in equality. The semantic state they encode does: whether an Event is active or voided and whether it has a correcting Event are part of the comparison. No persisted metadata is needed.

| Operation | Replay success requires | Otherwise |
|---|---|---|
| Record Event | The same `event_uuid` exists, its business fields equal the request, it is still active (`voided_at` and `void_reason` are `null`), and no Event corrects it. | Conflict, fail closed. This includes an Event that was later voided or corrected. |
| Correct Event | The corrected `event_uuid` exists, its business fields equal the request, its `corrects_event_uuid` is the requested target, the target is voided with the requested `void_reason`, and the corrected Event is still active with no correcting Event. | Conflict, fail closed. |
| Void Event | The target is voided, no Event corrects it, and its normalized `void_reason` equals the request. | Conflict, fail closed. |
| Create Schedule | The same `schedule_uuid` exists and the complete persisted Schedule equals the requested final state. | Conflict, fail closed. |
| Hard-delete Schedule | May succeed as a no-op only when the implementation can prove the replay without persisted metadata. | Fail closed. |

## Effective source and anchors

The effective anchor is derived on every projection and never persisted.

```text
relevant = non-voided Events whose schedule_uuids contain the Schedule
           (the Schedule's enabled state does not matter)

if relevant is not empty:
    D = max(performed_date) over relevant
    G = the Events in relevant with performed_date == D

Event group source:
    calendar anchor = D
    runtime anchor  = the only Event's runtime_seconds if |G| == 1 (null -> UNKNOWN)
                      UNKNOWN if |G| > 1
```

Source selection, with `A = initial_anchor`:

| Case | Condition | Source |
|---|---|---|
| No baseline | `A` is `null` and `relevant` is empty | calendar UNKNOWN, Runtime UNKNOWN |
| No baseline | `A` is `null` and `relevant` is not empty | Event group |
| Dated baseline | `A.date` is set and `relevant` is empty | `A` |
| Dated baseline | `D >= A.date` | Event group (same date: the Event wins) |
| Dated baseline | `D < A.date` | `A` |
| Date-less Runtime baseline, `rB = A.runtime_seconds` | `relevant` is empty | `A` (calendar UNKNOWN, Runtime `rB`) |
| Date-less Runtime baseline | every Runtime in `G` is known and `>= rB` | Event group |
| Date-less Runtime baseline | every Runtime in `G` is known and `< rB` | `A` (calendar UNKNOWN, Runtime `rB`) |
| Date-less Runtime baseline | otherwise (an unknown Runtime in `G`, or a mix) | calendar UNKNOWN, Runtime UNKNOWN |

A date-less `initial_anchor` always has a known `runtime_seconds`, because at least one component must be known.

Rules:

- **Same source.** The calendar and Runtime anchor components always come from the same source. When the source is `A`, they are `A.date` and `A.runtime_seconds`. A missing Runtime is never filled from an older Event, from `initial_anchor`, or from the current canonical Runtime.
- **Backdated Events.** A backdated Event older than the current effective source enters history but never moves the effective anchor backward, whether the newer source is another Event or a dated `initial_anchor`.
- **Same-day ambiguity.** When the latest date has more than one relevant Event, the calendar anchor is that date and the Runtime anchor is UNKNOWN. There is no tie-breaker: not `recorded_at`, not Runtime magnitude, not UUID, not list order, and not a sequence field, even when the Runtime values are equal.
- **Void and correction.** Source selection runs again over the remaining non-voided Events. When all linked Events are voided, `initial_anchor` can be the effective source again. This never unlocks the baseline:

  ```text
  baseline lock    = every historical Event reference, including voided Events
  effective source = dated or date-less baseline + non-voided Event history, by the rules above
  ```

- **Interval type added to a locked Schedule.** The new dimension's anchor can come only from the winning source under the same-source rule; otherwise it is UNKNOWN.

### Runtime contradiction safety

History is never corrected automatically, and nothing is voided on the person's behalf. These rules only make the Runtime projection UNKNOWN; a known calendar component stays known.

- **Rule A.** If the current canonical Runtime is known and the effective Runtime anchor is greater than it, the Runtime projection is UNKNOWN.
- **Rule B.** If the winning dated source (one Event, or a dated `initial_anchor`) has a known Runtime `rW`, and a relevant Event dated on or before the winner's date has a known Runtime greater than `rW`, the Runtime projection is UNKNOWN.
- **Rule B′.** If a date-less Runtime `initial_anchor` wins with `rB`, and any relevant Event has a known Runtime greater than `rB`, the Runtime projection is UNKNOWN.

## Due state

Per condition, against its threshold:

```text
before threshold   -> OK
equal to threshold -> DUE
past threshold     -> OVERDUE
missing or unsafe  -> UNKNOWN
```

A Schedule with both intervals is due by whichever comes first. The combined state is the maximum of the condition states in the order `OK < UNKNOWN < DUE < OVERDUE`; for example, `DUE` with `UNKNOWN` is `DUE`, and `OVERDUE` with `UNKNOWN` is `OVERDUE`. A disabled Schedule has no active due projection. A Schedule of an archived Asset has no active due projection either; see [Archived Assets](#archived-assets). "Today" is Home Assistant's configured local civil date, `dt_util.now().date()`.

A preparation reminder is active only from `lead_days` before a known calendar due date. It never changes the due state or the due calculation, and it must not suggest that time remains once the Schedule is `DUE` or `OVERDUE`.

### Future calendar anchor

Added by the projection erratum of 2026-09-26; the original freeze did not state this rule.

If the effective calendar anchor is later than Home Assistant's current local civil date, the calendar projection is UNKNOWN: there is no calendar due date, and the calendar condition state is UNKNOWN. An anchor equal to today is not in the future and is projected normally.

This is a defensive projection rule. A future anchor can exist without Store corruption: an Event is accepted while the Home Assistant clock is wrongly ahead, and the clock is later corrected. Therefore:

- The Store stays valid and loadable. Load-time validation never compares persisted dates with the current date.
- The Event or `initial_anchor` is not changed, voided, or re-dated, and effective-source selection is unchanged: the future date remains the selected source.
- The calendar projection stays UNKNOWN until the anchor is no longer in the future or the history is corrected.
- The Runtime projection is not affected by this rule and can still be known under its own rules, including Rules A, B, and B′.
- The combined state still uses the order `OK < UNKNOWN < DUE < OVERDUE`.
- A preparation reminder is UNKNOWN, because the calendar due date is unknown.

The mutation rule that rejects a user-entered future date is separate and unchanged.

## Calendar arithmetic

Due dates use civil-date arithmetic, never timestamps or 86 400-second days.

| Unit | Rule | Example |
|---|---|---|
| `days` | Civil days | |
| `months` | Target month; the day is clamped to the last valid day of that month | `2026-01-31 + 1 month = 2026-02-28` |
| `years` | Semantic years, equivalent to 12·N months; the stored unit stays `years` | `2028-02-29 + 1 year = 2029-02-28`; `2028-02-29 + 4 years = 2032-02-29` |

## Persisted versus derived

| Persisted | Derived, never persisted |
|---|---|
| Schedule configuration: name, enabled state, intervals, preparation reminder | Effective source and effective anchor |
| `initial_anchor` | Due dates, Runtime thresholds, and due state |
| Event historical facts, including the Runtime snapshot | Preparation-reminder activity |
| Void and correction metadata | Baseline lock and hard-delete eligibility |
| | Correction chain and void kind (manual or correction) |
| | Replay and idempotency decisions |
| | Runtime-based due-date forecasts (a later extension) |

There are no lock flags, projection caches, sequence numbers, Runtime epochs, Schedule tombstones, correction patches, or idempotency or transaction metadata in the Store.

## Archived Assets

Added by the Archive coordination of 2026-09-26. The persisted Maintenance record shapes and load invariants are unchanged, and no `archived` field is added to Maintenance records. Archive state is only `asset.archived_at`, defined in [Asset Archive and Store 4.1 frozen architecture](asset-archive-store-v4.md).

### Projection

- A Schedule that belongs to an archived Asset has no active due projection and no active preparation projection.
- This is separate from `enabled == false`. The persisted `enabled` value is unchanged by Archive and Restore.
- Future Home Assistant active-projection entities represent it as unavailable or no active projection, never as `off`. The entities stay registered.
- Restore resumes the normal projection from the unchanged persisted Schedule and Event history. A Schedule may immediately project `OVERDUE`.

### Mutation boundary

Replay is checked first, before the archive guard, as in mutation rule 3. After replay, for an archived Asset:

| Allowed | Blocked |
|---|---|
| Void Event | Create Schedule |
| Correct Event (it may atomically create the corrected Event under the correction model above) | Edit Schedule current configuration |
| | Set baseline |
| | Add or remove interval |
| | Enable or disable |
| | Hard-delete Schedule |
| | Record Event (ordinary new Event) |

Void and Correct Event are corrections of already-persisted historical facts, which Archive does not block; every other operation is current management. A future requirement that changes this table needs an explicit review.

## Migration and version boundary

- Store 3.1 migrates to Store 4.1. The Maintenance part of the migration only adds empty `maintenance_schedules` and `maintenance_events` mappings (`add_maintenance_collections`). All Store 3.1 data is preserved; the Archive part adds `archived_at = null` to every Asset. The complete pipeline, including the Store 3.1 Asset and Purchase source preflight, is in [Asset Archive and Store 4.1 frozen architecture: Migration](asset-archive-store-v4.md#migration).
- Migration never creates Schedules, Events, or baselines, and never infers performed maintenance from Purchase, installation, Runtime, or Lifecycle data. Maintenance for existing Assets starts empty.
- The migration result is validated as a complete Store before it is saved or published, and the saved result is read back and verified.
- The existing fail-closed version handling is kept: a wrong minor within the same major is rejected with the project's own error, never with `NotImplementedError`, which Home Assistant would treat as a signal to load the data unchanged. Downgrading from Store 4.1 to a 0.7.x release is unsupported; restore a backup instead.
- Archive preserves the Asset record, identity, and history, so every Maintenance reference to an existing Asset remains valid. Permanent deletion is 0.9.x and out of scope here.

## Open items

None for the persisted schema. The Store version-number assignment that was open at the Maintenance freeze was resolved on 2026-09-26: Store 4.1.
