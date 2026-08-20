"""BidCurve clearing — implements the 'follow SCED via offer curve' archetype.

Per design.md §"Gates and clearing":
  cleared_MW = max{ q : bid_price(q) ≤ clearing_price }   (discharge)
             = max{ q : bid_price(q) ≥ clearing_price }   (charge, mirror)

A `BidCurve` is a tuple of `(mw, $/MWh)` tiers. For a discharge offer (mw > 0),
each tier means "I am willing to deliver up to `mw` MW total IF clearing price
≥ $/MWh." Tiers are cumulative: as price rises, the strategy is willing to
deliver more, so |mw| is non-decreasing AND price is non-decreasing.

Charge mirrors: as price falls, the strategy is willing to charge more, so
|mw| is non-decreasing AND price is non-increasing.
"""

from __future__ import annotations

from ..battery import AssetConfig
from ..errors import BidValidationError
from ..strategy_base import BidCurve, Product

_TOL = 1e-9


def validate_bid_curve(curve: BidCurve, asset: AssetConfig) -> None:
    """Bid-level rule class (a) for BidCurve. Mirrors validate_bid for SelfSchedule."""
    if not curve.tiers:
        raise BidValidationError("empty bid curve")

    first_mw = curve.tiers[0][0]
    if first_mw == 0:
        raise BidValidationError("first tier has zero MW")
    is_discharge = first_mw > 0

    # Gate alignment.
    if curve.product == Product.DA_Energy:
        if curve.period_start.minute != 0 or curve.period_start.second != 0:
            raise BidValidationError(
                f"DA energy curve must start at top of hour, got {curve.period_start}"
            )
    elif curve.product == Product.RT_Energy:
        if curve.period_start.minute % 5 != 0 or curve.period_start.second != 0:
            raise BidValidationError(
                f"RT energy curve must start on 5-min boundary, got {curve.period_start}"
            )

    prev_abs_mw = 0.0
    prev_price: float | None = None
    for mw, price in curve.tiers:
        # Sign consistency.
        if (mw > 0) != is_discharge or mw == 0:
            raise BidValidationError(
                f"tier {mw} MW disagrees with curve direction ({'discharge' if is_discharge else 'charge'})"
            )

        abs_mw = abs(mw)
        if abs_mw <= prev_abs_mw + _TOL:
            raise BidValidationError(
                f"curve tiers must be strictly increasing in |MW|, got {abs_mw} after {prev_abs_mw}"
            )

        if abs_mw - asset.power_mw > 1e-6:
            raise BidValidationError(
                f"|{mw}| MW exceeds {asset.asset_id} nameplate {asset.power_mw}"
            )

        rem = abs_mw % asset.bid_increment
        if rem > _TOL and abs(rem - asset.bid_increment) > _TOL:
            raise BidValidationError(f"{mw} MW not a multiple of {asset.bid_increment} MW")

        if prev_price is not None:
            if is_discharge and price < prev_price - _TOL:
                raise BidValidationError(
                    f"discharge curve must be non-decreasing in price, got {price} after {prev_price}"
                )
            if not is_discharge and price > prev_price + _TOL:
                raise BidValidationError(
                    f"charge curve must be non-increasing in price, got {price} after {prev_price}"
                )

        prev_abs_mw = abs_mw
        prev_price = price


def clear_bid_curve(curve: BidCurve, clearing_price: float) -> float:
    """Clear a curve against a single scalar price.

    Returns signed cleared MW. Discharge (positive tiers) clears max |mw| where
    tier price ≤ clearing_price; charge (negative tiers) clears max |mw| where
    tier price ≥ clearing_price. Out of merit → 0.
    """
    if not curve.tiers:
        return 0.0

    is_discharge = curve.tiers[0][0] > 0
    cleared_abs = 0.0

    for mw, price in curve.tiers:
        in_merit = (
            (price <= clearing_price + _TOL) if is_discharge else (price >= clearing_price - _TOL)
        )
        if in_merit:
            cleared_abs = max(cleared_abs, abs(mw))

    return cleared_abs if is_discharge else -cleared_abs
