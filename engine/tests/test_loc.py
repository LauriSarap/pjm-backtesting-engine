"""LOC formula + runner wiring.

`settle_loc` is `max(0, energy_LMP − cycling_cost) × reserve_MW × hours`,
forfeited when `perf_score < 0.25`. Runner emits one row per MTU per AS
product with code `LOC_SR_RTO` / `LOC_Reg_v2`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from pjm_engine.battery import ASSETS
from pjm_engine.markets import REG_LOC_FORFEIT_SCORE, settle_loc

UTC = ZoneInfo("UTC")
DT = 1.0 / 12.0


# ─── Formula ──────────────────────────────────────────────────────────────────


def test_settle_loc_in_the_money():
    """RT_LMP $80, cycle $5, 100 MW SR for 5 min: (80-5) × 100 × 1/12 = $625."""
    rev = settle_loc(energy_lmp=80.0, cycling_cost=5.0, reserve_mw=100.0, hours=DT)
    assert rev == pytest.approx(75.0 * 100.0 * DT, abs=1e-9)


def test_settle_loc_below_cycle_cost_is_zero():
    """LMP $3 < cycle $5 → no opportunity cost; LOC = 0."""
    assert settle_loc(3.0, 5.0, 100.0, DT) == 0.0


def test_settle_loc_negative_lmp_is_zero():
    """During glut hours holding reserve has no foregone profit."""
    assert settle_loc(-20.0, 5.0, 100.0, DT) == 0.0


def test_settle_loc_zero_reserve_is_zero():
    assert settle_loc(80.0, 5.0, 0.0, DT) == 0.0


def test_settle_loc_cycle_cap_clips_oversized_reserve():
    """A 1200 MWh asset reserves 300 MW; cycle cap = 2 × 1200 / 24 = 100 MW.
    Effective MW for LOC accrual is the cap, not 300."""
    cap_mw = 2.0 * 1200.0 / 24.0
    rev = settle_loc(
        energy_lmp=80.0,
        cycling_cost=5.0,
        reserve_mw=300.0,
        hours=DT,
        asset_energy_mwh=1200.0,
    )
    assert rev == pytest.approx((80.0 - 5.0) * cap_mw * DT, rel=1e-6)


def test_settle_loc_cycle_cap_passes_through_when_under():
    """A small reserve below the cap should NOT be inflated by it — formula
    must use min(reserve_mw, cap_mw), not max."""
    cap_mw = 2.0 * 1200.0 / 24.0  # 100 MW
    rev = settle_loc(
        energy_lmp=80.0,
        cycling_cost=5.0,
        reserve_mw=50.0,
        hours=DT,
        asset_energy_mwh=1200.0,
    )
    assert rev == pytest.approx((80.0 - 5.0) * 50.0 * DT, rel=1e-6)
    assert 50.0 < cap_mw  # sanity


def test_settle_loc_no_cap_when_asset_size_omitted():
    """Backwards-compat: callers without asset context get the original geometric
    formula (effectively unbounded — same as pre-2026-05-04 behavior)."""
    rev = settle_loc(
        energy_lmp=80.0,
        cycling_cost=5.0,
        reserve_mw=400.0,
        hours=DT,
    )
    assert rev == pytest.approx((80.0 - 5.0) * 400.0 * DT, rel=1e-6)


def test_settle_loc_perf_score_forfeit():
    """Reg perf < 0.25 forfeits LOC for the interval."""
    rev_full = settle_loc(80.0, 5.0, 100.0, DT, perf_score=1.0)
    rev_forfeit = settle_loc(80.0, 5.0, 100.0, DT, perf_score=0.20)
    assert rev_full > 0
    assert rev_forfeit == 0.0


def test_settle_loc_at_forfeit_threshold_pays():
    """0.25 boundary: spec says forfeit when *strictly* below, so 0.25 still pays."""
    rev = settle_loc(80.0, 5.0, 100.0, DT, perf_score=REG_LOC_FORFEIT_SCORE)
    assert rev > 0


def test_settle_loc_per_hour():
    rev_mtu = settle_loc(80.0, 5.0, 100.0, DT)
    rev_hour = settle_loc(80.0, 5.0, 100.0, 1.0)
    assert rev_hour == pytest.approx(12 * rev_mtu, abs=1e-9)


# ─── Runner wiring: SR LOC at RT gate ─────────────────────────────────────────


def test_rt_gate_emits_loc_sr_row_when_sr_held():
    """Held SR position + visible DA LMP → one LOC_SR_RTO row per MTU at the
    expected formula value. Smoke-tests the wiring in `_emit_loc_row`.

    Post `loc_v2` (2026-05-06): the runner uses the parent-hour DA LMP as
    the foregone-revenue price, not the per-MTU RT LMP. Asserts both the
    formula version and the price source.
    """
    from helpers import lookup_price

    from pjm_engine.data import (
        load_da_hrl_lmps,
        load_da_sec_prices,
        load_da_sr_prices,
        load_reg_prices,
        load_rt_fivemin_mnt_lmps,
        load_rt_sec_prices,
        load_rt_sr_prices,
    )
    from pjm_engine.events import RTGateClosing
    from pjm_engine.runner import BacktestResult, _handle_rt_gate
    from pjm_engine.settle import Award, build_price_tables
    from pjm_engine.strategy_base import (
        Acknowledgment,
        BaseStrategy,
        Commitments,
        Context,
        DataView,
        Product,
    )

    asset = ASSETS["example_a"]
    hour_start = datetime(2025, 11, 3, 5, 0, tzinfo=UTC)
    mtu_start = hour_start
    gate_ts = mtu_start - timedelta(minutes=5)

    da_lmps = load_da_hrl_lmps()
    da_lmps_zone = da_lmps[da_lmps["zone"] == asset.zone].reset_index(drop=True)
    rt_lmps = load_rt_fivemin_mnt_lmps()
    rt_lmps_zone = rt_lmps[rt_lmps["zone"] == asset.zone].reset_index(drop=True)
    da_lmp = lookup_price(da_lmps_zone, "total_lmp_da", hour_start, zone=asset.zone)

    # Pre-seed an SR_RTO award covering this hour, no DA energy.
    commitments = Commitments()
    commitments.add_award(
        Award(
            product=Product.SR_RTO,
            period_start=hour_start,
            period_end=hour_start + timedelta(hours=1),
            cleared_mw=50.0,
            clearing_price=8.0,
        )
    )

    class NoOp(BaseStrategy):
        def on_event(self, event, ctx):
            return Acknowledgment()

    gate = RTGateClosing(timestamp=gate_ts, asset_id=asset.asset_id, mtu_start=mtu_start)
    result = BacktestResult(asset=asset)
    view = DataView(as_of=gate_ts, da_lmps=da_lmps_zone, rt_lmps=rt_lmps_zone)
    ctx = Context(asset=asset, commitments=commitments, view=view)

    tables = build_price_tables(
        da_lmps_zone,
        rt_lmps_zone,
        load_reg_prices(),
        load_da_sr_prices(),
        load_rt_sr_prices(),
        load_da_sec_prices(),
        load_rt_sec_prices(),
    )

    _handle_rt_gate(
        gate,
        NoOp(),
        ctx,
        asset,
        tables,
        current_soc=0.5 * asset.energy_mwh,
        commitments=commitments,
        result=result,
    )

    loc_rows = [r for r in result.revenue_rows if r.product == "LOC_SR_RTO"]
    # Pass asset_energy_mwh so the cycle cap matches the runner's emission;
    # for the 1000 MWh example asset the cap is 2 × 1000 / 24 ≈ 83 MW, so
    # the 50 MW reserve passes through uncapped.
    expected = settle_loc(
        energy_lmp=da_lmp,
        cycling_cost=asset.cycle_cost,
        reserve_mw=50.0,
        hours=DT,
        asset_energy_mwh=asset.energy_mwh,
    )
    if expected > 0.0:
        assert len(loc_rows) == 1
        assert loc_rows[0].revenue == pytest.approx(expected, abs=1e-9)
        assert loc_rows[0].cleared_mw == 50.0
        assert loc_rows[0].clearing_price == pytest.approx(da_lmp, abs=1e-9)
        assert loc_rows[0].formula_version == "loc_v2"
    else:
        # If DA LMP ≤ cycle cost this hour, LOC is 0 and no row is emitted.
        assert loc_rows == []


def test_emit_loc_row_uses_da_lmp_not_rt_lmp():
    """Discriminating test: build PriceTables where RT LMP and DA LMP differ
    significantly at a chosen hour, run the runner's `_emit_loc_row` directly,
    and assert the emitted row's `clearing_price` matches DA LMP. This is the
    `loc_v2` semantic guarantee: foregone-revenue price is the DA arbitrage
    the AS-committed asset gave up, not the RT scarcity windfall it couldn't
    have actually captured."""
    from pjm_engine.events import RTGateClosing
    from pjm_engine.runner import BacktestResult, _emit_loc_row
    from pjm_engine.settle import PriceTables

    asset = ASSETS["example_a"]
    hour_start = datetime(2025, 11, 3, 5, 0, tzinfo=UTC)
    mtu_start = hour_start + timedelta(minutes=15)
    mtu_end = mtu_start + timedelta(minutes=5)
    gate_ts = mtu_start - timedelta(minutes=5)

    DA_LMP = 50.0  # what the asset would have arbed in DA
    RT_LMP = 800.0  # scarcity spike — must NOT drive LOC

    tables = PriceTables(
        da_lmp_by_hour={hour_start: DA_LMP},
        rt_lmp_by_mtu={mtu_start: RT_LMP},
        da_sr_by_hour={},
        rt_sr_by_mtu={},
        da_sec_by_hour={},
        rt_sec_by_mtu={},
        reg_by_mtu={},
    )

    gate = RTGateClosing(timestamp=gate_ts, asset_id=asset.asset_id, mtu_start=mtu_start)
    result = BacktestResult(asset=asset)

    _emit_loc_row(
        result=result,
        event=gate,
        asset=asset,
        period_start_utc=mtu_start,
        period_end_utc=mtu_end,
        tables=tables,
        reserve_mw=50.0,
        product_label="LOC_SR_RTO",
    )

    loc_rows = [r for r in result.revenue_rows if r.product == "LOC_SR_RTO"]
    assert len(loc_rows) == 1
    row = loc_rows[0]
    assert row.clearing_price == pytest.approx(DA_LMP, abs=1e-9)
    assert row.formula_version == "loc_v2"

    # Magnitude check: if we had wrongly used RT_LMP=$800, revenue would be
    # ~16x higher. The DA-LMP bound is exactly the point of `loc_v2`.
    expected_da = settle_loc(
        energy_lmp=DA_LMP,
        cycling_cost=asset.cycle_cost,
        reserve_mw=50.0,
        hours=DT,
        asset_energy_mwh=asset.energy_mwh,
    )
    assert row.revenue == pytest.approx(expected_da, abs=1e-9)


def test_emit_loc_row_falls_back_to_rt_when_da_missing():
    """If DA LMP is missing (rare data gap), the runner falls back to RT LMP
    rather than dropping the LOC row entirely. Verifies the fallback path.
    The emitted row's `formula_version` is still `loc_v2` — the version
    string tracks the formula contract, not which feed happened to provide
    the price."""
    from pjm_engine.events import RTGateClosing
    from pjm_engine.runner import BacktestResult, _emit_loc_row
    from pjm_engine.settle import PriceTables

    asset = ASSETS["example_a"]
    hour_start = datetime(2025, 11, 3, 5, 0, tzinfo=UTC)
    mtu_start = hour_start + timedelta(minutes=15)
    mtu_end = mtu_start + timedelta(minutes=5)
    gate_ts = mtu_start - timedelta(minutes=5)

    RT_LMP = 60.0

    tables = PriceTables(
        da_lmp_by_hour={},  # missing — gap
        rt_lmp_by_mtu={mtu_start: RT_LMP},
        da_sr_by_hour={},
        rt_sr_by_mtu={},
        da_sec_by_hour={},
        rt_sec_by_mtu={},
        reg_by_mtu={},
    )

    gate = RTGateClosing(timestamp=gate_ts, asset_id=asset.asset_id, mtu_start=mtu_start)
    result = BacktestResult(asset=asset)

    _emit_loc_row(
        result=result,
        event=gate,
        asset=asset,
        period_start_utc=mtu_start,
        period_end_utc=mtu_end,
        tables=tables,
        reserve_mw=50.0,
        product_label="LOC_SR_RTO",
    )

    loc_rows = [r for r in result.revenue_rows if r.product == "LOC_SR_RTO"]
    assert len(loc_rows) == 1
    assert loc_rows[0].clearing_price == pytest.approx(RT_LMP, abs=1e-9)


def test_emit_loc_row_skips_when_both_prices_missing():
    """No DA, no RT → no row. Engine never crashes on missing data here."""
    from pjm_engine.events import RTGateClosing
    from pjm_engine.runner import BacktestResult, _emit_loc_row
    from pjm_engine.settle import PriceTables

    asset = ASSETS["example_a"]
    mtu_start = datetime(2025, 11, 3, 5, 15, tzinfo=UTC)
    gate_ts = mtu_start - timedelta(minutes=5)

    tables = PriceTables(
        da_lmp_by_hour={},
        rt_lmp_by_mtu={},
        da_sr_by_hour={},
        rt_sr_by_mtu={},
        da_sec_by_hour={},
        rt_sec_by_mtu={},
        reg_by_mtu={},
    )

    gate = RTGateClosing(timestamp=gate_ts, asset_id=asset.asset_id, mtu_start=mtu_start)
    result = BacktestResult(asset=asset)

    _emit_loc_row(
        result=result,
        event=gate,
        asset=asset,
        period_start_utc=mtu_start,
        period_end_utc=mtu_start + timedelta(minutes=5),
        tables=tables,
        reserve_mw=50.0,
        product_label="LOC_SR_RTO",
    )
    assert result.revenue_rows == []
