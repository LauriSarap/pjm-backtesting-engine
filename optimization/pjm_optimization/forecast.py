"""Forecaster plug-ins for rolling-horizon optimization.

A `Forecaster` answers the question every realistic-ops strategy has to ask
at every gate: "given what I can see right now, what will DA / RT / AS prices
look like over the next N hours so I can solve a horizon MILP?"

The interface is deliberately narrow — one method, returning aligned price
Series — so concrete forecasters can range from the oracle (cheats with
realized data, used to validate that the rolling-horizon math reproduces the
PF ceiling) to whatever model the user plugs in, without the strategy code
changing.

Sign convention: prices are $/MWh. Index is tz-aware UTC; DA at hour starts
(top of hour), RT at 5-min MTU starts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

EPT = ZoneInfo("America/New_York")
ONE_HOUR = timedelta(hours=1)
MTU_5MIN = timedelta(minutes=5)


@dataclass(frozen=True)
class PriceForecast:
    """Aligned DA + RT price forecasts for a horizon starting at `start_utc`.

    `da_lmps` indexed at hour starts in `[start_utc, start_utc + horizon)`.
    `rt_lmps` indexed at MTU starts in `[start_utc, start_utc + horizon)`,
    must contain exactly 12 MTUs per DA hour for `solve_perfect_foresight`.
    """

    start_utc: datetime
    horizon_hours: int
    da_lmps: pd.Series  # index: hour start UTC, values: $/MWh
    rt_lmps: pd.Series  # index: MTU start UTC, values: $/MWh

    def __post_init__(self) -> None:
        if len(self.da_lmps) != self.horizon_hours:
            raise ValueError(f"da_lmps has {len(self.da_lmps)} rows; expected {self.horizon_hours}")
        if len(self.rt_lmps) != 12 * self.horizon_hours:
            raise ValueError(
                f"rt_lmps has {len(self.rt_lmps)} rows; expected {12 * self.horizon_hours}"
            )


class Forecaster(ABC):
    """Strategy-side price forecaster contract.

    The oracle lives in this file; real forecasters get plugged in by the
    user. The strategy depends on the ABC; swapping forecasters is a
    constructor argument.
    """

    @abstractmethod
    def forecast(
        self,
        as_of: datetime,
        start_utc: datetime,
        horizon_hours: int,
        zone: str,
    ) -> PriceForecast:
        """Return DA + RT LMP forecast for the horizon `[start_utc, start_utc + horizon)`.

        `as_of` is the strategy's information cutoff (typically the event
        timestamp) — implementations that are honest must
        not peek beyond it. The oracle deliberately does peek.
        """


# ─── PerfectOracleForecaster ──────────────────────────────────────────────────


class PerfectOracleForecaster(Forecaster):
    """Cheats: returns the realized prices for the requested horizon.

    Used to validate the rolling-horizon math. A strategy fed this forecaster
    should produce results approaching the perfect-foresight MILP ceiling
    (the gap reflects (a) DA-only re-solves vs joint-window optimization,
    (b) per-day SoC handoff vs free SoC across the whole window).

    Wraps the same DA + RT LMP DataFrames the engine uses, filtered to the
    asset's zone.
    """

    def __init__(self, da_lmps: pd.DataFrame, rt_lmps: pd.DataFrame) -> None:
        self._da = da_lmps
        self._rt = rt_lmps

    def forecast(
        self,
        as_of: datetime,
        start_utc: datetime,
        horizon_hours: int,
        zone: str,
    ) -> PriceForecast:
        end_utc = start_utc + horizon_hours * ONE_HOUR

        da = self._da
        da_zone = da[da["zone"] == zone] if "zone" in da.columns else da
        da_window = da_zone[
            (da_zone["datetime_beginning_utc"] >= start_utc)
            & (da_zone["datetime_beginning_utc"] < end_utc)
        ].sort_values("datetime_beginning_utc")
        da_series = pd.Series(
            da_window["total_lmp_da"].to_numpy(),
            index=pd.DatetimeIndex(da_window["datetime_beginning_utc"]),
        )

        rt = self._rt
        rt_zone = rt[rt["zone"] == zone] if "zone" in rt.columns else rt
        rt_window = rt_zone[
            (rt_zone["datetime_beginning_utc"] >= start_utc)
            & (rt_zone["datetime_beginning_utc"] < end_utc)
        ].sort_values("datetime_beginning_utc")
        rt_series = pd.Series(
            rt_window["total_lmp_rt"].to_numpy(),
            index=pd.DatetimeIndex(rt_window["datetime_beginning_utc"]),
        )

        return PriceForecast(
            start_utc=start_utc,
            horizon_hours=horizon_hours,
            da_lmps=da_series,
            rt_lmps=rt_series,
        )
