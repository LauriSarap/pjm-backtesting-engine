"""Real-Time energy deviation settlement.

PJM dual-settlement: DA award is financial; RT pays/charges the deviation
between actual physical delivery and the DA position, at the RT 5-min LMP.

  Energy revenue per MTU =  DA_MW × DA_LMP × (1/12)
                          + (actual_MW − DA_MW) × RT_LMP × (1/12)

This module handles only the RT deviation leg. The DA leg lives in da_energy.
"""

from __future__ import annotations


def settle_rt_energy(
    actual_mw: float,
    da_cleared_mw: float,
    rt_lmp: float,
    hours: float = 1.0 / 12.0,  # 5-min MTU = 1/12 h
) -> float:
    deviation = actual_mw - da_cleared_mw
    return deviation * rt_lmp * hours
