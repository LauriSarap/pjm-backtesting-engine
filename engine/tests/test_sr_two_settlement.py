"""SR/Sec two-settlement (M28 §6.2.2 + §19.2.2) — H4 fix.

Before: the RT gate paid full `MW × RT_MCP × 1/12` for any held DA SR/Sec
position, on top of the full DA capacity payment. That double-counted the
two-settlement structure: PJM's Balancing credit pays only the *delta*
between RT and DA assignments (`(RT − DA) × RT_MCP / 12`), so a strategy
that bids only DA must earn $0 at the RT gate (delta = 0).

These tests pin the new behavior so we don't regress.

LOC is independent of the credit calculation: an asset that holds upward
headroom — regardless of which gate paid for the MW — gives up RT energy
revenue. LOC therefore still emits on the *total* held SR/Sec MW.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from pjm_engine.battery import ASSETS

UTC = ZoneInfo("UTC")
DT = 1.0 / 12.0
HOUR = timedelta(hours=1)
MTU = timedelta(minutes=5)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _build_zone_frames(asset):
    """Load DA + RT LMP frames pre-filtered to this asset's zone."""
    from pjm_engine.data import load_da_hrl_lmps, load_rt_fivemin_mnt_lmps

    da_lmps = load_da_hrl_lmps()
    da_lmps_zone = da_lmps[da_lmps["zone"] == asset.zone].reset_index(drop=True)
    rt_lmps = load_rt_fivemin_mnt_lmps()
    rt_lmps_zone = rt_lmps[rt_lmps["zone"] == asset.zone].reset_index(drop=True)
    return da_lmps_zone, rt_lmps_zone


def _build_tables(da_lmps_zone, rt_lmps_zone):
    """Build the runner's PriceTables from the canonical loaders."""
    from pjm_engine.data import (
        load_da_sec_prices,
        load_da_sr_prices,
        load_reg_prices,
        load_rt_sec_prices,
        load_rt_sr_prices,
    )
    from pjm_engine.settle import build_price_tables

    return build_price_tables(
        da_lmps_zone,
        rt_lmps_zone,
        load_reg_prices(),
        load_da_sr_prices(),
        load_rt_sr_prices(),
        load_da_sec_prices(),
        load_rt_sec_prices(),
    )


# ─── 1. DA-only SR → no RT credit row ─────────────────────────────────────────


def test_da_only_sr_no_rt_credit_emitted():
    """Strategy bids SR at DA only. At the RT gate, the held DA SR position
    is the asset's *only* SR commitment — DA assignment = total assignment,
    so the M28 §6.2.2 Balancing credit delta = 0 and no RT SR_RTO row is
    emitted. The DA gate still emits its full DA capacity row."""
    from pjm_engine.events import DAGateClosing, RTGateClosing
    from pjm_engine.runner import BacktestResult, _handle_da_gate, _handle_rt_gate
    from pjm_engine.strategy_base import (
        Acknowledgment,
        BaseStrategy,
        Commitments,
        Context,
        DataView,
        Product,
        SelfSchedule,
    )

    asset = ASSETS["example_a"]
    op_date = date(2025, 11, 3)
    hour_start = datetime(2025, 11, 3, 5, 0, tzinfo=UTC)
    mtu_start = hour_start
    gate_ts = mtu_start - timedelta(minutes=5)

    da_lmps_zone, rt_lmps_zone = _build_zone_frames(asset)
    tables = _build_tables(da_lmps_zone, rt_lmps_zone)

    # Step 1: DA gate — strategy bids 50 MW SR at DA.
    class DAOnlySR(BaseStrategy):
        def on_event(self, event, ctx):
            if isinstance(event, DAGateClosing):
                return [SelfSchedule(Product.SR_RTO, hour_start, 50.0)]
            return Acknowledgment()

    commitments = Commitments()
    da_result = BacktestResult(asset=asset)
    da_view = DataView(
        as_of=datetime(2025, 11, 2, 16, 0, tzinfo=UTC),
        da_lmps=da_lmps_zone,
    )
    da_ctx = Context(asset=asset, commitments=commitments, view=da_view)
    da_gate = DAGateClosing(
        timestamp=datetime(2025, 11, 2, 16, 0, tzinfo=UTC),
        asset_id=asset.asset_id,
        operating_date=op_date,
    )
    _handle_da_gate(
        da_gate,
        DAOnlySR(),
        da_ctx,
        asset,
        tables,
        current_soc=0.5 * asset.energy_mwh,
        commitments=commitments,
        result=da_result,
    )
    da_sr_rows = [r for r in da_result.revenue_rows if r.product == "SR_RTO"]
    assert len(da_sr_rows) == 1, "DA leg should emit exactly one SR_RTO row"
    assert da_sr_rows[0].cleared_mw == 50.0
    # DA SR award is held in the DA-side cache.
    assert commitments.da_sr_rto_cleared_mw_at(hour_start) == 50.0
    assert commitments.rt_sr_rto_added_mw_at(mtu_start) == 0.0
    assert commitments.sr_rto_cleared_mw_at(hour_start) == 50.0  # total

    # Step 2: RT gate — strategy adds nothing. Delta = 0 → no RT SR row.
    rt_result = BacktestResult(asset=asset)
    rt_view = DataView(as_of=gate_ts, da_lmps=da_lmps_zone, rt_lmps=rt_lmps_zone)
    rt_ctx = Context(asset=asset, commitments=commitments, view=rt_view)
    gate = RTGateClosing(
        timestamp=gate_ts,
        asset_id=asset.asset_id,
        mtu_start=mtu_start,
    )
    _handle_rt_gate(
        gate,
        DAOnlySR(),
        rt_ctx,
        asset,
        tables,
        current_soc=0.5 * asset.energy_mwh,
        commitments=commitments,
        result=rt_result,
    )
    rt_sr_rows = [r for r in rt_result.revenue_rows if r.product == "SR_RTO"]
    assert rt_sr_rows == [], (
        "DA-only SR commitment must produce zero RT SR rows under M28 §6.2.2 "
        f"two-settlement; got {len(rt_sr_rows)}"
    )


# ─── 2. RT-added SR → delta credit row ────────────────────────────────────────


def test_rt_added_sr_pays_delta_credit():
    """Synthesize an RT-side SR award (5-min period) on an MTU and verify the
    runner emits exactly one RT SR_RTO row whose revenue equals
    `delta_MW × RT_SRMCP × 1/12`. This exercises the structural code path —
    no production strategy bids SR at RT today, but the API + handler must
    settle correctly when one does."""
    from pjm_engine.events import RTGateClosing
    from pjm_engine.markets import settle_reserve
    from pjm_engine.runner import BacktestResult, _handle_rt_gate
    from pjm_engine.settle import Award
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

    da_lmps_zone, rt_lmps_zone = _build_zone_frames(asset)
    tables = _build_tables(da_lmps_zone, rt_lmps_zone)
    rt_srmcp = tables.rt_sr_by_mtu.get(mtu_start)
    if rt_srmcp is None:
        pytest.skip("no RT SRMCP for chosen MTU in cached data")

    commitments = Commitments()
    # Manually inject an RT-side SR award (period_end = mtu_start + 5 min →
    # routed to `_rt_sr_by_period`).
    commitments.add_award(
        Award(
            product=Product.SR_RTO,
            period_start=mtu_start,
            period_end=mtu_start + MTU,
            cleared_mw=5.0,
            clearing_price=float(rt_srmcp),
        )
    )
    assert commitments.rt_sr_rto_added_mw_at(mtu_start) == 5.0
    assert commitments.da_sr_rto_cleared_mw_at(hour_start) == 0.0
    assert commitments.sr_rto_cleared_mw_at(hour_start) == 5.0  # total

    class NoOp(BaseStrategy):
        def on_event(self, event, ctx):
            return Acknowledgment()

    rt_result = BacktestResult(asset=asset)
    rt_view = DataView(as_of=gate_ts, da_lmps=da_lmps_zone, rt_lmps=rt_lmps_zone)
    rt_ctx = Context(asset=asset, commitments=commitments, view=rt_view)
    gate = RTGateClosing(
        timestamp=gate_ts,
        asset_id=asset.asset_id,
        mtu_start=mtu_start,
    )
    _handle_rt_gate(
        gate,
        NoOp(),
        rt_ctx,
        asset,
        tables,
        current_soc=0.5 * asset.energy_mwh,
        commitments=commitments,
        result=rt_result,
    )

    rt_sr_rows = [r for r in rt_result.revenue_rows if r.product == "SR_RTO"]
    assert len(rt_sr_rows) == 1
    row = rt_sr_rows[0]
    expected = settle_reserve(cleared_mw=5.0, mcp=float(rt_srmcp), hours=DT)
    assert row.cleared_mw == pytest.approx(5.0, abs=1e-9)
    assert row.clearing_price == pytest.approx(float(rt_srmcp), abs=1e-9)
    assert row.revenue == pytest.approx(expected, abs=1e-9)
    assert row.formula_version == "rt_v2_two_settlement"


# ─── 3. LOC emits on TOTAL headroom, not delta ────────────────────────────────


def test_da_sr_loc_emitted_on_total_headroom():
    """Holding 10 MW DA SR with no RT addition: the credit delta is 0 (no RT
    SR row emitted) but LOC must still see the asset's full 10 MW upward-
    headroom commitment as the foregone-energy quantity. The strategy gave
    up that 10 MW of RT discharge regardless of which gate paid for it."""
    from pjm_engine.events import RTGateClosing
    from pjm_engine.runner import BacktestResult, _handle_rt_gate
    from pjm_engine.settle import Award, PriceTables
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
    mtu_start = hour_start + timedelta(minutes=15)
    gate_ts = mtu_start - timedelta(minutes=5)

    # In-the-money DA LMP so LOC fires (LMP > cycle_cost) and the row is
    # actually emitted — purely-zero LOC produces no row.
    DA_LMP = 80.0
    tables = PriceTables(
        da_lmp_by_hour={hour_start: DA_LMP},
        rt_lmp_by_mtu={mtu_start: DA_LMP},
        da_sr_by_hour={},
        rt_sr_by_mtu={},
        da_sec_by_hour={},
        rt_sec_by_mtu={},
        reg_by_mtu={},
    )

    commitments = Commitments()
    commitments.add_award(
        Award(
            product=Product.SR_RTO,
            period_start=hour_start,
            period_end=hour_start + HOUR,
            cleared_mw=10.0,
            clearing_price=8.0,
        )
    )

    class NoOp(BaseStrategy):
        def on_event(self, event, ctx):
            return Acknowledgment()

    result = BacktestResult(asset=asset)
    view = DataView(as_of=gate_ts, da_lmps=pd.DataFrame())
    ctx = Context(asset=asset, commitments=commitments, view=view)
    gate = RTGateClosing(
        timestamp=gate_ts,
        asset_id=asset.asset_id,
        mtu_start=mtu_start,
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

    sr_rt_rows = [r for r in result.revenue_rows if r.product == "SR_RTO"]
    assert sr_rt_rows == [], "DA-only SR must not emit an RT credit row"

    loc_rows = [r for r in result.revenue_rows if r.product == "LOC_SR_RTO"]
    assert len(loc_rows) == 1
    # The runner emits LOC at reserve_mw = total_sr_mw = 10 (DA + 0 RT). The
    # cycle cap may clip the *effective* MW used in the dollar formula but
    # the cleared_mw stamp on the row is the raw reserve, not the delta.
    assert loc_rows[0].cleared_mw == pytest.approx(10.0, abs=1e-9)


# ─── 4. Sec mirror of test 1 ──────────────────────────────────────────────────


def test_sec_da_only_no_rt_credit_emitted():
    """Mirror of `test_da_only_sr_no_rt_credit_emitted` for Sec_RTO under
    M28 §19.2.2. DA-only Sec → delta = 0 → no RT Sec row."""
    from pjm_engine.events import DAGateClosing, RTGateClosing
    from pjm_engine.runner import BacktestResult, _handle_da_gate, _handle_rt_gate
    from pjm_engine.strategy_base import (
        Acknowledgment,
        BaseStrategy,
        Commitments,
        Context,
        DataView,
        Product,
        SelfSchedule,
    )

    asset = ASSETS["example_a"]
    op_date = date(2025, 11, 3)
    hour_start = datetime(2025, 11, 3, 5, 0, tzinfo=UTC)
    mtu_start = hour_start
    gate_ts = mtu_start - timedelta(minutes=5)

    da_lmps_zone, rt_lmps_zone = _build_zone_frames(asset)
    tables = _build_tables(da_lmps_zone, rt_lmps_zone)

    class DAOnlySec(BaseStrategy):
        def on_event(self, event, ctx):
            if isinstance(event, DAGateClosing):
                return [SelfSchedule(Product.Sec_RTO, hour_start, 50.0)]
            return Acknowledgment()

    commitments = Commitments()
    da_result = BacktestResult(asset=asset)
    da_view = DataView(
        as_of=datetime(2025, 11, 2, 16, 0, tzinfo=UTC),
        da_lmps=da_lmps_zone,
    )
    da_ctx = Context(asset=asset, commitments=commitments, view=da_view)
    da_gate = DAGateClosing(
        timestamp=datetime(2025, 11, 2, 16, 0, tzinfo=UTC),
        asset_id=asset.asset_id,
        operating_date=op_date,
    )
    _handle_da_gate(
        da_gate,
        DAOnlySec(),
        da_ctx,
        asset,
        tables,
        current_soc=0.5 * asset.energy_mwh,
        commitments=commitments,
        result=da_result,
    )
    assert commitments.da_sec_rto_cleared_mw_at(hour_start) == 50.0
    assert commitments.rt_sec_rto_added_mw_at(mtu_start) == 0.0
    assert commitments.sec_rto_cleared_mw_at(hour_start) == 50.0  # total

    rt_result = BacktestResult(asset=asset)
    rt_view = DataView(as_of=gate_ts, da_lmps=da_lmps_zone, rt_lmps=rt_lmps_zone)
    rt_ctx = Context(asset=asset, commitments=commitments, view=rt_view)
    gate = RTGateClosing(
        timestamp=gate_ts,
        asset_id=asset.asset_id,
        mtu_start=mtu_start,
    )
    _handle_rt_gate(
        gate,
        DAOnlySec(),
        rt_ctx,
        asset,
        tables,
        current_soc=0.5 * asset.energy_mwh,
        commitments=commitments,
        result=rt_result,
    )
    rt_sec_rows = [r for r in rt_result.revenue_rows if r.product == "Sec_RTO"]
    assert rt_sec_rows == [], (
        "DA-only Sec commitment must produce zero RT Sec rows under M28 "
        f"§19.2.2 two-settlement; got {len(rt_sec_rows)}"
    )
