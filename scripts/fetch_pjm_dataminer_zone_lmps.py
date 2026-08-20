#!/usr/bin/env python3
"""Fetch PJM Data Miner zone-aggregate DA and RT LMPs."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import time as datetime_time
from pathlib import Path
from typing import Any

import _dataminer as dm

ROW_COUNT = 1_000_000
DEFAULT_OUTPUT = dm.RAW_PJM / "zone_lmps"

ZONES = {
    # Public PJM zone-aggregate pnodes. Adjust to the zones you study.
    "PJM-RTO": 1,
    "COMED": 33092371,
    "PSEG": 51301,
    "DOM": 34964545,
    "AEP": 8445784,
}

COMMON_FIELDS = [
    "datetime_beginning_utc",
    "datetime_beginning_ept",
    "pnode_id",
    "pnode_name",
    "voltage",
    "equipment",
    "type",
    "zone",
]


@dataclass(frozen=True)
class Feed:
    key: str
    name: str
    interval_minutes: int
    fields: list[str]
    chunk: str
    direct_zone_filter: bool = False
    include_type_filter: bool = True


FEEDS = {
    "da": Feed(
        key="da",
        name="da_hrl_lmps",
        interval_minutes=60,
        chunk="month",
        fields=[
            *COMMON_FIELDS,
            "system_energy_price_da",
            "total_lmp_da",
            "congestion_price_da",
            "marginal_loss_price_da",
            "row_is_current",
            "version_nbr",
        ],
        direct_zone_filter=False,
    ),
    # Settlement-verified aggregate/zonal five-minute RT LMPs. The non-verified
    # rt_fivemin_hrl_lmps archive blocks type=ZONE filtering.
    "rt5": Feed(
        key="rt5",
        name="rt_fivemin_mnt_lmps",
        interval_minutes=5,
        chunk="month",
        fields=[
            *COMMON_FIELDS,
            "system_energy_price_rt",
            "total_lmp_rt",
            "congestion_price_rt",
            "marginal_loss_price_rt",
        ],
        direct_zone_filter=True,
    ),
    # Preliminary/current RT five-minute LMPs. Use only to fill the newest tail
    # before settlements-verified rt_fivemin_mnt_lmps catches up.
    "rt5-current": Feed(
        key="rt5-current",
        name="rt_fivemin_hrl_lmps",
        interval_minutes=5,
        chunk="month",
        fields=[
            *COMMON_FIELDS,
            "system_energy_price_rt",
            "total_lmp_rt",
            "congestion_price_rt",
            "marginal_loss_price_rt",
            "row_is_current",
            "version_nbr",
        ],
        direct_zone_filter=True,
        include_type_filter=False,
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch PJM Data Miner zone-level LMPs for the BESS backtest zones."
    )
    parser.add_argument("--start", default="2021-01-01T00:00:00")
    parser.add_argument("--end", default=dm.default_end())
    parser.add_argument(
        "--feed",
        choices=["all", *FEEDS.keys()],
        default="all",
        help="Data feed to fetch. Default: all.",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sleep", type=float, default=0.25)
    parser.add_argument(
        "--keep-all-zones",
        action="store_true",
        help="Write all PJM zone rows instead of only the configured study zones.",
    )
    return parser.parse_args()


def date_filter(start_ept: datetime, end_ept: datetime, interval_minutes: int) -> str:
    # Query through the final interval start, then drop any row at or after the
    # exclusive end locally.
    return dm.date_filter(start_ept, end_ept - timedelta(minutes=interval_minutes))


def fetch_window(
    *,
    feed: Feed,
    start_ept: datetime,
    end_ept: datetime,
    key: str,
    keep_all_zones: bool,
    sleep_seconds: float,
) -> list[dict[str, Any]]:
    try:
        return fetch_window_once(
            feed=feed,
            start_ept=start_ept,
            end_ept=end_ept,
            key=key,
            keep_all_zones=keep_all_zones,
            sleep_seconds=sleep_seconds,
        )
    except urllib.error.HTTPError as error:
        if error.code != 400:
            raise
        if (end_ept - start_ept).total_seconds() <= feed.interval_minutes * 60:
            raise
        whole_days = (end_ept.date() - start_ept.date()).days
        if whole_days > 1:
            midpoint = datetime.combine(
                start_ept.date() + timedelta(days=max(1, whole_days // 2)),
                datetime_time.min,
            )
        else:
            midpoint = start_ept + (end_ept - start_ept) / 2
        print(
            f"  splitting {feed.name} window after PJM 400: "
            f"{start_ept.isoformat()} to {end_ept.isoformat()}",
            flush=True,
        )
        return [
            *fetch_window(
                feed=feed,
                start_ept=start_ept,
                end_ept=midpoint,
                key=key,
                keep_all_zones=keep_all_zones,
                sleep_seconds=sleep_seconds,
            ),
            *fetch_window(
                feed=feed,
                start_ept=midpoint,
                end_ept=end_ept,
                key=key,
                keep_all_zones=keep_all_zones,
                sleep_seconds=sleep_seconds,
            ),
        ]


def fetch_window_once(
    *,
    feed: Feed,
    start_ept: datetime,
    end_ept: datetime,
    key: str,
    keep_all_zones: bool,
    sleep_seconds: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    keep_names = set(ZONES)

    base_params: dict[str, Any] = {
        "RowCount": ROW_COUNT,
        "StartRow": 1,
        "datetime_beginning_ept": date_filter(start_ept, end_ept, feed.interval_minutes),
        "format": "csv",
        "download": "true",
    }
    if feed.include_type_filter:
        base_params["type"] = "ZONE"
    if feed.direct_zone_filter and not keep_all_zones:
        batches = []
        for pnode_id in ZONES.values():
            batches.extend(dm.query_csv(feed.name, {**base_params, "pnode_id": pnode_id}, key))
            time.sleep(sleep_seconds)
    else:
        batches = dm.query_csv(feed.name, base_params, key)
    for row in batches:
        row_ept = dm.parse_pjm_datetime_strict(row["datetime_beginning_ept"])
        if row_ept >= end_ept:
            continue
        if keep_all_zones or row.get("pnode_name") in keep_names:
            rows.append(row)

    return rows


def expected_rows(start_ept: datetime, end_ept: datetime, interval_minutes: int) -> int:
    intervals = int((end_ept - start_ept).total_seconds() // (interval_minutes * 60))
    return intervals


def validate_zone_rows(
    rows: list[dict[str, Any]],
    *,
    start_ept: datetime,
    end_ept: datetime,
    interval_minutes: int,
) -> list[dict[str, Any]]:
    results = []
    for zone_name in sorted({str(row["pnode_name"]) for row in rows}):
        zone_rows = [row for row in rows if row["pnode_name"] == zone_name]
        stamps = [dm.parse_pjm_datetime_strict(row["datetime_beginning_utc"]) for row in zone_rows]
        unique_stamps = sorted(set(stamps))
        duplicate_count = len(stamps) - len(unique_stamps)
        gaps = []
        for left, right in zip(unique_stamps, unique_stamps[1:]):
            delta_minutes = int((right - left).total_seconds() // 60)
            if delta_minutes != interval_minutes:
                gaps.append(
                    {
                        "from": left.isoformat(),
                        "to": right.isoformat(),
                        "delta_minutes": delta_minutes,
                    }
                )
        expected = (
            int((unique_stamps[-1] - unique_stamps[0]).total_seconds() // (interval_minutes * 60))
            + 1
            if unique_stamps
            else expected_rows(start_ept, end_ept, interval_minutes)
        )
        results.append(
            {
                "zone": zone_name,
                "pnode_id": zone_rows[0].get("pnode_id") if zone_rows else None,
                "rows": len(zone_rows),
                "expected_rows_from_utc_span": expected,
                "unique_intervals": len(unique_stamps),
                "duplicate_intervals": duplicate_count,
                "gap_count": len(gaps),
                "gaps": gaps[:10],
                "first_utc": unique_stamps[0].isoformat() if unique_stamps else None,
                "last_utc": unique_stamps[-1].isoformat() if unique_stamps else None,
                "complete": len(zone_rows) == expected and duplicate_count == 0 and not gaps,
            }
        )
    return results


def main() -> None:
    args = parse_args()
    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end)
    feeds = list(FEEDS.values()) if args.feed == "all" else [FEEDS[args.feed]]
    key = dm.api_key()
    summaries = []

    for feed in feeds:
        feed_dir = args.output_dir / feed.name
        for start_ept, end_ept in dm.iter_windows(start, end, feed.chunk):
            print(
                f"fetching {feed.name} {start_ept.isoformat()} to {end_ept.isoformat()}",
                flush=True,
            )
            rows = fetch_window(
                feed=feed,
                start_ept=start_ept,
                end_ept=end_ept,
                key=key,
                keep_all_zones=args.keep_all_zones,
                sleep_seconds=args.sleep,
            )
            label = f"month={start_ept:%Y-%m}"
            output_path = feed_dir / f"{label}.csv"
            dm.write_csv(output_path, rows, feed.fields)
            validations = validate_zone_rows(
                rows,
                start_ept=start_ept,
                end_ept=end_ept,
                interval_minutes=feed.interval_minutes,
            )
            complete = all(item["complete"] for item in validations)
            summaries.append(
                {
                    "feed": feed.name,
                    "start_ept": start_ept.isoformat(),
                    "end_ept_exclusive": end_ept.isoformat(),
                    "path": str(output_path),
                    "rows": len(rows),
                    "complete": complete,
                    "zones": validations,
                }
            )
            print(f"  rows={len(rows)} complete={complete} -> {output_path}", flush=True)
            time.sleep(args.sleep)

    summary_path = args.output_dir / "validation_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summaries, indent=2) + "\n")
    incomplete = [item for item in summaries if not item["complete"]]
    print(f"wrote {summary_path}")
    if incomplete:
        print(json.dumps(incomplete[:5], indent=2))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
