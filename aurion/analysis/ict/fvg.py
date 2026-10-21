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

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

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


def _tracked(fvg: FVG, later: Sequence[Bar], point: float) -> FVG:
    """Apply fill and inversion over the bars that closed after confirmation."""
    if not later:
        return fvg
    size = fvg.size
    if size <= 0:
        return fvg
    tol = 0.5 * point

    if fvg.direction is Direction.UP:
        deepest = min(b.ohlc.l for b in later)
        fill = (fvg.hi - deepest) / size
        inverted_bar = next((b for b in later if b.ohlc.c < fvg.lo - tol), None)
    else:
        highest = max(b.ohlc.h for b in later)
        fill = (highest - fvg.lo) / size
        inverted_bar = next((b for b in later if b.ohlc.c > fvg.hi + tol), None)

    fill = min(max(fill, 0.0), 1.0)
    if fill >= 1.0:
        state = ArrayState.MITIGATED
    elif fill >= 0.5:
        state = ArrayState.CE_TRADED
    elif fill > 0.0:
        state = ArrayState.TOUCHED
    else:
        state = ArrayState.UNTOUCHED

    mitigated_at = None
    if state is ArrayState.MITIGATED:
        for b in later:
            reached = b.ohlc.l <= fvg.lo if fvg.direction is Direction.UP else b.ohlc.h >= fvg.hi
            if reached:
                mitigated_at = b.close_time
                break

    if inverted_bar is not None:
        return replace(
            fvg,
            state=ArrayState.INVERTED,
            fill_frac=fill,
            mitigated_at=mitigated_at,
            inverted_at=inverted_bar.close_time,
        )
    return replace(fvg, state=state, fill_frac=fill, mitigated_at=mitigated_at)


def find_fvgs(
    bars: Sequence[Bar],
    min_atr: float,
    point: float,
    atr_period: int = 14,
    track: bool = True,
) -> list[FVG]:
    """All FVGs in ``bars``, with fill state as of the last bar.

    ``min_atr`` is the admission threshold ``g``: the gap must span at least ``g x ATR`` at
    its own confirmation bar, with the ATR frozen there.
    """
    atrs = atr_series(bars, atr_period)
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
        fvg = FVG(
            instrument=bars[i].instrument,
            timeframe=bars[i].timeframe,
            direction=Direction.UP if imbalance.bullish else Direction.DOWN,
            lo=imbalance.lo,
            hi=imbalance.hi,
            size_atr=imbalance.size / atr_i,
            event_time=bars[i].open_time,
            confirmed_at=bars[i + 1].close_time,
            middle_index=i,
        )
        if track:
            fvg = _tracked(fvg, bars[i + 2 :], point)
        out.append(fvg)
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


def find_bprs(fvgs: Sequence[FVG], window_bars: int, bar_minutes: int) -> list[BalancedPriceRange]:
    """Intersections between opposite-polarity FVGs confirmed close together in time."""
    bullish = [f for f in fvgs if f.direction is Direction.UP]
    bearish = [f for f in fvgs if f.direction is Direction.DOWN]
    span_seconds = window_bars * bar_minutes * 60
    out: list[BalancedPriceRange] = []
    for b in bullish:
        for s in bearish:
            if abs((b.confirmed_at - s.confirmed_at).total_seconds()) > span_seconds:
                continue
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
