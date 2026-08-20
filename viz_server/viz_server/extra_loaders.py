"""Viz-only extra feed loaders.

These mirror the structure of `pjm_engine.data` loaders (return a DataFrame
sorted by `published_at` with `datetime_beginning_utc` + value columns), but
live in viz_server so we don't touch the engine. The engine remains the
single source of truth for backtests; these are read-only feeds for
research replay.

Cache location: `<data root>/cache/viz_<name>.parquet` (prefix avoids
collision if the engine someday adds an equivalent loader under its own
name). Paths come from `pjm_engine.data` ($PJM_DATA_ROOT, else ./data).
"""

from __future__ import annotations

from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from pjm_engine.data import CACHE, RAW_PJM

UTC = ZoneInfo("UTC")
EPT = ZoneInfo("America/New_York")


def _parse_pjm_utc(series: pd.Series) -> pd.Series:
    """PJM dataminer UTC dates: '1/1/2021 5:00:00 AM'."""
    return pd.to_datetime(series, format="%m/%d/%Y %I:%M:%S %p").dt.tz_localize(UTC)


# Map PJM load-data short codes to the long codes used by zone LMPs and our
# zone selector. Covers every load_area appearing in hrl_load_metered
# (2021-2026) that corresponds to an LMP zone; AEP sub-areas and ATSI member
# companies collapse to their transmission zone. Anything not in the map
# (munis/coops like EASTON, SMECO, UGI, VMEU) keeps its load_area code as-is,
# so users can also pick those directly if they want.
LOAD_AREA_TO_ZONE = {
    "AECO": "AECO",  # already matches
    "AEPAPT": "AEP",  # Appalachian Power
    "AEPIMP": "AEP",  # Indiana Michigan Power
    "AEPKPT": "AEP",  # Kentucky Power
    "AEPOPT": "AEP",  # AEP Ohio
    "AP": "APS",
    "BC": "BGE",
    "CE": "COMED",
    "DAY": "DAY",  # already matches
    "DEOK": "DEOK",  # already matches
    "DOM": "DOM",  # already matches
    "DPLCO": "DPL",
    "DUQ": "DUQ",  # already matches
    "EKPC": "EKPC",  # already matches
    "JC": "JCPL",
    "ME": "METED",
    "OE": "ATSI",  # Ohio Edison
    "OVEC": "OVEC",  # already matches
    "PAPWR": "ATSI",  # Penn Power
    "PE": "PECO",
    "PEPCO": "PEPCO",  # already matches
    "PL": "PPL",  # legacy short code
    "PLCO": "PPL",
    "PN": "PENELEC",
    "PS": "PSEG",
    "RECO": "RECO",  # already matches
    "RTO": "PJM-RTO",
}


def load_hrl_load_metered(refresh: bool = False) -> pd.DataFrame:
    """Hourly metered load by zone.

    Schema in:  datetime_beginning_utc, ..., zone, load_area, mw, is_verified
    Schema out: datetime_beginning_utc, datetime_beginning_ept, zone, mw,
                is_verified, published_at

    `published_at = MTU_start + 1h` — operational visibility shortly after
    each hour ends. Verified values land later (the `is_verified` flag), but
    PJM publishes preliminary metered load with a short lag.
    """
    cache_path = CACHE / "viz_load_metered.parquet"
    if cache_path.exists() and not refresh:
        return pd.read_parquet(cache_path)

    src = RAW_PJM / "context" / "hrl_load_metered"
    files = sorted(src.glob("year=*.csv"))
    if not files:
        raise FileNotFoundError(f"no hrl_load_metered CSVs at {src}")

    frames = [pd.read_csv(f, low_memory=False) for f in files]
    df = pd.concat(frames, ignore_index=True)

    df["datetime_beginning_utc"] = _parse_pjm_utc(df["datetime_beginning_utc"])
    df["datetime_beginning_ept"] = df["datetime_beginning_utc"].dt.tz_convert(EPT)

    # Normalize load_area into the zone naming used by LMP feeds.
    df["zone"] = df["load_area"].map(LOAD_AREA_TO_ZONE).fillna(df["load_area"])

    df["published_at"] = df["datetime_beginning_utc"] + pd.Timedelta(hours=1)

    df = df[
        [
            "datetime_beginning_utc",
            "datetime_beginning_ept",
            "zone",
            "mw",
            "is_verified",
            "published_at",
        ]
    ]
    df = df.sort_values(["published_at", "datetime_beginning_utc", "zone"])
    df = df.reset_index(drop=True)

    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    return df


def _load_gen_area_rto(
    csv_dir: str,
    out_name: str,
    value_col_in: str,
    value_col_out: str,
    refresh: bool,
) -> pd.DataFrame:
    """Shared loader for solar_gen / wind_gen filtered to area='RTO'.

    The raw data has multiple areas (MIDATL, RTO, SOUTH, WEST, RFC, OTHER).
    For viz we only carry RTO (system-wide aggregate) — sub-region feeds
    can be added later if needed. RTO-wide means non-zonal w.r.t. LMP zones.
    """
    cache_path = CACHE / f"viz_{out_name}.parquet"
    if cache_path.exists() and not refresh:
        return pd.read_parquet(cache_path)

    src = RAW_PJM / "context" / csv_dir
    files = sorted(src.glob("year=*.csv"))
    if not files:
        raise FileNotFoundError(f"no {csv_dir} CSVs at {src}")

    frames = [pd.read_csv(f, low_memory=False) for f in files]
    df = pd.concat(frames, ignore_index=True)
    df = df[df["area"] == "RTO"].copy()

    df["datetime_beginning_utc"] = _parse_pjm_utc(df["datetime_beginning_utc"])
    df["datetime_beginning_ept"] = df["datetime_beginning_utc"].dt.tz_convert(EPT)
    df = df.rename(columns={value_col_in: value_col_out})

    # Operationally visible ~1 h after the hour completes.
    df["published_at"] = df["datetime_beginning_utc"] + pd.Timedelta(minutes=60)

    df = df[
        [
            "datetime_beginning_utc",
            "datetime_beginning_ept",
            value_col_out,
            "published_at",
        ]
    ]
    df = df.sort_values(["published_at", "datetime_beginning_utc"])
    df = df.reset_index(drop=True)

    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    return df


def load_solar_gen(refresh: bool = False) -> pd.DataFrame:
    """Hourly RTO-wide solar generation. Non-zonal.

    `published_at = MTU_start + 1h`.
    """
    return _load_gen_area_rto("solar_gen", "solar_gen", "solar_generation_mw", "solar_mw", refresh)


def load_wind_gen(refresh: bool = False) -> pd.DataFrame:
    """Hourly RTO-wide wind generation. Non-zonal.

    `published_at = MTU_start + 1h`.
    """
    return _load_gen_area_rto("wind_gen", "wind_gen", "wind_generation_mw", "wind_mw", refresh)


def load_gen_outages(refresh: bool = False) -> pd.DataFrame:
    """Daily PJM RTO generation outages by type.

    Source: `gen_outages_by_type` — daily forecast publishes one row per
    region (Mid Atlantic - Dominion, Western, PJM RTO). We keep the
    pre-aggregated `PJM RTO` rows and split the value into total / forced /
    planned / maintenance MW. Each forecast_execution_date publishes the
    outage forecast for the current and future days.

    Schema out: datetime_beginning_utc (forecast_date EPT-midnight in UTC),
                datetime_beginning_ept, total_mw, forced_mw, planned_mw,
                maintenance_mw, published_at
    """
    cache_path = CACHE / "viz_gen_outages.parquet"
    if cache_path.exists() and not refresh:
        return pd.read_parquet(cache_path)

    src = RAW_PJM / "context" / "gen_outages_by_type"
    files = sorted(src.glob("year=*.csv"))
    if not files:
        raise FileNotFoundError(f"no gen_outages_by_type CSVs at {src}")

    frames = [pd.read_csv(f, low_memory=False) for f in files]
    df = pd.concat(frames, ignore_index=True)
    df = df[df["region"] == "PJM RTO"].copy()

    # Both date columns are PJM-EPT midnights ("1/1/2025 12:00:00 AM").
    # forecast_date  = the calendar day the outage MW value applies to.
    # forecast_execution_date_ept = the day this forecast was published.
    fd = pd.to_datetime(df["forecast_date"], format="%m/%d/%Y %I:%M:%S %p")
    fe = pd.to_datetime(df["forecast_execution_date_ept"], format="%m/%d/%Y %I:%M:%S %p")
    df["datetime_beginning_ept"] = fd.dt.tz_localize(EPT)
    df["datetime_beginning_utc"] = df["datetime_beginning_ept"].dt.tz_convert(UTC)
    df["published_at"] = fe.dt.tz_localize(EPT).dt.tz_convert(UTC)

    df = df.rename(
        columns={
            "total_outages_mw": "total_mw",
            "forced_outages_mw": "forced_mw",
            "planned_outages_mw": "planned_mw",
            "maintenance_outages_mw": "maintenance_mw",
        }
    )

    df = df[
        [
            "datetime_beginning_utc",
            "datetime_beginning_ept",
            "total_mw",
            "forced_mw",
            "planned_mw",
            "maintenance_mw",
            "published_at",
        ]
    ]
    df = df.sort_values(["published_at", "datetime_beginning_utc"])
    df = df.reset_index(drop=True)

    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    return df


def _load_rto_load_actual() -> pd.DataFrame:
    src = RAW_PJM / "context" / "hrl_load_metered"
    files = sorted(src.glob("year=*.csv"))
    if not files:
        raise FileNotFoundError(f"no hrl_load_metered CSVs at {src}")

    frames = []
    for f in files:
        sub = pd.read_csv(f, low_memory=False)
        sub = sub[sub["load_area"] == "RTO"]
        frames.append(sub)
    df = pd.concat(frames, ignore_index=True)
    df["datetime_beginning_utc"] = _parse_pjm_utc(df["datetime_beginning_utc"])
    return df[["datetime_beginning_utc", "mw"]].rename(columns={"mw": "load_actual_mw"})


def _load_rto_gen_actual(csv_dir: str, value_col: str, out_col: str) -> pd.DataFrame:
    src = RAW_PJM / "context" / csv_dir
    files = sorted(src.glob("year=*.csv"))
    if not files:
        raise FileNotFoundError(f"no {csv_dir} CSVs at {src}")

    frames = []
    for f in files:
        sub = pd.read_csv(f, low_memory=False)
        sub = sub[sub["area"] == "RTO"]
        frames.append(sub)
    df = pd.concat(frames, ignore_index=True)
    df["datetime_beginning_utc"] = _parse_pjm_utc(df["datetime_beginning_utc"])
    return df[["datetime_beginning_utc", value_col]].rename(columns={value_col: out_col})


def _forecast_noise(
    actual: np.ndarray,
    *,
    peak: float,
    sigma_frac: float,
    seed: int,
    mode: str,
) -> np.ndarray:
    """Deterministic synthetic forecast error with daily weather-regime misses."""
    rng = np.random.default_rng(seed)
    white = rng.normal(0.0, 1.0, len(actual))
    ar = np.empty(len(actual), dtype=float)
    prev = 0.0
    for i, v in enumerate(white):
        prev = 0.72 * prev + 0.28 * v
        ar[i] = prev
    daily = np.repeat(rng.normal(0.0, 1.0, int(np.ceil(len(actual) / 24))), 24)[: len(actual)]
    regime = 0.60 * daily + 0.40 * ar

    if mode == "load":
        scale = np.maximum(actual, 1.0) * sigma_frac
    elif mode == "solar":
        daylight = np.clip(actual / max(peak, 1.0), 0.0, 1.0)
        scale = peak * sigma_frac * (0.05 + np.sqrt(daylight))
    else:  # wind
        scale = (
            peak * sigma_frac * (0.25 + 0.75 * np.sqrt(np.clip(actual / max(peak, 1.0), 0.0, 1.0)))
        )
    return regime * scale


def _forecast_revisions(
    actual: np.ndarray,
    *,
    peak: float,
    mode: str,
    base_sigma: float,
    residual_sigmas: dict[str, float],
    seed: int,
) -> dict[str, np.ndarray]:
    """Build revisions that mostly converge instead of jumping independently.

    DA gets the full day/weather miss. Later revisions retain a shrinking share
    of that same miss plus a small residual error, so the "latest at decision"
    line moves like an updated forecast rather than a new random draw.
    """
    base_error = _forecast_noise(actual, peak=peak, sigma_frac=base_sigma, seed=seed, mode=mode)
    retained = {
        "da": 1.00,
        "6h": 0.72,
        "3h": 0.52,
        "1h": 0.34,
        "15m": 0.20,
    }
    out: dict[str, np.ndarray] = {}
    for idx, stage in enumerate(["da", "6h", "3h", "1h", "15m"]):
        residual_sigma = residual_sigmas.get(stage, 0.0)
        residual = (
            0.0
            if residual_sigma == 0.0
            else _forecast_noise(
                actual,
                peak=peak,
                sigma_frac=residual_sigma,
                seed=seed + 1000 + idx,
                mode=mode,
            )
        )
        out[stage] = np.maximum(0.0, actual + retained[stage] * base_error + residual)
    return out


def load_synthetic_rto_forecasts(refresh: bool = False) -> pd.DataFrame:
    """Synthetic RTO load/renewable forecasts from historical actuals.

    The frame has one row per forecast revision. `published_at` is when that
    revision would have been knowable; `datetime_beginning_utc` is the target
    operating hour. Fixed-horizon columns (`*_forecast_da_mw`,
    `*_forecast_1h_mw`) are populated only on their matching revision rows.
    `*_forecast_asof_mw` is populated on every forecast revision and is served
    with `has_revisions=True` so `/api/series` keeps the latest revision as of
    the selected decision time.
    """
    cache_path = CACHE / "viz_synthetic_rto_forecasts.parquet"
    if cache_path.exists() and not refresh:
        return pd.read_parquet(cache_path)

    load = _load_rto_load_actual()
    solar = _load_rto_gen_actual("solar_gen", "solar_generation_mw", "solar_actual_mw")
    wind = _load_rto_gen_actual("wind_gen", "wind_generation_mw", "wind_actual_mw")

    actual = (
        load.merge(solar, on="datetime_beginning_utc", how="inner")
        .merge(wind, on="datetime_beginning_utc", how="inner")
        .sort_values("datetime_beginning_utc")
        .reset_index(drop=True)
    )
    actual["datetime_beginning_ept"] = actual["datetime_beginning_utc"].dt.tz_convert(EPT)
    actual["net_load_actual_mw"] = (
        actual["load_actual_mw"] - actual["solar_actual_mw"] - actual["wind_actual_mw"]
    )

    stages = [
        ("da", None),
        ("6h", pd.Timedelta(hours=6)),
        ("3h", pd.Timedelta(hours=3)),
        ("1h", pd.Timedelta(hours=1)),
        ("15m", pd.Timedelta(minutes=15)),
    ]

    load_vals = actual["load_actual_mw"].to_numpy(dtype=float)
    solar_vals = actual["solar_actual_mw"].to_numpy(dtype=float)
    wind_vals = actual["wind_actual_mw"].to_numpy(dtype=float)
    solar_peak = float(np.nanquantile(solar_vals, 0.995))
    wind_peak = float(np.nanquantile(wind_vals, 0.995))
    load_revisions = _forecast_revisions(
        load_vals,
        peak=0.0,
        mode="load",
        base_sigma=0.065,
        residual_sigmas={"6h": 0.010, "3h": 0.009, "1h": 0.007, "15m": 0.005},
        seed=100,
    )
    solar_revisions = _forecast_revisions(
        solar_vals,
        peak=solar_peak,
        mode="solar",
        base_sigma=0.430,
        residual_sigmas={"6h": 0.060, "3h": 0.055, "1h": 0.045, "15m": 0.030},
        seed=200,
    )
    wind_revisions = _forecast_revisions(
        wind_vals,
        peak=wind_peak,
        mode="wind",
        base_sigma=0.480,
        residual_sigmas={"6h": 0.075, "3h": 0.065, "1h": 0.055, "15m": 0.040},
        seed=300,
    )

    pieces: list[pd.DataFrame] = []
    time_cols = [
        "datetime_beginning_utc",
        "datetime_beginning_ept",
    ]
    actual_cols = [
        "datetime_beginning_utc",
        "datetime_beginning_ept",
        "load_actual_mw",
        "solar_actual_mw",
        "wind_actual_mw",
        "net_load_actual_mw",
    ]

    for label, lead in stages:
        part = actual[time_cols].copy()
        for col in ["load_actual_mw", "solar_actual_mw", "wind_actual_mw", "net_load_actual_mw"]:
            part[col] = np.nan
        part["forecast_stage"] = label
        if lead is None:
            op_date_ept = part["datetime_beginning_ept"].dt.normalize()
            publish_ept = op_date_ept - pd.Timedelta(days=1) + pd.Timedelta(hours=10, minutes=45)
            part["published_at"] = publish_ept.dt.tz_convert(UTC)
        else:
            part["published_at"] = part["datetime_beginning_utc"] - lead

        load_fcst = load_revisions[label]
        solar_fcst = solar_revisions[label]
        wind_fcst = wind_revisions[label]
        net_fcst = load_fcst - solar_fcst - wind_fcst

        part["load_forecast_asof_mw"] = load_fcst
        part["solar_forecast_asof_mw"] = solar_fcst
        part["wind_forecast_asof_mw"] = wind_fcst
        part["net_load_forecast_asof_mw"] = net_fcst

        for prefix in ["load", "solar", "wind", "net_load"]:
            part[f"{prefix}_forecast_da_mw"] = np.nan
            part[f"{prefix}_forecast_1h_mw"] = np.nan
        if label == "da":
            part["load_forecast_da_mw"] = load_fcst
            part["solar_forecast_da_mw"] = solar_fcst
            part["wind_forecast_da_mw"] = wind_fcst
            part["net_load_forecast_da_mw"] = net_fcst
        elif label == "1h":
            part["load_forecast_1h_mw"] = load_fcst
            part["solar_forecast_1h_mw"] = solar_fcst
            part["wind_forecast_1h_mw"] = wind_fcst
            part["net_load_forecast_1h_mw"] = net_fcst
        pieces.append(part)

    actual_rows = actual[actual_cols].copy()
    actual_rows["forecast_stage"] = "actual"
    actual_rows["published_at"] = actual_rows["datetime_beginning_utc"] + pd.Timedelta(hours=1)
    for col in [
        "load_forecast_asof_mw",
        "solar_forecast_asof_mw",
        "wind_forecast_asof_mw",
        "net_load_forecast_asof_mw",
        "load_forecast_da_mw",
        "solar_forecast_da_mw",
        "wind_forecast_da_mw",
        "net_load_forecast_da_mw",
        "load_forecast_1h_mw",
        "solar_forecast_1h_mw",
        "wind_forecast_1h_mw",
        "net_load_forecast_1h_mw",
    ]:
        actual_rows[col] = np.nan
    pieces.append(actual_rows)

    df = pd.concat(pieces, ignore_index=True)
    df = df.sort_values(["published_at", "datetime_beginning_utc", "forecast_stage"])
    df = df.reset_index(drop=True)

    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    return df
