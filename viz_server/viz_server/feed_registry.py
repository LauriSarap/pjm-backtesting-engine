"""Feed registry: the public surface of feeds the viz tool can render.

Each feed_id maps to:
  - a `loader_key`: identifies which `pjm_engine` loader produces the
    backing DataFrame. Multiple feed_ids can share one loader (e.g.
    `reg_rmccp` and `reg_rmpcp` both come from `load_reg_prices()`),
    so `runtime` only needs one CachedView per loader.
  - a `value_column`: which column of the DataFrame is the y-value.
  - layout/display metadata (pane, unit, display_name) used by the
    frontend FeedToggle.

The registry is the *display* layer; as-of filtering is owned
exclusively by `pjm_engine.data.view_as_of` / `CachedView`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

from pjm_engine import data as engine_data

from . import extra_loaders


@dataclass(frozen=True)
class LoaderSpec:
    """Identifies a backing pjm_engine loader.

    The viz_server preloads one CachedView per LoaderSpec at boot.
    """

    key: str
    fn: Callable[..., pd.DataFrame]
    zonal: bool  # True if rows are filtered/joined on a `zone` column


@dataclass(frozen=True)
class FeedSpec:
    """A single y-axis series the frontend can render."""

    feed_id: str
    display_name: str
    loader: LoaderSpec
    value_column: str
    pane: str  # 'prices' | 'load' | 'gen' | 'as'
    unit: str  # '$/MWh' | 'MW' | 'score' | …
    has_revisions: bool = False  # True for forecast feeds


# --- Loaders ---------------------------------------------------------------

LOADER_DA_HRL_LMPS = LoaderSpec(key="da_hrl_lmps", fn=engine_data.load_da_hrl_lmps, zonal=True)
LOADER_RT_FIVEMIN_MNT_LMPS = LoaderSpec(
    key="rt_fivemin_mnt_lmps", fn=engine_data.load_rt_fivemin_mnt_lmps, zonal=True
)
LOADER_REG_PRICES = LoaderSpec(key="reg_prices", fn=engine_data.load_reg_prices, zonal=False)
LOADER_REG_MARKET_RESULTS = LoaderSpec(
    key="reg_market_results", fn=engine_data.load_reg_market_results, zonal=False
)
LOADER_DA_SR_PRICES = LoaderSpec(key="da_sr_prices", fn=engine_data.load_da_sr_prices, zonal=False)
LOADER_RT_SR_PRICES = LoaderSpec(key="rt_sr_prices", fn=engine_data.load_rt_sr_prices, zonal=False)

# viz-only loaders (live in viz_server, not the engine).
LOADER_LOAD_METERED = LoaderSpec(
    key="load_metered", fn=extra_loaders.load_hrl_load_metered, zonal=True
)
LOADER_SOLAR_GEN = LoaderSpec(key="solar_gen", fn=extra_loaders.load_solar_gen, zonal=False)
LOADER_WIND_GEN = LoaderSpec(key="wind_gen", fn=extra_loaders.load_wind_gen, zonal=False)
LOADER_GEN_OUTAGES = LoaderSpec(key="gen_outages", fn=extra_loaders.load_gen_outages, zonal=False)
LOADER_SYNTHETIC_RTO_FORECASTS = LoaderSpec(
    key="synthetic_rto_forecasts",
    fn=extra_loaders.load_synthetic_rto_forecasts,
    zonal=False,
)


LOADERS: dict[str, LoaderSpec] = {
    s.key: s
    for s in [
        LOADER_DA_HRL_LMPS,
        LOADER_RT_FIVEMIN_MNT_LMPS,
        LOADER_REG_PRICES,
        LOADER_REG_MARKET_RESULTS,
        LOADER_DA_SR_PRICES,
        LOADER_RT_SR_PRICES,
        LOADER_LOAD_METERED,
        LOADER_SOLAR_GEN,
        LOADER_WIND_GEN,
        LOADER_GEN_OUTAGES,
        LOADER_SYNTHETIC_RTO_FORECASTS,
    ]
}


# --- Feeds -----------------------------------------------------------------

FEEDS: dict[str, FeedSpec] = {
    f.feed_id: f
    for f in [
        FeedSpec(
            feed_id="da_lmp",
            display_name="DA LMP",
            loader=LOADER_DA_HRL_LMPS,
            value_column="total_lmp_da",
            pane="prices",
            unit="$/MWh",
        ),
        FeedSpec(
            feed_id="rt_lmp",
            display_name="RT LMP",
            loader=LOADER_RT_FIVEMIN_MNT_LMPS,
            value_column="total_lmp_rt",
            pane="prices",
            unit="$/MWh",
        ),
        FeedSpec(
            feed_id="reg_rmccp",
            display_name="Reg RMCCP",
            loader=LOADER_REG_PRICES,
            value_column="rmccp",
            pane="as",
            unit="$/MWh",
        ),
        FeedSpec(
            feed_id="reg_rmpcp",
            display_name="Reg RMPCP",
            loader=LOADER_REG_PRICES,
            value_column="rmpcp",
            pane="as",
            unit="$/MWh",
        ),
        FeedSpec(
            feed_id="da_sr_mcp",
            display_name="DA Sync Reserve MCP",
            loader=LOADER_DA_SR_PRICES,
            value_column="mcp",
            pane="as",
            unit="$/MWh",
        ),
        FeedSpec(
            feed_id="rt_sr_mcp",
            display_name="RT Sync Reserve MCP",
            loader=LOADER_RT_SR_PRICES,
            value_column="mcp",
            pane="as",
            unit="$/MWh",
        ),
        FeedSpec(
            feed_id="reg_requirement",
            display_name="Reg Requirement",
            loader=LOADER_REG_MARKET_RESULTS,
            value_column="requirement",
            pane="as",
            unit="MW",
        ),
        FeedSpec(
            feed_id="reg_perfscore",
            display_name="RTO Reg Perf Score",
            loader=LOADER_REG_MARKET_RESULTS,
            value_column="rto_perfscore",
            pane="as",
            unit="score",
        ),
        FeedSpec(
            feed_id="load_metered",
            display_name="Metered Load",
            loader=LOADER_LOAD_METERED,
            value_column="mw",
            pane="load",
            unit="MW",
        ),
        FeedSpec(
            feed_id="solar_gen",
            display_name="Solar Generation (RTO)",
            loader=LOADER_SOLAR_GEN,
            value_column="solar_mw",
            pane="gen",
            unit="MW",
        ),
        FeedSpec(
            feed_id="wind_gen",
            display_name="Wind Generation (RTO)",
            loader=LOADER_WIND_GEN,
            value_column="wind_mw",
            pane="gen",
            unit="MW",
        ),
        FeedSpec(
            feed_id="gen_outage_total",
            display_name="Gen Outage Total (RTO)",
            loader=LOADER_GEN_OUTAGES,
            value_column="total_mw",
            pane="outages",
            unit="MW",
        ),
        FeedSpec(
            feed_id="gen_outage_forced",
            display_name="Gen Outage Forced (RTO)",
            loader=LOADER_GEN_OUTAGES,
            value_column="forced_mw",
            pane="outages",
            unit="MW",
        ),
        FeedSpec(
            feed_id="gen_outage_planned",
            display_name="Gen Outage Planned (RTO)",
            loader=LOADER_GEN_OUTAGES,
            value_column="planned_mw",
            pane="outages",
            unit="MW",
        ),
        FeedSpec(
            feed_id="gen_outage_maintenance",
            display_name="Gen Outage Maintenance (RTO)",
            loader=LOADER_GEN_OUTAGES,
            value_column="maintenance_mw",
            pane="outages",
            unit="MW",
        ),
        FeedSpec(
            feed_id="rto_load_actual",
            display_name="RTO Load Actual",
            loader=LOADER_SYNTHETIC_RTO_FORECASTS,
            value_column="load_actual_mw",
            pane="forecast",
            unit="MW",
        ),
        FeedSpec(
            feed_id="rto_solar_actual",
            display_name="RTO Solar Actual",
            loader=LOADER_SYNTHETIC_RTO_FORECASTS,
            value_column="solar_actual_mw",
            pane="forecast",
            unit="MW",
        ),
        FeedSpec(
            feed_id="rto_wind_actual",
            display_name="RTO Wind Actual",
            loader=LOADER_SYNTHETIC_RTO_FORECASTS,
            value_column="wind_actual_mw",
            pane="forecast",
            unit="MW",
        ),
        FeedSpec(
            feed_id="rto_net_load_actual",
            display_name="RTO Net Load Actual",
            loader=LOADER_SYNTHETIC_RTO_FORECASTS,
            value_column="net_load_actual_mw",
            pane="forecast",
            unit="MW",
        ),
        FeedSpec(
            feed_id="rto_load_forecast_da",
            display_name="RTO Load Forecast DA",
            loader=LOADER_SYNTHETIC_RTO_FORECASTS,
            value_column="load_forecast_da_mw",
            pane="forecast",
            unit="MW",
        ),
        FeedSpec(
            feed_id="rto_load_forecast_1h",
            display_name="RTO Load Forecast 1h",
            loader=LOADER_SYNTHETIC_RTO_FORECASTS,
            value_column="load_forecast_1h_mw",
            pane="forecast",
            unit="MW",
        ),
        FeedSpec(
            feed_id="rto_load_forecast_asof",
            display_name="RTO Load Forecast As-Of",
            loader=LOADER_SYNTHETIC_RTO_FORECASTS,
            value_column="load_forecast_asof_mw",
            pane="forecast",
            unit="MW",
            has_revisions=True,
        ),
        FeedSpec(
            feed_id="rto_solar_forecast_da",
            display_name="RTO Solar Forecast DA",
            loader=LOADER_SYNTHETIC_RTO_FORECASTS,
            value_column="solar_forecast_da_mw",
            pane="forecast",
            unit="MW",
        ),
        FeedSpec(
            feed_id="rto_solar_forecast_1h",
            display_name="RTO Solar Forecast 1h",
            loader=LOADER_SYNTHETIC_RTO_FORECASTS,
            value_column="solar_forecast_1h_mw",
            pane="forecast",
            unit="MW",
        ),
        FeedSpec(
            feed_id="rto_solar_forecast_asof",
            display_name="RTO Solar Forecast As-Of",
            loader=LOADER_SYNTHETIC_RTO_FORECASTS,
            value_column="solar_forecast_asof_mw",
            pane="forecast",
            unit="MW",
            has_revisions=True,
        ),
        FeedSpec(
            feed_id="rto_wind_forecast_da",
            display_name="RTO Wind Forecast DA",
            loader=LOADER_SYNTHETIC_RTO_FORECASTS,
            value_column="wind_forecast_da_mw",
            pane="forecast",
            unit="MW",
        ),
        FeedSpec(
            feed_id="rto_wind_forecast_1h",
            display_name="RTO Wind Forecast 1h",
            loader=LOADER_SYNTHETIC_RTO_FORECASTS,
            value_column="wind_forecast_1h_mw",
            pane="forecast",
            unit="MW",
        ),
        FeedSpec(
            feed_id="rto_wind_forecast_asof",
            display_name="RTO Wind Forecast As-Of",
            loader=LOADER_SYNTHETIC_RTO_FORECASTS,
            value_column="wind_forecast_asof_mw",
            pane="forecast",
            unit="MW",
            has_revisions=True,
        ),
        FeedSpec(
            feed_id="rto_net_load_forecast_da",
            display_name="RTO Net Load Forecast DA",
            loader=LOADER_SYNTHETIC_RTO_FORECASTS,
            value_column="net_load_forecast_da_mw",
            pane="forecast",
            unit="MW",
        ),
        FeedSpec(
            feed_id="rto_net_load_forecast_1h",
            display_name="RTO Net Load Forecast 1h",
            loader=LOADER_SYNTHETIC_RTO_FORECASTS,
            value_column="net_load_forecast_1h_mw",
            pane="forecast",
            unit="MW",
        ),
        FeedSpec(
            feed_id="rto_net_load_forecast_asof",
            display_name="RTO Net Load Forecast As-Of",
            loader=LOADER_SYNTHETIC_RTO_FORECASTS,
            value_column="net_load_forecast_asof_mw",
            pane="forecast",
            unit="MW",
            has_revisions=True,
        ),
    ]
}


def list_feeds() -> list[dict]:
    """Public metadata listing for /api/feeds."""
    return [
        {
            "feed_id": f.feed_id,
            "display_name": f.display_name,
            "pane": f.pane,
            "unit": f.unit,
            "zonal": f.loader.zonal,
            "has_revisions": f.has_revisions,
        }
        for f in FEEDS.values()
    ]
