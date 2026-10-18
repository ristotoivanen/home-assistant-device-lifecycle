# Device Lifecycle 0.9.0 — Independent AI review bundle

**STATUS: WORKING DRAFT — NOT APPROVED / NOT FROZEN.**

## How to use

1. Download the **complete GitHub repository ZIP pinned to one immutable commit SHA**, supplied by the project owner. It contains the actual 0.8.0 code, tests, frozen architecture contracts, the 0.9.0 draft, and this review kit.
2. Attach that **same ZIP** separately to Reviewer A, Reviewer B and Reviewer C. Paste the contents of the corresponding PROMPT-A/B/C file as the message. Use genuinely different AI models where practical (Claude, Gemini, Codex, for example).
3. Do **not** show any reviewer another reviewer's conclusions before **all three initial reports are saved**. The initial reports must remain unedited.
4. Each reviewer returns one standalone Markdown report with source/line references, concrete falsifying cases, explicit uncertainty, and its actual model/version.
5. After initial reports are sealed, use PROMPT-D for cross-challenge. Reconcile findings and blocking architectural decisions through owner adjudication. No automatic design freeze.

**Review sources:** docs/0.9.0-master-architecture-v0.1.md, docs/reviews/0.9.0-independent-review-protocol-v0.1.md, ARCHITECTURE.md, docs/asset-archive-store-v4.md, docs/maintenance-store-v4-schema.md, docs/0.8.0-post-release-validation.md, all relevant custom_components/device_lifecycle code and tests.

**Do not use a moving branch URL as the review input.** The archive must be pinned by its full commit SHA. The README and review prompts themselves are part of the same ZIP, but there are **no initial model reviews** in this package.

Only documentation was changed from production baseline; no integration code or tests were edited for this review kit. The GitHub PR remains Draft.

## Output naming

review-A-[actual-model].md, review-B-[actual-model].md, review-C-[actual-model].md. Later: cross-challenge-[actual-model].md. Deliver files as attachments if supported; otherwise return full raw Markdown.

**The design belongs to 0.9.0, followed by 0.9.x stabilization and 1.0.0 submission readiness. HACS/default approval is external and not a prerequisite to publishing 1.0.0.**