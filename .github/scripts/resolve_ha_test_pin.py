"""Resolve pytest-homeassistant-custom-component releases to Home Assistant.

Used by the advisory latest-HA compatibility workflow (0.7.3 WP-05).

pytest-homeassistant-custom-component follows Home Assistant closely and
also publishes releases pinned to Home Assistant betas, so installing the
newest release is not the same as testing the newest *stable* Home
Assistant. This script walks the releases newest first and prints the first
one whose exact `homeassistant==` pin is a final X.Y.Z release:

    python .github/scripts/resolve_ha_test_pin.py
    -> "<package version> <Home Assistant version>"

Given a package version, it prints that release's pin instead, which the
workflow uses to report the supported baseline next to the latest one:

    python .github/scripts/resolve_ha_test_pin.py 0.13.366
    -> "0.13.366 2026.9.3"

Only the standard library is used so it runs before any dependency install.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request

PACKAGE = "pytest-homeassistant-custom-component"
PYPI = f"https://pypi.org/pypi/{PACKAGE}"
NUMERIC_VERSION = re.compile(r"^\d+(\.\d+)*$")
STABLE_HA_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
# Home Assistant betas rarely span more than a handful of package releases;
# stop instead of walking the whole history if something is badly wrong.
MAX_CANDIDATES = 30


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def _ha_pin(version: str) -> str | None:
    """Return the exact Home Assistant version one package release pins."""
    requires = _get(f"{PYPI}/{version}/json")["info"].get("requires_dist") or []
    for requirement in requires:
        name, separator, pinned = requirement.split(";", 1)[0].partition("==")
        if separator and name.strip() == "homeassistant":
            return pinned.strip()
    return None


def _newest_first(releases: dict[str, list[dict]]) -> list[str]:
    """Return published, non-yanked numeric releases, newest first."""
    published = [
        version
        for version, files in releases.items()
        if NUMERIC_VERSION.match(version)
        and files
        and not all(file.get("yanked") for file in files)
    ]
    return sorted(
        published,
        key=lambda version: tuple(int(part) for part in version.split(".")),
        reverse=True,
    )


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        print("usage: resolve_ha_test_pin.py [PACKAGE_VERSION]", file=sys.stderr)
        return 2

    if argv:
        pin = _ha_pin(argv[0])
        if pin is None:
            print(f"{PACKAGE} {argv[0]} has no homeassistant== pin", file=sys.stderr)
            return 1
        print(argv[0], pin)
        return 0

    candidates = _newest_first(_get(f"{PYPI}/json")["releases"])
    for version in candidates[:MAX_CANDIDATES]:
        pin = _ha_pin(version)
        if pin is not None and STABLE_HA_VERSION.match(pin):
            print(version, pin)
            return 0

    print(
        f"No {PACKAGE} release among the newest {MAX_CANDIDATES} pins a "
        "stable Home Assistant version",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
