"""Generate a synthetic market-data cache so the quickstart runs with no API keys.

Writes schema-compatible parquet files for every feed `prepare_market_data`
loads, covering a configurable window of plausible (but fake) prices for one
zone. The engine can then run any strategy end-to-end — e.g. the
perfect-foresight benchmark:

    python evaluation/scripts/generate_synthetic_data.py
    python optimization/scripts/run_perfect_foresight.py

Output lands in $PJM_DATA_ROOT/cache (default ./data/cache). Real PJM data
fetched via scripts/ overwrites these files feed-by-feed (delete the cache
first to avoid mixing synthetic and real rows).

The prices are noise around simple daily shapes — good for exercising the
machinery and developing strategies offline, meaningless for revenue
estimates.
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from pjm_engine.data import CACHE
from pjm_engine.time_utils import half_hour_starts_utc, operating_hour_starts_utc

EPT = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
MTU_5MIN = timedelta(minutes=5)

ZONE = "PJM-RTO"


def _hours(start: date, days: int) -> pd.DatetimeIndex:
    out = []
    for i in range(days):
        out.extend(operating_hour_starts_utc(start + timedelta(days=i)))
    return pd.DatetimeIndex(out)


def _half_hours(start: date, days: int) -> pd.DatetimeIndex:
    out = []
    for i in range(days):
        out.extend(half_hour_starts_utc(start + timedelta(days=i)))
    return pd.DatetimeIndex(out)


def _mtus(hours: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return pd.DatetimeIndex([h + i * MTU_5MIN for h in hours for i in range(12)])


def _daily_shape(index: pd.DatetimeIndex, base: float, swing: float) -> np.ndarray:
    """Two-peak daily price shape (morning + evening) in EPT hours."""
    hod = np.array([ts.tz_convert(EPT).hour + ts.tz_convert(EPT).minute / 60 for ts in index])
    morning = np.exp(-((hod - 8.0) ** 2) / 8.0)
    evening = np.exp(-((hod - 19.0) ** 2) / 6.0)
    return base + swing * (0.6 * morning + evening)


def _da_publish(index: pd.DatetimeIndex) -> pd.Series:
    """DA feeds publish at D-1 13:30 EPT for operating day D."""
    ept = pd.Series(index.tz_convert(EPT), index=index)
    op_date = ept.dt.normalize()
    return (op_date - pd.Timedelta(days=1) + pd.Timedelta(hours=13, minutes=30)).dt.tz_convert(UTC)


def _price_frame(index: pd.DatetimeIndex, published_at: pd.Series) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "datetime_beginning_utc": index,
            "datetime_beginning_ept": index.tz_convert(EPT),
            "published_at": published_at.to_numpy(),
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=date(2025, 10, 1),
        help="first operating day (default 2025-10-01, post-redesign)",
    )
    parser.add_argument(
        "--days", type=int, default=14, help="number of operating days (default 14)"
    )
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    rng = np.random.default_rng(args.seed)
    hours = _hours(args.start, args.days)
    mtus = _mtus(hours)
    half_hours = _half_hours(args.start, args.days)
    CACHE.mkdir(parents=True, exist_ok=True)

    # DA hourly zone LMPs.
    da_lmp = _daily_shape(hours, base=28.0, swing=35.0) + rng.normal(0, 4, len(hours))
    da = _price_frame(hours, _da_publish(hours))
    da["zone"] = ZONE
    da["pnode_id"] = 1
    da["pnode_name"] = ZONE
    da["type"] = "ZONE"
    da["total_lmp_da"] = da_lmp
    da.sort_values(["published_at", "datetime_beginning_utc"]).to_parquet(
        CACHE / "da_hrl_lmps.parquet", index=False
    )

    # RT 5-min zone LMPs: DA shape + noise + occasional spikes.
    rt_base = np.repeat(da_lmp, 12)
    spikes = (rng.random(len(mtus)) < 0.01) * rng.uniform(50, 250, len(mtus))
    rt = _price_frame(mtus, pd.Series(mtus + pd.Timedelta(minutes=10), index=mtus))
    rt["zone"] = ZONE
    rt["pnode_id"] = 1
    rt["pnode_name"] = ZONE
    rt["type"] = "ZONE"
    rt["total_lmp_rt"] = rt_base + rng.normal(0, 7, len(mtus)) + spikes
    rt.sort_values(["published_at", "datetime_beginning_utc"]).to_parquet(
        CACHE / "rt_fivemin_mnt_lmps.parquet", index=False
    )

    # Regulation 5-min clearing prices (post-redesign single product).
    reg = _price_frame(mtus, pd.Series(mtus + pd.Timedelta(minutes=10), index=mtus))
    reg["rmccp"] = np.clip(rng.normal(9.0, 3.0, len(mtus)), 0.0, None)
    reg["rmpcp"] = np.clip(rng.normal(2.0, 0.8, len(mtus)), 0.0, None)
    reg["mcp"] = reg["rmccp"] + reg["rmpcp"]
    reg.sort_values(["published_at", "datetime_beginning_utc"]).to_parquet(
        CACHE / "reg_prices.parquet", index=False
    )

    # Regulation procurement summary (half-hour blocks, post-redesign shape).
    rmr = _price_frame(
        half_hours,
        pd.Series(half_hours - pd.Timedelta(minutes=10), index=half_hours),
    )
    rmr["requirement"] = 525.0
    rmr["regd_ssmw"] = pd.NA
    rmr["rega_ssmw"] = pd.NA
    rmr["regd_procure"] = pd.NA
    rmr["rega_procure"] = 525.0
    rmr["total_mw"] = 525.0
    rmr["deficiency"] = 0.0
    rmr["rto_perfscore"] = np.clip(rng.normal(0.94, 0.02, len(half_hours)), 0.85, 1.0)
    rmr["rega_mileage"] = rng.uniform(6.0, 14.0, len(half_hours))
    rmr["regd_mileage"] = pd.NA
    rmr["rega_hourly"] = pd.NA
    rmr["regd_hourly"] = pd.NA
    rmr["is_approved"] = True
    rmr["modified_datetime_utc"] = pd.NaT
    rmr.sort_values(["published_at", "datetime_beginning_utc"]).to_parquet(
        CACHE / "reg_market_results.parquet", index=False
    )

    # Reserve clearing prices: SR mostly small with rare scarcity spikes;
    # Sec mostly $0 (matches the real feeds' character).
    def reserve_frame(index, published, low, high, spike_p, zero_p):
        mcp = rng.uniform(low, high, len(index))
        mcp[rng.random(len(index)) < zero_p] = 0.0
        mcp += (rng.random(len(index)) < spike_p) * rng.uniform(100, 800, len(index))
        f = _price_frame(index, published)
        f["mcp"] = mcp
        f["mcp_capped"] = f["mcp"]
        return f.sort_values(["published_at", "datetime_beginning_utc"])

    da_publish_h = _da_publish(hours)
    rt_publish = pd.Series(mtus + pd.Timedelta(minutes=10), index=mtus)
    reserve_frame(hours, da_publish_h, 0.5, 6.0, 0.002, 0.3).to_parquet(
        CACHE / "da_sr_prices.parquet", index=False
    )
    reserve_frame(mtus, rt_publish, 0.2, 5.0, 0.005, 0.5).to_parquet(
        CACHE / "rt_sr_prices.parquet", index=False
    )
    reserve_frame(hours, da_publish_h, 0.0, 1.0, 0.0, 0.8).to_parquet(
        CACHE / "da_sec_prices.parquet", index=False
    )
    reserve_frame(mtus, rt_publish, 0.0, 1.0, 0.001, 0.85).to_parquet(
        CACHE / "rt_sec_prices.parquet", index=False
    )

    # RPM BRA clearing prices for the delivery years the window can touch.
    dys = sorted({y for y in (args.start.year - 1, args.start.year, args.start.year + 1)})
    pd.DataFrame(
        {
            "dy_start_year": dys,
            "lda": ["RTO"] * len(dys),
            "price_per_mw_day": [rng.uniform(90, 280) for _ in dys],
        }
    ).to_parquet(CACHE / "rpm_clearing_prices.parquet", index=False)

    n_days = args.days
    print(f"synthetic cache written to {CACHE}")
    print(
        f"  window: {args.start} → {args.start + timedelta(days=n_days - 1)} "
        f"({n_days} days, zone {ZONE})"
    )
    print(f"  feeds:  {len(sorted(CACHE.glob('*.parquet')))} parquet files")


if __name__ == "__main__":
    main()
