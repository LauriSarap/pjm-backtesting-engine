"""Run the perfect-foresight MILP for one asset over one window.

Usage:
    python -m optimization.scripts.run_perfect_foresight --asset example_a \
        --start 2025-10-01 --end 2025-10-14
"""

from __future__ import annotations

import argparse
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from pjm_engine.battery import ASSETS
from pjm_engine.data import load_da_hrl_lmps, load_rt_fivemin_mnt_lmps
from pjm_optimization.perfect_foresight import (
    solve_perfect_foresight,
    write_parquet,
)

UTC = timezone.utc
EPT = ZoneInfo("America/New_York")


def _to_utc(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=EPT).astimezone(UTC)


def _slice(df: pd.DataFrame, zone: str, win_start: datetime, win_end: datetime) -> pd.Series:
    sub = df[
        (df["zone"] == zone)
        & (df["datetime_beginning_utc"] >= win_start)
        & (df["datetime_beginning_utc"] < win_end)
    ]
    price_col = "total_lmp_da" if "total_lmp_da" in sub.columns else "total_lmp_rt"
    return pd.Series(sub[price_col].values, index=sub["datetime_beginning_utc"].values).sort_index()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", default="example_a", choices=list(ASSETS))
    parser.add_argument("--start", default="2025-10-01", help="ISO date, inclusive (00:00 EPT)")
    parser.add_argument(
        "--end", default="2025-10-14", help="ISO date, inclusive last operating day"
    )
    parser.add_argument("--initial-soc-pct", type=float, default=0.5)
    parser.add_argument("--out-dir", default=None)
    parser.add_argument(
        "--no-final-soc",
        action="store_true",
        help="Don't pin final SoC to initial; lets the MILP run the battery down.",
    )
    parser.add_argument(
        "--no-cycle-cost",
        action="store_true",
        help="Drop cycle-cost penalty (gives a looser, less realistic ceiling).",
    )
    parser.add_argument(
        "--no-link-da",
        action="store_true",
        help="Decouple DA from physical (allows unbounded virtual bidding).",
    )
    parser.add_argument(
        "--time-limit", type=float, default=None, help="Solver time limit in seconds."
    )
    args = parser.parse_args()

    asset = ASSETS[args.asset]
    start_d = date.fromisoformat(args.start)
    end_d = date.fromisoformat(args.end)

    runs_root = Path(os.environ.get("PJM_RUNS_ROOT", "evaluation/runs")).resolve()
    out_dir = (
        Path(args.out_dir) if args.out_dir else runs_root / "ceiling"
    ) / f"{args.start}_{args.end}"

    print(
        f"asset:  {asset.asset_id} ({asset.zone}, {asset.power_mw} MW / {asset.energy_mwh} MWh)\n"
        f"window: {start_d} → {end_d} (inclusive)\n"
        f"out:    {out_dir}\n"
    )

    win_start = _to_utc(start_d)
    win_end = _to_utc(end_d + timedelta(days=1))

    print("loading LMPs...")
    da_full = load_da_hrl_lmps()
    rt_full = load_rt_fivemin_mnt_lmps()
    da_series = _slice(da_full, asset.zone, win_start, win_end)
    rt_series = _slice(rt_full, asset.zone, win_start, win_end)
    print(f"  DA hours: {len(da_series)}, RT MTUs: {len(rt_series)}")

    initial_soc = args.initial_soc_pct * asset.energy_mwh
    cycle_cost = 0.0 if args.no_cycle_cost else asset.cycle_cost
    print(f"  initial_soc: {initial_soc} MWh, cycle_cost: ${cycle_cost}/MWh")

    print("\nsolving MILP...")
    result = solve_perfect_foresight(
        asset=asset,
        da_lmps=da_series,
        rt_lmps=rt_series,
        initial_soc_mwh=initial_soc,
        final_soc_constraint=not args.no_final_soc,
        cycle_cost_usd_mwh=cycle_cost,
        link_da_to_physical=not args.no_link_da,
        time_limit_sec=args.time_limit,
        verbose=True,
    )

    print(
        f"\nstatus:        {result.solver_status}\n"
        f"solve time:    {result.solve_seconds:.2f}s\n"
        f"DA revenue:    ${result.da_revenue:>14,.2f}\n"
        f"RT revenue:    ${result.rt_revenue:>14,.2f}\n"
        f"cycle cost:    ${result.cycle_cost:>14,.2f}\n"
        f"net obj:       ${result.net_objective:>14,.2f}\n"
        f"gross revenue: ${result.total_revenue:>14,.2f}\n"
    )

    rev_path, soc_path = write_parquet(result, out_dir)
    print(f"wrote: {rev_path}")
    print(f"wrote: {soc_path}")


if __name__ == "__main__":
    main()
