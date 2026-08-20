"""Metric correctness tests. CRITICAL: capture-rate div-by-zero."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from pjm_eval import metrics
from pjm_eval.io import AssetConfig
from pjm_eval.time import TimeWindow
from tests.conftest import make_revenue

UTC = timezone.utc


# ─── Revenue ──────────────────────────────────────────────────────────────


def test_total_empty():
    assert metrics.total(pd.DataFrame({"revenue": []})) == 0.0


def test_total_and_by_product_consistent():
    rev = make_revenue(
        [
            {
                "period_start_utc": datetime(2025, 11, 1, h, tzinfo=UTC),
                "product": "DA_Energy",
                "revenue": 100.0,
            }
            for h in range(24)
        ]
        + [
            {
                "period_start_utc": datetime(2025, 11, 1, h, tzinfo=UTC),
                "product": "RT_Energy",
                "revenue": -10.0,
            }
            for h in range(24)
        ]
    )
    assert abs(metrics.total(rev) - metrics.by_product(rev).sum()) < 1e-9


def test_per_kw_year_scales_linearly(asset_a: AssetConfig):
    """Doubling window → halving $/kW-yr (same total $)."""
    rev = make_revenue(
        [{"period_start_utc": datetime(2025, 1, 1, tzinfo=UTC), "revenue": 1_000_000.0}]
    )
    w_short = TimeWindow(datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 7, 1, tzinfo=UTC))
    w_long = TimeWindow(datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    short_kwyr = metrics.per_kw_year(rev, asset_a, w_short)
    long_kwyr = metrics.per_kw_year(rev, asset_a, w_long)
    # Half-year window → twice the $/kW-yr.
    assert abs(short_kwyr / long_kwyr - 2.0) < 0.02


# ─── Capture rate ─────────────────────────────────────────────────────────


def test_capture_rate_baseline_floor():
    assert metrics.capture_rate(0.0, baseline_total=0.0, ceiling_total=100.0) == 0.0


def test_capture_rate_at_ceiling():
    assert metrics.capture_rate(100.0, baseline_total=0.0, ceiling_total=100.0) == 1.0


def test_capture_rate_halfway():
    assert metrics.capture_rate(50.0, baseline_total=0.0, ceiling_total=100.0) == 0.5


def test_capture_rate_no_ceiling_returns_nan():
    out = metrics.capture_rate(50.0, baseline_total=0.0, ceiling_total=None)
    assert out != out  # NaN


def test_capture_rate_div_by_zero_raises():
    with pytest.raises(ValueError, match="undefined"):
        metrics.capture_rate(50.0, baseline_total=100.0, ceiling_total=100.0)


def test_capture_rate_strategy_beats_ceiling_warns():
    with pytest.warns(UserWarning, match="beats ceiling"):
        rate = metrics.capture_rate(150.0, baseline_total=0.0, ceiling_total=100.0)
    assert rate == 1.5


# ─── Risk ─────────────────────────────────────────────────────────────────


def test_sharpe_constant_series_is_zero():
    daily = pd.Series([100.0] * 30)
    assert metrics.sharpe(daily) == 0.0


def test_sharpe_short_series_is_zero():
    assert metrics.sharpe(pd.Series([100.0])) == 0.0


def test_max_drawdown_monotone_up_is_zero():
    equity = pd.Series([1, 2, 3, 4, 5], dtype=float).cumsum()
    assert metrics.max_drawdown(equity) == 0.0


def test_max_drawdown_known_dip():
    equity = pd.Series([0, 100, 50, 80, 30], dtype=float)
    # Peak = 100, trough after = 30, drawdown = -70.
    assert metrics.max_drawdown(equity) == -70.0


def test_worst_n():
    daily = pd.Series([10.0, -5.0, 3.0, -20.0, 7.0])
    worst = metrics.worst_n(daily, n=2)
    assert sorted(worst.values.tolist()) == [-20.0, -5.0]


def test_daily_pnl_buckets_by_ept_date():
    """Hours within one EPT operating day sum into one daily bucket."""
    # 24 UTC-spanning rows that all fall in 2025-11-15 EPT.
    # 2025-11-15 EPT = 2025-11-15 05:00 UTC → 2025-11-16 05:00 UTC (post-DST so EST = UTC-5).
    rows = [
        {
            "period_start_utc": datetime(2025, 11, 15, 5, tzinfo=UTC) + timedelta(hours=h),
            "revenue": 10.0,
        }
        for h in range(24)
    ]
    rev = make_revenue(rows)
    daily = metrics.daily_pnl(rev)
    assert len(daily) == 1
    assert daily.iloc[0] == 240.0


# ─── Physical ─────────────────────────────────────────────────────────────


def test_cycles_full_charge_discharge_is_one_cycle(asset_a: AssetConfig):
    """SoC trajectory: 50% → 90% → 10% → 50% on a 1000 MWh asset."""
    soc = pd.DataFrame(
        {
            "ts_utc": pd.to_datetime(
                [
                    "2025-11-01 00:00",
                    "2025-11-01 04:00",
                    "2025-11-01 12:00",
                    "2025-11-01 16:00",
                ],
                utc=True,
            ),
            "soc_mwh": [500.0, 900.0, 100.0, 500.0],
        }
    )
    # Δ = [-, 400, 800, 400]; |Δ|.sum() = 1600 MWh (excluding first NaN).
    # cycles = 1600 / (2 * 1000) = 0.8.
    soc["cum_cycles"] = soc["soc_mwh"].diff().abs().fillna(0).cumsum() / (2 * asset_a.energy_mwh)
    assert abs(metrics.cycles(soc) - 0.8) < 1e-6


def test_soc_distribution_sums_to_one():
    soc = pd.DataFrame({"soc_pct": [0.1, 0.2, 0.3, 0.5, 0.7, 0.85, 0.95]})
    dist = metrics.soc_distribution(soc)
    assert abs(dist.sum() - 1.0) < 1e-9


# ─── Attribution ──────────────────────────────────────────────────────────


def test_per_product_monthly_sums_to_total():
    rev = make_revenue(
        [
            {
                "period_start_utc": datetime(2025, 11, 1, h, tzinfo=UTC),
                "product": p,
                "revenue": 100.0,
            }
            for p in ("DA_Energy", "RT_Energy")
            for h in range(24)
        ]
    )
    pivot = metrics.per_product_monthly(rev)
    assert abs(pivot.values.sum() - metrics.total(rev)) < 1e-9
