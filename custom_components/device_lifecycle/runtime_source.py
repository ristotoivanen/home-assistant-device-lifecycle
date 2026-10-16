"""Versioned logical measurement-source authorization, separate from Asset identity.

Only an explicit configuration flow creates an authorization. Registry equality
cannot prove physical hardware identity, or restore authorization after a gap.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from typing import Any
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .const import CONF_SOURCE_ENTITY_ID, CONF_SOURCE_IDENTITY

_IDENTITY_FIELDS = (
    "registry_id",
    "domain",
    "platform",
    "unique_id",
    "config_entry_id",
    "config_subentry_id",
    "device_id",
)
_REQUIRED_FIELDS = {"version", "authorization_id", *_IDENTITY_FIELDS}
_OPTIONAL_IDS = {"config_entry_id", "config_subentry_id", "device_id"}
_INVALIDATION_UPDATES = "device_lifecycle_runtime_source_invalidation_updates"


def _identity(row: er.RegistryEntry) -> dict[str, Any]:
    return {
        "registry_id": row.id,
        "domain": row.domain,
        "platform": row.platform,
        "unique_id": row.unique_id,
        "config_entry_id": row.config_entry_id,
        "config_subentry_id": row.config_subentry_id,
        "device_id": row.device_id,
    }


def authorize_source(row: er.RegistryEntry) -> dict[str, Any]:
    """Capture the selected row only at explicit user confirmation."""
    return {"version": 1, "authorization_id": uuid4().hex, **_identity(row)}


@contextmanager
def watch_source_selection(
    hass: HomeAssistant, entity_id: str
) -> Iterator[Callable[[], bool]]:
    """Remember selection gaps during a form's await, even without an old binding.

    Present tuple equality cannot reveal remove/return (ABA). This observer only
    refuses stale consent; it never establishes or persists authorization.
    """
    changed = False

    @callback
    def registry_changed(event: Event) -> None:
        nonlocal changed
        changes = event.data.get("changes", {})
        if (
            event.data.get("entity_id") == entity_id
            or changes.get("entity_id") == entity_id
        ) and (
            event.data["action"] in ("remove", "create")
            or {"entity_id", "unique_id", "platform", "config_entry_id",
                "config_subentry_id", "device_id"}.intersection(changes)
        ):
            changed = True

    unsubscribe = hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED, registry_changed
    )
    try:
        yield lambda: changed
    finally:
        unsubscribe()


def source_status(hass: HomeAssistant, data: Mapping[str, Any]) -> str:
    """Validate the saved contract without learning identity from present state."""
    binding = data.get(CONF_SOURCE_IDENTITY)
    if binding is None:
        return "legacy_confirmation_required"
    if (
        not isinstance(binding, dict)
        or set(binding) not in (_REQUIRED_FIELDS, _REQUIRED_FIELDS | {"invalidated"})
        or type(binding.get("version")) is not int
        or binding["version"] != 1
        or not isinstance(binding.get("authorization_id"), str)
        or not binding["authorization_id"]
        or any(
            not (isinstance(binding[key], str) and binding[key])
            and not (key in _OPTIONAL_IDS and binding[key] is None)
            for key in _IDENTITY_FIELDS
        )
    ):
        return "invalid_binding"
    if "invalidated" in binding:
        return "authorization_invalidated"
    registry = er.async_get(hass)
    row = registry.async_get(str(data.get(CONF_SOURCE_ENTITY_ID) or ""))
    if row is None:
        return (
            "source_renamed"
            if registry.async_get(binding["registry_id"])
            else "registry_missing"
        )
    if _identity(row) != {key: binding[key] for key in _IDENTITY_FIELDS}:
        return "identity_mismatch"
    return "authorized"


def source_invalidator(
    hass: HomeAssistant,
    entry: ConfigEntry,
    subentry: ConfigSubentry,
) -> Callable[[str], None]:
    """Persist a detected gap; stale callbacks cannot revoke a new approval.

    HA owns ConfigSubentry persistence. Invalidation alone needs no reload: the
    current writer is already paused and must retain its proven pending deltas.
    The invalidation survives reload/restart, including a row that returns (ABA).
    """
    authorized = subentry.data.get(CONF_SOURCE_IDENTITY)

    def invalidate(reason: str) -> None:
        if not isinstance(authorized, dict) or reason == "invalid_binding":
            return
        if hass.config_entries.async_get_entry(entry.entry_id) is not entry:
            return
        current = entry.subentries.get(subentry.subentry_id)
        if (
            current is None
            or current.data.get(CONF_SOURCE_IDENTITY) != authorized
            or "invalidated" in authorized
        ):
            return
        data = {
            **current.data,
            CONF_SOURCE_IDENTITY: {**authorized, "invalidated": reason},
        }
        expected = deepcopy(entry.as_dict())
        expected.pop("modified_at")
        for item in expected["subentries"]:
            if item["subentry_id"] == current.subentry_id:
                item["data"] = data
        # HA can eagerly start update listeners inside async_update_subentry.
        # Install the exact expected notification before invoking it.
        updates = hass.data.setdefault(_INVALIDATION_UPDATES, {})
        count = updates.get(entry.entry_id, (None, 0))[1]
        updates[entry.entry_id] = (expected, count + 1)
        hass.config_entries.async_update_subentry(entry, current, data=data)

    return invalidate


def consume_invalidation_update(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Skip only the exact invalidation notification, never a user's later edit.

    This transient hint controls reload scheduling, not authorization. The
    authoritative revocation is in the saved ConfigSubentry itself.
    """
    updates = hass.data.get(_INVALIDATION_UPDATES, {})
    expected, count = updates.pop(entry.entry_id, (None, 0))
    if expected is None:
        return False
    actual = entry.as_dict()
    actual.pop("modified_at")
    if expected != actual:
        return False
    if count > 1:
        updates[entry.entry_id] = (expected, count - 1)
    return True
