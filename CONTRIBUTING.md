# Contributing

## Setup

```bash
uv sync                       # installs all workspace packages + dev tools
uv run pytest engine optimization evaluation viz_server
```

The test suites run without market data. Data-dependent tests skip. To run
them, either generate synthetic data
(`uv run python evaluation/scripts/generate_synthetic_data.py`) or fetch real
PJM data (see `docs/pjm-data.md`).

## Checks

Before opening a PR:

```bash
uv run ruff check .
uv run ruff format .
uv run pytest engine optimization evaluation viz_server
```

CI runs the same three commands plus the synthetic-data quickstart.

## Layout rules

- `engine/` must not import from strategies, evaluation, or viz. Strategies
  implement `BaseStrategy.on_event(event, ctx)` and read data only through
  `ctx.view`.
- `evaluation/pjm_eval` reads engine output structurally (parquet /
  duck-typed results), never through `pjm_engine` imports.
- Settlement formulas carry PJM manual citations (such as `M28 §6.2.2`) in
  their docstrings. Keep the citations accurate when you touch a formula.
  `docs/pjm-manuals.md` lists the manuals.
- Comments state constraints the code can't. Put explanations in `docs/`.

## Scope

Market-mechanics fidelity fixes (settlement formulas, gate timing,
publication lags, validators) are the most valuable contributions and need a
manual citation or a PJM data observation to back them. The repo ships no
trading strategies and accepts none. The only reference algorithm is the
perfect-foresight MILP benchmark. Strategy code belongs in your own
repository, built against the `BaseStrategy` contract.
