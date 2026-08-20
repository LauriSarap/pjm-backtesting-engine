"""Sec_RTO (30-Minute Reserve) settlement, validation, and runner wiring.

Shape mirrors SR_RTO — hourly DA capacity payment, per-MTU RT
adjustments, 30-min sustain rule. PJM service strings differ ("Thirty Minutes
Reserve" / "30MIN" vs SR's "Synchronized Reserve" / "SR"). Often clears at $0
in the data, so revenue tests assert formula correctness
not magnitude.
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
DT = 1.0 / 12.0


def _ts(*, year=2025, month=11, day=3, hour=5, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def da_sec_prices() -> pd.DataFrame:
    from pjm_engine.data import load_da_sec_prices

    return load_da_sec_prices()


@pytest.fixture(scope="module")
def rt_sec_prices() -> pd.DataFrame:
    from pjm_engine.data import load_rt_sec_prices

    return load_rt_sec_prices()


# ─── Loaders pull only the right service ──────────────────────────────────────


def test_da_sec_prices_loaded_with_published_at(da_sec_prices):
    assert len(da_sec_prices) > 0
    assert "mcp" in da_sec_prices.columns
    assert "published_at" in da_sec_prices.columns


def test_rt_sec_prices_loaded_5min_grain(rt_sec_prices):
    assert len(rt_sec_prices) > 0
    # 5-min cadence: differences between consecutive timestamps should mostly
    # be 5 min (modulo gaps).
    sample = rt_sec_prices["datetime_beginning_utc"].head(20)
    diffs = sample.diff().dropna()
    assert (diffs == pd.Timedelta(minutes=5)).any()


# ─── Validator: Sec_RTO bid-level rules ──────────────────────────────────────


def test_sec_rto_bid_must_be_hour_aligned():
    a = ASSETS["example_a"]
    bad = datetime(2025, 11, 3, 5, 30, tzinfo=UTC)
    with pytest.raises(BidValidationError, match="top of hour"):
        validate_bid(SelfSchedule(Product.Sec_RTO, bad, 50.0), a)


def test_sec_rto_bid_must_be_positive():
    a = ASSETS["example_a"]
    on_hour = _ts()
    with pytest.raises(BidValidationError, match="positive upward capacity"):
        validate_bid(SelfSchedule(Product.Sec_RTO, on_hour, -50.0), a)


def test_sec_rto_bid_respects_nameplate():
    a = ASSETS["example_a"]  # 250 MW
    on_hour = _ts()
    with pytest.raises(BidValidationError, match="exceeds .* nameplate"):
        validate_bid(SelfSchedule(Product.Sec_RTO, on_hour, 300.0), a)


def test_sec_rto_at_full_nameplate_legal():
    a = ASSETS["example_a"]
    on_hour = _ts()
    validate_bid(SelfSchedule(Product.Sec_RTO, on_hour, a.power_mw), a)


# ─── Sub-zone match: *_RTO products require asset.sub_zone == "RTO" ──────────


def test_sec_rto_rejected_for_mad_zone_asset():
    """Sec_RTO bid from a MAD-registered asset must raise SubZoneMismatchError.
    Defensive scaffolding — all 5 Charged assets are RTO today, but the
    rule guards against accidental cross-zone bidding when MAD assets land."""
    from dataclasses import replace as _replace

    from pjm_engine.errors import SubZoneMismatchError

    a_rto = ASSETS["example_a"]
    a_mad = _replace(a_rto, asset_id="hypothetical_mad", sub_zone="MAD")
    on_hour = _ts()
    with pytest.raises(SubZoneMismatchError, match="RTO sub-zone"):
        validate_bid(SelfSchedule(Product.Sec_RTO, on_hour, 50.0), a_mad)


def test_sr_rto_rejected_for_mad_zone_asset():
    from dataclasses import replace as _replace

    from pjm_engine.errors import SubZoneMismatchError

    a_rto = ASSETS["example_a"]
    a_mad = _replace(a_rto, asset_id="hypothetical_mad", sub_zone="MAD")
    on_hour = _ts()
    with pytest.raises(SubZoneMismatchError, match="RTO sub-zone"):
        validate_bid(SelfSchedule(Product.SR_RTO, on_hour, 50.0), a_mad)


def test_sr_sec_accepted_for_rto_asset():
    """All 5 Charged assets are RTO — happy path locks in current behavior."""
    a = ASSETS["example_a"]
    on_hour = _ts()
    validate_bid(SelfSchedule(Product.SR_RTO, on_hour, 50.0), a)
    validate_bid(SelfSchedule(Product.Sec_RTO, on_hour, 50.0), a)


# ─── Cross-product cap: SR + Sec stack additively ────────────────────────────


def test_validate_stack_sr_plus_sec_overflow():
    """250 MW SR + 125 MW Sec on a 250 MW battery — combined upward = 375."""
    from pjm_engine.settle import Award

    a = ASSETS["example_a"]
    hour = _ts()
    sr = Award(Product.SR_RTO, hour, hour + timedelta(hours=1), 250.0, 8.0)
    sec_bid = SelfSchedule(Product.Sec_RTO, hour, 125.0)
    with pytest.raises(BidValidationError, match="cross-product power cap"):
        validate_stack([sec_bid], a, existing_awards=[sr])


# ─── AS-aware SoC: Sec sustain rule ───────────────────────────────────────────


def test_simulate_soc_rejects_sec_when_sustain_short():
    """50 MW Sec needs 25 MWh discharge headroom; only 10 MWh available → raise."""
    from pjm_engine.settle import Award
    from pjm_engine.validation import simulate_soc

    a = ASSETS["example_a"]
    hour = _ts()
    sec = Award(Product.Sec_RTO, hour, hour + timedelta(hours=1), 50.0, 0.0)
    near_min = a.soc_min_mwh + 10.0
    with pytest.raises(SoCInfeasibleError, match="Sec sustain"):
        simulate_soc([sec], initial_soc_mwh=near_min, asset=a)


# ─── Runner: Sec_RTO at DA gate emits revenue rows ────────────────────────────


def test_da_gate_commits_sec_award_and_emits_revenue():
    """Strategy returning Sec_RTO at the DA gate produces an award + DA revenue
    row settled at the DA Sec MCP for that hour. Sec MCP is often $0 — assert
    the formula matches the cached value (which may be 0)."""
    from pjm_engine.data import (
        load_da_hrl_lmps,
        load_da_sec_prices,
        load_da_sr_prices,
        load_reg_prices,
        load_rt_fivemin_mnt_lmps,
        load_rt_sec_prices,
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
    hour = _ts()

    da_lmps = load_da_hrl_lmps()
    da_lmps_zone = da_lmps[da_lmps["zone"] == asset.zone].reset_index(drop=True)
    rt_lmps_zone = (
        load_rt_fivemin_mnt_lmps().pipe(lambda d: d[d["zone"] == asset.zone]).reset_index(drop=True)
    )

    class StubSecStrategy(BaseStrategy):
        def on_event(self, event, ctx):
            return [SelfSchedule(Product.Sec_RTO, hour, 50.0)]

    gate = DAGateClosing(
        timestamp=datetime(2025, 11, 2, 16, 0, tzinfo=UTC),
        asset_id=asset.asset_id,
        operating_date=op_date,
    )
    commitments = Commitments()
    result = BacktestResult(asset=asset)
    view = DataView(as_of=gate.timestamp, da_lmps=da_lmps_zone)
    ctx = Context(asset=asset, commitments=commitments, view=view)

    da_sec = load_da_sec_prices()
    tables = build_price_tables(
        da_lmps_zone,
        rt_lmps_zone,
        load_reg_prices(),
        load_da_sr_prices(),
        load_rt_sr_prices(),
        da_sec,
        load_rt_sec_prices(),
    )

    _handle_da_gate(
        gate,
        StubSecStrategy(),
        ctx,
        asset,
        tables,
        current_soc=0.5 * asset.energy_mwh,
        commitments=commitments,
        result=result,
    )

    sec_awards = [a for a in commitments.awards if a.product == Product.Sec_RTO]
    assert len(sec_awards) == 1
    assert sec_awards[0].cleared_mw == 50.0
    assert sec_awards[0].period_end - sec_awards[0].period_start == timedelta(hours=1)

    expected_price = float(da_sec[da_sec["datetime_beginning_utc"] == hour]["mcp"].iloc[0])
    sec_rows = [r for r in result.revenue_rows if r.product == "Sec_RTO"]
    assert len(sec_rows) == 1
    assert sec_rows[0].clearing_price == pytest.approx(expected_price, abs=1e-9)
    assert sec_rows[0].revenue == pytest.approx(50.0 * expected_price, abs=1e-9)
