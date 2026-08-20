"""RPM (capacity) settlement: formula, loader, runner emission.

Capacity is a flat monthly accrual at the BRA-cleared price for
the asset's (Delivery Year, LDA). No PAH events; strategies don't bid.
The engine fires one `MonthlyCapacityAccrual` per fully-in-window month
and emits one `Capacity_RPM` row per event.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from pjm_engine.battery import ASSETS, AssetConfig
from pjm_engine.data import dy_start_year_for_month, load_rpm_clearing_prices
from pjm_engine.events import MonthlyCapacityAccrual
from pjm_engine.markets import settle_capacity
from pjm_engine.runner import (
    BacktestResult,
    _build_events,
    _handle_monthly_capacity_accrual,
)

UTC = ZoneInfo("UTC")


# ─── Formula ──────────────────────────────────────────────────────────────────


def test_settle_capacity_basic():
    """200 MW UCAP × $269.92/MW-day × 31 days = $1,673,504."""
    rev = settle_capacity(ucap_mw=200.0, price_per_mw_day=269.92, days_in_month=31)
    assert rev == pytest.approx(200.0 * 269.92 * 31, rel=1e-9)


def test_settle_capacity_zero_ucap_returns_zero():
    """Asset with no capacity commitment gets no capacity revenue."""
    assert settle_capacity(ucap_mw=0.0, price_per_mw_day=269.92, days_in_month=31) == 0.0


def test_settle_capacity_zero_price_returns_zero():
    """Empty/missing BRA price → zero, not NaN."""
    assert settle_capacity(ucap_mw=200.0, price_per_mw_day=0.0, days_in_month=31) == 0.0


def test_settle_capacity_negative_ucap_treated_as_zero():
    """Defensive: bad config (negative UCAP) returns 0 not negative revenue."""
    assert settle_capacity(ucap_mw=-50.0, price_per_mw_day=100.0, days_in_month=30) == 0.0


# ─── Delivery-year mapping ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "year,month,expected_dy",
    [
        (2025, 6, 2025),  # June starts DY 2025/26
        (2025, 12, 2025),  # December still in DY 2025/26
        (2026, 1, 2025),  # January 2026 = second half of DY 2025/26
        (2026, 5, 2025),  # May 2026 last month of DY 2025/26
        (2026, 6, 2026),  # June 2026 starts DY 2026/27
        (2025, 5, 2024),  # May 2025 last month of DY 2024/25
    ],
)
def test_dy_start_year_for_month(year, month, expected_dy):
    assert dy_start_year_for_month(year, month) == expected_dy


# ─── Loader ───────────────────────────────────────────────────────────────────


def test_load_rpm_clearing_prices_returns_2025_26_for_our_ldas():
    """The example asset's LDA (RTO) has a non-zero BRA clearing price
    for DY 2025/26."""
    prices = load_rpm_clearing_prices()
    assert not prices.empty
    for lda in ["RTO"]:
        match = prices[(prices["dy_start_year"] == 2025) & (prices["lda"] == lda)]
        assert not match.empty, f"no DY 2025/26 BRA price for LDA {lda}"
        assert float(match["price_per_mw_day"].iloc[0]) > 0


# ─── Event scheduling ─────────────────────────────────────────────────────────


def test_build_events_emits_one_capacity_event_per_full_month():
    """Oct 1, 2025 → Mar 31, 2026 spans 6 full months → 6 capacity events."""
    sched = _build_events(
        ASSETS["example_a"],
        start_date=date(2025, 10, 1),
        end_date=date(2026, 3, 31),
    )
    cap_events = [e for e in sched.drain() if isinstance(e, MonthlyCapacityAccrual)]
    assert len(cap_events) == 6

    expected = [
        (2025, 10, 31),
        (2025, 11, 30),
        (2025, 12, 31),
        (2026, 1, 31),
        (2026, 2, 28),
        (2026, 3, 31),
    ]
    actual = sorted((e.year, e.month, e.days_in_month) for e in cap_events)
    assert actual == expected


def test_build_events_skips_partial_first_month():
    """Mid-month start should NOT accrue a partial first month — only fully-
    in-window months count. Asset operator who runs Oct 15 → Dec 31 misses
    October (which began before window) and gets Nov + Dec only."""
    sched = _build_events(
        ASSETS["example_a"],
        start_date=date(2025, 10, 15),
        end_date=date(2025, 12, 31),
    )
    cap_events = [e for e in sched.drain() if isinstance(e, MonthlyCapacityAccrual)]
    months = sorted((e.year, e.month) for e in cap_events)
    assert months == [(2025, 11), (2025, 12)]


# ─── Runner emission ──────────────────────────────────────────────────────────


def test_handle_monthly_capacity_accrual_emits_one_row():
    """Direct handler test: given a non-empty RPM table and an asset with
    UCAP, emit exactly one Capacity_RPM revenue row at the right magnitude."""
    asset = ASSETS["example_a"]  # ucap_mw = 125, lda = RTO
    rpm = pd.DataFrame(
        {
            "dy_start_year": [2025],
            "lda": ["RTO"],
            "price_per_mw_day": [269.92],
        }
    )
    event = MonthlyCapacityAccrual(
        timestamp=datetime(2025, 11, 1, tzinfo=UTC),
        asset_id=asset.asset_id,
        year=2025,
        month=11,
        days_in_month=30,
    )
    result = BacktestResult(asset=asset)

    _handle_monthly_capacity_accrual(event, asset, rpm, result)

    cap_rows = [r for r in result.revenue_rows if r.product == "Capacity_RPM"]
    assert len(cap_rows) == 1
    row = cap_rows[0]
    assert row.cleared_mw == 125.0
    assert row.clearing_price == pytest.approx(269.92, abs=1e-9)
    assert row.revenue == pytest.approx(125.0 * 269.92 * 30, abs=1e-9)
    assert row.formula_version == "rpm_v1"
    assert row.period_start_utc == datetime(2025, 11, 1, tzinfo=UTC)
    assert row.period_end_utc == datetime(2025, 12, 1, tzinfo=UTC)


def test_handle_capacity_skips_when_asset_has_zero_ucap():
    """An asset without a capacity commitment (ucap_mw = 0) emits no row."""
    asset = AssetConfig(
        asset_id="no_cap_battery",
        zone="PECO",
        power_mw=100,
        energy_mwh=400,
        pnode_id=99999,
        pnode_name="TEST",
        lda="EMAAC",
        ucap_mw=0.0,  # no capacity commitment
    )
    rpm = pd.DataFrame(
        {
            "dy_start_year": [2025],
            "lda": ["EMAAC"],
            "price_per_mw_day": [269.92],
        }
    )
    event = MonthlyCapacityAccrual(
        timestamp=datetime(2025, 11, 1, tzinfo=UTC),
        asset_id=asset.asset_id,
        year=2025,
        month=11,
        days_in_month=30,
    )
    result = BacktestResult(asset=asset)
    _handle_monthly_capacity_accrual(event, asset, rpm, result)
    assert result.revenue_rows == []


def test_handle_capacity_skips_when_lda_missing_from_rpm_table():
    """If the RPM table has no entry for the asset's (DY, LDA), skip silently
    rather than crash. Capacity is additive baseline; missing data → no row."""
    asset = ASSETS["example_a"]  # lda = RTO
    rpm = pd.DataFrame(
        {
            "dy_start_year": [2025],
            "lda": ["EMAAC"],  # not RTO
            "price_per_mw_day": [100.0],
        }
    )
    event = MonthlyCapacityAccrual(
        timestamp=datetime(2025, 11, 1, tzinfo=UTC),
        asset_id=asset.asset_id,
        year=2025,
        month=11,
        days_in_month=30,
    )
    result = BacktestResult(asset=asset)
    _handle_monthly_capacity_accrual(event, asset, rpm, result)
    assert result.revenue_rows == []


def test_handle_capacity_uses_correct_dy_for_jan_2026():
    """Jan 2026 must look up DY 2025/26 (start_year=2025), NOT DY 2026/27.
    Confirms the dy_start_year_for_month wiring."""
    asset = ASSETS["example_a"]  # ucap_mw = 125, lda = RTO
    rpm = pd.DataFrame(
        {
            "dy_start_year": [2025, 2026],
            "lda": ["RTO", "RTO"],
            "price_per_mw_day": [269.92, 999.99],  # different prices to disambiguate
        }
    )
    event = MonthlyCapacityAccrual(
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        asset_id=asset.asset_id,
        year=2026,
        month=1,
        days_in_month=31,
    )
    result = BacktestResult(asset=asset)
    _handle_monthly_capacity_accrual(event, asset, rpm, result)
    assert len(result.revenue_rows) == 1
    # Must use the DY 2025 price (269.92), not the DY 2026 price (999.99).
    assert result.revenue_rows[0].clearing_price == pytest.approx(269.92, abs=1e-9)


# ─── End-to-end: capacity rows show up in run_backtest output ─────────────────


def test_run_backtest_emits_capacity_rows_for_every_full_month():
    """Black-box: run a noop strategy on the example asset Oct 2025 → Mar 2026, expect
    6 Capacity_RPM rows totaling ucap × $/MW-day × total_days."""
    from pjm_engine.runner import run_backtest
    from pjm_engine.strategy_base import Acknowledgment, BaseStrategy

    class NoOp(BaseStrategy):
        def on_event(self, event, ctx):
            return Acknowledgment()

    asset = ASSETS["example_a"]
    result = run_backtest(
        strategy=NoOp(),
        asset=asset,
        start_date=date(2025, 10, 1),
        end_date=date(2026, 3, 31),
    )
    cap_rows = [r for r in result.revenue_rows if r.product == "Capacity_RPM"]
    assert len(cap_rows) == 6
    total_capacity_revenue = sum(r.revenue for r in cap_rows)
    expected_days = 31 + 30 + 31 + 31 + 28 + 31  # Oct → Mar 2026
    expected = asset.ucap_mw * 269.92 * expected_days
    assert total_capacity_revenue == pytest.approx(expected, rel=1e-6)
