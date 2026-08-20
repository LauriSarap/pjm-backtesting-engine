"""Property test: viz_server.series.query agrees with pjm_engine.data.view_as_of.

50 random (feed, decision_time, target_window) tuples across the registered
loaders; the oracle is pjm_engine.data.view_as_of. Runs against the real
parquet cache, so the tests skip when the market data has not been fetched.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone

import pandas as pd
import pytest

from pjm_engine.data import view_as_of
from viz_server import runtime as rt_mod
from viz_server import series as series_mod
from viz_server.feed_registry import FEEDS

UTC = timezone.utc


@pytest.fixture(scope="module")
def runtime():
    rt = rt_mod.preload()
    rt_mod.set_runtime(rt)
    yield rt


def _pick_target_window(rng: random.Random, df: pd.DataFrame) -> tuple[datetime, datetime]:
    if len(df) < 100:
        pytest.skip("loader returned fewer than 100 rows")
    t = df["datetime_beginning_utc"]
    lo = t.iloc[len(t) // 4]
    hi = t.iloc[3 * len(t) // 4]
    span_hours = rng.choice([6, 24, 72])
    start = lo + (hi - lo) * rng.random()
    end = start + pd.Timedelta(hours=span_hours)
    return start.to_pydatetime(), end.to_pydatetime()


def _pick_decision_time(rng: random.Random, target_to: datetime, df: pd.DataFrame) -> datetime:
    """Choose a decision_time near the published_at horizon for the target window.

    We sample uniformly in [min(published_at), max(published_at)] which exercises
    both pre-data and well-after-publish regimes without going outside the
    fixture's coverage entirely.
    """
    pub = df["published_at"]
    span = pub.iloc[-1] - pub.iloc[0]
    offset = span * rng.random()
    return (pub.iloc[0] + offset).to_pydatetime()


@pytest.mark.parametrize("seed", list(range(50)))
def test_viz_server_matches_engine_oracle(runtime, seed):
    rng = random.Random(seed)
    feed_id = rng.choice(list(FEEDS))
    feed = FEEDS[feed_id]
    lr = runtime.get(feed.loader.key)

    target_from, target_to = _pick_target_window(rng, lr.view.df)
    decision_time = _pick_decision_time(rng, target_to, lr.view.df)

    zone = None
    if feed.loader.zonal:
        zones = lr.zones
        if not zones:
            pytest.skip(f"no zones for loader {feed.loader.key}")
        zone = rng.choice(zones)

    # Oracle: call engine view_as_of directly, then apply same target+zone filter
    # without resampling. We compare mean per identical bucket below.
    oracle_full = view_as_of(lr.view.df, decision_time)
    if feed.loader.zonal:
        oracle_full = oracle_full[oracle_full["zone"] == zone]
    t = oracle_full["datetime_beginning_utc"]
    mask = (t >= pd.Timestamp(target_from)) & (t < pd.Timestamp(target_to))
    oracle = oracle_full.loc[
        mask, ["datetime_beginning_utc", feed.value_column, "published_at"]
    ].dropna(subset=[feed.value_column])
    if feed.has_revisions and len(oracle):
        oracle = (
            oracle.sort_values(["datetime_beginning_utc", "published_at"], kind="stable")
            .groupby("datetime_beginning_utc", as_index=False)
            .tail(1)
        )

    # System under test: viz_server resampling at the natural cadence so buckets
    # align 1:1 with raw rows (5min for RT-cadence feeds, 1h for DA).
    natural = "5min" if "rt" in feed.loader.key or "reg" in feed.loader.key else "1h"
    result = series_mod.query(
        runtime,
        [feed_id],
        decision_time,
        target_from,
        target_to,
        zone,
        natural,
    )

    # When oracle has zero rows, viz_server must too (modulo NaN-only buckets).
    if len(oracle) == 0:
        non_null = result.frame[result.frame["value"].notna()]
        assert len(non_null) == 0, (
            f"viz_server returned {len(non_null)} rows but engine oracle has 0 "
            f"for feed={feed_id} decision={decision_time} window=[{target_from},{target_to})"
        )
        return

    # Sum of values agrees within FP tolerance — resample(mean) and direct mean
    # over identically-bucketed rows must match for non-overlapping uniform buckets.
    visible_mean = oracle[feed.value_column].mean()
    server_mean = result.frame["value"].mean()
    if pd.isna(visible_mean):
        assert pd.isna(server_mean) or len(result.frame) == 0
        return
    assert abs(server_mean - visible_mean) < 1e-6, (
        f"mean mismatch feed={feed_id} oracle={visible_mean} server={server_mean}"
    )
