"""Canonical identity of Runtime ConfigSubentries.

The one authority for which existing Asset a Runtime ConfigSubentry
identifies. It is the rule Runtime reconciliation has always used:

1. A subentry without a usable ``device_id`` identifies no Asset.
2. The Asset its ``asset_uuid`` names, when that Asset exists and its
   primary Home Assistant device is unset or equals ``device_id``.
3. Otherwise the Asset whose primary Home Assistant device is ``device_id``.
4. Otherwise no existing Asset.

This is identity only. It creates, changes, and rewrites nothing, reads no
Home Assistant registry or Store manager state, and looks at every Asset,
active or archived. Whether an identified Asset may be managed is decided by
the caller. ``None`` means only that no existing Asset is identified; it
does not mean "create one".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol
from uuid import UUID

from .const import CONF_ASSET_UUID, CONF_DEVICE_ID, SUBENTRY_TYPE_RUNTIME

# The stored role of an Asset's primary Home Assistant device reference.
PRIMARY_ROLE = "primary"


class RuntimeSubentryLike(Protocol):
    """The ConfigSubentry fields identity resolution reads."""

    @property
    def subentry_id(self) -> str:
        """The ConfigSubentry ID."""

    @property
    def subentry_type(self) -> str:
        """The ConfigSubentry type."""

    @property
    def data(self) -> Mapping[str, Any]:
        """The ConfigSubentry data."""


def primary_device_id(asset: Mapping[str, Any]) -> str | None:
    """Return an Asset's primary Home Assistant device reference."""
    for reference in asset.get("ha_device_refs", []):
        if reference.get("role") == PRIMARY_ROLE:
            return str(reference.get("device_id") or "") or None
    return None


def _asset_uuid_hint(value: Any) -> str | None:
    """Return the canonical form of a subentry's ``asset_uuid`` hint, if any.

    Any spelling the ``uuid`` module accepts is a usable hint, exactly as
    reconciliation has always read it; anything else is no hint.
    """
    if not value:
        return None
    try:
        return str(UUID(str(value)))
    except TypeError, ValueError, AttributeError:
        return None


def resolve_runtime_subentry_asset(
    assets: Mapping[str, Mapping[str, Any]],
    subentry_data: Mapping[str, Any],
) -> str | None:
    """Return the UUID of the existing Asset a Runtime subentry identifies."""
    device_id = str(subentry_data.get(CONF_DEVICE_ID) or "")
    if not device_id:
        return None

    named_uuid = _asset_uuid_hint(subentry_data.get(CONF_ASSET_UUID))
    if named_uuid is not None:
        named = assets.get(named_uuid)
        if named is not None and primary_device_id(named) in (None, device_id):
            return named_uuid

    for asset_uuid in sorted(assets):
        if primary_device_id(assets[asset_uuid]) == device_id:
            return asset_uuid
    return None


def runtime_subentries_resolving_to(
    subentries: Iterable[RuntimeSubentryLike],
    assets: Mapping[str, Mapping[str, Any]],
    asset_uuid: str,
) -> tuple[str, ...]:
    """Return the IDs of every Runtime subentry that identifies the Asset.

    Non-Runtime subentries and unresolved Runtime subentries are ignored.
    The IDs are sorted.
    """
    return tuple(
        sorted(
            {
                subentry.subentry_id
                for subentry in subentries
                if subentry.subentry_type == SUBENTRY_TYPE_RUNTIME
                and resolve_runtime_subentry_asset(assets, subentry.data) == asset_uuid
            }
        )
    )
