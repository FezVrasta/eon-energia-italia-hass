"""Tariff band, holiday and DST tests.

The DST cases are the ones that matter. E.ON send twenty-four hourly fields every
day of the year, including the two days when the Italian day is not twenty-four
hours long, and getting that mapping wrong double-counted an hour of real data.
"""

from __future__ import annotations

from datetime import date

import pytest

from pyeonenergia import (
    easter_sunday,
    fascia_for_hour,
    hour_start_for_field,
    is_italian_holiday,
    local_hour_starts,
    parse_italian_date,
)

# Italy in 2026: clocks forward 29 March (23 hours), back 25 October (25 hours).
SPRING_FORWARD = date(2026, 3, 29)
FALL_BACK = date(2026, 10, 25)
ORDINARY_TUESDAY = date(2026, 9, 22)
SATURDAY = date(2026, 9, 26)
SUNDAY = date(2026, 9, 27)


class TestEaster:
    @pytest.mark.parametrize(
        ("year", "expected"),
        [
            (2024, date(2024, 3, 31)),
            (2025, date(2025, 4, 20)),
            (2026, date(2026, 4, 5)),
            (2027, date(2027, 3, 28)),
            (2030, date(2030, 4, 21)),
        ],
    )
    def test_known_dates(self, year, expected):
        assert easter_sunday(year) == expected


class TestHolidays:
    @pytest.mark.parametrize(
        "day",
        [date(2026, 1, 1), date(2026, 4, 25), date(2026, 8, 15), date(2026, 12, 26)],
    )
    def test_fixed_holidays(self, day):
        assert is_italian_holiday(day)

    def test_easter_monday_moves_with_easter(self):
        # Pasquetta 2026 is 6 April. It is the only moving holiday that can land
        # on a weekday, which is why it is worth computing at all.
        assert is_italian_holiday(date(2026, 4, 6))
        assert is_italian_holiday(date(2027, 3, 29))

    def test_an_ordinary_day_is_not_a_holiday(self):
        assert not is_italian_holiday(ORDINARY_TUESDAY)


class TestFascia:
    @pytest.mark.parametrize(
        ("hour", "expected"),
        [
            (1, "F3"),  # 00:00, night
            (7, "F3"),  # 06:00, still night
            (8, "F2"),  # 07:00, shoulder
            (10, "F1"),  # 09:00, peak
            (19, "F1"),  # 18:00, last peak hour
            (20, "F2"),  # 19:00, evening shoulder
            (24, "F3"),  # 23:00, back to night
        ],
    )
    def test_weekday_bands(self, hour, expected):
        assert fascia_for_hour(ORDINARY_TUESDAY, hour) == expected

    def test_saturday_has_no_peak(self):
        assert fascia_for_hour(SATURDAY, 11) == "F2"
        assert fascia_for_hour(SATURDAY, 3) == "F3"

    def test_sunday_is_entirely_off_peak(self):
        assert all(fascia_for_hour(SUNDAY, hour) == "F3" for hour in range(1, 25))

    def test_a_holiday_is_off_peak_even_midweek(self):
        # 25 April 2026 is a Saturday, so use Ferragosto, which is a Saturday too.
        # 1 May 2026 is a Friday: a working day that is still entirely F3.
        assert date(2026, 5, 1).weekday() == 4
        assert all(fascia_for_hour(date(2026, 5, 1), hour) == "F3" for hour in range(1, 25))


class TestDaylightSaving:
    def test_an_ordinary_day_has_24_hours(self):
        assert len(local_hour_starts(ORDINARY_TUESDAY)) == 24

    def test_the_short_day_has_23(self):
        assert len(local_hour_starts(SPRING_FORWARD)) == 23

    def test_the_long_day_has_25(self):
        assert len(local_hour_starts(FALL_BACK)) == 25

    @pytest.mark.parametrize("day", [ORDINARY_TUESDAY, SPRING_FORWARD, FALL_BACK])
    def test_no_hour_is_ever_repeated(self, day):
        # The original bug: two readings landing on the same instant, the second
        # overwriting the first after its value had gone into the running total.
        hours = local_hour_starts(day)
        assert len(set(hours)) == len(hours)

    @pytest.mark.parametrize("day", [ORDINARY_TUESDAY, SPRING_FORWARD, FALL_BACK])
    def test_hours_are_consecutive(self, day):
        hours = local_hour_starts(day)
        gaps = {(b - a).total_seconds() for a, b in zip(hours, hours[1:])}
        assert gaps == {3600.0}

    def test_the_24th_field_has_nowhere_to_go_on_the_short_day(self):
        assert hour_start_for_field(SPRING_FORWARD, 24) is None
        assert hour_start_for_field(SPRING_FORWARD, 23) is not None

    def test_every_field_maps_on_the_long_day(self):
        # 25 fields would be needed to cover it; E.ON send 24, so the last local
        # hour simply goes unreported rather than being folded onto another.
        assert all(hour_start_for_field(FALL_BACK, h) is not None for h in range(1, 25))

    def test_out_of_range_fields_are_rejected(self):
        assert hour_start_for_field(ORDINARY_TUESDAY, 0) is None
        assert hour_start_for_field(ORDINARY_TUESDAY, 25) is None


class TestItalianDates:
    def test_parses(self):
        assert parse_italian_date("15/09/2026") == date(2026, 9, 15)

    @pytest.mark.parametrize("value", [None, "", "nonsense", "2026-09-15", "32/01/2026"])
    def test_rejects_anything_else(self, value):
        assert parse_italian_date(value) is None
