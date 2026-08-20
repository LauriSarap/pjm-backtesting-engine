"""SR event delivery, shortfall, and clawback.

Covers:
  1. Loader: dedups RTO+sub-zone pairs, parses EPT→UTC, derives duration.
  2. Pure delivery math: full delivery vs proportional shortfall.
  3. Runner integration: short event with adequate SoC → no charges.
  4. Runner integration: long event exceeds 30-min sustain → shortfall + clawback.
  5. Clawback caps at accrued (no clawback on first event before any SR credits).
  6. Sub-zone events still apply to RTO assets (defensive — sub_zones field is
     informational; RTO obligation comes from the RTO row in the source data).
  7. M28 §6.2.2: shortfall uses RT SRMCP per 5-min MTU, NOT DA SRMCP × hours.
  8. M28 §6.3.3: clawback uses RetroactivePenaltyMW × RT_SRMCP / 12, NOT a
     refund of prior-credit revenue rows.
  9. M11 §4.5.2: events <10 min skip clawback (shortfall still applies).
 10. `_clear_to_award` distinguishes explicit-zero from no-bid.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from pjm_engine.battery import ASSETS
from pjm_engine.events import SREventCalled
from pjm_engine.markets.sr_events import (
    SR_CLAWBACK_DAYS,
    settle_sr_shortfall_per_mtu,
    simulate_sr_delivery,
)
from pjm_engine.runner import (
    BacktestResult,
    _build_events,
    _clear_to_award,
    _handle_sr_event_called,
)
from pjm_engine.settle import Award, PriceTables, RevenueRow
from pjm_engine.strategy_base import BidCurve, Commitments, Product, SelfSchedule

UTC = ZoneInfo("UTC")


# ─── 1. Loader ────────────────────────────────────────────────────────────────


def test_load_sr_events_dedups_rto_subzone_pairs():
    """Source CSVs have two rows per event when MAD sub-zone is also active.
    Loader must collapse to one row per (event_start, event_end) tuple."""
    from pjm_engine.data import load_sr_events

    df = load_sr_events(refresh=True)
    assert len(df) > 0
    # No duplicate (start, end) tuples.
    dup = df.duplicated(subset=["event_start_utc", "event_end_utc"]).sum()
    assert dup == 0


def test_load_sr_events_parses_ept_to_utc():
    from pjm_engine.data import load_sr_events

    df = load_sr_events()
    assert df["event_start_utc"].dt.tz is not None
    assert str(df["event_start_utc"].dt.tz) == "UTC"
    # Duration is positive on every row.
    assert (df["duration_minutes"] > 0).all()


# ─── 2. Pure delivery math ────────────────────────────────────────────────────


def test_simulate_sr_delivery_full():
    """Asset has plenty of SoC headroom → delivers full SR commitment."""
    r = simulate_sr_delivery(
        sr_committed_mw=100.0,
        soc_at_event_mwh=800.0,
        soc_min_mwh=160.0,
        event_duration_hours=10.0 / 60.0,  # 10-min event
        discharge_efficiency=0.92,
    )
    assert r.delivered_mw == pytest.approx(100.0)
    assert r.shortfall_mw == 0.0


def test_simulate_sr_delivery_partial():
    """Asset has only 5 MWh of headroom → can deliver less than committed."""
    r = simulate_sr_delivery(
        sr_committed_mw=100.0,
        soc_at_event_mwh=165.0,
        soc_min_mwh=160.0,
        event_duration_hours=30.0 / 60.0,  # 30-min event
        discharge_efficiency=0.92,
    )
    # available = 5 MWh; required = 100 × 0.5 / 0.92 = 54.3 MWh
    # delivered = 5 × 0.92 / 0.5 = 9.2 MW
    assert r.delivered_mw == pytest.approx(9.2, rel=1e-3)
    assert r.shortfall_mw == pytest.approx(90.8, rel=1e-3)


def test_simulate_sr_delivery_zero_when_at_floor():
    r = simulate_sr_delivery(
        sr_committed_mw=100.0,
        soc_at_event_mwh=160.0,
        soc_min_mwh=160.0,
        event_duration_hours=0.5,
        discharge_efficiency=0.92,
    )
    assert r.delivered_mw == 0.0
    assert r.shortfall_mw == pytest.approx(100.0)


def test_settle_sr_shortfall_per_mtu_sign_and_scale():
    # 50 MW shortfall × $20/MWh × 3 MTUs / 12 = $250 (debit → negative)
    assert settle_sr_shortfall_per_mtu(shortfall_mw=50.0, rt_srmcps=[20.0] * 3) == -250.0
    # No shortfall → no charge.
    assert settle_sr_shortfall_per_mtu(shortfall_mw=0.0, rt_srmcps=[20.0] * 3) == 0.0


# ─── 3. Runner integration: full delivery ────────────────────────────────────


def _empty_tables(
    srmcp_at_hour: dict,
    rt_sr_by_mtu: dict | None = None,
) -> PriceTables:
    """Minimal PriceTables for SR-event handler tests.

    Post-M28-fix `_handle_sr_event_called` reads from `rt_sr_by_mtu` (per-MTU
    RT SRMCP) for both shortfall and clawback. `da_sr_by_hour` is retained
    for back-compat callers but is no longer consulted by the SR-event path.
    """
    return PriceTables(
        da_lmp_by_hour={},
        rt_lmp_by_mtu={},
        da_sr_by_hour=srmcp_at_hour,
        rt_sr_by_mtu=rt_sr_by_mtu or {},
        da_sec_by_hour={},
        rt_sec_by_mtu={},
        reg_by_mtu={},
    )


def _rt_sr_per_mtu(start: datetime, end: datetime, price: float) -> dict:
    """Build `{mtu_start: price}` for every 5-min MTU in [start, end)."""
    out: dict = {}
    t = start
    while t < end:
        out[t] = price
        t += timedelta(minutes=5)
    return out


def _make_sr_event(start_utc: datetime, duration_min: int) -> SREventCalled:
    return SREventCalled(
        timestamp=start_utc,
        asset_id="example_a",
        event_end=start_utc + timedelta(minutes=duration_min),
        sub_zones="",
    )


def test_handler_no_shortfall_when_soc_adequate():
    asset = ASSETS["example_a"]
    hour = datetime(2025, 11, 15, 14, 0, tzinfo=UTC)
    event = _make_sr_event(hour + timedelta(minutes=20), duration_min=10)
    commits = Commitments()
    commits.add_award(
        Award(
            product=Product.SR_RTO,
            period_start=hour,
            period_end=hour + timedelta(hours=1),
            cleared_mw=100.0,
            clearing_price=20.0,
        )
    )
    result = BacktestResult(asset=asset)
    _handle_sr_event_called(
        event=event,
        asset=asset,
        tables=_empty_tables({hour: 20.0}),
        current_soc=800.0,  # 640 MWh above floor — way more than needed
        commitments=commits,
        result=result,
    )
    assert result.revenue_rows == []  # no shortfall, no clawback


def test_handler_no_event_when_no_sr_committed():
    asset = ASSETS["example_a"]
    hour = datetime(2025, 11, 15, 14, 0, tzinfo=UTC)
    event = _make_sr_event(hour + timedelta(minutes=20), duration_min=10)
    result = BacktestResult(asset=asset)
    _handle_sr_event_called(
        event=event,
        asset=asset,
        tables=_empty_tables({hour: 20.0}),
        current_soc=800.0,
        commitments=Commitments(),
        result=result,
    )
    assert result.revenue_rows == []


# ─── 4. Runner integration: shortfall + clawback ─────────────────────────────


def test_handler_emits_shortfall_and_clawback_when_soc_insufficient():
    asset = ASSETS["example_a"]
    hour = datetime(2025, 11, 15, 14, 0, tzinfo=UTC)
    event = _make_sr_event(hour + timedelta(minutes=20), duration_min=120)
    commits = Commitments()
    # Seed prior SR commitments for every operating hour in the 30-day lookback.
    # The new clawback path reads `commitments.sr_rto_cleared_mw_at(parent_hour)`,
    # not revenue rows, so we must populate the ledger directly.
    for d_back in range(1, 31):
        for h in range(24):
            ph = (hour - timedelta(days=d_back)).replace(hour=h, minute=0)
            commits.add_award(
                Award(
                    product=Product.SR_RTO,
                    period_start=ph,
                    period_end=ph + timedelta(hours=1),
                    cleared_mw=100.0,
                    clearing_price=12.5,
                )
            )
    # Add the event-hour SR commitment (the at-risk position).
    commits.add_award(
        Award(
            product=Product.SR_RTO,
            period_start=hour,
            period_end=hour + timedelta(hours=1),
            cleared_mw=100.0,
            clearing_price=25.0,
        )
    )
    result = BacktestResult(asset=asset)

    # Per-MTU RT SRMCP across both event window and lookback: flat $12 / MWh.
    rt_sr = _rt_sr_per_mtu(
        event.timestamp - timedelta(days=SR_CLAWBACK_DAYS),
        event.event_end,
        12.0,
    )
    n_pre = len(result.revenue_rows)
    _handle_sr_event_called(
        event=event,
        asset=asset,
        tables=_empty_tables({hour: 25.0}, rt_sr_by_mtu=rt_sr),
        current_soc=110.0,  # only 10 MWh above the 100 MWh floor
        commitments=commits,
        result=result,
    )

    new_rows = result.revenue_rows[n_pre:]
    products = [r.product for r in new_rows]
    assert "SR_RTO_shortfall" in products
    assert "SR_RTO_clawback" in products

    shortfall = next(r for r in new_rows if r.product == "SR_RTO_shortfall")
    clawback = next(r for r in new_rows if r.product == "SR_RTO_clawback")

    # Shortfall is large (event is 120 min, headroom is 10 MWh → tiny delivery).
    assert shortfall.revenue < 0
    assert shortfall.cleared_mw > 90.0  # almost the whole 100 MW

    assert clawback.revenue < 0
    assert clawback.period_start_utc == event.timestamp - timedelta(days=SR_CLAWBACK_DAYS)
    assert clawback.period_end_utc == event.timestamp


def test_clawback_is_zero_with_no_prior_credits():
    asset = ASSETS["example_a"]
    hour = datetime(2025, 11, 15, 14, 0, tzinfo=UTC)
    event = _make_sr_event(hour + timedelta(minutes=20), duration_min=120)
    commits = Commitments()
    commits.add_award(
        Award(
            product=Product.SR_RTO,
            period_start=hour,
            period_end=hour + timedelta(hours=1),
            cleared_mw=400.0,
            clearing_price=25.0,
        )
    )
    result = BacktestResult(asset=asset)
    rt_sr = _rt_sr_per_mtu(event.timestamp, event.event_end, 12.0)
    _handle_sr_event_called(
        event=event,
        asset=asset,
        tables=_empty_tables({hour: 25.0}, rt_sr_by_mtu=rt_sr),
        current_soc=170.0,
        commitments=commits,
        result=result,
    )
    products = [r.product for r in result.revenue_rows]
    # Shortfall yes (asset can't deliver), but clawback row absent (no prior
    # SR commitments in the 30-day lookback ledger).
    assert "SR_RTO_shortfall" in products
    assert "SR_RTO_clawback" not in products


# ─── 5. Scheduler integration ────────────────────────────────────────────────


def test_build_events_schedules_sr_events_in_window():
    asset = ASSETS["example_a"]
    sr_events = pd.DataFrame(
        {
            "event_start_utc": [
                pd.Timestamp("2025-10-15 16:52", tz="UTC"),
                pd.Timestamp("2024-09-01 12:00", tz="UTC"),  # outside window
            ],
            "event_end_utc": [
                pd.Timestamp("2025-10-15 16:57", tz="UTC"),
                pd.Timestamp("2024-09-01 12:30", tz="UTC"),
            ],
            "sub_zones": ["", "MidAtlantic-Dominion (MAD)"],
            "duration_minutes": [5.0, 30.0],
            "percent_deployed": [100, 100],
            "published_at": [
                pd.Timestamp("2025-10-15 17:02", tz="UTC"),
                pd.Timestamp("2024-09-01 12:35", tz="UTC"),
            ],
        }
    )
    sched = _build_events(
        asset,
        date(2025, 10, 15),
        date(2025, 10, 15),
        sr_events=sr_events,
    )
    sr_in_sched = [e for *_, e in sched._heap if isinstance(e, SREventCalled)]
    assert len(sr_in_sched) == 1
    assert sr_in_sched[0].timestamp == datetime(2025, 10, 15, 16, 52, tzinfo=UTC)


# ─── 6. M28 §6.2.2: shortfall uses RT SRMCP per MTU ──────────────────────────


def test_sr_shortfall_uses_rt_srmcp_per_mtu_not_da():
    """Shortfall = Σ shortfall_MW × RT_SRMCP[mtu] × (1/12), NOT DA × event_hours."""
    asset = ASSETS["example_a"]
    hour = datetime(2026, 2, 15, 14, 0, tzinfo=UTC)
    event = SREventCalled(
        timestamp=hour,
        asset_id=asset.asset_id,
        event_end=hour + timedelta(minutes=30),
        sub_zones="",
    )
    commits = Commitments()
    commits.add_award(
        Award(
            product=Product.SR_RTO,
            period_start=hour,
            period_end=hour + timedelta(hours=1),
            cleared_mw=5.0,
            clearing_price=50.0,
        )
    )
    # Six 5-min RT SRMCPs, each different — and DA is set to a wholly different
    # value so the test discriminates which feed got read.
    rt_sr = {
        hour + timedelta(minutes=0): 100.0,
        hour + timedelta(minutes=5): 200.0,
        hour + timedelta(minutes=10): 300.0,
        hour + timedelta(minutes=15): 400.0,
        hour + timedelta(minutes=20): 500.0,
        hour + timedelta(minutes=25): 600.0,
    }
    DA_DECOY = 50.0  # different from any RT price; appearing here would be a bug

    result = BacktestResult(asset=asset)
    _handle_sr_event_called(
        event=event,
        asset=asset,
        tables=_empty_tables({hour: DA_DECOY}, rt_sr_by_mtu=rt_sr),
        current_soc=asset.soc_min_mwh,  # zero headroom → 100% shortfall
        commitments=commits,
        result=result,
    )

    sf = [r for r in result.revenue_rows if r.product == "SR_RTO_shortfall"]
    assert len(sf) == 1
    # 5 MW × (100+200+300+400+500+600) × (1/12) = 5 × 2100 / 12 = 875.0
    assert sf[0].revenue == pytest.approx(-875.0)
    # The DA decoy price MUST NOT appear in any computation. If the code had
    # used DA × event_hours, the magnitude would be 5 × 50 × 0.5 = -125.0,
    # and 875 would be unreachable.
    assert sf[0].revenue != pytest.approx(-125.0)
    # The clearing_price stamp on the row averages the RT prices, not DA.
    assert sf[0].clearing_price == pytest.approx(2100.0 / 6)
    assert sf[0].clearing_price != pytest.approx(DA_DECOY)


# ─── 7. M28 §6.3.3: clawback uses RT SRMCP × prior MW ────────────────────────


def test_sr_clawback_uses_rt_srmcp_per_mtu():
    """Clawback = Σ prior_SR_MW[mtu] × RT_SRMCP[mtu] × (1/12), NOT a refund of
    our own prior-credit revenue rows."""
    asset = ASSETS["example_a"]
    hour = datetime(2026, 2, 15, 14, 0, tzinfo=UTC)
    # Long event so clawback fires (>10 min).
    event = SREventCalled(
        timestamp=hour,
        asset_id=asset.asset_id,
        event_end=hour + timedelta(minutes=60),
        sub_zones="",
    )
    commits = Commitments()
    # At-risk position for the event hour itself (drives shortfall path).
    commits.add_award(
        Award(
            product=Product.SR_RTO,
            period_start=hour,
            period_end=hour + timedelta(hours=1),
            cleared_mw=400.0,
            clearing_price=25.0,
        )
    )
    # Seed three prior hours of SR awards, each 10 MW. Build RT SR prices so
    # only those three hours have data inside the lookback (other MTUs have
    # no commitment OR no price → contribute 0).
    rt_sr: dict = {}
    prior_hours = [
        (hour - timedelta(days=2), 60.0, 10.0),  # 60 $/MWh, 10 MW
        (hour - timedelta(days=10), 30.0, 10.0),
        (hour - timedelta(days=20), 90.0, 10.0),
    ]
    for ph, _price, mw in prior_hours:
        commits.add_award(
            Award(
                product=Product.SR_RTO,
                period_start=ph,
                period_end=ph + timedelta(hours=1),
                cleared_mw=mw,
                clearing_price=20.0,
            )
        )
    for ph, price, _mw in prior_hours:
        for i in range(12):  # 12 MTUs per hour
            rt_sr[ph + timedelta(minutes=5 * i)] = price
    # Event window also needs RT SR prices (drives the shortfall path).
    for i in range(12):
        rt_sr[hour + timedelta(minutes=5 * i)] = 12.0
    result = BacktestResult(asset=asset)
    # Pre-load with sentinel revenue rows (deliberately huge) — the NEW code
    # ignores these. Old code would have refunded the sum.
    for ph, _p, _mw in prior_hours:
        result.revenue_rows.append(
            RevenueRow(
                event_ts_utc=ph,
                asset_id=asset.asset_id,
                product="SR_RTO",
                period_start_utc=ph,
                period_end_utc=ph + timedelta(hours=1),
                cleared_mw=10.0,
                clearing_price=99999.0,
                revenue=99_999.99,
                formula_version="sr_v1",
            )
        )
    n_pre_seed_revenue = sum(r.revenue for r in result.revenue_rows)
    n_pre = len(result.revenue_rows)

    _handle_sr_event_called(
        event=event,
        asset=asset,
        tables=_empty_tables({hour: 25.0}, rt_sr_by_mtu=rt_sr),
        current_soc=asset.soc_min_mwh,  # zero headroom → maximum shortfall
        commitments=commits,
        result=result,
    )
    new_rows = result.revenue_rows[n_pre:]
    cb = [r for r in new_rows if r.product == "SR_RTO_clawback"]
    assert len(cb) == 1
    # Each prior hour contributes 12 MTUs × 10 MW × price × (1/12)
    #                            = 10 MW × price (per hour)
    # Total: 10 × (60 + 30 + 90) = $1800 → -1800.0
    assert cb[0].revenue == pytest.approx(-1800.0)
    # Discriminator: the magnitude does NOT equal the sum of prior-credit rows.
    assert abs(cb[0].revenue) != pytest.approx(n_pre_seed_revenue)
    assert abs(cb[0].revenue) < 10_000.0  # vs $99,999×3 ~ $300k under old logic


# ─── 8. M11 §4.5.2: events <10 min skip clawback, keep shortfall ─────────────


def test_sr_event_under_10_min_skips_clawback():
    asset = ASSETS["example_a"]
    hour = datetime(2026, 2, 15, 14, 0, tzinfo=UTC)
    # 8-min event (<10).
    event = SREventCalled(
        timestamp=hour,
        asset_id=asset.asset_id,
        event_end=hour + timedelta(minutes=8),
        sub_zones="",
    )
    commits = Commitments()
    commits.add_award(
        Award(
            product=Product.SR_RTO,
            period_start=hour,
            period_end=hour + timedelta(hours=1),
            cleared_mw=100.0,
            clearing_price=25.0,
        )
    )
    # Seed prior commitments so the clawback path WOULD fire if it ran.
    prior_hour = hour - timedelta(days=5)
    commits.add_award(
        Award(
            product=Product.SR_RTO,
            period_start=prior_hour,
            period_end=prior_hour + timedelta(hours=1),
            cleared_mw=50.0,
            clearing_price=20.0,
        )
    )
    rt_sr = _rt_sr_per_mtu(
        event.timestamp - timedelta(days=SR_CLAWBACK_DAYS),
        event.event_end,
        30.0,
    )
    result = BacktestResult(asset=asset)
    _handle_sr_event_called(
        event=event,
        asset=asset,
        tables=_empty_tables({hour: 25.0}, rt_sr_by_mtu=rt_sr),
        current_soc=asset.soc_min_mwh,  # full shortfall
        commitments=commits,
        result=result,
    )
    products = [r.product for r in result.revenue_rows]
    assert "SR_RTO_shortfall" in products
    assert "SR_RTO_clawback" not in products


def test_sr_event_at_10_min_threshold_includes_clawback():
    """Exactly 10 min meets the threshold → clawback is emitted."""
    asset = ASSETS["example_a"]
    hour = datetime(2026, 2, 15, 14, 0, tzinfo=UTC)
    event = SREventCalled(
        timestamp=hour,
        asset_id=asset.asset_id,
        event_end=hour + timedelta(minutes=10),
        sub_zones="",
    )
    commits = Commitments()
    commits.add_award(
        Award(
            product=Product.SR_RTO,
            period_start=hour,
            period_end=hour + timedelta(hours=1),
            cleared_mw=100.0,
            clearing_price=25.0,
        )
    )
    prior_hour = hour - timedelta(days=5)
    commits.add_award(
        Award(
            product=Product.SR_RTO,
            period_start=prior_hour,
            period_end=prior_hour + timedelta(hours=1),
            cleared_mw=50.0,
            clearing_price=20.0,
        )
    )
    rt_sr = _rt_sr_per_mtu(
        event.timestamp - timedelta(days=SR_CLAWBACK_DAYS),
        event.event_end,
        30.0,
    )
    result = BacktestResult(asset=asset)
    _handle_sr_event_called(
        event=event,
        asset=asset,
        tables=_empty_tables({hour: 25.0}, rt_sr_by_mtu=rt_sr),
        current_soc=asset.soc_min_mwh,
        commitments=commits,
        result=result,
    )
    products = [r.product for r in result.revenue_rows]
    assert "SR_RTO_shortfall" in products
    assert "SR_RTO_clawback" in products


# ─── 9. _clear_to_award explicit zero MW ─────────────────────────────────────


def test_clear_to_award_explicit_zero_mw():
    """SelfSchedule(mw=0) yields an Award with cleared_mw=0 — not None."""
    mtu_start = datetime(2026, 2, 15, 14, 0, tzinfo=UTC)
    mtu_end = mtu_start + timedelta(minutes=5)
    bid = SelfSchedule(product=Product.RT_Energy, period_start=mtu_start, mw=0.0)
    award = _clear_to_award(
        bid,
        clearing_price=42.0,
        period_end=mtu_end,
        da_cleared_mw=100.0,
    )
    assert award is not None  # Q3: explicit zero must NOT collapse to None
    assert award.cleared_mw == 0.0
    assert award.clearing_price == 42.0
    assert award.da_cleared_mw == 100.0


def test_clear_to_award_bid_curve_out_of_merit_returns_none():
    """A BidCurve that misses clearing still returns None (no award)."""
    mtu_start = datetime(2026, 2, 15, 14, 0, tzinfo=UTC)
    mtu_end = mtu_start + timedelta(minutes=5)
    # Discharge tier at $1000/MWh; clearing price $20 → out of merit.
    curve = BidCurve(
        product=Product.RT_Energy,
        period_start=mtu_start,
        tiers=((50.0, 1000.0),),
    )
    award = _clear_to_award(
        curve,
        clearing_price=20.0,
        period_end=mtu_end,
        da_cleared_mw=10.0,
    )
    assert award is None


def test_clear_to_award_explicit_zero_then_rt_settlement_emits_deviation():
    """End-to-end: SelfSchedule(mw=0) at RT gate + DA position N → engine
    emits a deviation row of (0 − N) × RT_LMP × 1/12."""
    from pjm_engine.events import RTGateClosing
    from pjm_engine.runner import _handle_rt_gate
    from pjm_engine.strategy_base import (
        BaseStrategy,
        Context,
        DataView,
    )

    asset = ASSETS["example_a"]
    hour = datetime(2026, 2, 15, 14, 0, tzinfo=UTC)
    mtu_start = hour
    gate_ts = mtu_start - timedelta(minutes=5)
    DA_MW = 50.0
    RT_LMP = 80.0

    commits = Commitments()
    commits.add_award(
        Award(
            product=Product.DA_Energy,
            period_start=hour,
            period_end=hour + timedelta(hours=1),
            cleared_mw=DA_MW,
            clearing_price=40.0,
        )
    )

    tables = PriceTables(
        da_lmp_by_hour={hour: 40.0},
        rt_lmp_by_mtu={mtu_start: RT_LMP},
        da_sr_by_hour={},
        rt_sr_by_mtu={},
        da_sec_by_hour={},
        rt_sec_by_mtu={},
        reg_by_mtu={},
    )

    class ZeroOutRT(BaseStrategy):
        def on_event(self, event, ctx):
            return SelfSchedule(
                product=Product.RT_Energy,
                period_start=event.mtu_start,
                mw=0.0,
            )

    gate = RTGateClosing(timestamp=gate_ts, asset_id=asset.asset_id, mtu_start=mtu_start)
    result = BacktestResult(asset=asset)
    view = DataView(as_of=gate_ts, da_lmps=pd.DataFrame(), rt_lmps=pd.DataFrame())
    ctx = Context(asset=asset, commitments=commits, view=view)

    _handle_rt_gate(
        gate,
        ZeroOutRT(),
        ctx,
        asset,
        tables,
        current_soc=0.5 * asset.energy_mwh,
        commitments=commits,
        result=result,
    )
    rt_rows = [r for r in result.revenue_rows if r.product == "RT_Energy"]
    assert len(rt_rows) == 1
    # deviation = (0 − 50) × 80 × 1/12 = -333.333...
    assert rt_rows[0].revenue == pytest.approx(-DA_MW * RT_LMP / 12.0)
    assert rt_rows[0].cleared_mw == 0.0
