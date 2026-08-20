"""RPM (Reliability Pricing Model) capacity revenue.

Capacity payments are PJM's third major revenue line for batteries, distinct
from energy and AS markets. Settlement is monthly: an asset accrues
`UCAP_MW × $/MW-day × days_in_month` for every month inside its capacity
commitment year. Clearing prices come from the Base Residual Auction (BRA)
which clears ~3 years before the Delivery Year.

**Modeling scope.** This module models the flat monthly accrual only:

  monthly_capacity_revenue = ucap_mw × price_per_mw_day × days_in_month

It does NOT model:
- **Performance Assessment Hour (PAH) penalties.** When PJM declares an
  emergency, every capacity-committed asset must deliver UCAP MW or pay
  a non-performance charge up to the entire annual capacity revenue.
- **Capacity offer / clearing logic.** We assume the asset cleared at the
  observed BRA clearing price for its (Delivery Year, LDA) — true for
  any asset that bid at or below the clearing price. A backtest is a
  retrospective so this is fine; live trading would need offer simulation.
- **ELCC class evolution.** The asset's UCAP MW is taken from
  `AssetConfig.ucap_mw` (set per-DY by the operator). PJM re-runs ELCC
  studies annually; the example asset uses 50% of nameplate, the DY
  2025/26 4-hr storage class rating.

PJM Delivery Year runs June 1 → May 31, so the loader's
`dy_start_year_for_month` maps a calendar month to the right DY before
the price lookup.
"""

from __future__ import annotations


def settle_capacity(
    ucap_mw: float,
    price_per_mw_day: float,
    days_in_month: int,
) -> float:
    """One month of capacity revenue at the cleared BRA price.

    Pure formula — no PAH penalties, no de-rating beyond the input UCAP MW.
    Returns 0 if `ucap_mw <= 0` or `price_per_mw_day <= 0` so the runner
    can skip emitting empty rows for assets that didn't clear.
    """
    if ucap_mw <= 0.0 or price_per_mw_day <= 0.0:
        return 0.0
    return ucap_mw * price_per_mw_day * days_in_month
