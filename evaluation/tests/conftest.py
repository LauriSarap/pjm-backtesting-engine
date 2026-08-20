"""Synthetic fixtures used by all tests. No real PJM data needed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from pjm_eval.io import AssetConfig, Run
from pjm_eval.time import TimeWindow

UTC = timezone.utc


def make_revenue(rows: list[dict]) -> pd.DataFrame:
    """Build a revenue DataFrame from sparse row dicts. Fills required cols."""
    out = []
    for r in rows:
        out.append(
            {
                "event_ts_utc": r["period_start_utc"],
                "asset_id": r.get("asset_id", "Example BESS"),
                "product": r.get("product", "DA_Energy"),
                "period_start_utc": r["period_start_utc"],
                "period_end_utc": r.get(
                    "period_end_utc", r["period_start_utc"] + timedelta(hours=1)
                ),
                "cleared_mw": r.get("cleared_mw", 100.0),
                "clearing_price": r.get("clearing_price", 50.0),
                "revenue": r["revenue"],
                "formula_version": r.get("formula_version", "v1"),
            }
        )
    return pd.DataFrame(out)


def make_soc(rows: list[tuple[datetime, float]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["ts_utc", "soc_mwh"])


@pytest.fixture
def asset_a() -> AssetConfig:
    return AssetConfig(
        asset_id="Example BESS",
        zone="PJM-RTO",
        power_mw=250.0,
        energy_mwh=1000.0,
    )


@pytest.fixture
def tmp_run(tmp_path: Path, asset_a: AssetConfig):
    """Factory: tmp_run(label, revenue_df, soc_df, window) → Run."""

    def _make(label: str, rev: pd.DataFrame, soc: pd.DataFrame, window: TimeWindow) -> Run:
        rev_path = tmp_path / f"revenue_{label}.parquet"
        soc_path = tmp_path / f"soc_{label}.parquet"
        rev.to_parquet(rev_path)
        soc.to_parquet(soc_path)
        return Run(
            label=label,
            asset_id="Example BESS",
            window=window,
            revenue_path=rev_path,
            soc_path=soc_path,
        )

    return _make
