"""Ruff baseline regression gate (0.7.2 WP9 / 072-12).

Runs Ruff and compares the resulting (repository-relative file path, rule
code) -> finding-count mapping against a small, version-controlled
baseline in ``ruff_baseline.json``.

Rules enforced:
  - A (file, rule) pair already in the baseline may have AT MOST its
    recorded count. Fewer is fine (the debt shrank). MORE fails.
  - A (file, rule) pair Ruff reports that is NOT in the baseline at all
    fails — whether that's a brand-new rule in an already-listed file, or
    a finding in a file with no prior debt.

This intentionally cannot be satisfied by an unlimited per-file/per-rule
ignore: growing a *pre-existing* debt item still fails CI, which a plain
`ruff.toml` per-file-ignore cannot express. The baseline identifies debt
by file path + rule code only, never by line number, so unrelated edits
elsewhere in a file do not spuriously shift or hide existing entries.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = REPO_ROOT / "ruff_baseline.json"


def _current_counts() -> dict[tuple[str, str], int]:
    """Return {(file, rule): count} for Ruff's current findings."""
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--output-format=json", "."],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    # Ruff exits 0 (no findings) or 1 (findings present); anything else is
    # a real tool/config error and must not be silently treated as "clean".
    if result.returncode not in (0, 1):
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(f"ruff check exited unexpectedly: {result.returncode}")

    findings = json.loads(result.stdout or "[]")
    counts: dict[tuple[str, str], int] = {}
    for finding in findings:
        path = Path(finding["filename"]).resolve().relative_to(REPO_ROOT).as_posix()
        key = (path, finding["code"])
        counts[key] = counts.get(key, 0) + 1
    return counts


def _baseline() -> dict[tuple[str, str], int]:
    """Return {(file, rule): allowed_count} from the tracked baseline."""
    entries = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return {(entry["file"], entry["rule"]): entry["count"] for entry in entries}


def main() -> int:
    baseline = _baseline()
    current = _current_counts()

    regressions = [
        f"{file}: {rule} has {count} finding(s), baseline allows {baseline.get((file, rule), 0)}"
        for (file, rule), count in sorted(current.items())
        if count > baseline.get((file, rule), 0)
    ]

    if regressions:
        print("Ruff baseline regression gate FAILED:")
        for line in regressions:
            print(f"  - {line}")
        print(
            "\nA (file, rule) pair may not exceed its recorded ruff_baseline.json "
            "count. Fix the new finding(s) instead of raising the baseline."
        )
        return 1

    shrunk = [
        f"{file}: {rule} baseline {allowed} -> now {current.get((file, rule), 0)}"
        for (file, rule), allowed in sorted(baseline.items())
        if current.get((file, rule), 0) < allowed
    ]
    if shrunk:
        print("Some baseline debt has shrunk since it was recorded (informational):")
        for line in shrunk:
            print(f"  - {line}")

    print("Ruff baseline regression gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
