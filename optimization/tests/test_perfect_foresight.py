"""Sanity tests for the perfect-foresight MILP.

Synthetic-LMP cases for shape correctness; one real-data test that the
ceiling actually beats a fixed daily schedule on a week of PJM history.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from pjm_engine.battery import ASSETS
from pjm_optimization.perfect_foresight import solve_perfect_foresight

UTC = timezone.utc


def _make_lmps(start: datetime, da_hourly: list[float], rt_hourly: list[float] | None = None):
    """Build (da_series, rt_series) where each hour's RT is constant across its 12 MTUs."""
    if rt_hourly is None:
        rt_hourly = list(da_hourly)
    n_hours = len(da_hourly)
    hour_idx = [start + timedelta(hours=h) for h in range(n_hours)]
    da = pd.Series(da_hourly, index=pd.DatetimeIndex(hour_idx, tz="UTC"))

    mtu_idx = [start + timedelta(minutes=5 * t) for t in range(n_hours * 12)]
    rt_vals = [rt_hourly[t // 12] for t in range(n_hours * 12)]
    rt = pd.Series(rt_vals, index=pd.DatetimeIndex(mtu_idx, tz="UTC"))
    return da, rt


# ─── Shape correctness ───────────────────────────────────────────────────────


def test_constant_lmp_no_revenue():
    """Flat DA = RT prices ⇒ no spread to capture, MILP should sit still."""
    asset = ASSETS["example_a"]
    start = datetime(2025, 10, 1, tzinfo=UTC)
    da, rt = _make_lmps(start, [50.0] * 24)

    r = solve_perfect_foresight(
        asset=asset,
        da_lmps=da,
        rt_lmps=rt,
        initial_soc_mwh=0.5 * asset.energy_mwh,
        final_soc_constraint=True,
        cycle_cost_usd_mwh=0.0,  # without cycle cost, dispatch is free; result still 0
    )
    # No spread, no revenue. Tolerance for solver float noise.
    assert abs(r.total_revenue) < 5.0, (
        f"expected ~0 revenue on flat LMP, got ${r.total_revenue:.2f}"
    )


def test_arb_when_rt_equals_da():
    """If RT ≡ DA, total revenue per hour reduces to phys × DA_LMP × dt:
    the DA/RT split is degenerate (any DA position cancels in the deviation
    leg). Just assert total revenue is positive and matches the physical
    dispatch evaluated at the realised LMP."""
    asset = ASSETS["example_a"]
    start = datetime(2025, 10, 1, tzinfo=UTC)
    pattern = [20.0] * 4 + [30.0] * 8 + [40.0] * 4 + [80.0] * 4 + [40.0] * 4
    da, rt = _make_lmps(start, pattern, pattern)

    r = solve_perfect_foresight(
        asset=asset,
        da_lmps=da,
        rt_lmps=rt,
        initial_soc_mwh=0.5 * asset.energy_mwh,
        final_soc_constraint=True,
        cycle_cost_usd_mwh=0.0,
    )
    assert r.total_revenue > 0
    # Cross-check: total revenue = sum_t phys_net[t] × LMP[t] × dt
    # (true whenever RT == DA per-MTU; verifies the MILP's accounting).
    expected = float((r.phys_net_mw * r.rt_lmps).sum() * (1.0 / 12.0))
    assert r.total_revenue == pytest.approx(expected, rel=1e-6, abs=1.0)


def test_within_hour_rt_variation_captured():
    """Hour 12 has 6 MTUs at $20 then 6 at $200. Even with link_da_to_physical
    (no virtual bidding), MILP can charge in cheap MTUs + discharge in pricy
    MTUs of the same hour, capturing the within-hour spread."""
    asset = ASSETS["example_a"]
    start = datetime(2025, 10, 1, tzinfo=UTC)
    da_pat = [50.0] * 24
    da, rt = _make_lmps(start, da_pat, da_pat)

    # Override hour 12 RT prices: 6 MTUs cheap, 6 MTUs expensive.
    h12_start = 12 * 12
    rt = rt.copy()
    for i in range(6):
        rt.iloc[h12_start + i] = 20.0
    for i in range(6, 12):
        rt.iloc[h12_start + i] = 200.0

    r = solve_perfect_foresight(
        asset=asset,
        da_lmps=da,
        rt_lmps=rt,
        initial_soc_mwh=0.5 * asset.energy_mwh,
        final_soc_constraint=True,
        cycle_cost_usd_mwh=0.0,
    )
    # Charge during the cheap half, discharge during the pricy half.
    cheap_charge = r.phys_charge_mw[h12_start : h12_start + 6].sum()
    pricy_discharge = r.phys_discharge_mw[h12_start + 6 : h12_start + 12].sum()
    assert cheap_charge > 0, "expected charge in cheap RT MTUs of hour 12"
    assert pricy_discharge > 0, "expected discharge in pricy RT MTUs of hour 12"
    assert r.total_revenue > 0


def test_soc_stays_in_bounds():
    """SoC trajectory respects [SoC_min, SoC_max] across the whole window."""
    asset = ASSETS["example_a"]
    start = datetime(2025, 10, 1, tzinfo=UTC)
    pattern = [20.0] * 6 + [80.0] * 6 + [30.0] * 6 + [90.0] * 6
    da, rt = _make_lmps(start, pattern)

    r = solve_perfect_foresight(
        asset=asset,
        da_lmps=da,
        rt_lmps=rt,
        initial_soc_mwh=0.5 * asset.energy_mwh,
        final_soc_constraint=False,
    )
    soc = r.soc_mwh
    assert soc.min() >= asset.soc_min_mwh - 1e-3, f"SoC went below floor: {soc.min()}"
    assert soc.max() <= asset.soc_max_mwh + 1e-3, f"SoC went above ceil: {soc.max()}"


def test_no_simultaneous_charge_discharge():
    """Charge and discharge cannot both be non-zero in the same MTU."""
    asset = ASSETS["example_a"]
    start = datetime(2025, 10, 1, tzinfo=UTC)
    pattern = [20.0] * 12 + [80.0] * 12
    da, rt = _make_lmps(start, pattern)

    r = solve_perfect_foresight(
        asset=asset,
        da_lmps=da,
        rt_lmps=rt,
        initial_soc_mwh=0.5 * asset.energy_mwh,
        final_soc_constraint=True,
    )
    for t in range(len(r.phys_charge_mw)):
        ch, dis = r.phys_charge_mw[t], r.phys_discharge_mw[t]
        assert not (ch > 1e-3 and dis > 1e-3), (
            f"MTU {t}: charge {ch} AND discharge {dis} both non-zero"
        )


def test_link_da_to_physical_constrains_virtual_bidding():
    """With link_da_to_physical=True, da_net[h] must equal hour-avg phys_net.
    Without it, the MILP virtual-bids ±nameplate to harvest DA-RT spread."""
    asset = ASSETS["example_a"]
    start = datetime(2025, 10, 1, tzinfo=UTC)
    # Persistent DA > RT spread so virtual-DA-discharge always profitable.
    da, rt = _make_lmps(start, [80.0] * 24, [50.0] * 24)

    with_link = solve_perfect_foresight(
        asset=asset,
        da_lmps=da,
        rt_lmps=rt,
        initial_soc_mwh=0.5 * asset.energy_mwh,
        final_soc_constraint=True,
        cycle_cost_usd_mwh=0.0,
        link_da_to_physical=True,
    )
    no_link = solve_perfect_foresight(
        asset=asset,
        da_lmps=da,
        rt_lmps=rt,
        initial_soc_mwh=0.5 * asset.energy_mwh,
        final_soc_constraint=True,
        cycle_cost_usd_mwh=0.0,
        link_da_to_physical=False,
    )
    # No-link version should make far more revenue from virtual bidding alone.
    assert no_link.total_revenue > with_link.total_revenue + 1000
    # And under the link constraint, da_net must equal hour-avg phys_net.
    phys_net = with_link.phys_discharge_mw - with_link.phys_charge_mw
    H = len(with_link.hour_starts)
    for h in range(H):
        avg_phys = phys_net[h * 12 : (h + 1) * 12].mean()
        assert with_link.da_net_mw[h] == pytest.approx(avg_phys, abs=1e-3)


# ─── Real-data integration ───────────────────────────────────────────────────


def test_pf_beats_fixed_schedule_on_a_real_week():
    """Perfect foresight is the ceiling — it must beat a dumb fixed daily
    charge/discharge schedule run through the engine on real data."""
    from datetime import date

    from pjm_engine.data import load_da_hrl_lmps, load_rt_fivemin_mnt_lmps
    from pjm_engine.events import DAGateClosing
    from pjm_engine.runner import run_backtest
    from pjm_engine.strategy_base import Product, SelfSchedule
    from pjm_engine.time_utils import operating_hour_starts_utc

    class FixedDailySchedule:
        """Charge 50 MW in the third hour, discharge 50 MW in the 19th hour
        of every operating day. No trading logic."""

        def should_resolve(self, event):
            return isinstance(event, DAGateClosing)

        def on_event(self, event, ctx):
            hours = operating_hour_starts_utc(event.operating_date)
            return [
                SelfSchedule(Product.DA_Energy, hours[2], -50.0),
                SelfSchedule(Product.DA_Energy, hours[18], +50.0),
            ]

    asset = ASSETS["example_a"]
    start_d = date(2025, 11, 16)
    end_d = date(2025, 11, 22)

    naive = run_backtest(
        strategy=FixedDailySchedule(),
        asset=asset,
        start_date=start_d,
        end_date=end_d,
        initial_soc_pct=0.5,
    )

    # Window: 00:00 EPT on start_d through 00:00 EPT on day after end_d.
    from zoneinfo import ZoneInfo

    EPT = ZoneInfo("America/New_York")
    win_start = datetime(start_d.year, start_d.month, start_d.day, tzinfo=EPT).astimezone(UTC)
    win_end = (
        datetime(end_d.year, end_d.month, end_d.day, tzinfo=EPT) + timedelta(days=1)
    ).astimezone(UTC)

    da_full = load_da_hrl_lmps()
    rt_full = load_rt_fivemin_mnt_lmps()
    da = da_full[
        (da_full["zone"] == asset.zone)
        & (da_full["datetime_beginning_utc"] >= win_start)
        & (da_full["datetime_beginning_utc"] < win_end)
    ]
    rt = rt_full[
        (rt_full["zone"] == asset.zone)
        & (rt_full["datetime_beginning_utc"] >= win_start)
        & (rt_full["datetime_beginning_utc"] < win_end)
    ]
    da_series = pd.Series(
        da["total_lmp_da"].values, index=da["datetime_beginning_utc"].values
    ).sort_index()
    rt_series = pd.Series(
        rt["total_lmp_rt"].values, index=rt["datetime_beginning_utc"].values
    ).sort_index()

    pf = solve_perfect_foresight(
        asset=asset,
        da_lmps=da_series,
        rt_lmps=rt_series,
        initial_soc_mwh=0.5 * asset.energy_mwh,
        final_soc_constraint=True,
        cycle_cost_usd_mwh=asset.cycle_cost,
    )

    assert pf.total_revenue > naive.total_revenue, (
        f"perfect-foresight ${pf.total_revenue:,.0f} should beat "
        f"the fixed schedule's ${naive.total_revenue:,.0f}"
    )
