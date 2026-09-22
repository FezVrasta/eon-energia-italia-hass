"""Italian electricity tariff bands, holidays, and E.ON's hourly field layout.

This is the domain knowledge that sits between E.ON's raw response and anything
useful: which of the three ARERA bands an hour falls in, which days are national
holidays, and how the twenty-four `valore_hNN` fields map onto real instants when
the local day is not twenty-four hours long.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .const import FASCIA_F1, FASCIA_F2, FASCIA_F3

#: E.ON bill Italian supplies, so this is the timezone the hourly fields are in.
ITALY = ZoneInfo("Europe/Rome")

#: Italian national holidays that fall on the same date every year, as (month, day).
ITALIAN_HOLIDAYS_FIXED: tuple[tuple[int, int], ...] = (
    (1, 1),  # Capodanno
    (1, 6),  # Epifania
    (4, 25),  # Festa della Liberazione
    (5, 1),  # Festa dei Lavoratori
    (6, 2),  # Festa della Repubblica
    (8, 15),  # Ferragosto
    (11, 1),  # Ognissanti
    (12, 8),  # Immacolata Concezione
    (12, 25),  # Natale
    (12, 26),  # Santo Stefano
)


def easter_sunday(year: int) -> date:
    """Return Easter Sunday, by the Anonymous Gregorian algorithm."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month, day_offset = divmod(h + ell - 7 * m + 114, 31)
    return date(year, month, day_offset + 1)


def is_italian_holiday(day: date | datetime) -> bool:
    """Whether `day` is an Italian national holiday.

    Easter Monday is the only moving holiday that can land on a weekday, so it is
    the only one worth computing; Easter Sunday itself is already off-peak.
    """
    if isinstance(day, datetime):
        day = day.date()

    if (day.month, day.day) in ITALIAN_HOLIDAYS_FIXED:
        return True

    easter_monday = easter_sunday(day.year) + timedelta(days=1)
    return (day.month, day.day) == (easter_monday.month, easter_monday.day)


def fascia_for_hour(day: date | datetime, hour: int) -> str:
    """Return the ARERA tariff band for an hour of a day.

    `hour` is E.ON's 1-based field number, so hour 1 covers 00:00-01:00 and hour
    24 covers 23:00-00:00.

    F1 peak      Mon-Fri 08:00-19:00
    F2 mid-peak  Mon-Fri 07:00-08:00 and 19:00-23:00, Sat 07:00-23:00
    F3 off-peak  nights 23:00-07:00, Sundays, and national holidays
    """
    if isinstance(day, datetime):
        day = day.date()

    start_hour = hour - 1
    weekday = day.weekday()  # 0 = Monday

    if weekday == 6 or is_italian_holiday(day):
        return FASCIA_F3

    if weekday == 5:
        return FASCIA_F2 if 7 <= start_hour < 23 else FASCIA_F3

    if 8 <= start_hour < 19:
        return FASCIA_F1
    if start_hour == 7 or 19 <= start_hour < 23:
        return FASCIA_F2
    return FASCIA_F3


def local_hour_starts(day: date | datetime, tz: ZoneInfo = ITALY) -> list[datetime]:
    """Return every hour start in a local calendar day, as UTC instants.

    Usually 24, but 23 on the spring-forward day and 25 on the autumn one.

    The obvious `midnight + timedelta(hours=n)` is wrong on those two days:
    timedelta arithmetic on an aware datetime is wall-clock, so it bumps the naive
    fields and keeps the same offset. That yields 24 hours on every day of the
    year and never surfaces the repeated or missing one. In October it put two of
    E.ON's readings on the same instant, and the second overwrote the first after
    its value had already gone into the running total.

    Stepping in UTC makes the arithmetic absolute, which is the whole point.
    """
    if isinstance(day, datetime):
        day = day.date()

    start = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(timezone.utc)
    following = day + timedelta(days=1)
    end = datetime(
        following.year, following.month, following.day, tzinfo=tz
    ).astimezone(timezone.utc)

    count = round((end - start) / timedelta(hours=1))
    return [start + timedelta(hours=index) for index in range(count)]


def hour_start_for_field(
    day: date | datetime, hour: int, tz: ZoneInfo = ITALY
) -> datetime | None:
    """Map one `valore_hNN` field to an instant, or None if it has none.

    E.ON always send `valore_h01` through `valore_h24`, so on a 23-hour day the
    last field has nowhere to go and on a 25-hour day the final hour goes
    unreported. Both are reported honestly - a dropped field or a gap - rather
    than folded onto a neighbouring hour, which is what produced double counts.
    """
    hours = local_hour_starts(day, tz)
    if not 1 <= hour <= len(hours):
        return None
    return hours[hour - 1]


def parse_italian_date(value: str | None) -> date | None:
    """Parse E.ON's `DD/MM/YYYY` dates, returning None for anything unusable."""
    if not value:
        return None
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date()
    except (ValueError, AttributeError):
        return None
