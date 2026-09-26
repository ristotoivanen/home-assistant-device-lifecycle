"""Civil-date calendar arithmetic for Maintenance due dates."""

from __future__ import annotations

from datetime import date

import pytest

from custom_components.device_lifecycle.maintenance_projection import (
    DueState,
    add_calendar_interval,
    calendar_condition,
)


@pytest.mark.parametrize(
    ("anchor", "value", "unit", "expected"),
    [
        # Civil days, across month, year, and leap-day boundaries.
        (date(2026, 9, 26), 1, "days", date(2026, 9, 27)),
        (date(2026, 12, 31), 1, "days", date(2027, 1, 1)),
        (date(2028, 2, 28), 1, "days", date(2028, 2, 29)),
        (date(2027, 2, 28), 1, "days", date(2027, 3, 1)),
        (date(2026, 3, 28), 7, "days", date(2026, 4, 4)),
        (date(2026, 1, 1), 365, "days", date(2027, 1, 1)),
        # Months, clamped to the last valid day of the target month.
        (date(2026, 1, 31), 1, "months", date(2026, 2, 28)),
        (date(2028, 1, 31), 1, "months", date(2028, 2, 29)),
        (date(2026, 3, 31), 1, "months", date(2026, 4, 30)),
        (date(2026, 8, 31), 6, "months", date(2027, 2, 28)),
        (date(2026, 1, 15), 1, "months", date(2026, 2, 15)),
        (date(2026, 11, 30), 3, "months", date(2027, 2, 28)),
        (date(2026, 12, 1), 1, "months", date(2027, 1, 1)),
        (date(2026, 6, 1), 24, "months", date(2028, 6, 1)),
        # Years are semantic years: 12 x N months, clamped the same way.
        (date(2028, 2, 29), 1, "years", date(2029, 2, 28)),
        (date(2028, 2, 29), 4, "years", date(2032, 2, 29)),
        (date(2026, 9, 26), 2, "years", date(2028, 9, 26)),
    ],
)
def test_add_calendar_interval(
    anchor: date, value: int, unit: str, expected: date
) -> None:
    """Due dates use civil-date arithmetic, never 86 400-second days."""
    assert add_calendar_interval(anchor, value, unit) == expected


def test_years_equal_twelve_months() -> None:
    """A year interval is exactly the same as 12 x N months."""
    for anchor in (date(2028, 2, 29), date(2026, 1, 31), date(2026, 7, 4)):
        for years in (1, 2, 3, 4, 100):
            assert add_calendar_interval(anchor, years, "years") == (
                add_calendar_interval(anchor, years * 12, "months")
            )


def test_clamped_day_is_not_carried_into_the_next_cycle() -> None:
    """Each cycle starts from the actual effective anchor, not a remembered day.

    Jan 31 + 1 month is Feb 28. Maintenance done on Feb 28 anchors the next
    cycle on Feb 28, so the next due date is Mar 28, not Mar 31.
    """
    first_due = add_calendar_interval(date(2026, 1, 31), 1, "months")
    assert first_due == date(2026, 2, 28)
    assert add_calendar_interval(first_due, 1, "months") == date(2026, 3, 28)


@pytest.mark.parametrize(
    ("anchor", "value", "unit"),
    [
        (date(9999, 12, 31), 1, "days"),
        (date(9999, 12, 1), 1, "months"),
        (date(9999, 1, 1), 1, "years"),
        (date(2026, 1, 1), 10**12, "days"),
        (date(2026, 1, 1), 10**9, "months"),
        (date(2026, 1, 1), 10**9, "years"),
        (date(2026, 1, 1), 8000, "years"),
    ],
)
def test_unrepresentable_due_date_is_none(anchor: date, value: int, unit: str) -> None:
    """A due date beyond the representable range is UNKNOWN, never an error."""
    assert add_calendar_interval(anchor, value, unit) is None


def test_last_representable_due_dates() -> None:
    """The boundary itself is still representable."""
    assert add_calendar_interval(date(9999, 12, 30), 1, "days") == date.max
    assert add_calendar_interval(date(9999, 11, 30), 1, "months") == date(9999, 12, 30)
    assert add_calendar_interval(date(9998, 12, 31), 1, "years") == date.max


def test_unknown_unit_is_a_programming_error() -> None:
    """Validated Schedules only use days, months, or years."""
    with pytest.raises(ValueError, match="Unknown calendar interval unit"):
        add_calendar_interval(date(2026, 1, 1), 1, "weeks")


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 12, 25), DueState.OK),
        (date(2026, 12, 31), DueState.OK),
        (date(2027, 1, 1), DueState.DUE),
        (date(2027, 1, 2), DueState.OVERDUE),
        (date(2030, 1, 1), DueState.OVERDUE),
    ],
)
def test_calendar_condition_states(today: date, expected: DueState) -> None:
    """Before the due date is OK, on it DUE, after it OVERDUE."""
    condition = calendar_condition(date(2026, 1, 1), 12, "months", today)
    assert condition.due == date(2027, 1, 1)
    assert condition.state is expected


def test_calendar_condition_unknown_anchor() -> None:
    """A missing calendar anchor is UNKNOWN with no due date."""
    condition = calendar_condition(None, 6, "months", date(2026, 9, 26))
    assert condition.due is None
    assert condition.state is DueState.UNKNOWN


def test_calendar_condition_overflow_is_unknown() -> None:
    """An unrepresentable due date is an UNKNOWN projection boundary."""
    condition = calendar_condition(date(9999, 12, 1), 1, "months", date(2026, 9, 26))
    assert condition.due is None
    assert condition.state is DueState.UNKNOWN
