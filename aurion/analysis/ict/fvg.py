"""Fair value gaps, inverse FVGs and balanced price ranges. §5.1, §5.2, §5.6.

Fill tracking is the part that is easy to get subtly wrong, so the states are explicit:

    UNTOUCHED (fill 0) -> TOUCHED (0 < fill < 0.5) -> CE_TRADED (0.5 <= fill < 1)
    -> MITIGATED (fill >= 1)

A mitigated gap is **retained**, not deleted: "price is at a previously mitigated FVG" is a
feature. It is excluded from "available PD array" queries instead.

Mitigation and inversion use different triggers on purpose. A wick through the gap fills
it; a *close* beyond it inverts its polarity. Using one trigger for both would make the two
states indistinguishable.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from aurion.analysis.base import ArrayState, Direction
from aurion.analysis.primitives import atr_series, three_candle_imbalance
from aurion.core.timeframe import TimeFrame
from aurion.data.types import Bar


@dataclass(frozen=True, slots=True)
class FVG:
    instrument: str
    timeframe: TimeFrame
    direction: Direction
    lo: float
    hi: float
    size_atr: float
    event_time: datetime
    confirmed_at: datetime
    state: ArrayState = ArrayState.UNTOUCHED
    fill_frac: float = 0.0
    mitigated_at: datetime | None = None
    inverted_at: datetime | None = None
    middle_index: int = -1

    @property
    def size(self) -> float:
        return self.hi - self.lo

    @property
    def consequent_encroachment(self) -> float:
        return (self.hi + self.lo) / 2.0

    @property
    def available(self) -> bool:
        """Usable as a PD array: not mitigated, not inverted."""
        return self.state not in (ArrayState.MITIGATED, ArrayState.INVERTED)

    @property
    def effective_direction(self) -> Direction:
        """An inverted FVG trades as the opposite polarity (§5.2)."""
        return self.direction.opposite if self.state is ArrayState.INVERTED else self.direction

    def proximal_edge(self) -> float:
        """The edge price reaches first when retracing into the gap."""
        return self.hi if self.direction is Direction.UP else self.lo

    def contains(self, price: float) -> bool:
        return self.lo <= price <= self.hi


@dataclass(frozen=True, slots=True)
class _SuffixExtrema:
    """Suffix minima and maxima of a bar window, computed once per call.

    Fill tracking is otherwise O(gaps x bars): each FVG re-scans every later bar, which
    profiling showed at 88,485 calls and a third of total runtime. With suffix arrays the
    deepest retrace behind any gap is an O(1) lookup, and the two quantities that genuinely
    need a position (``mitigated_at`` and the first inverting bar) are scanned only when
    the suffix bound proves one exists.
    """

    min_low: tuple[float, ...]
    max_high: tuple[float, ...]
    min_close: tuple[float, ...]
    max_close: tuple[float, ...]

    @classmethod
    def of(cls, bars: Sequence[Bar]) -> _SuffixExtrema:
        n = len(bars)
        inf = float("inf")
        min_low = [inf] * (n + 1)
        max_high = [-inf] * (n + 1)
        min_close = [inf] * (n + 1)
        max_close = [-inf] * (n + 1)
        for i in range(n - 1, -1, -1):
            o = bars[i].ohlc
            min_low[i] = min(min_low[i + 1], o.l)
            max_high[i] = max(max_high[i + 1], o.h)
            min_close[i] = min(min_close[i + 1], o.c)
            max_close[i] = max(max_close[i + 1], o.c)
        return cls(tuple(min_low), tuple(max_high), tuple(min_close), tuple(max_close))


@dataclass(frozen=True, slots=True)
class _Tracked:
    state: ArrayState
    fill_frac: float
    mitigated_at: datetime | None
    inverted_at: datetime | None


def _track(
    direction: Direction,
    lo: float,
    hi: float,
    bars: Sequence[Bar],
    start: int,
    suffix: _SuffixExtrema,
    point: float,
) -> _Tracked:
    """Fill and inversion state from the bars at ``start`` onward.

    Returns the state rather than a rebuilt FVG so the caller can construct the object
    once. Going through ``dataclasses.replace`` meant two constructions per gap, and with
    ~88,000 gap-trackings per backtest week that was a fifth of total runtime.
    """
    size = hi - lo
    if start >= len(bars) or size <= 0:
        return _Tracked(ArrayState.UNTOUCHED, 0.0, None, None)
    tol = 0.5 * point
    bullish = direction is Direction.UP

    if bullish:
        fill = (hi - suffix.min_low[start]) / size
        may_invert = suffix.min_close[start] < lo - tol
    else:
        fill = (suffix.max_high[start] - lo) / size
        may_invert = suffix.max_close[start] > hi + tol
    fill = min(max(fill, 0.0), 1.0)

    state = (
        ArrayState.MITIGATED
        if fill >= 1.0
        else ArrayState.CE_TRADED
        if fill >= 0.5
        else ArrayState.TOUCHED
        if fill > 0.0
        else ArrayState.UNTOUCHED
    )

    mitigated_at: datetime | None = None
    if state is ArrayState.MITIGATED:
        for b in bars[start:]:
            if (b.ohlc.l <= lo) if bullish else (b.ohlc.h >= hi):
                mitigated_at = b.close_time
                break

    inverted_at: datetime | None = None
    if may_invert:
        for b in bars[start:]:
            if (b.ohlc.c < lo - tol) if bullish else (b.ohlc.c > hi + tol):
                inverted_at = b.close_time
                break
    if inverted_at is not None:
        return _Tracked(ArrayState.INVERTED, fill, mitigated_at, inverted_at)
    return _Tracked(state, fill, mitigated_at, None)


def find_fvgs(
    bars: Sequence[Bar],
    min_atr: float,
    point: float,
    atr_period: int = 14,
    track: bool = True,
    atrs: Sequence[float | None] | None = None,
) -> list[FVG]:
    """All FVGs in ``bars``, with fill state as of the last bar.

    ``min_atr`` is the admission threshold ``g``: the gap must span at least ``g x ATR`` at
    its own confirmation bar, with the ATR frozen there.
    """
    atrs = atr_series(bars, atr_period) if atrs is None else atrs
    suffix = _SuffixExtrema.of(bars) if track else None
    out: list[FVG] = []
    for i in range(1, len(bars) - 1):
        atr_i = atrs[i]
        if atr_i is None or atr_i <= 0:
            continue
        imbalance = three_candle_imbalance(bars[i - 1], bars[i + 1])
        if imbalance is None:
            continue
        if imbalance.size < min_atr * atr_i:
            continue
        direction = Direction.UP if imbalance.bullish else Direction.DOWN
        tracked = (
            _track(direction, imbalance.lo, imbalance.hi, bars, i + 2, suffix, point)
            if track and suffix is not None
            else _Tracked(ArrayState.UNTOUCHED, 0.0, None, None)
        )
        out.append(
            FVG(
                instrument=bars[i].instrument,
                timeframe=bars[i].timeframe,
                direction=direction,
                lo=imbalance.lo,
                hi=imbalance.hi,
                size_atr=imbalance.size / atr_i,
                event_time=bars[i].open_time,
                confirmed_at=bars[i + 1].close_time,
                state=tracked.state,
                fill_frac=tracked.fill_frac,
                mitigated_at=tracked.mitigated_at,
                inverted_at=tracked.inverted_at,
                middle_index=i,
            )
        )
    return out


def available_fvgs(
    fvgs: Sequence[FVG], as_of: datetime, direction: Direction | None = None
) -> list[FVG]:
    """Confirmed, unmitigated, optionally filtered to one effective polarity."""
    out = [f for f in fvgs if f.confirmed_at <= as_of and f.available]
    if direction is not None:
        out = [f for f in out if f.effective_direction is direction]
    return out


@dataclass(frozen=True, slots=True)
class BalancedPriceRange:
    """Overlap of a bullish and a bearish FVG formed within ``window`` bars (§5.6).

    Polarity is bidirectional by construction. The signal engine treats a BPR as support in
    a bullish dealing range and resistance in a bearish one, which is a heuristic choice
    and is recorded as one.
    """

    instrument: str
    timeframe: TimeFrame
    lo: float
    hi: float
    event_time: datetime
    confirmed_at: datetime
    bullish_event: datetime
    bearish_event: datetime

    @property
    def size(self) -> float:
        return self.hi - self.lo


def find_bprs(
    fvgs: Sequence[FVG], window_bars: int, bar_minutes: int
) -> list[BalancedPriceRange]:
    """Intersections between opposite-polarity FVGs confirmed close together in time.

    The window is bisected rather than scanned: the naive pairing is O(bullish x bearish)
    with a timedelta conversion per pair, which profiling showed as a quarter of total
    runtime. Sorting the bearish gaps once and taking the time slice gives the same pairs.
    """
    bullish = [f for f in fvgs if f.direction is Direction.UP]
    bearish = sorted(
        (f for f in fvgs if f.direction is Direction.DOWN), key=lambda f: f.confirmed_at
    )
    bearish_times = [f.confirmed_at for f in bearish]
    span = timedelta(minutes=window_bars * bar_minutes)
    out: list[BalancedPriceRange] = []
    for b in bullish:
        left = bisect_left(bearish_times, b.confirmed_at - span)
        right = bisect_right(bearish_times, b.confirmed_at + span)
        for s in bearish[left:right]:
            lo = max(b.lo, s.lo)
            hi = min(b.hi, s.hi)
            if hi <= lo:
                continue
            out.append(
                BalancedPriceRange(
                    instrument=b.instrument,
                    timeframe=b.timeframe,
                    lo=lo,
                    hi=hi,
                    event_time=max(b.event_time, s.event_time),
                    confirmed_at=max(b.confirmed_at, s.confirmed_at),
                    bullish_event=b.event_time,
                    bearish_event=s.event_time,
                )
            )
    out.sort(key=lambda r: r.confirmed_at)
    return out
