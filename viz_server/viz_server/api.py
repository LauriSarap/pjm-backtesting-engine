"""FastAPI app for the viz_server.

Boots a 127.0.0.1-only HTTP server that pre-loads every registered feed at
startup, wraps each in a CachedView, and serves as-of slices over Arrow
IPC.

Endpoints:
    GET /api/feeds   -> JSON: feed metadata
    GET /api/zones   -> JSON: available zones
    GET /api/series  -> Arrow IPC bytes (or JSON when format=json)
    GET /healthz     -> {"ok": true} after startup completes

Design rules:
- Engine is the single source of truth. We never patch loaders, never mutate
  the frames pjm_engine returns. Read-only.
- 127.0.0.1 only. No auth. Local research instrument.
- Per-request 5s timeout, asyncio.Semaphore(8) global concurrency cap.
- Server returns UTC; client converts to EPT at render.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from pjm_eval.io import (
    PF_STRATEGY_NAME,
    RunNotFound,
    SchemaMismatchError,
    StrategyNotFound,
    StrategyRunInfo,
    find_strategy_run,
    list_strategy_runs,
    load_pf_ceiling_revenue,
    load_strategy_revenue,
    schema_check_at_boot,
)
from pjm_eval.time import TimeWindow

from . import runtime as runtime_mod
from . import series as series_mod
from .arrow_stream import encode_series
from .errors import (
    INVALID_PARAMS,
    QUERY_TIMEOUT,
    UNSUPPORTED_FEED,
    envelope,
)
from .feed_registry import FEEDS, list_feeds

log = logging.getLogger("viz_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


# Concurrency cap. FastAPI's threadpool would let us oversubscribe pandas;
# 8 lets us serve a few panes in parallel without thrashing.
_SEM = asyncio.Semaphore(8)
_REQUEST_TIMEOUT_S = 5.0


# Strategy run discovery: walk evaluation/runs/ at boot, hold the index in
# memory. Tiny (<1KB), refreshed only on restart.
_STRATEGY_RUNS: list[StrategyRunInfo] = []


def _runs_root() -> Path:
    """`$PJM_RUNS_ROOT` if set, else `evaluation/runs` relative to CWD."""
    override = os.environ.get("PJM_RUNS_ROOT")
    if override:
        return Path(override)
    return Path("evaluation/runs").resolve()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    log.info("viz_server starting up — preloading engine feeds…")
    rt = await asyncio.to_thread(runtime_mod.preload)
    runtime_mod.set_runtime(rt)
    log.info(
        "viz_server ready: %d loaders, boot=%.2fs",
        len(rt.loaders),
        rt.boot_seconds,
    )

    # Strategy runs: validate schemas + index for /api/strategy_runs.
    runs_root = _runs_root()
    log.info("scanning strategy runs at %s", runs_root)
    try:
        schema_check_at_boot(runs_root)
    except SchemaMismatchError:
        # Fail fast: a corrupted parquet must not stay hidden behind a
        # late-loading endpoint. Re-raise to abort startup.
        log.exception("strategy run schema check failed; refusing to start")
        raise
    global _STRATEGY_RUNS
    _STRATEGY_RUNS = list_strategy_runs(runs_root)
    log.info("indexed %d strategy run tuples", len(_STRATEGY_RUNS))

    yield
    log.info("viz_server shutting down")


app = FastAPI(
    title="PJM viz_server",
    version="0.1.0",
    lifespan=_lifespan,
    docs_url="/docs",
)

# CORS for the Vite dev server (default 5173) on the same host.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# --- Helpers ---------------------------------------------------------------


def _parse_iso_utc(name: str, raw: str) -> datetime:
    try:
        # Accept both "Z" and explicit offsets.
        s = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise _bad_param(f"{name} must be ISO8601 UTC, got {raw!r}: {e}") from e
    if dt.tzinfo is None:
        raise _bad_param(f"{name} must be tz-aware ISO8601, got naive {raw!r}")
    return dt.astimezone(timezone.utc)


def _bad_param(message: str) -> HTTPException:
    return HTTPException(status_code=400, detail=envelope(INVALID_PARAMS, message))


# --- Endpoints -------------------------------------------------------------


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": runtime_mod.runtime_ready()}


@app.get("/api/feeds")
def api_feeds() -> dict:
    return {"feeds": list_feeds()}


@app.get("/api/zones")
def api_zones() -> dict:
    rt = runtime_mod.get_runtime()
    return {"zones": rt.all_zones()}


@app.get("/api/series")
async def api_series(
    feeds: str = Query(..., description="comma-separated feed_ids"),
    zone: str | None = Query(None, description="zone name; required for zonal feeds"),
    target_from: str = Query(..., description="UTC ISO8601"),
    target_to: str = Query(..., description="UTC ISO8601"),
    decision_time: str | None = Query(None, description="UTC ISO8601; default = now"),
    resolution: str = Query("auto", pattern="^(auto|5min|15min|1h|1d)$"),
    format: str = Query("arrow", pattern="^(arrow|json)$"),
) -> Response:
    feed_ids = [f.strip() for f in feeds.split(",") if f.strip()]
    if not feed_ids:
        raise _bad_param("feeds must be a comma-separated list of feed_ids")

    unknown = [fid for fid in feed_ids if fid not in FEEDS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=envelope(
                UNSUPPORTED_FEED,
                f"unknown feed_id(s): {unknown}",
                remediation=f"valid feed_ids: {sorted(FEEDS)}",
            ),
        )

    t_from = _parse_iso_utc("target_from", target_from)
    t_to = _parse_iso_utc("target_to", target_to)
    if t_to <= t_from:
        raise _bad_param("target_to must be strictly greater than target_from")

    dt = (
        _parse_iso_utc("decision_time", decision_time)
        if decision_time
        else datetime.now(timezone.utc)
    )

    rt = runtime_mod.get_runtime()

    async def _run():
        return await asyncio.to_thread(
            series_mod.query,
            rt,
            feed_ids,
            dt,
            t_from,
            t_to,
            zone,
            resolution,  # type: ignore[arg-type]
        )

    try:
        async with _SEM:
            result = await asyncio.wait_for(_run(), timeout=_REQUEST_TIMEOUT_S)
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=503,
            content=envelope(
                QUERY_TIMEOUT,
                f"query exceeded {_REQUEST_TIMEOUT_S}s",
                remediation="narrow target window or coarsen resolution",
            ),
        )

    if format == "json":
        df = result.frame.copy()
        if len(df):
            df["t"] = df["t"].dt.tz_convert("UTC").dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            # Mixed-cadence resample produces NaN buckets (e.g. DA at 5min
            # resolution leaves 11 NaN slots per hour). json.dumps rejects NaN,
            # so coerce to None.
            df["value"] = df["value"].astype(object).where(df["value"].notna(), None)
        return {
            "resolution": result.resolution,
            "decision_time": dt.isoformat(),
            "target_from": t_from.isoformat(),
            "target_to": t_to.isoformat(),
            "partial": bool(result.errors),
            "errors": [e.as_dict() for e in result.errors],
            "rows": df.to_dict(orient="records"),
        }

    body = encode_series(result, dt, t_from, t_to)
    return Response(content=body, media_type="application/vnd.apache.arrow.stream")


# --- Strategy run endpoints ------------------------------------------------
#
# /api/strategy_runs   -> discovery: which (run, strategy, asset) tuples exist
# /api/strategy_series -> as-of-filtered cleared MW + cum revenue,
#                          with optional PF ceiling overlay
#
# These read parquet files emitted by pjm_engine.runner. Loader lives in
# pjm_eval.io; endpoint surface lives here. Data volumes are tiny (158 rev
# rows + 721 soc rows per month per strategy), so JSON is fine -- no Arrow IPC.


@app.get("/api/strategy_runs")
def api_strategy_runs() -> dict:
    """Available (run, strategy, asset) tuples discovered at boot."""
    out = []
    for info in _STRATEGY_RUNS:
        out.append(
            {
                "run": info.run_name,
                "strategy": info.strategy,
                "asset": info.asset_id,
                "window": {
                    "from": info.window.start.isoformat(),
                    "to": info.window.end.isoformat(),
                },
                "is_pf_ceiling": info.is_pf_ceiling(),
            }
        )
    return {"runs": out}


def _serialize_cleared(df: pd.DataFrame) -> list[dict]:
    """Per-award rows the frontend turns into signed bars (energy) /
    capacity bands (AS)."""
    if df.empty:
        return []
    return [
        {
            "t": ts.isoformat(),
            "t_end": te.isoformat(),
            "mw": float(mw),
            "product": str(prod),
            "revenue": float(rev),
        }
        for ts, te, mw, prod, rev in zip(
            df["period_start_utc"],
            df["period_end_utc"],
            df["cleared_mw"],
            df["product"],
            df["revenue"],
        )
    ]


def _serialize_cum_revenue(df: pd.DataFrame) -> list[dict]:
    """Step series: at each award's period_start, the running total."""
    if df.empty:
        return []
    sorted_df = df.sort_values("period_start_utc")
    cum = sorted_df["revenue"].cumsum()
    return [
        {"t": ts.isoformat(), "value": float(v)}
        for ts, v in zip(sorted_df["period_start_utc"], cum)
    ]


@app.get("/api/strategy_series")
def api_strategy_series(
    run: str = Query(..., description="run name, e.g. 'demo'"),
    strategy: str = Query(..., description="strategy id, e.g. 'perfect_foresight'"),
    asset: str = Query(..., description="asset id, e.g. 'example_a'"),
    target_from: str = Query(..., description="UTC ISO8601"),
    target_to: str = Query(..., description="UTC ISO8601"),
    decision_time: str | None = Query(None, description="UTC ISO8601; default = now"),
    include_pf: bool = Query(
        True, description="include perfect_foresight ceiling line if available"
    ),
) -> dict:
    """As-of-filtered cleared MW + cumulative revenue for a strategy.

    PF ceiling is included by default if a perfect_foresight run exists with
    the same (run, asset). Always returned unfiltered (it's the oracle).
    """
    runs_root = _runs_root()
    t_from = _parse_iso_utc("target_from", target_from)
    t_to = _parse_iso_utc("target_to", target_to)
    if t_to <= t_from:
        raise _bad_param("target_to must be strictly greater than target_from")
    dt = (
        _parse_iso_utc("decision_time", decision_time)
        if decision_time
        else datetime.now(timezone.utc)
    )
    window = TimeWindow(start=t_from, end=t_to)

    try:
        info = find_strategy_run(runs_root, run, strategy, asset)
    except RunNotFound as e:
        raise HTTPException(
            status_code=404,
            detail=envelope("run_not_found", str(e), remediation=""),
        ) from e
    except StrategyNotFound as e:
        raise HTTPException(
            status_code=404,
            detail=envelope("strategy_not_found", str(e), remediation=""),
        ) from e

    if info.is_pf_ceiling():
        # Caller asked for PF directly: no decision_time clamp.
        df = load_pf_ceiling_revenue(info, window)
    else:
        df = load_strategy_revenue(info, window, dt)

    response: dict = {
        "strategy": {
            "run": info.run_name,
            "strategy": info.strategy,
            "asset": info.asset_id,
            "is_pf_ceiling": info.is_pf_ceiling(),
            "window": {
                "from": info.window.start.isoformat(),
                "to": info.window.end.isoformat(),
            },
        },
        "decision_time": dt.isoformat(),
        "target_from": t_from.isoformat(),
        "target_to": t_to.isoformat(),
        "cleared_mw": _serialize_cleared(df),
        "cum_revenue": _serialize_cum_revenue(df),
        "pf_ceiling": None,
        "partial": False,
    }

    # Optional PF ceiling overlay. Only attempt if this isn't already PF.
    if include_pf and not info.is_pf_ceiling():
        try:
            pf_info = find_strategy_run(runs_root, run, PF_STRATEGY_NAME, asset)
            pf_df = load_pf_ceiling_revenue(pf_info, window)
            response["pf_ceiling"] = _serialize_cum_revenue(pf_df)
        except (RunNotFound, StrategyNotFound):
            # PF not available for this run/asset; render strategy alone.
            response["partial"] = True
            response["pf_ceiling"] = None

    return response
