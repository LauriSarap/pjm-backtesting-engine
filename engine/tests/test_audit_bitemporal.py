"""Bitemporal leakage probes across every feed and every gate type.

This expands `test_data.py` (DA LMP only) to all 5 feeds the engine loads
(`da_hrl_lmps`, `rt_fivemin_mnt_lmps`, `reg_prices`, `da_sr_prices`,
`rt_sr_prices`) and to every gate type the runner fires (DA gate, Reg offer
gate, per-MTU RT gate). A failure here means a strategy can read data that
hadn't been published yet — the engine's no-lookahead guarantee breaks.

References:
- design.md, "What this is" — the no-lookahead requirement
- pjm-data.md §5 — publication-delay table
- pjm-data.md §5.2 — RT LMP blind window (2 MTUs at decision time t)
- PJM Manual 11 §2.5.3.4 / §3.7.6 — LPC posts prelim ≈ MTU_end + 5 min
"""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from pjm_engine.data import (
    CachedView,
    load_da_hrl_lmps,
    load_da_sr_prices,
    load_reg_prices,
    load_rt_fivemin_mnt_lmps,
    load_rt_sr_prices,
    view_as_of,
)
from pjm_engine.runner import (
    _da_gate_time_for,
    _reg_offer_gate_time_for,
)
from pjm_engine.strategy_base import DataView

UTC = ZoneInfo("UTC")
EPT = ZoneInfo("America/New_York")
RT_GATE_LEAD = timedelta(minutes=5)


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def da_lmps():
    return load_da_hrl_lmps()


@pytest.fixture(scope="module")
def rt_lmps():
    return load_rt_fivemin_mnt_lmps()


@pytest.fixture(scope="module")
def reg_prices():
    return load_reg_prices()


@pytest.fixture(scope="module")
def da_sr_prices():
    return load_da_sr_prices()


@pytest.fixture(scope="module")
def rt_sr_prices():
    return load_rt_sr_prices()


# Sample as_of timestamps inside the covered data window.
_AS_OF_SAMPLES = [
    datetime(2025, 10, 1, 5, 0, tzinfo=UTC),
    datetime(2025, 10, 15, 18, 35, tzinfo=UTC),
    datetime(2025, 11, 7, 0, 0, tzinfo=UTC),
    datetime(2026, 1, 3, 12, 30, tzinfo=UTC),
    datetime(2026, 3, 31, 22, 45, tzinfo=UTC),
]


# ── 1. published_at formula correctness, per feed ───────────────────────────


def test_da_lmp_published_at_d_minus_1_1330_ept(da_lmps):
    """DA LMP for operating day D must publish at D-1 13:30 EPT (pjm-data.md §5.1)."""
    sample = da_lmps.sample(20, random_state=0)
    for _, row in sample.iterrows():
        op_date = row["datetime_beginning_ept"].date()
        pub_ept = row["published_at"].astimezone(EPT)
        assert pub_ept.date() == op_date - timedelta(days=1), (
            f"DA LMP at {row['datetime_beginning_ept']} has published_at "
            f"{pub_ept} — expected day before"
        )
        assert (pub_ept.hour, pub_ept.minute) == (13, 30), (
            f"DA LMP published_at must be 13:30 EPT, got {pub_ept.time()}"
        )


def test_rt_lmp_published_at_mtu_start_plus_10min(rt_lmps):
    """RT LMP must publish at MTU_start + 10 min = MTU_end + 5 min
    (pjm-data.md §5.0.1, §5.1; M11 §2.5.3.4 / §3.7.6)."""
    sample = rt_lmps.sample(20, random_state=0)
    expected = sample["datetime_beginning_utc"] + pd.Timedelta(minutes=10)
    actual = sample["published_at"]
    assert (expected.values == actual.values).all(), (
        "RT LMP published_at != datetime_beginning_utc + 10 min"
    )


def test_reg_prices_published_at_mtu_start_plus_10min(reg_prices):
    """RT 5-min reg prices: same convention as RT LMP (pjm-data.md §5.1)."""
    sample = reg_prices.sample(min(20, len(reg_prices)), random_state=0)
    expected = sample["datetime_beginning_utc"] + pd.Timedelta(minutes=10)
    actual = sample["published_at"]
    assert (expected.values == actual.values).all()


def test_da_sr_published_at_d_minus_1_1330_ept(da_sr_prices):
    """DA SRMCP publishes with the rest of DA results: D-1 13:30 EPT."""
    sample = da_sr_prices.sample(min(20, len(da_sr_prices)), random_state=0)
    for _, row in sample.iterrows():
        op_date = row["datetime_beginning_ept"].date()
        pub_ept = row["published_at"].astimezone(EPT)
        assert pub_ept.date() == op_date - timedelta(days=1)
        assert (pub_ept.hour, pub_ept.minute) == (13, 30)


def test_rt_sr_published_at_mtu_start_plus_10min(rt_sr_prices):
    """RT 5-min SRMCP: same convention as RT LMP (pjm-data.md §5.1)."""
    sample = rt_sr_prices.sample(min(20, len(rt_sr_prices)), random_state=0)
    expected = sample["datetime_beginning_utc"] + pd.Timedelta(minutes=10)
    actual = sample["published_at"]
    assert (expected.values == actual.values).all()


# ── 2. view_as_of never returns rows with published_at > as_of, per feed ─────


@pytest.mark.parametrize("as_of", _AS_OF_SAMPLES)
def test_view_da_lmps_no_leak(da_lmps, as_of):
    view = view_as_of(da_lmps, as_of)
    assert (view["published_at"] <= as_of).all()


@pytest.mark.parametrize("as_of", _AS_OF_SAMPLES)
def test_view_rt_lmps_no_leak(rt_lmps, as_of):
    view = view_as_of(rt_lmps, as_of)
    assert (view["published_at"] <= as_of).all()


@pytest.mark.parametrize("as_of", _AS_OF_SAMPLES)
def test_view_reg_prices_no_leak(reg_prices, as_of):
    view = view_as_of(reg_prices, as_of)
    if not view.empty:
        assert (view["published_at"] <= as_of).all()


@pytest.mark.parametrize("as_of", _AS_OF_SAMPLES)
def test_view_da_sr_no_leak(da_sr_prices, as_of):
    view = view_as_of(da_sr_prices, as_of)
    if not view.empty:
        assert (view["published_at"] <= as_of).all()


@pytest.mark.parametrize("as_of", _AS_OF_SAMPLES)
def test_view_rt_sr_no_leak(rt_sr_prices, as_of):
    view = view_as_of(rt_sr_prices, as_of)
    if not view.empty:
        assert (view["published_at"] <= as_of).all()


# ── 3. CachedView matches view_as_of (cache must not loosen the filter) ──────


def test_cached_view_matches_view_as_of_da(da_lmps):
    cache = CachedView(da_lmps)
    rng = random.Random(7)
    pubs = da_lmps["published_at"]
    for _ in range(20):
        idx = rng.randrange(len(pubs))
        as_of = pubs.iloc[idx].to_pydatetime()
        a = view_as_of(da_lmps, as_of)
        b = cache.at(as_of)
        assert len(a) == len(b), f"len mismatch at {as_of}: view={len(a)} cache={len(b)}"
        # same row indices in same order
        assert (a.index == b.index).all()


def test_cached_view_matches_view_as_of_rt(rt_lmps):
    cache = CachedView(rt_lmps)
    rng = random.Random(8)
    pubs = rt_lmps["published_at"]
    for _ in range(20):
        idx = rng.randrange(len(pubs))
        as_of = pubs.iloc[idx].to_pydatetime()
        a = view_as_of(rt_lmps, as_of)
        b = cache.at(as_of)
        assert len(a) == len(b)


# ── 4. DA gate visibility at D-1 11:00 EPT ───────────────────────────────────


def _operating_day_in_data() -> date:
    """An operating day we know is in our data window."""
    return date(2025, 11, 7)


def test_da_gate_blind_to_operating_day_da_lmps(da_lmps):
    """At DA gate (D-1 11:00 EPT), strategy must NOT see operating-day-D DA LMPs.
    DA LMPs publish at D-1 13:30 EPT, 2.5 hours after the gate."""
    op_day = _operating_day_in_data()
    gate = _da_gate_time_for(op_day)
    view = view_as_of(da_lmps, gate)
    # An MTU "belongs" to op_day if its EPT date == op_day.
    leaked = view[view["datetime_beginning_ept"].dt.date == op_day]
    assert leaked.empty, f"DA LMP for {op_day} leaked at gate {gate} ({len(leaked)} rows)"


def test_da_gate_can_see_prior_day_da_lmps(da_lmps):
    """Sanity: D-1's DA LMPs (published at D-2 13:30 EPT) ARE visible at the gate."""
    op_day = _operating_day_in_data()
    gate = _da_gate_time_for(op_day)
    view = view_as_of(da_lmps, gate)
    prior = view[view["datetime_beginning_ept"].dt.date == op_day - timedelta(days=1)]
    assert not prior.empty, "expected D-1 DA LMPs to be visible at gate"


def test_da_gate_blind_to_operating_day_da_sr(da_sr_prices):
    """At DA gate, strategy must NOT see operating-day-D DA SRMCPs."""
    op_day = _operating_day_in_data()
    gate = _da_gate_time_for(op_day)
    view = view_as_of(da_sr_prices, gate)
    leaked = view[view["datetime_beginning_ept"].dt.date == op_day]
    assert leaked.empty


# ── 5. Reg offer gate visibility at D-1 14:15 EPT ────────────────────────────


def test_reg_offer_gate_blind_to_operating_day_reg_prices(reg_prices):
    """At Reg offer gate (D-1 14:15 EPT), strategy must NOT see operating-day-D
    reg prices. Day-D reg prices publish per-MTU starting at MTU_start + 10 min,
    all of which are on or after D 00:10 EPT."""
    op_day = _operating_day_in_data()
    gate = _reg_offer_gate_time_for(op_day)
    view = view_as_of(reg_prices, gate)
    if view.empty:
        pytest.skip("reg_prices feed empty in window")
    leaked = view[view["datetime_beginning_ept"].dt.date == op_day]
    assert leaked.empty, (
        f"reg prices for {op_day} leaked at reg-offer gate {gate} ({len(leaked)} rows)"
    )


def test_reg_offer_gate_can_see_da_results(da_lmps):
    """Sanity: DA results (publish D-1 13:30 EPT) are visible at the Reg offer
    gate (D-1 14:15 EPT) — used by stack strategies that condition Reg on DA."""
    op_day = _operating_day_in_data()
    gate = _reg_offer_gate_time_for(op_day)
    view = view_as_of(da_lmps, gate)
    on_day = view[view["datetime_beginning_ept"].dt.date == op_day]
    assert not on_day.empty, "expected day-D DA LMPs visible at Reg offer gate"


# ── 6. RT gate visibility at MTU_start - 5 min ───────────────────────────────


def _sample_mtu_start() -> datetime:
    """A 5-min MTU we know is in our data window."""
    return datetime(2025, 11, 7, 18, 0, tzinfo=UTC)


def test_rt_gate_blind_to_target_mtu_rt_lmp(rt_lmps):
    """At RT gate (mtu_start - 5 min), strategy must NOT see the target MTU's
    RT LMP (publishes at mtu_start + 10 min — 15 min after the gate)."""
    mtu_start = _sample_mtu_start()
    gate = mtu_start - RT_GATE_LEAD
    view = view_as_of(rt_lmps, gate)
    target = view[view["datetime_beginning_utc"] == mtu_start]
    assert target.empty


def test_rt_gate_blind_to_target_mtu_reg_price(reg_prices):
    mtu_start = _sample_mtu_start()
    gate = mtu_start - RT_GATE_LEAD
    view = view_as_of(reg_prices, gate)
    target = view[view["datetime_beginning_utc"] == mtu_start]
    assert target.empty


def test_rt_gate_blind_to_target_mtu_rt_sr(rt_sr_prices):
    mtu_start = _sample_mtu_start()
    gate = mtu_start - RT_GATE_LEAD
    view = view_as_of(rt_sr_prices, gate)
    target = view[view["datetime_beginning_utc"] == mtu_start]
    assert target.empty


def test_rt_gate_blind_window_is_2_mtus(rt_lmps):
    """At gate t = mtu_start - 5 min, the latest visible RT LMP is for the
    MTU starting at t - 10 min = mtu_start - 15 min. That's a 2-MTU blind
    window matching pjm-data.md §5.2 (M11 §2.5.3.4 / §3.7.6)."""
    mtu_start = _sample_mtu_start()
    gate = mtu_start - RT_GATE_LEAD
    view = view_as_of(rt_lmps, gate)
    expected_latest = mtu_start - timedelta(minutes=15)
    latest_visible = view["datetime_beginning_utc"].max()
    assert latest_visible == expected_latest, (
        f"blind window broken: latest visible MTU is {latest_visible} "
        f"but expected {expected_latest} (mtu_start - 15 min)"
    )


def test_rt_gate_blind_window_same_for_reg_prices(reg_prices):
    """Reg prices share the RT-LMP publication formula → same blind window."""
    mtu_start = _sample_mtu_start()
    gate = mtu_start - RT_GATE_LEAD
    view = view_as_of(reg_prices, gate)
    if view.empty:
        pytest.skip("reg_prices feed empty in window")
    expected_latest = mtu_start - timedelta(minutes=15)
    latest_visible = view["datetime_beginning_utc"].max()
    assert latest_visible == expected_latest


# ── 7. DataView accessor honors the bitemporal filter ────────────────────────


def test_dataview_da_lmp_returns_none_for_unpublished_hour(da_lmps):
    """If you ask DataView for a DA LMP that hasn't published yet, you get None,
    not the eventual value."""
    op_day = _operating_day_in_data()
    gate = _da_gate_time_for(op_day)  # before DA results post for op_day
    view = view_as_of(da_lmps, gate)
    dv = DataView(as_of=gate, da_lmps=view)
    target_hour = datetime(op_day.year, op_day.month, op_day.day, 18, tzinfo=EPT)
    target_utc = target_hour.astimezone(UTC)
    # Find the zone string from the data itself
    zone = view["zone"].dropna().iloc[0]
    assert dv.da_lmp(zone, target_utc) is None


# ── 8. Strategy-contract: ctx.view is a DataView pinned to event.timestamp ───


def test_runner_ctx_view_is_event_pinned_dataview(da_lmps):
    """`ctx.view` is the `DataView` already pinned to the event's timestamp —
    not a callable. The engine builds the view once per event and hands it
    to the strategy as a plain field.

    Earlier the field was `Callable[[datetime], DataView]` whose argument
    was silently ignored — strategies that wrote `ctx.view(t)` got back the
    gate's view regardless of `t`, a contract drift. The new shape removes
    the callable wrapper entirely; strategies say `ctx.view` and the
    bitemporal guarantee is enforced at construction time."""
    from pjm_engine.battery import ASSETS
    from pjm_engine.strategy_base import Commitments, Context

    gate = _da_gate_time_for(_operating_day_in_data())
    view = view_as_of(da_lmps, gate)
    dv = DataView(as_of=gate, da_lmps=view)
    ctx = Context(asset=ASSETS["example_a"], commitments=Commitments(), view=dv)
    assert ctx.view is dv
    assert ctx.view.as_of == gate
