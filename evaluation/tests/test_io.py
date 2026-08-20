"""IO tests: parquet schema validation, asset config parser, SoC derivation."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from pjm_eval.io import (
    load_revenue,
    load_soc,
    parse_pjm_assets_md,
)
from pjm_eval.time import TimeWindow
from tests.conftest import make_revenue, make_soc

UTC = timezone.utc


# ─── parse_pjm_assets_md ──────────────────────────────────────────────────

_ASSETS_MD = """\
# PJM assets

## Sizing

| Asset | Zone | MW / MWh |
|---|---|---:|
| Example BESS | PJM-RTO | 250 / 1,000 |
| Example Small | PJM-RTO | 50 / 200 |

## Constraints

SoC band 10-90%, RTE 0.85, cycle cost $5/MWh — uniform across assets.
"""


def test_parse_assets_md_returns_all_rows(tmp_path):
    md = tmp_path / "example-assets.md"
    md.write_text(_ASSETS_MD)
    assets = parse_pjm_assets_md(md)
    assert len(assets) == 2
    assert "Example BESS" in assets
    a = assets["Example BESS"]
    assert a.zone == "PJM-RTO"
    assert a.power_mw == 250.0
    # "250 / 1,000" exercises the thousands-separator stripping.
    assert a.energy_mwh == 1000.0
    assert assets["Example Small"].energy_mwh == 200.0


def test_parse_assets_md_defaults(tmp_path):
    """SoC band, RTE, cycle cost should be set to defaults from the markdown's
    constraints section."""
    md = tmp_path / "example-assets.md"
    md.write_text(_ASSETS_MD)
    a = parse_pjm_assets_md(md)["Example BESS"]
    assert a.soc_min_pct == 0.10
    assert a.soc_max_pct == 0.90
    assert a.rte == 0.85
    assert a.cycle_cost_per_mwh == 5.0


# ─── load_revenue ─────────────────────────────────────────────────────────


def test_load_revenue_rejects_missing_cols(tmp_path, asset_a, tmp_run):
    bad = pd.DataFrame({"revenue": [1.0]})
    bad_path = tmp_path / "bad.parquet"
    bad.to_parquet(bad_path)
    from pjm_eval.io import Run

    run = Run(
        label="bad",
        asset_id="X",
        window=TimeWindow(datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
        revenue_path=bad_path,
        soc_path=bad_path,
    )
    with pytest.raises(ValueError, match="missing cols"):
        load_revenue(run)


def test_load_revenue_filters_by_asset_and_window(tmp_run, asset_a):
    rev = make_revenue(
        [
            {
                "period_start_utc": datetime(2025, 6, 1, h, tzinfo=UTC),
                "asset_id": "Example BESS",
                "revenue": 100.0,
            }
            for h in range(5)
        ]
        + [
            # outside window
            {
                "period_start_utc": datetime(2024, 12, 1, h, tzinfo=UTC),
                "asset_id": "Example BESS",
                "revenue": 9999.0,
            }
            for h in range(5)
        ]
        + [
            # different asset
            {
                "period_start_utc": datetime(2025, 6, 1, h, tzinfo=UTC),
                "asset_id": "Other",
                "revenue": 9999.0,
            }
            for h in range(5)
        ]
    )
    soc = make_soc([(datetime(2025, 6, 1, h, tzinfo=UTC), 200.0) for h in range(5)])
    window = TimeWindow(datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    run = tmp_run("test", rev, soc, window)
    loaded = load_revenue(run)
    assert len(loaded) == 5
    assert (loaded["asset_id"] == "Example BESS").all()
    assert loaded["revenue"].sum() == 500.0


def test_load_revenue_rejects_nan_revenue(tmp_run, asset_a):
    rev = make_revenue([{"period_start_utc": datetime(2025, 6, 1, tzinfo=UTC), "revenue": 100.0}])
    rev.loc[0, "revenue"] = float("nan")
    soc = make_soc([(datetime(2025, 6, 1, tzinfo=UTC), 200.0)])
    window = TimeWindow(datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    run = tmp_run("nan", rev, soc, window)
    with pytest.raises(ValueError, match="NaN revenue"):
        load_revenue(run)


# ─── load_soc ─────────────────────────────────────────────────────────────


def test_load_soc_derives_pct(tmp_run, asset_a):
    rev = make_revenue([{"period_start_utc": datetime(2025, 6, 1, tzinfo=UTC), "revenue": 100.0}])
    soc_rows = [
        (datetime(2025, 6, 1, h, tzinfo=UTC), float(h * 50))  # 0, 50, 100, ...
        for h in range(5)
    ]
    soc = make_soc(soc_rows)
    window = TimeWindow(datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    run = tmp_run("soc", rev, soc, window)
    loaded = load_soc(run, asset_a)
    assert "soc_pct" in loaded.columns
    # asset_a.energy_mwh = 1000; row at h=4 has soc_mwh=200 → 0.2
    assert abs(loaded.iloc[4]["soc_pct"] - 0.2) < 1e-9


def test_load_soc_derives_cycles(tmp_run, asset_a):
    """Throughput 50 + 50 = 100 MWh on a 1000 MWh asset → 0.05 cycles."""
    rev = make_revenue([{"period_start_utc": datetime(2025, 6, 1, tzinfo=UTC), "revenue": 100.0}])
    soc = make_soc(
        [
            (datetime(2025, 6, 1, 0, tzinfo=UTC), 200.0),
            (datetime(2025, 6, 1, 1, tzinfo=UTC), 250.0),  # +50
            (datetime(2025, 6, 1, 2, tzinfo=UTC), 200.0),  # -50
        ]
    )
    window = TimeWindow(datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    run = tmp_run("cyc", rev, soc, window)
    loaded = load_soc(run, asset_a)
    assert abs(loaded["cum_cycles"].iloc[-1] - 100.0 / (2 * 1000.0)) < 1e-9


def test_load_soc_optional_cols_become_na(tmp_run, asset_a):
    rev = make_revenue([{"period_start_utc": datetime(2025, 6, 1, tzinfo=UTC), "revenue": 100.0}])
    soc = make_soc([(datetime(2025, 6, 1, h, tzinfo=UTC), 200.0) for h in range(3)])
    window = TimeWindow(datetime(2025, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC))
    run = tmp_run("opt", rev, soc, window)
    loaded = load_soc(run, asset_a)
    for col in ("charge_mw", "discharge_mw", "reg_deployment_mw"):
        assert col in loaded.columns
        assert loaded[col].isna().all()
