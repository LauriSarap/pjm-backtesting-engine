"""Lost Opportunity Cost.

LOC compensates a battery when an AS commitment prevents it from running its
profitable energy position. Per design.md §LOC:

    LOC_i = max(0, energy_LMP − cycling_cost) × AS_reserved_MW × dt

`AS_reserved_MW` is the upward-headroom MW carved out for SR / Reg-up
during interval i; `cycling_cost` is the asset's $/MWh degradation charge.
Reg LOC is forfeited when the 5-min performance score < 0.25; under
perfect tracking (score = 1.0) the threshold never trips.

**Two corrections layered on the bare formula:**

1. **Cycle cap** (`LOC_MAX_CYCLES_PER_DAY = 2.0`). The bare per-MTU formula
   over-credits batteries that hold full-nameplate reserve every MTU all
   year — physically impossible since a 4-hr battery can only cycle ~2x/day.
   Effective `reserve_mw` is capped at
   `LOC_MAX_CYCLES_PER_DAY × asset.energy_mwh / 24` (e.g. 100 MW for a
   300 MW / 1200 MWh battery). The uncapped formula is preserved when
   `asset_energy_mwh=None` (callers without asset context).

2. **DA-LMP foregone bound** (`loc_v2`). The runner passes the DA LMP at
   the parent hour as `energy_lmp`, not the RT LMP at the MTU. An
   AS-committed asset wasn't running RT energy — its true foregone
   opportunity is the DA arbitrage it gave up by holding upward headroom.
   Using RT LMP would credit RT scarcity windfalls the asset couldn't have
   actually captured (it was holding for SR/Sec the whole hour). DA LMP is
   smoother than RT and is what a real strategy plans against, so this
   matches both tariff intent (M28) and how operators model LOC. The bare
   `settle_loc` function stays price-source-agnostic; the semantic decision
   lives in the runner caller (`runner._emit_loc_row`).

LOC is a real revenue line paid by PJM. The runner emits it under product
codes `LOC_SR_RTO` and `LOC_Sec_RTO` so it nets cleanly in `revenue_by_product`.

Caveat (design.md §LOC): full PJM Regulation LOC has more constraints —
self-scheduled Reg isn't eligible, Reg-only resources can have zero
opportunity cost. This formula is the simplified upward-headroom model,
sufficient for ranking strategies.
"""

from __future__ import annotations

REG_LOC_FORFEIT_SCORE = 0.25
LOC_MAX_CYCLES_PER_DAY = 2.0
"""Cap on per-asset average daily cycling for LOC accrual purposes.

Real PJM batteries typically do 1.0-1.5 cycles/day on AS-stacked operation;
2.0/day is a generous upper bound. Tunable; raise to revert to the
unconstrained geometric formula (effectively `inf`)."""


def settle_loc(
    energy_lmp: float,
    cycling_cost: float,
    reserve_mw: float,
    hours: float,
    perf_score: float = 1.0,
    asset_energy_mwh: float | None = None,
) -> float:
    """Per-period LOC credit in dollars. Returns 0 if perf_score < 0.25.

    Formula: `max(0, energy_LMP − cycling_cost) × effective_MW × hours`. Sign
    convention: LOC is always non-negative (PJM doesn't claw back when the
    held headroom would have lost money against energy).

    `energy_lmp` is the foregone-revenue price. The runner passes the
    parent-hour DA LMP (`loc_v2`). Function is price-source-agnostic —
    semantic choice is upstream.

    `effective_MW = min(reserve_mw, LOC_MAX_CYCLES_PER_DAY × energy_mwh / 24)`
    when `asset_energy_mwh` is provided. Without that cap, a 300 MW / 1200 MWh
    battery accrues LOC against 300 MW for every MTU — implying ~6 cycles/day
    of throughput, which is physically impossible. The cap reduces effective
    MW to 100 MW for that battery (2 cycles/day × 1200 MWh / 24h)."""
    if perf_score < REG_LOC_FORFEIT_SCORE:
        return 0.0
    margin = max(0.0, energy_lmp - cycling_cost)
    effective_mw = reserve_mw
    if asset_energy_mwh is not None:
        max_throughput_mw = LOC_MAX_CYCLES_PER_DAY * asset_energy_mwh / 24.0
        effective_mw = min(reserve_mw, max_throughput_mw)
    return margin * effective_mw * hours
