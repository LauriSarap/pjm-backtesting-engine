"""Settlement orchestrator.

Walks each Award through the formula for its product and emits RevenueRows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from .errors import RegimeBoundaryError
from .markets import settle_da_energy, settle_reg_v1, settle_reserve, settle_rt_energy
from .strategy_base import Product


@dataclass(frozen=True)
class PriceTables:
    """O(1) hash lookups for engine-side clearing.

    Pandas boolean-mask lookups (`df[(df.zone==z) & (df.utc==t)]`) at every
    one of ~9000 RT gates per backtest were the dominant cost. Building these
    dicts once per backtest reduces price lookup from O(N) per call to O(1).
    """

    da_lmp_by_hour: dict  # hour_start_utc → DA LMP
    rt_lmp_by_mtu: dict  # mtu_start_utc → RT LMP
    da_sr_by_hour: dict  # hour_start_utc → DA SRMCP
    rt_sr_by_mtu: dict  # mtu_start_utc → RT SRMCP
    da_sec_by_hour: dict  # hour_start_utc → DA 30-Minute MCP
    rt_sec_by_mtu: dict  # mtu_start_utc → RT 30-Minute MCP
    reg_by_mtu: dict  # mtu_start_utc → (RMCCP, RMPCP); v2 5-min split
    # Defaults to empty so v2-only test fixtures don't need to pass it; the
    # production builder populates it for callers running pre-2025-10 dates.
    reg_v1_by_hour: dict = field(default_factory=dict)
    # Per-period regulation performance score from `reg_market_results.rto_perfscore`.
    # Keys are hour_start_utc pre-redesign (hourly cadence) and half_hour_start_utc
    # post-redesign (half-hourly cadence). Settlement sites lookup with the right
    # granularity and fall back to `runner.REG_PERF_SCORE` when missing.
    # Mileage ratio stays at 1.0 under perfect tracking (the `rega_mileage`
    # column is the benchmark in MW-of-movement, not a ratio).
    perf_score_by_period: dict = field(default_factory=dict)


def build_price_tables(
    da_lmps: pd.DataFrame,
    rt_lmps: pd.DataFrame,
    reg_prices: pd.DataFrame,
    da_sr_prices: pd.DataFrame,
    rt_sr_prices: pd.DataFrame,
    da_sec_prices: pd.DataFrame,
    rt_sec_prices: pd.DataFrame,
    reg_market_results: pd.DataFrame | None = None,
) -> PriceTables:
    """Frames must already be zone-filtered (only one row per timestamp).

    `reg_market_results` is the system-wide regulation procurement summary
    (`load_reg_market_results()`). Used to populate `reg_v1_by_hour` —
    `(rega_hourly, regd_hourly)` for each pre-2025-10 hour. Pass None to skip
    v1 wiring (v2-only callers) and `reg_v1_by_hour` will be empty.
    """
    reg_v1_by_hour: dict = {}
    perf_score_by_period: dict = {}
    if reg_market_results is not None and not reg_market_results.empty:
        # Pre-redesign rows have BOTH `rega_hourly` and `regd_hourly` populated;
        # post-redesign rows have only `rega_*`. Filter to the v1 window so we
        # don't index v2 rows under v1 keys (which would silently mis-price).
        from .validation import REG_V2_START_EPT

        v1 = reg_market_results[reg_market_results["datetime_beginning_utc"] < REG_V2_START_EPT]
        for ts, rega, regd in zip(
            v1["datetime_beginning_utc"],
            v1["rega_hourly"],
            v1["regd_hourly"],
        ):
            # NaN protection: pre-redesign rows should always have both, but
            # if a row is half-populated skip it rather than emitting NaN $.
            if pd.notna(rega) and pd.notna(regd):
                reg_v1_by_hour[ts] = (float(rega), float(regd))

        # Per-period performance score for both regimes. Pre-redesign rows are
        # hourly (HH:00); post-redesign rows are half-hourly (HH:00 + HH:30).
        # We populate the same dict for both — settlement-site lookups use the
        # right granularity (`hour_start` for v1, `half_hour_start` for v2).
        for ts, score in zip(
            reg_market_results["datetime_beginning_utc"],
            reg_market_results["rto_perfscore"],
        ):
            if pd.notna(score):
                perf_score_by_period[ts] = float(score)

    return PriceTables(
        da_lmp_by_hour=dict(zip(da_lmps["datetime_beginning_utc"], da_lmps["total_lmp_da"])),
        rt_lmp_by_mtu=dict(zip(rt_lmps["datetime_beginning_utc"], rt_lmps["total_lmp_rt"])),
        da_sr_by_hour=dict(zip(da_sr_prices["datetime_beginning_utc"], da_sr_prices["mcp"])),
        rt_sr_by_mtu=dict(zip(rt_sr_prices["datetime_beginning_utc"], rt_sr_prices["mcp"])),
        da_sec_by_hour=dict(zip(da_sec_prices["datetime_beginning_utc"], da_sec_prices["mcp"])),
        rt_sec_by_mtu=dict(zip(rt_sec_prices["datetime_beginning_utc"], rt_sec_prices["mcp"])),
        reg_by_mtu={
            ts: (float(rmccp), float(rmpcp))
            for ts, rmccp, rmpcp in zip(
                reg_prices["datetime_beginning_utc"],
                reg_prices["rmccp"],
                reg_prices["rmpcp"],
            )
        },
        reg_v1_by_hour=reg_v1_by_hour,
        perf_score_by_period=perf_score_by_period,
    )


@dataclass(frozen=True)
class Award:
    """A cleared bid: how much, when, at what price.

    For RT_Energy awards, `da_cleared_mw` is the parent hour's DA position
    (engine-populated at clearing). For DA_Energy and others, it's unused (= 0).
    For RegA_v1 / RegD_v1 awards, `perf_score` carries the performance score
    applied at settlement (default 1.0; runner stamps with `REG_PERF_SCORE`).
    """

    product: Product
    period_start: datetime  # tz-aware UTC, MTU/hour start
    period_end: datetime  # tz-aware UTC, exclusive
    cleared_mw: float  # signed: + discharge, − charge (= actual delivery for RT)
    clearing_price: float  # $/MWh
    da_cleared_mw: float = 0.0  # for RT_Energy: DA position at parent hour
    perf_score: float = 1.0  # for Reg_v1: applied at settlement


@dataclass(frozen=True)
class RevenueRow:
    """Per-event, per-product, per-period dollar line. Output schema in design.md."""

    event_ts_utc: datetime
    asset_id: str
    product: str
    period_start_utc: datetime
    period_end_utc: datetime
    cleared_mw: float
    clearing_price: float
    revenue: float
    formula_version: str


def settle(award: Award, asset_id: str, event_ts: datetime) -> RevenueRow:
    """Dispatch on product → formula → RevenueRow."""
    hours = (award.period_end - award.period_start).total_seconds() / 3600.0

    if award.product == Product.DA_Energy:
        revenue = settle_da_energy(award.cleared_mw, award.clearing_price, hours)
        version = "v1"
    elif award.product == Product.RT_Energy:
        revenue = settle_rt_energy(
            actual_mw=award.cleared_mw,
            da_cleared_mw=award.da_cleared_mw,
            rt_lmp=award.clearing_price,
            hours=hours,
        )
        version = "v1"
    elif award.product in (Product.SR_RTO, Product.Sec_RTO):
        revenue = settle_reserve(award.cleared_mw, award.clearing_price, hours)
        version = "da_v1"
    elif award.product in (Product.RegA_v1, Product.RegD_v1):
        # Pre-redesign Reg: `cleared_MW × score × hourly_price × hours`.
        # `clearing_price` carries the v1 per-product hourly price (rega_hourly
        # for RegA awards, regd_hourly for RegD awards) and `perf_score` the
        # per-hour score — the runner sets both at award-construction time.
        revenue = settle_reg_v1(
            cleared_mw=award.cleared_mw,
            perf_score=award.perf_score,
            hourly_clearing_price=award.clearing_price,
            hours=hours,
        )
        version = "v1"
    else:
        raise RegimeBoundaryError(f"no settlement formula registered for {award.product}")

    return RevenueRow(
        event_ts_utc=event_ts,
        asset_id=asset_id,
        product=award.product.value,
        period_start_utc=award.period_start,
        period_end_utc=award.period_end,
        cleared_mw=award.cleared_mw,
        clearing_price=award.clearing_price,
        revenue=revenue,
        formula_version=version,
    )
