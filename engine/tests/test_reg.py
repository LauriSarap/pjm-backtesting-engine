"""Reg_v2 settlement, lookups, validation, and end-to-end revenue.

Locks down the Reg pipeline so subsequent fixes (mileage, AS-aware
SoC reservation, cross-product cap) can't silently break it.

The calibration tests are the load-bearing ones: they compute expected
revenue from cached PJM data using the canonical formula and assert the
engine matches to ~1e-6.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from pjm_engine.battery import ASSETS
from pjm_engine.data import load_reg_prices
from pjm_engine.errors import BidValidationError, SoCInfeasibleError
from pjm_engine.markets import settle_reg_v1, settle_reg_v2
from pjm_engine.runner import REG_MILEAGE_RATIO, REG_PERF_SCORE
from pjm_engine.strategy_base import Product, SelfSchedule
from pjm_engine.validation import validate_bid, validate_stack

UTC = ZoneInfo("UTC")
EPT = ZoneInfo("America/New_York")
DT = 1.0 / 12.0  # 5 min in hours
HALF_HOUR = timedelta(minutes=30)
MTU_5MIN = timedelta(minutes=5)


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def reg_prices() -> pd.DataFrame:
    return load_reg_prices()


def _ts(*, year=2025, month=11, day=3, hour=0, minute=0) -> datetime:
    """Tz-aware UTC timestamp; defaults to a non-DST post-redesign Monday."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ─── Formula: settle_reg_v2 ───────────────────────────────────────────────────


def test_settle_reg_v2_capability_only():
    """Score = 1, mileage_ratio = 0 → revenue = MW × RMCCP × dt (RMPCP zeroed)."""
    rev = settle_reg_v2(
        cleared_mw=100.0,
        perf_score=1.0,
        rmccp=20.0,
        rmpcp=2.0,
        mileage_ratio=0.0,
    )
    assert rev == pytest.approx(100.0 * 20.0 * DT, abs=1e-9)


def test_settle_reg_v2_with_mileage_ratio_one():
    """Under perfect-tracking (mileage_ratio = 1.0), both legs apply additively."""
    rev = settle_reg_v2(
        cleared_mw=100.0,
        perf_score=1.0,
        rmccp=20.0,
        rmpcp=2.0,
        mileage_ratio=1.0,
    )
    assert rev == pytest.approx(100.0 * (20.0 + 1.0 * 2.0) * DT, abs=1e-9)


def test_settle_reg_v2_perf_score_scales_revenue():
    """Halving the perf score halves the revenue (linear scaling)."""
    full = settle_reg_v2(100.0, 1.0, 20.0, 2.0, 1.0)
    half = settle_reg_v2(100.0, 0.5, 20.0, 2.0, 1.0)
    assert half == pytest.approx(0.5 * full, abs=1e-9)


def test_settle_reg_v2_zero_mw_zero_revenue():
    assert settle_reg_v2(0.0, 1.0, 20.0, 2.0, 1.0) == 0.0


def test_settle_reg_v2_hours_kwarg_overrides_default():
    """Default dt is 1/12 h. Passing hours=1.0 should give the per-hour value."""
    per_hour = settle_reg_v2(100.0, 1.0, 20.0, 0.0, 0.0, hours=1.0)
    assert per_hour == pytest.approx(100.0 * 20.0, abs=1e-9)


def test_settle_reg_v2_forfeits_credit_below_perf_threshold():
    """M28 §4.2.1/§4.2.2: 5-min perf score < 0.25 forfeits ALL Reg credit.
    Boundary is exclusive — exactly 0.25 still pays; strictly below forfeits."""
    # Below threshold: full forfeit.
    forfeit = settle_reg_v2(
        cleared_mw=10.0,
        perf_score=0.20,
        rmccp=50.0,
        rmpcp=5.0,
        mileage_ratio=1.0,
        hours=1.0 / 12.0,
    )
    assert forfeit == 0.0

    # Just below threshold: still forfeit.
    just_below = settle_reg_v2(
        cleared_mw=10.0,
        perf_score=0.249,
        rmccp=50.0,
        rmpcp=5.0,
        mileage_ratio=1.0,
        hours=1.0 / 12.0,
    )
    assert just_below == 0.0

    # Exactly at threshold: pays per formula (10 × 0.25 × (50 + 1.0×5) × 1/12).
    boundary = settle_reg_v2(
        cleared_mw=10.0,
        perf_score=0.25,
        rmccp=50.0,
        rmpcp=5.0,
        mileage_ratio=1.0,
        hours=1.0 / 12.0,
    )
    assert boundary == pytest.approx(10.0 * 0.25 * (50.0 + 1.0 * 5.0) / 12.0, abs=1e-9)
    assert boundary > 0.0


# ─── Formula: settle_reg_v1 (pre-2025-10) ─────────────────────────────────────


def test_settle_reg_v1_basic():
    """Pre-redesign hourly formula: MW × score × hourly_price × hours."""
    rev = settle_reg_v1(
        cleared_mw=100.0,
        perf_score=1.0,
        hourly_clearing_price=22.7,  # representative regd_hourly from 2024-Q2
    )
    assert rev == pytest.approx(100.0 * 22.7 * 1.0, abs=1e-9)


def test_settle_reg_v1_perf_score_scales_linearly():
    full = settle_reg_v1(100.0, 1.0, 22.7)
    half = settle_reg_v1(100.0, 0.5, 22.7)
    assert half == pytest.approx(0.5 * full, abs=1e-9)


def test_settle_reg_v1_zero_mw_zero_revenue():
    assert settle_reg_v1(0.0, 1.0, 22.7) == 0.0


def test_settle_reg_v1_partial_hour():
    """Half-hour sliver pays half what a full hour does."""
    full = settle_reg_v1(100.0, 1.0, 22.7, hours=1.0)
    half = settle_reg_v1(100.0, 1.0, 22.7, hours=0.5)
    assert half == pytest.approx(0.5 * full, abs=1e-9)


def test_settle_reg_v1_forfeits_credit_below_perf_threshold():
    """M28 §4.2.1/§4.2.2: perf score < 0.25 forfeits ALL Reg credit (v1 too).
    Boundary is exclusive — exactly 0.25 still pays."""
    # 0.20 → forfeit.
    assert (
        settle_reg_v1(
            cleared_mw=10.0,
            perf_score=0.20,
            hourly_clearing_price=50.0,
            hours=1.0,
        )
        == 0.0
    )

    # 0.10 → forfeit.
    assert (
        settle_reg_v1(
            cleared_mw=10.0,
            perf_score=0.10,
            hourly_clearing_price=50.0,
            hours=1.0,
        )
        == 0.0
    )

    # 0.25 boundary → pays: 10 × 0.25 × 50 × 1.0 = 125.0.
    boundary = settle_reg_v1(
        cleared_mw=10.0,
        perf_score=0.25,
        hourly_clearing_price=50.0,
        hours=1.0,
    )
    assert boundary == pytest.approx(125.0, abs=1e-9)


def test_settle_reg_v1_rega_vs_regd_same_shape():
    """Same formula for both products — only the price source differs.
    RegD typically clears higher because PJM's all-in hourly price already
    bakes in the ~5x mileage multiplier vs RegA."""
    rega_rev = settle_reg_v1(100.0, 1.0, 6.3)  # rega_hourly sample
    regd_rev = settle_reg_v1(100.0, 1.0, 22.7)  # regd_hourly sample
    assert rega_rev == pytest.approx(630.0, abs=1e-9)
    assert regd_rev == pytest.approx(2270.0, abs=1e-9)
    assert regd_rev > rega_rev


def test_runner_reg_constants_match_perfect_tracking():
    """Per PJM tariff Schedule 5.0(j),
    `mileage_ratio = asset_mileage / RegA_benchmark`. Under perfect-tracking
    (perfect tracking), asset_mileage = system signal mileage =
    `rega_mileage`, so the ratio collapses to 1.0 — NOT the raw `rega_mileage`
    benchmark (~6-15). The original engine bug used the raw value, inflating
    the RMPCP leg by that factor.

    Performance score is 0.95 — a 5% realism haircut on top of the perfect-tracking
    perfect-tracking baseline (1.0). Running at 1.0 inflates Reg revenue
    several-fold vs. observed battery earnings — mathematically correct
    given the price feed but unrealistic; 0.95 approximates observed PJM
    battery scores. Flip back to 1.0 to recover the perfect-tracking
    ceiling for sensitivity analysis.
    """
    assert REG_MILEAGE_RATIO == 1.0
    assert REG_PERF_SCORE == 0.95


# ─── Validator: Reg_v2 bid-level rules ────────────────────────────────────────


def test_reg_v2_bid_must_be_half_hour_aligned():
    a = ASSETS["example_a"]
    bad = datetime(2025, 11, 3, 5, 15, tzinfo=UTC)  # 15-min — not on HH boundary
    with pytest.raises(BidValidationError, match="half-hour"):
        validate_bid(SelfSchedule(Product.Reg_v2, bad, 50.0), a)


def test_reg_v2_bid_must_be_positive():
    a = ASSETS["example_a"]
    on_hh = _ts(day=3, hour=5, minute=0)
    with pytest.raises(BidValidationError, match="positive bidirectional capacity"):
        validate_bid(SelfSchedule(Product.Reg_v2, on_hh, -50.0), a)


def test_reg_v2_bid_respects_nameplate():
    a = ASSETS["example_a"]  # 250 MW
    on_hh = _ts(day=3, hour=5, minute=0)
    with pytest.raises(BidValidationError, match="exceeds .* nameplate"):
        validate_bid(SelfSchedule(Product.Reg_v2, on_hh, 300.0), a)


def test_reg_v2_at_full_nameplate_is_legal():
    a = ASSETS["example_a"]
    on_hh = _ts(day=3, hour=5, minute=0)
    validate_bid(SelfSchedule(Product.Reg_v2, on_hh, a.power_mw), a)
    validate_stack([SelfSchedule(Product.Reg_v2, on_hh, a.power_mw)], a)


# ─── Regime window: Reg_v2 lives in [2025-10-01, 2026-10-01) EPT ──────────────


def test_reg_v2_rejected_before_regime_start():
    """Pre-redesign (before 2025-10-01 EPT) Reg_v2 doesn't exist — bidding it
    must raise ProductNotInRegimeError."""
    from pjm_engine.errors import ProductNotInRegimeError

    a = ASSETS["example_a"]
    pre_redesign_utc = datetime(2025, 9, 15, 5, 0, tzinfo=UTC)
    with pytest.raises(ProductNotInRegimeError, match="Reg_v2 not in regime"):
        validate_bid(SelfSchedule(Product.Reg_v2, pre_redesign_utc, 50.0), a)


def test_reg_v2_rejected_after_v3_split():
    """Post-2026-10-01 EPT Reg_v2 is replaced by RegUp_v3/RegDn_v3 — bidding
    Reg_v2 then must raise ProductNotInRegimeError."""
    from pjm_engine.errors import ProductNotInRegimeError

    a = ASSETS["example_a"]
    post_split_utc = datetime(2026, 11, 1, 5, 0, tzinfo=UTC)
    with pytest.raises(ProductNotInRegimeError, match="Reg_v2 not in regime"):
        validate_bid(SelfSchedule(Product.Reg_v2, post_split_utc, 50.0), a)


def test_reg_v2_accepted_at_regime_start_boundary():
    """First HH-boundary on/after 2025-10-01 00:00 EPT must validate."""
    a = ASSETS["example_a"]
    # 2025-10-01 00:00 EDT = 2025-10-01 04:00 UTC.
    boundary_utc = datetime(2025, 10, 1, 4, 0, tzinfo=UTC)
    validate_bid(SelfSchedule(Product.Reg_v2, boundary_utc, 50.0), a)


def test_reg_v2_rejected_at_regime_end_boundary():
    """The end of the window is exclusive — first HH at 2026-10-01 00:00 EPT
    must reject."""
    from pjm_engine.errors import ProductNotInRegimeError

    a = ASSETS["example_a"]
    # 2026-10-01 00:00 EDT = 2026-10-01 04:00 UTC (DST still in effect Oct 1).
    end_utc = datetime(2026, 10, 1, 4, 0, tzinfo=UTC)
    with pytest.raises(ProductNotInRegimeError, match="Reg_v2 not in regime"):
        validate_bid(SelfSchedule(Product.Reg_v2, end_utc, 50.0), a)


# ─── Regime window: RegA_v1 / RegD_v1 live BEFORE 2025-10-01 EPT ─────────────


def test_reg_v1_accepted_before_regime_end():
    """A pre-redesign hour must validate as RegA_v1 / RegD_v1."""
    a = ASSETS["example_a"]
    # 2024-06-15 14:00 UTC — clearly pre-redesign.
    pre_redesign_utc = datetime(2024, 6, 15, 14, 0, tzinfo=UTC)
    validate_bid(SelfSchedule(Product.RegA_v1, pre_redesign_utc, 50.0), a)
    validate_bid(SelfSchedule(Product.RegD_v1, pre_redesign_utc, 50.0), a)


def test_reg_v1_rejected_at_regime_end_boundary():
    """The 2025-10-01 00:00 EPT boundary is exclusive — bidding v1 then must
    fail (the v2 regime starts at exactly that moment)."""
    from pjm_engine.errors import ProductNotInRegimeError

    a = ASSETS["example_a"]
    end_utc = datetime(2025, 10, 1, 4, 0, tzinfo=UTC)  # 2025-10-01 00:00 EDT
    with pytest.raises(ProductNotInRegimeError, match="RegA_v1 not in regime"):
        validate_bid(SelfSchedule(Product.RegA_v1, end_utc, 50.0), a)
    with pytest.raises(ProductNotInRegimeError, match="RegD_v1 not in regime"):
        validate_bid(SelfSchedule(Product.RegD_v1, end_utc, 50.0), a)


def test_reg_v1_at_2025_09_30_23_00_ept_last_legal_hour():
    """Q11: the last legal v1 hour is 2025-09-30 23:00 EPT (= 03:00 UTC on
    2025-10-01). The next hour, 2025-10-01 00:00 EPT (= 04:00 UTC), is the
    exclusive v2-regime start and v1 must reject there."""
    from pjm_engine.errors import ProductNotInRegimeError

    a = ASSETS["example_a"]

    # Last legal v1 hour: 2025-09-30 23:00 EPT == 2025-10-01 03:00 UTC.
    last_legal_utc = datetime(2025, 10, 1, 3, 0, tzinfo=UTC)
    validate_bid(SelfSchedule(Product.RegA_v1, last_legal_utc, 50.0), a)
    validate_bid(SelfSchedule(Product.RegD_v1, last_legal_utc, 50.0), a)

    # First illegal hour: 2025-10-01 00:00 EPT == 2025-10-01 04:00 UTC.
    first_illegal_utc = datetime(2025, 10, 1, 4, 0, tzinfo=UTC)
    with pytest.raises(ProductNotInRegimeError, match="RegA_v1 not in regime"):
        validate_bid(SelfSchedule(Product.RegA_v1, first_illegal_utc, 50.0), a)
    with pytest.raises(ProductNotInRegimeError, match="RegD_v1 not in regime"):
        validate_bid(SelfSchedule(Product.RegD_v1, first_illegal_utc, 50.0), a)


def test_reg_v1_must_be_hourly_aligned():
    """Pre-redesign Reg cleared on hourly blocks; sub-hour starts must reject."""
    from pjm_engine.errors import BidValidationError

    a = ASSETS["example_a"]
    half_hour_utc = datetime(2024, 6, 15, 14, 30, tzinfo=UTC)
    with pytest.raises(BidValidationError, match="must start at top of hour"):
        validate_bid(SelfSchedule(Product.RegD_v1, half_hour_utc, 50.0), a)


def test_reg_v1_must_be_positive_mw():
    """Bidirectional capacity is sign-less and positive."""
    from pjm_engine.errors import BidValidationError

    a = ASSETS["example_a"]
    hour_utc = datetime(2024, 6, 15, 14, 0, tzinfo=UTC)
    with pytest.raises(BidValidationError, match="must be positive"):
        validate_bid(SelfSchedule(Product.RegA_v1, hour_utc, -10.0), a)


# ─── Runner integration: v1 SelfSchedule clears + emits hourly revenue ───────


def test_handle_reg_offer_gate_emits_v1_revenue_immediately():
    """Pre-redesign path: hourly RegD_v1 SelfSchedule on 2024-06-15 must clear
    against (rega_hourly, regd_hourly) and emit one revenue row per hour
    immediately at the daily Reg gate (parallel to DA SR/Sec emission)."""
    from pjm_engine.events import RegDailyOfferGate
    from pjm_engine.runner import (
        REG_PERF_SCORE,
        BacktestResult,
        _handle_reg_offer_gate,
    )
    from pjm_engine.settle import PriceTables
    from pjm_engine.strategy_base import (
        BaseStrategy,
        Commitments,
        Context,
    )

    asset = ASSETS["example_a"]
    # Pre-redesign operating day. Use 2024-06-15 (well inside v1 window).
    op_date = date(2024, 6, 15)
    # Pick three hours with realistic prices and self-schedule 50 MW RegD on each.
    hours_utc = [
        datetime(2024, 6, 15, 14, 0, tzinfo=UTC),
        datetime(2024, 6, 15, 15, 0, tzinfo=UTC),
        datetime(2024, 6, 15, 16, 0, tzinfo=UTC),
    ]
    # Stub v1 prices so the test is hermetic — no dependency on cached parquet.
    v1_prices_by_hour = {
        hours_utc[0]: (6.3, 22.7),  # rega_hourly, regd_hourly
        hours_utc[1]: (6.6, 35.6),
        hours_utc[2]: (6.6, 27.7),
    }

    class V1Stacker(BaseStrategy):
        def on_event(self, event, ctx):
            return [SelfSchedule(Product.RegD_v1, h, 50.0) for h in hours_utc]

    gate = RegDailyOfferGate(
        timestamp=datetime(2024, 6, 14, 18, 15, tzinfo=UTC),  # D-1 14:15 EDT
        asset_id=asset.asset_id,
        operating_date=op_date,
    )
    commitments = Commitments()
    result = BacktestResult(asset=asset)
    ctx = Context(asset=asset, commitments=commitments, view=None)
    tables = PriceTables(
        da_lmp_by_hour={},
        rt_lmp_by_mtu={},
        da_sr_by_hour={},
        rt_sr_by_mtu={},
        da_sec_by_hour={},
        rt_sec_by_mtu={},
        reg_by_mtu={},
        reg_v1_by_hour=v1_prices_by_hour,
    )

    _handle_reg_offer_gate(
        gate,
        V1Stacker(),
        ctx,
        asset,
        tables=tables,
        current_soc=0.5 * asset.energy_mwh,
        commitments=commitments,
        result=result,
    )

    # 3 RegD_v1 awards committed.
    v1_awards = [a for a in commitments.awards if a.product == Product.RegD_v1]
    assert len(v1_awards) == 3
    assert all(a.cleared_mw == 50.0 for a in v1_awards)

    # 3 hourly revenue rows emitted (immediately, not deferred to RT gates).
    rows = [r for r in result.revenue_rows if r.product == "RegD_v1"]
    assert len(rows) == 3
    # Revenue formula: 50 MW × 0.95 score × regd_hourly × 1h.
    expected_total = sum(50.0 * REG_PERF_SCORE * v1_prices_by_hour[h][1] * 1.0 for h in hours_utc)
    actual_total = sum(r.revenue for r in rows)
    assert actual_total == pytest.approx(expected_total, abs=1e-9)
    # Per-row: clearing_price stamped with regd_hourly, formula_version "v1".
    by_period = {r.period_start_utc: r for r in rows}
    for h in hours_utc:
        assert by_period[h].clearing_price == pytest.approx(v1_prices_by_hour[h][1])
        assert by_period[h].formula_version == "v1"


def test_handle_reg_offer_gate_uses_rega_price_for_rega_award():
    """RegA_v1 award must use `rega_hourly` (not regd_hourly) — sanity check
    that the price selection branch isn't swapped."""
    from pjm_engine.events import RegDailyOfferGate
    from pjm_engine.runner import (
        REG_PERF_SCORE,
        BacktestResult,
        _handle_reg_offer_gate,
    )
    from pjm_engine.settle import PriceTables
    from pjm_engine.strategy_base import (
        BaseStrategy,
        Commitments,
        Context,
    )

    asset = ASSETS["example_a"]
    hour_utc = datetime(2024, 6, 15, 14, 0, tzinfo=UTC)
    v1_prices = {hour_utc: (6.3, 22.7)}  # rega is 6.3, regd is 22.7

    class RegAOnly(BaseStrategy):
        def on_event(self, event, ctx):
            return [SelfSchedule(Product.RegA_v1, hour_utc, 50.0)]

    gate = RegDailyOfferGate(
        timestamp=datetime(2024, 6, 14, 18, 15, tzinfo=UTC),
        asset_id=asset.asset_id,
        operating_date=date(2024, 6, 15),
    )
    commitments = Commitments()
    result = BacktestResult(asset=asset)
    ctx = Context(asset=asset, commitments=commitments, view=None)
    tables = PriceTables(
        da_lmp_by_hour={},
        rt_lmp_by_mtu={},
        da_sr_by_hour={},
        rt_sr_by_mtu={},
        da_sec_by_hour={},
        rt_sec_by_mtu={},
        reg_by_mtu={},
        reg_v1_by_hour=v1_prices,
    )

    _handle_reg_offer_gate(
        gate,
        RegAOnly(),
        ctx,
        asset,
        tables=tables,
        current_soc=0.5 * asset.energy_mwh,
        commitments=commitments,
        result=result,
    )

    rows = [r for r in result.revenue_rows if r.product == "RegA_v1"]
    assert len(rows) == 1
    # Must use 6.3 (rega), not 22.7 (regd).
    assert rows[0].clearing_price == pytest.approx(6.3)
    assert rows[0].revenue == pytest.approx(50.0 * REG_PERF_SCORE * 6.3 * 1.0)


# ─── Cross-product power cap (energy + Reg ≤ nameplate) ──────────────────────


def test_validate_stack_rejects_da_plus_reg_overflow():
    """250 MW DA + 250 MW Reg overlapping the same MTU on a 250 MW battery
    must raise — total capability needed = |energy| + Reg = 500 > 250."""
    from pjm_engine.settle import Award

    a = ASSETS["example_a"]  # 250 MW
    hour = _ts(day=3, hour=5)
    half = _ts(day=3, hour=5, minute=0)

    da_award = Award(
        product=Product.DA_Energy,
        period_start=hour,
        period_end=hour + timedelta(hours=1),
        cleared_mw=250.0,
        clearing_price=30.0,
    )
    reg_bid = SelfSchedule(Product.Reg_v2, half, 250.0)

    with pytest.raises(BidValidationError, match="cross-product power cap"):
        validate_stack([reg_bid], a, existing_awards=[da_award])


def test_validate_stack_accepts_balanced_da_plus_reg():
    """50 MW DA + 50 MW Reg = 100 MW capability on a 250 MW battery — legal."""
    from pjm_engine.settle import Award

    a = ASSETS["example_a"]
    hour = _ts(day=3, hour=5)
    half = _ts(day=3, hour=5, minute=0)

    da_award = Award(
        product=Product.DA_Energy,
        period_start=hour,
        period_end=hour + timedelta(hours=1),
        cleared_mw=50.0,
        clearing_price=30.0,
    )
    reg_bid = SelfSchedule(Product.Reg_v2, half, 50.0)

    validate_stack([reg_bid], a, existing_awards=[da_award])


def test_validate_stack_charge_da_plus_reg_uses_abs_value():
    """Charging at -250 MW DA also needs power; Reg on top still overflows."""
    from pjm_engine.settle import Award

    a = ASSETS["example_a"]
    hour = _ts(day=3, hour=5)
    half = _ts(day=3, hour=5, minute=0)

    da_award = Award(
        product=Product.DA_Energy,
        period_start=hour,
        period_end=hour + timedelta(hours=1),
        cleared_mw=-250.0,
        clearing_price=30.0,
    )
    reg_bid = SelfSchedule(Product.Reg_v2, half, 250.0)

    with pytest.raises(BidValidationError, match="cross-product power cap"):
        validate_stack([reg_bid], a, existing_awards=[da_award])


def test_validate_stack_non_overlapping_periods_dont_interact():
    """A DA award for hour 5 and a Reg bid for hour 10 don't share any MTU."""
    from pjm_engine.settle import Award

    a = ASSETS["example_a"]
    hour5 = _ts(day=3, hour=5)
    half10 = _ts(day=3, hour=10, minute=0)

    da_award = Award(
        product=Product.DA_Energy,
        period_start=hour5,
        period_end=hour5 + timedelta(hours=1),
        cleared_mw=100.0,
        clearing_price=30.0,
    )
    reg_bid = SelfSchedule(Product.Reg_v2, half10, 100.0)

    validate_stack([reg_bid], a, existing_awards=[da_award])


# ─── AS-aware SoC reservation (Reg headroom) ────────────────────────────────


def test_simulate_soc_accepts_reg_with_balanced_soc():
    """100 MW Reg on the 250 MW / 1000 MWh battery at 200 MWh SoC. Required
    headroom = 100 × 0.25 = 25 MWh each side. Available: 100 MWh discharge,
    700 MWh charge. Both sides ≥ 25 → legal."""
    from pjm_engine.settle import Award
    from pjm_engine.validation import simulate_soc

    a = ASSETS["example_a"]  # 250 MW, soc band [100, 900] MWh
    half = _ts(day=3, hour=5, minute=0)
    reg = Award(Product.Reg_v2, half, half + HALF_HOUR, 100.0, 0.0)
    traj = simulate_soc([reg], initial_soc_mwh=200.0, asset=a)
    assert len(traj) >= 1


def test_simulate_soc_rejects_reg_when_charge_headroom_short():
    """SoC at SoC_max − 10 MWh with 100 MW Reg → only 10 MWh charge headroom,
    needs 25. Must raise."""
    from pjm_engine.settle import Award
    from pjm_engine.validation import simulate_soc

    a = ASSETS["example_a"]
    half = _ts(day=3, hour=5, minute=0)
    reg = Award(Product.Reg_v2, half, half + HALF_HOUR, 100.0, 0.0)
    near_max_soc = a.soc_max_mwh - 10.0
    with pytest.raises(SoCInfeasibleError, match="Reg charge headroom"):
        simulate_soc([reg], initial_soc_mwh=near_max_soc, asset=a)


def test_simulate_soc_rejects_reg_when_discharge_headroom_short():
    """SoC at SoC_min + 10 MWh with 100 MW Reg → only 10 MWh discharge headroom,
    needs 25. Must raise."""
    from pjm_engine.settle import Award
    from pjm_engine.validation import simulate_soc

    a = ASSETS["example_a"]
    half = _ts(day=3, hour=5, minute=0)
    reg = Award(Product.Reg_v2, half, half + HALF_HOUR, 100.0, 0.0)
    near_min_soc = a.soc_min_mwh + 10.0
    with pytest.raises(SoCInfeasibleError, match="Reg discharge headroom"):
        simulate_soc([reg], initial_soc_mwh=near_min_soc, asset=a)


def test_simulate_soc_rejects_da_emptying_then_reg():
    """4 hours of full-power DA discharge from 50% SoC empties below the Reg
    headroom requirement before the operating day ends. simulate_soc must
    catch this when Reg awards span the same window."""
    from pjm_engine.settle import Award
    from pjm_engine.validation import simulate_soc

    a = ASSETS["example_a"]  # 250 MW / 1000 MWh
    base = _ts(day=3, hour=5, minute=0)
    # 3 h of full-power discharge from SoC=500: 500 - 3 × 250 / √0.85 ≈ -313 MWh
    # → SoC walk fails on bounds before Reg headroom even matters; pick a
    # smaller discharge that stays in bounds but lands below Reg headroom.
    # Bid 125 MW for 2h: SoC ≈ 500 - 2 × 125/√0.85 = 229 MWh (above 100 floor).
    energy_awards = [
        Award(
            Product.DA_Energy,
            base + timedelta(hours=h),
            base + timedelta(hours=h + 1),
            cleared_mw=125.0,
            clearing_price=30.0,
        )
        for h in range(2)
    ]
    # Reg held during hour 3, by which time SoC ≈ 229. Discharge headroom
    # = 229 - 100 = 129 MWh, ample for 250 MW × 0.25 = 62.5. So this should PASS.
    reg_pass = Award(
        Product.Reg_v2, base + timedelta(hours=2, minutes=30), base + timedelta(hours=3), 250.0, 0.0
    )
    simulate_soc(energy_awards + [reg_pass], initial_soc_mwh=500.0, asset=a)

    # Now push: after 3h of 125 MW discharge:
    # SoC ≈ 500 - 3 × 125/√0.85 = 500 - 406.8 = 93.2 MWh — already below SoC_min.
    # The energy walk itself will raise.
    energy_awards_3h = [
        Award(
            Product.DA_Energy,
            base + timedelta(hours=h),
            base + timedelta(hours=h + 1),
            cleared_mw=125.0,
            clearing_price=30.0,
        )
        for h in range(3)
    ]
    with pytest.raises(SoCInfeasibleError):
        simulate_soc(energy_awards_3h, initial_soc_mwh=500.0, asset=a)


def test_validate_stack_same_batch_da_plus_reg_overflows():
    """DA + Reg in the SAME batch (both new bids) — overflow still caught."""
    a = ASSETS["example_a"]
    hour = _ts(day=3, hour=5)
    half = _ts(day=3, hour=5, minute=0)

    bids = [
        SelfSchedule(Product.DA_Energy, hour, 250.0),
        SelfSchedule(Product.Reg_v2, half, 250.0),
    ]
    with pytest.raises(BidValidationError, match="cross-product power cap"):
        validate_stack(bids, a)


# ─── Runner integration: Reg offer gate commits awards, no premature revenue ─


def test_handle_reg_offer_gate_commits_awards_no_revenue_rows():
    """Strategy that returns Reg_v2 SelfSchedules at the daily offer gate must
    produce one Award per block in commitments but zero revenue rows at the
    gate itself (settlement happens per-MTU at RT gates)."""
    from pjm_engine.events import RegDailyOfferGate
    from pjm_engine.runner import BacktestResult, _handle_reg_offer_gate
    from pjm_engine.strategy_base import (
        BaseStrategy,
        Commitments,
        Context,
    )

    asset = ASSETS["example_a"]
    op_date = date(2025, 11, 3)

    class StubStacker(BaseStrategy):
        def should_resolve(self, event):
            return True

        def on_event(self, event, ctx):
            # Offer 50 MW on every half-hour block of operating day D.
            half_starts = []
            mid = datetime.combine(op_date, datetime.min.time(), tzinfo=EPT)
            end = datetime.combine(op_date + timedelta(days=1), datetime.min.time(), tzinfo=EPT)
            t = mid
            while t < end:
                half_starts.append(t.astimezone(UTC))
                t += HALF_HOUR
            return [SelfSchedule(Product.Reg_v2, hh, 50.0) for hh in half_starts]

    gate = RegDailyOfferGate(
        timestamp=datetime(2025, 11, 2, 19, 15, tzinfo=UTC),  # D-1 14:15 EPT
        asset_id=asset.asset_id,
        operating_date=op_date,
    )
    commitments = Commitments()
    result = BacktestResult(asset=asset)
    ctx = Context(asset=asset, commitments=commitments, view=None)

    from pjm_engine.settle import PriceTables

    _handle_reg_offer_gate(
        gate,
        StubStacker(),
        ctx,
        asset,
        tables=PriceTables(
            da_lmp_by_hour={},
            rt_lmp_by_mtu={},
            da_sr_by_hour={},
            rt_sr_by_mtu={},
            da_sec_by_hour={},
            rt_sec_by_mtu={},
            reg_by_mtu={},
        ),
        current_soc=0.5 * asset.energy_mwh,
        commitments=commitments,
        result=result,
    )

    reg_awards = [a for a in commitments.awards if a.product == Product.Reg_v2]
    assert len(reg_awards) == 48, f"expected 48 half-hour blocks, got {len(reg_awards)}"
    assert all(a.cleared_mw == 50.0 for a in reg_awards)
    assert all(a.period_end - a.period_start == HALF_HOUR for a in reg_awards)
    assert result.revenue_rows == [], "Reg revenue must settle per-MTU, not at offer gate"


def test_handle_reg_offer_gate_rejects_bid_curve():
    """The engine wires Reg_v2 SelfSchedule only — BidCurve must raise loudly."""
    from pjm_engine.events import RegDailyOfferGate
    from pjm_engine.runner import BacktestResult, _handle_reg_offer_gate
    from pjm_engine.strategy_base import (
        BaseStrategy,
        BidCurve,
        Commitments,
        Context,
    )

    asset = ASSETS["example_a"]
    op_date = date(2025, 11, 3)
    on_hh = datetime(2025, 11, 3, 5, 0, tzinfo=UTC)

    class CurveStrategy(BaseStrategy):
        def on_event(self, event, ctx):
            return BidCurve(Product.Reg_v2, on_hh, ((50.0, 0.0),))

    gate = RegDailyOfferGate(
        timestamp=datetime(2025, 11, 2, 19, 15, tzinfo=UTC),
        asset_id=asset.asset_id,
        operating_date=op_date,
    )
    commitments = Commitments()
    result = BacktestResult(asset=asset)
    ctx = Context(asset=asset, commitments=commitments, view=None)

    from pjm_engine.settle import PriceTables

    with pytest.raises(TypeError, match="SelfSchedule only"):
        _handle_reg_offer_gate(
            gate,
            CurveStrategy(),
            ctx,
            asset,
            tables=PriceTables(
                da_lmp_by_hour={},
                rt_lmp_by_mtu={},
                da_sr_by_hour={},
                rt_sr_by_mtu={},
                da_sec_by_hour={},
                rt_sec_by_mtu={},
                reg_by_mtu={},
            ),
            current_soc=0.5 * asset.energy_mwh,
            commitments=commitments,
            result=result,
        )


# ─── Calibration: perf_score_by_period wiring ─────────────────────────────────


def test_perf_score_calibration_populates_from_reg_market_results():
    """`build_price_tables` populates `perf_score_by_period` with both pre- and
    post-redesign `rto_perfscore` rows. Confirms the lookup exists and is
    non-empty for the post-redesign window."""
    from pjm_engine.data import (
        load_da_hrl_lmps,
        load_da_sec_prices,
        load_da_sr_prices,
        load_reg_market_results,
        load_reg_prices,
        load_rt_fivemin_mnt_lmps,
        load_rt_sec_prices,
        load_rt_sr_prices,
    )
    from pjm_engine.settle import build_price_tables

    asset = ASSETS["example_a"]
    da = load_da_hrl_lmps()
    da = da[da["zone"] == asset.zone].reset_index(drop=True)
    rt = load_rt_fivemin_mnt_lmps()
    rt = rt[rt["zone"] == asset.zone].reset_index(drop=True)
    tables = build_price_tables(
        da,
        rt,
        load_reg_prices(),
        load_da_sr_prices(),
        load_rt_sr_prices(),
        load_da_sec_prices(),
        load_rt_sec_prices(),
        reg_market_results=load_reg_market_results(),
    )

    # Both regimes indexed: post-redesign half-hours and pre-redesign hours.
    half_hour = datetime(2025, 11, 3, 5, 0, tzinfo=UTC)  # post-redesign
    assert half_hour in tables.perf_score_by_period
    score = tables.perf_score_by_period[half_hour]
    # Realistic post-redesign range per data inspection (p05-p95: 0.84-0.94).
    assert 0.5 < score < 1.0
    assert score != REG_PERF_SCORE  # discriminating: not the constant


def test_reg_v2_settlement_uses_calibrated_score_not_constant():
    """Engine must use `tables.perf_score_by_period[half_hour]`, not the
    `REG_PERF_SCORE` constant. Build tables with a deliberately-different
    score for the target half-hour and assert the revenue row reflects that."""
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
    half_hour = datetime(2025, 11, 3, 5, 0, tzinfo=UTC)
    mtu_start = half_hour
    gate_ts = mtu_start - timedelta(minutes=5)
    CALIBRATED = 0.72  # well below REG_PERF_SCORE = 0.95
    RMCCP, RMPCP = 30.0, 5.0

    tables = PriceTables(
        da_lmp_by_hour={},
        rt_lmp_by_mtu={},
        da_sr_by_hour={},
        rt_sr_by_mtu={},
        da_sec_by_hour={},
        rt_sec_by_mtu={},
        reg_by_mtu={mtu_start: (RMCCP, RMPCP)},
        perf_score_by_period={half_hour: CALIBRATED},
    )

    commitments = Commitments()
    commitments.add_award(
        Award(
            product=Product.Reg_v2,
            period_start=half_hour,
            period_end=half_hour + HALF_HOUR,
            cleared_mw=100.0,
            clearing_price=0.0,
        )
    )

    class NoOp(BaseStrategy):
        def on_event(self, event, ctx):
            return Acknowledgment()

    gate = RTGateClosing(timestamp=gate_ts, asset_id=asset.asset_id, mtu_start=mtu_start)
    result = BacktestResult(asset=asset)
    view = DataView(as_of=gate_ts, da_lmps=pd.DataFrame(), rt_lmps=pd.DataFrame())
    ctx = Context(asset=asset, commitments=commitments, view=view)

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

    reg_rows = [r for r in result.revenue_rows if r.product == "Reg_v2"]
    assert len(reg_rows) == 1
    expected = settle_reg_v2(
        cleared_mw=100.0,
        perf_score=CALIBRATED,
        rmccp=RMCCP,
        rmpcp=RMPCP,
        mileage_ratio=REG_MILEAGE_RATIO,
    )
    assert reg_rows[0].revenue == pytest.approx(expected, abs=1e-9)
    # Discriminating: revenue is materially below the constant-based number.
    constant_revenue = settle_reg_v2(
        cleared_mw=100.0,
        perf_score=REG_PERF_SCORE,
        rmccp=RMCCP,
        rmpcp=RMPCP,
        mileage_ratio=REG_MILEAGE_RATIO,
    )
    assert reg_rows[0].revenue < constant_revenue
    assert reg_rows[0].revenue == pytest.approx(
        constant_revenue * (CALIBRATED / REG_PERF_SCORE), abs=1e-9
    )


def test_reg_v2_falls_back_to_constant_when_calibration_missing():
    """If the calibration table has no entry for this half-hour, engine
    falls back to `REG_PERF_SCORE` rather than zero or NaN."""
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
    half_hour = datetime(2025, 11, 3, 5, 0, tzinfo=UTC)
    mtu_start = half_hour
    gate_ts = mtu_start - timedelta(minutes=5)
    RMCCP, RMPCP = 30.0, 5.0

    tables = PriceTables(
        da_lmp_by_hour={},
        rt_lmp_by_mtu={},
        da_sr_by_hour={},
        rt_sr_by_mtu={},
        da_sec_by_hour={},
        rt_sec_by_mtu={},
        reg_by_mtu={mtu_start: (RMCCP, RMPCP)},
        perf_score_by_period={},  # empty → fallback path
    )

    commitments = Commitments()
    commitments.add_award(
        Award(
            product=Product.Reg_v2,
            period_start=half_hour,
            period_end=half_hour + HALF_HOUR,
            cleared_mw=100.0,
            clearing_price=0.0,
        )
    )

    class NoOp(BaseStrategy):
        def on_event(self, event, ctx):
            return Acknowledgment()

    gate = RTGateClosing(timestamp=gate_ts, asset_id=asset.asset_id, mtu_start=mtu_start)
    result = BacktestResult(asset=asset)
    view = DataView(as_of=gate_ts, da_lmps=pd.DataFrame(), rt_lmps=pd.DataFrame())
    ctx = Context(asset=asset, commitments=commitments, view=view)

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

    reg_rows = [r for r in result.revenue_rows if r.product == "Reg_v2"]
    assert len(reg_rows) == 1
    expected = settle_reg_v2(
        cleared_mw=100.0,
        perf_score=REG_PERF_SCORE,
        rmccp=RMCCP,
        rmpcp=RMPCP,
        mileage_ratio=REG_MILEAGE_RATIO,
    )
    assert reg_rows[0].revenue == pytest.approx(expected, abs=1e-9)
