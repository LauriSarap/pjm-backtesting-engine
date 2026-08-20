"""Shared test config: skip data-dependent tests when data/ is not populated.

The repo ships without the multi-GB data/ directory. Loaders in
pjm_engine.data and viz_server.extra_loaders raise FileNotFoundError when
the raw CSVs (and parquet cache) are absent; wrap every public loader so
those tests skip instead of erroring. Run the scripts/fetch_pjm_*.py
fetchers to populate data/ and un-skip them.

This must run before viz_server.feed_registry is imported — LoaderSpec
captures the loader functions at import time, so the wrapped versions are
what the registry (and runtime.preload) end up calling.
"""

from __future__ import annotations

import functools

import pytest

import pjm_engine.data as _engine_data
import viz_server.extra_loaders as _extra_loaders


def _skip_on_missing_data(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except FileNotFoundError as exc:
            pytest.skip(f"market data not downloaded: {exc}")

    return wrapper


for _mod in (_engine_data, _extra_loaders):
    for _name in dir(_mod):
        if _name.startswith("load_"):
            setattr(_mod, _name, _skip_on_missing_data(getattr(_mod, _name)))
