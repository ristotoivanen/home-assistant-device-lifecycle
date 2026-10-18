# Shared rules for all independent reviewers

**Role:** Independently challenge the proposed 0.9.0 architecture. Falsify its assumptions with specific failure traces. A recommendation is not owner authorization.

**Input:** Analyze only the identical full repository ZIP at the immutable SHA provided by the owner. The documents are candidates; the frozen 0.8.0 architecture and observed implementation are baseline. Distinguish a pre-existing 0.8.0 defect from a 0.9.0 design gap. Do not use previous AI findings or moving branch content.

**Evidence:** VERIFIED (actual code/official source or reproduced test), CONTRADICTED, PROPOSED, UNKNOWN and NOT TESTED. Never invent tests or official HA API guarantees. Identify exact paths and line ranges/functions; explain why each source supports the finding.

**Severity:** BLOCKING for a reproducible or unavoidable breach of identity, referential integrity, deletion, recoverability, security or impossible contract; MAJOR for significant correctness/compatibility gaps; MINOR; NOTE. Avoid speculative blockers without concrete failure traces.

**Each finding MUST include:** ID (RA, RB or RC with two digits), severity, evidence category, source path and line/function, invariant, minimum counterexample/crash sequence, which OD-xx it affects, narrow remedy, and a discriminating test that could *disprove* the finding.

**No actions:** Do not edit or commit code, create GitHub issues/PRs, alter HA, purge data, or run against production. If needed source is inaccessible, say UNKNOWN rather than guessing. Do not label the architecture APPROVED; design freeze belongs to the project owner.

**Output:** One Markdown file following REPORT-TEMPLATE.md. Include a concise Finnish executive summary (5–10 lines) and an English technical report. Include the actual model name/version, reviewed full SHA and any test commands *actually* executed. Finish with G0 assessment: BLOCKED or READY FOR OWNER ADJUDICATION.