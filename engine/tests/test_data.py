"""Non-leakage probe — the single most important test in the engine.

If this fails, the engine's adversarial-precision guarantee is broken and no
backtest result can be trusted.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from pjm_engine.data import load_da_hrl_lmps, view_as_of
from pjm_engine.errors import DataNotAvailableError

UTC = ZoneInfo("UTC")


@pytest.fixture(scope="module")
def da_lmps() -> pd.DataFrame:
    return load_da_hrl_lmps()


def test_published_at_present_and_tz_aware(da_lmps):
    assert "published_at" in da_lmps.columns
    assert da_lmps["published_at"].dt.tz is not None


def test_view_as_of_rejects_naive_datetime(da_lmps):
    naive = datetime(2025, 11, 1, 12, 0)
    with pytest.raises(DataNotAvailableError):
        view_as_of(da_lmps, naive)


def test_view_as_of_filters_published_at(da_lmps):
    """The core non-leakage probe: at random as_of values, no served row may
    have published_at > as_of."""
    rng = random.Random(42)
    pubs = da_lmps["published_at"]
    for _ in range(50):
        idx = rng.randrange(len(pubs))
        as_of = pubs.iloc[idx]
        view = view_as_of(da_lmps, as_of)
        assert (view["published_at"] <= as_of).all()


def test_da_lmp_published_d_minus_1_at_1330_ept(da_lmps):
    """An operating-day-D row should have published_at = D-1 13:30 EPT."""
    sample = da_lmps.iloc[0]
    op_date_ept = sample["datetime_beginning_ept"].date()
    pub_ept = sample["published_at"].astimezone(ZoneInfo("America/New_York"))
    assert pub_ept.date() == op_date_ept - timedelta(days=1)
    assert pub_ept.hour == 13
    assert pub_ept.minute == 30
