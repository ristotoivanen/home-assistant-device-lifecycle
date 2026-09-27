"""Repairs for Runtime tracking that resolves to an archived Asset.

A restored backup or an older configuration can hold a Runtime
ConfigSubentry whose canonical target is an archived Asset. Setup
quarantines that subentry (no Runtime writer, initialization, or rewrite)
and reports the conflict here: one advisory, non-fixable issue per
quarantined subentry, derived from the Store and the ConfigEntry only.

The exits are restoring the Asset or removing the Runtime configuration;
the next setup then derives no issue and the owned one is deleted. The
person's Ignore state is never consulted, and nothing is ever written to
the Store or a subentry. Unloading keeps the issues, so a reload does not
churn them; only removing the entry deletes them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .storage import AssetStoreManager

TRANSLATION_KEY = "runtime_archived_asset"
_ISSUE_PREFIX = "runtime_archived_asset_"
_ISSUE_ID_PATTERN = re.compile(r"runtime_archived_asset_(?P<subentry_id>\S+)")


def runtime_conflict_issue_id(subentry_id: str) -> str:
    """Return the deterministic issue ID for one quarantined subentry."""
    return f"{_ISSUE_PREFIX}{subentry_id}"


def is_owned_runtime_conflict_issue(domain: str, issue_id: str) -> bool:
    """Return whether an issue belongs to this module, by domain and ID."""
    return domain == DOMAIN and _ISSUE_ID_PATTERN.fullmatch(issue_id) is not None


def _owned_issue_ids(hass: HomeAssistant) -> list[str]:
    return sorted(
        issue_id
        for domain, issue_id in ir.async_get(hass).issues
        if is_owned_runtime_conflict_issue(domain, issue_id)
    )


@callback
def async_sync_runtime_conflict_issues(
    hass: HomeAssistant,
    manager: AssetStoreManager,
    quarantined: Iterable[str],
) -> None:
    """Make the owned issues match the quarantined subentries exactly."""
    desired: set[str] = set()
    for subentry_id in sorted(quarantined):
        asset = manager.quarantined_runtime_asset(subentry_id)
        if asset is None:
            continue
        issue_id = runtime_conflict_issue_id(subentry_id)
        desired.add(issue_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=TRANSLATION_KEY,
            translation_placeholders={
                "asset_name": str(asset.get("name") or asset["asset_id"]),
                "asset_id": asset["asset_id"],
            },
        )
    for issue_id in _owned_issue_ids(hass):
        if issue_id not in desired:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


@callback
def async_delete_runtime_conflict_issues(hass: HomeAssistant) -> None:
    """Delete every owned issue, for permanent removal of the config entry."""
    for issue_id in _owned_issue_ids(hass):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
