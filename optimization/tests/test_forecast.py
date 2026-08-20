"""Forecaster contract + concrete-forecaster tests.

Covers:
  1. PriceForecast post-init validation (correct row counts).
  2. PerfectOracleForecaster returns realized prices for the requested window.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from pjm_optimization.forecast import (
    PerfectOracleForecaster,
    PriceForecast,
)

UTC = timezone.utc
ONE_HOUR = timedelta(hours=1)
MTU = timedelta(minutes=5)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_da_df(start: datetime, n_hours: int, base_price: float, zone: str = "PECO"):
    rows = []
    for h in range(n_hours):
        ts = start + h * ONE_HOUR
        # Distinct price per hour to test mapping correctness.
        rows.append(
            {
                "datetime_beginning_utc": ts,
                "zone": zone,
                "total_lmp_da": base_price + h,
            }
        )
    return pd.DataFrame(rows)


def _make_rt_df(start: datetime, n_hours: int, base_price: float, zone: str = "PECO"):
    rows = []
    for t in range(n_hours * 12):
        ts = start + t * MTU
        rows.append(
            {
                "datetime_beginning_utc": ts,
                "zone": zone,
                "total_lmp_rt": base_price + 0.1 * t,
            }
        )
    return pd.DataFrame(rows)


# ─── PriceForecast validation ────────────────────────────────────────────────


def test_price_forecast_validates_row_counts():
    start = datetime(2024, 6, 15, 4, 0, tzinfo=UTC)
    da = pd.Series([50.0] * 24, index=pd.DatetimeIndex([start + h * ONE_HOUR for h in range(24)]))
    rt = pd.Series(
        [50.0] * 287,  # one short
        index=pd.DatetimeIndex([start + t * MTU for t in range(287)]),
    )
    with pytest.raises(ValueError, match="rt_lmps has 287"):
        PriceForecast(start_utc=start, horizon_hours=24, da_lmps=da, rt_lmps=rt)


def test_price_forecast_correct_shape_constructs():
    start = datetime(2024, 6, 15, 4, 0, tzinfo=UTC)
    da = pd.Series([50.0] * 24, index=pd.DatetimeIndex([start + h * ONE_HOUR for h in range(24)]))
    rt = pd.Series([50.0] * 288, index=pd.DatetimeIndex([start + t * MTU for t in range(288)]))
    fc = PriceForecast(start_utc=start, horizon_hours=24, da_lmps=da, rt_lmps=rt)
    assert len(fc.da_lmps) == 24
    assert len(fc.rt_lmps) == 288


# ─── PerfectOracleForecaster ─────────────────────────────────────────────────


def test_perfect_oracle_returns_realized_prices():
    """Returned series must equal the realized DataFrame slice for the horizon."""
    start = datetime(2024, 6, 15, 4, 0, tzinfo=UTC)
    da_df = _make_da_df(start, n_hours=72, base_price=30.0)
    rt_df = _make_rt_df(start, n_hours=72, base_price=30.0)

    f = PerfectOracleForecaster(da_df, rt_df)
    fc = f.forecast(
        as_of=datetime(2024, 6, 14, 15, 0, tzinfo=UTC),  # cutoff irrelevant for oracle
        start_utc=start + 24 * ONE_HOUR,
        horizon_hours=24,
        zone="PECO",
    )
    assert len(fc.da_lmps) == 24
    assert len(fc.rt_lmps) == 288
    # Hour 24 should have base + 24 = 54.
    assert fc.da_lmps.iloc[0] == pytest.approx(54.0)
    assert fc.da_lmps.iloc[-1] == pytest.approx(77.0)


def test_perfect_oracle_filters_by_zone():
    """Multiple zones in source df: only requested zone's rows return."""
    start = datetime(2024, 6, 15, 4, 0, tzinfo=UTC)
    peco = _make_da_df(start, n_hours=24, base_price=30.0, zone="PECO")
    pseg = _make_da_df(start, n_hours=24, base_price=200.0, zone="PSEG")
    da_all = pd.concat([peco, pseg], ignore_index=True)
    rt_all = pd.concat(
        [
            _make_rt_df(start, n_hours=24, base_price=30.0, zone="PECO"),
            _make_rt_df(start, n_hours=24, base_price=200.0, zone="PSEG"),
        ],
        ignore_index=True,
    )

    f = PerfectOracleForecaster(da_all, rt_all)
    fc = f.forecast(
        as_of=datetime(2024, 6, 14, 15, 0, tzinfo=UTC),
        start_utc=start,
        horizon_hours=24,
        zone="PSEG",
    )
    # PSEG base 200, hour 0 → 200.
    assert fc.da_lmps.iloc[0] == pytest.approx(200.0)
