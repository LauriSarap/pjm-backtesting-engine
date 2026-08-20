# PJM data reference

The PJM data feeds the engine consumes, fetched by the scripts in
`scripts/` into `$PJM_DATA_ROOT` (default `./data`) under `raw/pjm/`. This
file covers what each datum means and, just as importantly, when each datum
first becomes knowable in real-time operations. The engine's bitemporal
`published_at` rules are derived from this file, so if a timing claim here
is wrong, the backtest is wrong.

All times are EPT (Eastern Prevailing Time, PJM's market clock: Eastern
Standard in winter, Eastern Daylight in summer) unless marked UTC. "MTU"
means market time unit, the settlement interval for the product. "D-1"
means the day before operating day D.

## 1. The markets in one picture

PJM runs two energy markets (Day-Ahead hourly, Real-Time 5-minute) and
ancillary-service products that clear together with energy. On October 1,
2025 the Regulation Market Redesign collapsed the two regulation products
(RegA and RegD) into a single bidirectional signal cleared on half-hour
assignment blocks, and some feeds change cadence at that boundary. The
trading-day timeline for operating day D (EPT):

| When | What |
|---|---|
| D-1 11:00 | DA gate close: energy + DA reserve bids lock |
| D-1 before 13:30 | DA results post: hourly DA LMPs and DA awards |
| D-1 14:15 | Daily Regulation offer locks; RAC begins |
| T-65 min | RT offer-revision lock for the operating hour |
| T-35 min | Regulation MW / availability updates lock |
| every 5 min | RT SCED + LPC: 5-min basepoints, LMPs, reserve MCPs |
| every half hour (post-Oct-2025) | Reg assignment block; AReg posted |
| Tuesday after operating week | Preliminary weekly settlement statement |
| ~30-60 days after | Verified statement; performance scores reconciled |

SCED is PJM's real-time dispatch engine and LPC is the price calculator
that runs alongside it. RAC is the reliability commitment run after the DA
market. AReg is the assigned regulation MW.

## 2. Ancillary-service products

| Product | Response | Battery? |
|---|---|---|
| Regulation | 2-sec AGC signal, half-hour assignment (post-redesign) | Yes |
| Synchronized Reserve (SR) | 10 min from online, 30-min sustain | Yes |
| Secondary (30-min) Reserve | 10-30 min, online or offline | Yes (clears near $0 most hours) |
| Non-Synchronized Reserve | 10 min from offline | **No, storage resources are excluded** |

AGC is the automatic generation control signal, the fast setpoint a
regulating resource must follow. Substitution runs up the reliability
ladder only: a synchronized MW counts toward Primary and 30-min, but not
the other way around. Primary Reserve itself is not separately bid.

### 2.1 Regulation

Gate timing (the engine's Reg visibility model):

| Action / datum | When (EPT) |
|---|---|
| Daily cost/price Reg offer locks | D-1 14:15 |
| Hourly Reg MW / min / max updates lock | T-35 min |
| Half-hour availability / status updates lock | T-35 min |
| AReg (assigned MW) posts | HH:00 - 10 min / HH:30 - 10 min (M11 §3.7.5) |
| 5-min RMCP / RMCCP / RMPCP post | ~MTU_end + 5-10 min |
| 5-min performance score | settlement time only (Tuesday prelim, verified ~30-60 days) |

Regulation is not cleared day-ahead. The daily offer is submitted before
the operating day, but AReg is assigned in half-hour real-time blocks. The
post-redesign award is a single bidirectional capacity: one sign-less MW
quantity regulating both directions around a midpoint.

Pricing: the regulation clearing price RMCP splits into RMCCP (capability,
paid for being assigned) and RMPCP (performance or "mileage", paid for
movement, called `reg_pcp` in PJM CSVs). Per 5-min interval i:

```
Reg_credit_i = Reg_MW * score * (RMCCP + Mileage_Ratio * RMPCP) / 12
```

Signal-following energy additionally settles as RT energy: positive
deployment sells at RT LMP, negative buys. The Mileage Ratio is the 5-min
actual requested mileage divided by the resource's own rolling 30-day
average daily mileage (M28 §4.2.1, M11 §3.5). Under the engine's
perfect-tracking assumption it collapses to 1.0 (see `example-assets.md`).
Score thresholds: a 5-min score below 0.25 forfeits that interval's credit
and LOC. A rolling score below 0.40 disqualifies the resource until it
requalifies. Self-deassignment scores zero.

### 2.2 Synchronized Reserve

A paid promise: if PJM calls an event, inject the cleared MW within 10
minutes and sustain it for up to 30 minutes. Offers go in DA by D-1 11:00,
with RT revisions until T-65 min. DA cleared MW and the DA clearing price
(SRMCP) post by D-1 13:30. RT 5-min SRMCP and assignments post 5-10 minutes
after each MTU.

Revenue is two-settlement (M28 §6.2.1/§6.2.2): the DA leg pays
`DA_MW * DA_SRMCP_h` per hour, and the balancing leg pays only the delta,
`(RT_MW - DA_MW) * RT_SRMCP / 12` per MTU. A DA-only position therefore
earns $0 at RT gates. There is no mileage or performance score in normal
hours. PJM penalizes event non-delivery separately (§7.2).

**Battery SoC requirement** (enforced by the engine's validator): X MW of
SR for an hour requires continuous upward headroom of X MW **and at least
X * 0.5 MWh of energy available**, the 30-min sustain rule.

Reserve zones: the system-wide RTO zone plus at most one active Sub-Zone
(Mid-Atlantic Dominion in the covered window). Bus and resource-to-sub-zone
mappings live in `sync_pri_reserves_buses_list` /
`sync_pri_reserves_resources_list`. The RTO demand curve steps at $850/MWh
at the requirement and $300/MWh at requirement + 190 MW (M11 §4.3).

### 2.3 Secondary (30-minute) Reserve

Same two-settlement shape as SR (M28 §19.2.2) with its own clearing price.
It clears at or near zero most hours because idle 30-minute capability is
plentiful.

## 3. The two energy markets

**Day-Ahead:** one auction per day, gate D-1 11:00, hourly granularity.
Awards are financially binding: a DA cleared position earns DA LMP
regardless of what the battery actually does in real time. **Real-Time:**
SCED clears every 5 minutes and settles deviations from the DA schedule at
the RT 5-min LMP
(`rev = MW_DA * LMP_DA + sum_i (MW_RT,i - MW_DA) * LMP_RT,i / 12`). Every
LMP decomposes as `total = system energy + congestion + loss`, in both DA
and RT, and the feeds carry all components.

## 4. Raw data feeds

The scripts in `scripts/` fetch DataMiner2 CSVs into `$PJM_DATA_ROOT`
(default `./data`) under `raw/pjm/`, partitioned by month (LMP feeds) or
quarter (AS feeds). A full 2021-01 through 2026-05 pull is about 1.1 GB.
The engine's parquet cache lives alongside under `$PJM_DATA_ROOT`/`cache/`.
Feeds with `row_is_current` / `version_nbr` columns carry PJM's republished
corrections. The loader filters to `row_is_current=True`.

Environment variables:

- **`PJM_API_KEY`**: required by the DataMiner fetchers. Register a free
  key at PJM's API portal (<https://apiportal.pjm.com>).
- **`GRIDSTATUS_API_KEY`**: required by the GridStatus fetchers.
- **`PJM_DATA_ROOT`**: root directory for raw and cached data (default
  `./data` relative to the current directory): raw CSVs under `raw/pjm/`,
  parquet cache under `cache/`.
- **`PJM_RUNS_ROOT`**: root directory for backtest run output (default
  `./evaluation/runs`). The run tooling uses it, not the fetch scripts.

The scripts:

| Script | Purpose |
|---|---|
| `fetch_pjm_dataminer_zone_lmps.py` | Zone-level LMPs from DataMiner2: `da_hrl_lmps`, `rt_fivemin_mnt_lmps` (and the preliminary `rt_fivemin_hrl_lmps` tail) |
| `fetch_pjm_dataminer_da_lmps.py` | DA hourly LMPs for specific pricing nodes (nodal studies beyond the zone aggregates) |
| `fetch_pjm_dataminer_ancillary_services.py` | The AS feeds: DA/RT reserve and Reg prices, market results, `reg_prices`, sub-zone mapping lists, `sync_reserve_events` |
| `fetch_pjm_dataminer_context.py` | Optional load / generation / weather context feeds (forecasts, fuel mix, outages), not consumed by the engine |
| `fetch_pjm_dataminer_catalog.py` | Snapshot of the active DataMiner2 feed catalog (names, coverage, posting cadence) to CSV |
| `fetch_pjm_capacity_market.py` | RPM capacity-market workbooks (auction clearing prices, schedules) from pjm.com; no API key needed |
| `fetch_pjm_gridstatus_da_lmps.py` | DA hourly LMPs for chosen pnodes via the GridStatus API (alternative LMP source) |
| `fetch_pjm_gridstatus_nodes.py` | One-day DA LMP snapshot via GridStatus to enumerate PJM pricing nodes |
| `audit_pjm_data.py` | Audits the locally fetched raw data (coverage, gaps, schema) and writes a Markdown + JSON report |

| Feed | What | Cadence | Coverage |
|---|---|---|---|
| `da_hrl_lmps` | DA hourly LMPs per zone pnode (all components) | hourly | 2021-01 - present |
| `rt_fivemin_mnt_lmps` | RT 5-min LMPs, settlements-verified | 5-min | 2021-01 - present |
| `rt_fivemin_hrl_lmps` | RT 5-min LMPs, preliminary | 5-min | rolling ~2-day tail only |
| `da_ancillary_services` | DA AS clearing prices, long format | hourly | 2022-10 - present |
| `da_reserve_market_results` | DA reserve clearing detail (mcp, req MW, cleared MW) | hourly | 2022-10 - present |
| `ancillary_services_fivemin_hrl` | RT 5-min AS prices, long format | 5-min | 2021-01 - present |
| `reserve_market_results` | RT reserve detail incl. `reg_ccp`/`reg_pcp` | hourly pre-2022-10, 5-min after | 2021-01 - present |
| `reg_market_results` | Reg procurement summary: `rto_perfscore`, mileage, `modified_datetime_utc` | hourly pre-2025-10, half-hourly after | 2021-01 - present |
| `reg_prices` | RT 5-min RMCCP/RMPCP split | 5-min | 2026-04 - present only |
| `sync_reserve_events` | Historical SR event log | per event | 5 years |
| `sync_pri_reserves_{buses,resources}_list` | Sub-zone mappings | reference | versioned by effective date |

Feed gaps: the DA AS feeds start 2022-10 (re-fetch from DataMiner2 if
earlier coverage is needed), and `reg_prices` starts 2026-04, so historical
RMCCP/RMPCP are reconstructed from
`reserve_market_results.reg_ccp`/`reg_pcp`.

## 5. Publication timing (the bitemporal model)

Raw PJM CSVs carry no `published_at` column. The loader computes it from
the row's settlement timestamp plus the feed's known publication delay
(§5.1). Where PJM provides a row-level revision marker
(`reg_market_results.modified_datetime_utc`), the loader uses it directly.

### 5.0.1 Backtest simplification: verified RT LMP treated as live

In real operations the verified RT feed (`rt_fivemin_mnt_lmps`) appears
about 2 days after the operating day. A live operator sees the preliminary
feed at MTU_end + 5-10 min, but PJM retains only a 2-day tail of it, so no
historical preliminary data exists. **Engine convention:**
`rt_fivemin_mnt_lmps` is stamped `published_at = MTU_end + 5 min`
(= `MTU_start + 10 min`), per M11 §2.5.3.4 (prices post shortly after
dispatch signals) and §3.7.6 (LPC posts with energy every 5 min). The
verified value stands in for what the preliminary feed showed. This is
acceptable because preliminary and verified RT LMPs typically differ by a
fraction of a percent.

### 5.1 The publication-delay table (engine-wired)

| Datum | `published_at` rule | Source feed |
|---|---|---|
| DA hourly LMP | **D-1 13:30 EPT** of operating day D | `da_hrl_lmps` |
| DA energy / AS awards | D-1 13:30 EPT | synthesized from cleared bids |
| DA AS clearing prices | D-1 13:30 EPT | `da_ancillary_services`, `da_reserve_market_results` |
| RT 5-min LMP | **MTU_end + 5 min** (M11 §2.5.3.4 / §3.7.6; see §5.0.1) | `rt_fivemin_mnt_lmps` |
| RT 5-min reserve clearing | MTU_end + 5 min (M11 §3.7.6) | `reserve_market_results`, `ancillary_services_fivemin_hrl` |
| RT 5-min RMCP / RMCCP / RMPCP | MTU_end + 5 min (M11 §3.7.6) | `reg_prices` (2026-04+) or `reserve_market_results` |
| Reg summary rows (perf score, mileage) | `modified_datetime_utc` when populated; else block_start - 10 min (M11 §3.7.5) | `reg_market_results` |
| AReg per-asset assignment | HH:00/HH:30 - 10 min (M11 §3.7.5) | not fetched by the scripts in `scripts/`; award assumed = cleared MW |
| Per-asset perf score / mileage | Tuesday after operating week | not fetched by the scripts in `scripts/`; see §5.3 |

### 5.2 Decision-time visibility (the RT blind window)

Engine convention: `published_at(rt_lmp) = MTU_end + 5 min = MTU_start +
10 min`. At decision time `t` for an MTU starting at `S >= t`:

- All DA LMPs for the operating day are visible (locked D-1 13:30 EPT).
- RT LMPs are visible for MTUs with `MTU_start + 10 <= t`, that is, MTUs
  starting at or before `t - 10 min`.
- MTUs in `[t - 10, t]` are blind, as is the target MTU itself.

A concrete walk-through: at `t = 18:30:00`, deciding for the MTU starting
`S = 18:30`, the latest visible RT LMP is for `[18:15, 18:20]` (published
18:25). `[18:20, 18:25]` becomes visible at exactly 18:30, and
`[18:25, 18:30]` is not visible (published 18:35). So the blind window is
**2 MTUs (10 min)** leading up to `t`. The engine fires one RT gate per MTU
at `MTU_start - 5 min`, which means a persistence forecast for the target
MTU is the RT LMP of the MTU ending at `t - 5 min`, a value 5-10 minutes
stale. And deviation cannot escape a reserve commitment: SoC reservation
rules and performance penalties still apply.

### 5.3 Not in the static feeds

- **AReg per-asset assignments**: historical data is system-aggregate. The
  engine assumes the award follows the cleared MW.
- **2-sec regulation control signal**: PJM archives it separately. The
  exogenous performance score under the perfect-tracking assumption
  absorbs it.
- **Per-asset performance scores**: only the system-wide
  `reg_market_results.rto_perfscore` is published. The engine uses it as a
  proxy for every asset.
- **Member settlement statements**: these are member-level artifacts. The
  engine reconstructs settlement from raw feeds plus formulas instead.

## 6. Settlement cadence

PJM bills weekly: a preliminary statement the Tuesday after the operating
week and a verified statement 30-60 days later (the M28 §19.4
reconciliation window, cadence per M29), with true-ups possible for months
afterward.
Score-dependent Regulation revenue is therefore bitemporally versioned
(`weekly_preliminary` -> `weekly_verified` -> `adjustment`). The data layer
tracks versions and serves the latest with `published_at <= now`.

## 7. Performance and penalty rules

### 7.1 Regulation

A 5-min score below 0.25 forfeits the interval's Reg credit and LOC. A
rolling historic score below 0.40 disqualifies the resource from the Reg
market until it requalifies. Self-deassignment scores zero for the affected
period.

### 7.2 Synchronized Reserve (event non-delivery)

Shortfall on a called event costs twice: a charge of SRMCP x MW not
delivered on the event day, **plus a retroactive refund of prior SR
credits** going back the average inter-event interval. That interval is a
rolling 2-year statistic, computed annually, and about 30 days in
practice. The engine encodes it as `SR_CLAWBACK_DAYS`, replays historical
events from `sync_reserve_events`, and emits shortfall and clawback rows.

### 7.3 Secondary Reserve

Sec pays credit only for MW that actually responded during an event, and
there is no retroactive clawback. Within-day Sec clawback per M11 §4.5.2
is not modeled (see the design.md calibration caveats).
