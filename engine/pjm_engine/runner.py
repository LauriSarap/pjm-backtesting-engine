"""Backtest runner — main loop with DA + RT dual settlement.

Event ordering: DA gate (D-1 11:00 EPT) commits financial DA awards. Then 12
RT gates per operating hour (each at mtu_start − 5 min) ask the strategy for
actual physical dispatch. Per-MTU SoC walks at RT gates; DA gate just checks
feasibility.

Strategies that don't bid RT return Acknowledgment at RT gates; the engine
then dispatches at the DA position (zero deviation).
"""

from __future__ import annotations

import calendar
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from . import data
from .battery import AssetConfig
from .data import CachedView, dy_start_year_for_month, empty_rto_forecasts
from .errors import DataGapError, SoCInfeasibleError
from .events import (
    DAGateClosing,
    EventScheduler,
    MonthlyCapacityAccrual,
    RegDailyOfferGate,
    RTGateClosing,
    SREventCalled,
)
from .invariants import (
    assert_award_within_bid,
    assert_no_double_counted_revenue_rows,
    assert_soc_in_bounds,
)
from .markets import clear_bid_curve, settle_capacity, settle_loc, settle_reg_v2, settle_reserve
from .markets.sr_events import (
    SR_CLAWBACK_DAYS,
    SR_MIN_EVENT_MIN_FOR_CLAWBACK,
    SR_SHORTFALL_TOLERANCE_MW,
    settle_sr_clawback_per_mtu,
    settle_sr_shortfall_per_mtu,
    simulate_sr_delivery,
)
from .settle import (
    Award,
    PriceTables,
    RevenueRow,
    build_price_tables,
    settle,
)
from .strategy_base import (
    Acknowledgment,
    BaseStrategy,
    BidCurve,
    Commitments,
    Context,
    DataView,
    Product,
    SelfSchedule,
)
from .time_utils import operating_hour_starts_utc as _operating_hour_starts_utc
from .validation import bid_max_abs_mw, simulate_soc, validate_bid, validate_stack

logger = logging.getLogger(__name__)

BidLike = SelfSchedule | BidCurve

EPT = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
RT_GATE_LEAD = timedelta(minutes=5)  # gate fires 5 min before each MTU start
ONE_HOUR = timedelta(hours=1)
MTU_5MIN = timedelta(minutes=5)
HALF_HOUR = timedelta(minutes=30)

# Regulation settlement knobs under the perfect-tracking assumption.
#   - mileage_ratio = 1.0. Per PJM tariff Schedule 5.0(j), mileage_ratio =
#     asset_mileage / RegA_benchmark; under perfect tracking the asset's
#     mileage equals the system signal's mileage (which IS the benchmark
#     reported as `rega_mileage`), so the ratio collapses to 1.0. Using the
#     raw `rega_mileage` (~6-15) here would inflate the RMPCP leg by that
#     factor.
#   - perf_score = 0.95. Perfect tracking says 1.0; this 5% haircut
#     approximates observed PJM battery scores (typical 0.85-0.95). Running
#     at perf_score=1.0 inflates Reg revenue several-fold vs. observed
#     battery earnings — mathematically correct given the price feed
#     (post-redesign RMCCP elevated by scarcity-pricing co-optimization
#     spikes) but unrealistic. Tunable; flip to 1.0 to recover the
#     perfect-tracking ceiling for sensitivity analysis.
REG_PERF_SCORE = 0.95
REG_MILEAGE_RATIO = 1.0


def _half_hour_start_for(mtu_start: datetime) -> datetime:
    """Round MTU start down to the half-hour assignment block boundary."""
    return mtu_start.replace(minute=(mtu_start.minute // 30) * 30, second=0, microsecond=0)


@dataclass
class BacktestResult:
    asset: AssetConfig
    revenue_rows: list[RevenueRow] = field(default_factory=list)
    soc_trajectory: list[tuple[datetime, float]] = field(default_factory=list)

    @property
    def total_revenue(self) -> float:
        return sum(r.revenue for r in self.revenue_rows)

    @property
    def revenue_by_product(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in self.revenue_rows:
            out[r.product] = out.get(r.product, 0.0) + r.revenue
        return out

    def to_revenue_df(self) -> pd.DataFrame:
        return pd.DataFrame([r.__dict__ for r in self.revenue_rows])

    def to_soc_df(self) -> pd.DataFrame:
        return pd.DataFrame(self.soc_trajectory, columns=["ts_utc", "soc_mwh"])


# ─── event construction ──────────────────────────────────────────────────────


def _da_gate_time_for(operating_date: date) -> datetime:
    publish_local = datetime.combine(
        operating_date - timedelta(days=1),
        datetime.min.time().replace(hour=11),
        tzinfo=EPT,
    )
    return publish_local.astimezone(UTC)


def _reg_offer_gate_time_for(operating_date: date) -> datetime:
    """Daily Reg offer locks at D-1 14:15 EPT (pjm-data.md §2.1)."""
    publish_local = datetime.combine(
        operating_date - timedelta(days=1),
        datetime.min.time().replace(hour=14, minute=15),
        tzinfo=EPT,
    )
    return publish_local.astimezone(UTC)


def _build_events(
    asset: AssetConfig,
    start_date: date,
    end_date: date,
    sr_events: pd.DataFrame | None = None,
) -> EventScheduler:
    sched = EventScheduler()
    d = start_date
    while d <= end_date:
        sched.push(
            DAGateClosing(timestamp=_da_gate_time_for(d), asset_id=asset.asset_id, operating_date=d)
        )
        sched.push(
            RegDailyOfferGate(
                timestamp=_reg_offer_gate_time_for(d), asset_id=asset.asset_id, operating_date=d
            )
        )
        for hour_start in _operating_hour_starts_utc(d):
            for i in range(12):
                mtu_start = hour_start + i * MTU_5MIN
                sched.push(
                    RTGateClosing(
                        timestamp=mtu_start - RT_GATE_LEAD,
                        asset_id=asset.asset_id,
                        mtu_start=mtu_start,
                    )
                )
        d += timedelta(days=1)

    # Monthly capacity accrual: one event per (year, month) whose first day
    # falls inside [start_date, end_date]. Fires at month-1 00:00 UTC; the
    # handler emits a single `Capacity_RPM` revenue row covering the full
    # month at the BRA-cleared price for the asset's (DY, LDA). Skipping
    # months that are fully before start or after end keeps the row count
    # accurate when the window is mid-month — only fully-in-window months
    # accrue (a partial-month strategy run still gets capacity for any
    # complete month it spans).
    y, m = start_date.year, start_date.month
    while date(y, m, 1) <= end_date:
        first_of_month = date(y, m, 1)
        days_in_month = calendar.monthrange(y, m)[1]
        last_of_month = date(y, m, days_in_month)
        if first_of_month >= start_date and last_of_month <= end_date:
            timestamp_utc = datetime(y, m, 1, tzinfo=UTC)
            sched.push(
                MonthlyCapacityAccrual(
                    timestamp=timestamp_utc,
                    asset_id=asset.asset_id,
                    year=y,
                    month=m,
                    days_in_month=days_in_month,
                )
            )
        # Advance one month
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1

    # SR events: schedule any historical event whose start falls in
    # [start_date 00:00 EPT, end_date+1 00:00 EPT). Sub-zone filtering is
    # not modeled: every scheduled event is treated as obligating the asset
    # (all shipped assets are registered in the RTO sub-zone, which every
    # event covers).
    if sr_events is not None and not sr_events.empty:
        win_start = datetime.combine(
            start_date,
            datetime.min.time(),
            tzinfo=EPT,
        ).astimezone(UTC)
        win_end = datetime.combine(
            end_date + timedelta(days=1),
            datetime.min.time(),
            tzinfo=EPT,
        ).astimezone(UTC)
        in_window = sr_events[
            (sr_events["event_start_utc"] >= win_start) & (sr_events["event_start_utc"] < win_end)
        ]
        for _, row in in_window.iterrows():
            sched.push(
                SREventCalled(
                    timestamp=row["event_start_utc"].to_pydatetime(),
                    asset_id=asset.asset_id,
                    event_end=row["event_end_utc"].to_pydatetime(),
                    sub_zones=row["sub_zones"] or "",
                )
            )
    return sched


# ─── response normalization ──────────────────────────────────────────────────


def _normalize_response(resp) -> list[BidLike]:
    if isinstance(resp, Acknowledgment):
        return []
    if isinstance(resp, (SelfSchedule, BidCurve)):
        return [resp]
    if isinstance(resp, list):
        for r in resp:
            if not isinstance(r, (SelfSchedule, BidCurve)):
                raise TypeError(
                    f"unsupported item {type(r).__name__} in response list; "
                    f"expected SelfSchedule or BidCurve"
                )
        return resp
    raise TypeError(
        f"unsupported response type {type(resp).__name__}; "
        f"expected SelfSchedule, BidCurve, Acknowledgment, or list of those"
    )


def _clear_to_award(
    bid: BidLike,
    clearing_price: float,
    period_end,
    da_cleared_mw: float = 0.0,
) -> Award | None:
    """Clear a bid against a scalar clearing price.

    Distinguishes:
      - Explicit `SelfSchedule(mw=0)` → returns Award(cleared_mw=0). The
        strategy is **declaring** zero MW for this period; downstream
        settlement must treat that as a real RT position (e.g. RT_Energy
        deviation = 0 − DA_MW, charged at the RT LMP).
      - BidCurve that misses clearing → returns None. The bid was real but
        out-of-merit; the engine then falls back to the DA position with
        zero deviation.
    """
    if isinstance(bid, SelfSchedule):
        # Explicit declaration: keep the Award even at 0 MW so the runner
        # emits a deviation row instead of silently falling back to DA.
        cleared_mw = bid.mw
    else:
        cleared_mw = clear_bid_curve(bid, clearing_price)
        if abs(cleared_mw) < 1e-9:
            return None  # out-of-merit BidCurve → no award

    return Award(
        product=bid.product,
        period_start=bid.period_start,
        period_end=period_end,
        cleared_mw=cleared_mw,
        clearing_price=clearing_price,
        da_cleared_mw=da_cleared_mw,
    )


# ─── main loop ───────────────────────────────────────────────────────────────


@dataclass
class PreparedMarketData:
    """Pre-loaded zone-filtered DataFrames + indexed PriceTables for one asset.

    Built once via ``prepare_market_data(asset)`` and passed to multiple
    ``run_backtest`` calls so the load + table-build cost (~49% of a single
    strategy's runtime, per cProfile) isn't paid per strategy.
    """

    da_lmps: pd.DataFrame
    rt_lmps: pd.DataFrame
    reg_prices: pd.DataFrame
    reg_market_results: pd.DataFrame  # hourly v1 RegA/RegD prices + system perf score
    da_sr_prices: pd.DataFrame
    rt_sr_prices: pd.DataFrame
    da_sec_prices: pd.DataFrame
    rt_sec_prices: pd.DataFrame
    rto_forecasts: pd.DataFrame
    sr_events: pd.DataFrame
    rpm_clearing_prices: pd.DataFrame  # RPM BRA $/MW-day by (dy_start_year, lda)
    tables: PriceTables


def prepare_market_data(asset: AssetConfig) -> PreparedMarketData:
    """Load all raw price feeds, zone-filter for this asset, build PriceTables."""
    da_lmps = data.load_da_hrl_lmps()
    rt_lmps = data.load_rt_fivemin_mnt_lmps()
    reg_prices = data.load_reg_prices()
    reg_market_results = data.load_reg_market_results()
    da_sr_prices = data.load_da_sr_prices()
    rt_sr_prices = data.load_rt_sr_prices()
    da_sec_prices = data.load_da_sec_prices()
    rt_sec_prices = data.load_rt_sec_prices()
    try:
        rto_forecasts = data.load_rto_forecasts()
    except FileNotFoundError:
        rto_forecasts = empty_rto_forecasts()
    try:
        sr_events = data.load_sr_events()
    except FileNotFoundError:
        sr_events = pd.DataFrame(
            columns=[
                "event_start_utc",
                "event_end_utc",
                "duration_minutes",
                "sub_zones",
                "percent_deployed",
                "published_at",
            ]
        )
    try:
        rpm_clearing_prices = data.load_rpm_clearing_prices()
    except FileNotFoundError:
        # Soft-empty: capacity revenue is additive baseline; missing data
        # → no capacity rows, rest of engine still works.
        rpm_clearing_prices = pd.DataFrame(columns=["dy_start_year", "lda", "price_per_mw_day"])

    # Single-asset run → pre-filter to this asset's zone once. Strategies'
    # in-loop `[df["zone"] == asset.zone]` filters become no-ops on the
    # already-filtered data; saves a per-event pyarrow string scan that
    # dominated DART's runtime (`rt_lmps_recent` was 78% of profile).
    da_lmps = da_lmps[da_lmps["zone"] == asset.zone].reset_index(drop=True)
    rt_lmps = rt_lmps[rt_lmps["zone"] == asset.zone].reset_index(drop=True)
    # reg_prices is system-wide PJM_RTO — no zone filter.

    tables = build_price_tables(
        da_lmps,
        rt_lmps,
        reg_prices,
        da_sr_prices,
        rt_sr_prices,
        da_sec_prices,
        rt_sec_prices,
        reg_market_results=reg_market_results,
    )
    return PreparedMarketData(
        da_lmps=da_lmps,
        rt_lmps=rt_lmps,
        reg_prices=reg_prices,
        reg_market_results=reg_market_results,
        da_sr_prices=da_sr_prices,
        rt_sr_prices=rt_sr_prices,
        da_sec_prices=da_sec_prices,
        rt_sec_prices=rt_sec_prices,
        rto_forecasts=rto_forecasts,
        sr_events=sr_events,
        rpm_clearing_prices=rpm_clearing_prices,
        tables=tables,
    )


def run_backtest(
    strategy: BaseStrategy,
    asset: AssetConfig,
    start_date: date,
    end_date: date,
    initial_soc_pct: float = 0.5,
    out_dir: Path | None = None,
    market_data: PreparedMarketData | None = None,
) -> BacktestResult:
    if market_data is None:
        market_data = prepare_market_data(asset)
    da_lmps = market_data.da_lmps
    rt_lmps = market_data.rt_lmps
    reg_prices = market_data.reg_prices
    rto_forecasts = market_data.rto_forecasts
    tables = market_data.tables

    scheduler = _build_events(
        asset,
        start_date,
        end_date,
        sr_events=market_data.sr_events,
    )
    current_soc = initial_soc_pct * asset.energy_mwh
    commitments = Commitments()
    result = BacktestResult(asset=asset)
    if scheduler:
        result.soc_trajectory.append((scheduler.peek_timestamp(), current_soc))

    da_cache = CachedView(da_lmps)
    rt_cache = CachedView(rt_lmps)
    reg_price_cache = CachedView(reg_prices)
    rto_forecast_cache = CachedView(rto_forecasts)

    for event in scheduler.drain():
        # Bitemporal view for this event. CachedView memoizes by cutoff so
        # adjacent events that see the same data don't re-slice.
        view_da = da_cache.at(event.timestamp)
        view_rt = rt_cache.at(event.timestamp)
        view_reg = reg_price_cache.at(event.timestamp)
        view_rto_forecasts = rto_forecast_cache.at(event.timestamp)
        view = DataView(
            as_of=event.timestamp,
            da_lmps=view_da,
            rt_lmps=view_rt,
            reg_prices=view_reg,
            rto_forecasts=view_rto_forecasts,
        )
        ctx = Context(asset=asset, commitments=commitments, view=view, current_soc_mwh=current_soc)

        if isinstance(event, DAGateClosing):
            _handle_da_gate(event, strategy, ctx, asset, tables, current_soc, commitments, result)
        elif isinstance(event, RegDailyOfferGate):
            _handle_reg_offer_gate(
                event, strategy, ctx, asset, tables, current_soc, commitments, result
            )
        elif isinstance(event, RTGateClosing):
            current_soc = _handle_rt_gate(
                event, strategy, ctx, asset, tables, current_soc, commitments, result
            )
        elif isinstance(event, SREventCalled):
            _handle_sr_event_called(
                event,
                asset,
                tables,
                current_soc,
                commitments,
                result,
            )
        elif isinstance(event, MonthlyCapacityAccrual):
            _handle_monthly_capacity_accrual(
                event,
                asset,
                market_data.rpm_clearing_prices,
                result,
            )

    assert_no_double_counted_revenue_rows(result.revenue_rows)

    if out_dir is not None:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        result.to_revenue_df().to_parquet(out_dir / f"revenue_{asset.asset_id}.parquet")
        result.to_soc_df().to_parquet(out_dir / f"soc_{asset.asset_id}.parquet")

    return result


def _handle_da_gate(
    event: DAGateClosing,
    strategy: BaseStrategy,
    ctx: Context,
    asset: AssetConfig,
    tables: PriceTables,
    current_soc: float,
    commitments: Commitments,
    result: BacktestResult,
) -> None:
    """Commit financial DA awards. SoC walking happens at RT gates.

    DA_Energy clears at the zone's DA LMP. SR_RTO clears at the RTO Sub-Zone
    DA SRMCP and emits its DA capacity revenue immediately (per-MTU RT SR
    adjustments are then emitted at each RT gate from the held SR position)."""
    if not strategy.should_resolve(event):
        return

    response = strategy.on_event(event, ctx)
    bids = _normalize_response(response)
    if not bids:
        return

    for bid in bids:
        validate_bid(bid, asset)
    validate_stack(bids, asset, existing_awards=commitments.awards)

    # Clear at historical prices, each product against its own DA table.
    # Per-bid gap handling: a strategy persisting prior-day hours into a
    # spring-forward operating day (or any non-existent UTC hour) lands a bid
    # for a missing hour. Skip that one bid and keep going — same forgiving
    # pattern used for RT SR/Reg lookups in `_handle_rt_gate`.
    da_tables = {
        Product.DA_Energy: (tables.da_lmp_by_hour, "DA LMP"),
        Product.SR_RTO: (tables.da_sr_by_hour, "DA SR price"),
        Product.Sec_RTO: (tables.da_sec_by_hour, "DA Sec price"),
    }
    new_awards: list[Award] = []
    for bid in bids:
        if bid.product not in da_tables:
            continue  # RT_Energy bids belong at the RT gate, not DA
        price_table, price_label = da_tables[bid.product]
        price = price_table.get(bid.period_start)
        if price is None:
            logger.warning(
                "skip DA %s bid %s: no %s",
                bid.product.value,
                bid.period_start,
                price_label,
            )
            continue
        award = _clear_to_award(bid, float(price), bid.period_start + ONE_HOUR)
        if award is None:
            continue
        assert_award_within_bid(abs(award.cleared_mw), bid_max_abs_mw(bid))
        new_awards.append(award)

    # Feasibility preview: walk pending + new awards from current_soc.
    #
    # current_soc reflects every MTU dispatched up to but not including the one
    # at gate_time (RT gates fire at mtu_start − 5 min, so MTU [gate, gate+5min)
    # hasn't dispatched yet). Pending awards are those with period_start >=
    # gate_time — i.e., D-1's afternoon/evening hours not yet fired plus any RT
    # awards still in the future. Walking them alongside new_awards closes the
    # ~13-hour gap between gate (D-1 11:00 EPT) and start of D's bids that the
    # earlier "walk new_awards alone" formulation pretended away.
    pending = [a for a in commitments.awards if a.period_start >= event.timestamp]
    try:
        simulate_soc(pending + new_awards, current_soc, asset)
    except SoCInfeasibleError as e:
        logger.warning("skip DA %s: SoCInfeasible: %s", event.operating_date, e)
        return

    commitments.add_awards(new_awards)
    for award in new_awards:
        result.revenue_rows.append(settle(award, asset.asset_id, event.timestamp))


def _handle_reg_offer_gate(
    event: RegDailyOfferGate,
    strategy: BaseStrategy,
    ctx: Context,
    asset: AssetConfig,
    tables: PriceTables,
    current_soc: float,
    commitments: Commitments,
    result: BacktestResult,
) -> None:
    """Daily Reg offer commit at D-1 14:15 EPT.

    Strategy submits one of:
      - `Reg_v2` SelfSchedules for the operating day's half-hour assignment
        blocks (post-2025-10): committed as Awards; revenue settles per-MTU
        at RT gates against RMCCP/RMPCP. No revenue row emitted here.
      - `RegA_v1` / `RegD_v1` SelfSchedules for the operating day's hourly
        blocks (pre-2025-10): clearing_price is the per-product `*_hourly`
        from `reg_market_results`. Revenue is settled and emitted IMMEDIATELY
        (parallel to DA SR/Sec) since the hourly price is the all-in $/MW-h
        credit and there's no per-MTU price split for v1.
    """
    if not strategy.should_resolve(event):
        return

    response = strategy.on_event(event, ctx)
    bids = _normalize_response(response)
    if not bids:
        return

    for bid in bids:
        validate_bid(bid, asset)
    validate_stack(bids, asset, existing_awards=commitments.awards)

    new_awards: list[Award] = []
    v1_awards: list[Award] = []
    for bid in bids:
        if bid.product == Product.Reg_v2:
            period_end = bid.period_start + HALF_HOUR
            # Only SelfSchedule is supported for Reg — clearing_price is a
            # placeholder; per-MTU settlement uses RMCCP/RMPCP at RT gates.
            if not isinstance(bid, SelfSchedule):
                raise TypeError(f"Reg_v2 supports SelfSchedule only, got {type(bid).__name__}")
            award = Award(
                product=Product.Reg_v2,
                period_start=bid.period_start,
                period_end=period_end,
                cleared_mw=bid.mw,
                clearing_price=0.0,
            )
            assert_award_within_bid(abs(award.cleared_mw), bid_max_abs_mw(bid))
            new_awards.append(award)
        elif bid.product in (Product.RegA_v1, Product.RegD_v1):
            if not isinstance(bid, SelfSchedule):
                raise TypeError(
                    f"{bid.product.value} supports SelfSchedule only, got {type(bid).__name__}"
                )
            v1_prices = tables.reg_v1_by_hour.get(bid.period_start)
            if v1_prices is None:
                logger.warning(
                    "skip %s bid %s: no Reg_v1 prices (likely outside pre-2025-10 window)",
                    bid.product.value,
                    bid.period_start,
                )
                continue
            rega_hourly, regd_hourly = v1_prices
            price = rega_hourly if bid.product == Product.RegA_v1 else regd_hourly
            # Per-hour perf score from `reg_market_results.rto_perfscore` (pre-
            # redesign rows are hourly). Falls back to constant when missing.
            score = tables.perf_score_by_period.get(bid.period_start, REG_PERF_SCORE)
            award = Award(
                product=bid.product,
                period_start=bid.period_start,
                period_end=bid.period_start + ONE_HOUR,
                cleared_mw=bid.mw,
                clearing_price=price,
                perf_score=score,
            )
            assert_award_within_bid(abs(award.cleared_mw), bid_max_abs_mw(bid))
            v1_awards.append(award)

    # SoC feasibility preview: walk the operating day under existing
    # commitments + new Reg awards (v1 + v2). Reg headroom is enforced inside
    # simulate_soc; if any MTU lacks it, reject the whole batch (strategy
    # may re-bid smaller Reg next event).
    pending = [a for a in commitments.awards if a.period_start >= event.timestamp]
    try:
        simulate_soc(
            pending + new_awards + v1_awards,
            initial_soc_mwh=current_soc,
            asset=asset,
        )
    except SoCInfeasibleError as e:
        logger.warning("skip Reg %s: SoCInfeasible: %s", event.operating_date, e)
        return

    commitments.add_awards(new_awards)
    commitments.add_awards(v1_awards)
    # v1 settles immediately at the gate (hourly all-in price is known); v2
    # settles per-MTU at RT gates so no row emitted for it here.
    for award in v1_awards:
        result.revenue_rows.append(settle(award, asset.asset_id, event.timestamp))


def _emit_loc_row(
    result: BacktestResult,
    event: RTGateClosing,
    asset: AssetConfig,
    period_start_utc: datetime,
    period_end_utc: datetime,
    tables: PriceTables,
    reserve_mw: float,
    product_label: str,
    perf_score: float = 1.0,
) -> None:
    """Emit one LOC_<product> revenue row for this MTU.

    Foregone-revenue price is the **DA LMP at the parent hour**, not the
    RT LMP at this MTU. An AS-committed asset wasn't running RT energy in
    the first place — its true foregone opportunity is the DA arbitrage it
    gave up by holding upward headroom. Using RT LMP would credit RT scarcity
    windfalls the asset couldn't have actually captured (it was holding for
    SR/Sec). The cycle cap on `effective_MW` is preserved upstream in
    `settle_loc` (see `markets/loc.py`).

    Falls back to RT LMP if DA LMP is missing (rare data gap).
    """
    parent_hour_start = period_start_utc.replace(minute=0, second=0, microsecond=0)
    da_lmp = tables.da_lmp_by_hour.get(parent_hour_start)
    if da_lmp is None:
        # Data-gap fallback: use RT LMP. Logged on first miss for visibility.
        da_lmp = tables.rt_lmp_by_mtu.get(period_start_utc)
        if da_lmp is None:
            return
    revenue = settle_loc(
        energy_lmp=float(da_lmp),
        cycling_cost=asset.cycle_cost,
        reserve_mw=reserve_mw,
        hours=1.0 / 12.0,
        perf_score=perf_score,
        asset_energy_mwh=asset.energy_mwh,
    )
    if revenue == 0.0:
        return
    result.revenue_rows.append(
        RevenueRow(
            event_ts_utc=event.timestamp,
            asset_id=asset.asset_id,
            product=product_label,
            period_start_utc=period_start_utc,
            period_end_utc=period_end_utc,
            cleared_mw=reserve_mw,
            clearing_price=float(da_lmp),
            revenue=revenue,
            formula_version="loc_v2",
        )
    )


def _handle_monthly_capacity_accrual(
    event: MonthlyCapacityAccrual,
    asset: AssetConfig,
    rpm_clearing_prices: pd.DataFrame,
    result: BacktestResult,
) -> None:
    """Emit one `Capacity_RPM` row covering this calendar month.

    Looks up the BRA clearing price for `(dy_start_year, asset.lda)` where
    `dy_start_year` is derived from `(event.year, event.month)` per PJM's
    June-1-start Delivery Year convention. If the asset has zero UCAP MW
    or the price feed is missing for this DY/LDA, no row is emitted (silent
    skip — capacity is an additive baseline, not a load-bearing settlement).

    Not modeled: PAH events, offer simulation, de-rating beyond the input
    `asset.ucap_mw`. See `markets/capacity.py` module docstring."""
    if asset.ucap_mw <= 0.0 or rpm_clearing_prices.empty:
        return

    dy = dy_start_year_for_month(event.year, event.month)
    match = rpm_clearing_prices[
        (rpm_clearing_prices["dy_start_year"] == dy) & (rpm_clearing_prices["lda"] == asset.lda)
    ]
    if match.empty:
        logger.warning(
            "skip Capacity %d-%02d: no BRA price for DY %d/%d LDA %s",
            event.year,
            event.month,
            dy,
            dy + 1,
            asset.lda,
        )
        return
    price_per_mw_day = float(match["price_per_mw_day"].iloc[0])

    revenue = settle_capacity(
        ucap_mw=asset.ucap_mw,
        price_per_mw_day=price_per_mw_day,
        days_in_month=event.days_in_month,
    )
    if revenue == 0.0:
        return

    period_start = datetime(event.year, event.month, 1, tzinfo=UTC)
    period_end = period_start + timedelta(days=event.days_in_month)
    result.revenue_rows.append(
        RevenueRow(
            event_ts_utc=event.timestamp,
            asset_id=asset.asset_id,
            product=Product.Capacity_RPM.value,
            period_start_utc=period_start,
            period_end_utc=period_end,
            cleared_mw=asset.ucap_mw,
            clearing_price=price_per_mw_day,
            revenue=revenue,
            formula_version="rpm_v1",
        )
    )


def _handle_sr_event_called(
    event: SREventCalled,
    asset: AssetConfig,
    tables: PriceTables,
    current_soc: float,
    commitments: Commitments,
    result: BacktestResult,
) -> None:
    """SR event delivery check + shortfall + retroactive clawback.

    Per M28 §6.2.2 / §6.3.3 / M11 §4.5.2:

      1. **Shortfall** for the event itself — for each 5-min MTU in
         `[event.timestamp, event.event_end)`, charge
         `shortfall_MW × RT_SRMCP[mtu] × (1/12)`. One row per event,
         summed across MTUs.
      2. **Retroactive clawback** — for each 5-min MTU in
         `[event_ts - SR_CLAWBACK_DAYS, event_ts)`, charge
         `RetroactivePenaltyMW[mtu] × RT_SRMCP[mtu] × (1/12)` where
         `RetroactivePenaltyMW[mtu]` is the asset's **prior cleared SR_RTO
         MW** at that MTU's parent hour. NOT a refund of our own
         prior-credit revenue rows.
      3. **Events shorter than 10 min** (M11 §4.5.2): skip the clawback
         path entirely; only the per-event shortfall row is emitted.

    The clawback row stamps `[event_ts - SR_CLAWBACK_DAYS, event_ts]` for
    attribution.
    """
    event_hours = (event.event_end - event.timestamp).total_seconds() / 3600.0
    if event_hours <= 0.0:
        return
    parent_hour = event.timestamp.replace(minute=0, second=0, microsecond=0)
    sr_committed = commitments.sr_rto_cleared_mw_at(parent_hour)
    if sr_committed <= 0.0:
        return  # asset wasn't holding SR this hour — nothing to deliver, no risk

    delivery = simulate_sr_delivery(
        sr_committed_mw=sr_committed,
        soc_at_event_mwh=current_soc,
        soc_min_mwh=asset.soc_min_mwh,
        event_duration_hours=event_hours,
        discharge_efficiency=asset.eta_out,
    )
    if delivery.shortfall_mw <= SR_SHORTFALL_TOLERANCE_MW:
        return  # delivered in full (or within tolerance)

    # 1) Per-event shortfall row at the per-MTU RT SRMCP (M28 §6.2.2).
    _emit_event_shortfall(
        event=event,
        asset=asset,
        tables=tables,
        shortfall_mw=delivery.shortfall_mw,
        result=result,
    )

    # 2) Retroactive clawback path (M28 §6.3.3). Skip for sub-10-min events
    #    per M11 §4.5.2 — the live-event shortfall above stands but no
    #    retroactive obligation attaches.
    event_minutes = event_hours * 60.0
    if event_minutes < SR_MIN_EVENT_MIN_FOR_CLAWBACK:
        return
    _emit_retroactive_clawback(
        event=event,
        asset=asset,
        tables=tables,
        commitments=commitments,
        result=result,
    )


def _emit_event_shortfall(
    event: SREventCalled,
    asset: AssetConfig,
    tables: PriceTables,
    shortfall_mw: float,
    result: BacktestResult,
) -> None:
    """Emit one `SR_RTO_shortfall` row summed across the event's 5-min MTUs."""
    rt_srmcps: list[float] = []
    avg_price_num = 0.0
    mtu = event.timestamp
    while mtu < event.event_end:
        price = tables.rt_sr_by_mtu.get(mtu)
        if price is not None:
            rt_srmcps.append(float(price))
            avg_price_num += float(price)
        mtu = mtu + MTU_5MIN

    if not rt_srmcps:
        return  # no priced MTU intersects the event — can't settle (data gap)

    shortfall_dollars = settle_sr_shortfall_per_mtu(
        shortfall_mw=shortfall_mw,
        rt_srmcps=rt_srmcps,
    )
    avg_price = avg_price_num / len(rt_srmcps)
    result.revenue_rows.append(
        RevenueRow(
            event_ts_utc=event.timestamp,
            asset_id=asset.asset_id,
            product="SR_RTO_shortfall",
            period_start_utc=event.timestamp,
            period_end_utc=event.event_end,
            cleared_mw=shortfall_mw,
            clearing_price=avg_price,
            revenue=shortfall_dollars,
            formula_version="sr_event_v2",
        )
    )


def _emit_retroactive_clawback(
    event: SREventCalled,
    asset: AssetConfig,
    tables: PriceTables,
    commitments: Commitments,
    result: BacktestResult,
) -> None:
    """Emit one `SR_RTO_clawback` row aggregating per-MTU retroactive penalty.

    For each 5-min MTU in `[event_ts - SR_CLAWBACK_DAYS, event_ts)` where the
    asset held a non-zero SR_RTO commitment, charge
    `prior_MW × RT_SRMCP[mtu] × (1/12)` per M28 §6.3.3.
    """
    clawback_window_start = event.timestamp - timedelta(days=SR_CLAWBACK_DAYS)
    pairs: list[tuple[float, float]] = []
    mtu = clawback_window_start
    while mtu < event.timestamp:
        parent_hr = mtu.replace(minute=0, second=0, microsecond=0)
        prior_mw = commitments.sr_rto_cleared_mw_at(parent_hr)
        if prior_mw > 0.0:
            rt_srmcp = tables.rt_sr_by_mtu.get(mtu)
            if rt_srmcp is not None:
                pairs.append((float(prior_mw), float(rt_srmcp)))
        mtu = mtu + MTU_5MIN

    clawback_dollars = settle_sr_clawback_per_mtu(prior_mw_and_rt_srmcp=pairs)
    if clawback_dollars == 0.0:
        return
    result.revenue_rows.append(
        RevenueRow(
            event_ts_utc=event.timestamp,
            asset_id=asset.asset_id,
            product="SR_RTO_clawback",
            period_start_utc=clawback_window_start,
            period_end_utc=event.timestamp,
            cleared_mw=0.0,
            clearing_price=0.0,
            revenue=clawback_dollars,
            formula_version="sr_event_v2",
        )
    )


def _settle_reserve_rt_leg(
    event: RTGateClosing,
    asset: AssetConfig,
    result: BacktestResult,
    *,
    da_mw: float,
    rt_added_mw: float,
    rt_mcp_table: dict,
    product: Product,
    loc_label: str,
    tables: PriceTables,
) -> None:
    """Emit the RT balancing row (delta × RT MCP / 12) and the LOC row for
    one reserve product at one MTU. Shared by SR and Sec — the two-settlement
    shape is identical, only the price table and labels differ."""
    delta_mw = rt_added_mw  # = (DA + RT_added) − DA
    if abs(delta_mw) > 1e-9:
        rt_mcp_raw = rt_mcp_table.get(event.mtu_start)
        if rt_mcp_raw is None:
            logger.warning(
                "skip RT %s MTU %s: no RT price",
                product.value,
                event.mtu_start,
            )
        else:
            rt_mcp = float(rt_mcp_raw)
            result.revenue_rows.append(
                RevenueRow(
                    event_ts_utc=event.timestamp,
                    asset_id=asset.asset_id,
                    product=product.value,
                    period_start_utc=event.mtu_start,
                    period_end_utc=event.mtu_end,
                    cleared_mw=delta_mw,
                    clearing_price=rt_mcp,
                    revenue=settle_reserve(
                        cleared_mw=delta_mw,
                        mcp=rt_mcp,
                        hours=1.0 / 12.0,
                    ),
                    formula_version="rt_v2_two_settlement",
                )
            )
    total_mw = da_mw + rt_added_mw
    if total_mw > 0.0:
        _emit_loc_row(
            result,
            event,
            asset,
            event.mtu_start,
            event.mtu_end,
            tables,
            reserve_mw=total_mw,
            product_label=loc_label,
        )


def _handle_rt_gate(
    event: RTGateClosing,
    strategy: BaseStrategy,
    ctx: Context,
    asset: AssetConfig,
    tables: PriceTables,
    current_soc: float,
    commitments: Commitments,
    result: BacktestResult,
) -> float:
    """Per-MTU RT gate: ONE 5-min MTU of physical dispatch.

    Strategy can submit a single RT SelfSchedule or BidCurve targeting this
    MTU to deviate from DA. Engine clears at historical RT LMP, settles
    deviation, walks SoC for one 5-min step. Returns advanced current_soc.

    If the strategy returns Acknowledgment (or a bid for a different MTU),
    physical dispatch falls back to the parent hour's DA position.
    """
    mtu_start = event.mtu_start
    hour_start = event.parent_hour_start
    da_mw_for_hour = commitments.da_cleared_mw_at(hour_start)

    rt_award: Award | None = None

    if strategy.should_resolve(event):
        response = strategy.on_event(event, ctx)
        bids = _normalize_response(response)
        if bids:
            for bid in bids:
                validate_bid(bid, asset)
            validate_stack(bids, asset, existing_awards=commitments.awards)
            # Only the bid targeting this exact MTU counts; ignore stale/future bids.
            rt_bids_for_mtu = [
                b for b in bids if b.product == Product.RT_Energy and b.period_start == mtu_start
            ]
            if len(rt_bids_for_mtu) > 1:
                raise ValueError(
                    f"strategy returned {len(rt_bids_for_mtu)} RT bids for MTU "
                    f"{mtu_start}; per-MTU model accepts at most one"
                )
            if rt_bids_for_mtu:
                bid = rt_bids_for_mtu[0]
                price = tables.rt_lmp_by_mtu.get(mtu_start)
                if price is None:
                    raise DataGapError(f"no RT LMP for zone={asset.zone} mtu={mtu_start}")
                rt_award = _clear_to_award(
                    bid,
                    float(price),
                    event.mtu_end,
                    da_cleared_mw=da_mw_for_hour,
                )
                if rt_award is not None:
                    assert_award_within_bid(abs(rt_award.cleared_mw), bid_max_abs_mw(bid))

    # Per-MTU reserve two-settlement, SR (M28 §6.2.2) and Sec (M28 §19.2.2):
    # Balancing credit = (Capped_RT_assignment − DA_assignment) × RT_MCP / 12.
    # Today no strategy bids reserves at RT, so the RT assignment carries DA
    # forward and the delta is 0 → no RT row emitted; the DA leg already paid
    # full DA capacity × DA MCP × 1h at the DA gate. LOC still emits on the
    # asset's total upward-headroom commitment (which IS what the asset gives
    # up regardless of who paid for the MW).
    # Not modeled: (a) the Capped_RT < DA case — if the asset is dispatched
    # to Eco_Max the balancing credit turns negative; the SoC validator
    # prevents that case in backtests. (b) Sec's extra subtracted term,
    # "Secondary Reserve Shortfall MW" (M11 §4.5.2) — within-day Sec
    # shortfalls are treated as zero.
    _settle_reserve_rt_leg(
        event,
        asset,
        result,
        da_mw=commitments.da_sr_rto_cleared_mw_at(event.parent_hour_start),
        rt_added_mw=commitments.rt_sr_rto_added_mw_at(mtu_start),
        rt_mcp_table=tables.rt_sr_by_mtu,
        product=Product.SR_RTO,
        loc_label="LOC_SR_RTO",
        tables=tables,
    )
    _settle_reserve_rt_leg(
        event,
        asset,
        result,
        da_mw=commitments.da_sec_rto_cleared_mw_at(event.parent_hour_start),
        rt_added_mw=commitments.rt_sec_rto_added_mw_at(mtu_start),
        rt_mcp_table=tables.rt_sec_by_mtu,
        product=Product.Sec_RTO,
        loc_label="LOC_Sec_RTO",
        tables=tables,
    )

    # Per-MTU Reg revenue: independent of energy dispatch (perfect-tracking
    # activation leaves SoC unchanged). Settle first so it survives the
    # no-energy path.
    half_hour = _half_hour_start_for(mtu_start)
    reg_mw = commitments.reg_v2_cleared_mw_at(half_hour)
    if reg_mw > 0.0:
        rmccp_rmpcp = tables.reg_by_mtu.get(mtu_start)
        if rmccp_rmpcp is None:
            logger.warning("skip Reg MTU %s: no Reg price", mtu_start)
        else:
            rmccp, rmpcp = rmccp_rmpcp
            # Per-period perf score from `reg_market_results.rto_perfscore`
            # (system-wide RTO score; per-asset scores aren't published — see
            # pjm-data.md §5.3). Half-hourly cadence post-redesign,
            # keyed on the assignment-block start. Falls back to REG_PERF_SCORE
            # constant when calibration data is missing for this block.
            score = tables.perf_score_by_period.get(half_hour, REG_PERF_SCORE)
            revenue = settle_reg_v2(
                cleared_mw=reg_mw,
                perf_score=score,
                rmccp=rmccp,
                rmpcp=rmpcp,
                mileage_ratio=REG_MILEAGE_RATIO,
            )
            result.revenue_rows.append(
                RevenueRow(
                    event_ts_utc=event.timestamp,
                    asset_id=asset.asset_id,
                    product=Product.Reg_v2.value,
                    period_start_utc=mtu_start,
                    period_end_utc=event.mtu_end,
                    cleared_mw=reg_mw,
                    clearing_price=rmccp + REG_MILEAGE_RATIO * rmpcp,
                    revenue=revenue,
                    formula_version="v2",
                )
            )
        # No Reg LOC emission: PJM Regulation LOC excludes self-scheduled
        # Regulation, and every Reg award here comes from a SelfSchedule
        # (`_handle_reg_offer_gate` raises TypeError on BidCurve), so no
        # award qualifies (M28 Reg LOC).

    # Determine actual dispatch MW for this MTU: RT override else DA fallback.
    actual_mw = rt_award.cleared_mw if rt_award is not None else da_mw_for_hour
    if actual_mw == 0.0 and rt_award is None:
        # No DA position, no RT bid → nothing happens this MTU.
        return current_soc

    # Synthesize a single-MTU "dispatch award" for SoC simulation only.
    dispatch_award = Award(
        product=Product.RT_Energy,
        period_start=mtu_start,
        period_end=event.mtu_end,
        cleared_mw=actual_mw,
        clearing_price=0.0,  # unused for SoC walk
        da_cleared_mw=da_mw_for_hour,
    )

    try:
        traj = simulate_soc([dispatch_award], current_soc, asset)
    except SoCInfeasibleError as e:
        logger.warning("skip RT MTU %s: SoCInfeasible: %s", mtu_start, e)
        return current_soc

    if rt_award is not None:
        commitments.add_award(rt_award)
        result.revenue_rows.append(settle(rt_award, asset.asset_id, event.timestamp))

    if traj:
        result.soc_trajectory.extend(traj[1:])
        new_soc = traj[-1][1]
        assert_soc_in_bounds(new_soc, asset)
        return new_soc
    return current_soc
