# Design: PJM BESS market-mechanics backtesting engine

## What this is

A Python backtesting engine that simulates PJM market mechanics for a
battery (BESS) against historical data. An event-driven simulator walks the
market clock. At each event it shows the strategy only what had been
published by that moment, asks for bids, clears them against historical
clearing prices, and settles with the formula in force on that operating
date.

The engine contains no strategy. A strategy is a class implementing
`on_event(event, context)` in a separate package, and the engine never
imports strategy code. The repo ships exactly one reference algorithm: the
perfect-foresight MILP benchmark in `optimization/`. It is a ceiling to
measure against, not a tradable strategy.

Two hard requirements are enforced structurally rather than by convention.
First, no lookahead: a strategy cannot read data with `published_at > now`,
and `data.py:view_as_of` is the only read path. Second, no rule breaking:
every response passes market-rule and physical validators before anything
commits.

A vocabulary note for readers new to PJM. An LMP (locational marginal
price) is the $/MWh price of energy at a location. An MTU (market time
unit) is the settlement interval, 5 minutes in the real-time market. SoC is
the battery's state of charge. EPT is Eastern Prevailing Time, PJM's market
clock. Everything else is defined where it first appears, with the PJM
manual section it came from.

## Scope

- **Markets:** DA energy, RT energy (5-min), Regulation (RegA/RegD v1
  before Oct 2025, single bidirectional v2 after), Synchronized Reserve,
  Secondary (30-min) Reserve, monthly capacity (RPM) accrual, Lost
  Opportunity Cost. Non-Synchronized Reserve is excluded because storage
  resources may not offer it. Supplemental is not a separately-cleared
  product post-2022.
- **Window:** 2021-2026 data. The post-redesign window (Oct 2025 onward)
  has the most complete feed coverage and received the most testing.
- **Spatial:** zone-level LMPs only. Nodal pricing is out of scope.
- **Assets:** batteries modeled as perfect-tracking price-takers. They
  follow dispatch exactly, with no forced outages and no basis risk. These
  docs call this "the perfect-tracking assumption" throughout. Assets are
  independent, and portfolio P&L is the sum. Example configs live in
  `example-assets.md`.

Stack: Python 3.11+, pandas + pyarrow, OR-tools/CBC for the MILP benchmark,
pytest, stdlib `heapq` scheduler. No database. The parquet store fits in
RAM.

## Architecture (file-by-file)

All in `engine/pjm_engine/`:

- **`data.py`**: CSV ingest, parquet cache, `published_at` computed per
  `pjm-data.md` §5, `view_as_of(df, as_of)`.
- **`events.py`**: `Event` subtypes (`DAGateClosing`, `RTGateClosing`,
  `RegAssignmentBlock`, ...) and a `heapq` scheduler ordered on
  `(timestamp, priority, sequence)`. Info events fire after decision events.
- **`time_utils.py`**: DST-correct EPT operating-day grids walked in UTC:
  hourly and half-hourly interval starts for 23/24/25-hour days.
- **`battery.py`**: `AssetConfig` (power, energy, efficiencies, cycle cost,
  `ucap_mw`), SoC dynamics, cycle counting.
- **`markets/`**: one module per settlement family (`da_energy`,
  `rt_energy`, `reg`, `reserves`, `capacity`, `loc`, `sr_events`,
  `clearing`), each exposing per-family settle functions (for example
  `settle_reg_v1` vs `settle_reg_v2`). The product's regime carries the
  formula version, chosen by operating date and enforced by
  `ProductNotInRegimeError`.
- **`validation.py`**: bid, stack, and physical-state validators, including
  the reserve-aware SoC simulation.
- **`invariants.py`**: always-on floor invariants. These catch engine bugs,
  not strategy bugs.
- **`settle.py`**: `PriceTables` (O(1) price-lookup dicts built once per
  backtest), `Award`, `RevenueRow`, and `settle()`. Awards in, revenue rows
  out.
- **`strategy_base.py`**: `BaseStrategy` ABC, `Context`, `BidCurve`,
  `SelfSchedule`, `Acknowledgment`. The contract only.
- **`errors.py`**: exception classes (error table below).
- **`runner.py`**: the main loop. Walk events, call strategy, validate,
  clear, commit, settle, emit parquet.

## Gates and clearing

Strategies submit a `BidCurve` (monotonic price-quantity tiers) or a
`SelfSchedule` (a plain MW commitment) at gate events. A `BidCurve` clears
as `cleared_MW = max{q : bid_price(q) <= clearing_price}` for supply offers,
with the mirrored inequality for charging, floored to PJM's 0.1 MW
increment. A `SelfSchedule` clears in full at any price.

The DA gate (D-1 11:00 EPT) clears the financial DA energy position and
DA-cleared reserve commitments. The daily Regulation offer locks at D-1
14:15 EPT (`pjm-data.md` §2.1). Per-MTU RT gates fire at `MTU_start - 5
min`, roughly 288 per day. That cadence yields the correct RT-LMP blind
window per `pjm-data.md` §5.2. PJM's T-65 offer-revision lock freezes offer parameters
but does not commit physical dispatch, so the per-MTU gate model is
deliberately more permissive than a T-65 commit-and-freeze model.

Energy deviation settles automatically at RT LMP with no per-MWh penalty.
PJM does allocate balancing operating reserve costs to deviators, and that
is not modeled, a small understatement of deviation cost. Reserve and
regulation deviations do carry penalties: a Reg score below 0.25 forfeits
the interval, and an SR shortfall on a called event forfeits the credit
plus a retroactive clawback.

## Validation pipeline

Every response passes these checks, in order:

- **(a)** bid-level: power bounds, 0.1 MW increments, gate alignment,
  product-in-regime, storage eligibility, sub-zone match, curve
  monotonicity.
- **(b)** stack-level: total power cap across energy + reserves, charge
  XOR discharge per MTU, SR substitution toward Primary/30-min.
- **(c)** physical state: the engine simulates the SoC trajectory implied
  by cleared awards plus prior commitments and rejects infeasible batches.
  The simulation includes reserve-aware reservations: 1 MWh of Reg
  headroom per MW-h, and the SR sustain rule of `pjm-data.md` §2.2 (X MW
  of SR requires at least 0.5 * X MWh available).
- **(d)** settlement-formula rules inside `markets/`: score thresholds,
  clawbacks.
- **(e)** the bitemporal filter itself.
- **(f)** floor invariants after every event: revenue conservation,
  `cleared_MW <= bid_MW`, SoC in bounds, cycles monotonic.

Bid and stack violations and data leakage fail the run as strategy bugs.
The engine rejects SoC-infeasible batches and re-prompts the strategy.
Invariant violations are engine bugs and always fatal.

## Errors

| Error | Trigger | Default response |
|---|---|---|
| `DataNotAvailableError` | View query for `published_at > as_of` | Hard fail (strategy bug) |
| `BidValidationError` | Power, increment, gate, or eligibility violation | Hard fail (strategy bug) |
| `ProductNotInRegimeError` | Product bid outside its regime window | Hard fail |
| `SubZoneMismatchError` | SR bid into a sub-zone the asset isn't in | Hard fail |
| `RegimeBoundaryError` | Settlement date with no formula version | Hard fail |
| `DataGapError` | Required feed missing for the interval | Hard fail |
| `SoCInfeasibleError` | Cleared awards would breach SoC bounds | Reject batch, re-prompt strategy |
| `InvariantError` | Floor invariant violated | Hard fail (engine bug) |

## Settlement

`_h` means per hour and `_i` means per 5-min MTU (1/12 of an hour). Energy uses
PJM's two-settlement system: the DA position is financially binding, and
real time settles only the deviation from it. Reserve RT legs pay the
RT-minus-DA delta the same way (rows carry
`formula_version="rt_v2_two_settlement"`).

| Product | Revenue |
|---|---|
| DA energy | `cleared_MW * DA_LMP * hours` |
| RT energy (deviation) | `(actual_MW - DA_cleared_MW) * RT_LMP / 12` per MTU |
| Reg v2 (post-2025-10) | `MW * score * (RMCCP + mileage_ratio * RMPCP) / 12` per MTU |
| Reg v1 (RegA/RegD) | `MW * score * hourly_price * hours` from `reg_market_results` |
| Reg signal energy | `reg_deployment_MW * RT_LMP / 12` per MTU (settles as RT energy) |
| Synchronized Reserve | DA: `DA_MW * DA_SRMCP_h`; RT (M28 §6.2.2): `(RT_MW - DA_MW) * RT_SRMCP / 12` |
| Secondary Reserve | same two-settlement shape (M28 §19.2.2), shortfall MW held at 0 |
| Capacity (RPM) | `ucap_mw * BRA_$/MW-day * days_in_month`, one row per full month |
| Cycle cost | `-$5/MWh * \|dispatch_MW\| * hours` (objective-side penalty, not revenue) |

RMCCP and RMPCP are the two legs of the regulation clearing price
(capability and performance). SRMCP is the Synchronized Reserve clearing
price, and BRA is the capacity auction. `pjm-data.md` covers all of them.

SR events: the engine replays PJM's historical `sync_reserve_events` log. A
shortfall on a called event emits a forfeit row plus a retroactive clawback
of ~30 days of prior SR credits (`SR_CLAWBACK_DAYS`, `pjm-data.md` §7.2).

Validation fixtures are hand-computed operating days settled line-by-line
from the manuals, with tiered tolerances: hard lines ±0.5%,
mileage-dependent lines ±5%, day total ±1%. Non-leakage probes assert that
no served row ever has `published_at > as_of`.

## LOC

Lost Opportunity Cost compensates a battery whose reserve award keeps it
from running its profitable energy position (`markets/loc.py`):

```
LOC_i = max(0, DA_LMP_h - cycling_cost) * effective_MW / 12
effective_MW = min(reserve_MW, LOC_MAX_CYCLES_PER_DAY * energy_mwh / 24)
```

Two bounds keep LOC realistic. The cycle cap (`LOC_MAX_CYCLES_PER_DAY =
2.0`) limits per-day throughput. And the foregone-value bound (`loc_v2`)
uses the parent-hour DA LMP rather than the MTU's RT LMP, because what the
battery gave up by holding headroom is the DA arbitrage, not an RT scarcity
windfall it could never have captured. The engine emits one LOC row per
reserve product, named `LOC_<product>`. Reg LOC is not emitted: self-scheduled Regulation is
ineligible under PJM rules, and full M28 Reg LOC is deliberately not
modeled.

## Output schema

Two parquet streams per asset, written incrementally under a run directory
(tooling resolves the runs root via `$PJM_RUNS_ROOT`, default
`./evaluation/runs` relative to the current directory):

- **`revenue/asset_id={id}/year={Y}/month={M}.parquet`**: one row per
  (event, product, period): `event_ts_utc`, `asset_id`, `product`
  (`DA_Energy`, `RT_Energy`, `Reg_v2`, `RegA_v1`, `RegD_v1`, `SR_RTO`,
  `Sec_RTO`, `LOC_SR_RTO`, `LOC_Sec_RTO`, `Capacity_RPM`, plus
  shortfall/clawback rows), `period_start_utc`, `period_end_utc`,
  `cleared_mw`, `clearing_price`, `revenue`, `formula_version`.
- **`soc/asset_id={id}/year={Y}/month={M}.parquet`**: one row per MTU:
  `ts_utc`, `asset_id`, `soc_mwh`, `soc_pct`, `charge_mw`, `discharge_mw`,
  `reg_deployment_mw` (signed, + = inject), `cum_cycles`.

Timestamps are stored UTC. PJM feeds are hour-beginning. DST days (23h/25h)
fall out of UTC storage naturally, with gate times converted via `zoneinfo`
at firing time. Reserve and regulation prices are $/MW-h, so 5-min revenue
divides by 12.

## What changed at the regime boundary

| | Pre-Oct-2025 (v1) | Post-Oct-2025 (v2) | Post-Oct-2026 (v3 stub) |
|---|---|---|---|
| Reg products | RegA, RegD (separate) | Single bidirectional Reg | RegUp + RegDn (split) |
| Reg cadence | Hourly | Half-hourly assignment blocks | TBD |
| Settlement | Mileage-premium per product | Single PFP formula | Stub until PJM publishes specs |
| Bid schema | `RegA_v1`, `RegD_v1` | `Reg_v2` | `RegUp_v3`, `RegDn_v3` |

`ProductNotInRegimeError` enforces the boundary in both directions.

## Perfect-foresight MILP benchmark

`optimization/pjm_optimization/perfect_foresight.py` solves the whole
window as one energy-only MILP (a mixed-integer linear program, solved by
OR-tools/CBC in about 30 seconds for 6 months) with full knowledge of
prices. That makes it the upper-bound P&L a backtest is measured against,
not a strategy anyone could trade. Because it is energy-only, it is an
honest ceiling for energy runs. There is no reserve-stacked ceiling.

`optimization/pjm_optimization/forecast.py` defines the `Forecaster`
interface for rolling-horizon strategies, with one reference
implementation: an oracle that returns realized prices, used to validate
the rolling-horizon math against the ceiling.

## Evaluation package

`evaluation/pjm_eval` scores and reports engine output. It does not import
from the engine. Parquet is the contract, and product and formula-version
strings are opaque to it. It parses asset configs from
`docs/example-assets.md`. Metrics (total, $/kW-yr, capture rate, drawdown,
cycles) are small and local. `evaluation/scripts/generate_synthetic_data.py`
writes a schema-compatible synthetic cache so the whole pipeline runs
without fetched data. `optimization/scripts/run_perfect_foresight.py` is
the end-to-end demonstration. It produces revenue/SoC parquet in the runs
layout the viz tool reads.

## Calibration caveats

The engine enforces the rules correctly, but the perfect-tracking,
price-taker modeling assumptions inflate reserve and regulation revenue
relative to live operations:

1. **Price-taker clearing** (the dominant source): a large Reg bid would
   depress the clearing price in a real auction. The engine clears it at
   the historical price unchanged.
2. **Performance score** is the system-wide `rto_perfscore` per period, not
   a per-asset score. PJM does not publish per-asset history.
3. **Mileage ratio = 1.0**: correct under the perfect-tracking assumption,
   blind to tracking error.
4. **Capacity revenue is a no-stress-year ceiling**, not expected value.
   Performance Assessment Hours are not simulated, and a few of them in a
   stressed summer can wipe out a year of capacity revenue (M21B, Tariff
   Att DD).

The consequence: relative ordering of reserve-stacked runs is informative,
but absolute $/kW-yr for Reg-heavy runs can be a multiple of a realistic
ceiling. Energy-only results do not suffer this inflation. Also not
modeled: balancing operating reserve cost allocation, make-whole/uplift,
ramp-rate limits, Sec within-day clawback (Sec revenue slightly
overstated), Reg rolling disqualification, storage shoulder-interval LOC
(correctly not emitted per M28 §4.2.2.2), and portfolio-level SR
aggregate-response offsets.

## Visualization

`viz_server/` + `viz_client/` replay the bitemporal market view: a local
FastAPI server serving only `view_as_of`-filtered series for a chosen
decision time (the same oracle as the engine, property-tested against it),
and a React UI with a scrubbable decision-time cursor. A two-cursor mode
pins a target MTU and replays how its prices and forecasts became visible.
