"""Strategy run loader tests for the viz tool integration.

Covers:
- decision-time clamp: event_ts_utc <= decision_time
- empty window returns empty DataFrame (not exception)
- PF ceiling loader rejects non-PF input
- PF ceiling loader does NOT clamp on decision_time
- schema_check_at_boot raises SchemaMismatchError on missing columns
- find_strategy_run raises StrategyNotFound / RunNotFound by name
- list_strategy_runs walks the directory and returns metadata
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from pjm_eval.io import (
    PF_STRATEGY_NAME,
    RunNotFound,
    SchemaMismatchError,
    StrategyNotFound,
    find_strategy_run,
    list_strategy_runs,
    load_pf_ceiling_revenue,
    load_strategy_revenue,
    schema_check_at_boot,
)
from pjm_eval.time import TimeWindow
from tests.conftest import make_revenue, make_soc

UTC = timezone.utc


# ─── Test fixtures specific to strategy run discovery ────────────────────


def _write_run(
    runs_root: Path,
    run_name: str,
    strategy: str,
    asset_id: str,
    rev: pd.DataFrame,
    soc: pd.DataFrame,
) -> Path:
    """Create the on-disk layout viz_server expects."""
    strat_dir = runs_root / run_name / strategy
    strat_dir.mkdir(parents=True, exist_ok=True)
    rev.to_parquet(strat_dir / f"revenue_{asset_id}.parquet")
    soc.to_parquet(strat_dir / f"soc_{asset_id}.parquet")
    return strat_dir


def _three_revenue_rows():
    """Three DA awards: events at 10:00, 12:00, 14:00; periods later that day."""
    base = datetime(2025, 10, 15, tzinfo=UTC)
    rows = []
    for hour in (10, 12, 14):
        rows.append(
            {
                "asset_id": "example_a",
                "period_start_utc": base + timedelta(hours=hour),
                "period_end_utc": base + timedelta(hours=hour + 1),
                "cleared_mw": 100.0,
                "clearing_price": 30.0,
                "revenue": 3000.0,
            }
        )
    df = make_revenue(rows)
    # Override event_ts_utc to be 1h before each period (stand-in for DA gate).
    df["event_ts_utc"] = df["period_start_utc"] - pd.Timedelta(hours=1)
    return df


# ─── load_strategy_revenue: decision-time clamp ─────────────────────────────


def test_revenue_clamps_on_decision_time(tmp_path):
    rev = _three_revenue_rows()
    soc = make_soc([(datetime(2025, 10, 15, tzinfo=UTC), 200.0)])
    _write_run(tmp_path, "demo", "alpha", "example_a", rev, soc)

    info = find_strategy_run(tmp_path, "demo", "alpha", "example_a")
    # Decision time between events 1 and 2: only the first event is visible.
    decision_time = datetime(2025, 10, 15, 10, 0, tzinfo=UTC)
    df = load_strategy_revenue(info, info.window, decision_time)
    assert len(df) == 1
    assert df.iloc[0]["period_start_utc"] == datetime(2025, 10, 15, 10, tzinfo=UTC)


def test_revenue_decision_time_includes_equal_event_ts(tmp_path):
    """event_ts_utc <= decision_time, not strictly less than."""
    rev = _three_revenue_rows()
    _write_run(
        tmp_path,
        "demo",
        "alpha",
        "example_a",
        rev,
        make_soc([(datetime(2025, 10, 15, tzinfo=UTC), 200.0)]),
    )

    info = find_strategy_run(tmp_path, "demo", "alpha", "example_a")
    # Exactly equal to event_ts of the second event (11:00).
    decision_time = datetime(2025, 10, 15, 11, 0, tzinfo=UTC)
    df = load_strategy_revenue(info, info.window, decision_time)
    assert len(df) == 2  # events 1 and 2 both visible


def test_revenue_empty_window_returns_empty_df(tmp_path):
    rev = _three_revenue_rows()
    _write_run(
        tmp_path,
        "demo",
        "alpha",
        "example_a",
        rev,
        make_soc([(datetime(2025, 10, 15, tzinfo=UTC), 200.0)]),
    )

    info = find_strategy_run(tmp_path, "demo", "alpha", "example_a")
    far_future = TimeWindow(
        start=datetime(2030, 1, 1, tzinfo=UTC),
        end=datetime(2030, 1, 2, tzinfo=UTC),
    )
    df = load_strategy_revenue(
        info,
        far_future,
        datetime(2030, 1, 2, tzinfo=UTC),
    )
    assert df.empty
    # Frame still has the schema columns even when empty (no exception path).
    from pjm_eval.io import REVENUE_REQUIRED_COLS

    assert REVENUE_REQUIRED_COLS <= set(df.columns)


# ─── load_pf_ceiling_revenue: oracle, no clamp ────────────────────────────


def test_pf_loader_rejects_non_pf_strategy(tmp_path):
    rev = _three_revenue_rows()
    _write_run(
        tmp_path,
        "demo",
        "alpha",
        "example_a",
        rev,
        make_soc([(datetime(2025, 10, 15, tzinfo=UTC), 200.0)]),
    )

    info = find_strategy_run(tmp_path, "demo", "alpha", "example_a")
    with pytest.raises(ValueError, match="non-PF strategy"):
        load_pf_ceiling_revenue(info, info.window)


def test_pf_loader_returns_all_rows(tmp_path):
    """No decision_time arg; PF returns the entire window regardless."""
    rev = _three_revenue_rows()
    _write_run(
        tmp_path,
        "demo",
        PF_STRATEGY_NAME,
        "example_a",
        rev,
        make_soc([(datetime(2025, 10, 15, tzinfo=UTC), 200.0)]),
    )

    info = find_strategy_run(
        tmp_path,
        "demo",
        PF_STRATEGY_NAME,
        "example_a",
    )
    assert info.is_pf_ceiling()
    df = load_pf_ceiling_revenue(info, info.window)
    assert len(df) == 3  # all three events present, no clamp


# ─── Discovery: find_strategy_run / list_strategy_runs ────────────────────


def test_find_strategy_run_raises_run_not_found(tmp_path):
    with pytest.raises(RunNotFound, match="run 'nonexistent'"):
        find_strategy_run(tmp_path, "nonexistent", "alpha", "example_a")


def test_find_strategy_run_raises_strategy_not_found(tmp_path):
    rev = _three_revenue_rows()
    _write_run(
        tmp_path,
        "demo",
        "alpha",
        "example_a",
        rev,
        make_soc([(datetime(2025, 10, 15, tzinfo=UTC), 200.0)]),
    )

    with pytest.raises(StrategyNotFound, match="strategy 'absent'"):
        find_strategy_run(tmp_path, "demo", "absent", "example_a")


def test_find_strategy_run_raises_strategy_not_found_for_missing_asset(tmp_path):
    rev = _three_revenue_rows()
    _write_run(
        tmp_path,
        "demo",
        "alpha",
        "example_a",
        rev,
        make_soc([(datetime(2025, 10, 15, tzinfo=UTC), 200.0)]),
    )

    with pytest.raises(StrategyNotFound, match="asset 'example_b'"):
        find_strategy_run(tmp_path, "demo", "alpha", "example_b")


def test_list_strategy_runs_walks_directory(tmp_path):
    rev = _three_revenue_rows()
    soc = make_soc([(datetime(2025, 10, 15, tzinfo=UTC), 200.0)])
    _write_run(tmp_path, "demo", "alpha", "example_a", rev, soc)
    _write_run(tmp_path, "demo", PF_STRATEGY_NAME, "example_a", rev, soc)
    _write_run(tmp_path, "full", "alpha", "example_a", rev, soc)

    infos = list_strategy_runs(tmp_path)
    assert len(infos) == 3
    keys = {(i.run_name, i.strategy) for i in infos}
    assert keys == {
        ("demo", "alpha"),
        ("demo", PF_STRATEGY_NAME),
        ("full", "alpha"),
    }


def test_list_strategy_runs_skips_assets_without_soc(tmp_path):
    """A revenue parquet without a matching soc parquet is skipped, not raised."""
    rev = _three_revenue_rows()
    strat_dir = tmp_path / "demo" / "alpha"
    strat_dir.mkdir(parents=True)
    rev.to_parquet(strat_dir / "revenue_example_a.parquet")
    # NO soc_example_a.parquet on purpose.

    infos = list_strategy_runs(tmp_path)
    assert infos == []


def test_list_strategy_runs_empty_when_no_runs_dir(tmp_path):
    assert list_strategy_runs(tmp_path / "doesnotexist") == []


# ─── schema_check_at_boot: fail-fast ──────────────────────────────────────


def test_schema_check_passes_on_valid_parquets(tmp_path):
    rev = _three_revenue_rows()
    soc = make_soc([(datetime(2025, 10, 15, tzinfo=UTC), 200.0)])
    _write_run(tmp_path, "demo", "alpha", "example_a", rev, soc)

    schema_check_at_boot(tmp_path)  # No raise.


def test_schema_check_raises_on_missing_revenue_column(tmp_path):
    rev = _three_revenue_rows().drop(columns=["revenue"])
    soc = make_soc([(datetime(2025, 10, 15, tzinfo=UTC), 200.0)])
    strat_dir = tmp_path / "demo" / "alpha"
    strat_dir.mkdir(parents=True)
    rev.to_parquet(strat_dir / "revenue_example_a.parquet")
    soc.to_parquet(strat_dir / "soc_example_a.parquet")

    with pytest.raises(SchemaMismatchError, match="missing required columns"):
        schema_check_at_boot(tmp_path)


def test_schema_check_raises_on_missing_soc_column(tmp_path):
    rev = _three_revenue_rows()
    bad_soc = pd.DataFrame({"ts_utc": [datetime(2025, 10, 15, tzinfo=UTC)]})
    # Missing soc_mwh.
    strat_dir = tmp_path / "demo" / "alpha"
    strat_dir.mkdir(parents=True)
    rev.to_parquet(strat_dir / "revenue_example_a.parquet")
    bad_soc.to_parquet(strat_dir / "soc_example_a.parquet")

    with pytest.raises(SchemaMismatchError, match="soc parquet"):
        schema_check_at_boot(tmp_path)


def test_schema_check_no_runs_dir_is_noop(tmp_path):
    schema_check_at_boot(tmp_path / "doesnotexist")  # No raise.
