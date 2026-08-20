"""PJM market-mechanics backtesting engine."""

from .battery import ASSETS, AssetConfig
from .errors import (
    BidValidationError,
    DataGapError,
    DataNotAvailableError,
    EngineError,
    InvariantError,
    ProductNotInRegimeError,
    RegimeBoundaryError,
    SoCInfeasibleError,
    SubZoneMismatchError,
)
from .runner import BacktestResult, PreparedMarketData, prepare_market_data, run_backtest
from .strategy_base import (
    Acknowledgment,
    BaseStrategy,
    BidCurve,
    Commitments,
    Context,
    DataView,
    Product,
    SelfSchedule,
    StrategyResponse,
)

__version__ = "0.1.0"

__all__ = [
    "ASSETS",
    "Acknowledgment",
    "AssetConfig",
    "BacktestResult",
    "BaseStrategy",
    "BidCurve",
    "BidValidationError",
    "Commitments",
    "Context",
    "DataGapError",
    "DataNotAvailableError",
    "DataView",
    "EngineError",
    "InvariantError",
    "PreparedMarketData",
    "Product",
    "ProductNotInRegimeError",
    "RegimeBoundaryError",
    "SelfSchedule",
    "SoCInfeasibleError",
    "StrategyResponse",
    "SubZoneMismatchError",
    "prepare_market_data",
    "run_backtest",
]
