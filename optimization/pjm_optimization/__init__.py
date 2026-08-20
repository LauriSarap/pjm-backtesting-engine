"""PJM optimization package.

Contains MILP formulations that compute revenue ceilings and rolling-horizon
strategies for the BESS backtesting engine. Outputs match the engine's
parquet schema, so the eval (`pjm-eval`) treats MILP runs as a strategy run
with a `ceiling` role.
"""

from pjm_optimization.forecast import (
    Forecaster,
    PerfectOracleForecaster,
    PriceForecast,
)
from pjm_optimization.perfect_foresight import (
    PerfectForesightResult,
    solve_perfect_foresight,
    write_parquet,
)

__all__ = [
    "Forecaster",
    "PerfectForesightResult",
    "PerfectOracleForecaster",
    "PriceForecast",
    "solve_perfect_foresight",
    "write_parquet",
]
