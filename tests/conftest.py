from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from aurion.config.models import AurionConfig, load_config
from aurion.core.timeframe import UTC, TimeFrame
from aurion.data.types import OHLC, Bar, M1Candle


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


def bars_from(
    rows: list[tuple[float, float, float, float]],
    instrument: str = "TEST",
    timeframe: TimeFrame = TimeFrame.M5,
    start: datetime | None = None,
    volume: int = 100,
) -> list[Bar]:
    """Bars from ``(open, close, upper_wick, lower_wick)``.

    Deliberately not ``(o, h, l, c)``: hand-written OHLC tuples kept violating
    ``high >= max(o, c)``, which OHLC's validator rejects. Deriving the extremes from wick
    sizes makes an invalid fixture impossible to write.
    """
    t0 = start or datetime(2024, 6, 12, 9, 0, tzinfo=UTC)
    step = timeframe.duration
    out: list[Bar] = []
    for i, (o, c, up, dn) in enumerate(rows):
        out.append(
            Bar(
                instrument=instrument,
                timeframe=timeframe,
                open_time=t0 + i * step,
                close_time=t0 + (i + 1) * step,
                ohlc=OHLC(o, max(o, c) + up, min(o, c) - dn, c),
                tick_volume=volume,
                constituents=timeframe.minutes,
            )
        )
    return out


def zigzag(
    deltas: list[float],
    start_price: float = 100.0,
    mirror: bool = False,
    instrument: str = "TEST",
    timeframe: TimeFrame = TimeFrame.M5,
    start: datetime | None = None,
) -> list[Bar]:
    """Deterministic zigzag with varied wicks.

    The wicks must vary: with equal wicks, every reversal bar's extreme ties its
    neighbour's (open == prior close) and the strict-inequality swing definition suppresses
    every fractal. ``mirror`` swaps the wick formulas as well, since the asymmetry between
    them would otherwise break a mirrored pattern.
    """
    rows: list[tuple[float, float, float, float]] = []
    level = start_price
    for i, d in enumerate(deltas):
        o, c = level, level + d
        a = 0.10 + 0.05 * ((i * 7) % 5)
        b = 0.10 + 0.05 * ((i * 3) % 4)
        rows.append((o, c, b, a) if mirror else (o, c, a, b))
        level = c
    return bars_from(rows, instrument, timeframe, start)


@pytest.fixture
def bar_factory():
    return bars_from


@pytest.fixture
def zigzag_factory():
    return zigzag
