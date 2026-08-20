"""BidCurve clearing + validation tests.

Hand-computed cases per design.md §"Gates and clearing":
  cleared_MW = max{ q : bid_price(q) ≤ clearing_price }   (discharge)
             = max{ q : bid_price(q) ≥ clearing_price }   (charge)
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from pjm_engine.battery import ASSETS
from pjm_engine.errors import BidValidationError
from pjm_engine.markets import clear_bid_curve, validate_bid_curve
from pjm_engine.strategy_base import BidCurve, Product
from pjm_engine.validation import validate_bid, validate_stack

UTC = ZoneInfo("UTC")
EPT = ZoneInfo("America/New_York")


# ─── clear_bid_curve: discharge ──────────────────────────────────────────────


def _hour():
    return datetime(2025, 11, 15, 18, 0, tzinfo=UTC)


def test_discharge_curve_clears_in_merit_tiers():
    """((50,$40), (75,$80), (100,$150)) at RT $90 → 75 MW (tier 2 in merit, tier 3 not)."""
    curve = BidCurve(
        product=Product.RT_Energy,
        period_start=_hour(),
        tiers=((50.0, 40.0), (75.0, 80.0), (100.0, 150.0)),
    )
    assert clear_bid_curve(curve, clearing_price=90.0) == 75.0


def test_discharge_curve_clears_zero_when_price_below_first_tier():
    curve = BidCurve(
        product=Product.RT_Energy,
        period_start=_hour(),
        tiers=((50.0, 40.0), (100.0, 150.0)),
    )
    assert clear_bid_curve(curve, clearing_price=20.0) == 0.0


def test_discharge_curve_clears_full_when_price_above_all_tiers():
    curve = BidCurve(
        product=Product.RT_Energy,
        period_start=_hour(),
        tiers=((50.0, 40.0), (100.0, 150.0)),
    )
    assert clear_bid_curve(curve, clearing_price=200.0) == 100.0


def test_discharge_curve_at_exact_tier_price_clears_that_tier():
    """Boundary: clearing price == tier price means tier is in merit."""
    curve = BidCurve(
        product=Product.DA_Energy,
        period_start=_hour(),
        tiers=((50.0, 40.0), (100.0, 80.0)),
    )
    assert clear_bid_curve(curve, clearing_price=80.0) == 100.0


# ─── clear_bid_curve: charge (mirror) ────────────────────────────────────────


def test_charge_curve_clears_when_price_below_threshold():
    """Charge ((-50,$30), (-75,$20), (-100,$10)) at RT $25 → -50 MW (only tier 1 in merit)."""
    curve = BidCurve(
        product=Product.RT_Energy,
        period_start=_hour(),
        tiers=((-50.0, 30.0), (-75.0, 20.0), (-100.0, 10.0)),
    )
    assert clear_bid_curve(curve, clearing_price=25.0) == -50.0


def test_charge_curve_clears_zero_when_price_above_first_tier():
    curve = BidCurve(
        product=Product.RT_Energy,
        period_start=_hour(),
        tiers=((-50.0, 30.0), (-100.0, 10.0)),
    )
    assert clear_bid_curve(curve, clearing_price=50.0) == 0.0


def test_charge_curve_clears_full_at_low_price():
    curve = BidCurve(
        product=Product.RT_Energy,
        period_start=_hour(),
        tiers=((-50.0, 30.0), (-100.0, 10.0)),
    )
    assert clear_bid_curve(curve, clearing_price=5.0) == -100.0


# ─── validate_bid_curve ──────────────────────────────────────────────────────


def test_validate_rejects_empty_curve():
    curve = BidCurve(product=Product.RT_Energy, period_start=_hour(), tiers=())
    with pytest.raises(BidValidationError):
        validate_bid_curve(curve, ASSETS["example_a"])


def test_validate_rejects_mixed_signs():
    curve = BidCurve(
        product=Product.RT_Energy,
        period_start=_hour(),
        tiers=((50.0, 40.0), (-25.0, 60.0)),
    )
    with pytest.raises(BidValidationError, match="disagrees"):
        validate_bid_curve(curve, ASSETS["example_a"])


def test_validate_rejects_non_increasing_mw():
    curve = BidCurve(
        product=Product.RT_Energy,
        period_start=_hour(),
        tiers=((75.0, 40.0), (50.0, 80.0)),
    )
    with pytest.raises(BidValidationError, match="increasing in"):
        validate_bid_curve(curve, ASSETS["example_a"])


def test_validate_rejects_discharge_price_decreasing():
    curve = BidCurve(
        product=Product.RT_Energy,
        period_start=_hour(),
        tiers=((50.0, 80.0), (75.0, 40.0)),
    )
    with pytest.raises(BidValidationError, match="non-decreasing in price"):
        validate_bid_curve(curve, ASSETS["example_a"])


def test_validate_rejects_charge_price_increasing():
    curve = BidCurve(
        product=Product.RT_Energy,
        period_start=_hour(),
        tiers=((-50.0, 10.0), (-75.0, 30.0)),
    )
    with pytest.raises(BidValidationError, match="non-increasing in price"):
        validate_bid_curve(curve, ASSETS["example_a"])


def test_validate_rejects_mw_above_nameplate():
    curve = BidCurve(
        product=Product.RT_Energy,
        period_start=_hour(),
        tiers=((50.0, 40.0), (300.0, 80.0)),  # example_a is 250 MW
    )
    with pytest.raises(BidValidationError, match="nameplate"):
        validate_bid_curve(curve, ASSETS["example_a"])


def test_validate_rejects_da_curve_off_hour_boundary():
    curve = BidCurve(
        product=Product.DA_Energy,
        period_start=_hour() + timedelta(minutes=15),
        tiers=((50.0, 40.0),),
    )
    with pytest.raises(BidValidationError, match="top of hour"):
        validate_bid_curve(curve, ASSETS["example_a"])


def test_validate_rejects_rt_curve_off_5min_boundary():
    curve = BidCurve(
        product=Product.RT_Energy,
        period_start=_hour() + timedelta(minutes=2),
        tiers=((50.0, 40.0),),
    )
    with pytest.raises(BidValidationError, match="5-min boundary"):
        validate_bid_curve(curve, ASSETS["example_a"])


def test_validate_accepts_well_formed_discharge_curve():
    curve = BidCurve(
        product=Product.RT_Energy,
        period_start=_hour(),
        tiers=((50.0, 40.0), (75.0, 80.0), (100.0, 150.0)),
    )
    validate_bid_curve(curve, ASSETS["example_a"])  # no raise


# ─── validate_bid + validate_stack work for mixed types ──────────────────────


def test_validate_bid_dispatches_to_curve_validator():
    """validate_bid(BidCurve) must run the curve-specific checks."""
    bad_curve = BidCurve(
        product=Product.RT_Energy,
        period_start=_hour(),
        tiers=((75.0, 40.0), (50.0, 80.0)),
    )
    with pytest.raises(BidValidationError):
        validate_bid(bad_curve, ASSETS["example_a"])


def test_validate_stack_uses_curve_max_mw_for_total():
    """Two RT discharge curves on the same MTU must sum (max-MW) ≤ nameplate."""
    asset = ASSETS["example_a"]  # 250 MW
    c1 = BidCurve(Product.RT_Energy, _hour(), tiers=((150.0, 40.0),))
    c2 = BidCurve(Product.RT_Energy, _hour(), tiers=((150.0, 50.0),))
    with pytest.raises(BidValidationError, match="exceeds nameplate"):
        validate_stack([c1, c2], asset)


def test_validate_stack_rejects_curve_charge_plus_curve_discharge_in_same_period():
    asset = ASSETS["example_a"]
    c_dis = BidCurve(Product.RT_Energy, _hour(), tiers=((50.0, 40.0),))
    c_chg = BidCurve(Product.RT_Energy, _hour(), tiers=((-50.0, 20.0),))
    with pytest.raises(BidValidationError, match="charge\\+discharge"):
        validate_stack([c_dis, c_chg], asset)


# ─── e2e: a bid-curve strategy clears against historical RT LMP ──────────────


def test_runner_clears_bid_curve_against_historical_lmp():
    """Submit a BidCurve at the RT gate; engine must clear it at the period's RT LMP."""
    from pjm_engine.events import Event, RTGateClosing
    from pjm_engine.runner import run_backtest
    from pjm_engine.strategy_base import (
        Acknowledgment,
        BaseStrategy,
        BidCurve,
        Context,
        Product,
    )

    class FullDischargeAt40(BaseStrategy):
        """Discharge 100 MW whenever RT LMP ≥ $40, else 0."""

        def should_resolve(self, event: Event) -> bool:
            return isinstance(event, RTGateClosing)

        def on_event(self, event: Event, ctx: Context):
            if not isinstance(event, RTGateClosing):
                return Acknowledgment()
            return BidCurve(
                product=Product.RT_Energy,
                period_start=event.mtu_start,
                tiers=((100.0, 40.0),),
            )

    result = run_backtest(
        strategy=FullDischargeAt40(),
        asset=ASSETS["example_a"],
        start_date=date(2025, 11, 16),
        end_date=date(2025, 11, 17),
        initial_soc_pct=0.5,
    )
    products = {r.product for r in result.revenue_rows}
    assert "RT_Energy" in products, "curve never cleared"
    # Every cleared RT row should be exactly +100 MW or 0 (zero rows are dropped).
    rt_rows = [r for r in result.revenue_rows if r.product == "RT_Energy"]
    assert all(r.cleared_mw == 100.0 for r in rt_rows), (
        f"expected 100 MW per cleared MTU, got {set(r.cleared_mw for r in rt_rows)}"
    )
    # And the corresponding RT LMP must be ≥ $40 for every cleared MTU.
    assert all(r.clearing_price >= 40.0 - 1e-9 for r in rt_rows), (
        "curve cleared below its $40 threshold — clearing logic broken"
    )
