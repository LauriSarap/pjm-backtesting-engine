#!/usr/bin/env python3
"""Fetch targeted PJM day-ahead hourly LMPs from the GridStatus API."""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DATASET = "pjm_lmp_day_ahead_hourly"
API_BASE = f"https://api.gridstatus.io/v1/datasets/{DATASET}/query"
ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
# Data lives outside the package: $PJM_DATA_ROOT if set, else ./data
DATA_ROOT = Path(os.environ.get("PJM_DATA_ROOT", "data")).resolve()
DEFAULT_OUTPUT = DATA_ROOT / "raw" / "gridstatus" / "da_hrl_lmps"
WORKING_PNODES = {
    # pnode_id: pnode_name — replace with your asset's settlement pnode(s).
    # 1 is the PJM-RTO aggregate, matching the example asset in
    # engine/pjm_engine/battery.py.
    1: "PJM-RTO",
}
FIELDS = [
    "datetime_beginning_utc",
    "datetime_beginning_ept",
    "pnode_id",
    "pnode_name",
    "location",
    "type",
    "system_energy_price_da",
    "total_lmp_da",
    "congestion_price_da",
    "marginal_loss_price_da",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch targeted PJM DA hourly LMPs from GridStatus."
    )
    parser.add_argument("--start-ept", default="2021-01-01T00:00:00")
    parser.add_argument("--end-ept", default=datetime.now().strftime("%Y-%m-%dT00:00:00"))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=50_000)
    parser.add_argument("--sleep", type=float, default=0.5)
    parser.add_argument("--api-key", default=os.environ.get("GRIDSTATUS_API_KEY"))
    return parser.parse_args()


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=ET).astimezone(UTC)


def fetch_pnode(
    *,
    pnode_id: int,
    start_utc: datetime,
    end_utc: datetime,
    api_key: str,
    limit: int,
) -> list[dict[str, object]]:
    params = urllib.parse.urlencode(
        {
            "start_time": start_utc.isoformat().replace("+00:00", "Z"),
            "end_time": end_utc.isoformat().replace("+00:00", "Z"),
            "limit": str(limit),
            "filter_column": "location_id",
            "filter_value": str(pnode_id),
        }
    )
    request = urllib.request.Request(
        f"{API_BASE}?{params}",
        headers={
            "Accept": "application/json",
            "x-api-key": api_key,
            "User-Agent": "pjm-backtesting-engine",
        },
    )
    with urllib.request.urlopen(request, timeout=180) as response:
        payload = json.load(response)
    rows = payload["data"] if isinstance(payload, dict) else payload
    if len(rows) >= limit:
        raise RuntimeError(
            f"GridStatus returned {len(rows)} rows for {pnode_id}; "
            "increase pagination support before trusting this pull."
        )
    return rows


def normalize(row: dict[str, object]) -> dict[str, object]:
    utc_stamp = datetime.fromisoformat(str(row["interval_start_utc"]))
    ept_stamp = utc_stamp.astimezone(ET).replace(tzinfo=None)
    return {
        "datetime_beginning_utc": utc_stamp.replace(tzinfo=None).isoformat(),
        "datetime_beginning_ept": ept_stamp.isoformat(),
        "pnode_id": row.get("location_id"),
        "pnode_name": row.get("location_short_name"),
        "location": row.get("location"),
        "type": row.get("location_type"),
        "system_energy_price_da": row.get("energy"),
        "total_lmp_da": row.get("lmp"),
        "congestion_price_da": row.get("congestion"),
        "marginal_loss_price_da": row.get("loss"),
    }


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def validate(
    rows: list[dict[str, object]],
    *,
    start_utc: datetime,
    end_utc: datetime,
) -> dict[str, object]:
    stamps = sorted(
        datetime.fromisoformat(str(row["datetime_beginning_utc"])).replace(tzinfo=UTC)
        for row in rows
    )
    duplicate_count = len(stamps) - len(set(stamps))
    gaps = []
    unique = sorted(set(stamps))
    for left, right in zip(unique, unique[1:]):
        delta_hours = int((right - left).total_seconds() // 3600)
        if delta_hours != 1:
            gaps.append(
                {
                    "from": left.isoformat(),
                    "to": right.isoformat(),
                    "delta_hours": delta_hours,
                }
            )
    expected = int((end_utc - start_utc).total_seconds() // 3600)
    return {
        "rows": len(rows),
        "expected_rows": expected,
        "unique_utc_hours": len(unique),
        "duplicate_utc_hours": duplicate_count,
        "gap_count": len(gaps),
        "gaps": gaps[:10],
        "first_utc": unique[0].isoformat() if unique else None,
        "last_utc": unique[-1].isoformat() if unique else None,
        "complete": len(rows) == expected and duplicate_count == 0 and not gaps,
    }


def main() -> None:
    args = parse_args()
    if not args.api_key:
        raise SystemExit("GRIDSTATUS_API_KEY is not set")

    start_ept = datetime.fromisoformat(args.start_ept)
    end_ept = datetime.fromisoformat(args.end_ept)
    start_utc = as_utc(start_ept)
    end_utc = as_utc(end_ept)
    summaries = []

    for pnode_id, pnode_name in WORKING_PNODES.items():
        print(f"fetching {pnode_id} {pnode_name}: {start_utc.isoformat()} to {end_utc.isoformat()}")
        raw_rows = fetch_pnode(
            pnode_id=pnode_id,
            start_utc=start_utc,
            end_utc=end_utc,
            api_key=args.api_key,
            limit=args.limit,
        )
        rows = [normalize(row) for row in raw_rows]
        output_path = args.output_dir / f"pnode_id={pnode_id}.csv"
        write_csv(output_path, rows)
        summary = {
            "pnode_id": pnode_id,
            "pnode_name": pnode_name,
            "start_ept": start_ept.isoformat(),
            "end_ept_exclusive": end_ept.isoformat(),
            "path": str(output_path),
            **validate(rows, start_utc=start_utc, end_utc=end_utc),
        }
        summaries.append(summary)
        print(
            f"  rows={summary['rows']} expected={summary['expected_rows']} "
            f"complete={summary['complete']} -> {output_path}"
        )
        time.sleep(args.sleep)

    summary_path = args.output_dir / "validation_summary.json"
    summary_path.write_text(json.dumps(summaries, indent=2) + "\n")
    print(f"wrote {summary_path}")
    incomplete = [summary for summary in summaries if not summary["complete"]]
    if incomplete:
        print(json.dumps(incomplete, indent=2))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
