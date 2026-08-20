"""Time windows, regime boundaries, DST-aware bucketing.

Conventions (from design.md and pjm-data.md):
- Internal storage is UTC. Bucketing by operating-day converts to EPT.
- Regime boundary: 2025-10-01 00:00 EPT = 2025-10-01 04:00 UTC.
- DST: spring-forward EPT day = 23 hours; fall-back EPT day = 25 hours.
  UTC storage handles this automatically; daily bucketing must group on
  the EPT calendar date so the count comes out right.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd

UTC = timezone.utc
EPT = ZoneInfo("America/New_York")

REGIME_BOUNDARIES = {
    "v1_to_v2": datetime(2025, 10, 1, 4, 0, tzinfo=UTC),  # 2025-10-01 00:00 EPT
}


@dataclass(frozen=True)
class TimeWindow:
    """Closed-open [start, end). Both must be tz-aware."""

    start: datetime
    end: datetime

    def __post_init__(self):
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("TimeWindow requires tz-aware datetimes")
        if self.start >= self.end:
            raise ValueError(f"start ({self.start}) must be < end ({self.end})")

    def years(self) -> float:
        return (self.end - self.start).total_seconds() / (365.25 * 86400)


def split_at_regime(window: TimeWindow) -> list[TimeWindow]:
    """Split window at any regime boundaries it crosses."""
    cuts = sorted(b for b in REGIME_BOUNDARIES.values() if window.start < b < window.end)
    if not cuts:
        return [window]
    out: list[TimeWindow] = []
    prev = window.start
    for c in cuts:
        out.append(TimeWindow(prev, c))
        prev = c
    out.append(TimeWindow(prev, window.end))
    return out


Freq = Literal["5min", "1h", "1D", "1W", "1ME", "1QE", "1YE"]


def bucket_revenue(rev: pd.DataFrame, freq: Freq, ts_col: str = "period_start_utc") -> pd.DataFrame:
    """Sum revenue into buckets.

    For sub-day frequencies, group on UTC (no DST concern).
    For 1D and longer, group on the EPT operating-day calendar so a
    spring-forward day correctly contains 23 hourly rows and a fall-back
    day contains 25.
    """
    df = rev.copy()
    ts = pd.to_datetime(df[ts_col], utc=True)

    if freq in ("5min", "1h"):
        out = df.groupby(pd.Grouper(key=ts_col, freq=freq))["revenue"].sum()
        return out.to_frame("revenue").reset_index()

    if freq == "1D":
        df["_ept_date"] = ts.dt.tz_convert(EPT).dt.date
        out = df.groupby("_ept_date")["revenue"].sum()
        return (
            out.to_frame("revenue")
            .reset_index()
            .rename(columns={"_ept_date": "operating_date_ept"})
        )

    # 1W/1ME/1QE/1YE: group on EPT-converted timestamps.
    ept_ts = ts.dt.tz_convert(EPT).dt.tz_localize(None)
    df = df.set_index(ept_ts)
    out = df.groupby(pd.Grouper(freq=freq))["revenue"].sum()
    return (
        out.to_frame("revenue")
        .reset_index()
        .rename(columns={ept_ts.name or "index": "bucket_start_ept"})
    )
