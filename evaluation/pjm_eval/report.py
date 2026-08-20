"""compare() runs and HTML stitching.

compare() is the user-facing entrypoint: takes Run handles, returns a
ReportData with all metrics. write_html() turns ReportData into a
self-contained HTML page with embedded plots.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import jinja2
import pandas as pd

from . import metrics, plots
from .io import AssetConfig, Run, load_revenue, load_soc
from .time import TimeWindow


@dataclass
class StrategyView:
    run: Run
    rev: pd.DataFrame
    soc: pd.DataFrame
    total: float
    kw_yr: float
    sharpe: float
    max_dd: float
    cycles: float


@dataclass
class ReportData:
    asset: AssetConfig
    window: TimeWindow
    strategies: dict[str, StrategyView]  # label → view
    captures: dict[str, float]  # label → capture rate (NaN if no ceiling)
    baseline_label: str | None
    ceiling_label: str | None


def compare(
    runs: list[Run], asset: AssetConfig, baseline: Run | None = None, ceiling: Run | None = None
) -> ReportData:
    """Load runs, compute metrics, return a ReportData. One asset, one window."""
    all_runs = list(runs)
    for extra in (baseline, ceiling):
        if extra is not None and extra not in all_runs:
            all_runs.append(extra)

    windows = {r.window for r in all_runs}
    if len(windows) > 1:
        raise ValueError(f"runs span different windows: {windows}")
    window = windows.pop()

    views: dict[str, StrategyView] = {}
    for r in all_runs:
        rev = load_revenue(r)
        soc = load_soc(r, asset)
        daily = metrics.daily_pnl(rev)
        views[r.label] = StrategyView(
            run=r,
            rev=rev,
            soc=soc,
            total=metrics.total(rev),
            kw_yr=metrics.per_kw_year(rev, asset, window),
            sharpe=metrics.sharpe(daily),
            max_dd=metrics.max_drawdown(daily.cumsum()),
            cycles=metrics.cycles(soc),
        )

    base_total = views[baseline.label].total if baseline else None
    ceil_total = views[ceiling.label].total if ceiling else None
    captures = {
        r.label: (
            metrics.capture_rate(views[r.label].total, base_total, ceil_total)
            if base_total is not None
            else float("nan")
        )
        for r in runs
    }

    return ReportData(
        asset=asset,
        window=window,
        strategies=views,
        captures=captures,
        baseline_label=baseline.label if baseline else None,
        ceiling_label=ceiling.label if ceiling else None,
    )


# ─── HTML rendering ──────────────────────────────────────────────────────

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>PJM eval — {{ asset.asset_id }}</title>
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif;
         max-width: 1100px; margin: 2em auto; padding: 0 1em; color: #222; }
  h1, h2, h3 { color: #111; }
  table { border-collapse: collapse; margin: 1em 0; }
  th, td { padding: 6px 12px; border: 1px solid #ddd; text-align: right; }
  th { background: #f5f5f5; }
  td.label { text-align: left; }
  img { max-width: 100%; height: auto; margin: 1em 0; border: 1px solid #eee; }
  .meta { color: #666; font-size: 0.9em; }
  details.glossary { background: #fafafa; border: 1px solid #e0e0e0;
                      padding: 0.6em 1em; border-radius: 4px; margin: 1em 0; }
  details.glossary summary { font-weight: 600; cursor: pointer; }
  dl.glossary { margin: 0.8em 0 0; }
  dl.glossary dt { font-weight: 600; margin-top: 0.6em; }
  dl.glossary dd { margin: 0.1em 0 0 1.2em; color: #444; }
</style>
</head><body>
<h1>PJM eval — {{ asset.asset_id }}</h1>
<p class="meta">
  Window: {{ window_str }} ·
  Asset: {{ asset.zone }}, {{ "%.0f"|format(asset.power_mw) }} MW /
  {{ "%.0f"|format(asset.energy_mwh) }} MWh ·
  Generated {{ generated_at }}
</p>

<details class="glossary" open>
<summary>About this report &mdash; what each metric means</summary>
<p>This report compares trading strategies for one PJM battery over one window,
scored against historical market data. Each strategy ran in the engine, which
publishes only what would have been knowable in real time (no future-peeking),
clears bids against historical prices, and writes per-event revenue + SoC to
parquet. This eval reads those files and computes the metrics below.</p>

<dl class="glossary">
  <dt>Total $</dt>
  <dd>Sum of every settled revenue line over the window. DA + RT energy and
      Reg_v2 capacity today; SR / Supp / Sec / LOC will land here when those
      markets do.</dd>

  <dt>$/kW-yr</dt>
  <dd>Total $ normalized to dollars per kW of nameplate, annualized. Industry
      standard. For comparison: real BESS energy-only revenue is roughly
      $50&ndash;150/kW-yr; AS-stacked is higher. Numbers below that suggest
      something upstream is wrong (e.g. days getting skipped on SoC violations).</dd>

  <dt>Capture</dt>
  <dd>(strategy &minus; baseline) / (ceiling &minus; baseline). Tells you where
      between the floor and the perfect-foresight ceiling each strategy landed.
      0% = matches baseline; 100% = matches perfect-foresight.
      Shows &ldquo;&mdash;&rdquo; when the report includes no
      perfect-foresight ceiling run.</dd>

  <dt>Sharpe</dt>
  <dd>Annualized risk-adjusted return on daily P&amp;L
      (mean &divide; stdev &times; &radic;365). Higher is better; &gt; 1 is good.
      Constant or near-constant daily P&amp;L gives Sharpe &asymp; 0 by construction.</dd>

  <dt>Max DD</dt>
  <dd>Largest peak-to-trough drop in cumulative revenue. Always &le; 0.
      &ldquo;$0&rdquo; means cumulative revenue rose monotonically over the
      window (no losing day big enough to dip below a prior peak).</dd>

  <dt>Cycles</dt>
  <dd>Full-equivalent battery cycles
      (total &Sigma;|&Delta;SoC| throughput &divide; 2&times;energy capacity).
      Each cycle is roughly $5/MWh &times; throughput in degradation cost.
      A strategy with similar revenue but fewer cycles is preferable.</dd>
</dl>

<p class="meta">
<strong>Roles:</strong> the <em>baseline</em> is the floor every strategy is
measured against (a naive policy you should beat). The <em>ceiling</em> is the
theoretical max under full price knowledge (perfect-foresight MILP). The other
strategies are what's being evaluated.
</p>

<p class="meta">
<strong>Scope notes:</strong> per-asset only (no portfolio aggregation).
Capture-rate vs perfect-foresight is energy-only — the PF MILP doesn't model
Reg or reserves, so an AS-stacking strategy's capture % is not meaningful.
</p>
</details>

<h2>Leaderboard</h2>
<table>
  <thead><tr>
    <th class="label">Strategy</th>
    <th>Total $</th><th>$/kW-yr</th>
    <th>Capture{% if ceiling_label is none %}<br><span class="meta">(no ceiling yet)</span>{% endif %}</th>
    <th>Sharpe</th><th>Max DD</th><th>Cycles</th>
  </tr></thead>
  <tbody>
  {% for label, v in strategies.items() %}
  <tr>
    <td class="label">
      {{ label }}{% if label == baseline_label %} <span class="meta">(baseline)</span>{% endif %}{% if label == ceiling_label %} <span class="meta">(ceiling)</span>{% endif %}
    </td>
    <td>${{ "{:,.0f}".format(v.total) }}</td>
    <td>${{ "{:,.0f}".format(v.kw_yr) }}</td>
    <td>{% if captures.get(label) is none or captures.get(label) != captures.get(label) %}—{% else %}{{ "{:.1%}".format(captures[label]) }}{% endif %}</td>
    <td>{{ "{:.2f}".format(v.sharpe) }}</td>
    <td>${{ "{:,.0f}".format(v.max_dd) }}</td>
    <td>{{ "{:.1f}".format(v.cycles) }}</td>
  </tr>
  {% endfor %}
  </tbody>
</table>

<h2>Equity curve</h2>
<img src="data:image/png;base64,{{ equity_b64 }}">

<h2>Per-product attribution</h2>
{% for label, b64 in attribution_b64s.items() %}
  <h3>{{ label }}</h3>
  <img src="data:image/png;base64,{{ b64 }}">
{% endfor %}

<h2>Daily P&amp;L distribution</h2>
{% for label, b64 in distribution_b64s.items() %}
  <h3>{{ label }}</h3>
  <img src="data:image/png;base64,{{ b64 }}">
{% endfor %}

<h2>Daily P&amp;L heatmap</h2>
<p class="meta">Each cell is one operating day. Green = profitable, red = losing,
gray = strategy didn't trade.</p>
{% for label, b64 in heatmap_b64s.items() %}
  <h3>{{ label }}</h3>
  <img src="data:image/png;base64,{{ b64 }}">
{% endfor %}

<h2>Day spot-checks (best / worst / median P&amp;L day per strategy)</h2>
{% for label, traces in trace_b64s.items() %}
  <h3>{{ label }}</h3>
  {% for kind, b64 in traces.items() %}
    <h4>{{ kind }}</h4>
    <img src="data:image/png;base64,{{ b64 }}">
  {% endfor %}
{% endfor %}
</body></html>
"""


def write_html(data: ReportData, out: Path) -> Path:
    """Render the report and write to disk. Returns the path written."""
    revs = {label: v.rev for label, v in data.strategies.items()}

    equity_b64 = plots.fig_to_b64(
        plots.equity_curve(
            revs,
            data.baseline_label,
            data.ceiling_label,
        )
    )
    attribution_b64s = {
        label: plots.fig_to_b64(plots.attribution_stack(v.rev))
        for label, v in data.strategies.items()
    }
    distribution_b64s = {
        label: plots.fig_to_b64(plots.daily_pnl_distribution(v.rev))
        for label, v in data.strategies.items()
    }
    heatmap_b64s = {
        label: plots.fig_to_b64(plots.calendar_heatmap(v.rev, title=f"{label} — daily P&L"))
        for label, v in data.strategies.items()
    }

    trace_b64s: dict[str, dict[str, str]] = {}
    for label, v in data.strategies.items():
        daily = metrics.daily_pnl(v.rev)
        if daily.empty:
            trace_b64s[label] = {}
            continue
        sorted_idx = daily.sort_values().index
        picks: dict[str, object] = {
            "worst": sorted_idx[0].date(),
            "median": sorted_idx[len(sorted_idx) // 2].date(),
            "best": sorted_idx[-1].date(),
        }
        # Dedupe: with very few days, best/worst/median can collide.
        seen: set = set()
        traces: dict[str, str] = {}
        for kind, day in picks.items():
            if day in seen:
                continue
            seen.add(day)
            pnl = daily[pd.Timestamp(day)]
            traces[f"{kind} day — {day} (${pnl:,.0f})"] = plots.fig_to_b64(
                plots.day_trace(v.rev, v.soc, day)
            )
        trace_b64s[label] = traces

    window_str = f"{data.window.start.isoformat()} → {data.window.end.isoformat()}"
    html = jinja2.Template(_HTML_TEMPLATE).render(
        asset=data.asset,
        window_str=window_str,
        strategies=data.strategies,
        captures=data.captures,
        baseline_label=data.baseline_label,
        ceiling_label=data.ceiling_label,
        equity_b64=equity_b64,
        attribution_b64s=attribution_b64s,
        distribution_b64s=distribution_b64s,
        heatmap_b64s=heatmap_b64s,
        trace_b64s=trace_b64s,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    return out
