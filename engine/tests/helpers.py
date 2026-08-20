"""Independent price lookups used as a settlement oracle.

Deliberately re-derives prices with plain boolean-mask filtering on the raw
frames — not the engine's PriceTables dicts — so tests cross-check the
engine's O(1) lookups against an independent read path.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd


def lookup_price(
    df: pd.DataFrame,
    value_col: str,
    ts: datetime,
    zone: str | None = None,
) -> float:
    mask = df["datetime_beginning_utc"] == ts
    if zone is not None:
        mask &= df["zone"] == zone
    sub = df[mask]
    if sub.empty:
        raise KeyError(f"no {value_col} row for ts={ts} zone={zone}")
    return float(sub[value_col].iloc[0])


class FixedDailySchedule:
    """Test fixture: self-schedule one fixed charge hour and one fixed
    discharge hour of DA energy per operating day. No trading logic —
    exercises the DA-gate → clear → settle → RT-fallback path."""

    def __init__(self, mw: float = 50.0, charge_idx: int = 2, discharge_idx: int = 18):
        self.mw = mw
        self.charge_idx = charge_idx
        self.discharge_idx = discharge_idx

    def should_resolve(self, event) -> bool:
        from pjm_engine.events import DAGateClosing

        return isinstance(event, DAGateClosing)

    def on_event(self, event, ctx):
        from pjm_engine.strategy_base import Product, SelfSchedule
        from pjm_engine.time_utils import operating_hour_starts_utc

        hours = operating_hour_starts_utc(event.operating_date)
        return [
            SelfSchedule(Product.DA_Energy, hours[self.charge_idx], -self.mw),
            SelfSchedule(Product.DA_Energy, hours[self.discharge_idx], +self.mw),
        ]
