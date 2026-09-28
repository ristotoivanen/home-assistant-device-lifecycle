"""Canonical value helpers for frozen persisted schemas.

These helpers implement the canonical representations frozen in
docs/maintenance-store-v4-schema.md. They are domain-neutral: they know
nothing about Assets, Maintenance, the Store manager, or Home Assistant.

Two kinds of helper are deliberately kept apart:

- validators (``require_*``, ``parse_*``) accept only an already canonical
  value and never repair it, so load-time validation cannot silently change
  persisted data;
- normalizers (``normalize_*``, ``decimal_from_input``, ``format_decimal``)
  turn new input into its canonical form at mutation time.

Existing Asset, Lifecycle, Replacement, and Runtime validation keeps its own
historical rules and does not use these stricter helpers.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID


class CanonicalValueError(ValueError):
    """A value is not in, or cannot be brought into, its canonical form."""


def require_text(value: Any) -> str:
    """Return canonical required text: a trimmed, non-empty string."""
    if type(value) is not str:
        raise CanonicalValueError("Text must be a string")
    if not value or value != value.strip():
        raise CanonicalValueError("Text must be trimmed and non-empty")
    return value


def optional_text(value: Any) -> str | None:
    """Return canonical optional text: None or canonical required text."""
    if value is None:
        return None
    return require_text(value)


def normalize_required_text(value: Any) -> str:
    """Trim new required text input; empty or whitespace-only is invalid."""
    if type(value) is not str:
        raise CanonicalValueError("Text must be a string")
    normalized = value.strip()
    if not normalized:
        raise CanonicalValueError("Text is required")
    return normalized


def normalize_optional_text(value: Any) -> str | None:
    """Trim new optional text input; empty or whitespace-only becomes None."""
    if value is None:
        return None
    if type(value) is not str:
        raise CanonicalValueError("Text must be a string")
    return value.strip() or None


def parse_civil_date(value: Any) -> date:
    """Return a canonical ``YYYY-MM-DD`` civil date.

    The string must name a real Gregorian date and survive a round trip, so
    compact (``20260926``) and week-date (``2026-W39-6``) spellings that
    ``date.fromisoformat`` also accepts are rejected. The date is never
    compared with the current day.
    """
    if type(value) is not str:
        raise CanonicalValueError("Civil date must be a string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as err:
        raise CanonicalValueError("Civil date is not a valid date") from err
    if parsed.isoformat() != value:
        raise CanonicalValueError("Civil date must use canonical YYYY-MM-DD")
    return parsed


def parse_canonical_utc(value: Any) -> datetime:
    """Return a canonical aware UTC timestamp.

    The canonical form is ``datetime.isoformat()`` of an aware UTC datetime,
    as produced by ``utc_now_iso``. ``Z``, ``-00:00``, a zero fraction such
    as ``.000000``, and any non-UTC offset are rejected. No ordering between
    timestamps is implied.
    """
    if type(value) is not str:
        raise CanonicalValueError("Timestamp must be a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise CanonicalValueError("Timestamp is not a valid timestamp") from err
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise CanonicalValueError("Timestamp must be an aware UTC timestamp")
    if parsed.isoformat() != value:
        raise CanonicalValueError("Timestamp is not in canonical form")
    return parsed


def utc_now_iso() -> str:
    """Return the current time as a canonical UTC timestamp."""
    return datetime.now(UTC).isoformat()


def parse_canonical_decimal(value: Any, *, positive: bool = False) -> Decimal:
    """Return a persisted canonical non-negative Decimal string's value.

    The string must equal ``format(Decimal(value), "f")``, which rejects
    exponent notation, a leading ``+``, whitespace, leading zeros, and
    other alternative spellings. ``"3600"`` and ``"3600.0"`` are both
    canonical and numerically equal; neither is rewritten into the other.
    NaN, infinities, negative values, and ``-0`` are rejected. With
    ``positive=True`` the value must also be greater than zero.
    """
    if type(value) is not str:
        raise CanonicalValueError("Decimal must be a string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as err:
        raise CanonicalValueError("Decimal is not a valid number") from err
    if not parsed.is_finite():
        raise CanonicalValueError("Decimal must be finite")
    if parsed.is_signed():
        # Rejects negative values and the explicit negative zero "-0".
        raise CanonicalValueError("Decimal must not be negative")
    if format(parsed, "f") != value:
        raise CanonicalValueError("Decimal is not in canonical plain form")
    if positive and parsed == 0:
        raise CanonicalValueError("Decimal must be greater than zero")
    return parsed


def canonical_decimals_equal(first: str | None, second: str | None) -> bool:
    """Compare two optional canonical Decimal strings numerically.

    Two ``None`` values are equal, and ``None`` never equals a number.
    ``"3600"`` equals ``"3600.0"``. Runtime values must be compared this way,
    never as strings.
    """
    if first is None or second is None:
        return first is None and second is None
    return parse_canonical_decimal(first) == parse_canonical_decimal(second)


def decimal_from_input(value: Any) -> Decimal:
    """Convert new numeric input to a finite, non-negative Decimal.

    The value always goes through ``str`` first, so a binary float such as
    ``1.1`` becomes exactly ``Decimal("1.1")`` instead of its binary
    expansion. Negative zero becomes zero; any other negative value is
    rejected. No unit conversion is performed.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise CanonicalValueError("Decimal input must be a number or string")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as err:
        raise CanonicalValueError("Decimal input is not a valid number") from err
    if not parsed.is_finite():
        raise CanonicalValueError("Decimal input must be finite")
    if parsed.is_zero():
        return parsed.copy_abs()
    if parsed < 0:
        raise CanonicalValueError("Decimal input must not be negative")
    return parsed


def format_decimal(value: Decimal) -> str:
    """Return the canonical persisted plain string for a Decimal.

    Uses ``format(value, "f")`` and never ``Decimal.normalize()``, so an
    exponent-valued Decimal such as ``Decimal("3.6E+3")`` becomes ``"3600"``
    and the result always passes ``parse_canonical_decimal``.
    """
    if not isinstance(value, Decimal) or not value.is_finite():
        raise CanonicalValueError("Only a finite Decimal can be formatted")
    if value.is_zero():
        value = value.copy_abs()
    elif value < 0:
        raise CanonicalValueError("A negative Decimal cannot be formatted")
    return format(value, "f")


def require_canonical_uuid(value: Any) -> str:
    """Return a canonical lowercase, hyphenated UUID string.

    Uppercase, braced, ``urn:uuid:``, and unhyphenated spellings are valid
    UUIDs but not canonical, so they are rejected. Equality with a mapping
    key is checked by the caller.
    """
    if type(value) is not str:
        raise CanonicalValueError("UUID must be a string")
    try:
        parsed = UUID(value)
    except ValueError as err:
        raise CanonicalValueError("UUID is not valid") from err
    if str(parsed) != value:
        raise CanonicalValueError("UUID is not in canonical form")
    return value
