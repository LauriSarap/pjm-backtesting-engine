#!/usr/bin/env python3
"""Fetch PJM Data Miner ancillary-services price, market-result, and billing feeds."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import _dataminer as dm

ROW_COUNT = 1_000_000
DEFAULT_OUTPUT = dm.RAW_PJM / "ancillary_services"


@dataclass(frozen=True)
class Feed:
    name: str
    label: str
    layer: str
    start: str
    date_field: str | None = "datetime_beginning_ept"
    chunk: str = "month"


FEEDS = {
    "da-as-lmps": Feed(
        name="da_ancillary_services",
        label="Day-Ahead Ancillary Service LMPs",
        layer="prices",
        start="2022-10-01T00:00:00",
    ),
    "rt-as-5m-lmps": Feed(
        name="ancillary_services_fivemin_hrl",
        label="Real-Time Ancillary Service Five-Minute LMPs",
        layer="prices",
        start="2021-01-01T00:00:00",
    ),
    "rt-as-hourly-lmps": Feed(
        name="ancillary_services",
        label="Real-Time Ancillary Service Hourly LMPs",
        layer="prices",
        start="2021-01-01T00:00:00",
    ),
    "da-market-results": Feed(
        name="da_reserve_market_results",
        label="Day-Ahead Ancillary Service Market Results",
        layer="market_results",
        start="2022-10-01T00:00:00",
    ),
    "rt-market-results": Feed(
        name="reserve_market_results",
        label="Real-Time Ancillary Service Market Results",
        layer="market_results",
        start="2021-01-01T00:00:00",
    ),
    "reg-prices": Feed(
        name="reg_prices",
        label="Regulation Prices",
        layer="market_results",
        start="2026-04-01T00:00:00",
    ),
    "reg-market-data": Feed(
        name="reg_market_results",
        label="Regulation Market Data",
        layer="market_results",
        start="2021-01-01T00:00:00",
    ),
    "reserve-subzone-buses": Feed(
        name="sync_pri_reserves_buses_list",
        label="Reserve Subzone Buses",
        layer="reference",
        start="2022-10-01T00:00:00",
        date_field=None,
        chunk="all",
    ),
    "reserve-subzone-resources": Feed(
        name="sync_pri_reserves_resources_list",
        label="Reserve Subzone Resources",
        layer="reference",
        start="2022-10-01T00:00:00",
        date_field=None,
        chunk="all",
    ),
    "dispatched-reserves": Feed(
        name="dispatched_reserves",
        label="Dispatched Reserves",
        layer="validation",
        start="2021-01-01T00:00:00",
    ),
    "rt-dispatched-reserves": Feed(
        name="rt_dispatch_reserves",
        label="Real-Time Dispatched Reserves",
        layer="validation",
        start="2021-01-01T00:00:00",
    ),
    "operational-reserves": Feed(
        name="operational_reserves",
        label="Operational Reserves",
        layer="validation",
        start="2021-01-01T00:00:00",
    ),
    "sync-reserve-events": Feed(
        name="sync_reserve_events",
        label="Synchronized Reserve Events",
        layer="validation",
        start="2021-01-01T00:00:00",
        date_field="event_start_ept",
    ),
    "reg-billing": Feed(
        name="reg_zone_prelim_bill",
        label="PJM Regulation Zone Preliminary Billing Data",
        layer="billing",
        start="2021-01-01T00:00:00",
    ),
    "sync-reserve-billing": Feed(
        name="sync_reserve_prelim_billing",
        label="Synchronized Reserve Preliminary Billing Data",
        layer="billing",
        start="2022-10-01T00:00:00",
    ),
    "non-sync-reserve-billing": Feed(
        name="non_sync_reserve_prelim_billing",
        label="Non-Synchronized Reserve Preliminary Billing Data",
        layer="billing",
        start="2022-10-01T00:00:00",
    ),
    "secondary-non-sync-reserve-billing": Feed(
        name="secondary_nonsync_reserve_prelim_billing",
        label="Secondary Non Synchronized Reserve Preliminary Billing Data",
        layer="billing",
        start="2022-10-01T00:00:00",
    ),
}


FEED_GROUPS = {
    "first": [
        "da-as-lmps",
        "rt-as-5m-lmps",
        "rt-as-hourly-lmps",
        "da-market-results",
        "rt-market-results",
        "reg-prices",
        "reg-market-data",
        "reserve-subzone-buses",
        "reserve-subzone-resources",
    ],
    "validation": [
        "dispatched-reserves",
        "rt-dispatched-reserves",
        "operational-reserves",
        "sync-reserve-events",
    ],
    "billing": [
        "reg-billing",
        "sync-reserve-billing",
        "non-sync-reserve-billing",
        "secondary-non-sync-reserve-billing",
    ],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch PJM Data Miner ancillary-services feeds.")
    parser.add_argument("--start", default="2021-01-01T00:00:00")
    parser.add_argument("--end", default=dm.default_end())
    parser.add_argument(
        "--feed",
        choices=["all", *FEED_GROUPS.keys(), *FEEDS.keys()],
        default="first",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--sleep",
        type=float,
        default=10.5,
        help="Seconds between API requests. Non-member PJM limit is 6/min.",
    )
    return parser.parse_args()


def date_filter(start: datetime, end: datetime) -> str:
    return dm.date_filter(start, end - timedelta(minutes=1))


def summarize_rows(rows: list[dict[str, Any]], date_fields: list[str]) -> dict[str, Any]:
    first: datetime | None = None
    last: datetime | None = None
    for row in rows:
        for field in date_fields:
            parsed = dm.parse_pjm_datetime(row.get(field))
            if parsed is None:
                continue
            first = parsed if first is None or parsed < first else first
            last = parsed if last is None or parsed > last else last
            break
    sample_keys = []
    if rows:
        sample_keys = [key for key, value in rows[0].items() if value not in ("", None)]
    return {
        "rows": len(rows),
        "first_timestamp": first.isoformat() if first else None,
        "last_timestamp": last.isoformat() if last else None,
        "sample_non_empty_fields": sample_keys,
    }


def selected_feeds(name: str) -> list[Feed]:
    if name == "all":
        keys = [key for group in FEED_GROUPS.values() for key in group]
        return [FEEDS[key] for key in keys]
    if name in FEED_GROUPS:
        return [FEEDS[key] for key in FEED_GROUPS[name]]
    return [FEEDS[name]]


def main() -> None:
    args = parse_args()
    requested_start = datetime.fromisoformat(args.start)
    requested_end = datetime.fromisoformat(args.end)
    key = dm.api_key()
    summaries = []

    for feed in selected_feeds(args.feed):
        metadata = dm.fetch_metadata(feed.name, key)
        fields = dm.metadata_fields(metadata)
        if not fields:
            raise RuntimeError(f"No metadata columns found for {feed.name}")

        feed_start = max(datetime.fromisoformat(feed.start), requested_start)
        feed_end = requested_end
        feed_dir = args.output_dir / feed.name
        print(f"fetching {feed.name} ({feed.label})", flush=True)

        if feed.chunk == "all" or feed.date_field is None:
            rows = dm.query_csv(
                feed.name,
                {
                    "RowCount": ROW_COUNT,
                    "StartRow": 1,
                    "format": "csv",
                    "download": "true",
                },
                key,
            )
            output_path = feed_dir / "all.csv"
            dm.write_csv(output_path, rows, fields)
            summary = {
                "feed": feed.name,
                "label": feed.label,
                "layer": feed.layer,
                "path": str(output_path),
                **summarize_rows(rows, ["effective_date", "datetime_beginning_ept"]),
            }
            summaries.append(summary)
            print(f"  rows={len(rows)} -> {output_path}", flush=True)
            time.sleep(args.sleep)
            continue

        chunk = "quarter" if feed.chunk == "month" else feed.chunk
        for start, end in dm.iter_windows(feed_start, feed_end, chunk):
            params = {
                "RowCount": ROW_COUNT,
                "StartRow": 1,
                feed.date_field: date_filter(start, end),
                "format": "csv",
                "download": "true",
            }
            print(f"  {start:%Y-%m}: {start.isoformat()} to {end.isoformat()}", flush=True)
            rows = dm.query_csv(feed.name, params, key)
            output_path = feed_dir / f"{chunk}={start:%Y-%m}.csv"
            dm.write_csv(output_path, rows, fields)
            summary = {
                "feed": feed.name,
                "label": feed.label,
                "layer": feed.layer,
                "start_ept": start.isoformat(),
                "end_ept_exclusive": end.isoformat(),
                "path": str(output_path),
                **summarize_rows(
                    rows,
                    [
                        "datetime_beginning_utc",
                        "datetime_beginning_ept",
                        "event_start_ept",
                        "effective_date",
                    ],
                ),
            }
            summaries.append(summary)
            print(f"    rows={len(rows)} -> {output_path}", flush=True)
            time.sleep(args.sleep)

    summary_path = args.output_dir / "validation_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summaries, indent=2) + "\n")
    print(f"wrote {summary_path}", flush=True)


if __name__ == "__main__":
    main()
