# BL-04 review and verification record

Recorded 2026-10-17 for [Draft PR #23](https://github.com/ristotoivanen/home-assistant-device-lifecycle/pull/23).
This is a documentation-only follow-up to the accepted implementation. The
2026-10-15 authorization paragraph in the [implemented contract](../0.8.1-runtime-source-identity.md)
records the original local implementation scope. Subsequent owner instructions
authorize the existing Draft PR, its documentation and hosted CI. They do not
authorize Ready for review, merge, release, tags or production operations.

## Exact accepted source

| Object | Git SHA |
| --- | --- |
| BL-03 baseline | `ac171bdc5df66d137adb7d77bffd1a3b8d1d45e7` |
| Permanent RED commit | `d283544649e21d7e27c706594ba880979127c080` |
| Accepted implementation commit | `1c2d29ffc91457e3d9af7bbfbc309edd752efa59` |
| Accepted implementation tree | `84a90a6c6e4bc9d83271d3bf39cb36c8d75efb42` |

The v6 review froze 157 file contents while HEAD was the RED commit. The final
implementation commit records those same contents. All 157 working files and
implementation-commit blobs were independently compared with the preserved
freeze during this handoff: zero mismatches. This additional review record does
not change any accepted file, production source, test, translation or workflow.

Preserved evidence SHA-256 values:

- `source-freeze-v6.json`: `bc09f220f16d559af764ea09a5c6a15982cba741bb44c79773071f92dd4a902d`
- `complete-final.patch`: `7db9be78487f701b90f15504a71bedfacb041c2569da21350ae35c913fdd1377`
- `independent-acceptance-v6/REVIEW.md`: `1f63b2028c2333ae7968ea286215ea665be5b80e8be3a12f1747293fb2712b9e`

The original full logs, XML, rejected candidates and RED/GREEN packages remain
preserved as owner-held evidence. They are not all published in this repository.

## Defect, authorization and recovery

The baseline trusted the State at a mutable `entity_id`. A replacement could
inherit that address and incorrectly increase the original Asset's canonical
Runtime from 1010 to 1040 seconds. The permanent RED commit reproduced 36 intended
failures with eight passing controls; 32 failures were numeric attribution errors.
The corrected original witness remains at 1010 seconds.

Runtime ConfigSubentry data now stores independently versioned source consent,
including a fresh authorization nonce, registry-record ID and logical origin,
owner, subentry and device guards. Missing, malformed, unknown-version or revoked
consent cannot authorize the current occupant of an address. Identity checks guard
setup, projection, events and lifecycle interval sealing. A detected gap is
latched and persisted; a returning row cannot silently resume observation.

Legacy, replaced and renamed sources require explicit configuration confirmation.
Registry-less sources cannot be durably authorized. Committed Runtime and already
sealed valid pending deltas are retained; an uncertain active tail contributes
zero. Reconfiguration checkpoints the old writer and revalidates both selection
and consent after awaits. Failed writes retain the form and retry state.
Cancellation releases observers/reservations. English and Finnish forms and the
[README recovery guidance](../../README.md#runtime-tracking) describe these rules.

Canonical Store 4.1, ConfigEntry v4, integration version, Asset ownership/UUIDs,
Runtime initialization provenance and BL-03 persistence fences are unchanged.
`storage.py`, `models.py`, `runtime_identity.py`, `manifest.json`, the BL-03 ADR
and both original persistence suites are byte-identical to the baseline.

## Local execution and independent acceptance

The preserved final v6 matrix reports these local executions, separately from
hosted CI:

| Home Assistant | Full suite | BL-04 cases | Combined coverage |
| --- | --- | --- | --- |
| 2026.8.0 | 2995 PASS, 1 expected SKIP | 152 PASS | 98.72% |
| 2026.9.3 | 2996 PASS | 152 PASS | 98.71% |
| 2026.10.1 | 2996 PASS | 152 PASS | 98.72% |

The minimum-version skip is the existing child-device resolution case because
HA 2026.8 has no child-device API. There are no BL-04 skips. Each full suite also
passes the unchanged 59 BL-03 safety and 37 persistence lifecycle cases and runs
14 child OS checks, not added again to the suite totals. Local Ruff baseline,
dashboard, dependency, coverage and whitespace gates passed.

The final independent v6 disposition is **ACCEPT** for the exact freeze. On HA
2026.9.3 the reviewer independently executed 280 repository checks and 80 external
probes, with zero failures, errors or skips, and verified all 157 hashes before
and after execution. It inspected the full three-version matrix without claiming
to rerun all three full suites. Earlier rejected creation, ABA, presentation,
stale-authorization and initial-selection cases (G1-G5) remain preserved; their
permanent corrected-behavior regressions pass in v6.

## Owner review

The full [owner static review, comment 6230450388](https://github.com/ristotoivanen/home-assistant-device-lifecycle/pull/23#issuecomment-6230450388)
identifies the accepted implementation commit/tree and has disposition
**APPROVE WITH FINDINGS, for the PR and CI stage**. It is an issue comment,
not a claim of a formal GitHub reviewer APPROVE submission.

The report was retrieved using GitHub CLI's `.body` output. The SHA-256 of that
retrieved text, including its output newline, is
`551cbf10e2ed0c1716da5c7bdca39ccab46f5caaba416e59894377aec978cec4`.
Its bytes have not been compared with the original downloaded report. The
original report is preserved; byte equivalence is not asserted.

OR-BL04-01 accepts the durable identity/revocation design. OR-BL04-02 accepts
accounting with an explicit residual scheduling limitation. OR-BL04-03 accepts
consent and conservative availability costs. OR-BL04-04 retains the documented
persistence, backup, downgrade and physical-identity risks. OR-BL04-05 requires
hosted verification before a separate owner merge decision. The static reviewer
ran eight isolated synthetic status cases and disclosed that the full HA matrix
and original 157-file comparison were supplied evidence, not its own reruns.

## Hosted verification of the implementation

The following existing PR-triggered runs were inspected, without rerunning them:

| Workflow | Run | Result |
| --- | --- | --- |
| Quality gates | [39008304556](https://github.com/ristotoivanen/home-assistant-device-lifecycle/actions/runs/39008304556) | PASS: HA 2026.8.0, 2995 passed/1 expected skip; HA 2026.9.3, 2996 passed; 98.72% coverage; Ruff/dashboard/diff PASS |
| Validate with HACS | [39008304540](https://github.com/ristotoivanen/home-assistant-device-lifecycle/actions/runs/39008304540) | PASS: all nine HACS checks |
| Validate with hassfest | [39008304517](https://github.com/ristotoivanen/home-assistant-device-lifecycle/actions/runs/39008304517) | PASS |

All run metadata identifies implementation head `1c2d29ffc91457e3d9af7bbfbc309edd752efa59`.
Quality/hassfest checkout logs identify GitHub's synthetic PR merge commit
`53f36995503da40f04985badc203dd32cdeb8a25`; its tree is exactly the accepted
`84a90a6c6e4bc9d83271d3bf39cb36c8d75efb42`. Every job and listed workflow step
completed successfully. The single pytest skip above is not a skipped job.

Non-failing hosted warnings/notices include Node 20 actions being forced onto
Node 24, Node `punycode`/`url.parse()` deprecations, and the announced
`ubuntu-latest` migration to Ubuntu 26 starting 2026-10-19. Ruff reports reduced
baseline debt informationally. No test or production changes suppress these.

These run IDs predate this documentation-only follow-up. CI for the final PR
head, including the separately dispatched latest-stable HA compatibility result
and its resolved version, must be assessed from [PR #23 checks](https://github.com/ristotoivanen/home-assistant-device-lifecycle/pull/23/checks)
and the final handoff. A previous local PASS is not a hosted CI PASS.

## Remaining limits and owner boundaries

Logical registry authorization cannot attest physical hardware when an upstream
integration reuses every guard. Fully erased/offline history or a consistent old
backup cannot reveal an unobserved gap. HA configuration/registry persistence and
canonical Runtime are not one power-loss transaction. Downgrading removes this
protection. Valid pending time stays volatile during persistent disk failure,
without an abrupt-crash loss bound. Conservative legacy/registry-less/rename
pauses are accepted availability costs. Arbitrary scheduling, hardware, full
production bootstrap and abrupt power loss are not exhaustively tested.

Future HA `RestoreStateData.last_states` compatibility remains separate BL-05
work. A latest-stable failure must be reported and investigated, not waived or
silently folded into BL-04. BL-01, BL-02, BL-05 and BL-06 and final overall
regressions remain open release concerns in the owner review. This record does
not close them or authorize release. PR #23 remains Draft for a separate owner
review and merge decision.
