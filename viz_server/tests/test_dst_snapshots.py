"""DST snapshot tests for EPT operating-day windows.

PJM operating-days run EPT 00:00 → next-EPT 00:00. On spring-forward day the
window spans 23 hours; on fall-back day 25 hours; otherwise 24. The frontend
builds these bounds via `eptDayWindow` (date-fns-tz), and the bitemporal
slicer re-uses them as raw UTC `target_from` / `target_to`. We pin a few
known DST-transition days here so any future timezone refactor breaks loudly.

Locked fixtures:
  * 2024-03-10 — spring-forward, 23-hour day
  * 2024-11-03 — fall-back, 25-hour day
  * 2024-05-01 — normal EDT day, 24 hours
  * 2024-01-15 — normal EST day, 24 hours
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from viz_server import runtime as rt_mod
from viz_server import series as series_mod

UTC = timezone.utc
EPT = ZoneInfo("America/New_York")


def _ept_day_window(yyyymmdd: str) -> tuple[datetime, datetime]:
    """Mirror the frontend's `eptDayWindow`: EPT 00:00 → next-day EPT 00:00,
    expressed in UTC. Spans 23/24/25h depending on DST transitions."""
    from_local = datetime.fromisoformat(f"{yyyymmdd}T00:00:00").replace(tzinfo=EPT)
    next_day = (from_local + timedelta(days=1)).date()
    to_local = datetime.combine(next_day, datetime.min.time()).replace(tzinfo=EPT)
    return from_local.astimezone(UTC), to_local.astimezone(UTC)


@pytest.fixture(scope="module")
def runtime():
    rt = rt_mod.preload()
    rt_mod.set_runtime(rt)
    yield rt


@pytest.mark.parametrize(
    "op_day,expected_hours",
    [
        ("2024-03-10", 23),  # spring-forward
        ("2024-11-03", 25),  # fall-back
        ("2024-05-01", 24),  # normal EDT
        ("2024-01-15", 24),  # normal EST
    ],
)
def test_ept_day_window_spans_correct_hours(op_day: str, expected_hours: int):
    target_from, target_to = _ept_day_window(op_day)
    span = target_to - target_from
    assert span == timedelta(hours=expected_hours), (
        f"{op_day}: expected {expected_hours}h, got {span}"
    )


@pytest.mark.parametrize(
    "op_day,expected_hours",
    [
        ("2024-03-10", 23),
        ("2024-11-03", 25),
        ("2024-05-01", 24),
        ("2024-01-15", 24),
    ],
)
def test_ept_day_window_aligns_to_local_midnight(op_day: str, expected_hours: int):
    """Both endpoints must land on EPT local midnight."""
    target_from, target_to = _ept_day_window(op_day)
    from_ept = target_from.astimezone(EPT)
    to_ept = target_to.astimezone(EPT)
    assert (from_ept.hour, from_ept.minute) == (0, 0), f"{op_day}: from not at EPT midnight"
    assert (to_ept.hour, to_ept.minute) == (0, 0), f"{op_day}: to not at EPT midnight"
    # Use UTC subtraction; subtracting zoneinfo-aware datetimes in EPT space
    # gives wall-clock delta (always 24h), masking the DST adjustment.
    assert (target_to - target_from).total_seconds() / 3600 == expected_hours


def test_spring_forward_skips_02_ept_hour(runtime):
    """On the spring-forward day, the EPT 02:00–03:00 wall-clock hour is
    skipped — meaning the UTC range covers 23 distinct hour-starts when
    bucketed at hourly resolution.

    We sanity-check via a feed whose loader publishes one row per hour
    (reg_prices, RMCCP/RMPCP) so the bucket count is observable. If the
    loader doesn't have data for this date we skip the test rather than
    fail (test data coverage is a separate concern).
    """
    target_from, target_to = _ept_day_window("2024-03-10")
    decision_time = target_to + timedelta(days=7)  # well after publish horizon

    res = series_mod.query(
        runtime,
        feeds=["reg_rmccp"],
        decision_time=decision_time,
        target_from=target_from,
        target_to=target_to,
        zone=None,
        resolution="1h",
    )

    df = res.frame[(res.frame["feed"] == "reg_rmccp") & res.frame["value"].notna()]
    rows = list(df.itertuples(index=False))
    if not rows:
        pytest.skip("reg_prices fixture lacks 2024-03-10 coverage")
    # 23 distinct hour-starts — no duplicate or missing buckets through DST.
    times = sorted({r.t for r in rows})  # type: ignore[attr-defined]
    assert len(times) == 23, f"expected 23 hourly buckets on spring-forward, got {len(times)}"


def test_fall_back_includes_25_distinct_hours(runtime):
    """On the fall-back day, EPT 01:00–02:00 wall-clock occurs twice (once in
    EDT, once in EST). The UTC view covers 25 distinct hour-starts — the
    repeat hour is two genuinely different UTC moments.
    """
    target_from, target_to = _ept_day_window("2024-11-03")
    decision_time = target_to + timedelta(days=7)

    res = series_mod.query(
        runtime,
        feeds=["reg_rmccp"],
        decision_time=decision_time,
        target_from=target_from,
        target_to=target_to,
        zone=None,
        resolution="1h",
    )

    df = res.frame[(res.frame["feed"] == "reg_rmccp") & res.frame["value"].notna()]
    rows = list(df.itertuples(index=False))
    if not rows:
        pytest.skip("reg_prices fixture lacks 2024-11-03 coverage")
    times = sorted({r.t for r in rows})  # type: ignore[attr-defined]
    assert len(times) == 25, f"expected 25 hourly buckets on fall-back, got {len(times)}"


def test_normal_day_24_hours(runtime):
    target_from, target_to = _ept_day_window("2024-05-01")
    decision_time = target_to + timedelta(days=7)

    res = series_mod.query(
        runtime,
        feeds=["reg_rmccp"],
        decision_time=decision_time,
        target_from=target_from,
        target_to=target_to,
        zone=None,
        resolution="1h",
    )

    df = res.frame[(res.frame["feed"] == "reg_rmccp") & res.frame["value"].notna()]
    rows = list(df.itertuples(index=False))
    if not rows:
        pytest.skip("reg_prices fixture lacks 2024-05-01 coverage")
    times = sorted({r.t for r in rows})  # type: ignore[attr-defined]
    assert len(times) == 24, f"expected 24 hourly buckets on normal day, got {len(times)}"
