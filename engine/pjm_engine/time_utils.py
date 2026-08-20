"""DST-correct EPT operating-day grids in UTC.

PJM operating days are anchored to EPT midnight, so 23-hour (spring-forward)
and 25-hour (fall-back) days are the rule, not the exception. Naïve wall-time
arithmetic — `t += timedelta(hours=1)` on a tz-aware EPT datetime — does NOT
walk the canonical UTC grid: it duplicates a UTC hour at spring-forward
(02:00 EST is non-existent walltime, Python collapses it onto 03:00 EDT) and
skips a UTC hour at fall-back (01:00 EDT → 02:00 EST jumps over the second
01:00 EST). Both errors silently corrupt event scheduling and bid lists on
the four DST days each year.

Anchor in UTC, walk in UTC. UTC is monotonic; the EPT-midnight endpoints give
us 23/24/25 hours automatically.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

EPT = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
ONE_HOUR = timedelta(hours=1)
HALF_HOUR = timedelta(minutes=30)


def operating_hour_starts_utc(operating_date: date) -> list[datetime]:
    """UTC starts of every hour belonging to an EPT operating day.

    Length is 23 on spring-forward, 24 on a normal day, 25 on fall-back.
    Returned datetimes are tz-aware UTC and strictly increasing.
    """
    start_utc = datetime.combine(operating_date, datetime.min.time(), tzinfo=EPT).astimezone(UTC)
    end_utc = datetime.combine(
        operating_date + timedelta(days=1), datetime.min.time(), tzinfo=EPT
    ).astimezone(UTC)
    out: list[datetime] = []
    t = start_utc
    while t < end_utc:
        out.append(t)
        t += ONE_HOUR
    return out


def half_hour_starts_utc(operating_date: date) -> list[datetime]:
    """UTC starts of every half-hour belonging to an EPT operating day.

    Length is 46 on spring-forward, 48 on a normal day, 50 on fall-back.
    """
    start_utc = datetime.combine(operating_date, datetime.min.time(), tzinfo=EPT).astimezone(UTC)
    end_utc = datetime.combine(
        operating_date + timedelta(days=1), datetime.min.time(), tzinfo=EPT
    ).astimezone(UTC)
    out: list[datetime] = []
    t = start_utc
    while t < end_utc:
        out.append(t)
        t += HALF_HOUR
    return out
