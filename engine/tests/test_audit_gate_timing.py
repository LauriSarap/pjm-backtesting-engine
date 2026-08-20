"""D3 audit — gate timing vs M11/M12 + DST correctness.

M11 §2.3.1 timeline (DA + RT + RAC):
- 11:00 EPT D-1: DA market bid period closes.
- Prior to 13:30 EPT D-1: DA results post.
- 14:15 EPT D-1: RT energy market offer period closes (RAC starts).
- 18:30 EPT D-1 → T-65 min: RT offer revision window.
- T-65 min before each operating hour: RT offer revision lock.

pjm-data.md §2.1, Regulation product details:
- Daily Reg offer locks at D-1 14:15 EPT.
- Hourly Reg MW updates up to T-35 min.

Engine (runner.py):
- DA gate: D-1 11:00 EPT  (`_da_gate_time_for`)
- Reg offer gate: D-1 14:15 EPT  (`_reg_offer_gate_time_for`)
- RT gate: mtu_start - 5 min, per 5-min MTU  (`_build_events`)

The engine fires RT gates at T-5 min (per-MTU dispatch model) NOT T-65
(offer-revision lock). This is a deliberate design choice documented in
design.md / pjm-data.md §5.2 and is *more* permissive than reality: a strategy
that wanted to model the T-65 commit-and-freeze world would need a
different gate.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pjm_engine.battery import ASSETS
from pjm_engine.events import (
    DAGateClosing,
    RTGateClosing,
)
from pjm_engine.runner import (
    _build_events,
    _da_gate_time_for,
    _reg_offer_gate_time_for,
)
from pjm_engine.time_utils import (
    half_hour_starts_utc as _half_hour_starts_utc,
)
from pjm_engine.time_utils import (
    operating_hour_starts_utc as _operating_hour_starts_utc,
)

UTC = ZoneInfo("UTC")
EPT = ZoneInfo("America/New_York")


# ── 1. Daily gate times match M11 / pjm-data.md ─────────────────────────────────


def test_da_gate_at_d_minus_1_1100_ept_summer():
    """DA close = D-1 11:00 EPT (EDT in summer) per M11 §2.3.1."""
    op_day = date(2025, 8, 15)
    gate = _da_gate_time_for(op_day)
    gate_ept = gate.astimezone(EPT)
    assert gate_ept.date() == op_day - timedelta(days=1)
    assert (gate_ept.hour, gate_ept.minute) == (11, 0)
    assert gate_ept.utcoffset() == timedelta(hours=-4)  # EDT


def test_da_gate_at_d_minus_1_1100_ept_winter():
    """Same but EST, to confirm DST handling on the gate side."""
    op_day = date(2025, 12, 15)
    gate = _da_gate_time_for(op_day)
    gate_ept = gate.astimezone(EPT)
    assert (gate_ept.hour, gate_ept.minute) == (11, 0)
    assert gate_ept.utcoffset() == timedelta(hours=-5)  # EST


def test_reg_offer_gate_at_d_minus_1_1415_ept():
    """Daily Reg offer locks D-1 14:15 EPT (pjm-data.md §2.1)."""
    op_day = date(2025, 11, 7)
    gate = _reg_offer_gate_time_for(op_day)
    gate_ept = gate.astimezone(EPT)
    assert gate_ept.date() == op_day - timedelta(days=1)
    assert (gate_ept.hour, gate_ept.minute) == (14, 15)


def test_reg_offer_gate_after_da_gate():
    """At D-1, the Reg offer gate must come after DA close — strategies that
    want to condition their Reg stack on DA awards rely on this ordering."""
    op_day = date(2025, 11, 7)
    assert _reg_offer_gate_time_for(op_day) > _da_gate_time_for(op_day)
    assert _reg_offer_gate_time_for(op_day) - _da_gate_time_for(op_day) == timedelta(
        hours=3, minutes=15
    )


# ── 2. RT gate lead time = 5 min before each MTU ─────────────────────────────


def test_rt_gate_lead_is_5_min():
    """Engine fires RT gates at mtu_start - 5 min (per-MTU dispatch model)."""
    asset = ASSETS["example_a"]
    sched = _build_events(asset, date(2025, 11, 7), date(2025, 11, 7))
    rt_events = [e for e in sched.drain() if isinstance(e, RTGateClosing)]
    assert rt_events, "no RT gates scheduled"
    sample = rt_events[100]  # arbitrary mid-day MTU
    assert sample.timestamp == sample.mtu_start - timedelta(minutes=5)


# ── 3. DST: spring-forward day has 23 hours / 46 half-hours ──────────────────

# 2025-03-09 is the spring-forward day (02:00 EST → 03:00 EDT).
# 2025-11-02 is the fall-back day (02:00 EDT → 01:00 EST, repeating 01:00).


def test_dst_spring_forward_23_operating_hours():
    starts = _operating_hour_starts_utc(date(2025, 3, 9))
    assert len(starts) == 23, f"expected 23 EPT hours on spring-forward day, got {len(starts)}"
    # No EPT hour with hour=2 should appear in walltime.
    walltimes_ept = [s.astimezone(EPT).hour for s in starts]
    assert 2 not in walltimes_ept


def test_dst_fall_back_25_operating_hours():
    starts = _operating_hour_starts_utc(date(2025, 11, 2))
    assert len(starts) == 25, f"expected 25 EPT hours on fall-back day, got {len(starts)}"
    # The 01:00 EPT hour should appear twice in walltime (once EDT, once EST).
    walltimes_ept = [s.astimezone(EPT).hour for s in starts]
    assert walltimes_ept.count(1) == 2


def test_dst_spring_forward_46_half_hours():
    starts = _half_hour_starts_utc(date(2025, 3, 9))
    assert len(starts) == 46


def test_dst_fall_back_50_half_hours():
    starts = _half_hour_starts_utc(date(2025, 11, 2))
    assert len(starts) == 50


def test_normal_day_24_hours_48_half_hours():
    """Sanity baseline."""
    assert len(_operating_hour_starts_utc(date(2025, 11, 7))) == 24
    assert len(_half_hour_starts_utc(date(2025, 11, 7))) == 48


def test_dst_spring_forward_day_event_count():
    """Engine must schedule fewer RT gates on a 23-hour day."""
    asset = ASSETS["example_a"]
    sched_normal = _build_events(asset, date(2025, 11, 7), date(2025, 11, 7))
    sched_spring = _build_events(asset, date(2025, 3, 9), date(2025, 3, 9))
    rt_normal = [e for e in sched_normal.drain() if isinstance(e, RTGateClosing)]
    rt_spring = [e for e in sched_spring.drain() if isinstance(e, RTGateClosing)]
    assert len(rt_normal) == 24 * 12
    assert len(rt_spring) == 23 * 12


def test_dst_fall_back_day_event_count():
    """Engine must schedule extra RT gates on a 25-hour day."""
    asset = ASSETS["example_a"]
    sched_fall = _build_events(asset, date(2025, 11, 2), date(2025, 11, 2))
    rt_fall = [e for e in sched_fall.drain() if isinstance(e, RTGateClosing)]
    assert len(rt_fall) == 25 * 12


# ── 4. Event ordering at colliding timestamps ────────────────────────────────


def test_event_ordering_da_before_rt_at_same_timestamp():
    """Regression — at 11:00 EDT D-1, DA gate for D coincides with RT gate for
    mtu_start = 11:05 (gate at 11:00). All gate events share priority 10, so
    ordering is by insertion. `_build_events` pushes DA gate first when its
    operating_date is iterated — but the RT gate for D-1 was pushed earlier
    in the loop. Lock in current behavior (RT-of-D-1 fires first) so any
    future ordering change is intentional."""
    asset = ASSETS["example_a"]
    # Day D-1 = 2025-11-06, day D = 2025-11-07. DA gate for D = 2025-11-06 16:00 UTC.
    sched = _build_events(asset, date(2025, 11, 6), date(2025, 11, 7))
    da_gate = _da_gate_time_for(date(2025, 11, 7))  # 2025-11-06 16:00 UTC
    # collect colliding events
    events = list(sched.drain())
    at_collision = [e for e in events if e.timestamp == da_gate]
    types = [type(e).__name__ for e in at_collision]
    # The DA gate AND an RT gate (for mtu_start = da_gate + 5 min on D-1) must
    # both be present.
    assert "DAGateClosing" in types
    assert "RTGateClosing" in types
    # Document the current order (RT was inserted first in d=D-1 iteration).
    rt_first = next(i for i, t in enumerate(types) if t == "RTGateClosing")
    da_first = next(i for i, t in enumerate(types) if t == "DAGateClosing")
    assert rt_first < da_first, (
        "Current ordering: RT gate of D-1 fires before DA gate of D at same UTC. "
        "If you change PRIO_* constants, update this assertion."
    )


# ── 5. D6.5: per-bid DataGapError tolerance at DA gate ───────────────────────


def test_da_gate_skips_bid_for_missing_hour_without_crashing():
    """Strategy bids a UTC hour with no DA LMP row (any reason — out-of-range
    date, cache gap, future-day-persistence over a feed boundary): the runner
    must skip that bid and keep going, not raise DataGapError.
    The original audit narrative pinned this on spring-forward DST
    drift; current loader behavior fills both the prior- and operating-day
    rows in the global cache so DST alone doesn't trigger it, but the same
    fragility surfaces for any feed-boundary or pre-cache hour, so we test
    the general case."""
    from pjm_engine.battery import ASSETS
    from pjm_engine.data import (
        load_da_hrl_lmps,
        load_da_sr_prices,
        load_reg_prices,
        load_rt_fivemin_mnt_lmps,
        load_rt_sr_prices,
    )
    from pjm_engine.runner import BacktestResult, _handle_da_gate
    from pjm_engine.settle import build_price_tables
    from pjm_engine.strategy_base import (
        BaseStrategy,
        Commitments,
        Context,
        DataView,
        Product,
        SelfSchedule,
    )

    asset = ASSETS["example_a"]
    op_date = date(1999, 1, 1)
    missing_hour = datetime(1999, 1, 1, 5, 0, tzinfo=UTC)

    da_lmps = load_da_hrl_lmps()
    da_lmps_zone = da_lmps[da_lmps["zone"] == asset.zone].reset_index(drop=True)
    rt_lmps_zone = (
        load_rt_fivemin_mnt_lmps().pipe(lambda d: d[d["zone"] == asset.zone]).reset_index(drop=True)
    )

    assert (da_lmps_zone["datetime_beginning_utc"] == missing_hour).sum() == 0

    class StubBadHourStrategy(BaseStrategy):
        def on_event(self, event, ctx):
            return [SelfSchedule(Product.DA_Energy, missing_hour, 10.0)]

    gate = DAGateClosing(
        timestamp=datetime(1998, 12, 31, 16, 0, tzinfo=UTC),
        asset_id=asset.asset_id,
        operating_date=op_date,
    )
    commitments = Commitments()
    result = BacktestResult(asset=asset)
    view = DataView(as_of=gate.timestamp, da_lmps=da_lmps_zone)
    ctx = Context(asset=asset, commitments=commitments, view=view)

    from pjm_engine.data import load_da_sec_prices, load_rt_sec_prices

    tables = build_price_tables(
        da_lmps_zone,
        rt_lmps_zone,
        load_reg_prices(),
        load_da_sr_prices(),
        load_rt_sr_prices(),
        load_da_sec_prices(),
        load_rt_sec_prices(),
    )

    # Pre-fix this raised DataGapError. Now should run cleanly and emit nothing.
    _handle_da_gate(
        gate,
        StubBadHourStrategy(),
        ctx,
        asset,
        tables,
        current_soc=0.5 * asset.energy_mwh,
        commitments=commitments,
        result=result,
    )
    assert commitments.awards == []
    assert result.revenue_rows == []
