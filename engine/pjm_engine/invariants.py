"""Floor invariants — engine-bug detectors that run after every event.

If any of these fires, it's an engine bug, not a strategy bug. Hard-fail loud.
"""

from __future__ import annotations

from collections import Counter

from .battery import AssetConfig
from .errors import InvariantError
from .settle import RevenueRow


def assert_no_double_counted_revenue_rows(rows: list[RevenueRow]) -> None:
    """No two rows share (asset_id, product, period_start, formula_version).

    Catches engine bugs where revenue is double-emitted: a held SR position
    settling twice for the same MTU, a Reg credit logged at both gates, etc."""
    keys = [(r.asset_id, r.product, r.period_start_utc, r.formula_version) for r in rows]
    dups = [k for k, n in Counter(keys).items() if n > 1]
    if dups:
        sample = dups[:3]
        raise InvariantError(
            f"{len(dups)} duplicate revenue rows by "
            f"(asset_id, product, period_start, formula_version); first: {sample}"
        )


def assert_soc_in_bounds(soc_mwh: float, asset: AssetConfig, tol: float = 1e-6) -> None:
    if soc_mwh < asset.soc_min_mwh - tol or soc_mwh > asset.soc_max_mwh + tol:
        raise InvariantError(
            f"SoC {soc_mwh} MWh outside [{asset.soc_min_mwh}, {asset.soc_max_mwh}] "
            f"for {asset.asset_id}"
        )


def assert_award_within_bid(cleared_mw: float, bid_mw: float, tol: float = 1e-6) -> None:
    if abs(cleared_mw) - abs(bid_mw) > tol:
        raise InvariantError(f"cleared |{cleared_mw}| > bid |{bid_mw}|")
