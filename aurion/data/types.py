"""Market data value types.

Price basis follows `docs/02-definitions.md` §0.3: bid and ask are stored, mid is derived.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aurion.core.timeframe import TimeFrame


@dataclass(frozen=True, slots=True)
class OHLC:
    o: float
    h: float
    l: float  # noqa: E741 - `l` matches the standard OHLC notation used throughout the docs
    c: float

    def __post_init__(self) -> None:
        if not (self.l <= self.o <= self.h and self.l <= self.c <= self.h):
            raise ValueError(f"inconsistent OHLC: o={self.o} h={self.h} l={self.l} c={self.c}")

    @property
    def body(self) -> float:
        return abs(self.c - self.o)

    @property
    def range(self) -> float:
        return self.h - self.l

    @property
    def upper_wick(self) -> float:
        return self.h - max(self.o, self.c)

    @property
    def lower_wick(self) -> float:
        return min(self.o, self.c) - self.l

    @property
    def is_up(self) -> bool:
        return self.c > self.o

    @property
    def is_down(self) -> bool:
        return self.c < self.o


def mid_of(bid: OHLC, ask: OHLC) -> OHLC:
    """Field-wise midpoint of bid and ask OHLC.

    Honest limitation: the true mid high within a bar is not generally
    ``(bid_high + ask_high) / 2`` unless the spread is constant across the bar, because the
    bid high and the ask high can occur at different instants. With OHLC input rather than
    ticks this is the best available estimate, and the error is bounded by the intra-bar
    spread variation. It is recorded here because the alternative -- quietly treating it as
    exact -- would hide a real approximation in the one place every downstream definition
    depends on.
    """
    return OHLC(
        o=(bid.o + ask.o) / 2.0,
        h=(bid.h + ask.h) / 2.0,
        l=(bid.l + ask.l) / 2.0,
        c=(bid.c + ask.c) / 2.0,
    )


@dataclass(frozen=True, slots=True)
class M1Candle:
    """A base-timeframe candle. Incomplete candles are never constructed (see adapters)."""

    instrument: str
    open_time: datetime
    close_time: datetime
    bid: OHLC
    ask: OHLC
    tick_volume: int

    @property
    def mid(self) -> OHLC:
        return mid_of(self.bid, self.ask)

    @property
    def spread_close(self) -> float:
        return self.ask.c - self.bid.c

    @property
    def confirmed_at(self) -> datetime:
        """A bar is never visible before its close. `docs/02-definitions.md` §0.2."""
        return self.close_time


@dataclass(frozen=True, slots=True)
class Bar:
    """A resampled bar on mid prices.

    Resampled bars carry mid only: analysis runs on mid (§0.3) and all triggering and fill
    resolution happens on M1, where bid and ask are retained. Storing bid/ask at every
    timeframe would double the cache for data no code path reads.
    """

    instrument: str
    timeframe: TimeFrame
    open_time: datetime
    close_time: datetime
    ohlc: OHLC
    tick_volume: int
    constituents: int
    incomplete: bool = False
    dst_irregular: bool = False

    @property
    def confirmed_at(self) -> datetime:
        return self.close_time

    @property
    def usable(self) -> bool:
        """Incomplete bars are excluded from analysis, including from ATR (§2.2 rule 2)."""
        return not self.incomplete


@dataclass(frozen=True, slots=True)
class Quote:
    instrument: str
    time: datetime
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class Heartbeat:
    time: datetime
