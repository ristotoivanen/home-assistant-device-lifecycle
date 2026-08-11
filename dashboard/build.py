#!/usr/bin/env python3
"""Build the checked-in localized Device Lifecycle dashboards."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re


DASHBOARD_DIR = Path(__file__).resolve().parent
SOURCE_PATH = DASHBOARD_DIR / "src" / "dashboard.yaml"
TRANSLATION_DIR = DASHBOARD_DIR / "src" / "translations"
OUTPUT_PATHS = {
    "en": DASHBOARD_DIR / "device-lifecycle-dashboard.en.yaml",
    "fi": DASHBOARD_DIR / "device-lifecycle-dashboard.fi.yaml",
}
TOKEN_PATTERN = re.compile(r"__DL_I18N_SEGMENT_\d{4}__")


def _load_template() -> str:
    """Load the literal dashboard template from its YAML source document."""
    lines = SOURCE_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    try:
        template_index = lines.index("template: |\n")
    except ValueError as err:
        raise ValueError("dashboard source is missing its template block") from err

    template_lines: list[str] = []
    for line in lines[template_index + 1 :]:
        if line == "\n":
            template_lines.append(line)
            continue
        if not line.startswith("  "):
            raise ValueError("dashboard template lines must use two-space indentation")
        template_lines.append(line[2:])
    return "".join(template_lines)


def _build(language: str) -> str:
    """Render one localized ready-to-import dashboard."""
    translation_path = TRANSLATION_DIR / f"{language}.json"
    translation = json.loads(translation_path.read_text(encoding="utf-8"))
    segments = translation.get("segments")
    if not isinstance(segments, dict):
        raise ValueError(f"{translation_path} has no segment mapping")

    output = _load_template()
    expected_tokens = set(TOKEN_PATTERN.findall(output))
    if expected_tokens != set(segments):
        missing = sorted(expected_tokens - set(segments))
        extra = sorted(set(segments) - expected_tokens)
        raise ValueError(
            f"{language} translation token mismatch: missing={missing}, extra={extra}"
        )
    for token, value in segments.items():
        if not isinstance(value, str):
            raise ValueError(f"{language} translation {token} must be text")
        marker = f"{token}\n"
        if output.count(marker) != 1:
            raise ValueError(f"dashboard source must contain {token} exactly once")
        output = output.replace(marker, value)
    if TOKEN_PATTERN.search(output):
        raise ValueError(f"{language} dashboard contains unresolved translations")
    return output


def main() -> int:
    """Write dashboards or verify that checked-in artifacts are current."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify generated dashboards without rewriting them",
    )
    args = parser.parse_args()

    stale: list[str] = []
    for language, output_path in OUTPUT_PATHS.items():
        output = _build(language)
        if args.check:
            if not output_path.is_file() or output_path.read_text(
                encoding="utf-8"
            ) != output:
                stale.append(str(output_path.relative_to(DASHBOARD_DIR.parent)))
        else:
            output_path.write_text(output, encoding="utf-8")
    if stale:
        raise SystemExit(f"generated dashboards are stale: {', '.join(stale)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
