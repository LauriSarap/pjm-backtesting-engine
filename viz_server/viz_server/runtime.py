"""Boot-time preload of every registered feed into a CachedView.

Preload runs once at FastAPI lifespan startup. Per-request cost is then a
dict lookup + searchsorted (microseconds). Memory footprint depends on which
loaders are registered — ~500MB-2GB total with all registered feeds.

The engine is the single source of truth: we import its loaders, call them
exactly as a strategy would, and wrap the returned frame in its own
`CachedView` class. We never patch the engine.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import pandas as pd

from pjm_engine.data import CachedView

from .feed_registry import LOADERS, LoaderSpec

log = logging.getLogger(__name__)


@dataclass
class LoaderRuntime:
    """Per-loader runtime state.

    `view` is the CachedView wrapping the full loaded DataFrame. `zones` is
    the sorted unique set of zones (empty for non-zonal loaders).
    """

    spec: LoaderSpec
    view: CachedView
    zones: tuple[str, ...]


@dataclass
class Runtime:
    """In-memory state shared across requests.

    Populated once at startup; immutable thereafter.
    """

    loaders: dict[str, LoaderRuntime]
    boot_seconds: float
    rss_bytes_at_boot: int

    def get(self, loader_key: str) -> LoaderRuntime:
        return self.loaders[loader_key]

    def all_zones(self) -> list[str]:
        seen: set[str] = set()
        for lr in self.loaders.values():
            seen.update(lr.zones)
        return sorted(seen)


def _rss_bytes() -> int:
    try:
        import resource

        # Linux: ru_maxrss is KB; macOS: bytes. We're Linux-first.
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    except Exception:
        return 0


def _zones_of(df) -> tuple[str, ...]:
    if "zone" not in df.columns:
        return ()
    s = df["zone"].dropna()
    s = s[s != ""]
    return tuple(sorted(s.unique()))


def _viz_published_at_override(key: str, df: pd.DataFrame) -> pd.DataFrame:
    """Per-loader viz-only `published_at` overrides.

    The engine's defaults are conservative — `reg_market_results` stamps
    rows with `modified_datetime_utc` (settlement-time, ~3 weeks after the
    operating day). For research replay that hides the operational-visibility
    moment: a trader operationally knew the requirement and clearing MW
    shortly after the assignment block ended.

    Override here to `datetime_beginning_utc + 40 min` (assignment-block-end
    + 10 min). Engine's other consumers (strategies, evaluation, backtest
    runner) still see the conservative default — we only mutate viz_server's
    in-memory copy.

    `rto_perfscore` is genuinely settlement-time, so under this override the
    panel will show the ex-post score as if it had been knowable in real
    time. Documented in viz_client/src/views/Reference.tsx.
    """
    if key != "reg_market_results":
        return df
    out = df.copy()
    out["published_at"] = out["datetime_beginning_utc"] + pd.Timedelta(minutes=40)
    return out.sort_values("published_at", kind="stable").reset_index(drop=True)


def preload() -> Runtime:
    """Load every registered feed and wrap in CachedView. Logs progress."""
    t0 = time.perf_counter()
    loaders: dict[str, LoaderRuntime] = {}

    log.info("viz_server preload: %d loaders", len(LOADERS))
    for key, spec in LOADERS.items():
        s = time.perf_counter()
        df = spec.fn()
        df = _viz_published_at_override(key, df)
        view = CachedView(df)
        zones = _zones_of(df) if spec.zonal else ()
        loaders[key] = LoaderRuntime(spec=spec, view=view, zones=zones)
        elapsed = time.perf_counter() - s
        log.info(
            "  loaded %s rows=%d cols=%d zones=%d elapsed=%.2fs",
            key,
            len(df),
            len(df.columns),
            len(zones),
            elapsed,
        )

    boot = time.perf_counter() - t0
    rss = _rss_bytes()
    log.info(
        "viz_server preload done: %.2fs total, RSS=%dMB",
        boot,
        rss // (1024 * 1024),
    )
    return Runtime(loaders=loaders, boot_seconds=boot, rss_bytes_at_boot=rss)


# Process-wide singleton populated by the FastAPI lifespan hook. Modules that
# need it import the accessor below; nothing imports `_runtime` directly.
_runtime: Runtime | None = None


def set_runtime(rt: Runtime) -> None:
    global _runtime
    _runtime = rt


def get_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError(
            "runtime not initialized — startup hook didn't run "
            "(running viz_server.api outside FastAPI lifespan?)"
        )
    return _runtime


def runtime_ready() -> bool:
    return _runtime is not None
