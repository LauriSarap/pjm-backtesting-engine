"""Shared helpers for scripts that pull from the PJM Data Miner 2 API.

Requires a PJM API key: register for a free Data Miner 2 key at
https://apiportal.pjm.com and export it as PJM_API_KEY.
"""

from __future__ import annotations

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

API_ROOT = "https://api.pjm.com/api/v1"
USER_AGENT = "pjm-backtesting-engine"
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}

# Data lives outside the package: $PJM_DATA_ROOT if set, else ./data
DATA_ROOT = Path(os.environ.get("PJM_DATA_ROOT", "data")).resolve()
RAW_PJM = DATA_ROOT / "raw" / "pjm"


def api_key() -> str:
    key = os.environ.get("PJM_API_KEY")
    if not key:
        sys.exit(
            "PJM_API_KEY is not set. Register for a free PJM Data Miner 2 API key "
            "at https://apiportal.pjm.com, then export it as PJM_API_KEY."
        )
    return key


def default_end() -> str:
    """Midnight today: the most recent complete day boundary."""
    return datetime.now().strftime("%Y-%m-%dT00:00:00")


def _request(url: str, key: str, accept: str) -> urllib.request.Request:
    return urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "Ocp-Apim-Subscription-Key": key,
            "User-Agent": USER_AGENT,
        },
    )


def get_json(
    endpoint: str,
    key: str,
    params: dict[str, Any] | None = None,
    timeout: float = 120,
) -> dict[str, Any]:
    url = f"{API_ROOT}/{endpoint}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = _request(url, key, "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def paginate_json(
    feed: str,
    params: dict[str, Any],
    key: str,
    *,
    row_count: int,
    sleep_seconds: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    start_row = 1
    total_rows: int | None = None
    while total_rows is None or start_row <= total_rows:
        payload = get_json(feed, key, {**params, "RowCount": row_count, "StartRow": start_row})
        if "errors" in payload:
            raise RuntimeError(f"PJM API errors for {feed}: {payload['errors']}")
        total_rows = int(payload.get("totalRows", 0))
        batch = payload.get("items", [])
        if not isinstance(batch, list):
            raise RuntimeError(f"Unexpected PJM API payload for {feed}: {payload}")
        rows.extend(batch)
        start_row += row_count
        if start_row <= total_rows:
            time.sleep(sleep_seconds)
    return rows


def query_csv(
    feed: str, params: dict[str, Any], key: str, *, timeout: float = 240
) -> list[dict[str, str]]:
    url = f"{API_ROOT}/{feed}?{urllib.parse.urlencode(params)}"
    request = _request(url, key, "text/csv")
    text = None
    last_error: Exception | None = None
    for attempt in range(1, 6):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8-sig")
            break
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_HTTP_CODES:
                raise
            last_error = error
        except urllib.error.URLError as error:
            last_error = error
        wait_seconds = min(60, 2**attempt)
        print(
            f"  retrying {feed} after request failure "
            f"({attempt}/5, wait={wait_seconds}s): {last_error}",
            flush=True,
        )
        time.sleep(wait_seconds)
    if text is None:
        raise RuntimeError(f"PJM API request failed after retries: {last_error}")
    if text.lstrip().startswith("{"):
        payload = json.loads(text)
        if "errors" in payload:
            raise RuntimeError(f"PJM API errors for {feed}: {payload['errors']}")
        raise RuntimeError(f"Unexpected PJM API response for {feed}: {payload}")
    return list(csv.DictReader(text.splitlines()))


def fetch_metadata(feed: str, key: str) -> dict[str, Any]:
    return get_json(f"{feed}/metadata", key, timeout=60)


def metadata_fields(metadata: dict[str, Any]) -> list[str]:
    columns = metadata.get("fields") or metadata.get("columns") or []
    pairs = []
    for index, column in enumerate(columns):
        name = column.get("name") or column.get("fieldName")
        if not name:
            continue
        ordinal = column.get("ordinalPosition")
        pairs.append((int(ordinal) if ordinal is not None else index, str(name)))
    return [name for _, name in sorted(pairs)]


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def add_month(dt: datetime) -> datetime:
    if dt.month == 12:
        return datetime(dt.year + 1, 1, 1)
    return datetime(dt.year, dt.month + 1, 1)


def iter_windows(start: datetime, end: datetime, chunk: str) -> Iterator[tuple[datetime, datetime]]:
    cursor = start
    while cursor < end:
        if chunk == "year":
            next_cursor = datetime(cursor.year + 1, 1, 1)
        elif chunk == "quarter":
            next_cursor = add_month(add_month(add_month(cursor)))
        elif chunk == "month":
            next_cursor = add_month(cursor)
        elif chunk == "day":
            next_cursor = cursor + timedelta(days=1)
        else:
            raise ValueError(f"Unsupported chunk size: {chunk}")
        next_cursor = min(next_cursor, end)
        yield cursor, next_cursor
        cursor = next_cursor


def date_filter(start: datetime, final_inclusive: datetime) -> str:
    """Data Miner date filters are inclusive at both ends."""
    return f"{start:%Y-%m-%dT%H:%M:%S}.0000000 to {final_inclusive:%Y-%m-%dT%H:%M:%S}.0000000"


def parse_pjm_datetime(value: Any) -> datetime | None:
    text = str(value or "")
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_pjm_datetime_strict(value: Any) -> datetime:
    parsed = parse_pjm_datetime(value)
    if parsed is None:
        raise ValueError(f"Unsupported PJM datetime format: {value!r}")
    return parsed
