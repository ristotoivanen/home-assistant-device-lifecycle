"""Exact Store record shapes and the Store 3.1 migration-source preflight.

The shape authority for the production Store 4.1 validator and the
3.1 -> 4.1 migration step in storage.py. The canonical target is
docs/asset-archive-store-v4.md.

Store 3.1 already enforces its exact top-level shape, but it has never
enforced exact Asset and Purchase record key sets. Before any Store 4.1
transform runs, the migration proves that every Asset and Purchase has
exactly the Store 3.1 keys. The check is observational only: unknown or
missing keys are evidence, never data to strip, whitelist, or rebuild, and a
failure names only record identifiers and key names, never field values.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

STORE_3_1_TOP_LEVEL_KEYS = frozenset(
    {
        "next_asset_number",
        "purchases",
        "assets",
        "lifecycle_events",
        "replacement_records",
    }
)

STORE_4_1_TOP_LEVEL_KEYS = STORE_3_1_TOP_LEVEL_KEYS | frozenset(
    {"maintenance_schedules", "maintenance_events"}
)

ASSET_KEYS_3_1 = frozenset(
    {
        "asset_uuid",
        "asset_id",
        "name",
        "category",
        "purchase_uuid",
        "deployment_state",
        "installed_date",
        "ha_area_id",
        "warranty",
        "runtime",
        "lifecycle",
        "manufacturer",
        "model",
        "model_id",
        "serial_number",
        "sw_version",
        "hw_version",
        "notes",
        "field_sources",
        "ha_device_refs",
    }
)

ASSET_KEYS_4_1 = ASSET_KEYS_3_1 | frozenset({"archived_at"})

PURCHASE_KEYS = frozenset(
    {
        "purchase_uuid",
        "config_subentry_id",
        "configured",
        "name",
        "purchase_date",
        "seller",
        "total_price",
        "currency",
        "receipt_reference",
        "receipt_url",
        "notes",
        "asset_uuids",
    }
)


def _key_names(keys: Iterable[Any]) -> tuple[str, ...]:
    """Return key names in a deterministic order, whatever their type."""
    return tuple(sorted(str(key) for key in keys))


class StoreShapeError(ValueError):
    """A Store collection or record does not have its exact key set.

    ``kind`` names the collection (for example ``assets``), ``record_id``
    the map key of the offending record, or ``None`` when the collection
    itself is missing or not a mapping. ``missing`` and ``unexpected`` are
    sorted tuples of key names. No field value is ever carried.
    """

    def __init__(
        self,
        kind: str,
        *,
        record_id: str | None = None,
        missing: tuple[str, ...] = (),
        unexpected: tuple[str, ...] = (),
        reason: str,
    ) -> None:
        """Describe the shape failure without any field value."""
        self.kind = kind
        self.record_id = record_id
        self.missing = missing
        self.unexpected = unexpected
        self.reason = reason
        subject = kind if record_id is None else f"{kind} record {record_id}"
        detail = f"{subject}: {reason}"
        if missing or unexpected:
            detail += f"; missing={list(missing)}, unexpected={list(unexpected)}"
        super().__init__(detail)


def require_exact_record_keys(
    records: Any,
    expected: frozenset[str],
    *,
    kind: str,
) -> None:
    """Require every record of one collection to have exactly ``expected``.

    Records are checked in the sorted order of their map keys (as strings),
    so the first reported failure never depends on insertion order. Nothing
    is changed, copied back, or normalized.
    """
    if not isinstance(records, Mapping):
        raise StoreShapeError(kind, reason="collection is not a mapping")
    for record_key in sorted(records, key=str):
        record = records[record_key]
        record_id = str(record_key)
        if not isinstance(record, Mapping):
            raise StoreShapeError(
                kind,
                record_id=record_id,
                reason="record is not a mapping",
            )
        keys = set(record)
        if keys != expected:
            raise StoreShapeError(
                kind,
                record_id=record_id,
                missing=_key_names(expected - keys),
                unexpected=_key_names(keys - expected),
                reason="record has an invalid key set",
            )


def preflight_store_3_1_record_shapes(data: Any) -> None:
    """Prove the Asset and Purchase record shapes of a Store 3.1 payload.

    This is only the migration-source compatibility check. It runs after the
    normal Store 3.1 whole-Store validation, which stays the authority for
    every other rule, and before any Store 4.1 transform.
    """
    if not isinstance(data, Mapping):
        raise StoreShapeError("store", reason="payload is not a mapping")
    for kind, expected in (("assets", ASSET_KEYS_3_1), ("purchases", PURCHASE_KEYS)):
        if kind not in data:
            raise StoreShapeError(kind, reason="collection is missing")
        require_exact_record_keys(data[kind], expected, kind=kind)
