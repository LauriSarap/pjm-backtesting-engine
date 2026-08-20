"""Day-Ahead energy settlement.

`cleared_mw × DA_LMP × hours` — signed, so + = discharge revenue, − = charge cost.
"""

from __future__ import annotations


def settle_da_energy(cleared_mw: float, da_lmp: float, hours: float = 1.0) -> float:
    return cleared_mw * da_lmp * hours
