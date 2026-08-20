"""Bitemporal data layer.

Loads PJM CSVs, stamps each row with a computed `published_at`, caches to
parquet. The only path the strategy has to read history is via
`view_as_of(df, as_of)` — strategies CANNOT see rows with `published_at > as_of`.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .errors import DataNotAvailableError

EPT = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

# Data lives outside the package: $PJM_DATA_ROOT if set, else ./data
# relative to the working directory. The fetch scripts write the same layout.
DATA_ROOT = Path(os.environ.get("PJM_DATA_ROOT", "data")).resolve()
RAW_PJM = DATA_ROOT / "raw" / "pjm"
CACHE = DATA_ROOT / "cache"

RTO_FORECAST_COLUMNS = [
    "datetime_beginning_utc",
    "datetime_beginning_ept",
    "load_actual_mw",
    "solar_actual_mw",
    "wind_actual_mw",
    "net_load_actual_mw",
    "forecast_stage",
    "published_at",
    "load_forecast_asof_mw",
    "solar_forecast_asof_mw",
    "wind_forecast_asof_mw",
    "net_load_forecast_asof_mw",
    "load_forecast_da_mw",
    "load_forecast_1h_mw",
    "solar_forecast_da_mw",
    "solar_forecast_1h_mw",
    "wind_forecast_da_mw",
    "wind_forecast_1h_mw",
    "net_load_forecast_da_mw",
    "net_load_forecast_1h_mw",
]


def empty_rto_forecasts() -> pd.DataFrame:
    """Schema-compatible empty RTO forecast feed for runs without forecast cache."""
    return pd.DataFrame(columns=RTO_FORECAST_COLUMNS)


def _parse_pjm_utc(series: pd.Series) -> pd.Series:
    """PJM dataminer UTC dates look like '1/1/2021 5:00:00 AM'. UTC is unambiguous."""
    return pd.to_datetime(series, format="%m/%d/%Y %I:%M:%S %p").dt.tz_localize(UTC)


def load_da_hrl_lmps(refresh: bool = False) -> pd.DataFrame:
    """DA hourly zone LMPs with computed `published_at` = D-1 13:30 EPT."""
    cache_path = CACHE / "da_hrl_lmps.parquet"
    if cache_path.exists() and not refresh:
        return pd.read_parquet(cache_path)

    src = RAW_PJM / "zone_lmps" / "da_hrl_lmps"
    files = sorted(src.glob("month=*.csv"))
    if not files:
        raise FileNotFoundError(f"no DA LMP CSVs at {src}")

    frames = [pd.read_csv(f) for f in files]
    df = pd.concat(frames, ignore_index=True)

    # Filter to the latest version PJM has posted for each (timestamp, pnode).
    df = df[df["row_is_current"].astype(bool)].copy()

    # UTC is the canonical timestamp; EPT is derived (zoneinfo handles DST).
    df["datetime_beginning_utc"] = _parse_pjm_utc(df["datetime_beginning_utc"])
    df["datetime_beginning_ept"] = df["datetime_beginning_utc"].dt.tz_convert(EPT)

    # PJM puts the zone label in `pnode_name` for ZONE-type rows. The dataminer
    # `zone` column is all-NaN; replace with pnode_name for ZONE rows, else empty.
    df["zone"] = df["pnode_name"].where(df["type"] == "ZONE", "")

    # published_at = D-1 at 13:30 EPT for operating day D.
    op_date_ept = df["datetime_beginning_ept"].dt.normalize()
    publish_ept = op_date_ept - pd.Timedelta(days=1) + pd.Timedelta(hours=13, minutes=30)
    df["published_at"] = publish_ept.dt.tz_convert(UTC)

    df = df.sort_values(["published_at", "datetime_beginning_utc", "pnode_id"])
    df = df.reset_index(drop=True)

    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    return df


def load_rt_fivemin_mnt_lmps(refresh: bool = False) -> pd.DataFrame:
    """RT 5-min zone LMPs (settlements-verified, used as live proxy per pjm-data.md §5.0.1).

    `published_at = MTU_end + 5 min = MTU_start + 10 min` — i.e. when a live
    operator would have seen the (preliminary) value. Per M11 §2.5.3.4 and
    §3.7.6, LPC posts simultaneously with energy every 5 min, so the prelim
    LMP lands ~5 min after MTU end (not 10 min).
    """
    cache_path = CACHE / "rt_fivemin_mnt_lmps.parquet"
    if cache_path.exists() and not refresh:
        return pd.read_parquet(cache_path)

    src = RAW_PJM / "zone_lmps" / "rt_fivemin_mnt_lmps"
    files = sorted(src.glob("month=*.csv"))
    if not files:
        raise FileNotFoundError(f"no RT LMP CSVs at {src}")

    frames = [pd.read_csv(f) for f in files]
    df = pd.concat(frames, ignore_index=True)

    df["datetime_beginning_utc"] = _parse_pjm_utc(df["datetime_beginning_utc"])
    df["datetime_beginning_ept"] = df["datetime_beginning_utc"].dt.tz_convert(EPT)
    df["zone"] = df["pnode_name"].where(df["type"] == "ZONE", "")

    # M11 §2.5.3.4 / §3.7.6 — LPC posts prelim ≈ MTU_end + 5 min
    df["published_at"] = df["datetime_beginning_utc"] + pd.Timedelta(minutes=10)

    df = df.sort_values(["published_at", "datetime_beginning_utc", "pnode_id"])
    df = df.reset_index(drop=True)

    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    return df


def load_reg_prices(refresh: bool = False) -> pd.DataFrame:
    """RT 5-min Regulation clearing prices (RMCCP + RMPCP) for the RTO market.

    Pulled from `reserve_market_results.locale=PJM_RTO,service=REG`. Pre-redesign
    (before 2022-10) these rows are hourly; from 2022-10 onward they're 5-min.

    `published_at = MTU_end + 5 min = MTU_start + 10 min` (same convention as
    RT LMP — verified-as-prelim per pjm-data.md §5.0.1; M11 §3.7.6).
    """
    cache_path = CACHE / "reg_prices.parquet"
    if cache_path.exists() and not refresh:
        return pd.read_parquet(cache_path)

    src = RAW_PJM / "ancillary_services" / "reserve_market_results"
    files = sorted(src.glob("quarter=*.csv"))
    if not files:
        raise FileNotFoundError(f"no reserve_market_results CSVs at {src}")

    frames = []
    for f in files:
        sub = pd.read_csv(f, dtype={"locale": "string", "service": "string"})
        sub = sub[(sub["locale"] == "PJM_RTO") & (sub["service"] == "REG")]
        frames.append(sub)
    df = pd.concat(frames, ignore_index=True)

    df["datetime_beginning_utc"] = _parse_pjm_utc(df["datetime_beginning_utc"])
    df["datetime_beginning_ept"] = df["datetime_beginning_utc"].dt.tz_convert(EPT)
    df = df.rename(columns={"reg_ccp": "rmccp", "reg_pcp": "rmpcp"})

    # M11 §2.5.3.4 / §3.7.6 — LPC posts prelim ≈ MTU_end + 5 min
    df["published_at"] = df["datetime_beginning_utc"] + pd.Timedelta(minutes=10)
    df = df[
        [
            "datetime_beginning_utc",
            "datetime_beginning_ept",
            "rmccp",
            "rmpcp",
            "mcp",
            "published_at",
        ]
    ]

    df = df.sort_values(["published_at", "datetime_beginning_utc"]).reset_index(drop=True)

    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    return df


def load_reg_market_results(refresh: bool = False) -> pd.DataFrame:
    """System-wide Regulation procurement summary: requirement, mileage, perf score.

    Pre-redesign rows are hourly with both `rega_*` and `regd_*` populated;
    post-redesign (2025-10-01+) rows are half-hourly with only `rega_*`.

    `published_at` uses `modified_datetime_utc` when present (PJM tells us when
    the row was last modified — that's the literal publish time); otherwise
    falls back to assignment-block-start − 10 min per M11 §3.7.5 (ASO posts
    results no later than 10 min before the start of the operating interval).
    """
    cache_path = CACHE / "reg_market_results.parquet"
    if cache_path.exists() and not refresh:
        return pd.read_parquet(cache_path)

    src = RAW_PJM / "ancillary_services" / "reg_market_results"
    files = sorted(src.glob("quarter=*.csv"))
    if not files:
        raise FileNotFoundError(f"no reg_market_results CSVs at {src}")

    frames = [pd.read_csv(f) for f in files]
    df = pd.concat(frames, ignore_index=True)

    df["datetime_beginning_utc"] = _parse_pjm_utc(df["datetime_beginning_utc"])
    df["datetime_beginning_ept"] = df["datetime_beginning_utc"].dt.tz_convert(EPT)

    mod = pd.to_datetime(
        df["modified_datetime_utc"],
        format="%m/%d/%Y %I:%M:%S %p",
        errors="coerce",
    ).dt.tz_localize(UTC)
    # M11 §3.7.5 — ASO clears 30 min before each half-hour interval and posts
    # results no later than 10 min prior to its start. So when PJM omits
    # `modified_datetime_utc`, fall back to block_start − 10 min (e.g. for the
    # 14:00–14:30 block, publication ≈ 13:50).
    fallback = df["datetime_beginning_utc"] - pd.Timedelta(minutes=10)
    df["published_at"] = mod.fillna(fallback)

    df = df.sort_values(["published_at", "datetime_beginning_utc"]).reset_index(drop=True)

    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    return df


def load_da_sr_prices(refresh: bool = False) -> pd.DataFrame:
    """DA Synchronized Reserve clearing prices (RTO Sub-Zone, hourly).

    Pulled from `da_reserve_market_results` filtered to
    (locale='PJM RTO Reserve Zone', service='Synchronized Reserve').
    Note: DA file uses long-form locale "PJM RTO Reserve Zone" while RT uses
    short-form "PJM_RTO" — handled separately in `load_rt_sr_prices`.

    `published_at = D-1 13:30 EPT` for operating day D, same as DA energy.
    """
    cache_path = CACHE / "da_sr_prices.parquet"
    if cache_path.exists() and not refresh:
        return pd.read_parquet(cache_path)

    src = RAW_PJM / "ancillary_services" / "da_reserve_market_results"
    files = sorted(src.glob("quarter=*.csv"))
    if not files:
        raise FileNotFoundError(f"no da_reserve_market_results CSVs at {src}")

    frames = []
    for f in files:
        sub = pd.read_csv(f, dtype={"locale": "string", "service": "string"}, low_memory=False)
        sub = sub[
            (sub["locale"] == "PJM RTO Reserve Zone") & (sub["service"] == "Synchronized Reserve")
        ]
        frames.append(sub)
    df = pd.concat(frames, ignore_index=True)

    df["datetime_beginning_utc"] = _parse_pjm_utc(df["datetime_beginning_utc"])
    df["datetime_beginning_ept"] = df["datetime_beginning_utc"].dt.tz_convert(EPT)

    op_date_ept = df["datetime_beginning_ept"].dt.normalize()
    publish_ept = op_date_ept - pd.Timedelta(days=1) + pd.Timedelta(hours=13, minutes=30)
    df["published_at"] = publish_ept.dt.tz_convert(UTC)

    df = df[
        [
            "datetime_beginning_utc",
            "datetime_beginning_ept",
            "mcp",
            "mcp_capped",
            "published_at",
        ]
    ]
    df = df.sort_values(["published_at", "datetime_beginning_utc"]).reset_index(drop=True)

    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    return df


def load_rt_sr_prices(refresh: bool = False) -> pd.DataFrame:
    """RT 5-min Synchronized Reserve clearing prices (RTO).

    Pulled from `reserve_market_results` filtered to
    (locale='PJM_RTO', service='SR'). Pre-2022-10 these rows are hourly;
    from 2022-10 onward 5-min.

    `published_at = MTU_end + 5 min = MTU_start + 10 min` (verified-as-prelim
    convention per pjm-data.md §5.0.1; M11 §3.7.6, same as RT LMP and Reg prices).
    """
    cache_path = CACHE / "rt_sr_prices.parquet"
    if cache_path.exists() and not refresh:
        return pd.read_parquet(cache_path)

    src = RAW_PJM / "ancillary_services" / "reserve_market_results"
    files = sorted(src.glob("quarter=*.csv"))
    if not files:
        raise FileNotFoundError(f"no reserve_market_results CSVs at {src}")

    frames = []
    for f in files:
        sub = pd.read_csv(f, dtype={"locale": "string", "service": "string"}, low_memory=False)
        sub = sub[(sub["locale"] == "PJM_RTO") & (sub["service"] == "SR")]
        frames.append(sub)
    df = pd.concat(frames, ignore_index=True)

    df["datetime_beginning_utc"] = _parse_pjm_utc(df["datetime_beginning_utc"])
    df["datetime_beginning_ept"] = df["datetime_beginning_utc"].dt.tz_convert(EPT)
    # M11 §2.5.3.4 / §3.7.6 — LPC posts prelim ≈ MTU_end + 5 min
    df["published_at"] = df["datetime_beginning_utc"] + pd.Timedelta(minutes=10)

    df = df[
        [
            "datetime_beginning_utc",
            "datetime_beginning_ept",
            "mcp",
            "mcp_capped",
            "published_at",
        ]
    ]
    df = df.sort_values(["published_at", "datetime_beginning_utc"]).reset_index(drop=True)

    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    return df


def load_da_sec_prices(refresh: bool = False) -> pd.DataFrame:
    """DA 30-Minute (Secondary) Reserve clearing prices (RTO Sub-Zone, hourly).

    Mirrors `load_da_sr_prices` — pulled from `da_reserve_market_results`
    filtered to (locale='PJM RTO Reserve Zone', service='Thirty Minutes
    Reserve'). Often clears at $0 (PJM has plenty of idle 30-min capacity)
    but kept as a real product for completeness.

    `published_at = D-1 13:30 EPT`.
    """
    cache_path = CACHE / "da_sec_prices.parquet"
    if cache_path.exists() and not refresh:
        return pd.read_parquet(cache_path)

    src = RAW_PJM / "ancillary_services" / "da_reserve_market_results"
    files = sorted(src.glob("quarter=*.csv"))
    if not files:
        raise FileNotFoundError(f"no da_reserve_market_results CSVs at {src}")

    frames = []
    for f in files:
        sub = pd.read_csv(f, dtype={"locale": "string", "service": "string"}, low_memory=False)
        sub = sub[
            (sub["locale"] == "PJM RTO Reserve Zone") & (sub["service"] == "Thirty Minutes Reserve")
        ]
        frames.append(sub)
    df = pd.concat(frames, ignore_index=True)

    df["datetime_beginning_utc"] = _parse_pjm_utc(df["datetime_beginning_utc"])
    df["datetime_beginning_ept"] = df["datetime_beginning_utc"].dt.tz_convert(EPT)

    op_date_ept = df["datetime_beginning_ept"].dt.normalize()
    publish_ept = op_date_ept - pd.Timedelta(days=1) + pd.Timedelta(hours=13, minutes=30)
    df["published_at"] = publish_ept.dt.tz_convert(UTC)

    df = df[
        [
            "datetime_beginning_utc",
            "datetime_beginning_ept",
            "mcp",
            "mcp_capped",
            "published_at",
        ]
    ]
    df = df.sort_values(["published_at", "datetime_beginning_utc"]).reset_index(drop=True)

    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    return df


def load_rt_sec_prices(refresh: bool = False) -> pd.DataFrame:
    """RT 5-min 30-Minute (Secondary) Reserve clearing prices (RTO).

    Mirrors `load_rt_sr_prices` — pulled from `reserve_market_results`
    filtered to (locale='PJM_RTO', service='30MIN').

    `published_at = MTU_end + 5 min = MTU_start + 10 min` (M11 §3.7.6).
    """
    cache_path = CACHE / "rt_sec_prices.parquet"
    if cache_path.exists() and not refresh:
        return pd.read_parquet(cache_path)

    src = RAW_PJM / "ancillary_services" / "reserve_market_results"
    files = sorted(src.glob("quarter=*.csv"))
    if not files:
        raise FileNotFoundError(f"no reserve_market_results CSVs at {src}")

    frames = []
    for f in files:
        sub = pd.read_csv(f, dtype={"locale": "string", "service": "string"}, low_memory=False)
        sub = sub[(sub["locale"] == "PJM_RTO") & (sub["service"] == "30MIN")]
        frames.append(sub)
    df = pd.concat(frames, ignore_index=True)

    df["datetime_beginning_utc"] = _parse_pjm_utc(df["datetime_beginning_utc"])
    df["datetime_beginning_ept"] = df["datetime_beginning_utc"].dt.tz_convert(EPT)
    # M11 §2.5.3.4 / §3.7.6 — LPC posts prelim ≈ MTU_end + 5 min
    df["published_at"] = df["datetime_beginning_utc"] + pd.Timedelta(minutes=10)

    df = df[
        [
            "datetime_beginning_utc",
            "datetime_beginning_ept",
            "mcp",
            "mcp_capped",
            "published_at",
        ]
    ]
    df = df.sort_values(["published_at", "datetime_beginning_utc"]).reset_index(drop=True)

    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    return df


def load_rto_forecasts(refresh: bool = False) -> pd.DataFrame:
    """Load materialized RTO load/solar/wind forecast revisions.

    The forecast generator lives with the viz feed loaders because the first
    consumer was the visualizer. The engine reads the materialized cache here
    so strategies can use the same bitemporal ``published_at`` contract as
    PJM price feeds without importing viz-server code.
    """
    cache_path = CACHE / "viz_synthetic_rto_forecasts.parquet"
    if not cache_path.exists() or refresh:
        raise FileNotFoundError(
            f"RTO forecast cache not found at {cache_path}; start the viz server "
            "or refresh load_synthetic_rto_forecasts() before running "
            "forecast-driven strategies."
        )
    df = pd.read_parquet(cache_path)
    return df.sort_values(["published_at", "datetime_beginning_utc", "forecast_stage"]).reset_index(
        drop=True
    )


def _parse_pjm_ept(series: pd.Series) -> pd.Series:
    """Parse PJM dataminer EPT timestamps and convert to UTC. Same format as
    `_parse_pjm_utc` but the source values are wall-clock EPT (with implicit
    DST). `tz_localize` raises on the spring-forward gap and on fall-back
    duplicates — neither is expected in SR-event data (events are
    instantaneous and PJM stamps them unambiguously); use ambiguous='infer'
    + nonexistent='shift_forward' as defensive defaults."""
    parsed = pd.to_datetime(series, format="%m/%d/%Y %I:%M:%S %p")
    return parsed.dt.tz_localize(EPT, ambiguous="infer", nonexistent="shift_forward").dt.tz_convert(
        UTC
    )


def load_sr_events(refresh: bool = False) -> pd.DataFrame:
    """Synchronized Reserve event log (pjm-data.md §2.2, §7.2).

    Loads `data/raw/pjm/ancillary_services/sync_reserve_events/quarter=*.csv`,
    dedups the RTO + sub-zone pair rows (PJM stamps the same event twice when
    a sub-zone is also active), parses EPT timestamps to UTC, returns one row
    per unique (event_start_utc, event_end_utc).

    Output columns:
      - `event_start_utc`, `event_end_utc` — tz-aware UTC
      - `duration_minutes` — float, derived from the event window
      - `sub_zones` — comma-joined sub-zones that were also obligated
        (e.g., "MidAtlantic-Dominion (MAD)" or "" for RTO-only). RTO-zone
        assets are obligated by every event regardless of sub_zones value.
      - `percent_deployed` — system-level deployment % (typically 100)
      - `published_at` — `event_end_utc + 5 min` (PJM publishes the event
        record shortly after it ends; the strategy can't see future events)

    Engine convention: only RTO-registered assets call this loader. Sub-zone-
    only events still appear in the table because the same event may obligate
    both RTO and a sub-zone; we keep the RTO row as canonical.
    """
    cache_path = CACHE / "sr_events.parquet"
    if cache_path.exists() and not refresh:
        return pd.read_parquet(cache_path)

    src = RAW_PJM / "ancillary_services" / "sync_reserve_events"
    files = sorted(src.glob("quarter=*.csv"))
    if not files:
        raise FileNotFoundError(f"no sync_reserve_events CSVs at {src}")

    frames = []
    for f in files:
        sub = pd.read_csv(
            f,
            dtype={
                "synchronized_reserve_zone": "string",
                "synchronized_sub_zone": "string",
            },
            low_memory=False,
        )
        if not sub.empty:
            frames.append(sub)
    if not frames:
        # Empty quarters are legal — return an empty frame with the right
        # columns so callers can `iterrows()` without special-casing.
        return pd.DataFrame(
            columns=[
                "event_start_utc",
                "event_end_utc",
                "duration_minutes",
                "sub_zones",
                "percent_deployed",
                "published_at",
            ]
        )
    df = pd.concat(frames, ignore_index=True)

    df["event_start_utc"] = _parse_pjm_ept(df["event_start_ept"])
    df["event_end_utc"] = _parse_pjm_ept(df["event_end_ept"])

    df["synchronized_sub_zone"] = df["synchronized_sub_zone"].fillna("")
    grouped = df.groupby(["event_start_utc", "event_end_utc"], as_index=False).agg(
        sub_zones=("synchronized_sub_zone", lambda s: ",".join(sorted(v for v in s.unique() if v))),
        percent_deployed=("percent_deployed", "max"),
    )
    grouped["duration_minutes"] = (
        grouped["event_end_utc"] - grouped["event_start_utc"]
    ).dt.total_seconds() / 60.0
    grouped["published_at"] = grouped["event_end_utc"] + pd.Timedelta(minutes=5)

    # Sort by `published_at` so `view_as_of`'s searchsorted contract holds for
    # this feed too — currently no caller threads it through `view_as_of`, but
    # the invariant must be intact for any future bitemporal use.
    grouped = grouped.sort_values(["published_at", "event_start_utc"]).reset_index(drop=True)

    CACHE.mkdir(parents=True, exist_ok=True)
    grouped.to_parquet(cache_path, index=False)
    return grouped


def load_rpm_clearing_prices(refresh: bool = False) -> pd.DataFrame:
    """RPM Base Residual Auction clearing prices, per (Delivery Year, LDA).

    Reads the normalized CSV produced by `scripts/fetch_pjm_capacity_market.py`
    (`resource_clearing_prices_long.csv`), filters to BRA + CP product type
    (the post-2018 Capacity Performance product), parses the "YYYY/YYYY"
    delivery-year string into the start-year int (DY 2025/26 → 2025), and
    returns one row per (`dy_start_year`, `lda`) combination.

    PJM Delivery Year runs June 1 → May 31. So calendar months Jun-Dec of
    year Y belong to DY Y; months Jan-May of year Y belong to DY Y-1. Use
    `dy_start_year_for_month(year, month)` to map a settlement month to
    the right DY.

    Output columns: `dy_start_year`, `lda`, `price_per_mw_day`.

    Pre-2018 BRAs cleared without a CP product (Annual / Limited / Ext Summer
    instead). Those rows are excluded. If pre-2018 backtests are added, this
    loader needs an equivalent filter for the older product mix.
    """
    cache_path = CACHE / "rpm_clearing_prices.parquet"
    if cache_path.exists() and not refresh:
        return pd.read_parquet(cache_path)

    src = RAW_PJM / "capacity_market" / "resource_clearing_prices_long.csv"
    if not src.exists():
        raise FileNotFoundError(
            f"no RPM clearing prices at {src}. "
            f"Run scripts/fetch_pjm_capacity_market.py to populate."
        )

    df = pd.read_csv(src)
    df = df[(df["auction"] == "BRA") & (df["capacity_product_type"] == "CP")]
    df = df.dropna(subset=["resource_clearing_price_mw_day"]).copy()

    # "2025/2026" → 2025
    df["dy_start_year"] = df["delivery_year"].str.split("/").str[0].astype(int)
    df = df.rename(columns={"resource_clearing_price_mw_day": "price_per_mw_day"})
    df = df[["dy_start_year", "lda", "price_per_mw_day"]]
    df = df.sort_values(["dy_start_year", "lda"]).reset_index(drop=True)

    CACHE.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path, index=False)
    return df


def dy_start_year_for_month(year: int, month: int) -> int:
    """Map a calendar (year, month) to the PJM Delivery Year start year.

    DY runs June 1 → May 31. Examples:
      (2025, 6) → 2025  (DY 2025/26)
      (2025, 12) → 2025 (DY 2025/26)
      (2026, 1) → 2025  (DY 2025/26, second half)
      (2026, 5) → 2025  (DY 2025/26, last month)
      (2026, 6) → 2026  (DY 2026/27 begins)
    """
    return year if month >= 6 else year - 1


def view_as_of(df: pd.DataFrame, as_of: datetime) -> pd.DataFrame:
    """The adversarial-precision filter. Strategies see only this.

    Loaders sort `published_at` ascending, so we use `searchsorted` for an
    O(log N) cutoff lookup + zero-copy slice instead of an O(N) boolean mask.
    The monotonicity assert below is what makes that searchsorted correct —
    it catches future loaders that forget to sort by `published_at`.
    """
    if as_of.tzinfo is None:
        raise DataNotAvailableError(f"as_of must be tz-aware, got naive {as_of!r}")
    if not df.empty:
        assert df["published_at"].is_monotonic_increasing, (
            "view_as_of requires the input frame to be sorted by published_at "
            "ascending (searchsorted contract). Loader is missing the sort."
        )
    cutoff = df["published_at"].searchsorted(pd.Timestamp(as_of), side="right")
    return df.iloc[:cutoff]


class CachedView:
    """Memoize `view_as_of` by cutoff index.

    The bitemporal filter is event-driven: between consecutive events, the
    DA cutoff often doesn't move (DA data publishes once at the daily gate),
    and the RT cutoff stays put for the 15-min publication lag. The naïve
    runner rebuilt the slice on every event — 17k slices over a 30-day window.
    Caching by cutoff collapses that to ~1 slice per published-at boundary.
    """

    def __init__(self, df: pd.DataFrame) -> None:
        self.df = df
        self._published = df["published_at"]
        self._last_cutoff: int = -1
        self._last_view: pd.DataFrame | None = None

    def at(self, as_of: datetime) -> pd.DataFrame:
        if as_of.tzinfo is None:
            raise DataNotAvailableError(f"as_of must be tz-aware, got naive {as_of!r}")
        cutoff = int(self._published.searchsorted(pd.Timestamp(as_of), side="right"))
        if cutoff == self._last_cutoff and self._last_view is not None:
            return self._last_view
        self._last_cutoff = cutoff
        self._last_view = self.df.iloc[:cutoff]
        return self._last_view
