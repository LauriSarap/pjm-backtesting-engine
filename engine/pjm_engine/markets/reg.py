"""Regulation settlement — `Reg_v2` (post-2025-10) and `Reg_v1` (pre-2025-10).

`Reg_v2` (post-2025-10-01 redesign): single bidirectional product cleared on a
half-hour assignment block. Revenue settles every 5 minutes against per-MTU
RMCCP + mileage_ratio × RMPCP, scaled by performance score:

    reg_credit_i = cleared_MW × score × (RMCCP_i + mileage_ratio × RMPCP_i) × (1/12)

`Reg_v1` (pre-2025-10-01): two separately-priced products RegA (slow) and RegD
(fast). PJM published a single hourly clearing-price field per product
(`rega_hourly`, `regd_hourly`) that already aggregates capability + mileage:

    reg_v1_credit_h = cleared_MW × score × hourly_clearing_price × hours

Same shape for RegA and RegD — only the price source differs. Mileage benchmarks
exist (`rega_mileage`, `regd_mileage`) and a future iteration could split CCP
from PCP, but the all-in hourly price is the canonical settlement number for
backtesting and matches the post-redesign $/MW-h totals well enough for
strategy ranking.

Activation modeling (design.md):
    Asset is treated as perfect-tracking with score = 1.0; the bidirectional
    signal is assumed zero-mean so SoC is unchanged by Reg activity.
    Round-trip throughput losses are not modeled (see design.md calibration
    caveats).

This module owns only the dollar formulas. SoC reservation and power-stack
checks live in `validation.py`.
"""

from __future__ import annotations

# Per M28 §4.2.1/§4.2.2: 5-min Performance Score < 0.25 forfeits ALL Reg credit
# AND ALL Reg LOC for that interval. Threshold is exclusive (strictly below 25%
# forfeits; exactly 0.25 still pays).
REG_PERF_FORFEIT_THRESHOLD = 0.25


def settle_reg_v2(
    cleared_mw: float,
    perf_score: float,
    rmccp: float,
    rmpcp: float,
    mileage_ratio: float,
    hours: float = 1.0 / 12.0,
) -> float:
    """Reg credit for a single 5-min interval. Returns dollars.

    `cleared_mw` is the bidirectional capacity offered (always positive — the
    product is symmetric ±MW around midpoint, not a directional MW).
    """
    # M28 §4.2.1/§4.2.2: perf < 0.25 forfeits all Reg credit for the interval.
    if perf_score < REG_PERF_FORFEIT_THRESHOLD:
        return 0.0
    return cleared_mw * perf_score * (rmccp + mileage_ratio * rmpcp) * hours


def settle_reg_v1(
    cleared_mw: float,
    perf_score: float,
    hourly_clearing_price: float,
    hours: float = 1.0,
) -> float:
    """Pre-2025-10 Reg credit for a single hour. Returns dollars.

    `hourly_clearing_price` is `rega_hourly` or `regd_hourly` from PJM's
    `reg_market_results` feed — the all-in $/MW-h credit that already includes
    capability + performance/mileage payments. RegA and RegD share the same
    formula shape; the caller picks which price to pass.
    """
    # M28 §4.2.1/§4.2.2: perf < 0.25 forfeits all Reg credit for the interval.
    if perf_score < REG_PERF_FORFEIT_THRESHOLD:
        return 0.0
    return cleared_mw * perf_score * hourly_clearing_price * hours
