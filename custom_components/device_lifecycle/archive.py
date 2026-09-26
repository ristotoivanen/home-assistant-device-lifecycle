"""Asset Archive state: the Store transform and the Archive load invariants.

Inactive library code for the Store 4.1 upgrade: the production Store is
still 3.1, only the inactive Store 4.1 validator and migration step in
storage.py use the transform and the validator, and nothing in production
uses the Archive and Restore mutations yet. The canonical contract is
docs/asset-archive-store-v4.md.

An Asset's Archive state is one field, ``archived_at``: ``None`` while the
Asset is in active/current management, a canonical UTC timestamp while it is
archived. There is no Archive history, reason, or marker anywhere else.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .canonical import CanonicalValueError, parse_canonical_utc, require_canonical_uuid
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


# Archive and Restore snapshot mutations. They own only the Store-domain
# preconditions; Runtime configuration and writer facts are checked by the
# authoritative manager operation, not here.


class ArchiveOutcome(StrEnum):
    """A successful Archive or Restore: the state changed, or already held."""

    CHANGED = "changed"
    NO_OP = "no_op"


class ArchiveMutationError(ValueError):
    """An Archive or Restore request cannot be applied.

    ``code`` is stable: ``archive_request_invalid``,
    ``archive_asset_not_found``, ``archive_asset_deployed``, or
    ``archive_observed_utc_invalid``. Messages name at most the Asset UUID.
    """

    def __init__(self, message: str, *, code: str) -> None:
        """Carry a stable error code."""
        super().__init__(message)
        self.code = code


def _require_request_uuid(value: Any) -> None:
    try:
        require_canonical_uuid(value)
    except CanonicalValueError as err:
        raise ArchiveMutationError(
            "Archive request Asset UUID is not canonical",
            code="archive_request_invalid",
        ) from err


@dataclass(frozen=True, slots=True)
class ArchiveAssetRequest:
    """Remove one Asset from active/current management."""

    asset_uuid: str

    def __post_init__(self) -> None:
        """Accept only a canonical Asset UUID."""
        _require_request_uuid(self.asset_uuid)


@dataclass(frozen=True, slots=True)
class RestoreAssetRequest:
    """Return one archived Asset to active/current management."""

    asset_uuid: str

    def __post_init__(self) -> None:
        """Accept only a canonical Asset UUID."""
        _require_request_uuid(self.asset_uuid)


ArchiveRequest = ArchiveAssetRequest | RestoreAssetRequest


def _require_request(request: Any) -> None:
    if not isinstance(request, (ArchiveAssetRequest, RestoreAssetRequest)):
        raise TypeError(f"Unknown Archive request: {type(request).__name__}")


def archive_state_matches(asset: Mapping[str, Any], request: ArchiveRequest) -> bool:
    """Return whether an Asset already has the state a request asks for.

    Archive matches any archived Asset and Restore any active Asset. The
    ``archived_at`` value is deliberately ignored: Archive generates a new
    observed time on every attempt, so after an ambiguous write the persisted
    state, not the timestamp, proves that the requested transition happened.
    """
    _require_request(request)
    archived = asset_is_archived(asset)
    return archived if isinstance(request, ArchiveAssetRequest) else not archived


def apply_archive_request(
    assets: MutableMapping[str, Any],
    request: ArchiveRequest,
    *,
    observed_utc: str | None,
) -> ArchiveOutcome:
    """Apply one Archive or Restore request to a detached Assets mapping.

    The mapping is changed in place, like every manager mutator; the caller
    owns the detached copy, validation, persistence, and publishing. Only
    ``archived_at`` of the requested Asset can change.

    Order: the Asset exists, then NO_OP when the requested state already
    holds (an archived Asset keeps its existing timestamp), then, for
    Archive only, the Asset must not be ``deployed`` and ``observed_utc``
    must be a canonical UTC timestamp. Restore ignores ``observed_utc``.
    """
    _require_request(request)
    asset_uuid = request.asset_uuid
    if asset_uuid not in assets:
        raise ArchiveMutationError(
            f"Asset {asset_uuid} does not exist", code="archive_asset_not_found"
        )
    asset = assets[asset_uuid]
    if archive_state_matches(asset, request):
        return ArchiveOutcome.NO_OP

    if isinstance(request, RestoreAssetRequest):
        asset[ARCHIVED_AT] = None
        return ArchiveOutcome.CHANGED

    if asset[CONF_DEPLOYMENT_STATE] == DEPLOYMENT_STATE_DEPLOYED:
        raise ArchiveMutationError(
            f"Asset {asset_uuid} is deployed and cannot be archived",
            code="archive_asset_deployed",
        )
    try:
        parse_canonical_utc(observed_utc)
    except CanonicalValueError as err:
        raise ArchiveMutationError(
            "Archive observed time is not a canonical UTC timestamp",
            code="archive_observed_utc_invalid",
        ) from err
    asset[ARCHIVED_AT] = observed_utc
    return ArchiveOutcome.CHANGED
