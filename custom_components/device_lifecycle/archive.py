"""Asset Archive state: the Store transform and the Archive load invariants.

Inactive library code for the Store 4.1 upgrade: no production module
imports it yet, and the production Store is still 3.1. The canonical
contract is docs/asset-archive-store-v4.md.

An Asset's Archive state is one field, ``archived_at``: ``None`` while the
Asset is in active/current management, a canonical UTC timestamp while it is
archived. There is no Archive history, reason, or marker anywhere else.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .canonical import CanonicalValueError, parse_canonical_utc
from .const import CONF_DEPLOYMENT_STATE, DEPLOYMENT_STATE_DEPLOYED

ARCHIVED_AT = "archived_at"


class ArchiveCompositionError(ValueError):
    """A Store candidate cannot receive the Archive state."""


class ArchiveValidationError(ValueError):
    """An Asset's persisted Archive state violates a Store 4.1 invariant."""


def _sorted_asset_keys(assets: Mapping[Any, Any]) -> list[Any]:
    """Return Asset map keys in a deterministic order, whatever their type."""
    return sorted(assets, key=str)


def add_asset_archive_state(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of a pre-Archive Store candidate with every Asset active.

    This is only the Archive component of the Store 4.1 upgrade; it does not
    own the upgrade order or the final shape. Every Asset gains
    ``archived_at = None`` and nothing else changes: no Archive state is
    inferred from Lifecycle, Deployment, Runtime, Purchase, Home Assistant
    references, or Maintenance, and no clock is read. The input is never
    modified, and the result shares no mutable data with it.

    It applies exactly once. An Asset that already has ``archived_at``, even
    ``None``, is rejected rather than overwritten.
    """
    if not isinstance(candidate, Mapping):
        raise ArchiveCompositionError("Store candidate must be a mapping")
    if "assets" not in candidate:
        raise ArchiveCompositionError("Store candidate has no assets collection")
    assets = candidate["assets"]
    if not isinstance(assets, Mapping):
        raise ArchiveCompositionError("Store candidate assets is not a mapping")
    for asset_key in _sorted_asset_keys(assets):
        asset = assets[asset_key]
        if not isinstance(asset, Mapping):
            raise ArchiveCompositionError(f"Asset {asset_key} is not a mapping")
        if ARCHIVED_AT in asset:
            raise ArchiveCompositionError(
                f"Asset {asset_key} already contains {ARCHIVED_AT}"
            )

    result = {
        key: deepcopy(value) for key, value in candidate.items() if key != "assets"
    }
    archived_assets: dict[Any, Any] = {}
    for asset_key, asset in assets.items():
        copied = {field: deepcopy(value) for field, value in asset.items()}
        copied[ARCHIVED_AT] = None
        archived_assets[deepcopy(asset_key)] = copied
    result["assets"] = archived_assets
    return {key: result[key] for key in candidate}


def asset_is_archived(asset: Mapping[str, Any]) -> bool:
    """Return whether an already validated Store 4.1 Asset is archived.

    A missing ``archived_at`` is a schema or programming error and raises
    ``KeyError``; it is never read as "active".
    """
    return asset[ARCHIVED_AT] is not None


def validate_asset_archive_state(assets: Mapping[str, Any]) -> None:
    """Validate the Archive state of every Asset of a Store 4.1 payload.

    ``archived_at`` must be present and be ``None`` or a canonical UTC
    timestamp. It is never compared with the current clock, so a timestamp
    that looks like the future is valid. An archived Asset must not be
    ``deployed``; that contradiction fails closed and is never repaired by
    inferring a Restore or an undeploy. Everything else about an Asset is
    owned by the whole-Store validator.
    """
    if not isinstance(assets, Mapping):
        raise ArchiveValidationError("Assets collection is not a mapping")
    for asset_key in _sorted_asset_keys(assets):
        asset = assets[asset_key]
        if not isinstance(asset, Mapping):
            raise ArchiveValidationError(f"Asset {asset_key} is not a mapping")
        if ARCHIVED_AT not in asset:
            raise ArchiveValidationError(f"Asset {asset_key} has no {ARCHIVED_AT}")
        archived_at = asset[ARCHIVED_AT]
        if archived_at is None:
            continue
        try:
            parse_canonical_utc(archived_at)
        except CanonicalValueError as err:
            raise ArchiveValidationError(
                f"Asset {asset_key} has an invalid {ARCHIVED_AT}: "
                "not a canonical UTC timestamp"
            ) from err
        if asset.get(CONF_DEPLOYMENT_STATE) == DEPLOYMENT_STATE_DEPLOYED:
            raise ArchiveValidationError(
                f"Asset {asset_key} is archived and deployed at the same time"
            )
