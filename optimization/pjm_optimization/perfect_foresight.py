"""Perfect-foresight MILP: the revenue ceiling for energy-only BESS arb.

Joint DA + RT optimization given realized LMPs over an entire window.
Captures DA/RT dual settlement: DA is a financial commitment, physical
dispatch can deviate, deviation settles at RT_LMP. The MILP discovers
the classic DA/RT deviation plays when they are optimal.

What's modelled
---------------
- DA energy (financial, hourly): da_net[h] ∈ [-P, +P], earns DA_LMP[h] × da_net[h].
- RT physical dispatch (5-min MTU): phys_charge[t], phys_discharge[t] ∈ [0, P].
- Charge/discharge mutual-exclusion via binary indicator per MTU.
- SoC dynamics: SoC[t+1] = SoC[t] + charge × η_in × dt − discharge / η_out × dt.
- Cycle-cost objective penalty: $cycle_cost × (charge + discharge) × dt.
- Optional final-SoC = initial-SoC for fair comparison across windows.

What's NOT modelled (v0)
------------------------
- Ancillary services (Reg, SR, Supp, Sec). The ceiling is energy-only.
- Bid-increment discretisation (continuous MW). Adds <0.1% to revenue.
- Conduct-and-economic-impact thresholds on extreme self-schedules.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from ortools.linear_solver import pywraplp

from pjm_engine.battery import AssetConfig

MTU_DT = 1.0 / 12.0  # hours (5 min)
MTU_5MIN = timedelta(minutes=5)
ONE_HOUR = timedelta(hours=1)


@dataclass
class PerfectForesightResult:
    asset: AssetConfig
    hour_starts: list[datetime]
    mtu_starts: list[datetime]
    da_net_mw: np.ndarray  # one per hour, signed (+ discharge, − charge)
    phys_charge_mw: np.ndarray  # one per MTU, ≥ 0
    phys_discharge_mw: np.ndarray  # one per MTU, ≥ 0
    soc_mwh: np.ndarray  # one per MTU + 1 (start of run included)
    da_lmps: np.ndarray
    rt_lmps: np.ndarray
    da_revenue: float
    rt_revenue: float
    cycle_cost: float
    solve_seconds: float
    solver_status: str
    initial_soc_mwh: float
    cycle_cost_usd_mwh: float

    @property
    def total_revenue(self) -> float:
        """Gross revenue, ignoring cycle cost (matches engine's revenue rows)."""
        return self.da_revenue + self.rt_revenue

    @property
    def net_objective(self) -> float:
        """Revenue minus cycle cost — the value the MILP actually maximizes."""
        return self.da_revenue + self.rt_revenue - self.cycle_cost

    @property
    def phys_net_mw(self) -> np.ndarray:
        return self.phys_discharge_mw - self.phys_charge_mw


def solve_perfect_foresight(
    asset: AssetConfig,
    da_lmps: pd.Series,
    rt_lmps: pd.Series,
    initial_soc_mwh: float,
    final_soc_constraint: bool = True,
    cycle_cost_usd_mwh: float | None = None,
    link_da_to_physical: bool = True,
    time_limit_sec: float | None = None,
    verbose: bool = False,
) -> PerfectForesightResult:
    """Solve the joint DA+RT energy MILP for one asset over one window.

    `da_lmps`: tz-aware index at hour starts, values in $/MWh.
    `rt_lmps`: tz-aware index at 5-min MTU starts, values in $/MWh. Must contain
               exactly 12 MTUs per DA hour, contiguous.
    `link_da_to_physical`: if True (default), constrains `da_net[h]` = average
        physical dispatch over hour h. This produces the **physical-realistic
        ceiling**: the operator can only DA-clear what it actually delivers
        on average, capturing DA/RT spread only via within-hour MTU
        optimisation. If False, DA and physical are decoupled — the MILP
        will virtual-bid at nameplate every hour to harvest DA-RT spread,
        which is mathematically allowed (energy has no deviation penalty
        per M28 §3.8) but unrealistic for a battery operator under PJM's
        market-conduct screens.
    """
    if cycle_cost_usd_mwh is None:
        cycle_cost_usd_mwh = asset.cycle_cost

    hour_starts = sorted(da_lmps.index.to_pydatetime().tolist())
    mtu_starts = sorted(rt_lmps.index.to_pydatetime().tolist())
    H = len(hour_starts)
    T = len(mtu_starts)

    if T != 12 * H:
        raise ValueError(f"perfect-foresight expects 12 RT MTUs per DA hour; got T={T}, H={H}.")
    # Sanity: first MTU aligns with first hour.
    if mtu_starts[0] != hour_starts[0]:
        raise ValueError(f"first MTU {mtu_starts[0]} ≠ first DA hour {hour_starts[0]}")

    # Each MTU's parent hour index in [0, H).
    h_of_t: list[int] = []
    h_idx = 0
    for t_idx, t in enumerate(mtu_starts):
        while h_idx + 1 < H and t >= hour_starts[h_idx + 1]:
            h_idx += 1
        if not (hour_starts[h_idx] <= t < hour_starts[h_idx] + ONE_HOUR):
            raise ValueError(f"MTU {t} not contained in any hour")
        h_of_t.append(h_idx)

    da_lmp_arr = np.array([float(da_lmps.loc[h]) for h in hour_starts])
    rt_lmp_arr = np.array([float(rt_lmps.loc[t]) for t in mtu_starts])

    # ─── Build the solver ──────────────────────────────────────────────
    solver = pywraplp.Solver.CreateSolver("CBC_MIXED_INTEGER_PROGRAMMING")
    if solver is None:
        raise RuntimeError("CBC solver not available; install `ortools`.")

    if time_limit_sec is not None:
        solver.SetTimeLimit(int(time_limit_sec * 1000))

    P = asset.power_mw
    eta_in = asset.eta_in
    eta_out = asset.eta_out
    soc_min = asset.soc_min_mwh
    soc_max = asset.soc_max_mwh

    # Variables
    da_net = [solver.NumVar(-P, P, f"da_net[{h}]") for h in range(H)]
    phys_charge = [solver.NumVar(0, P, f"ch[{t}]") for t in range(T)]
    phys_discharge = [solver.NumVar(0, P, f"dis[{t}]") for t in range(T)]
    is_dis = [solver.IntVar(0, 1, f"is_dis[{t}]") for t in range(T)]
    soc = [solver.NumVar(soc_min, soc_max, f"soc[{i}]") for i in range(T + 1)]

    # Charge XOR discharge per MTU
    for t in range(T):
        solver.Add(phys_charge[t] <= P * (1 - is_dis[t]))
        solver.Add(phys_discharge[t] <= P * is_dis[t])

    # SoC dynamics
    solver.Add(soc[0] == initial_soc_mwh)
    for t in range(T):
        solver.Add(
            soc[t + 1]
            == soc[t]
            + phys_charge[t] * eta_in * MTU_DT
            - phys_discharge[t] * (1.0 / eta_out) * MTU_DT
        )

    if final_soc_constraint:
        solver.Add(soc[T] == initial_soc_mwh)

    if link_da_to_physical:
        # da_net[h] must equal the hour-average of phys_net[t] for the 12 MTUs
        # belonging to that hour. Linear: 12 × da_net[h] = Σ_t (phys_dis[t] − phys_ch[t]).
        for h in range(H):
            mtu_indices = [t for t in range(T) if h_of_t[t] == h]
            solver.Add(
                12 * da_net[h]
                == solver.Sum(phys_discharge[t] - phys_charge[t] for t in mtu_indices)
            )

    # Objective: DA + RT − cycle cost
    # DA revenue per hour = da_net[h] × DA_LMP[h] × 1h
    da_rev = solver.Sum(da_net[h] * da_lmp_arr[h] for h in range(H))

    # RT revenue per MTU = (phys_net[t] − da_net[h(t)]) × RT_LMP[t] × dt
    rt_rev = solver.Sum(
        (phys_discharge[t] - phys_charge[t] - da_net[h_of_t[t]]) * rt_lmp_arr[t] * MTU_DT
        for t in range(T)
    )

    # Cycle cost = $/MWh × throughput × dt
    cyc_cost = solver.Sum(
        cycle_cost_usd_mwh * (phys_charge[t] + phys_discharge[t]) * MTU_DT for t in range(T)
    )

    solver.Maximize(da_rev + rt_rev - cyc_cost)

    if verbose:
        solver.EnableOutput()

    t0 = time.time()
    status = solver.Solve()
    solve_seconds = time.time() - t0

    status_map = {
        pywraplp.Solver.OPTIMAL: "OPTIMAL",
        pywraplp.Solver.FEASIBLE: "FEASIBLE",
        pywraplp.Solver.INFEASIBLE: "INFEASIBLE",
        pywraplp.Solver.UNBOUNDED: "UNBOUNDED",
        pywraplp.Solver.ABNORMAL: "ABNORMAL",
        pywraplp.Solver.NOT_SOLVED: "NOT_SOLVED",
    }
    status_str = status_map.get(status, f"UNKNOWN({status})")
    if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        raise RuntimeError(f"solver did not find a solution: status={status_str}")

    # ─── Extract solution ──────────────────────────────────────────────
    da_net_arr = np.array([da_net[h].solution_value() for h in range(H)])
    pc_arr = np.array([phys_charge[t].solution_value() for t in range(T)])
    pd_arr = np.array([phys_discharge[t].solution_value() for t in range(T)])
    soc_arr = np.array([soc[i].solution_value() for i in range(T + 1)])

    da_revenue = float(np.sum(da_net_arr * da_lmp_arr))
    deviations = pd_arr - pc_arr - da_net_arr[h_of_t]
    rt_revenue = float(np.sum(deviations * rt_lmp_arr * MTU_DT))
    cycle_cost = float(np.sum(cycle_cost_usd_mwh * (pc_arr + pd_arr) * MTU_DT))

    return PerfectForesightResult(
        asset=asset,
        hour_starts=hour_starts,
        mtu_starts=mtu_starts,
        da_net_mw=da_net_arr,
        phys_charge_mw=pc_arr,
        phys_discharge_mw=pd_arr,
        soc_mwh=soc_arr,
        da_lmps=da_lmp_arr,
        rt_lmps=rt_lmp_arr,
        da_revenue=da_revenue,
        rt_revenue=rt_revenue,
        cycle_cost=cycle_cost,
        solve_seconds=solve_seconds,
        solver_status=status_str,
        initial_soc_mwh=initial_soc_mwh,
        cycle_cost_usd_mwh=cycle_cost_usd_mwh,
    )


# ─── Parquet output (matches engine schema) ────────────────────────────────

REVENUE_COLS = [
    "event_ts_utc",
    "asset_id",
    "product",
    "period_start_utc",
    "period_end_utc",
    "cleared_mw",
    "clearing_price",
    "revenue",
    "formula_version",
]


def write_parquet(
    result: PerfectForesightResult,
    out_dir: Path,
    formula_version: str = "pf_milp_v1",
    bid_increment_clip: float | None = None,
) -> tuple[Path, Path]:
    """Write revenue + soc parquet matching engine schema. Returns (rev, soc) paths.

    `bid_increment_clip`: if set, drops rows whose |MW| is below this threshold
    (treats them as effectively zero). Default = asset.bid_increment / 2.
    """
    if bid_increment_clip is None:
        bid_increment_clip = result.asset.bid_increment / 2.0

    asset_id = result.asset.asset_id
    H = len(result.hour_starts)

    # Hour index for each MTU
    h_of_t: list[int] = []
    h_idx = 0
    for t in result.mtu_starts:
        while h_idx + 1 < H and t >= result.hour_starts[h_idx + 1]:
            h_idx += 1
        h_of_t.append(h_idx)

    rows: list[dict] = []

    # DA awards (one row per hour with a non-trivial net DA position)
    for h, h_start in enumerate(result.hour_starts):
        net_mw = float(result.da_net_mw[h])
        if abs(net_mw) < bid_increment_clip:
            continue
        rows.append(
            {
                "event_ts_utc": h_start,
                "asset_id": asset_id,
                "product": "DA_Energy",
                "period_start_utc": h_start,
                "period_end_utc": h_start + ONE_HOUR,
                "cleared_mw": net_mw,
                "clearing_price": float(result.da_lmps[h]),
                "revenue": net_mw * float(result.da_lmps[h]),
                "formula_version": formula_version,
            }
        )

    # RT awards (one row per MTU with a non-trivial deviation)
    for t, mtu_start in enumerate(result.mtu_starts):
        h = h_of_t[t]
        phys_net = float(result.phys_discharge_mw[t] - result.phys_charge_mw[t])
        deviation = phys_net - float(result.da_net_mw[h])
        if abs(deviation) < bid_increment_clip:
            continue
        rt_lmp = float(result.rt_lmps[t])
        rows.append(
            {
                "event_ts_utc": mtu_start,
                "asset_id": asset_id,
                "product": "RT_Energy",
                "period_start_utc": mtu_start,
                "period_end_utc": mtu_start + MTU_5MIN,
                "cleared_mw": phys_net,
                "clearing_price": rt_lmp,
                "revenue": deviation * rt_lmp * MTU_DT,
                "formula_version": formula_version,
            }
        )

    rev_df = pd.DataFrame(rows, columns=REVENUE_COLS)
    for col in ("event_ts_utc", "period_start_utc", "period_end_utc"):
        rev_df[col] = pd.to_datetime(rev_df[col], utc=True)

    # SoC trajectory: every 5-min step.
    soc_rows = []
    for i, mtu_start in enumerate(result.mtu_starts):
        soc_rows.append({"ts_utc": mtu_start, "soc_mwh": float(result.soc_mwh[i])})
    if result.mtu_starts:
        soc_rows.append(
            {
                "ts_utc": result.mtu_starts[-1] + MTU_5MIN,
                "soc_mwh": float(result.soc_mwh[-1]),
            }
        )
    soc_df = pd.DataFrame(soc_rows)
    soc_df["ts_utc"] = pd.to_datetime(soc_df["ts_utc"], utc=True)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rev_path = out_dir / f"revenue_{asset_id}.parquet"
    soc_path = out_dir / f"soc_{asset_id}.parquet"
    rev_df.to_parquet(rev_path, index=False)
    soc_df.to_parquet(soc_path, index=False)
    return rev_path, soc_path
