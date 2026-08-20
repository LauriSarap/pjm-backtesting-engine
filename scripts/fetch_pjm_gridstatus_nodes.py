#!/usr/bin/env python3
"""Enumerate GridStatus PJM pricing nodes from a single DA LMP snapshot hour."""

from __future__ import annotations

import argparse
import csv
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Data lives outside the package: $PJM_DATA_ROOT if set, else ./data
DATA_ROOT = Path(os.environ.get("PJM_DATA_ROOT", "data")).resolve()
DEFAULT_OUTPUT = DATA_ROOT / "raw" / "gridstatus" / "pjm_nodes.csv"


def default_snapshot_hour() -> datetime:
    """A recent hour guaranteed to have published DA prices: yesterday, same hour."""
    now = datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0) - timedelta(days=1)


def parse_args() -> argparse.Namespace:
    snapshot = default_snapshot_hour()
    parser = argparse.ArgumentParser(
        description="Fetch one PJM DA LMP snapshot and save GridStatus pricing nodes."
    )
    parser.add_argument(
        "--start",
        default=snapshot.strftime("%Y-%m-%dT%H:00:00Z"),
        help="UTC inclusive interval start, e.g. 2025-01-01T04:00:00Z",
    )
    parser.add_argument(
        "--end",
        default=(snapshot + timedelta(hours=1)).strftime("%Y-%m-%dT%H:00:00Z"),
        help="UTC exclusive interval end, e.g. 2025-01-01T05:00:00Z",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    api_key = os.environ.get("GRIDSTATUS_API_KEY")
    if not api_key:
        raise SystemExit("GRIDSTATUS_API_KEY is not set")

    params = urllib.parse.urlencode(
        {
            "start_time": args.start,
            "end_time": args.end,
            "limit": "50000",
        }
    )
    url = f"https://api.gridstatus.io/v1/datasets/pjm_lmp_day_ahead_hourly/query?{params}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "x-api-key": api_key,
            "User-Agent": "pjm-backtesting-engine",
        },
    )

    with urllib.request.urlopen(request, timeout=90) as response:
        payload = json.load(response)

    rows = payload["data"] if isinstance(payload, dict) else payload
    by_location_id = {row["location_id"]: row for row in rows}

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "location_id",
        "location",
        "location_short_name",
        "location_type",
        "sample_interval_start_utc",
        "sample_lmp",
        "sample_energy",
        "sample_congestion",
        "sample_loss",
    ]
    with args.output.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for row in sorted(
            by_location_id.values(),
            key=lambda item: (
                str(item.get("location_type", "")),
                str(item.get("location_short_name", "")),
                int(item.get("location_id") or 0),
            ),
        ):
            writer.writerow(
                {
                    "location_id": row.get("location_id"),
                    "location": row.get("location"),
                    "location_short_name": row.get("location_short_name"),
                    "location_type": row.get("location_type"),
                    "sample_interval_start_utc": row.get("interval_start_utc"),
                    "sample_lmp": row.get("lmp"),
                    "sample_energy": row.get("energy"),
                    "sample_congestion": row.get("congestion"),
                    "sample_loss": row.get("loss"),
                }
            )

    print(f"wrote {args.output} ({len(by_location_id)} nodes)")


if __name__ == "__main__":
    main()
