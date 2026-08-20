#!/usr/bin/env python3
"""Fetch PJM Data Miner load, generation, and weather-context feeds."""

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
DEFAULT_OUTPUT = dm.RAW_PJM / "context"


@dataclass(frozen=True)
class Feed:
    name: str
    label: str
    layer: str
    date_field: str | None
    chunk: str
    start: str | None = None
    # PJM only exposes a trailing window for limited-retention feeds; the fetch
    # start is derived from --end minus this many days.
    retention_days: int | None = None


FEEDS = {
    # Full-history context feeds.
    "generation-by-fuel": Feed(
        "gen_by_fuel",
        "Generation by Fuel Type",
        "generation",
        "datetime_beginning_ept",
        "year",
        start="2021-01-01T00:00:00",
    ),
    "forecasted-generation-outages": Feed(
        "frcstd_gen_outages",
        "Forecasted Generation Outages",
        "generation_outages",
        "forecast_execution_date_ept",
        "year",
        start="2021-01-01T00:00:00",
    ),
    "generation-outages-by-type": Feed(
        "gen_outages_by_type",
        "Generation Outage for Seven Days by Type",
        "generation_outages",
        "forecast_execution_date_ept",
        "year",
        start="2021-01-01T00:00:00",
    ),
    "daily-generation-capacity": Feed(
        "day_gen_capacity",
        "Daily Generation Capacity",
        "generation_capacity",
        "bid_datetime_beginning_ept",
        "year",
        start="2021-01-01T00:00:00",
    ),
    "load-metered": Feed(
        "hrl_load_metered",
        "Hourly Load: Metered",
        "load",
        "datetime_beginning_ept",
        "year",
        start="2021-01-01T00:00:00",
    ),
    "load-estimated": Feed(
        "hrl_load_estimated",
        "Hourly Load: Estimated",
        "load",
        "datetime_beginning_ept",
        "year",
        start="2021-01-01T00:00:00",
    ),
    "load-preliminary": Feed(
        "hrl_load_prelim",
        "Hourly Load: Preliminary",
        "load",
        "datetime_beginning_ept",
        "year",
        start="2021-01-01T00:00:00",
    ),
    "historical-load-forecasts": Feed(
        "load_frcstd_hist",
        "Historical Load Forecasts",
        "load_forecast",
        "evaluated_at_ept",
        "month",
        start="2021-01-01T00:00:00",
    ),
    "wind-generation": Feed(
        "wind_gen",
        "Wind Generation",
        "renewables",
        "datetime_beginning_ept",
        "year",
        start="2021-01-01T00:00:00",
    ),
    "solar-generation": Feed(
        "solar_gen",
        "Solar Generation",
        "renewables",
        "datetime_beginning_ept",
        "year",
        start="2021-01-01T00:00:00",
    ),
    "rt-temperature-sets": Feed(
        "rt_tempset",
        "Real-Time Temperature Sets",
        "temperature_sets",
        "datetime_beginning_ept",
        "year",
        start="2021-01-01T00:00:00",
    ),
    # Officially limited-retention feeds. Pull the full available retention
    # window; older history is not exposed by PJM Data Miner for these feeds.
    "seven-day-load-forecast": Feed(
        "load_frcstd_7_day",
        "Seven-Day Load Forecast",
        "load_forecast_current",
        None,
        "all",
    ),
    "five-minute-load-forecast": Feed(
        "very_short_load_frcst",
        "Five Minute Load Forecast",
        "load_forecast_current",
        "evaluated_at_ept",
        "day",
        retention_days=30,
    ),
    "hourly-wind-forecast": Feed(
        "hourly_wind_power_forecast",
        "Hourly Wind Power Forecast",
        "renewable_forecast",
        "evaluated_at_ept",
        "month",
        retention_days=30,
    ),
    "hourly-solar-forecast": Feed(
        "hourly_solar_power_forecast",
        "Hourly Solar Power Forecast",
        "renewable_forecast",
        "evaluated_at_ept",
        "month",
        retention_days=30,
    ),
    "five-minute-wind-forecast": Feed(
        "five_min_wind_power_forecast",
        "Five Minute Wind Power Forecast",
        "renewable_forecast",
        "evaluated_at_ept",
        "day",
        retention_days=30,
    ),
    "five-minute-solar-forecast": Feed(
        "five_min_solar_power_forecast",
        "Five Minute Solar Power Forecast",
        "renewable_forecast",
        "evaluated_at_ept",
        "day",
        retention_days=30,
    ),
    "five-minute-solar-generation": Feed(
        "five_min_solar_generation",
        "Five Minute Solar Generation",
        "renewables",
        "datetime_beginning_ept",
        "month",
        retention_days=30,
    ),
}


FEED_GROUPS = {
    "core": [
        "generation-by-fuel",
        "forecasted-generation-outages",
        "generation-outages-by-type",
        "daily-generation-capacity",
        "load-metered",
        "load-estimated",
        "load-preliminary",
        "historical-load-forecasts",
        "wind-generation",
        "solar-generation",
        "rt-temperature-sets",
    ],
    "forecasts": [
        "seven-day-load-forecast",
        "five-minute-load-forecast",
        "hourly-wind-forecast",
        "hourly-solar-forecast",
        "five-minute-wind-forecast",
        "five-minute-solar-forecast",
        "five-minute-solar-generation",
    ],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch PJM Data Miner load/generation/weather-context feeds."
    )
    parser.add_argument(
        "--feed",
        choices=["all", *FEED_GROUPS.keys(), *FEEDS.keys()],
        default="all",
    )
    parser.add_argument("--start", default="2021-01-01T00:00:00")
    parser.add_argument("--end", default=dm.default_end())
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sleep", type=float, default=0.5)
    return parser.parse_args()


def date_filter(start: datetime, end: datetime) -> str:
    return dm.date_filter(start, end - timedelta(seconds=1))


def feed_window(
    feed: Feed, requested_start: datetime, requested_end: datetime
) -> tuple[datetime, datetime]:
    if feed.retention_days is not None:
        earliest = requested_end - timedelta(days=feed.retention_days)
    elif feed.start is not None:
        earliest = datetime.fromisoformat(feed.start)
    else:
        raise ValueError(f"{feed.name} has a date field but no start or retention")
    return max(earliest, requested_start), requested_end


def selected_feed_keys(name: str) -> list[str]:
    if name == "all":
        return [*FEED_GROUPS["core"], *FEED_GROUPS["forecasts"]]
    if name in FEED_GROUPS:
        return FEED_GROUPS[name]
    return [name]


def fetch_feed(
    feed: Feed,
    fields: list[str],
    key: str,
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    feed_dir = args.output_dir / feed.name
    summaries = []
    if feed.date_field is None:
        print(f"fetching {feed.name} current snapshot", flush=True)
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
        output_path = feed_dir / "current.csv"
        dm.write_csv(output_path, rows, fields)
        print(f"  rows={len(rows)} -> {output_path}", flush=True)
        return [
            {
                "feed": feed.name,
                "label": feed.label,
                "path": str(output_path),
                "rows": len(rows),
                "window": "current",
            }
        ]

    start, end = feed_window(
        feed, datetime.fromisoformat(args.start), datetime.fromisoformat(args.end)
    )
    for start_ept, end_ept in dm.iter_windows(start, end, feed.chunk):
        print(
            f"fetching {feed.name} {start_ept.isoformat()} to {end_ept.isoformat()}",
            flush=True,
        )
        rows = dm.query_csv(
            feed.name,
            {
                "RowCount": ROW_COUNT,
                "StartRow": 1,
                feed.date_field: date_filter(start_ept, end_ept),
                "format": "csv",
                "download": "true",
            },
            key,
        )
        if feed.chunk == "year":
            label = f"year={start_ept:%Y}"
        elif feed.chunk == "month":
            label = f"month={start_ept:%Y-%m}"
        elif feed.chunk == "day":
            label = f"day={start_ept:%Y-%m-%d}"
        else:
            label = "window"
        output_path = feed_dir / f"{label}.csv"
        dm.write_csv(output_path, rows, fields)
        print(f"  rows={len(rows)} -> {output_path}", flush=True)
        summaries.append(
            {
                "feed": feed.name,
                "label": feed.label,
                "path": str(output_path),
                "rows": len(rows),
                "start_ept": start_ept.isoformat(),
                "end_ept_exclusive": end_ept.isoformat(),
            }
        )
        time.sleep(args.sleep)
    return summaries


def main() -> None:
    args = parse_args()
    key = dm.api_key()
    feed_keys = selected_feed_keys(args.feed)
    summaries: list[dict[str, Any]] = []
    selected_feed_names = {FEEDS[feed_key].name for feed_key in feed_keys}
    for feed_key in feed_keys:
        feed = FEEDS[feed_key]
        metadata = dm.fetch_metadata(feed.name, key)
        fields = dm.metadata_fields(metadata)
        if not fields:
            raise RuntimeError(f"No metadata fields for {feed.name}")
        summaries.extend(fetch_feed(feed, fields, key, args))

    summary_path = args.output_dir / "fetch_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    if args.feed != "all" and summary_path.exists():
        existing = json.loads(summary_path.read_text())
        summaries = [
            item for item in existing if item.get("feed") not in selected_feed_names
        ] + summaries
    summary_path.write_text(json.dumps(summaries, indent=2) + "\n")
    print(f"wrote {summary_path}")


if __name__ == "__main__":
    main()
