"""DataView AS-feed accessors — bitemporal correctness + missing-data handling.

Locks in the AS-feed accessors: `da_sr_prices`, `rt_sr_prices`,
`da_sec_prices`, `rt_sec_prices`, and `reg_market_results` are loaded by the
runner for clearing AND exposed to strategies. This test file pins:

1. each accessor returns a DataFrame with the expected columns,
2. the bitemporal `published_at <= as_of` filter applies (synthetic fixture,
   no dependency on real data),
3. when a feed's underlying parquet is absent the accessor returns an empty
   schema-shaped frame and emits a warning (does NOT raise).

The accessors lazy-load from parquet via a module-level cache. To keep the
missing-data test hermetic without nuking the cache for the whole session,
that test uses `monkeypatch` to replace the loader.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from pjm_engine import strategy_base
from pjm_engine.strategy_base import (
    _AS_PRICE_SCHEMA,
    _REG_MARKET_RESULTS_SCHEMA,
    DataView,
)

UTC = ZoneInfo("UTC")
EPT = ZoneInfo("America/New_York")


# ─── Fixtures ────────────────────────────────────────────────────────────────


def _ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _make_as_price_frame(starts: list[datetime], pub_offset_min: int) -> pd.DataFrame:
    """Synthetic AS price frame with the canonical 5-column AS-price schema.

    `published_at = mtu_start + pub_offset_min` keeps `published_at`
    monotonic since `starts` is monotonic — which is the precondition the
    `searchsorted` slice in `_bitemporal_slice` relies on.
    """
    pubs = [s + timedelta(minutes=pub_offset_min) for s in starts]
    return pd.DataFrame(
        {
            "datetime_beginning_utc": pd.Series(starts, dtype="datetime64[ns, UTC]"),
            "datetime_beginning_ept": pd.Series(
                [s.astimezone(EPT) for s in starts], dtype="datetime64[ns, US/Eastern]"
            ),
            "mcp": [1.0 + i for i in range(len(starts))],
            "mcp_capped": [1.0 + i for i in range(len(starts))],
            "published_at": pd.Series(pubs, dtype="datetime64[ns, UTC]"),
        }
    )


def _make_reg_market_results_frame(starts: list[datetime]) -> pd.DataFrame:
    """Synthetic reg_market_results frame with all 17 canonical columns.

    Per M11 §3.7.5 the engine fallback stamps `published_at = block_start − 10
    min`. We reuse that here so the synthetic frame matches the real loader
    convention, which keeps the bitemporal-filter test below honest.
    """
    pubs = [s - timedelta(minutes=10) for s in starts]
    n = len(starts)
    return pd.DataFrame(
        {
            "datetime_beginning_utc": pd.Series(starts, dtype="datetime64[ns, UTC]"),
            "datetime_beginning_ept": pd.Series(
                [s.astimezone(EPT) for s in starts], dtype="datetime64[ns, US/Eastern]"
            ),
            "requirement": [1500.0] * n,
            "regd_ssmw": [None] * n,
            "rega_ssmw": [800.0] * n,
            "regd_procure": [None] * n,
            "rega_procure": [800.0] * n,
            "total_mw": [800.0] * n,
            "deficiency": [0.0] * n,
            "rto_perfscore": [0.93] * n,
            "rega_mileage": [12.5] * n,
            "regd_mileage": [None] * n,
            "rega_hourly": [25.0] * n,
            "regd_hourly": [None] * n,
            "is_approved": [True] * n,
            "modified_datetime_utc": pd.Series(pubs, dtype="datetime64[ns, UTC]"),
            "published_at": pd.Series(pubs, dtype="datetime64[ns, UTC]"),
        }
    )


@pytest.fixture
def synthetic_da_sr() -> pd.DataFrame:
    """6 hourly rows, published_at = D-1 13:30 EPT (DA gate convention)."""
    op_starts = [_ts(2025, 11, 3, h) for h in range(6)]
    pubs = [datetime(2025, 11, 2, 13, 30, tzinfo=EPT).astimezone(UTC) for _ in op_starts]
    df = pd.DataFrame(
        {
            "datetime_beginning_utc": pd.Series(op_starts, dtype="datetime64[ns, UTC]"),
            "datetime_beginning_ept": pd.Series(
                [s.astimezone(EPT) for s in op_starts], dtype="datetime64[ns, US/Eastern]"
            ),
            "mcp": [5.0, 6.0, 7.0, 8.0, 9.0, 10.0],
            "mcp_capped": [5.0, 6.0, 7.0, 8.0, 9.0, 10.0],
            "published_at": pd.Series(pubs, dtype="datetime64[ns, UTC]"),
        }
    )
    return df.sort_values(["published_at", "datetime_beginning_utc"]).reset_index(drop=True)


@pytest.fixture
def synthetic_rt_sr() -> pd.DataFrame:
    """12 5-min rows, published_at = mtu_start + 10 min (M11 §3.7.6)."""
    starts = [_ts(2025, 11, 3, 5, 5 * i) for i in range(12)]
    return _make_as_price_frame(starts, pub_offset_min=10)


@pytest.fixture
def synthetic_da_sec(synthetic_da_sr) -> pd.DataFrame:
    """Same shape as DA SR — Sec uses the identical schema."""
    return synthetic_da_sr.copy()


@pytest.fixture
def synthetic_rt_sec(synthetic_rt_sr) -> pd.DataFrame:
    return synthetic_rt_sr.copy()


@pytest.fixture
def synthetic_reg_market_results() -> pd.DataFrame:
    """6 half-hourly rows for a post-redesign Reg_v2 day."""
    base = _ts(2025, 11, 3, 5, 0)
    starts = [base + timedelta(minutes=30 * i) for i in range(6)]
    return _make_reg_market_results_frame(starts)


def _view(as_of: datetime, **kwargs) -> DataView:
    """Build a DataView with empty `da_lmps` (required positional) + injected AS frames."""
    return DataView(as_of=as_of, da_lmps=pd.DataFrame(), **kwargs)


# ─── Per-accessor schema tests ────────────────────────────────────────────────


def test_da_sr_prices_returns_expected_columns(synthetic_da_sr):
    as_of = _ts(2025, 11, 3, 23, 0)  # well after every published_at
    view = _view(as_of, _da_sr_prices=synthetic_da_sr)
    out = view.da_sr_prices()
    assert isinstance(out, pd.DataFrame)
    assert list(out.columns) == _AS_PRICE_SCHEMA
    assert len(out) == 6


def test_rt_sr_prices_returns_expected_columns(synthetic_rt_sr):
    as_of = _ts(2025, 11, 3, 23, 0)
    view = _view(as_of, _rt_sr_prices=synthetic_rt_sr)
    out = view.rt_sr_prices()
    assert isinstance(out, pd.DataFrame)
    assert list(out.columns) == _AS_PRICE_SCHEMA
    assert len(out) == 12


def test_da_sec_prices_returns_expected_columns(synthetic_da_sec):
    as_of = _ts(2025, 11, 3, 23, 0)
    view = _view(as_of, _da_sec_prices=synthetic_da_sec)
    out = view.da_sec_prices()
    assert isinstance(out, pd.DataFrame)
    assert list(out.columns) == _AS_PRICE_SCHEMA
    assert len(out) == 6


def test_rt_sec_prices_returns_expected_columns(synthetic_rt_sec):
    as_of = _ts(2025, 11, 3, 23, 0)
    view = _view(as_of, _rt_sec_prices=synthetic_rt_sec)
    out = view.rt_sec_prices()
    assert isinstance(out, pd.DataFrame)
    assert list(out.columns) == _AS_PRICE_SCHEMA
    assert len(out) == 12


def test_reg_market_results_returns_expected_columns(synthetic_reg_market_results):
    as_of = _ts(2025, 11, 3, 23, 0)
    view = _view(as_of, _reg_market_results=synthetic_reg_market_results)
    out = view.reg_market_results()
    assert isinstance(out, pd.DataFrame)
    assert list(out.columns) == _REG_MARKET_RESULTS_SCHEMA
    assert len(out) == 6


# ─── Bitemporal filter (the load-bearing guarantee) ───────────────────────────


def test_bitemporal_filter_excludes_future_published_rows(synthetic_rt_sr):
    """Pin `as_of` between two MTU publish boundaries; rows published *after*
    `as_of` must not appear in the accessor's output. This is the same
    `published_at <= as_of` contract `view_as_of` enforces for da_lmps.
    """
    # synthetic_rt_sr has MTU starts at 05:00, 05:05, 05:10, ... and
    # published_at = mtu + 10min, so publishes at 05:10, 05:15, 05:20, ...
    # Pin as_of = 05:18 → rows at 05:00 and 05:05 should be visible
    # (publish 05:10 and 05:15 ≤ 05:18); 05:10's publish is 05:20 > as_of,
    # so that row and everything after must be excluded.
    as_of = _ts(2025, 11, 3, 5, 18)
    view = _view(as_of, _rt_sr_prices=synthetic_rt_sr)
    out = view.rt_sr_prices()
    assert (out["published_at"] <= pd.Timestamp(as_of)).all(), (
        f"leak: max published_at {out['published_at'].max()} > as_of {as_of}"
    )
    # Cross-check: exactly the first 2 rows (05:00 and 05:05) should be visible.
    assert len(out) == 2
    assert out["datetime_beginning_utc"].tolist() == [
        _ts(2025, 11, 3, 5, 0),
        _ts(2025, 11, 3, 5, 5),
    ]


def test_bitemporal_filter_at_as_of_before_any_publish_returns_empty(synthetic_rt_sr):
    """An `as_of` strictly before the first `published_at` returns 0 rows."""
    as_of = _ts(2025, 11, 3, 4, 0)  # well before first publish at 05:10
    view = _view(as_of, _rt_sr_prices=synthetic_rt_sr)
    out = view.rt_sr_prices()
    assert len(out) == 0


def test_bitemporal_filter_applies_to_reg_market_results(synthetic_reg_market_results):
    """Same contract for the half-hour reg_market_results feed."""
    # published_at = start − 10 min (M11 §3.7.5). First row start 05:00 →
    # publish 04:50. Second row start 05:30 → publish 05:20. Pin as_of = 05:00
    # → only the first row should be visible (04:50 ≤ 05:00; 05:20 > 05:00).
    as_of = _ts(2025, 11, 3, 5, 0)
    view = _view(as_of, _reg_market_results=synthetic_reg_market_results)
    out = view.reg_market_results()
    assert len(out) == 1
    assert (out["published_at"] <= pd.Timestamp(as_of)).all()


# ─── Missing-data handling (no exception, warn-once, empty schema-shaped) ─────


def test_missing_feed_returns_empty_schema_frame_and_warns(monkeypatch, caplog):
    """If the underlying loader raises FileNotFoundError, the accessor must
    return an empty DataFrame WITH the expected schema and emit a single
    WARNING. This is the contract the recon note's Resolution section
    documents — silent swallows are not acceptable.
    """
    # Reset the module-level cache so this test is hermetic regardless of
    # whether earlier tests in the session already lazy-loaded the real feed.
    strategy_base._as_feed_cache.pop("load_da_sr_prices", None)

    # Replace the loader with one that raises, mimicking absent data/raw/.
    def _absent_loader():
        raise FileNotFoundError("no da_reserve_market_results CSVs at /nope")

    from pjm_engine import data as _data

    monkeypatch.setattr(_data, "load_da_sr_prices", _absent_loader)

    as_of = _ts(2025, 11, 3, 23, 0)
    # Build a DataView that does NOT inject `_da_sr_prices` → forces the
    # lazy-load path to run.
    view = _view(as_of)

    with caplog.at_level("WARNING", logger="pjm_engine.strategy_base"):
        out = view.da_sr_prices()

    assert isinstance(out, pd.DataFrame)
    assert list(out.columns) == _AS_PRICE_SCHEMA
    assert len(out) == 0
    assert any("load_da_sr_prices unavailable" in rec.message for rec in caplog.records), (
        f"expected WARNING about missing feed, got: {[r.message for r in caplog.records]}"
    )

    # Second call must NOT re-warn (cached as _MISSING) — keeps log noise
    # bounded over a per-event accessor call.
    caplog.clear()
    out2 = view.da_sr_prices()
    assert len(out2) == 0
    assert not any("load_da_sr_prices unavailable" in rec.message for rec in caplog.records)

    # Cleanup so other tests that lazy-load the real feed see a clean cache.
    strategy_base._as_feed_cache.pop("load_da_sr_prices", None)
