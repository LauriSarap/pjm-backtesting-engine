"""All plots in one module. Each function returns a matplotlib Figure.

Plots take pre-computed data (DataFrames or metric outputs) — they
never load parquet themselves. Caller closes/saves the figure.
"""

from __future__ import annotations

import base64
import io
from datetime import date as _date

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from . import metrics
from .time import EPT

_PALETTE = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf"]


def equity_curve(
    revs_by_label: dict[str, pd.DataFrame],
    baseline_label: str | None = None,
    ceiling_label: str | None = None,
) -> plt.Figure:
    """Cumulative daily P&L per strategy. Step-style + markers because revenue
    is sparse (one entry per operating day at most)."""
    fig, ax = plt.subplots(figsize=(10, 5))
    color_idx = 0
    for label, rev in revs_by_label.items():
        cum = metrics.daily_pnl(rev).cumsum()
        if cum.empty:
            continue
        if label == baseline_label:
            ax.step(
                cum.index,
                cum.values,
                where="post",
                label=f"{label} (baseline)",
                linestyle="--",
                color="gray",
                linewidth=1.6,
                alpha=0.85,
            )
            ax.scatter(cum.index, cum.values, color="gray", s=18, alpha=0.85)
        elif label == ceiling_label:
            ax.step(
                cum.index,
                cum.values,
                where="post",
                label=f"{label} (ceiling)",
                linestyle=":",
                color="black",
                linewidth=1.6,
                alpha=0.85,
            )
            ax.scatter(cum.index, cum.values, color="black", s=18, alpha=0.85)
        else:
            color = _PALETTE[color_idx % len(_PALETTE)]
            color_idx += 1
            ax.step(cum.index, cum.values, where="post", label=label, color=color, linewidth=2.0)
            ax.scatter(cum.index, cum.values, color=color, s=22)
    ax.axhline(0, color="black", alpha=0.2, linewidth=0.8)
    ax.set_xlabel("Operating date (EPT)")
    ax.set_ylabel("Cumulative revenue ($)")
    ax.set_title("Equity curve")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
    ax.yaxis.set_major_formatter(plt.matplotlib.ticker.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def daily_pnl_distribution(rev: pd.DataFrame, bins: int | None = None) -> plt.Figure:
    """Histogram of daily P&L. Bins auto-scale to sample size."""
    daily = metrics.daily_pnl(rev)
    if bins is None:
        # Sturges-ish, clamped to keep small samples readable.
        bins = max(5, min(40, len(daily) // 2 if len(daily) > 4 else 5))
    fig, ax = plt.subplots(figsize=(8, 5))
    if len(daily):
        ax.hist(daily.values, bins=bins, color="steelblue", edgecolor="white", alpha=0.85)
        ax.axvline(
            daily.mean(),
            color="red",
            linestyle="--",
            linewidth=1.5,
            label=f"mean ${daily.mean():,.0f}",
        )
        ax.axvline(0, color="black", alpha=0.3)
        ax.legend()
    ax.set_xlabel("Daily P&L ($)")
    ax.set_ylabel("Days")
    ax.set_title(f"Daily P&L distribution (n={len(daily)})")
    ax.xaxis.set_major_formatter(plt.matplotlib.ticker.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax.grid(alpha=0.3)
    fig.tight_layout()
    return fig


_PRODUCT_COLORS = {
    # Capacity is the always-on baseline — render first so it stacks at the
    # bottom of attribution_stack with energy/AS layered on top.
    "Capacity_RPM": "#bcbd22",
    "DA_Energy": "#1f77b4",
    "RT_Energy": "#ff7f0e",
    "Reg_v2": "#2ca02c",
    "SR_RTO": "#d62728",
    "Sec_RTO": "#8c564b",
}


def attribution_stack(rev: pd.DataFrame) -> plt.Figure:
    """Stacked bar of revenue per product. Bucket size adapts to window length:
    daily for ≤14 days, weekly for ≤60, monthly otherwise. With 1 monthly bar
    on a 14-day window the chart says nothing, so don't do that."""
    if len(rev) == 0:
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.set_title("Revenue by product (no data)")
        return fig

    ts = pd.to_datetime(rev["period_start_utc"], utc=True).dt.tz_convert(EPT)
    span_days = (ts.max() - ts.min()).total_seconds() / 86400.0
    # Note: pandas 2.2+ requires 'M' (not 'ME') for Period. 'ME' is the resample alias.
    if span_days <= 14:
        freq, label = "D", "Day"
    elif span_days <= 60:
        freq, label = "W", "Week"
    else:
        freq, label = "M", "Month"

    df = rev.copy()
    df["_bucket"] = ts.dt.tz_localize(None).dt.to_period(freq)
    pivot = df.pivot_table(
        index="_bucket",
        columns="product",
        values="revenue",
        aggfunc="sum",
        fill_value=0.0,
    )

    colors = [_PRODUCT_COLORS.get(p, None) for p in pivot.columns]
    fig, ax = plt.subplots(figsize=(10, 5))
    pivot.plot(
        kind="bar",
        stacked=True,
        ax=ax,
        color=[c for c in colors if c] or None,
        edgecolor="white",
        linewidth=0.4,
    )
    ax.axhline(0, color="black", alpha=0.3, linewidth=0.8)
    ax.set_xlabel(f"{label} (EPT)")
    ax.set_ylabel("Revenue ($)")
    ax.set_title(f"Revenue by product, per {label.lower()}")
    ax.yaxis.set_major_formatter(plt.matplotlib.ticker.FuncFormatter(lambda x, _: f"${x:,.0f}"))
    ax.grid(axis="y", alpha=0.3)
    ax.legend(title="product", loc="best")
    fig.tight_layout()
    return fig


def day_trace(rev: pd.DataFrame, soc: pd.DataFrame, day: _date) -> plt.Figure:
    """SoC + dispatch + price overlay for one EPT operating day.

    Dispatch and price are drawn as bars spanning each award's [period_start,
    period_end), so a 1-hour cleared award shows as a 1-hour-wide block
    rather than being extrapolated across the full day by step(where="post").
    """
    day_start = pd.Timestamp(day, tz=EPT).tz_convert("UTC")
    day_end = day_start + pd.Timedelta(days=1)

    rev_day = rev[
        (rev["period_start_utc"] >= day_start) & (rev["period_start_utc"] < day_end)
    ].copy()
    soc_day = soc[(soc["ts_utc"] >= day_start) & (soc["ts_utc"] < day_end)].copy()

    fig, axes = plt.subplots(
        3, 1, figsize=(11, 8), sharex=True, gridspec_kw={"height_ratios": [1, 1, 1]}
    )

    # SoC — continuous 5-min line.
    if not soc_day.empty:
        soc_t = pd.to_datetime(soc_day["ts_utc"], utc=True).dt.tz_convert(EPT)
        axes[0].plot(soc_t, soc_day["soc_pct"] * 100, color="green", linewidth=1.5)
    axes[0].set_ylabel("SoC %")
    axes[0].set_ylim(0, 100)
    axes[0].axhline(10, color="red", linestyle=":", alpha=0.5, label="bounds")
    axes[0].axhline(90, color="red", linestyle=":", alpha=0.5)
    axes[0].grid(alpha=0.3)
    axes[0].legend(loc="upper right", fontsize=8)

    # Dispatch + price — bars of explicit award width.
    if not rev_day.empty:
        rev_day["_t_start"] = pd.to_datetime(rev_day["period_start_utc"], utc=True).dt.tz_convert(
            EPT
        )
        rev_day["_t_end"] = pd.to_datetime(rev_day["period_end_utc"], utc=True).dt.tz_convert(EPT)
        rev_day["_width_days"] = (
            rev_day["_t_end"] - rev_day["_t_start"]
        ).dt.total_seconds() / 86400.0

        for prod, color in [("DA_Energy", "tab:blue"), ("RT_Energy", "tab:orange")]:
            sub = rev_day[rev_day["product"] == prod]
            if sub.empty:
                continue
            axes[1].bar(
                sub["_t_start"],
                sub["cleared_mw"],
                width=sub["_width_days"],
                align="edge",
                color=color,
                edgecolor="white",
                linewidth=0.5,
                label=prod,
                alpha=0.85,
            )
            axes[2].bar(
                sub["_t_start"],
                sub["clearing_price"],
                width=sub["_width_days"],
                align="edge",
                color=color,
                edgecolor="white",
                linewidth=0.5,
                label=prod,
                alpha=0.85,
            )

    axes[1].set_ylabel("Cleared MW")
    axes[1].axhline(0, color="black", alpha=0.4, linewidth=0.8)
    axes[1].grid(alpha=0.3)
    axes[1].legend(loc="best", fontsize=8)

    axes[2].set_ylabel("Cleared $/MWh")
    axes[2].axhline(0, color="black", alpha=0.4, linewidth=0.8)
    axes[2].set_xlabel("Time (EPT)")
    axes[2].grid(alpha=0.3)
    axes[2].legend(loc="best", fontsize=8)

    # Force x-axis to span the full day so empty hours are visible.
    axes[0].set_xlim(
        pd.Timestamp(day, tz=EPT),
        pd.Timestamp(day, tz=EPT) + pd.Timedelta(days=1),
    )

    fig.suptitle(f"{day} (EPT)")
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def calendar_heatmap(rev: pd.DataFrame, title: str | None = None) -> plt.Figure:
    """GitHub-style calendar of daily P&L over the full trading window.

    Rows = day-of-week (Mon at top). Columns = weeks. Cells colored by P&L
    (green positive, red negative, gray missing). Spans first → last day with
    revenue data; days the strategy didn't trade show as gray gaps.
    """
    daily = metrics.daily_pnl(rev)
    if daily.empty:
        fig, ax = plt.subplots(figsize=(8, 2.5))
        ax.set_title(title or "Daily P&L heatmap (no data)")
        ax.axis("off")
        return fig

    first_date = daily.index.min()
    last_date = daily.index.max()
    # Pad to whole weeks (Mon..Sun) so the grid is rectangular.
    first_monday = first_date - pd.Timedelta(days=int(first_date.weekday()))
    last_sunday = last_date + pd.Timedelta(days=6 - int(last_date.weekday()))
    full_range = pd.date_range(first_monday, last_sunday, freq="D")
    daily = daily.reindex(full_range)

    week_idx = ((daily.index - first_monday).days // 7).astype(int)
    dow = pd.Index(daily.index).dayofweek
    n_weeks = int(week_idx.max()) + 1

    grid = np.full((7, n_weeks), np.nan)
    grid[dow.values, week_idx.values] = daily.values

    finite = daily.dropna()
    if finite.empty:
        vmax = 1.0
    else:
        vmax = max(abs(float(finite.min())), abs(float(finite.max())), 1.0)

    cell_inch = 0.22
    fig_width = max(7.0, n_weeks * cell_inch + 2.0)
    fig, ax = plt.subplots(figsize=(fig_width, 3.0))
    ax.set_facecolor("#eaeaea")

    im = ax.pcolormesh(
        np.arange(n_weeks + 1),
        np.arange(8),
        np.ma.masked_invalid(grid),
        cmap="RdYlGn",
        vmin=-vmax,
        vmax=vmax,
        edgecolors="white",
        linewidth=0.5,
    )

    ax.set_yticks(np.arange(7) + 0.5)
    ax.set_yticklabels(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], fontsize=8)
    ax.invert_yaxis()

    # Month-start labels along the bottom.
    month_starts = pd.date_range(first_date, last_date, freq="MS")
    if len(month_starts):
        month_weeks = [(d - first_monday).days // 7 for d in month_starts]
        ax.set_xticks([w + 0.5 for w in month_weeks])
        ax.set_xticklabels(
            [d.strftime("%b %Y") for d in month_starts], rotation=0, ha="left", fontsize=8
        )
    else:
        ax.set_xticks([])
    ax.tick_params(axis="x", which="major", length=0)
    ax.set_xlim(0, n_weeks)

    cbar = fig.colorbar(im, ax=ax, fraction=0.015, pad=0.01, label="Daily P&L ($)")
    cbar.ax.yaxis.set_major_formatter(
        plt.matplotlib.ticker.FuncFormatter(lambda x, _: f"${x:,.0f}")
    )
    cbar.ax.tick_params(labelsize=8)

    ax.set_title(title or "Daily P&L heatmap")
    ax.set_aspect("equal")
    fig.tight_layout()
    return fig


def fig_to_b64(fig: plt.Figure) -> str:
    """Encode figure as base64 PNG and close it."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=100, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")
