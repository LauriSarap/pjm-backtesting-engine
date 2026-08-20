"""CRITICAL tests: DST and regime-boundary handling.

Without DST tests, every $/kW-yr is silently wrong twice a year.
Without regime split, pre/post-redesign comparisons silently mix
v1 and v2 reg formulas.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from pjm_eval.time import (
    REGIME_BOUNDARIES,
    TimeWindow,
    bucket_revenue,
    split_at_regime,
)
from tests.conftest import make_revenue

UTC = timezone.utc


# ─── TimeWindow ───────────────────────────────────────────────────────────


def test_timewindow_rejects_naive_datetimes():
    with pytest.raises(ValueError, match="tz-aware"):
        TimeWindow(datetime(2025, 1, 1), datetime(2025, 2, 1))


def test_timewindow_rejects_inverted():
    with pytest.raises(ValueError, match="must be <"):
        TimeWindow(datetime(2025, 2, 1, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC))


def test_timewindow_years():
    w = TimeWindow(datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    assert abs(w.years() - 1.0) < 0.01


# ─── Regime split ─────────────────────────────────────────────────────────


def test_split_pre_redesign_only():
    w = TimeWindow(datetime(2024, 6, 1, tzinfo=UTC), datetime(2024, 9, 1, tzinfo=UTC))
    assert split_at_regime(w) == [w]


def test_split_post_redesign_only():
    w = TimeWindow(datetime(2025, 11, 1, tzinfo=UTC), datetime(2026, 3, 1, tzinfo=UTC))
    assert split_at_regime(w) == [w]


def test_split_crossing_boundary():
    w = TimeWindow(datetime(2025, 9, 1, tzinfo=UTC), datetime(2025, 11, 1, tzinfo=UTC))
    parts = split_at_regime(w)
    boundary = REGIME_BOUNDARIES["v1_to_v2"]
    assert len(parts) == 2
    assert parts[0].end == boundary
    assert parts[1].start == boundary


def test_split_window_starts_at_boundary():
    boundary = REGIME_BOUNDARIES["v1_to_v2"]
    w = TimeWindow(boundary, boundary + timedelta(days=30))
    # Boundary is the start, not strictly inside (start, end), so no split.
    assert split_at_regime(w) == [w]


# ─── DST: spring-forward (23-hour day) ────────────────────────────────────


def test_dst_spring_forward_23_hours():
    """2026-03-08: clocks jump 02:00 EPT → 03:00 EPT, so the EPT day has 23h.

    UTC timestamps from 05:00 UTC (= 00:00 EPT pre-DST) to 03:00 UTC next day
    (= 23:00 EPT post-DST). Each operating hour produces one row; total = 23.
    """
    rows = []
    # 23 hourly UTC rows starting at 2026-03-08 05:00 UTC.
    start = datetime(2026, 3, 8, 5, 0, tzinfo=UTC)
    for i in range(23):
        rows.append(
            {
                "period_start_utc": start + timedelta(hours=i),
                "revenue": 100.0,
            }
        )
    rev = make_revenue(rows)

    daily = bucket_revenue(rev, "1D")
    # Should have exactly one EPT date (2026-03-08) with sum = 23 * 100.
    assert len(daily) == 1
    assert daily["operating_date_ept"].iloc[0] == pd.to_datetime("2026-03-08").date()
    assert daily["revenue"].iloc[0] == 23 * 100.0


def test_dst_fall_back_25_hours():
    """2026-11-01: clocks fall 02:00 EPT → 01:00 EPT, so EPT day has 25h.

    UTC timestamps from 04:00 UTC (= 00:00 EPT pre-fallback) to 04:00 UTC next
    day (= 23:00 EPT post-fallback). 25 hourly rows.
    """
    rows = []
    start = datetime(2026, 11, 1, 4, 0, tzinfo=UTC)
    for i in range(25):
        rows.append(
            {
                "period_start_utc": start + timedelta(hours=i),
                "revenue": 100.0,
            }
        )
    rev = make_revenue(rows)

    daily = bucket_revenue(rev, "1D")
    assert len(daily) == 1
    assert daily["operating_date_ept"].iloc[0] == pd.to_datetime("2026-11-01").date()
    assert daily["revenue"].iloc[0] == 25 * 100.0


# ─── Sum conservation across bucketing ────────────────────────────────────


def test_bucket_conservation():
    rows = [
        {"period_start_utc": datetime(2025, 12, 1, h, tzinfo=UTC), "revenue": 10.0 * h}
        for h in range(24)
    ]
    rev = make_revenue(rows)
    expected = sum(10.0 * h for h in range(24))

    for freq in ("1h", "1D", "1ME"):
        out = bucket_revenue(rev, freq)
        assert abs(out["revenue"].sum() - expected) < 1e-6, f"freq={freq}"
