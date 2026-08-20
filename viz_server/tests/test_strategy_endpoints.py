"""Endpoint tests for /api/strategy_runs and /api/strategy_series.

Calls the endpoint functions directly (sync) rather than spinning up
TestClient + lifespan, because lifespan preloads all engine feeds (~10s)
and would be wasted boot for these endpoints. The endpoints we care about
read parquet via pjm_eval.io, which doesn't depend on the engine runtime.

Hermetic: a small synthetic run (run "demo", strategy "demo_strategy", asset
"example_a", plus a perfect_foresight ceiling) is written to a temp
directory and served via $PJM_RUNS_ROOT. No fetched market data needed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from fastapi import HTTPException

from viz_server import api as viz_api

UTC = timezone.utc

RUN = "demo"
STRATEGY = "demo_strategy"
ASSET = "example_a"
WINDOW_FROM = datetime(2025, 6, 1, tzinfo=UTC)
N_HOURS = 48
WINDOW_TO = WINDOW_FROM + timedelta(hours=N_HOURS)


def _write_run(runs_root: Path, strategy: str, revenue_per_hour: float) -> None:
    """One (run, strategy, asset) with hourly DA_Energy awards.

    Each award's event_ts is 12h before its period start (DA-style lead),
    so an early decision_time clamps away the later awards.
    """
    starts = [WINDOW_FROM + timedelta(hours=h) for h in range(N_HOURS)]
    revenue = pd.DataFrame(
        {
            "event_ts_utc": [t - timedelta(hours=12) for t in starts],
            "asset_id": ASSET,
            "product": "DA_Energy",
            "period_start_utc": starts,
            "period_end_utc": [t + timedelta(hours=1) for t in starts],
            "cleared_mw": [50.0 if h % 2 else -50.0 for h in range(N_HOURS)],
            "clearing_price": 30.0,
            "revenue": revenue_per_hour,
            "formula_version": "test",
        }
    )
    soc = pd.DataFrame(
        {
            "ts_utc": starts,
            "soc_mwh": 500.0,
        }
    )
    strat_dir = runs_root / RUN / strategy
    strat_dir.mkdir(parents=True, exist_ok=True)
    revenue.to_parquet(strat_dir / f"revenue_{ASSET}.parquet", index=False)
    soc.to_parquet(strat_dir / f"soc_{ASSET}.parquet", index=False)


@pytest.fixture(scope="module")
def runs_root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("runs")
    _write_run(root, STRATEGY, revenue_per_hour=100.0)
    _write_run(root, "perfect_foresight", revenue_per_hour=250.0)
    return root


@pytest.fixture(autouse=True)
def _point_api_at_fixture(runs_root: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PJM_RUNS_ROOT", str(runs_root))


def _full_window():
    return (
        WINDOW_FROM.isoformat(),
        WINDOW_TO.isoformat(),
        WINDOW_TO.isoformat(),  # decision_time = end of window (everything visible)
    )


def test_strategy_series_smoke():
    """Synthetic parquet → endpoint → JSON response shape."""
    t_from, t_to, dt = _full_window()
    res = viz_api.api_strategy_series(
        run=RUN,
        strategy=STRATEGY,
        asset=ASSET,
        target_from=t_from,
        target_to=t_to,
        decision_time=dt,
        include_pf=True,
    )

    # Top-level shape
    assert set(res) >= {
        "strategy",
        "decision_time",
        "target_from",
        "target_to",
        "cleared_mw",
        "cum_revenue",
        "pf_ceiling",
        "partial",
    }
    # Strategy metadata
    assert res["strategy"]["strategy"] == STRATEGY
    assert res["strategy"]["asset"] == ASSET
    assert res["strategy"]["is_pf_ceiling"] is False
    # Some awards should exist
    assert len(res["cleared_mw"]) > 0
    assert len(res["cum_revenue"]) > 0
    # PF ceiling included
    assert res["pf_ceiling"] is not None
    assert len(res["pf_ceiling"]) > 0


def test_pf_ceiling_dominates_strategy():
    """The perfect-foresight ceiling must end at or above the strategy's
    cumulative revenue over the same window — PF is the oracle upper bound.
    """
    t_from, t_to, dt = _full_window()
    strat = viz_api.api_strategy_series(
        run=RUN,
        strategy=STRATEGY,
        asset=ASSET,
        target_from=t_from,
        target_to=t_to,
        decision_time=dt,
        include_pf=False,
    )
    pf = viz_api.api_strategy_series(
        run=RUN,
        strategy="perfect_foresight",
        asset=ASSET,
        target_from=t_from,
        target_to=t_to,
        decision_time=dt,
        include_pf=False,
    )
    assert pf["strategy"]["is_pf_ceiling"] is True
    assert strat["cum_revenue"], "expected at least one award"
    assert pf["cum_revenue"], "expected at least one PF award"
    strat_final = strat["cum_revenue"][-1]["value"]
    pf_final = pf["cum_revenue"][-1]["value"]
    assert pf_final >= strat_final, (
        f"PF ceiling ${pf_final:,.2f} must dominate strategy ${strat_final:,.2f}"
    )


def test_decision_time_clamp_shrinks_visible_revenue():
    """At an early decision_time, fewer awards should be visible."""
    t_from, t_to, _ = _full_window()

    # Late decision (everything visible)
    late = viz_api.api_strategy_series(
        run=RUN,
        strategy=STRATEGY,
        asset=ASSET,
        target_from=t_from,
        target_to=t_to,
        decision_time=WINDOW_TO.isoformat(),
        include_pf=False,
    )
    # Early decision: mid-window, only the earlier awards are known.
    early = viz_api.api_strategy_series(
        run=RUN,
        strategy=STRATEGY,
        asset=ASSET,
        target_from=t_from,
        target_to=t_to,
        decision_time=(WINDOW_FROM + timedelta(hours=N_HOURS // 2)).isoformat(),
        include_pf=False,
    )

    assert len(early["cum_revenue"]) < len(late["cum_revenue"]), (
        "early decision_time must clamp to fewer visible awards"
    )


def test_unknown_run_returns_404():
    t_from, t_to, dt = _full_window()
    with pytest.raises(HTTPException) as exc_info:
        viz_api.api_strategy_series(
            run="nonexistent_run",
            strategy=STRATEGY,
            asset=ASSET,
            target_from=t_from,
            target_to=t_to,
            decision_time=dt,
            include_pf=False,
        )
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["error"]["code"] == "run_not_found"


def test_unknown_strategy_returns_404():
    t_from, t_to, dt = _full_window()
    with pytest.raises(HTTPException) as exc_info:
        viz_api.api_strategy_series(
            run=RUN,
            strategy="absent_strategy",
            asset=ASSET,
            target_from=t_from,
            target_to=t_to,
            decision_time=dt,
            include_pf=False,
        )
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["error"]["code"] == "strategy_not_found"
