# Cross-challenge round — ONLY AFTER A/B/C initial reviews are sealed

Do not use this prompt in an initial review. Input: the same immutable source ZIP, all three initial review reports preserved unchanged, and optionally the draft decision register.

You are an adversarial reviewer of the *reviewers' findings*. For each blocking/major item:
- Locate the exact original evidence and attempt to disprove the claim with code, official HA APIs or a falsifying test.
- Separate factual contradiction from design preference, duplicate root causes and cascade effects.
- Construct at least two **new cross-domain** failure traces involving persistence ↔ Purge ↔ external HA registry.
- Preserve unresolved and rejected claims with reasons, do not erase or silently downgrade them.
- Propose issue/decision mapping OD-xx and actionable tests before design freeze.

Write a standalone cross-challenge Markdown report with source anchors and findings RX-01.., and actual reviewed SHA, model/version, unknowns and NOT TESTED. No code edits and no architecture approval.