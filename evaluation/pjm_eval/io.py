"""Parquet readers, Run handle, asset config from docs/example-assets.md.

No imports from engine. Product strings are opaque. Asset config comes
from the markdown file that's the single source of truth.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from .time import TimeWindow

REVENUE_REQUIRED_COLS = {
    "event_ts_utc",
    "asset_id",
    "product",
    "period_start_utc",
    "period_end_utc",
    "cleared_mw",
    "clearing_price",
    "revenue",
    "formula_version",
}
SOC_REQUIRED_COLS = {"ts_utc", "soc_mwh"}
SOC_OPTIONAL_COLS = ("charge_mw", "discharge_mw", "reg_deployment_mw")

PF_STRATEGY_NAME = "perfect_foresight"


# ─── Named exceptions (caught by viz_server, surfaced as error envelope) ──


class RunNotFound(LookupError):
    """Run directory does not exist under the configured strategy eval runs root."""


class StrategyNotFound(LookupError):
    """Strategy parquet for (run, strategy, asset) does not exist."""


class SchemaMismatchError(ValueError):
    """A parquet file's columns drifted from REVENUE_REQUIRED_COLS / SOC_REQUIRED_COLS.

    Raised at server boot by schema_check_at_boot() to fail fast before any
    request can hit the bad parquet.
    """


@dataclass(frozen=True)
class AssetConfig:
    """Eval-local asset config. Mirrors the fields in example-assets.md."""

    asset_id: str
    zone: str
    power_mw: float
    energy_mwh: float
    soc_min_pct: float = 0.10
    soc_max_pct: float = 0.90
    rte: float = 0.85
    cycle_cost_per_mwh: float = 5.0


@dataclass(frozen=True)
class Run:
    """One backtest output. Roles (baseline, ceiling) are NOT stored here —
    they're assigned at compare-time."""

    label: str
    asset_id: str
    window: TimeWindow
    revenue_path: Path
    soc_path: Path
    strategy_version: str = "unknown"

    def data_hash(self) -> str:
        h = hashlib.sha256()
        h.update(Path(self.revenue_path).read_bytes())
        return h.hexdigest()[:16]


# ─── Asset config from docs/example-assets.md ─────────────────────────────────

_DEFAULT_ASSETS_MD = Path(__file__).resolve().parents[2] / "docs" / "example-assets.md"


def parse_pjm_assets_md(path: Path | None = None) -> dict[str, AssetConfig]:
    """Parse the ## Sizing table from example-assets.md. Returns {asset_id: AssetConfig}.

    The constraints (SoC band, RTE, cycle cost) are uniform across all assets
    per the markdown's ## Constraints section, so we use the dataclass defaults.
    """
    p = Path(path) if path else _DEFAULT_ASSETS_MD
    text = p.read_text()

    out: dict[str, AssetConfig] = {}
    in_sizing = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            in_sizing = stripped.startswith("## Sizing")
            continue
        if not in_sizing or not stripped.startswith("|"):
            continue
        # Skip header and separator rows.
        if "Asset" in stripped and "MW" in stripped:
            continue
        # Separator row: cells are made of dashes, optional colons (alignment),
        # and spaces. Allow ":" so right-aligned columns ("---:") are skipped.
        if set(stripped.replace("|", "").strip()) <= {"-", ":", " "}:
            continue

        parts = [p.strip() for p in stripped.strip("|").split("|")]
        if len(parts) < 3:
            continue
        name, zone, mw_mwh = parts[0], parts[1], parts[2]
        # "250 / 1,000" → ("250", "1000")
        mw_str, mwh_str = (s.strip().replace(",", "") for s in mw_mwh.split("/"))
        out[name] = AssetConfig(
            asset_id=name,
            zone=zone,
            power_mw=float(mw_str),
            energy_mwh=float(mwh_str),
        )
    if not out:
        raise ValueError(f"no asset rows parsed from {p}")
    return out


# ─── Revenue parquet ──────────────────────────────────────────────────────


def load_revenue(run: Run) -> pd.DataFrame:
    """Read revenue parquet, validate schema, filter to run's asset and window.

    Hard-fails on missing required columns or NaN in revenue (engine bug
    sentinel)."""
    df = pd.read_parquet(run.revenue_path)
    missing = REVENUE_REQUIRED_COLS - set(df.columns)
    if missing:
        raise ValueError(f"revenue parquet {run.revenue_path} missing cols: {sorted(missing)}")
    if df["revenue"].isna().any():
        n = int(df["revenue"].isna().sum())
        raise ValueError(f"revenue parquet {run.revenue_path} has {n} NaN revenue rows")

    # Ensure timestamps are tz-aware UTC.
    for col in ("event_ts_utc", "period_start_utc", "period_end_utc"):
        df[col] = pd.to_datetime(df[col], utc=True)

    df = df[df["asset_id"] == run.asset_id]
    df = df[
        (df["period_start_utc"] >= run.window.start) & (df["period_start_utc"] < run.window.end)
    ]
    return df.reset_index(drop=True)


# ─── SoC parquet ──────────────────────────────────────────────────────────


def load_soc(run: Run, asset: AssetConfig) -> pd.DataFrame:
    """Read SoC parquet, derive missing columns, filter to window.

    Engine emits (ts_utc, soc_mwh) today. We derive soc_pct and cum_cycles;
    optional dispatch columns become NaN if absent.
    """
    df = pd.read_parquet(run.soc_path)
    missing = SOC_REQUIRED_COLS - set(df.columns)
    if missing:
        raise ValueError(f"soc parquet {run.soc_path} missing required cols: {sorted(missing)}")
    df = df.copy()
    df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True)
    df = df[(df["ts_utc"] >= run.window.start) & (df["ts_utc"] < run.window.end)]
    df = df.sort_values("ts_utc").reset_index(drop=True)

    df["soc_pct"] = df["soc_mwh"] / asset.energy_mwh

    # Full-equivalent cycles: total |Δsoc| throughput / (2 × energy_mwh).
    if len(df) >= 2:
        delta = df["soc_mwh"].diff().abs().fillna(0.0)
        df["cum_cycles"] = delta.cumsum() / (2.0 * asset.energy_mwh)
    else:
        df["cum_cycles"] = 0.0

    for col in SOC_OPTIONAL_COLS:
        if col not in df.columns:
            df[col] = pd.NA
    return df


# ─── Strategy run discovery + loaders for the viz tool ────────────────────
#
# Layout under the runs root (default `evaluation/runs/`):
#   {run_name}/{strategy}/{revenue|soc}_{asset_id}.parquet
#
# StrategyRunInfo is a flat handle viz_server uses to ask for series. It
# differs from `Run` (above) in that the window comes from the parquet
# itself rather than being supplied by the caller — discovery walks the
# directory.


@dataclass(frozen=True)
class StrategyRunInfo:
    """One (run, strategy, asset) tuple available on disk."""

    run_name: str  # e.g. "demo"
    strategy: str  # e.g. "perfect_foresight"
    asset_id: str  # "example_bess"
    window: TimeWindow  # min(period_start_utc) .. max(period_end_utc)
    revenue_path: Path
    soc_path: Path

    def is_pf_ceiling(self) -> bool:
        return self.strategy == PF_STRATEGY_NAME


def _parse_asset_from_filename(stem: str, prefix: str) -> str | None:
    """`revenue_example_bess` → `example_bess`. None if prefix doesn't match."""
    if not stem.startswith(prefix + "_"):
        return None
    return stem[len(prefix) + 1 :]


def list_strategy_runs(runs_root: Path) -> list[StrategyRunInfo]:
    """Walk strategy eval runs, return a StrategyRunInfo per (run, strategy, asset).

    Skips non-directories and files that don't follow the revenue_*.parquet /
    soc_*.parquet naming convention. Reads parquet metadata (no row scan) to
    derive the window from period_start_utc min/max.
    """
    if not runs_root.exists() or not runs_root.is_dir():
        return []

    out: list[StrategyRunInfo] = []
    for run_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        run_name = run_dir.name
        for strat_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
            strategy = strat_dir.name
            # Asset id is encoded in the filename; revenue and soc must agree.
            for rev_path in sorted(strat_dir.glob("revenue_*.parquet")):
                asset_id = _parse_asset_from_filename(rev_path.stem, "revenue")
                if asset_id is None:
                    continue
                soc_path = strat_dir / f"soc_{asset_id}.parquet"
                if not soc_path.exists():
                    # No SoC for this asset; skip rather than crash
                    continue
                window = _read_revenue_window(rev_path)
                if window is None:
                    continue
                out.append(
                    StrategyRunInfo(
                        run_name=run_name,
                        strategy=strategy,
                        asset_id=asset_id,
                        window=window,
                        revenue_path=rev_path,
                        soc_path=soc_path,
                    )
                )
    return out


def _read_revenue_window(path: Path) -> TimeWindow | None:
    """Cheap window probe: read only period_start_utc + period_end_utc columns."""
    try:
        df = pd.read_parquet(path, columns=["period_start_utc", "period_end_utc"])
    except Exception:
        return None
    if df.empty:
        return None
    start = pd.to_datetime(df["period_start_utc"], utc=True).min()
    end = pd.to_datetime(df["period_end_utc"], utc=True).max()
    if pd.isna(start) or pd.isna(end) or start >= end:
        return None
    return TimeWindow(start=start.to_pydatetime(), end=end.to_pydatetime())


def find_strategy_run(
    runs_root: Path,
    run_name: str,
    strategy: str,
    asset_id: str,
) -> StrategyRunInfo:
    """Locate one StrategyRunInfo or raise the appropriate named exception."""
    run_dir = runs_root / run_name
    if not run_dir.exists() or not run_dir.is_dir():
        raise RunNotFound(f"run '{run_name}' not found under {runs_root}")
    strat_dir = run_dir / strategy
    if not strat_dir.exists() or not strat_dir.is_dir():
        raise StrategyNotFound(f"strategy '{strategy}' not found in run '{run_name}'")
    rev_path = strat_dir / f"revenue_{asset_id}.parquet"
    soc_path = strat_dir / f"soc_{asset_id}.parquet"
    if not rev_path.exists():
        raise StrategyNotFound(
            f"revenue parquet for asset '{asset_id}' not found in {run_name}/{strategy}"
        )
    window = _read_revenue_window(rev_path)
    if window is None:
        raise StrategyNotFound(f"revenue parquet {rev_path} is empty or unreadable")
    return StrategyRunInfo(
        run_name=run_name,
        strategy=strategy,
        asset_id=asset_id,
        window=window,
        revenue_path=rev_path,
        soc_path=soc_path,
    )


def _load_revenue_filtered(
    info: StrategyRunInfo,
    window: TimeWindow,
    decision_time: datetime | None,
) -> pd.DataFrame:
    """Internal: read parquet, validate schema, filter by window + asset +
    optional decision_time. Public callers go through load_strategy_revenue
    or load_pf_ceiling_revenue (named for intent)."""
    df = pd.read_parquet(info.revenue_path)
    missing = REVENUE_REQUIRED_COLS - set(df.columns)
    if missing:
        raise SchemaMismatchError(
            f"revenue parquet {info.revenue_path} missing cols: {sorted(missing)}"
        )

    for col in ("event_ts_utc", "period_start_utc", "period_end_utc"):
        df[col] = pd.to_datetime(df[col], utc=True)

    df = df[df["asset_id"] == info.asset_id]
    df = df[(df["period_start_utc"] >= window.start) & (df["period_start_utc"] < window.end)]

    if decision_time is not None:
        df = df[df["event_ts_utc"] <= pd.Timestamp(decision_time)]

    return df.sort_values("period_start_utc").reset_index(drop=True)


def load_strategy_revenue(
    info: StrategyRunInfo,
    window: TimeWindow,
    decision_time: datetime,
) -> pd.DataFrame:
    """Bitemporally honest. Returns rows where event_ts_utc <= decision_time."""
    return _load_revenue_filtered(info, window, decision_time=decision_time)


def load_pf_ceiling_revenue(
    info: StrategyRunInfo,
    window: TimeWindow,
) -> pd.DataFrame:
    """Oracle line: no decision_time clamp.

    PF is solved with full foresight, so bitemporal filtering does not apply.
    Caller must pass an info where info.is_pf_ceiling() is True; we enforce
    it to prevent accidentally rendering a real strategy as the ceiling.
    """
    if not info.is_pf_ceiling():
        raise ValueError(
            f"load_pf_ceiling_revenue called on non-PF strategy "
            f"'{info.strategy}'. Use load_strategy_revenue instead."
        )
    return _load_revenue_filtered(info, window, decision_time=None)


def schema_check_at_boot(runs_root: Path) -> None:
    """Walk all strategy runs and validate parquet schemas before serving requests.

    Cheap: opens each parquet, checks column names against the required sets,
    does NOT read full row contents. ~10ms per file.

    Raises SchemaMismatchError on the first violation with full diff. The
    caller (viz_server lifespan) lets this propagate — server refuses to start.
    """
    if not runs_root.exists():
        return  # No runs yet; nothing to check.

    for info in list_strategy_runs(runs_root):
        # Revenue
        rev_cols = set(pd.read_parquet(info.revenue_path).columns)
        missing_rev = REVENUE_REQUIRED_COLS - rev_cols
        if missing_rev:
            raise SchemaMismatchError(
                f"revenue parquet at {info.revenue_path} missing required "
                f"columns: {sorted(missing_rev)}. "
                f"Required: {sorted(REVENUE_REQUIRED_COLS)}. "
                f"Got: {sorted(rev_cols)}."
            )
        # SoC
        soc_cols = set(pd.read_parquet(info.soc_path).columns)
        missing_soc = SOC_REQUIRED_COLS - soc_cols
        if missing_soc:
            raise SchemaMismatchError(
                f"soc parquet at {info.soc_path} missing required "
                f"columns: {sorted(missing_soc)}. "
                f"Required: {sorted(SOC_REQUIRED_COLS)}. "
                f"Got: {sorted(soc_cols)}."
            )
