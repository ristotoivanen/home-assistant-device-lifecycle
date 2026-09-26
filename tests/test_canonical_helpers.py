"""Canonical value helpers for the frozen Maintenance Store schema."""

from __future__ import annotations

import ast
import inspect
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from custom_components.device_lifecycle import canonical
from custom_components.device_lifecycle.canonical import (
    CanonicalValueError,
    canonical_decimals_equal,
    decimal_from_input,
    format_decimal,
    normalize_optional_text,
    normalize_required_text,
    optional_text,
    parse_canonical_decimal,
    parse_canonical_utc,
    parse_civil_date,
    require_canonical_uuid,
    require_text,
    utc_now_iso,
)

CANONICAL_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"


# Text


@pytest.mark.parametrize("value", ["Filter", "Filter change", "Ä ö", "a"])
def test_require_text_accepts_canonical_text(value: str) -> None:
    """Already canonical text is returned unchanged."""
    assert require_text(value) == value
    assert optional_text(value) == value


@pytest.mark.parametrize("value", [" foo", "foo ", "\tfoo", "foo\n", "   ", ""])
def test_require_text_rejects_untrimmed_or_empty_text(value: str) -> None:
    """Load-time validation rejects text that is not already canonical."""
    with pytest.raises(CanonicalValueError):
        require_text(value)
    with pytest.raises(CanonicalValueError):
        optional_text(value)


@pytest.mark.parametrize("value", [123, 1.5, b"foo", ["foo"], True])
def test_require_text_rejects_non_strings(value: Any) -> None:
    """Only a string can be canonical text."""
    with pytest.raises(CanonicalValueError):
        require_text(value)
    with pytest.raises(CanonicalValueError):
        optional_text(value)


def test_optional_text_accepts_none() -> None:
    """None is the only empty representation of optional text."""
    assert optional_text(None) is None
    with pytest.raises(CanonicalValueError):
        require_text(None)


def test_normalize_required_text_trims_and_rejects_empty() -> None:
    """Required input is trimmed; empty or whitespace-only input is invalid."""
    assert normalize_required_text("  Filter change \n") == "Filter change"
    assert normalize_required_text("Filter") == "Filter"
    for value in ("", "   ", "\t\n"):
        with pytest.raises(CanonicalValueError):
            normalize_required_text(value)
    for value in (None, 12):
        with pytest.raises(CanonicalValueError):
            normalize_required_text(value)


def test_normalize_optional_text_trims_and_maps_empty_to_none() -> None:
    """Optional input is trimmed, and empty input becomes None."""
    assert normalize_optional_text(None) is None
    assert normalize_optional_text("  note ") == "note"
    assert normalize_optional_text("") is None
    assert normalize_optional_text("   ") is None
    with pytest.raises(CanonicalValueError):
        normalize_optional_text(12)


# Civil date


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-26", date(2026, 9, 26)),
        ("2028-02-29", date(2028, 2, 29)),
        ("2000-02-29", date(2000, 2, 29)),
        ("0001-01-01", date(1, 1, 1)),
        ("9999-12-31", date(9999, 12, 31)),
    ],
)
def test_parse_civil_date_accepts_canonical_dates(value: str, expected: date) -> None:
    """Real Gregorian dates in canonical form are accepted."""
    assert parse_civil_date(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "2026-02-30",
        "2027-02-29",
        "1900-02-29",
        "2026-13-01",
        "20260926",
        "2026-W39-6",
        "26-09-2026",
        "2026-9-26",
        "2026-09-26 ",
        " 2026-09-26",
        "2026-09-26T00:00:00",
        "",
    ],
)
def test_parse_civil_date_rejects_invalid_or_noncanonical_dates(value: str) -> None:
    """Invalid dates and alternative ISO spellings are rejected."""
    with pytest.raises(CanonicalValueError):
        parse_civil_date(value)


@pytest.mark.parametrize("value", [None, 20260926, date(2026, 9, 26)])
def test_parse_civil_date_rejects_non_strings(value: Any) -> None:
    """Only a string can be a persisted civil date."""
    with pytest.raises(CanonicalValueError):
        parse_civil_date(value)


def test_python_accepts_the_alternative_iso_spellings_the_helper_rejects() -> None:
    """The round trip is what rejects spellings date.fromisoformat accepts."""
    assert date.fromisoformat("20260926") == date(2026, 9, 26)
    assert date.fromisoformat("2026-W39-6") == date(2026, 9, 26)


def test_parse_civil_date_never_compares_with_today() -> None:
    """A far-future date is structurally valid; future checks are elsewhere."""
    assert parse_civil_date("9999-12-31") == date(9999, 12, 31)


# UTC timestamp


@pytest.mark.parametrize(
    "value",
    ["2026-09-26T12:34:56+00:00", "2026-09-26T12:34:56.123456+00:00"],
)
def test_parse_canonical_utc_accepts_canonical_timestamps(value: str) -> None:
    """Aware UTC timestamps in isoformat form are accepted."""
    parsed = parse_canonical_utc(value)
    assert parsed.utcoffset() is not None
    assert parsed.isoformat() == value


@pytest.mark.parametrize(
    "value",
    [
        "2026-09-26T12:34:56Z",
        "2026-09-26T12:34:56-00:00",
        "2026-09-26T12:34:56.000000+00:00",
        "2026-09-26T12:34:56+02:00",
        "2026-09-26T12:34:56",
        "2026-09-26 12:34:56+00:00",
        "20260926T123456+0000",
        "2026-09-26",
        "not a timestamp",
        "",
    ],
)
def test_parse_canonical_utc_rejects_noncanonical_timestamps(value: str) -> None:
    """Non-canonical UTC spellings, other offsets, and naive values fail."""
    with pytest.raises(CanonicalValueError):
        parse_canonical_utc(value)


@pytest.mark.parametrize(
    "value",
    [None, 1727353496, datetime(2026, 9, 26, 12, 34, 56, tzinfo=UTC)],
)
def test_parse_canonical_utc_rejects_non_strings(value: Any) -> None:
    """Only a string can be a persisted timestamp."""
    with pytest.raises(CanonicalValueError):
        parse_canonical_utc(value)


def test_utc_now_iso_is_canonical() -> None:
    """The generated timestamp convention always passes validation."""
    assert parse_canonical_utc(utc_now_iso()).utcoffset().total_seconds() == 0


def test_timestamp_helper_implies_no_ordering() -> None:
    """Validation is per value; an earlier value is not rejected."""
    assert parse_canonical_utc("2020-01-01T00:00:00+00:00") < parse_canonical_utc(
        "2026-09-26T12:34:56+00:00"
    )


# Decimal, load-time


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", Decimal(0)),
        ("0.0", Decimal(0)),
        ("0.000", Decimal(0)),
        ("3600", Decimal(3600)),
        ("3600.0", Decimal(3600)),
        ("3600.00", Decimal(3600)),
        ("0.5", Decimal("0.5")),
        ("1284.53", Decimal("1284.53")),
        ("4624308.123456", Decimal("4624308.123456")),
    ],
)
def test_parse_canonical_decimal_accepts_plain_strings(
    value: str,
    expected: Decimal,
) -> None:
    """Plain non-negative decimal strings are accepted as they are."""
    assert parse_canonical_decimal(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "-1",
        "-0.5",
        "-0",
        "-0.0",
        "3.6E+3",
        "1e2",
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        " 3600",
        "3600 ",
        "+3600",
        "03600",
        "00",
        ".5",
        "5.",
        "1_000",
        "٣",
        "",
        "abc",
    ],
)
def test_parse_canonical_decimal_rejects_noncanonical_strings(value: str) -> None:
    """Negative, non-finite, and alternative spellings are rejected."""
    with pytest.raises(CanonicalValueError):
        parse_canonical_decimal(value)


@pytest.mark.parametrize("value", [None, 3600, 3600.0, Decimal(3600), True])
def test_parse_canonical_decimal_rejects_non_strings(value: Any) -> None:
    """Only a string can be a persisted canonical Decimal."""
    with pytest.raises(CanonicalValueError):
        parse_canonical_decimal(value)


def test_parse_canonical_decimal_positive_mode() -> None:
    """Positive mode rejects zero and accepts any value above it."""
    for value in ("0", "0.0"):
        with pytest.raises(CanonicalValueError):
            parse_canonical_decimal(value, positive=True)
    assert parse_canonical_decimal("0.001", positive=True) == Decimal("0.001")
    assert parse_canonical_decimal("3600", positive=True) == Decimal(3600)


def test_python_round_trip_alone_would_accept_negative_and_non_finite() -> None:
    """The finite and sign checks are needed on top of the round trip."""
    for value in ("-0", "-1", "NaN", "Infinity"):
        assert format(Decimal(value), "f") == value


def test_canonical_decimals_compare_numerically() -> None:
    """Runtime values are compared as Decimals, never as strings."""
    assert canonical_decimals_equal("3600", "3600.0")
    assert canonical_decimals_equal("0", "0.000")
    assert not canonical_decimals_equal("3600", "3600.5")
    assert canonical_decimals_equal(None, None)
    assert not canonical_decimals_equal(None, "0")
    assert not canonical_decimals_equal("0", None)
    with pytest.raises(CanonicalValueError):
        canonical_decimals_equal("3.6E+3", "3600")


# Decimal, mutation-time


def test_decimal_from_input_goes_through_str_for_floats() -> None:
    """A float never contributes its binary expansion."""
    binary_float = float("1.1")
    assert Decimal(binary_float) != Decimal("1.1")
    assert decimal_from_input(binary_float) == Decimal("1.1")
    assert decimal_from_input(1.1) == Decimal("1.1")
    assert format_decimal(decimal_from_input(1.1)) == "1.1"
    assert format_decimal(decimal_from_input(1284.53)) == "1284.53"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("3600", "3600"),
        (" 3600 ", "3600"),
        ("3.6E+3", "3600"),
        (3600, "3600"),
        (0, "0"),
        (0.0, "0.0"),
        (-0.0, "0.0"),
        ("-0", "0"),
        (Decimal("-0"), "0"),
        (Decimal("-0.0"), "0.0"),
        (Decimal("0.5"), "0.5"),
    ],
)
def test_decimal_from_input_is_canonical_after_formatting(
    value: Any,
    expected: str,
) -> None:
    """Input becomes a plain canonical string; negative zero becomes zero."""
    formatted = format_decimal(decimal_from_input(value))
    assert formatted == expected
    parse_canonical_decimal(formatted)


@pytest.mark.parametrize(
    "value",
    [
        -1,
        -0.5,
        "-1",
        Decimal("-0.001"),
        float("nan"),
        float("inf"),
        "NaN",
        "Infinity",
        "abc",
        None,
        True,
        [1],
    ],
)
def test_decimal_from_input_rejects_invalid_input(value: Any) -> None:
    """Negative, non-finite, non-numeric, and boolean input is rejected."""
    with pytest.raises(CanonicalValueError):
        decimal_from_input(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("3.6E+3"), "3600"),
        (Decimal("1E-7"), "0.0000001"),
        (Decimal("5E+1"), "50"),
        (Decimal("3600.0"), "3600.0"),
        (Decimal("-0"), "0"),
    ],
)
def test_format_decimal_writes_plain_notation(value: Decimal, expected: str) -> None:
    """Formatting never produces exponent notation or negative zero."""
    assert format_decimal(value) == expected
    assert parse_canonical_decimal(expected) == value


@pytest.mark.parametrize(
    "value",
    [Decimal(-1), Decimal("NaN"), Decimal("Infinity"), 3600, "3600", 1.0],
)
def test_format_decimal_rejects_values_it_cannot_persist(value: Any) -> None:
    """Only finite, non-negative Decimals are formatted."""
    with pytest.raises(CanonicalValueError):
        format_decimal(value)


def test_decimal_helpers_never_normalize() -> None:
    """Decimal.normalize() could emit exponent notation such as 3.6E+3."""
    assert Decimal(3600).normalize() == Decimal("3.6E+3")
    tree = ast.parse(inspect.getsource(canonical))
    assert not [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "normalize"
    ]


# UUID


def test_require_canonical_uuid_accepts_lowercase_hyphenated() -> None:
    """The canonical form is str(UUID(value))."""
    assert require_canonical_uuid(CANONICAL_UUID) == CANONICAL_UUID


@pytest.mark.parametrize(
    "value",
    [
        CANONICAL_UUID.upper(),
        "{" + CANONICAL_UUID + "}",
        "urn:uuid:" + CANONICAL_UUID,
        CANONICAL_UUID.replace("-", ""),
        " " + CANONICAL_UUID,
    ],
)
def test_require_canonical_uuid_rejects_alternative_spellings(value: str) -> None:
    """Valid but non-canonical UUID spellings are rejected."""
    with pytest.raises(CanonicalValueError):
        require_canonical_uuid(value)


@pytest.mark.parametrize(
    "value",
    ["not-a-uuid", "", None, 123, object()],
)
def test_require_canonical_uuid_rejects_invalid_values(value: Any) -> None:
    """Invalid UUIDs and non-strings are rejected."""
    with pytest.raises(CanonicalValueError):
        require_canonical_uuid(value)


def test_canonical_errors_are_value_errors() -> None:
    """Callers may translate one well-defined error into their own domain."""
    assert issubclass(CanonicalValueError, ValueError)
