"""Bar repositories. The read side of the data layer, behind a protocol.

`MarketView` is constructed over a repository, so the backtest (in-memory replay) and the
live runtime (SQLite) differ only in which implementation is injected
(`docs/01-architecture.md` §4).

Every accessor takes ``as_of`` and every implementation must filter on it. The filtering
lives here rather than in `MarketView` so that a future SQL implementation can push the
predicate into the query without `MarketView` changing -- and so that there is no accessor
anywhere that returns an unfiltered series.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from aurion.core.timeframe import TimeFrame
from aurion.data.types import Bar, M1Candle, Quote


@runtime_checkable
class BarRepository(Protocol):
    def bars(
        self, instrument: str, timeframe: TimeFrame, n: int, as_of: datetime
    ) -> Sequence[Bar]:
        """The last ``n`` bars with ``close_time <= as_of``, oldest first."""
        ...

    def m1(self, instrument: str, n: int, as_of: datetime) -> Sequence[M1Candle]: ...

    def m1_between(
        self, instrument: str, start: datetime, end: datetime, as_of: datetime
    ) -> Sequence[M1Candle]: ...

    def last_quote(self, instrument: str, as_of: datetime) -> Quote | None: ...

    def instruments(self) -> Sequence[str]: ...


class InMemoryBarRepository:
    """Replay and test repository. Keeps series sorted and filters on ``as_of``.

    Appends must be in time order per series, which the resampler and the replay source
    both guarantee. The invariant is asserted rather than assumed, because an out-of-order
    append would make the ``as_of`` bisect return the wrong window -- a silent look-ahead.
    """

    __slots__ = ("_bars", "_m1", "_max_bars", "_max_m1", "_max_quotes", "_quotes")

    def __init__(
        self,
        max_bars_per_series: int | None = None,
        max_m1_per_instrument: int | None = None,
        max_quotes_per_instrument: int = 512,
    ) -> None:
        self._bars: dict[tuple[str, TimeFrame], list[Bar]] = defaultdict(list)
        self._m1: dict[str, list[M1Candle]] = defaultdict(list)
        self._quotes: dict[str, list[Quote]] = defaultdict(list)
        # Bounded retention makes this usable as the LIVE hot window, not just a test
        # double: analysis needs only a trailing window, SQLite holds durability, and an
        # unbounded dict would grow without limit on a 1-2 GB box. None means unbounded,
        # which is what the backtest wants.
        self._max_bars = max_bars_per_series
        self._max_m1 = max_m1_per_instrument
        self._max_quotes = max_quotes_per_instrument

    @staticmethod
    def _trim(series: list[object], cap: int | None) -> None:
        """Drop from the front. Only the tail is ever read, so this loses nothing."""
        if cap is not None and len(series) > cap:
            del series[: len(series) - cap]

    # -- writes ------------------------------------------------------------
    def add_bar(self, bar: Bar) -> None:
        series = self._bars[(bar.instrument, bar.timeframe)]
        if series and bar.open_time <= series[-1].open_time:
            raise ValueError(
                f"out-of-order bar append for {bar.instrument} {bar.timeframe}: "
                f"{bar.open_time} after {series[-1].open_time}"
            )
        series.append(bar)
        self._trim(series, self._max_bars)  # type: ignore[arg-type]

    def add_m1(self, candle: M1Candle) -> None:
        series = self._m1[candle.instrument]
        if series and candle.open_time <= series[-1].open_time:
            raise ValueError(
                f"out-of-order M1 append for {candle.instrument}: "
                f"{candle.open_time} after {series[-1].open_time}"
            )
        series.append(candle)
        self._trim(series, self._max_m1)  # type: ignore[arg-type]

    def add_quote(self, quote: Quote) -> None:
        series = self._quotes[quote.instrument]
        series.append(quote)
        self._trim(series, self._max_quotes)  # type: ignore[arg-type]

    def clear(self, instrument: str) -> None:
        """Drop one instrument's hot window so it can be rebuilt from the store.

        Needed because the series are append-only: a repaired historical gap cannot be
        inserted, only replayed.
        """
        for key in [k for k in self._bars if k[0] == instrument]:
            del self._bars[key]
        self._m1.pop(instrument, None)
        self._quotes.pop(instrument, None)

    # -- reads -------------------------------------------------------------
    def bars(
        self, instrument: str, timeframe: TimeFrame, n: int, as_of: datetime
    ) -> Sequence[Bar]:
        series = self._bars.get((instrument, timeframe), [])
        cut = bisect_right([b.close_time for b in series], as_of)
        window = series[max(cut - n, 0) : cut]
        return window

    def m1(self, instrument: str, n: int, as_of: datetime) -> Sequence[M1Candle]:
        series = self._m1.get(instrument, [])
        cut = bisect_right([c.close_time for c in series], as_of)
        return series[max(cut - n, 0) : cut]

    def m1_between(
        self, instrument: str, start: datetime, end: datetime, as_of: datetime
    ) -> Sequence[M1Candle]:
        series = self._m1.get(instrument, [])
        closes = [c.close_time for c in series]
        hi = bisect_right(closes, min(end, as_of))
        return [c for c in series[:hi] if c.open_time >= start]

    def last_quote(self, instrument: str, as_of: datetime) -> Quote | None:
        series = self._quotes.get(instrument, [])
        eligible = [q for q in series if q.time <= as_of]
        return eligible[-1] if eligible else None

    def instruments(self) -> Sequence[str]:
        return sorted({inst for inst, _ in self._bars} | set(self._m1))
