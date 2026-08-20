"""Synchronized Reserve / Secondary Reserve settlement.

Per pjm-data.md §2.2:

    reserve revenue = cleared_MW × MCP × hours_cleared

No mileage, no performance score in normal hours. Shortfall on a called event
forfeits the credit + a retroactive clawback of prior SR credits going back to
the average inter-event interval (~30 days). The shortfall/clawback math lives
in `sr_events.py` (pjm-data.md §7.2); this module owns only the capacity
payment formula.

SR and Sec (30-min Reserve) share the formula — only the price source differs.
"""

from __future__ import annotations


def settle_reserve(cleared_mw: float, mcp: float, hours: float) -> float:
    """SR / Sec capacity payment for one period (hour or MTU). Returns dollars.

    `cleared_mw` is the upward-headroom capacity (always positive — reserves
    are one-directional). `hours` is 1.0 for a full DA hour, 1/12 for a
    5-min MTU.
    """
    return cleared_mw * mcp * hours
