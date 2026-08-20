"""SR_RTO settlement, validation, and end-to-end revenue.

Synchronized Reserve only. Sec has its own tests (test_sec.py); Supplemental
doesn't exist as a separately-cleared PJM product in the DataMiner feeds, and
NSR is ESR-excluded.

The end-to-end test (`test_flat_sr_revenue_matches_hand_calc`) is the
load-bearing one: it computes expected DA + RT SR revenue from cached PJM
data using `cleared_MW × MCP × hours` and asserts the engine matches per-MTU.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from pjm_engine.battery import ASSETS
from pjm_engine.errors import BidValidationError, SoCInfeasibleError
from pjm_engine.strategy_base import Product, SelfSchedule
from pjm_engine.validation import validate_bid, validate_stack

UTC = ZoneInfo("UTC")
EPT = ZoneInfo("America/New_York")
HOUR = timedelta(hours=1)
MTU_5MIN = timedelta(minutes=5)
DT = 1.0 / 12.0


def _ts(*, year=2025, month=11, day=3, hour=5, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def da_sr_prices() -> pd.DataFrame:
    from pjm_engine.data import load_da_sr_prices

    return load_da_sr_prices()


@pytest.fixture(scope="module")
def rt_sr_prices() -> pd.DataFrame:
    from pjm_engine.data import load_rt_sr_prices

    return load_rt_sr_prices()


# ─── Formula: settle_reserve ──────────────────────────────────────────────────────


def test_settle_reserve_per_hour():
    """Capacity × MCP × hours. No mileage, no perf score in normal hours."""
    from pjm_engine.markets import settle_reserve

    rev = settle_reserve(cleared_mw=100.0, mcp=8.0, hours=1.0)
    assert rev == pytest.approx(100.0 * 8.0 * 1.0, abs=1e-9)


def test_settle_reserve_per_mtu():
    """RT SR settles per 5-min MTU at hours=1/12."""
    from pjm_engine.markets import settle_reserve

    rev = settle_reserve(cleared_mw=100.0, mcp=12.0, hours=DT)
    assert rev == pytest.approx(100.0 * 12.0 * DT, abs=1e-9)


def test_settle_reserve_zero_mw():
    from pjm_engine.markets import settle_reserve

    assert settle_reserve(0.0, 8.0, 1.0) == 0.0


def test_settle_reserve_zero_price():
    """Sec / 30MIN often clears at $0 — formula must still work."""
    from pjm_engine.markets import settle_reserve

    assert settle_reserve(100.0, 0.0, 1.0) == 0.0


# ─── Validator: SR_RTO bid-level rules ────────────────────────────────────────


def test_sr_rto_bid_must_be_hour_aligned():
    """DA SR is hourly — bid must start at top of hour."""
    a = ASSETS["example_a"]
    bad = datetime(2025, 11, 3, 5, 30, tzinfo=UTC)
    with pytest.raises(BidValidationError, match="top of hour"):
        validate_bid(SelfSchedule(Product.SR_RTO, bad, 50.0), a)


def test_sr_rto_bid_must_be_positive():
    a = ASSETS["example_a"]
    on_hour = _ts(day=3, hour=5)
    with pytest.raises(BidValidationError, match="positive upward capacity"):
        validate_bid(SelfSchedule(Product.SR_RTO, on_hour, -50.0), a)


def test_sr_rto_bid_respects_nameplate():
    a = ASSETS["example_a"]  # 250 MW
    on_hour = _ts(day=3, hour=5)
    with pytest.raises(BidValidationError, match="exceeds .* nameplate"):
        validate_bid(SelfSchedule(Product.SR_RTO, on_hour, 300.0), a)


def test_sr_rto_at_full_nameplate_legal():
    a = ASSETS["example_a"]
    on_hour = _ts(day=3, hour=5)
    validate_bid(SelfSchedule(Product.SR_RTO, on_hour, a.power_mw), a)


# ─── Cross-product cap: SR + DA + Reg ────────────────────────────────────────


def test_validate_stack_da_plus_sr_overflow():
    """250 MW DA + 250 MW SR on a 250 MW battery — capability needed = 500 > 250."""
    from pjm_engine.settle import Award

    a = ASSETS["example_a"]
    hour = _ts(day=3, hour=5)
    da = Award(Product.DA_Energy, hour, hour + HOUR, 250.0, 30.0)
    sr_bid = SelfSchedule(Product.SR_RTO, hour, 250.0)
    with pytest.raises(BidValidationError, match="cross-product power cap"):
        validate_stack([sr_bid], a, existing_awards=[da])


def test_validate_stack_da_plus_reg_plus_sr_overflow():
    """100 + 100 + 100 = 300 MW capability on a 250 MW battery — overflow."""
    from pjm_engine.settle import Award

    a = ASSETS["example_a"]
    hour = _ts(day=3, hour=5)
    half = _ts(day=3, hour=5, minute=0)
    da = Award(Product.DA_Energy, hour, hour + HOUR, 100.0, 30.0)
    reg = Award(Product.Reg_v2, half, half + timedelta(minutes=30), 100.0, 0.0)
    sr_bid = SelfSchedule(Product.SR_RTO, hour, 100.0)
    with pytest.raises(BidValidationError, match="cross-product power cap"):
        validate_stack([sr_bid], a, existing_awards=[da, reg])


def test_validate_stack_balanced_da_reg_sr_legal():
    """30 + 30 + 30 = 90 ≤ 100 — legal."""
    from pjm_engine.settle import Award

    a = ASSETS["example_a"]
    hour = _ts(day=3, hour=5)
    half = _ts(day=3, hour=5, minute=0)
    da = Award(Product.DA_Energy, hour, hour + HOUR, 30.0, 30.0)
    reg = Award(Product.Reg_v2, half, half + timedelta(minutes=30), 30.0, 0.0)
    sr_bid = SelfSchedule(Product.SR_RTO, hour, 30.0)
    validate_stack([sr_bid], a, existing_awards=[da, reg])


# ─── AS-aware SoC: SR sustain rule ────────────────────────────────────────────


def test_simulate_soc_accepts_sr_with_balanced_soc():
    """100 MW SR on the 250 MW / 1000 MWh battery at 200 MWh SoC. Sustain
    needed = 100 × 0.5 = 50 MWh discharge headroom. Available = 200 - 100 =
    100 MWh. Plenty."""
    from pjm_engine.settle import Award
    from pjm_engine.validation import simulate_soc

    a = ASSETS["example_a"]
    hour = _ts(day=3, hour=5)
    sr = Award(Product.SR_RTO, hour, hour + HOUR, 100.0, 8.0)
    simulate_soc([sr], initial_soc_mwh=200.0, asset=a)


def test_simulate_soc_rejects_sr_when_sustain_short():
    """SoC at SoC_min + 30 MWh with 100 MW SR. Sustain needed = 50 MWh
    discharge headroom; only 30 available. Must raise."""
    from pjm_engine.settle import Award
    from pjm_engine.validation import simulate_soc

    a = ASSETS["example_a"]
    hour = _ts(day=3, hour=5)
    sr = Award(Product.SR_RTO, hour, hour + HOUR, 100.0, 8.0)
    near_min_soc = a.soc_min_mwh + 30.0
    with pytest.raises(SoCInfeasibleError, match="SR sustain"):
        simulate_soc([sr], initial_soc_mwh=near_min_soc, asset=a)


# ─── Runner integration: SR_RTO at DA gate emits revenue rows ────────────────


def test_da_gate_commits_sr_award_and_emits_da_revenue():
    """Strategy returning SR_RTO self-schedules at the DA gate produces both
    an SR award and a revenue row settled at the DA SRMCP for that hour."""
    from pjm_engine.data import (
        load_da_hrl_lmps,
        load_da_sr_prices,
        load_reg_prices,
        load_rt_fivemin_mnt_lmps,
        load_rt_sr_prices,
    )
    from pjm_engine.events import DAGateClosing
    from pjm_engine.runner import BacktestResult, _handle_da_gate
    from pjm_engine.settle import build_price_tables
    from pjm_engine.strategy_base import (
        BaseStrategy,
        Commitments,
        Context,
        DataView,
    )

    asset = ASSETS["example_a"]
    op_date = date(2025, 11, 3)
    hour = _ts(day=3, hour=5)

    da_lmps = load_da_hrl_lmps()
    da_lmps_zone = da_lmps[da_lmps["zone"] == asset.zone].reset_index(drop=True)
    rt_lmps_zone = (
        load_rt_fivemin_mnt_lmps().pipe(lambda d: d[d["zone"] == asset.zone]).reset_index(drop=True)
    )

    class StubSRStrategy(BaseStrategy):
        def on_event(self, event, ctx):
            return [SelfSchedule(Product.SR_RTO, hour, 50.0)]

    gate = DAGateClosing(
        timestamp=datetime(2025, 11, 2, 16, 0, tzinfo=UTC),
        asset_id=asset.asset_id,
        operating_date=op_date,
    )
    commitments = Commitments()
    result = BacktestResult(asset=asset)
    view = DataView(as_of=gate.timestamp, da_lmps=da_lmps_zone)
    ctx = Context(asset=asset, commitments=commitments, view=view)

    da_sr = load_da_sr_prices()
    from pjm_engine.data import load_da_sec_prices, load_rt_sec_prices

    tables = build_price_tables(
        da_lmps_zone,
        rt_lmps_zone,
        load_reg_prices(),
        da_sr,
        load_rt_sr_prices(),
        load_da_sec_prices(),
        load_rt_sec_prices(),
    )
    _handle_da_gate(
        gate,
        StubSRStrategy(),
        ctx,
        asset,
        tables,
        current_soc=0.5 * asset.energy_mwh,
        commitments=commitments,
        result=result,
    )

    sr_awards = [a for a in commitments.awards if a.product == Product.SR_RTO]
    assert len(sr_awards) == 1
    assert sr_awards[0].cleared_mw == 50.0
    assert sr_awards[0].period_end - sr_awards[0].period_start == HOUR

    sr_rows = [r for r in result.revenue_rows if r.product == "SR_RTO"]
    assert len(sr_rows) == 1, f"expected 1 SR revenue row, got {len(sr_rows)}"
    # DA SR revenue = cleared_MW × DA_SRMCP × 1h
    da_sr = load_da_sr_prices()
    expected_price = float(da_sr[da_sr["datetime_beginning_utc"] == hour]["mcp"].iloc[0])
    assert sr_rows[0].clearing_price == pytest.approx(expected_price, abs=1e-9)
    assert sr_rows[0].revenue == pytest.approx(50.0 * expected_price, abs=1e-9)


# ─── End-to-end: flat SR self-schedule on a real day ─────────────────────────


class FlatSR:
    """Test fixture: self-schedule a fixed SR_RTO MW on every operating hour."""

    def __init__(self, mw: float):
        self.mw = mw

    def should_resolve(self, event) -> bool:
        from pjm_engine.events import DAGateClosing

        return isinstance(event, DAGateClosing)

    def on_event(self, event, ctx):
        from pjm_engine.time_utils import operating_hour_starts_utc

        return [
            SelfSchedule(Product.SR_RTO, h, self.mw)
            for h in operating_hour_starts_utc(event.operating_date)
        ]


def test_flat_sr_revenue_matches_hand_calc(da_sr_prices, rt_sr_prices):
    """End-to-end: hold 125 MW of SR on Nov 3 2025 (post-redesign, no DST) on
    the example asset. Hand-compute expected DA SR + RT SR revenue from cached
    MCP data; engine must match per-row to ~1e-9.
    """
    from pjm_engine.markets import settle_reserve
    from pjm_engine.runner import run_backtest

    asset = ASSETS["example_a"]  # 250 MW
    result = run_backtest(
        strategy=FlatSR(mw=125.0),
        asset=asset,
        start_date=date(2025, 11, 3),
        end_date=date(2025, 11, 3),
        initial_soc_pct=0.5,
    )
    sr_rows = [r for r in result.revenue_rows if r.product == "SR_RTO"]
    assert sr_rows, "no SR revenue rows produced"

    # Per-row formula match: revenue = cleared_MW × MCP × hours
    for r in sr_rows:
        hours = (r.period_end_utc - r.period_start_utc).total_seconds() / 3600.0
        expected = settle_reserve(
            cleared_mw=r.cleared_mw,
            mcp=r.clearing_price,
            hours=hours,
        )
        assert r.revenue == pytest.approx(expected, abs=1e-9), (
            f"{r.period_start_utc} {r.formula_version}: "
            f"engine ${r.revenue:.6f} != hand ${expected:.6f} "
            f"(mw={r.cleared_mw} mcp={r.clearing_price} h={hours})"
        )
