"""Bid + stack + SoC validators.

Each function returns silently on success or raises a typed error. The runner
calls all three before clearing any awards.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Iterable
from zoneinfo import ZoneInfo

from .battery import AssetConfig, step_soc
from .errors import (
    BidValidationError,
    ProductNotInRegimeError,
    SoCInfeasibleError,
    SubZoneMismatchError,
)
from .markets import validate_bid_curve
from .settle import Award
from .strategy_base import BidCurve, Product, SelfSchedule

EPT = ZoneInfo("America/New_York")

# Regime windows (design.md §"What changed at the regime boundary"). Reg
# product flipped from RegA/RegD (v1) to single bidirectional Reg (v2) at
# 2025-10-01 00:00 EPT and is scheduled to split again to RegUp_v3/RegDn_v3 at
# 2026-10-01 00:00 EPT. Bidding outside a product's window is a strategy bug.
REG_V2_START_EPT = datetime(2025, 10, 1, 0, 0, tzinfo=EPT)
REG_V2_END_EPT = datetime(2026, 10, 1, 0, 0, tzinfo=EPT)
# Pre-redesign RegA / RegD lived in `[v1_start, v2_start)`. We don't pin a
# lower bound (engine has no historical floor); v1 is allowed for any
# `period_start < REG_V2_START_EPT`.
REG_V1_END_EPT = REG_V2_START_EPT

INCREMENT_TOL = 1e-9  # PJM 0.1 MW; allow tiny float slop
MTU_5MIN = timedelta(minutes=5)

# Reg SoC reservation: at any MTU with R MW of Reg held, SoC must have
# R × REG_SUSTAIN_HOURS MWh of headroom in BOTH directions. PJM Manual 12
# §4.6.3 doesn't pin an exact number for ESR Reg; 0.25 h gives a 15-minute
# extreme-signal cushion under the zero-mean-signal assumption and matches
# common production-cost-model SoC time requirements.
REG_SUSTAIN_HOURS = 0.25

# SR sustain rule (pjm-data.md §2.2): X MW SR cleared requires ≥ X × 0.5 MWh of
# energy available throughout the cleared hour — sustain a 30-min response if
# called. One-directional: only discharge headroom; SR doesn't charge.
SR_SUSTAIN_HOURS = 0.5

# Sec / 30-Minute Reserve sustain rule: same 30-min sustain shape as SR, just
# slower response time (10–30 min vs SR's 10 min). One-directional discharge
# headroom requirement matches SR — held capacity must be deliverable for the
# 30-min response window.
SEC_SUSTAIN_HOURS = 0.5

BidLike = SelfSchedule | BidCurve


def bid_max_abs_mw(bid: BidLike) -> float:
    """The biggest |MW| this bid could possibly clear at."""
    if isinstance(bid, SelfSchedule):
        return abs(bid.mw)
    return max(abs(mw) for mw, _ in bid.tiers)


def _direction_sign(bid: BidLike) -> int:
    """+1 discharge, -1 charge, 0 zero-MW (only possible for SelfSchedule)."""
    if isinstance(bid, SelfSchedule):
        return 1 if bid.mw > 0 else (-1 if bid.mw < 0 else 0)
    first_mw = bid.tiers[0][0] if bid.tiers else 0
    return 1 if first_mw > 0 else (-1 if first_mw < 0 else 0)


def validate_bid(bid: BidLike, asset: AssetConfig) -> None:
    """Bid-level rule class (a). Dispatches on bid type."""
    if isinstance(bid, BidCurve):
        validate_bid_curve(bid, asset)
        return

    # SelfSchedule path.
    if abs(bid.mw) - asset.power_mw > 1e-6:
        raise BidValidationError(
            f"|{bid.mw}| MW exceeds {asset.asset_id} nameplate {asset.power_mw}"
        )

    rem = abs(bid.mw) % asset.bid_increment
    if rem > INCREMENT_TOL and abs(rem - asset.bid_increment) > INCREMENT_TOL:
        raise BidValidationError(f"{bid.mw} MW not a multiple of {asset.bid_increment} MW")

    if bid.product == Product.DA_Energy:
        if bid.period_start.minute != 0 or bid.period_start.second != 0:
            raise BidValidationError(
                f"DA energy bid must start at top of hour, got {bid.period_start}"
            )

    if bid.product == Product.RT_Energy:
        if bid.period_start.minute % 5 != 0 or bid.period_start.second != 0:
            raise BidValidationError(
                f"RT energy bid must start on 5-min boundary, got {bid.period_start}"
            )

    if bid.product == Product.Reg_v2:
        # Regime window: Reg_v2 is the post-2025-10 redesign product, scheduled
        # to be replaced by RegUp_v3/RegDn_v3 at 2026-10-01 EPT.
        if not (REG_V2_START_EPT <= bid.period_start < REG_V2_END_EPT):
            raise ProductNotInRegimeError(
                f"Reg_v2 not in regime at {bid.period_start} "
                f"(valid: {REG_V2_START_EPT} ≤ t < {REG_V2_END_EPT})"
            )
        # Half-hour assignment block alignment (post-Oct-2025 redesign).
        if bid.period_start.minute not in (0, 30) or bid.period_start.second != 0:
            raise BidValidationError(
                f"Reg_v2 bid must start on a half-hour boundary, got {bid.period_start}"
            )
        # Reg is bidirectional capacity — sign-less, must be positive.
        if bid.mw <= 0:
            raise BidValidationError(
                f"Reg_v2 bid MW must be positive bidirectional capacity, got {bid.mw}"
            )

    if bid.product in (Product.RegA_v1, Product.RegD_v1):
        # Regime window: RegA/RegD lived pre-redesign (period_start < REG_V1_END_EPT).
        if bid.period_start >= REG_V1_END_EPT:
            raise ProductNotInRegimeError(
                f"{bid.product.value} not in regime at {bid.period_start} "
                f"(valid: t < {REG_V1_END_EPT})"
            )
        # Pre-redesign Reg cleared on hourly blocks.
        if bid.period_start.minute != 0 or bid.period_start.second != 0:
            raise BidValidationError(
                f"{bid.product.value} bid must start at top of hour, got {bid.period_start}"
            )
        # Sign-less bidirectional capacity (pjm-data.md §2.1; both RegA and RegD
        # are modeled as ±MW around midpoint).
        if bid.mw <= 0:
            raise BidValidationError(
                f"{bid.product.value} bid MW must be positive bidirectional capacity, got {bid.mw}"
            )

    if bid.product == Product.SR_RTO:
        # DA SR is hourly; RT SR adjustments are per-MTU but the strategy
        # bids hourly self-schedules at the DA gate (price-taker model).
        if bid.period_start.minute != 0 or bid.period_start.second != 0:
            raise BidValidationError(
                f"SR_RTO bid must start at top of hour, got {bid.period_start}"
            )
        # SR is one-directional upward capacity — must be positive.
        if bid.mw <= 0:
            raise BidValidationError(
                f"SR_RTO bid MW must be positive upward capacity, got {bid.mw}"
            )

    if bid.product == Product.Sec_RTO:
        # Sec mirrors SR's gate alignment (DA hourly).
        if bid.period_start.minute != 0 or bid.period_start.second != 0:
            raise BidValidationError(
                f"Sec_RTO bid must start at top of hour, got {bid.period_start}"
            )
        if bid.mw <= 0:
            raise BidValidationError(
                f"Sec_RTO bid MW must be positive upward capacity, got {bid.mw}"
            )

    # Sub-zone match (design.md §rule class a): the `*_RTO` reserve products
    # require the asset to be registered in the RTO sub-zone. PJM clears
    # SR / Sec / Supp separately per active sub-zone (RTO + at most one
    # active sub-zone like Mid-Atlantic Dominion); a resource registered in
    # MAD cannot serve the system-wide RTO requirement and vice versa.
    if bid.product in (Product.SR_RTO, Product.Sec_RTO):
        if asset.sub_zone != "RTO":
            raise SubZoneMismatchError(
                f"{bid.product.value} bid requires asset registered in RTO "
                f"sub-zone, got sub_zone={asset.sub_zone!r} on {asset.asset_id}"
            )


def _periods_to_mtus(period_start: datetime, period_end: datetime) -> list[datetime]:
    """Decompose a half-open [start, end) period into 5-min MTU starts."""
    out: list[datetime] = []
    t = period_start
    while t < period_end:
        out.append(t)
        t += MTU_5MIN
    return out


def _bid_period_end(bid: BidLike) -> datetime:
    """Closing edge of a bid's period — derived from the product."""
    if bid.product == Product.DA_Energy:
        return bid.period_start + timedelta(hours=1)
    if bid.product == Product.Reg_v2:
        return bid.period_start + timedelta(minutes=30)
    if bid.product in (Product.RegA_v1, Product.RegD_v1):
        return bid.period_start + timedelta(hours=1)
    if bid.product in (Product.SR_RTO, Product.Sec_RTO):
        return bid.period_start + timedelta(hours=1)
    # RT_Energy and any other 5-min product
    return bid.period_start + MTU_5MIN


def validate_stack(
    bids: list[BidLike],
    asset: AssetConfig,
    existing_awards: Iterable[Award] = (),
) -> None:
    """Stack-level rule class (b): per-(product, period) AND per-MTU cross-product caps.

    Two checks:
    1. Per-(product, period): charge XOR discharge, and sum of bids of the same
       product in the same period ≤ nameplate.
    2. Cross-product per-MTU: at every 5-min MTU touched by any new bid, the
       total demanded capacity (|energy| + Reg + future SR…) — counting both
       new bids AND any prior `existing_awards` covering that MTU — must not
       exceed the asset nameplate. PJM tariff treats Reg as ± MW around the
       energy midpoint, so total capability needed = |energy_MW| + Reg_MW.

    For BidCurves the worst-case |MW| (curve's max |tier MW|) is used.
    DA and RT bids in the same MTU collapse to one physical dispatch — DA is
    financial, RT overrides per-MTU. To avoid double-counting that overlap, an
    RT bid's contribution replaces the DA contribution for the MTUs it covers.
    """
    by_key: dict[tuple, list[BidLike]] = defaultdict(list)
    for b in bids:
        by_key[(b.product, b.period_start)].append(b)

    for (product, period), ps in by_key.items():
        signs = {_direction_sign(p) for p in ps if _direction_sign(p) != 0}
        if 1 in signs and -1 in signs:
            raise BidValidationError(
                f"charge+discharge in same period {period} for {product.value} on {asset.asset_id}"
            )
        total_abs = sum(bid_max_abs_mw(p) for p in ps)
        if total_abs - asset.power_mw > 1e-6:
            raise BidValidationError(
                f"sum {product.value} bids {total_abs} MW exceeds nameplate {asset.power_mw} at {period}"
            )

    if not bids:
        return

    # Cross-product per-MTU cap: collapse every bid + overlapping existing
    # award to its 5-min MTUs, summing |MW| with the DA/RT override rule.
    new_window_start = min(b.period_start for b in bids)
    new_window_end = max(_bid_period_end(b) for b in bids)

    energy_da_by_mtu: dict[datetime, float] = defaultdict(float)
    energy_rt_by_mtu: dict[datetime, float] = defaultdict(float)
    other_by_mtu: dict[datetime, float] = defaultdict(float)

    def _add_energy(product, mtus, mw):
        bucket = energy_rt_by_mtu if product == Product.RT_Energy else energy_da_by_mtu
        for m in mtus:
            bucket[m] += mw

    def _add_other(mtus, mw):
        for m in mtus:
            other_by_mtu[m] += mw

    for b in bids:
        mtus = _periods_to_mtus(b.period_start, _bid_period_end(b))
        mw = bid_max_abs_mw(b)
        if b.product in (Product.DA_Energy, Product.RT_Energy):
            _add_energy(b.product, mtus, mw)
        else:
            _add_other(mtus, mw)

    # Restrict existing_awards to those overlapping the new bids' time range.
    for a in existing_awards:
        if a.period_end <= new_window_start or a.period_start >= new_window_end:
            continue
        mtus = _periods_to_mtus(a.period_start, a.period_end)
        mw = abs(a.cleared_mw)
        if a.product in (Product.DA_Energy, Product.RT_Energy):
            _add_energy(a.product, mtus, mw)
        else:
            _add_other(mtus, mw)

    # Per-MTU total = max(|DA|, |RT|) + Σ other (Reg, future SR, etc.).
    touched_mtus = set(energy_da_by_mtu) | set(energy_rt_by_mtu) | set(other_by_mtu)
    for m in touched_mtus:
        physical = max(energy_da_by_mtu.get(m, 0.0), energy_rt_by_mtu.get(m, 0.0))
        total = physical + other_by_mtu.get(m, 0.0)
        if total - asset.power_mw > 1e-6:
            raise BidValidationError(
                f"cross-product power cap: {total:.3f} MW total at MTU {m} "
                f"(energy {physical:.3f} + reserves {other_by_mtu.get(m, 0.0):.3f}) "
                f"exceeds nameplate {asset.power_mw} on {asset.asset_id}"
            )


_REG_PRODUCTS = (Product.Reg_v2, Product.RegA_v1, Product.RegD_v1)


def _product_mw_at(
    awards: Iterable[Award],
    mtu_start: datetime,
    products: tuple[Product, ...],
) -> float:
    """Total MW of the given products held during the MTU starting at
    `mtu_start`. An MTU is covered when [start, start+5min) ⊆
    [award_start, award_end) — works for hourly (SR/Sec/Reg_v1) and
    half-hour (Reg_v2) blocks alike."""
    mtu_end = mtu_start + MTU_5MIN
    return sum(
        a.cleared_mw
        for a in awards
        if a.product in products and a.period_start <= mtu_start and a.period_end >= mtu_end
    )


def _check_reg_headroom(
    soc_mwh: float,
    reg_mw: float,
    asset: AssetConfig,
    ts: datetime,
) -> None:
    """Reg SoC reservation: ≥ reg_mw × REG_SUSTAIN_HOURS MWh both directions."""
    if reg_mw <= 0:
        return
    needed = reg_mw * REG_SUSTAIN_HOURS
    if soc_mwh - asset.soc_min_mwh < needed - 1e-6:
        raise SoCInfeasibleError(
            f"Reg discharge headroom: SoC {soc_mwh:.2f} MWh has only "
            f"{soc_mwh - asset.soc_min_mwh:.2f} MWh above SoC_min "
            f"({asset.soc_min_mwh:.2f}); needs {needed:.2f} MWh for "
            f"{reg_mw} MW Reg at {ts}"
        )
    if asset.soc_max_mwh - soc_mwh < needed - 1e-6:
        raise SoCInfeasibleError(
            f"Reg charge headroom: SoC {soc_mwh:.2f} MWh has only "
            f"{asset.soc_max_mwh - soc_mwh:.2f} MWh below SoC_max "
            f"({asset.soc_max_mwh:.2f}); needs {needed:.2f} MWh for "
            f"{reg_mw} MW Reg at {ts}"
        )


def _check_reserve_sustain(
    soc_mwh: float,
    reserve_mw: float,
    sustain_hours: float,
    label: str,
    asset: AssetConfig,
    ts: datetime,
) -> None:
    """Reserve sustain: ≥ reserve_mw × sustain_hours MWh of discharge
    headroom. Reserves are one-directional — no charge-headroom check.
    Held SR + Sec stack additively under the cross-product cap, but their
    sustain requirements are checked independently — both must be
    deliverable."""
    if reserve_mw <= 0:
        return
    needed = reserve_mw * sustain_hours
    if soc_mwh - asset.soc_min_mwh < needed - 1e-6:
        raise SoCInfeasibleError(
            f"{label} sustain: SoC {soc_mwh:.2f} MWh has only "
            f"{soc_mwh - asset.soc_min_mwh:.2f} MWh above SoC_min "
            f"({asset.soc_min_mwh:.2f}); needs {needed:.2f} MWh for "
            f"{reserve_mw} MW {label} at {ts}"
        )


def simulate_soc(
    awards: list[Award],
    initial_soc_mwh: float,
    asset: AssetConfig,
) -> list[tuple[datetime, float]]:
    """Walk 5-min MTUs across the awards' time span. RT awards override DA per MTU.

    Physical dispatch model: at each 5-min MTU, actual MW = RT award MW if one
    exists for that MTU, else the DA award MW for the parent hour, else 0.
    Reg awards in `awards` add a per-MTU SoC headroom check (see
    `_check_reg_headroom`); under perfect tracking they don't change SoC
    (signal is zero-mean) but they DO require headroom to be honored.

    Returns the full per-MTU trajectory; raises SoCInfeasibleError on any
    out-of-bounds energy step or insufficient Reg headroom.
    """
    if not awards:
        return []

    energy_awards = [a for a in awards if a.product in (Product.DA_Energy, Product.RT_Energy)]
    reg_awards = [a for a in awards if a.product in _REG_PRODUCTS]
    sr_awards = [a for a in awards if a.product == Product.SR_RTO]
    sec_awards = [a for a in awards if a.product == Product.Sec_RTO]

    if not energy_awards and not reg_awards and not sr_awards and not sec_awards:
        return []

    period_start = min(a.period_start for a in awards)
    period_end = max(a.period_end for a in awards)

    rt_by_mtu = {a.period_start: a for a in energy_awards if a.product == Product.RT_Energy}
    da_by_hour = {a.period_start: a for a in energy_awards if a.product == Product.DA_Energy}

    soc = initial_soc_mwh
    trajectory: list[tuple[datetime, float]] = [(period_start, soc)]

    def _check_as_reservations(soc_mwh: float, ts: datetime) -> None:
        _check_reg_headroom(
            soc_mwh,
            _product_mw_at(reg_awards, ts, _REG_PRODUCTS),
            asset,
            ts,
        )
        _check_reserve_sustain(
            soc_mwh,
            _product_mw_at(sr_awards, ts, (Product.SR_RTO,)),
            SR_SUSTAIN_HOURS,
            "SR",
            asset,
            ts,
        )
        _check_reserve_sustain(
            soc_mwh,
            _product_mw_at(sec_awards, ts, (Product.Sec_RTO,)),
            SEC_SUSTAIN_HOURS,
            "Sec",
            asset,
            ts,
        )

    # AS reservation must hold at the START of each AS-covered MTU.
    _check_as_reservations(soc, period_start)

    mtu = period_start
    while mtu < period_end:
        rt = rt_by_mtu.get(mtu)
        if rt is not None:
            actual_mw = rt.cleared_mw
        else:
            hour_start = mtu.replace(minute=0, second=0, microsecond=0)
            da = da_by_hour.get(hour_start)
            actual_mw = da.cleared_mw if da is not None else 0.0

        charge_mw = -actual_mw if actual_mw < 0 else 0.0
        discharge_mw = actual_mw if actual_mw > 0 else 0.0
        soc = step_soc(soc, charge_mw, discharge_mw, 1.0 / 12.0, asset)

        next_mtu = mtu + MTU_5MIN
        if soc < asset.soc_min_mwh - 1e-6 or soc > asset.soc_max_mwh + 1e-6:
            raise SoCInfeasibleError(
                f"SoC {soc:.2f} MWh out of [{asset.soc_min_mwh}, {asset.soc_max_mwh}] "
                f"at {next_mtu} after {actual_mw} MW dispatch"
            )

        # Headroom at the start of the next MTU (which is "now" after this step).
        _check_as_reservations(soc, next_mtu)

        trajectory.append((next_mtu, soc))
        mtu = next_mtu

    return trajectory
