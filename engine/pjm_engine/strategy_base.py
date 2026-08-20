"""Strategy contract — what every strategy implements.

Strategies live outside the engine and depend on this module. They never
touch data directly; the engine hands them a `Context` whose `.view` is the
only legal read path.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum

import pandas as pd

from .battery import AssetConfig
from .errors import DataNotAvailableError
from .events import Event

# SR/Sec award routing (M28 §6.2.2 / §19.2.2 two-settlement). DA awards span
# one operating hour; RT-side awards span one 5-min MTU. We route by the
# award's period length so a single `add_award` entry point handles both
# without callers needing to know the dict layout.
_ONE_HOUR_DELTA = timedelta(hours=1)
_FIVE_MIN_DELTA = timedelta(minutes=5)

logger = logging.getLogger(__name__)

# Schemas surfaced when an AS feed is missing from data/raw/. Strategies that
# call the new AS accessors (`da_sr_prices`, `rt_sr_prices`, `da_sec_prices`,
# `rt_sec_prices`, `reg_market_results`) get an empty DataFrame with these
# columns instead of an exception, so a strategy can safely `iterrows()` /
# `merge` on the result without special-casing absent feeds.
_AS_PRICE_SCHEMA = [
    "datetime_beginning_utc",
    "datetime_beginning_ept",
    "mcp",
    "mcp_capped",
    "published_at",
]
_REG_MARKET_RESULTS_SCHEMA = [
    "datetime_beginning_utc",
    "datetime_beginning_ept",
    "requirement",
    "regd_ssmw",
    "rega_ssmw",
    "regd_procure",
    "rega_procure",
    "total_mw",
    "deficiency",
    "rto_perfscore",
    "rega_mileage",
    "regd_mileage",
    "rega_hourly",
    "regd_hourly",
    "is_approved",
    "modified_datetime_utc",
    "published_at",
]

# Module-level lazy cache for DataView's AS accessors. Each entry maps a
# loader name to either a loaded DataFrame or the sentinel `_MISSING` so we
# warn-once on absent feeds and never reload during a backtest.
_MISSING = object()
_as_feed_cache: dict[str, object] = {}


def _lazy_load_as_feed(name: str, schema: list[str]) -> pd.DataFrame:
    """Lazy-load an AS feed parquet via the canonical loader in `data.py`.

    Cached at module level so per-event accessor calls don't reload. If the
    underlying CSV directory is empty the loader raises `FileNotFoundError`;
    we log a warning ONCE and cache an empty schema-shaped DataFrame so the
    gap is visible without flooding logs.
    """
    cached = _as_feed_cache.get(name)
    if cached is not None:
        if cached is _MISSING:
            return pd.DataFrame(columns=schema)
        return cached  # type: ignore[return-value]

    # Import lazily to avoid a circular import (`data` doesn't import this
    # module, but keeping the import local makes that invariant obvious).
    from . import data as _data

    loader = getattr(_data, name)
    try:
        df = loader()
    except FileNotFoundError as exc:
        logger.warning(
            "DataView AS accessor: %s unavailable (%s); returning empty frame "
            "with schema=%s. Strategies depending on this feed will see no rows.",
            name,
            exc,
            schema,
        )
        _as_feed_cache[name] = _MISSING
        return pd.DataFrame(columns=schema)
    _as_feed_cache[name] = df
    return df


def _bitemporal_slice(df: pd.DataFrame, as_of: datetime) -> pd.DataFrame:
    """Apply the same `published_at <= as_of` filter `view_as_of` applies.

    Defined inline (rather than imported from `data`) to keep `strategy_base`
    free of an import-time dependency on `data` — the DataView accessors only
    pull `data` in lazily, on first call.

    Mirrors the `view_as_of` contract: `as_of` MUST be tz-aware. A naive
    timestamp would silently coerce to UTC under `pd.Timestamp(...)` for some
    pandas versions and to NaT for others — neither is acceptable for a
    leakage-relevant filter, so we raise on the same condition `view_as_of`
    raises on.
    """
    if as_of.tzinfo is None:
        raise DataNotAvailableError(f"as_of must be tz-aware, got naive {as_of!r}")
    if df.empty:
        return df
    cutoff = df["published_at"].searchsorted(pd.Timestamp(as_of), side="right")
    return df.iloc[:cutoff]


class Product(str, Enum):
    DA_Energy = "DA_Energy"
    RT_Energy = "RT_Energy"
    RegA_v1 = "RegA_v1"  # pre-2025-10-01: slow-following Reg (mostly thermals)
    RegD_v1 = "RegD_v1"  # pre-2025-10-01: fast-following Reg (BESS sweet spot)
    Reg_v2 = "Reg_v2"  # post-2025-10-01
    SR_RTO = "SR_RTO"  # Synchronized Reserve, RTO Sub-Zone
    Sec_RTO = "Sec_RTO"  # 30-Minute (Secondary) Reserve, RTO
    Capacity_RPM = "Capacity_RPM"  # RPM capacity revenue: $/MW-day × UCAP × days/month
    # The post-2026-10 RegUp/RegDn (v3) products are not modeled.


# ─── Strategy responses ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class SelfSchedule:
    """Price-taker offer: clears at any price."""

    product: Product
    period_start: datetime  # tz-aware UTC, MTU/hour start
    mw: float  # signed: + discharge, − charge


@dataclass(frozen=True)
class BidCurve:
    """Price-quantity tiers. Cleared MW = max q with bid_price(q) ≤ clearing_price."""

    product: Product
    period_start: datetime
    tiers: tuple[tuple[float, float], ...]  # ((mw, $/MWh), ...) monotonic in price


@dataclass(frozen=True)
class Acknowledgment:
    """Strategy chose not to act on this event."""


StrategyResponse = SelfSchedule | BidCurve | Acknowledgment | list


# ─── Context handed to the strategy ──────────────────────────────────────────


@dataclass
class Commitments:
    """Per-asset ledger of cleared awards. Mutated by the engine after clearing.

    `awards` is the canonical list (used by validators that need to walk all
    awards in time order). The `_by_period_*` dicts are O(1) lookup caches
    for the per-event hot paths and must be kept in sync via `add_award`.

    SR / Sec awards live in two parallel caches each (`_da_*` keyed by
    operating-hour start, `_rt_*` keyed by MTU start). `add_award` routes
    by the award's period length: ≥ 1h → DA, 5 min → RT. The two-settlement
    M28 §6.2.2 / §19.2.2 RT credit pays only `(RT_assignment − DA_assignment)
    × RT_MCP / 12`, so the runner needs to query the DA-only and RT-only
    sides separately even though validators want the combined total.
    """

    awards: list = field(default_factory=list)
    _da_by_period: dict = field(default_factory=dict)
    _reg_by_period: dict = field(default_factory=dict)
    _rega_v1_by_period: dict = field(default_factory=dict)
    _regd_v1_by_period: dict = field(default_factory=dict)
    # SR_RTO: DA cache keyed by operating-hour start (period length 1h);
    # RT cache keyed by MTU start (period length 5 min).
    _da_sr_by_period: dict = field(default_factory=dict)
    _rt_sr_by_period: dict = field(default_factory=dict)
    # Sec_RTO: same shape as SR.
    _da_sec_by_period: dict = field(default_factory=dict)
    _rt_sec_by_period: dict = field(default_factory=dict)

    def add_award(self, award) -> None:
        """Append to the canonical list AND update the lookup cache."""
        self.awards.append(award)
        if award.product == Product.DA_Energy:
            self._da_by_period[award.period_start] = award.cleared_mw
        elif award.product == Product.Reg_v2:
            self._reg_by_period[award.period_start] = award.cleared_mw
        elif award.product == Product.RegA_v1:
            self._rega_v1_by_period[award.period_start] = award.cleared_mw
        elif award.product == Product.RegD_v1:
            self._regd_v1_by_period[award.period_start] = award.cleared_mw
        elif award.product == Product.SR_RTO:
            self._route_reserve_award(
                award,
                self._da_sr_by_period,
                self._rt_sr_by_period,
                label="SR_RTO",
            )
        elif award.product == Product.Sec_RTO:
            self._route_reserve_award(
                award,
                self._da_sec_by_period,
                self._rt_sec_by_period,
                label="Sec_RTO",
            )

    @staticmethod
    def _route_reserve_award(award, da_cache: dict, rt_cache: dict, *, label: str) -> None:
        """Route SR/Sec award to the DA or RT cache based on period length.

        DA-side awards span one operating hour; RT-side awards span one 5-min
        MTU. Anything else is unexpected and likely a misuse — raise so the
        bug surfaces at insertion rather than producing silently-wrong
        downstream settlement.
        """
        period = award.period_end - award.period_start
        if period >= _ONE_HOUR_DELTA:
            da_cache[award.period_start] = award.cleared_mw
        elif period == _FIVE_MIN_DELTA:
            rt_cache[award.period_start] = award.cleared_mw
        else:
            raise ValueError(
                f"unexpected {label} award period length {period} "
                f"(period_start={award.period_start}, period_end={award.period_end}); "
                "expected 1h (DA) or 5 min (RT)"
            )

    def add_awards(self, awards) -> None:
        for a in awards:
            self.add_award(a)

    def da_cleared_mw_at(self, hour_start_utc: datetime) -> float:
        """Net DA MW cleared for the given operating hour (0 if no DA award)."""
        return self._da_by_period.get(hour_start_utc, 0.0)

    def reg_v2_cleared_mw_at(self, half_hour_start_utc: datetime) -> float:
        """Reg_v2 cleared MW for the given half-hour assignment block (0 if none)."""
        return self._reg_by_period.get(half_hour_start_utc, 0.0)

    def rega_v1_cleared_mw_at(self, hour_start_utc: datetime) -> float:
        """Pre-2025-10 RegA cleared MW for the given operating hour (0 if none)."""
        return self._rega_v1_by_period.get(hour_start_utc, 0.0)

    def regd_v1_cleared_mw_at(self, hour_start_utc: datetime) -> float:
        """Pre-2025-10 RegD cleared MW for the given operating hour (0 if none)."""
        return self._regd_v1_by_period.get(hour_start_utc, 0.0)

    def reg_v1_cleared_mw_at(self, hour_start_utc: datetime) -> float:
        """Combined pre-2025-10 RegA+RegD cleared MW for an hour. Used by SoC
        headroom checks where total Reg capacity matters regardless of product."""
        return self._rega_v1_by_period.get(hour_start_utc, 0.0) + self._regd_v1_by_period.get(
            hour_start_utc, 0.0
        )

    def sr_rto_cleared_mw_at(self, hour_start_utc: datetime) -> float:
        """Total SR_RTO MW committed for the operating hour (DA + any RT additions).

        Validators (SoC sustain, cross-product power cap, SR-event delivery)
        need the asset's *total* committed SR for the hour, regardless of which
        gate paid for it. Sums the DA-cleared MW with the per-MTU RT additions
        falling within the hour.
        """
        return self._da_sr_by_period.get(hour_start_utc, 0.0) + sum(
            mw
            for mtu, mw in self._rt_sr_by_period.items()
            if hour_start_utc <= mtu < hour_start_utc + _ONE_HOUR_DELTA
        )

    def da_sr_rto_cleared_mw_at(self, hour_start_utc: datetime) -> float:
        """DA-side SR_RTO MW for the operating hour (0 if no DA SR award).

        M28 §6.2.2 two-settlement: this is the Day-ahead Synchronized Reserve
        Assignment that the Balancing credit subtracts from the Real-time
        assignment. Runner-only; validators should keep using
        `sr_rto_cleared_mw_at` for total committed MW.
        """
        return self._da_sr_by_period.get(hour_start_utc, 0.0)

    def rt_sr_rto_added_mw_at(self, mtu_start_utc: datetime) -> float:
        """Per-MTU RT-side SR_RTO addition (0 if none).

        Today no strategy bids SR at the RT gate, so this is structurally 0
        for the entire run; the API exists so the runner's two-settlement
        delta computation has a place to read from when RT bids land.
        """
        return self._rt_sr_by_period.get(mtu_start_utc, 0.0)

    def sec_rto_cleared_mw_at(self, hour_start_utc: datetime) -> float:
        """Total Sec_RTO MW committed for the operating hour (DA + any RT)."""
        return self._da_sec_by_period.get(hour_start_utc, 0.0) + sum(
            mw
            for mtu, mw in self._rt_sec_by_period.items()
            if hour_start_utc <= mtu < hour_start_utc + _ONE_HOUR_DELTA
        )

    def da_sec_rto_cleared_mw_at(self, hour_start_utc: datetime) -> float:
        """DA-side Sec_RTO MW for the operating hour (0 if no DA Sec award)."""
        return self._da_sec_by_period.get(hour_start_utc, 0.0)

    def rt_sec_rto_added_mw_at(self, mtu_start_utc: datetime) -> float:
        """Per-MTU RT-side Sec_RTO addition (0 if none)."""
        return self._rt_sec_by_period.get(mtu_start_utc, 0.0)


@dataclass
class Context:
    """Everything a strategy is allowed to see at one event.

    `view` is a `DataView` already pinned to this event's timestamp; its
    accessors filter by `published_at <= as_of`. There is no other read path.
    """

    asset: AssetConfig
    commitments: Commitments
    view: "DataView"
    # Asset SoC at this event's timestamp. The runner tracks SoC across MTUs;
    # strategies needing it (e.g. rolling-horizon MILP that constrains the
    # next 24h walk) read it here rather than re-walking commitments. Default
    # 0.0 so existing test fixtures that build Context manually don't break.
    current_soc_mwh: float = 0.0


@dataclass
class DataView:
    """Bitemporally-filtered handle over the engine's data tables.

    Adding new feeds = adding a new accessor here. Each accessor must be a
    pure read of the underlying frame filtered by `as_of` already.

    AS price feeds (`da_sr`, `rt_sr`, `da_sec`, `rt_sec`, `reg_market_results`)
    are NOT pre-attached by the runner — they're surfaced through the methods
    `da_sr_prices()`, `rt_sr_prices()`, `da_sec_prices()`, `rt_sec_prices()`,
    `reg_market_results()`. Each method lazy-loads the underlying parquet
    once (module-level cache in `_lazy_load_as_feed`) and then applies the
    same `published_at <= self.as_of` filter `view_as_of` applies.

    Tests / fixtures that want to inject a hand-built AS frame can pass it
    via the constructor (`_da_sr_prices=`, etc.) and the accessor will use
    that pinned frame instead of touching the parquet cache.
    """

    as_of: datetime
    da_lmps: pd.DataFrame  # already filtered by view_as_of
    rt_lmps: pd.DataFrame | None = None  # likewise (None if not loaded for this run)
    reg_prices: pd.DataFrame | None = None  # RMCCP + RMPCP, system-wide RTO market
    rto_forecasts: pd.DataFrame | None = None
    # AS-feed test/runner injection points. Default None → lazy-load via
    # `_lazy_load_as_feed`. When supplied (typically by tests), the accessor
    # applies `view_as_of` to the supplied frame directly.
    _da_sr_prices: pd.DataFrame | None = None
    _rt_sr_prices: pd.DataFrame | None = None
    _da_sec_prices: pd.DataFrame | None = None
    _rt_sec_prices: pd.DataFrame | None = None
    _reg_market_results: pd.DataFrame | None = None

    def da_lmp(self, zone: str, hour_start_utc: datetime) -> float | None:
        """Cleared DA LMP for (zone, hour). None if not yet published."""
        rows = self.da_lmps
        m = (rows["zone"] == zone) & (rows["datetime_beginning_utc"] == hour_start_utc)
        sub = rows[m]
        if sub.empty:
            return None
        return float(sub["total_lmp_da"].iloc[0])

    def rt_lmp(self, zone: str, mtu_start_utc: datetime) -> float | None:
        """Cleared RT 5-min LMP for (zone, MTU start). None if not yet published."""
        if self.rt_lmps is None:
            return None
        rows = self.rt_lmps
        m = (rows["zone"] == zone) & (rows["datetime_beginning_utc"] == mtu_start_utc)
        sub = rows[m]
        if sub.empty:
            return None
        return float(sub["total_lmp_rt"].iloc[0])

    def rt_lmps_recent(self, zone: str, before_utc: datetime, last_n: int) -> pd.DataFrame:
        """Most recent `last_n` RT LMPs for `zone`, strictly before `before_utc`.

        The bitemporal slice is already sorted by published_at, and for RT LMPs
        published_at = datetime_beginning_utc + 10min is strictly monotonic, so
        the slice is also sorted by MTU start. We only need a zone filter + tail.
        """
        if self.rt_lmps is None or self.rt_lmps.empty:
            return pd.DataFrame()
        rows = self.rt_lmps
        # Fast path: engine pre-filters `rt_lmps` to a single zone in
        # `prepare_market_data`, so the zone arg is a sanity check, not a
        # filter step. Skipping the redundant `rows[rows["zone"] == zone]`
        # scan + DataFrame alloc cuts a per-MTU-bidding strategy from 144s → ~10s
        # over a 6-month window (per-MTU call × 52k MTUs). Multi-zone
        # frames (test fixtures) fall through to the slow path.
        if rows["zone"].iloc[0] == zone and rows["zone"].iloc[-1] == zone:
            cutoff = rows["datetime_beginning_utc"].searchsorted(
                pd.Timestamp(before_utc), side="left"
            )
            return rows.iloc[:cutoff].tail(last_n)
        zone_rows = rows[rows["zone"] == zone]
        cutoff = zone_rows["datetime_beginning_utc"].searchsorted(
            pd.Timestamp(before_utc), side="left"
        )
        return zone_rows.iloc[:cutoff].tail(last_n)

    def rto_da_net_load_forecast_day(self, operating_date: date) -> pd.DataFrame:
        """DA net-load forecast rows for one EPT operating day, visible as-of."""
        if self.rto_forecasts is None or self.rto_forecasts.empty:
            return pd.DataFrame()
        rows = self.rto_forecasts
        rows = rows[rows["forecast_stage"] == "da"]
        if rows.empty:
            return rows
        return rows[rows["datetime_beginning_ept"].dt.date == operating_date].sort_values(
            "datetime_beginning_utc"
        )

    # ─── AS clearing-price accessors ────────────────────────────────────────
    # Each returns rows with `published_at <= self.as_of`. Sub-zone filter
    # is implicit: every loader pre-filters to `PJM RTO Reserve Zone` (DA)
    # or `PJM_RTO` (RT), so callers don't pass an `asset` arg — all
    # registry assets are RTO-zoned. If the underlying feed is absent
    # the accessor returns an empty schema-shaped DataFrame and emits a
    # one-time WARNING (see `_lazy_load_as_feed`).

    def da_sr_prices(self) -> pd.DataFrame:
        """DA Synchronized Reserve clearing prices (RTO sub-zone, hourly).

        Columns: `datetime_beginning_utc`, `datetime_beginning_ept`,
        `mcp`, `mcp_capped`, `published_at`. `published_at = D-1 13:30 EPT`
        for operating day D, identical to DA energy gate timing.
        """
        if self._da_sr_prices is not None:
            return _bitemporal_slice(self._da_sr_prices, self.as_of)
        df = _lazy_load_as_feed("load_da_sr_prices", _AS_PRICE_SCHEMA)
        return _bitemporal_slice(df, self.as_of)

    def rt_sr_prices(self) -> pd.DataFrame:
        """RT 5-min Synchronized Reserve clearing prices (RTO).

        Columns: `datetime_beginning_utc`, `datetime_beginning_ept`,
        `mcp`, `mcp_capped`, `published_at`.
        `published_at = MTU_start + 10 min` (verified-as-prelim convention,
        M11 §3.7.6).
        """
        if self._rt_sr_prices is not None:
            return _bitemporal_slice(self._rt_sr_prices, self.as_of)
        df = _lazy_load_as_feed("load_rt_sr_prices", _AS_PRICE_SCHEMA)
        return _bitemporal_slice(df, self.as_of)

    def da_sec_prices(self) -> pd.DataFrame:
        """DA 30-Minute (Secondary) Reserve clearing prices (RTO sub-zone, hourly).

        Columns same as `da_sr_prices`. `published_at = D-1 13:30 EPT`.
        Often clears at $0 in our window (PJM has plenty of idle 30-min
        capacity); the accessor still returns those rows so a strategy can
        explicitly observe the zero-clear regime.
        """
        if self._da_sec_prices is not None:
            return _bitemporal_slice(self._da_sec_prices, self.as_of)
        df = _lazy_load_as_feed("load_da_sec_prices", _AS_PRICE_SCHEMA)
        return _bitemporal_slice(df, self.as_of)

    def rt_sec_prices(self) -> pd.DataFrame:
        """RT 5-min 30-Minute (Secondary) Reserve clearing prices (RTO).

        Columns same as `rt_sr_prices`. `published_at = MTU_start + 10 min`
        (M11 §3.7.6).
        """
        if self._rt_sec_prices is not None:
            return _bitemporal_slice(self._rt_sec_prices, self.as_of)
        df = _lazy_load_as_feed("load_rt_sec_prices", _AS_PRICE_SCHEMA)
        return _bitemporal_slice(df, self.as_of)

    def reg_market_results(self) -> pd.DataFrame:
        """System-wide Regulation procurement summary (half-hour post-redesign).

        Columns: `datetime_beginning_utc`, `datetime_beginning_ept`,
        `requirement`, `regd_ssmw`, `rega_ssmw`, `regd_procure`,
        `rega_procure`, `total_mw`, `deficiency`, `rto_perfscore`,
        `rega_mileage`, `regd_mileage`, `rega_hourly`, `regd_hourly`,
        `is_approved`, `modified_datetime_utc`, `published_at`.

        Known limitations (post-redesign / Reg_v2 track):
        - `regd_*` columns are ~all-null (post-2025-10 PJM stopped splitting
          the procurement); only `rega_*` is populated.
        - `rto_perfscore` is the *system* score, not per-asset. The engine
          itself uses the constant `REG_PERF_SCORE` for asset settlement
          (see `runner.py`); strategies cannot condition on a per-asset
          perf-score signal because the engine doesn't simulate that
          variation.
        - `published_at` falls back to `datetime_beginning_utc − 10 min`
          when `modified_datetime_utc` is missing (M11 §3.7.5: ASO posts
          results no later than 10 min before the start of the operating
          interval). Pre-redesign hourly rows use the same fallback.
        """
        if self._reg_market_results is not None:
            return _bitemporal_slice(self._reg_market_results, self.as_of)
        df = _lazy_load_as_feed("load_reg_market_results", _REG_MARKET_RESULTS_SCHEMA)
        return _bitemporal_slice(df, self.as_of)


# ─── The base class ──────────────────────────────────────────────────────────


class BaseStrategy(ABC):
    """One instance per battery."""

    @abstractmethod
    def on_event(self, event: Event, ctx: Context) -> StrategyResponse:
        """Decide what to do at this event. See response types above."""

    def should_resolve(self, event: Event) -> bool:
        """Default: act on every event. Override to skip events you don't care
        about — the engine will then skip calling `on_event` entirely."""
        return True
