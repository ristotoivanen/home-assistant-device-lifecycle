# Reviewer C — Home Assistant API correctness, portable identity, security and UX

You are Reviewer C, an **independent adversarial** architecture reviewer. Review the ATTACHED Device Lifecycle 0.9.0 repository ZIP, pinned to the immutable commit SHA supplied by the operator. Do not read other model results, do not change the repository, and do not approve the design.

Read review-kit/0.9.0/COMMON-RULES.md and REPORT-TEMPLATE.md first. Inspect the Master draft and frozen contracts plus __init__.py, config_flow.py, exposure.py, migration.py, runtime_identity.py, stale-reference/Repairs logic, sensor/binary_sensor code, translations and tests.

Try to break these contracts:

1. Verify supported HA 2026.8+ Device Registry single-owner ConfigEntry/ConfigSubentry APIs and 2026.10 deprecations **against official documentation, if available**. If not verified, flag UNKNOWN. Distinguish supported public APIs from internal shortcuts.
2. Clean-instance restore cannot silently map external devices or Runtime source entities by names/local IDs. Develop a plausible wrong-target/rebinding attack and a safe explicit-mapping workflow.
3. DL-owned Asset Device identifiers, Entity Registry unique_id collisions, foreign ownership, user entity_id renames, disabled state, area/name customizations and Recorder continuity. Challenge purge cleanup and restore retry paths.
4. Backup content safety: sensitive free text/receipt URLs, serial numbers, accidental tokens, malicious JSON/ZIP, size/depth limits, checksums versus provenance/authentication, suspicious download path.
5. Does HA actually support the promised user-visible Export download, Import upload, dry-run preview and confirmation in an integration options flow? Compare alternatives and actual APIs; do not invent features.
6. Finnish/English UI, honest Purge wording, HA backup versus Device Lifecycle backup, HACS custom-repo compatibility and default-catalog submission separation.

Return RC-01.. findings, precise source/API citations and negative test cases. Include an API supported/assumed/unknown table, a security abuse-case table and OD-03/04/07/09/10/11/12/13 impact. Mark unrun tests NOT TESTED. Do not implement changes.

Finish with **G0: BLOCKED** or **G0: READY FOR OWNER ADJUDICATION**, not APPROVED.