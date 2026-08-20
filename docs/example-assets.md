# Example assets

Two fictional example batteries used throughout the repo (tests, example
runs, the viz reference page). They are round-number configurations for
demonstration only and do not correspond to any real project. Each asset
runs its own independent strategy, and portfolio P&L is the sum.

`example_a` is the canonical asset shipped in the engine registry
(`engine/pjm_engine/battery.py`) and pinned by the engine and optimization
tests. `example_b` exists only in the docs, to illustrate adding a second
asset in another zone.

## Sizing

| Asset | Zone | MW / MWh | UCAP MW |
|---|---|---|---:|
| example_a | PJM-RTO | 250 / 1,000 | 125 |
| example_b | DAY     | 20 / 80     | 10 |

MW is the power rating (how fast the battery can charge or discharge), MWh
is the energy rating (how much it holds), and UCAP is the unforced
capacity credited in PJM's capacity market. Modeling is zone-level only
(nodal pricing is out of scope, see `design.md`, Scope), so no pnode
mapping is needed. Any public PJM LMP zone works. Add your own rows to
model a different configuration.

## Constraints (both)

| | |
|---|---|
| SoC band | 10% - 90% |
| RTE | 0.85 |
| Bid increment | 0.1 MW |
| Cycle cost | $5/MWh × \|dispatch\| |
| Perf score | calibrated per-period from `reg_market_results.rto_perfscore` (mean ~0.89 in the post-redesign window); falls back to 0.95 constant when missing |
| Mileage ratio | 1.0 (perfect-tracking assumption, see note below) |
| SR Sub-Zone | RTO (neither example is in MAD) |
| NSR | excluded (storage resources may not offer it) |
| UCAP (capacity) | 4-hr storage ELCC class rating, ~50% of nameplate. Per-asset values in the sizing table. |

SoC is state of charge, and the band means the battery never runs below
10% or above 90% full. RTE is round-trip efficiency: store 1 MWh, get 0.85
MWh back. MAD is the Mid-Atlantic Dominion reserve sub-zone.

## Note on the performance score

The pure perfect-tracking assumption says `perf_score = 1.0`, meaning the
asset follows the regulation signal perfectly. The engine's fallback
constant is 0.95 instead, a 5% haircut to approximate observed PJM battery
scores (typically 0.85-0.95). Under `perf_score = 1.0` Regulation revenue
is mathematically correct but unrealistically high, which distorts
comparisons against energy-only runs. The constant lives in
`runner.py:REG_PERF_SCORE`. Flip it back to 1.0 to recover the
perfect-tracking ceiling for sensitivity analysis. When the historical
system-wide score is available the engine prefers it per period
(`reg_market_results.rto_perfscore`). Per-asset historical score ingest
stays out of scope.

## Note on the mileage ratio

Per M28 §4.2.1, `Mileage Ratio = 5-min actual requested mileage / Daily
historic requested mileage`, where the daily historic is the resource's own
rolling 30-day daily average requested mileage (M11 §3.5), NOT a "RegA
benchmark". The dataminer column `reg_market_results.rega_mileage` is a
system-wide mileage figure in MW-of-movement (typically 6-15), not a ratio.
Under the perfect-tracking assumption the asset's 5-min actual mileage
equals its own 30-day historic average, so the ratio collapses to 1.0. The
engine hardcodes `REG_MILEAGE_RATIO = 1.0` in `runner.py`.
