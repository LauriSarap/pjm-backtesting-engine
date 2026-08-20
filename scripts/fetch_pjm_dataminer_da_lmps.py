#!/usr/bin/env python3
"""Fetch PJM Data Miner day-ahead hourly LMPs for the configured pnodes."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import _dataminer as dm

FEED = "da_hrl_lmps"
ROW_COUNT = 50_000
FIELDS = [
    "datetime_beginning_utc",
    "datetime_beginning_ept",
    "pnode_id",
    "pnode_name",
    "voltage",
    "equipment",
    "type",
    "zone",
    "system_energy_price_da",
    "total_lmp_da",
    "congestion_price_da",
    "marginal_loss_price_da",
    "row_is_current",
    "version_nbr",
]
WORKING_PNODES = {
    # pnode_id: pnode_name — replace with your asset's settlement pnode(s).
    # 1 is the PJM-RTO aggregate, matching the example asset in
    # engine/pjm_engine/battery.py.
    1: "PJM-RTO",
}
DEFAULT_OUTPUT = dm.RAW_PJM / "da_hrl_lmps"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch PJM Data Miner DA hourly LMPs for working BESS pnodes."
    )
    parser.add_argument("--start-year", type=int, default=2021)
    parser.add_argument("--end", default=dm.default_end())
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sleep", type=float, default=0.25)
    return parser.parse_args()


def fetch_window(
    *,
    pnode_id: int,
    start_ept: datetime,
    end_ept: datetime,
    key: str,
    sleep_seconds: float,
) -> list[dict[str, Any]]:
    rows = dm.paginate_json(
        FEED,
        {
            "Sort": "datetime_beginning_ept",
            "Order": "Asc",
            "Fields": ",".join(FIELDS),
            "datetime_beginning_ept": dm.date_filter(start_ept, end_ept),
            "row_is_current": 1,
            "pnode_id": pnode_id,
        },
        key,
        row_count=ROW_COUNT,
        sleep_seconds=sleep_seconds,
    )
    # The API range is inclusive at the upper bound; keep half-open windows.
    return [
        row for row in rows if datetime.fromisoformat(str(row["datetime_beginning_ept"])) < end_ept
    ]


def expected_hours(start_ept: datetime, end_ept: datetime) -> int:
    return int((end_ept - start_ept).total_seconds() // 3600)


def validate(rows: list[dict[str, Any]], start_ept: datetime, end_ept: datetime) -> dict[str, Any]:
    stamps = [datetime.fromisoformat(str(row["datetime_beginning_ept"])) for row in rows]
    unique_stamps = sorted(set(stamps))
    duplicate_count = len(stamps) - len(unique_stamps)
    gaps = []
    for left, right in zip(unique_stamps, unique_stamps[1:]):
        delta_hours = int((right - left).total_seconds() // 3600)
        if delta_hours != 1:
            gaps.append(
                {
                    "from": left.isoformat(),
                    "to": right.isoformat(),
                    "delta_hours": delta_hours,
                }
            )
    expected = expected_hours(start_ept, end_ept)
    return {
        "rows": len(rows),
        "expected_rows": expected,
        "unique_hours": len(unique_stamps),
        "duplicate_hours": duplicate_count,
        "gap_count": len(gaps),
        "gaps": gaps[:10],
        "first_ept": unique_stamps[0].isoformat() if unique_stamps else None,
        "last_ept": unique_stamps[-1].isoformat() if unique_stamps else None,
        "complete": len(rows) == expected and duplicate_count == 0 and not gaps,
    }


def main() -> None:
    args = parse_args()
    key = dm.api_key()
    final_end = datetime.fromisoformat(args.end)
    summaries = []

    for pnode_id, pnode_name in WORKING_PNODES.items():
        year = args.start_year
        while year <= final_end.year:
            start_ept = datetime(year, 1, 1)
            end_ept = min(datetime(year + 1, 1, 1), final_end)
            if start_ept >= final_end:
                break

            print(
                f"fetching {pnode_id} {pnode_name} {start_ept.isoformat()} to {end_ept.isoformat()}"
            )
            rows = fetch_window(
                pnode_id=pnode_id,
                start_ept=start_ept,
                end_ept=end_ept,
                key=key,
                sleep_seconds=args.sleep,
            )
            output_path = args.output_dir / f"pnode_id={pnode_id}" / f"year={year}.csv"
            dm.write_csv(output_path, rows, FIELDS)
            validation = validate(rows, start_ept, end_ept)
            summary = {
                "pnode_id": pnode_id,
                "pnode_name": pnode_name,
                "year": year,
                "start_ept": start_ept.isoformat(),
                "end_ept_exclusive": end_ept.isoformat(),
                "path": str(output_path),
                **validation,
            }
            summaries.append(summary)
            print(
                f"  rows={summary['rows']} expected={summary['expected_rows']} "
                f"complete={summary['complete']} -> {output_path}"
            )
            year += 1
            time.sleep(args.sleep)

    summary_path = args.output_dir / "validation_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summaries, indent=2) + "\n")
    incomplete = [item for item in summaries if not item["complete"]]
    print(f"wrote {summary_path}")
    if incomplete:
        print("incomplete windows:")
        print(json.dumps(incomplete, indent=2))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
