"""Per-feed series query: bitemporal slice -> target-window filter -> resample.

This is the read path of `/api/series`. All bitemporal logic is delegated to
`pjm_engine.data.CachedView`; this module only does target-window filtering,
zone projection, and pandas resampling.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import pandas as pd

from .errors import (
    LOADER_FAILURE,
    MISSING_ZONE_DATA,
    PRE_DATA_DECISION_TIME,
    FeedError,
)
from .feed_registry import FEEDS, FeedSpec
from .runtime import Runtime

Resolution = Literal["auto", "5min", "15min", "1h", "1d"]

# Pandas freq strings keyed by resolution name.
_FREQ = {"5min": "5min", "15min": "15min", "1h": "1h", "1d": "1D"}


def _auto_resolution(target_from: datetime, target_to: datetime) -> str:
    span = target_to - target_from
    if span <= timedelta(days=2):
        return "5min"
    if span <= timedelta(days=14):
        return "15min"
    if span <= timedelta(days=90):
        return "1h"
    return "1d"


def resolve_resolution(requested: Resolution, target_from: datetime, target_to: datetime) -> str:
    return _auto_resolution(target_from, target_to) if requested == "auto" else requested


@dataclass(frozen=True)
class SeriesResult:
    """Long-form series rows + per-feed errors collected during the query.

    `frame` columns: t (UTC tz-aware), feed (str), value (float|null),
    source_zone (str). Sorted by (feed, t).
    """

    frame: pd.DataFrame
    resolution: str
    errors: list[FeedError]


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "t": pd.Series(dtype="datetime64[ns, UTC]"),
            "feed": pd.Series(dtype="string"),
            "value": pd.Series(dtype="float64"),
            "source_zone": pd.Series(dtype="string"),
        }
    )


def _slice_one(
    runtime: Runtime,
    feed: FeedSpec,
    decision_time: datetime,
    target_from: datetime,
    target_to: datetime,
    zone: str | None,
    freq: str,
) -> tuple[pd.DataFrame | None, FeedError | None]:
    lr = runtime.get(feed.loader.key)
    visible = lr.view.at(decision_time)

    if len(visible) == 0:
        return None, FeedError(
            feed=feed.feed_id,
            code=PRE_DATA_DECISION_TIME,
            message=f"no rows published before {decision_time.isoformat()}",
        )

    # Zone filter (zonal loaders only).
    if feed.loader.zonal:
        if zone is None:
            return None, FeedError(
                feed=feed.feed_id,
                code=MISSING_ZONE_DATA,
                message="feed is zonal but no zone supplied",
            )
        visible = visible[visible["zone"] == zone]
        if len(visible) == 0:
            return None, FeedError(
                feed=feed.feed_id,
                code=MISSING_ZONE_DATA,
                message=f"no rows for zone={zone!r}",
            )

    # Target-window filter (target_time is `datetime_beginning_utc`).
    target_col = "datetime_beginning_utc"
    t = visible[target_col]
    mask = (t >= pd.Timestamp(target_from)) & (t < pd.Timestamp(target_to))
    win_cols = [target_col, feed.value_column, "published_at"]
    win = visible.loc[mask, win_cols]
    if len(win) == 0:
        # Empty window is not an error — just no data in the slice.
        return None, None
    win = win.dropna(subset=[feed.value_column])
    if len(win) == 0:
        return None, None

    if feed.has_revisions:
        win = (
            win.sort_values([target_col, "published_at"], kind="stable")
            .groupby(target_col, as_index=False)
            .tail(1)
        )
    win = win[[target_col, feed.value_column]]

    # Resample to the requested bucket. Mean aggregator is sensible for both
    # prices and MW; tail behavior (rare nulls) preserved by .mean(numeric_only=True).
    win = win.set_index(target_col)
    bucketed = win[feed.value_column].resample(freq).mean()

    source_zone = zone if feed.loader.zonal else ""
    out = pd.DataFrame(
        {
            "t": bucketed.index.tz_convert("UTC"),
            "feed": feed.feed_id,
            "value": bucketed.to_numpy(),
            "source_zone": source_zone,
        }
    )
    return out, None


def query(
    runtime: Runtime,
    feeds: list[str],
    decision_time: datetime,
    target_from: datetime,
    target_to: datetime,
    zone: str | None,
    resolution: Resolution = "auto",
) -> SeriesResult:
    """Query a long-form multi-feed bitemporal slice.

    Unknown feed_ids must be filtered upstream — this function raises KeyError
    for missing FeedSpecs (the API layer translates that to 400 unsupported_feed).
    """
    freq = resolve_resolution(resolution, target_from, target_to)
    pieces: list[pd.DataFrame] = []
    errors: list[FeedError] = []

    for feed_id in feeds:
        feed = FEEDS[feed_id]
        try:
            frame, err = _slice_one(
                runtime, feed, decision_time, target_from, target_to, zone, freq
            )
        except Exception as exc:  # loader internals raised
            errors.append(FeedError(feed=feed_id, code=LOADER_FAILURE, message=str(exc)))
            continue
        if err is not None:
            errors.append(err)
        if frame is not None:
            pieces.append(frame)

    if not pieces:
        return SeriesResult(frame=_empty_frame(), resolution=freq, errors=errors)

    out = pd.concat(pieces, ignore_index=True)
    out = out.sort_values(["feed", "t"], kind="stable").reset_index(drop=True)
    return SeriesResult(frame=out, resolution=freq, errors=errors)
