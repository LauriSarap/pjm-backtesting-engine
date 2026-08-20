"""Asset configs and SoC dynamics for BESS assets.

Source of truth for SoC bounds, RTE, and the perfect-tracking assumption.
Ships with one generic example asset; add your own entries to `ASSETS`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class AssetConfig:
    asset_id: str
    zone: str  # PJM energy zone (e.g. PECO, COMED, or PJM-RTO)
    power_mw: float  # nameplate (charge and discharge symmetric)
    energy_mwh: float  # nameplate energy capacity
    pnode_id: int  # working settlement pnode (zone-level pricing only)
    pnode_name: str
    lda: str  # capacity LDA (price-key)
    sub_zone: str = "RTO"  # SR Sub-Zone

    soc_min_pct: float = 0.10
    soc_max_pct: float = 0.90
    rte: float = 0.85  # round-trip efficiency
    cycle_cost: float = 5.0  # $/MWh dispatch penalty
    bid_increment: float = 0.1  # MW

    # Capacity (RPM) UCAP rating in MW. PJM's ELCC class for 4-hr storage in
    # DY 2025/26 derates nameplate by ~50% (the rating evolves annually as
    # PJM re-runs ELCC studies; a single multiplier is the baseline
    # assumption). Override per-asset if a future DY publishes different
    # per-resource UCAP ratings.
    ucap_mw: float = 0.0

    @property
    def eta_in(self) -> float:
        """Charge-side efficiency — split RTE evenly across charge/discharge."""
        return math.sqrt(self.rte)

    @property
    def eta_out(self) -> float:
        """Discharge-side efficiency."""
        return math.sqrt(self.rte)

    @property
    def soc_min_mwh(self) -> float:
        return self.soc_min_pct * self.energy_mwh

    @property
    def soc_max_mwh(self) -> float:
        return self.soc_max_pct * self.energy_mwh


ASSETS: dict[str, AssetConfig] = {
    # Generic 4-hour battery settled at the PJM-RTO zone aggregate. Replace
    # or extend with your own asset(s) — zone, pnode, and LDA should match
    # the price rows in your data/ directory.
    "example_a": AssetConfig(
        asset_id="example_a",
        zone="PJM-RTO",
        power_mw=250,
        energy_mwh=1000,
        pnode_id=1,
        pnode_name="PJM-RTO",
        lda="RTO",
        ucap_mw=125,  # 250 MW × 0.50 ELCC class rating
    ),
}


def step_soc(
    soc_mwh: float, charge_mw: float, discharge_mw: float, dt_hours: float, asset: AssetConfig
) -> float:
    """Advance SoC by one step. Regulation deployment does not move SoC
    (the signal is assumed zero-mean; see design.md)."""
    delta = charge_mw * asset.eta_in * dt_hours - discharge_mw / asset.eta_out * dt_hours
    return soc_mwh + delta
