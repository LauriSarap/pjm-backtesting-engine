"""End-to-end smoke: compare() + write_html() against synthetic parquet."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pjm_eval.report import compare, write_html
from pjm_eval.time import TimeWindow
from tests.conftest import make_revenue, make_soc

UTC = timezone.utc


def _build_run(tmp_run, label, totals_per_day, window, n_days=10):
    rows = []
    soc_rows = []
    for d in range(n_days):
        ts = window.start + timedelta(days=d)
        rows.append(
            {
                "period_start_utc": ts,
                "product": "DA_Energy",
                "revenue": totals_per_day,
            }
        )
        soc_rows.append((ts, 200.0))
    rev = make_revenue(rows)
    soc = make_soc(soc_rows)
    return tmp_run(label, rev, soc, window)


def test_compare_two_strategies_with_baseline_and_ceiling(tmp_run, asset_a):
    window = TimeWindow(datetime(2025, 11, 1, tzinfo=UTC), datetime(2025, 11, 15, tzinfo=UTC))
    base = _build_run(tmp_run, "naive", totals_per_day=100.0, window=window)
    strat = _build_run(tmp_run, "alpha", totals_per_day=150.0, window=window)
    ceiling = _build_run(tmp_run, "ceiling", totals_per_day=200.0, window=window)

    data = compare([strat], asset=asset_a, baseline=base, ceiling=ceiling)
    assert set(data.strategies.keys()) == {"naive", "alpha", "ceiling"}
    # alpha at 150 between 100 (base) and 200 (ceil) → 50% capture.
    assert abs(data.captures["alpha"] - 0.5) < 1e-9


def test_write_html_produces_nonempty_file(tmp_path, tmp_run, asset_a):
    window = TimeWindow(datetime(2025, 11, 1, tzinfo=UTC), datetime(2025, 11, 5, tzinfo=UTC))
    base = _build_run(tmp_run, "naive", 100.0, window, n_days=4)
    strat = _build_run(tmp_run, "alpha", 150.0, window, n_days=4)

    data = compare([strat], asset=asset_a, baseline=base)
    out = tmp_path / "report.html"
    write_html(data, out)
    html = out.read_text()
    assert "<html" in html
    assert "Leaderboard" in html
    assert "naive" in html
    assert "alpha" in html
