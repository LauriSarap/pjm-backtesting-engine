#!/usr/bin/env python3
"""Fetch the catalog of active PJM Data Miner feeds."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any

import _dataminer as dm

DEFAULT_OUTPUT = dm.RAW_PJM / "dataminer_feed_catalog.csv"

FIELDS = [
    "name",
    "displayName",
    "category",
    "description",
    "firstAvailable",
    "postingFrequency",
    "postingDay",
    "retentionTime",
    "isFrequentlyAccessed",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch the active PJM Data Miner feed catalog.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def query_catalog(key: str) -> list[dict[str, Any]]:
    payload = dm.get_json("", key, {"isactive": "true", "startRow": 1, "rowCount": 500}, timeout=60)
    return list(payload["items"])


def main() -> None:
    args = parse_args()
    items = sorted(
        query_catalog(dm.api_key()),
        key=lambda item: (item.get("category") or "", item.get("displayName") or ""),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for item in items:
            writer.writerow({field: item.get(field, "") for field in FIELDS})
    print(f"wrote {args.output} ({len(items)} feeds)")


if __name__ == "__main__":
    main()
