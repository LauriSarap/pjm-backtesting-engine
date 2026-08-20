"""All exception classes raised by the engine.

Default responses per design.md error table. Strategies should never catch
EngineError — failure is loud by design.
"""


class EngineError(Exception):
    """Base for every engine-raised exception."""


class DataNotAvailableError(EngineError):
    """A view query asked for a row with `published_at > as_of`. Strategy bug."""


class BidValidationError(EngineError):
    """Bid violated power, increment, gate-alignment, or eligibility rule."""


class ProductNotInRegimeError(BidValidationError):
    """Strategy bid a product that doesn't exist in the operating-date regime
    (e.g. Reg_v2 for 2027 or RegUp_v3 for 2025)."""


class SubZoneMismatchError(BidValidationError):
    """SR bid into a Sub-Zone where the asset isn't registered."""


class RegimeBoundaryError(EngineError):
    """Settlement requested for an interval with no formula version."""


class DataGapError(EngineError):
    """Required data feed has no row for the queried interval. Hard fail."""


class SoCInfeasibleError(EngineError):
    """Cleared awards would drive SoC outside [SoC_min, SoC_max].
    Engine response: reject the batch, re-prompt strategy."""


class InvariantError(EngineError):
    """Floor invariant violated. Engine bug, not strategy bug. Hard fail run."""
