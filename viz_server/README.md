# viz_server

Bitemporal market-replay API for the React client in `viz_client/`. It is a
read-only consumer of `pjm_engine`. At startup it preloads every registered
feed (DA/RT LMPs, AS prices, load, renewables, outages, synthetic
forecasts) into in-memory `CachedView`s, then serves bitemporal slices,
that is, "what was knowable at decision time D about target window
[from, to)", over Arrow IPC or JSON. It also serves backtest strategy runs (cleared MW,
cumulative revenue, perfect-foresight ceiling) from parquet emitted by the
engine runner.

Binds to 127.0.0.1 only. No auth. Local research instrument.

## Endpoints

- `GET /api/feeds`: feed metadata
- `GET /api/zones`: available zones
- `GET /api/series`: bitemporal series slice (Arrow IPC, or `format=json`)
- `GET /api/strategy_runs`: backtest run discovery
- `GET /api/strategy_series`: cleared MW + cumulative revenue for one run
- `GET /healthz`: readiness

Interactive docs at `/docs`.

## Running

Requires fetched market data (see the repo README / `scripts/` fetchers).

```sh
python -m viz_server            # serves http://127.0.0.1:8765
```

Environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PJM_DATA_ROOT` | `./data` | market data root (`raw/pjm/`, `cache/`), from `pjm_engine.data` |
| `PJM_RUNS_ROOT` | `./evaluation/runs` | strategy backtest run parquet root |
| `VIZ_SERVER_HOST` | `127.0.0.1` | bind host |
| `VIZ_SERVER_PORT` | `8765` | bind port |
| `VIZ_SERVER_DEV` | unset | `1` enables uvicorn auto-reload on `viz_server/` |

Defaults resolve relative to the working directory, so start the server
from the repo root (or set the env vars).

## Client

`viz_client/` is a React + uPlot app built with Vite (bun as the package
manager):

```sh
cd viz_client
bun install
bun run dev        # Vite dev server on http://127.0.0.1:5173, proxies /api
bun run build      # production bundle in dist/
```

The dev server proxies `/api` to `http://127.0.0.1:8765` (override with
`VIZ_SERVER_URL`).

## Tests

```sh
uv run pytest viz_server
```

Data-dependent tests skip when the market data has not been fetched. The
strategy-endpoint tests use a synthetic fixture and always run.
