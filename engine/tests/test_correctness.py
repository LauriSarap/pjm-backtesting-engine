"""Correctness tests — prove the engine actually does the right thing.

Pipeline-runs tests live elsewhere. This file checks formulas, physics, and
settlement correctness against hand-computable references.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from helpers import lookup_price

from pjm_engine.battery import ASSETS, step_soc
from pjm_engine.data import load_da_hrl_lmps, view_as_of
from pjm_engine.errors import BidValidationError, SoCInfeasibleError
from pjm_engine.settle import Award, settle
from pjm_engine.strategy_base import Product, SelfSchedule
from pjm_engine.validation import simulate_soc, validate_bid, validate_stack

UTC = ZoneInfo("UTC")
EPT = ZoneInfo("America/New_York")


@pytest.fixture(scope="module")
def da_lmps() -> pd.DataFrame:
    return load_da_hrl_lmps()


# ─── Tier 1 #1 — hand-computed revenue ────────────────────────────────────────


def test_settle_da_energy_revenue_exact(da_lmps):
    """Pick a real LMP from the loader, settle a known bid, assert revenue = mw × lmp × hours."""
    hour = datetime(2025, 11, 15, 22, 0, tzinfo=UTC)  # 22:00 UTC = 17:00 EST
    actual_lmp = lookup_price(da_lmps, "total_lmp_da", hour, zone="COMED")

    award = Award(
        product=Product.DA_Energy,
        period_start=hour,
        period_end=hour + timedelta(hours=1),
        cleared_mw=100.0,
        clearing_price=actual_lmp,
    )
    row = settle(award, asset_id="example_a", event_ts=hour)

    assert row.revenue == pytest.approx(100.0 * actual_lmp, abs=1e-9)
    assert row.cleared_mw == 100.0
    assert row.product == "DA_Energy"
    assert row.formula_version == "v1"


def test_settle_charge_revenue_is_negative(da_lmps):
    """Charging at a positive LMP costs money — revenue must be negative."""
    hour = datetime(2025, 11, 15, 8, 0, tzinfo=UTC)
    lmp = lookup_price(da_lmps, "total_lmp_da", hour, zone="COMED")
    assert lmp > 0  # sanity: zone LMP at 8am is not pricing negative
    award = Award(Product.DA_Energy, hour, hour + timedelta(hours=1), -100.0, lmp)
    row = settle(award, "example_a", hour)
    assert row.revenue == pytest.approx(-100.0 * lmp, abs=1e-9)
    assert row.revenue < 0


# ─── Tier 1 #2 — SoC physics ─────────────────────────────────────────────────


def test_soc_charge_uses_eta_in():
    """Charge X MW × 1h adds X × √RTE MWh to the battery."""
    c = ASSETS["example_a"]
    eta = math.sqrt(0.85)
    new_soc = step_soc(200.0, charge_mw=100.0, discharge_mw=0.0, dt_hours=1.0, asset=c)
    assert new_soc == pytest.approx(200.0 + 100.0 * eta, abs=1e-9)
    assert c.eta_in == pytest.approx(eta, abs=1e-12)


def test_soc_discharge_uses_eta_out():
    """Discharge X MW × 1h removes X / √RTE MWh from the battery."""
    c = ASSETS["example_a"]
    eta = math.sqrt(0.85)
    new_soc = step_soc(200.0, charge_mw=0.0, discharge_mw=100.0, dt_hours=1.0, asset=c)
    assert new_soc == pytest.approx(200.0 - 100.0 / eta, abs=1e-9)


def test_soc_round_trip_loses_15_pct():
    """Charge 100 MWh grid → discharge it back. Net grid energy = 100 × RTE = 85 MWh.
    Equivalently: a balanced cycle that returns SoC to start has discharged eta² × charge."""
    c = ASSETS["example_a"]
    soc0 = 200.0
    after_charge = step_soc(soc0, 100.0, 0.0, 1.0, c)  # +92.2
    # Discharge MW that, over 1h, restores SoC.
    discharge_mw = 100.0 * c.eta_in * c.eta_out  # = 100 × 0.85
    after_discharge = step_soc(after_charge, 0.0, discharge_mw, 1.0, c)
    assert after_discharge == pytest.approx(soc0, abs=1e-9)
    # And the discharged grid energy = 85 MWh per 100 MWh charged.
    assert discharge_mw == pytest.approx(85.0, abs=1e-9)


def test_simulate_soc_raises_on_overcharge():
    """SoC sim must raise SoCInfeasibleError when awards drive SoC > soc_max."""
    c = ASSETS["example_a"]
    # 4 hours of 250 MW charge from 50% start = +922 MWh, blows past the 900 MWh cap.
    base = datetime(2025, 11, 15, 5, 0, tzinfo=UTC)
    awards = [
        Award(
            Product.DA_Energy,
            base + timedelta(hours=h),
            base + timedelta(hours=h + 1),
            cleared_mw=-250.0,
            clearing_price=30.0,
        )
        for h in range(4)
    ]
    with pytest.raises(SoCInfeasibleError, match="out of"):
        simulate_soc(awards, initial_soc_mwh=500.0, asset=c)


def test_simulate_soc_raises_on_overdischarge():
    """And below soc_min."""
    c = ASSETS["example_a"]
    base = datetime(2025, 11, 15, 5, 0, tzinfo=UTC)
    awards = [
        Award(
            Product.DA_Energy,
            base + timedelta(hours=h),
            base + timedelta(hours=h + 1),
            cleared_mw=+100.0,
            clearing_price=80.0,
        )
        for h in range(4)
    ]
    with pytest.raises(SoCInfeasibleError, match="out of"):
        simulate_soc(awards, initial_soc_mwh=200.0, asset=c)


# ─── Tier 1 #3 — validator rejection probes ──────────────────────────────────


def _ts(h: int) -> datetime:
    return datetime(2025, 11, 15, h, 0, tzinfo=UTC)


def test_validator_rejects_overpower():
    c = ASSETS["example_a"]  # 250 MW
    with pytest.raises(BidValidationError, match="exceeds .* nameplate"):
        validate_bid(SelfSchedule(Product.DA_Energy, _ts(8), 300.0), c)


def test_validator_rejects_non_increment():
    c = ASSETS["example_a"]
    with pytest.raises(BidValidationError, match="not a multiple"):
        validate_bid(SelfSchedule(Product.DA_Energy, _ts(8), 50.05), c)


def test_validator_rejects_non_hour_aligned():
    c = ASSETS["example_a"]
    bad = datetime(2025, 11, 15, 8, 30, tzinfo=UTC)
    with pytest.raises(BidValidationError, match="top of hour"):
        validate_bid(SelfSchedule(Product.DA_Energy, bad, 50.0), c)


def test_validator_rejects_charge_and_discharge_same_period():
    c = ASSETS["example_a"]
    with pytest.raises(BidValidationError, match="charge"):
        validate_stack(
            [
                SelfSchedule(Product.DA_Energy, _ts(8), +50),
                SelfSchedule(Product.DA_Energy, _ts(8), -50),
            ],
            c,
        )


def test_validator_rejects_sum_over_nameplate():
    c = ASSETS["example_a"]  # 250 MW
    with pytest.raises(BidValidationError, match="exceeds nameplate"):
        validate_stack(
            [
                SelfSchedule(Product.DA_Energy, _ts(8), 150),
                SelfSchedule(Product.DA_Energy, _ts(8), 150),
            ],
            c,
        )


def test_validator_accepts_legal_bid():
    c = ASSETS["example_a"]
    validate_bid(SelfSchedule(Product.DA_Energy, _ts(8), 100.0), c)
    validate_stack([SelfSchedule(Product.DA_Energy, _ts(8), -100.0)], c)


# ─── Tier 1 #4 — as-of filtering at the actual gate ───────────────────────────────


def test_view_at_da_gate_excludes_operating_day(da_lmps):
    """At the DA gate close (D-1 11:00 EPT), the strategy must NOT see day-D LMPs.

    Day-D LMPs are published D-1 13:30 EPT — 2.5 hours after our gate. Day-(D-1)
    LMPs were published D-2 13:30 EPT — already visible.
    """
    op_day = date(2025, 11, 15)
    gate_local = datetime.combine(
        op_day - timedelta(days=1), datetime.min.time().replace(hour=11), tzinfo=EPT
    )
    gate_utc = gate_local.astimezone(UTC)

    view = view_as_of(da_lmps, gate_utc)
    comed = view[view["zone"] == "COMED"]
    visible_op_dates = comed["datetime_beginning_ept"].dt.date.unique()

    assert op_day not in visible_op_dates, "leak: operating day LMPs visible at its DA gate"
    assert (op_day - timedelta(days=1)) in visible_op_dates, "prior day must be visible"


# ─── Tier 2 — award conservation, determinism ────────────────────────────────


def test_award_conservation_self_schedule(da_lmps):
    """For SelfSchedule, cleared_mw must equal bid_mw exactly."""
    from helpers import FixedDailySchedule

    from pjm_engine.runner import run_backtest

    result = run_backtest(
        strategy=FixedDailySchedule(mw=50.0),
        asset=ASSETS["example_a"],
        start_date=date(2025, 11, 16),
        end_date=date(2025, 11, 17),
        initial_soc_pct=0.5,
    )
    # The fixture bids the same |MW| for every award, so every cleared row
    # must carry exactly that MW.
    charges = [abs(r.cleared_mw) for r in result.revenue_rows if r.cleared_mw < 0]
    discharges = [abs(r.cleared_mw) for r in result.revenue_rows if r.cleared_mw > 0]
    assert charges and len(set(charges)) == 1, f"charge MW not constant: {set(charges)}"
    assert discharges and len(set(discharges)) == 1, f"discharge MW not constant: {set(discharges)}"
    # Each direction must respect the nameplate.
    asset = ASSETS["example_a"]
    assert charges[0] <= asset.power_mw + 1e-6
    assert discharges[0] <= asset.power_mw + 1e-6


def test_determinism(da_lmps):
    """Same inputs → identical outputs."""
    from helpers import FixedDailySchedule

    from pjm_engine.runner import run_backtest

    args = dict(
        asset=ASSETS["example_a"],
        start_date=date(2025, 11, 16),
        end_date=date(2025, 11, 17),
        initial_soc_pct=0.5,
    )
    r1 = run_backtest(strategy=FixedDailySchedule(), **args)
    r2 = run_backtest(strategy=FixedDailySchedule(), **args)

    assert r1.total_revenue == r2.total_revenue
    assert len(r1.revenue_rows) == len(r2.revenue_rows)
    for a, b in zip(r1.revenue_rows, r2.revenue_rows):
        assert a.revenue == b.revenue
        assert a.period_start_utc == b.period_start_utc
        assert a.cleared_mw == b.cleared_mw


# ─── Tier 3 — DST 25-hour day (sanity, may be brittle) ───────────────────────


def test_dst_fall_back_day_has_25_hours(da_lmps):
    """Nov 2 2025 is the fall-back day — clocks go 02:00 EDT → 01:00 EST.
    The EPT 'date' Nov 2 spans 25 wall-clock hours."""
    comed_nov2 = da_lmps[
        (da_lmps["zone"] == "COMED")
        & (da_lmps["datetime_beginning_ept"].dt.date == date(2025, 11, 2))
    ]
    assert len(comed_nov2) == 25, f"expected 25 rows for fall-back day, got {len(comed_nov2)}"
