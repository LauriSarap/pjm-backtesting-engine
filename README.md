# pjm-backtesting-engine

A Python backtesting engine that simulates PJM market mechanics for battery
storage (BESS) against historical data.

Every row of market data carries the time PJM actually published it. A
strategy can read only what had been published at each decision point, so
there is no lookahead to exploit by accident. Bids pass PJM's market rules
and the battery's physical limits before they clear. Cleared positions
settle with the formula in force on that operating date, including the
October 2025 regulation market redesign.

New to PJM? `docs/pjm-data.md` §1 explains the markets in one picture.

## Markets covered

- Day-ahead energy
- Real-time energy (5-minute)
- Regulation (pre- and post-redesign)
- Synchronized Reserve (including event delivery, shortfall, and clawback)
- Secondary (30-minute) Reserve
- RPM capacity accrual and Lost Opportunity Cost

## Layout

| path | role |
|---|---|
| `engine/` | `pjm-engine`: simulator, events, data layer, validators, settlement |
| `optimization/` | `pjm-optimization`: perfect-foresight MILP benchmark + forecaster plug-ins |
| `evaluation/` | `pjm-eval`: scoring, leaderboards, HTML reports |
| `viz_server/`, `viz_client/` | market replay API + local React UI |
| `scripts/` | PJM data fetchers (Data Miner 2 / gridstatus) |
| `docs/` | design doc and PJM data/manual references |

## Quickstart

Runs end-to-end with no data downloads and no API keys:

```bash
uv sync
uv run pytest engine optimization evaluation viz_server   # data tests skip

uv run python evaluation/scripts/generate_synthetic_data.py
uv run python optimization/scripts/run_perfect_foresight.py   # benchmark run
```

`generate_synthetic_data.py` writes a schema-compatible fake market into
`./data/cache`, enough to develop and test strategies offline. For real
results, fetch PJM data (the API keys are free):

```bash
export PJM_API_KEY=...          # https://apiportal.pjm.com (Data Miner 2)
python scripts/fetch_pjm_dataminer_zone_lmps.py
python scripts/fetch_pjm_dataminer_ancillary_services.py
# see docs/pjm-data.md for the full feed list and coverage notes
```

The engine reads market data from `$PJM_DATA_ROOT` (default `./data`) and
writes backtest output to `$PJM_RUNS_ROOT` (default `./evaluation/runs`).

## Writing a strategy

A strategy is a class implementing `on_event(event, ctx)`. The engine never
imports strategy code. `ctx.view` is the only data read path and is pinned
to the event's timestamp, so you cannot read anything PJM hadn't published
yet.

```python
from pjm_engine import ASSETS, BaseStrategy, Product, SelfSchedule, run_backtest


class Hold50(BaseStrategy):
    def on_event(self, event, ctx):
        return SelfSchedule(Product.SR_RTO, event.timestamp, 50.0)


result = run_backtest(Hold50(), ASSETS["example_a"], start_date=..., end_date=...)
print(result.revenue_by_product)
```

Responses are `SelfSchedule` (price-taker), `BidCurve` (price-quantity
tiers), `Acknowledgment` (no action), or a list of those. Every response
passes market-rule validators and a state-of-charge feasibility walk before
anything clears. The full contract is in
`engine/pjm_engine/strategy_base.py`. Measure your strategy against the
perfect-foresight ceiling from `optimization/`.

## Market replay UI

`viz_server` + `viz_client` replay the market view at any decision time and strategy
runs interactively:

```bash
uv run python -m viz_server          # API on 127.0.0.1
cd viz_client && bun install && bun run dev
```

See `viz_server/README.md`.

## Fidelity and limits

The simulation is zone-level (no nodal prices) and price-taker (your bids
don't move clearing prices), and it models regulation as perfect tracking.
These assumptions inflate regulation revenue, so read the calibration
caveats in `docs/design.md` before quoting absolute numbers. That document
cites the PJM manual section behind every settlement formula.
[`docs/pjm-manuals.md`](docs/pjm-manuals.md) lists the manuals. They are
public documents on pjm.com and are not redistributed here.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). CI runs ruff and the no-data test
suite plus the synthetic-data quickstart.

## License

[MIT](LICENSE)
