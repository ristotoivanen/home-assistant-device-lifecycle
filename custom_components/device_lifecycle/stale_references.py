"""Advisory Repairs issues for stale external Home Assistant device references.

Device Lifecycle stores Device Registry IDs it does not own, and another
integration can remove those devices at any time. A missing device is an
unresolved reference that stays stored for the person to repair (see
`migration._linkable`). This module only reports it in Home Assistant Repairs:

    canonical Asset `ha_device_refs`
    -> collect references
    -> resolve each one through the Device Registry
    -> derive the desired stale-reference issue set
    -> reconcile only the issues this module owns

It never writes the Store, a config subentry, the Device Registry or the
Entity Registry. Purchase and Runtime configurations are not read: every
device they list is projected onto an Asset primary relationship during
reconciliation, so the Asset's primary issue already represents it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .models import AssetData
from .storage import AssetStoreManager

ReferenceRole = Literal["primary", "related"]

ROLE_PRIMARY: ReferenceRole = "primary"
ROLE_RELATED: ReferenceRole = "related"
_ROLE_ORDER: dict[str, int] = {ROLE_PRIMARY: 0, ROLE_RELATED: 1}

TRANSLATION_KEYS: dict[ReferenceRole, str] = {
    ROLE_PRIMARY: "stale_primary_device",
    ROLE_RELATED: "stale_related_device",
}

_DIGEST_LENGTH = 32
_ISSUE_ID_PATTERN = re.compile(
    r"stale_device_(?P<role>primary|related)_"
    r"(?P<asset_uuid>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})_"
    rf"(?P<digest>[0-9a-f]{{{_DIGEST_LENGTH}}})"
)

# Device Registry actions that can change whether a stored ID resolves. An
# update keeps the device ID, so it cannot turn a reference stale or back.
_RESOLUTION_ACTIONS = frozenset({"create", "remove"})


@dataclass(frozen=True)
class StoredDeviceReference:
    """One canonical Asset relationship to an external Home Assistant device."""

    asset_uuid: str
    asset_id: str
    asset_name: str
    role: ReferenceRole
    device_id: str


@dataclass(frozen=True)
class StaleReferenceIssue:
    """One desired Repairs issue for one unresolved Asset relationship."""

    issue_id: str
    translation_key: str
    asset_id: str
    asset_name: str

    @property
    def translation_placeholders(self) -> dict[str, str]:
        """Name the Asset for people; the device ID is never shown."""
        return {"asset_id": self.asset_id, "asset_name": self.asset_name}


@dataclass(frozen=True)
class ParsedIssueId:
    """The components of one owned stale-reference issue ID."""

    role: ReferenceRole
    asset_uuid: str
    device_digest: str


def collect_asset_device_references(
    assets: Iterable[AssetData],
) -> tuple[StoredDeviceReference, ...]:
    """Return every stored primary and related reference in a fixed order.

    Only canonical Store relationships are read. Nothing is resolved here, so
    an unresolved reference is collected exactly like a valid one.
    """
    references = [
        StoredDeviceReference(
            asset_uuid=asset["asset_uuid"],
            asset_id=asset["asset_id"],
            asset_name=asset["name"],
            role=reference["role"],
            device_id=str(reference["device_id"]),
        )
        for asset in assets
        for reference in asset.get("ha_device_refs", [])
    ]
    return tuple(
        sorted(
            references,
            key=lambda item: (item.asset_uuid, _ROLE_ORDER[item.role], item.device_id),
        )
    )


def device_reference_resolves(
    registry: dr.DeviceRegistry,
    device_id: str,
) -> bool:
    """Return whether a stored device ID still resolves in the Device Registry.

    Deliberately the same question the Relationships entity and the Asset
    management UI ask, so Repairs never calls a device missing that they
    name. On 2026.9+ a child device resolves, and on both supported releases
    a pre-migration composite ID resolves while any of its split devices
    remain. The stricter entity-linking test in `migration` answers a
    different question and is not used here.
    """
    return registry.async_get(device_id) is not None


def _device_digest(device_id: str) -> str:
    """Return the fixed-length stand-in for a stored device ID."""
    return hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]


def stale_reference_issue_id(
    asset_uuid: str,
    role: ReferenceRole,
    device_id: str,
) -> str:
    """Return the deterministic issue ID for one Asset relationship.

    The stored device ID is only validated as a non-empty string, so it is
    hashed to keep every owned ID inside one closed grammar.
    """
    issue_id = f"stale_device_{role}_{asset_uuid}_{_device_digest(device_id)}"
    if parse_stale_reference_issue_id(issue_id) is None:
        raise ValueError(f"Cannot build a stale-reference issue ID for {role}")
    return issue_id


def parse_stale_reference_issue_id(issue_id: str) -> ParsedIssueId | None:
    """Parse an issue ID owned by this module, or return None for any other."""
    match = _ISSUE_ID_PATTERN.fullmatch(issue_id)
    if match is None:
        return None
    return ParsedIssueId(
        role=match["role"],  # type: ignore[arg-type]
        asset_uuid=match["asset_uuid"],
        device_digest=match["digest"],
    )


def is_owned_stale_reference_issue(domain: str, issue_id: str) -> bool:
    """Return whether an issue belongs to this module.

    Only the domain and the issue ID are durable: an issue that is not
    persistent comes back after a restart with neither translation key nor
    data, so neither can decide ownership.
    """
    return domain == DOMAIN and parse_stale_reference_issue_id(issue_id) is not None


def desired_stale_reference_issues(
    references: Iterable[StoredDeviceReference],
    registry: dr.DeviceRegistry,
) -> dict[str, StaleReferenceIssue]:
    """Return one issue per reference whose device no longer resolves."""
    issues: dict[str, StaleReferenceIssue] = {}
    for reference in references:
        if device_reference_resolves(registry, reference.device_id):
            continue
        issue_id = stale_reference_issue_id(
            reference.asset_uuid,
            reference.role,
            reference.device_id,
        )
        issues[issue_id] = StaleReferenceIssue(
            issue_id=issue_id,
            translation_key=TRANSLATION_KEYS[reference.role],
            asset_id=reference.asset_id,
            asset_name=reference.asset_name,
        )
    return issues


def _owned_issue_ids(hass: HomeAssistant) -> list[str]:
    """Return the IDs of every issue this module owns, in a fixed order."""
    return sorted(
        issue_id
        for domain, issue_id in ir.async_get(hass).issues
        if is_owned_stale_reference_issue(domain, issue_id)
    )


@callback
def async_sync_stale_reference_issues(
    hass: HomeAssistant,
    manager: AssetStoreManager,
) -> None:
    """Make the owned issues match the current canonical references exactly.

    Existing issues are updated in place, which keeps their creation time and
    any dismissal, and Home Assistant only announces an actual change, so an
    unchanged state is a no-op. Only owned issues that are no longer desired
    are deleted.
    """
    desired = desired_stale_reference_issues(
        collect_asset_device_references(manager.assets()),
        dr.async_get(hass),
    )
    for issue in desired.values():
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue.issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=issue.translation_key,
            translation_placeholders=issue.translation_placeholders,
        )
    for issue_id in _owned_issue_ids(hass):
        if issue_id not in desired:
            ir.async_delete_issue(hass, DOMAIN, issue_id)


@callback
def async_track_stale_reference_issues(
    hass: HomeAssistant,
    manager: AssetStoreManager,
) -> CALLBACK_TYPE:
    """Re-derive the owned issues whenever a device appears or disappears.

    The whole set is recomputed rather than the event's device alone: the
    removal of a composite device's last split device carries the split's
    ID, not the stored composite ID.
    """

    @callback
    def _async_device_registry_updated(
        event: Event[dr.EventDeviceRegistryUpdatedData],
    ) -> None:
        if event.data["action"] in _RESOLUTION_ACTIONS:
            async_sync_stale_reference_issues(hass, manager)

    return hass.bus.async_listen(
        dr.EVENT_DEVICE_REGISTRY_UPDATED,
        _async_device_registry_updated,
    )


@callback
def async_delete_stale_reference_issues(hass: HomeAssistant) -> None:
    """Delete every owned issue, for permanent removal of the config entry."""
    for issue_id in _owned_issue_ids(hass):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
