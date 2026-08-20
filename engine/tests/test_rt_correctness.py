"""Correctness tests — RT energy + dual settlement.

Same shape as test_correctness.py: hand-computed formulas, as-of probes,
money sanity. If any of these fail, dual-settlement is broken.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from helpers import lookup_price

from pjm_engine.battery import ASSETS
from pjm_engine.data import load_rt_fivemin_mnt_lmps, view_as_of
from pjm_engine.markets import settle_rt_energy
from pjm_engine.settle import Award, settle
from pjm_engine.strategy_base import Product

UTC = ZoneInfo("UTC")
EPT = ZoneInfo("America/New_York")


@pytest.fixture(scope="module")
def rt_lmps() -> pd.DataFrame:
    return load_rt_fivemin_mnt_lmps()


# ─── RT formula correctness ──────────────────────────────────────────────────


def test_rt_deviation_zero_when_actual_equals_da():
    """If actual_MW == DA_MW, deviation = 0, RT settlement = 0."""
    assert settle_rt_energy(actual_mw=100.0, da_cleared_mw=100.0, rt_lmp=50.0) == 0.0


def test_rt_deviation_positive_when_overdeliver_at_positive_lmp():
    """Deliver 110 against DA 100 at RT $50 over 5 min → +10 × 50 × 1/12 = $41.67."""
    rev = settle_rt_energy(actual_mw=110.0, da_cleared_mw=100.0, rt_lmp=50.0)
    assert rev == pytest.approx(10.0 * 50.0 / 12.0, abs=1e-9)


def test_rt_deviation_under_delivers_when_rt_below_da():
    """If DA cleared 100 at $80 and RT is now $30, under-delivering 60 collects spread.
    DA leg pays 100×80=$8000; RT deviation pays (60-100)×30/12 = -$100. Total = $7900.
    Vs full-deliver: 100×80 + 0 = $8000 — but you used 1.67× more SoC."""
    deviation_rev = settle_rt_energy(actual_mw=60.0, da_cleared_mw=100.0, rt_lmp=30.0)
    assert deviation_rev == pytest.approx(-40.0 * 30.0 / 12.0, abs=1e-9)


def test_settle_rt_award_dispatches_correctly(rt_lmps):
    """settle() must route RT_Energy awards through settle_rt_energy."""
    mtu = datetime(2025, 11, 15, 22, 0, tzinfo=UTC)
    actual_lmp = lookup_price(rt_lmps, "total_lmp_rt", mtu, zone="COMED")

    award = Award(
        product=Product.RT_Energy,
        period_start=mtu,
        period_end=mtu + timedelta(minutes=5),
        cleared_mw=80.0,  # actual delivery
        clearing_price=actual_lmp,
        da_cleared_mw=100.0,  # DA cleared 100 MW for the parent hour
    )
    row = settle(award, asset_id="example_a", event_ts=mtu)
    expected = (80.0 - 100.0) * actual_lmp / 12.0
    assert row.revenue == pytest.approx(expected, abs=1e-9)
    assert row.product == "RT_Energy"


# ─── as-of probe at the RT gate ─────────────────────────────────────────


def test_view_at_rt_gate_excludes_target_mtu(rt_lmps):
    """At the RT gate (mtu_start − 5min, per-MTU model), the strategy must NOT see
    the target MTU's RT LMP. published_at = MTU_start + 10min (M11 §3.7.6), so
    blind window spans the 2 MTUs ending in [t-10, t]."""
    target_mtu = datetime(2025, 11, 15, 22, 0, tzinfo=UTC)
    gate_utc = target_mtu - timedelta(minutes=5)

    view = view_as_of(rt_lmps, gate_utc)
    comed_visible = view[view["zone"] == "COMED"]
    visible_starts = set(comed_visible["datetime_beginning_utc"])  # tz-aware Timestamps
    visible_max_mtu = max(visible_starts)

    # Latest visible MTU_start must be ≤ gate − 10min (published_at = start + 10).
    assert visible_max_mtu <= gate_utc - timedelta(minutes=10), (
        f"leak: visible MTU {visible_max_mtu} too close to gate {gate_utc}"
    )
    # Target MTU not visible at its own gate.
    assert target_mtu not in visible_starts
    # 2-MTU blind window (excluding the target itself): MTUs starting at
    # target-5, target-10. Each has published_at = MTU_start + 10min,
    # which is target_mtu + (5, 0) — both > gate (target-5).
    for blind_offset in (5, 10):
        blind_mtu_start = target_mtu - timedelta(minutes=blind_offset)
        assert blind_mtu_start not in visible_starts, (
            f"MTU {blind_mtu_start} should be inside the 2-MTU blind window"
        )
    # Sanity: MTU starting target-15 must be visible (latest one published before gate).
    assert (target_mtu - timedelta(minutes=15)) in visible_starts


# ─── per-MTU SoC dynamics ────────────────────────────────────────────────────


def test_simulate_soc_walks_5min_steps_with_da_only():
    """A single 1h DA award at 100 MW discharge → 12 MTU SoC steps,
    each removing 100/√0.85 × (1/12) MWh from SoC."""
    import math

    from pjm_engine.validation import simulate_soc

    c = ASSETS["example_a"]
    base = datetime(2025, 11, 15, 18, 0, tzinfo=UTC)
    award = Award(
        Product.DA_Energy, base, base + timedelta(hours=1), cleared_mw=+100.0, clearing_price=80.0
    )
    traj = simulate_soc([award], initial_soc_mwh=300.0, asset=c)

    # Trajectory has 13 entries: start + 12 step ends.
    assert len(traj) == 13
    # Each step removes exactly 100/√0.85 / 12 MWh.
    expected_step = 100.0 / math.sqrt(0.85) / 12.0
    for i, (_, soc) in enumerate(traj):
        assert soc == pytest.approx(300.0 - i * expected_step, abs=1e-9), (
            f"step {i}: SoC {soc} ≠ {300 - i * expected_step}"
        )


def test_simulate_soc_rt_overrides_da_for_the_mtu():
    """If RT award covers an MTU, dispatch follows RT, not DA."""
    from pjm_engine.validation import simulate_soc

    c = ASSETS["example_a"]
    hour = datetime(2025, 11, 15, 18, 0, tzinfo=UTC)
    da = Award(
        Product.DA_Energy, hour, hour + timedelta(hours=1), cleared_mw=+100.0, clearing_price=80.0
    )
    # Override MTU 0 with 0 MW dispatch (full deviation, no discharge).
    rt_mtu0 = Award(
        Product.RT_Energy,
        hour,
        hour + timedelta(minutes=5),
        cleared_mw=0.0,
        clearing_price=30.0,
        da_cleared_mw=100.0,
    )

    traj = simulate_soc([da, rt_mtu0], initial_soc_mwh=300.0, asset=c)
    # MTU 0: no dispatch → SoC unchanged after 5 min.
    assert traj[1][1] == pytest.approx(300.0, abs=1e-9)
    # MTU 1 onward: DA fallback @ 100 MW discharge.
    import math

    step = 100.0 / math.sqrt(0.85) / 12.0
    assert traj[2][1] == pytest.approx(300.0 - step, abs=1e-9)


# ─── e2e: DA-only strategy still works through RT-aware runner ───────────────


def test_da_only_strategy_unchanged_through_rt_runner():
    """A DA-only strategy skips RT gates → engine dispatches at the DA
    position with zero deviation, so only DA revenue rows appear."""
    from helpers import FixedDailySchedule

    from pjm_engine.runner import run_backtest

    result = run_backtest(
        strategy=FixedDailySchedule(),
        asset=ASSETS["example_a"],
        start_date=date(2025, 11, 16),
        end_date=date(2025, 11, 17),
        initial_soc_pct=0.5,
    )
    # Must have only DA revenue, no RT deviation.
    products = {r.product for r in result.revenue_rows}
    assert products == {"DA_Energy"}, f"unexpected products: {products}"


# ─── DA gate still rejects strategy peeking at the operating day ─────────────


def test_view_da_at_rt_gate_includes_today(rt_lmps):
    """At any RT gate within an operating day, the strategy CAN see today's DA LMPs
    (they were published D-1 13:30 EPT, well before today's RT gates fired)."""
    from pjm_engine.data import load_da_hrl_lmps

    target_mtu_ept = datetime(2025, 11, 15, 18, 0, tzinfo=EPT)
    gate_utc = (target_mtu_ept - timedelta(minutes=5)).astimezone(UTC)

    da_lmps = load_da_hrl_lmps()
    view = view_as_of(da_lmps, gate_utc)
    comed = view[view["zone"] == "COMED"]
    visible_dates = set(comed["datetime_beginning_ept"].dt.date.unique())
    assert date(2025, 11, 15) in visible_dates, (
        "today's DA LMPs should be visible at RT gates of today"
    )
