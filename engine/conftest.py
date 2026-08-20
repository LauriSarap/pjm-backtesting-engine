"""Shared test config: skip data-dependent tests when data/ is not populated.

The repo ships without the multi-GB data/ directory. Loaders in
pjm_engine.data raise FileNotFoundError when the raw CSVs (and parquet
cache) are absent; wrap every public loader so those tests skip instead
of erroring. Run the scripts/fetch_pjm_*.py fetchers to populate data/
and un-skip them.
"""

from __future__ import annotations

import functools

import pytest

import pjm_engine.data as _data

_LOADERS = [name for name in dir(_data) if name.startswith("load_")]


def _skip_on_missing_data(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except FileNotFoundError as exc:
            pytest.skip(f"market data not downloaded: {exc}")

    return wrapper


for _name in _LOADERS:
    setattr(_data, _name, _skip_on_missing_data(getattr(_data, _name)))
