from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from aurion.config.models import AurionConfig, load_config
from aurion.core.timeframe import UTC
from aurion.data.types import OHLC, M1Candle


@pytest.fixture(scope="session")
def cfg() -> AurionConfig:
    return load_config("config.yaml")


def make_m1(
    instrument: str,
    open_time: datetime,
    mid_open: float,
    *,
    high_off: float = 0.0004,
    low_off: float = 0.0003,
    close_off: float = 0.0001,
    spread: float = 0.00002,
    volume: int = 10,
) -> M1Candle:
    """Build one M1 candle from a mid level, with a symmetric spread.

    Used by every test that needs price action: constructing bid/ask explicitly in each
    test would make the tests about OHLC bookkeeping rather than about the unit under test.
    """
    half = spread / 2.0
    mo, mh, ml, mc = (
        mid_open,
        mid_open + high_off,
        mid_open - low_off,
        mid_open + close_off,
    )
    bid = OHLC(mo - half, mh - half, ml - half, mc - half)
    ask = OHLC(mo + half, mh + half, ml + half, mc + half)
    return M1Candle(
        instrument=instrument,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=1),
        bid=bid,
        ask=ask,
        tick_volume=volume,
    )


@pytest.fixture
def m1_factory():
    return make_m1


@pytest.fixture
def ramp_series():
    """Deterministic rising M1 series: 120 minutes from 2024-06-12 09:00 UTC."""

    def _build(instrument: str = "EURUSD", n: int = 120, start: datetime | None = None):
        t0 = start or datetime(2024, 6, 12, 9, 0, tzinfo=UTC)
        return [
            make_m1(instrument, t0 + timedelta(minutes=i), 1.1000 + i * 0.0001)
            for i in range(n)
        ]

    return _build
