"""Leaderboard helpers for parameter sweeps.

This is intentionally lighter than the full HTML report path: run strategies,
score them in memory, and write one CSV + one compact HTML file.
"""

from __future__ import annotations

import html
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from . import metrics
from .io import AssetConfig
from .time import TimeWindow


@dataclass(frozen=True)
class LeaderboardRow:
    asset_id: str
    zone: str
    strategy: str
    family: str
    params_json: str
    total_revenue: float
    da_revenue: float
    rt_revenue: float
    reg_revenue: float
    sr_revenue: float
    capacity_revenue: float
    cycles: float
    sharpe: float
    max_drawdown: float
    trades: int
    revenue_rows: int
    skipped: bool = False
    error: str = ""


def score_result(
    result,
    asset: AssetConfig,
    window: TimeWindow,
    strategy: str,
    family: str,
    params: dict,
) -> LeaderboardRow:
    """Score one backtest. `result` is the engine's BacktestResult, taken
    structurally (needs `.to_revenue_df()`, `.to_soc_df()`, `.asset`) so
    pjm_eval keeps its no-engine-imports rule."""
    rev = result.to_revenue_df()
    soc = result.to_soc_df()

    if len(rev) == 0:
        product_rev = {}
        daily = pd.Series(dtype=float, name="revenue")
    else:
        product_rev = rev.groupby("product")["revenue"].sum().to_dict()
        daily = metrics.daily_pnl(rev)

    if len(soc) >= 2:
        soc = soc.copy()
        soc["soc_pct"] = soc["soc_mwh"] / asset.energy_mwh
        soc["cum_cycles"] = soc["soc_mwh"].diff().abs().fillna(0.0).cumsum() / (
            2.0 * asset.energy_mwh
        )
        cycles = metrics.cycles(soc)
    else:
        cycles = 0.0

    return LeaderboardRow(
        asset_id=result.asset.asset_id,
        zone=result.asset.zone,
        strategy=strategy,
        family=family,
        params_json=json.dumps(params, sort_keys=True),
        total_revenue=metrics.total(rev) if len(rev) else 0.0,
        da_revenue=float(product_rev.get("DA_Energy", 0.0)),
        rt_revenue=float(product_rev.get("RT_Energy", 0.0)),
        reg_revenue=float(product_rev.get("Reg_v2", 0.0)),
        sr_revenue=float(product_rev.get("SR_RTO", 0.0)),
        capacity_revenue=float(product_rev.get("Capacity_RPM", 0.0)),
        cycles=cycles,
        sharpe=metrics.sharpe(daily),
        max_drawdown=metrics.max_drawdown(daily.cumsum()) if len(daily) else 0.0,
        trades=int((rev["cleared_mw"].abs() > 1e-9).sum()) if len(rev) else 0,
        revenue_rows=len(rev),
    )


def skipped_row(
    asset_id: str,
    zone: str,
    strategy: str,
    family: str,
    params: dict,
    error: Exception,
) -> LeaderboardRow:
    return LeaderboardRow(
        asset_id=asset_id,
        zone=zone,
        strategy=strategy,
        family=family,
        params_json=json.dumps(params, sort_keys=True),
        total_revenue=0.0,
        da_revenue=0.0,
        rt_revenue=0.0,
        reg_revenue=0.0,
        sr_revenue=0.0,
        capacity_revenue=0.0,
        cycles=0.0,
        sharpe=0.0,
        max_drawdown=0.0,
        trades=0,
        revenue_rows=0,
        skipped=True,
        error=str(error),
    )


def rows_to_df(rows: list[LeaderboardRow]) -> pd.DataFrame:
    return pd.DataFrame([asdict(r) for r in rows])


def write_leaderboard(df: pd.DataFrame, out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "leaderboard.csv"
    html_path = out_dir / "leaderboard.html"

    ranked = df.sort_values(
        ["asset_id", "total_revenue"],
        ascending=[True, False],
    ).reset_index(drop=True)
    ranked.to_csv(csv_path, index=False)
    html_path.write_text(render_html(ranked))
    return csv_path, html_path


def _money(v: float) -> str:
    return f"${v:,.0f}"


def _fmt(v: float) -> str:
    return f"{v:,.2f}"


def render_html(df: pd.DataFrame) -> str:
    ok = df[~df["skipped"]].copy()
    best_by_asset = (
        ok.sort_values(["asset_id", "total_revenue"], ascending=[True, False])
        .groupby("asset_id", as_index=False)
        .head(1)
    )
    best_by_family = (
        ok.sort_values(["asset_id", "family", "total_revenue"], ascending=[True, True, False])
        .groupby(["asset_id", "family"], as_index=False)
        .head(1)
    )

    def table(rows: pd.DataFrame, limit: int | None = None) -> str:
        if limit is not None:
            rows = rows.head(limit)
        body = []
        for _, r in rows.iterrows():
            params = html.escape(r["params_json"])
            body.append(
                "<tr>"
                f"<td>{html.escape(str(r['asset_id']))}</td>"
                f"<td>{html.escape(str(r['zone']))}</td>"
                f"<td>{html.escape(str(r['family']))}</td>"
                f"<td>{html.escape(str(r['strategy']))}</td>"
                f"<td class='num'>{_money(float(r['total_revenue']))}</td>"
                f"<td class='num'>{_money(float(r['capacity_revenue']))}</td>"
                f"<td class='num'>{_money(float(r['da_revenue']))}</td>"
                f"<td class='num'>{_money(float(r['rt_revenue']))}</td>"
                f"<td class='num'>{_money(float(r['reg_revenue']))}</td>"
                f"<td class='num'>{_money(float(r['sr_revenue']))}</td>"
                f"<td class='num'>{_fmt(float(r['cycles']))}</td>"
                f"<td class='num'>{_fmt(float(r['sharpe']))}</td>"
                f"<td class='num'>{_money(float(r['max_drawdown']))}</td>"
                f"<td><code>{params}</code></td>"
                "</tr>"
            )
        return "\n".join(body)

    skipped = df[df["skipped"]]
    skipped_note = ""
    if len(skipped):
        skipped_note = f"<p class='meta'>{len(skipped)} runs skipped. See CSV for errors.</p>"

    all_ranked = ok.sort_values("total_revenue", ascending=False)
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>PJM Strategy Leaderboard</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif;
           max-width: 1280px; margin: 28px auto; padding: 0 18px; color: #222; }}
    table {{ border-collapse: collapse; width: 100%; margin: 14px 0 28px; }}
    th, td {{ border: 1px solid #ddd; padding: 6px 8px; vertical-align: top; }}
    th {{ background: #f5f5f5; text-align: left; }}
    td.num {{ text-align: right; white-space: nowrap; }}
    code {{ font-size: 12px; white-space: pre-wrap; }}
    .meta {{ color: #666; }}
  </style>
</head>
<body>
  <h1>PJM Strategy Leaderboard</h1>
  <p class="meta">Sorted by realized backtest revenue. CSV contains all rows and exact params.</p>
  {skipped_note}

  <h2>Best Overall Per Asset</h2>
  <table>
    <thead><tr><th>Asset</th><th>Zone</th><th>Family</th><th>Strategy</th>
    <th>Total</th><th>Capacity</th><th>DA</th><th>RT</th><th>Reg</th><th>SR</th>
    <th>Cycles</th><th>Sharpe</th><th>Max DD</th><th>Params</th></tr></thead>
    <tbody>{table(best_by_asset)}</tbody>
  </table>

  <h2>Best Per Family</h2>
  <table>
    <thead><tr><th>Asset</th><th>Zone</th><th>Family</th><th>Strategy</th>
    <th>Total</th><th>Capacity</th><th>DA</th><th>RT</th><th>Reg</th><th>SR</th>
    <th>Cycles</th><th>Sharpe</th><th>Max DD</th><th>Params</th></tr></thead>
    <tbody>{table(best_by_family)}</tbody>
  </table>

  <h2>Top 50 Runs</h2>
  <table>
    <thead><tr><th>Asset</th><th>Zone</th><th>Family</th><th>Strategy</th>
    <th>Total</th><th>Capacity</th><th>DA</th><th>RT</th><th>Reg</th><th>SR</th>
    <th>Cycles</th><th>Sharpe</th><th>Max DD</th><th>Params</th></tr></thead>
    <tbody>{table(all_ranked, 50)}</tbody>
  </table>
</body>
</html>
"""
