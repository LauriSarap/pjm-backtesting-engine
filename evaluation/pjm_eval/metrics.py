"""All metrics in one module — revenue, capture, risk, physical.

KISS: hand-rolled Sharpe / drawdown rather than a stats library. Each
metric is a pure function: DataFrames in, scalars or DataFrames out.
No I/O, no plotting.
"""

from __future__ import annotations

import warnings

import pandas as pd

from .io import AssetConfig
from .time import EPT, TimeWindow, bucket_revenue

# ─── Revenue ─────────────────────────────────────────────────────────────


def total(rev: pd.DataFrame) -> float:
    return float(rev["revenue"].sum()) if len(rev) else 0.0


def by_product(rev: pd.DataFrame) -> pd.Series:
    return rev.groupby("product")["revenue"].sum().sort_values(ascending=False)


def per_bucket(rev: pd.DataFrame, freq: str) -> pd.DataFrame:
    return bucket_revenue(rev, freq)


def per_kw_year(rev: pd.DataFrame, asset: AssetConfig, window: TimeWindow) -> float:
    """Annualized $ per kW of nameplate. Industry-standard normalization."""
    yrs = window.years()
    if yrs <= 0 or asset.power_mw <= 0:
        return float("nan")
    return total(rev) / yrs / (asset.power_mw * 1000.0)


# ─── Capture rate ────────────────────────────────────────────────────────


def capture_rate(
    strategy_total: float, baseline_total: float, ceiling_total: float | None
) -> float:
    """Where does `strategy` land between baseline (floor) and ceiling?

    Returns NaN if ceiling is None (no perfect-foresight available yet).
    Raises if baseline ≈ ceiling (degenerate, division by zero).
    Warns if strategy beats ceiling (capture > 1) — usually a bug.
    """
    if ceiling_total is None:
        return float("nan")
    spread = ceiling_total - baseline_total
    if abs(spread) < 1e-9:
        raise ValueError(
            f"capture_rate undefined: baseline ({baseline_total}) ≈ ceiling ({ceiling_total})"
        )
    rate = (strategy_total - baseline_total) / spread
    if rate > 1.0 + 1e-9:
        warnings.warn(
            f"capture_rate = {rate:.2%} > 100%: strategy beats ceiling. "
            "Likely a wrong ceiling or an engine bug."
        )
    return float(rate)


# ─── Risk (hand-rolled, no third-party deps) ─────────────────────────────


def daily_pnl(rev: pd.DataFrame) -> pd.Series:
    """Daily P&L indexed by EPT operating date.

    EPT-aware so DST days bucket correctly: a fall-back day (25 EPT
    hours) sums 25 hourly rows into one daily entry.
    """
    if len(rev) == 0:
        return pd.Series(dtype=float, name="revenue")
    ts = pd.to_datetime(rev["period_start_utc"], utc=True).dt.tz_convert(EPT)
    daily = rev.groupby(ts.dt.date)["revenue"].sum()
    daily.index = pd.to_datetime(daily.index)
    daily.name = "revenue"
    return daily.sort_index()


def sharpe(daily: pd.Series, periods_per_year: int = 365) -> float:
    """Annualized Sharpe of daily P&L. 365 because energy markets run weekends."""
    if len(daily) < 2 or daily.std(ddof=1) < 1e-9:
        return 0.0
    return float(daily.mean() / daily.std(ddof=1) * (periods_per_year**0.5))


def max_drawdown(equity: pd.Series) -> float:
    """Largest peak-to-trough drop. Returns ≤ 0."""
    if len(equity) == 0:
        return 0.0
    return float((equity - equity.cummax()).min())


def worst_n(daily: pd.Series, n: int = 5) -> pd.Series:
    return daily.nsmallest(n)


# ─── Physical ────────────────────────────────────────────────────────────


def cycles(soc: pd.DataFrame) -> float:
    """Total full-equivalent cycles. cum_cycles is set by load_soc."""
    if "cum_cycles" not in soc.columns or len(soc) == 0:
        return 0.0
    return float(soc["cum_cycles"].iloc[-1])


def soc_distribution(
    soc: pd.DataFrame, bands: list[tuple[float, float]] | None = None
) -> pd.Series:
    """Fraction of MTUs in each SoC pct band. Bands sum to 1.0."""
    if bands is None:
        bands = [(0.0, 0.30), (0.30, 0.70), (0.70, 1.01)]
    if len(soc) == 0:
        return pd.Series(dtype=float)
    n = len(soc)
    out = {
        f"[{lo:.0%}, {hi:.0%})": float(((soc["soc_pct"] >= lo) & (soc["soc_pct"] < hi)).sum() / n)
        for lo, hi in bands
    }
    return pd.Series(out)


# ─── Attribution ─────────────────────────────────────────────────────────


def per_product_monthly(rev: pd.DataFrame) -> pd.DataFrame:
    """Pivot: rows = EPT month, cols = product, values = revenue."""
    if len(rev) == 0:
        return pd.DataFrame()
    df = rev.copy()
    ts = pd.to_datetime(df["period_start_utc"], utc=True).dt.tz_convert(EPT)
    # to_period requires naive timestamps; drop tz after the EPT conversion.
    df["_month"] = ts.dt.tz_localize(None).dt.to_period("M")
    return df.pivot_table(
        index="_month",
        columns="product",
        values="revenue",
        aggfunc="sum",
        fill_value=0.0,
    )
