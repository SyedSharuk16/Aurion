"""Dealing range, premium/discount and the OTE band. §6.5.

Honest note on 0.62 / 0.79, repeated here because it is easy to lose: these are
Fibonacci-adjacent conventions with no derivation in market microstructure, and 0.62 is not
even the ratio it alludes to (0.618). They are frozen as `prior` after the Q13 re-derivation
and tested as a robustness variant rather than searched; if a fitted band ever drifts far
from them, that is a result worth reporting rather than a parameter to pin back.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from aurion.analysis.base import Direction, SwingKind
from aurion.analysis.structure.breaks import StructuralBreak
from aurion.analysis.structure.swings import Swing
from aurion.core.timeframe import TimeFrame
from aurion.data.types import Bar


@dataclass(frozen=True, slots=True)
class DealingRange:
    instrument: str
    timeframe: TimeFrame
    direction: Direction
    low: float
    high: float
    event_time: datetime
    confirmed_at: datetime

    @property
    def span(self) -> float:
        return self.high - self.low

    @property
    def equilibrium(self) -> float:
        return (self.high + self.low) / 2.0

    def position(self, price: float) -> float | None:
        """Where ``price`` sits in the range, 0 at the low and 1 at the high."""
        if self.span <= 0:
            return None
        return min(max((price - self.low) / self.span, 0.0), 1.0)

    def is_premium(self, price: float) -> bool | None:
        pos = self.position(price)
        return None if pos is None else pos > 0.5

    def retracement(self, price: float, direction: Direction) -> float | None:
        """How far price has retraced against ``direction``, 0 at the extreme.

        For a long in a bullish range, ``r = (high - price) / span``.
        """
        if self.span <= 0:
            return None
        r = (
            (self.high - price) / self.span
            if direction is Direction.UP
            else (price - self.low) / self.span
        )
        return min(max(r, 0.0), 1.0)

    def ote_band(self, direction: Direction, lo: float, hi: float) -> tuple[float, float]:
        """Price interval corresponding to retracement in ``[lo, hi]``."""
        if direction is Direction.UP:
            return (self.high - hi * self.span, self.high - lo * self.span)
        return (self.low + lo * self.span, self.low + hi * self.span)

    def in_ote(self, price: float, direction: Direction, lo: float, hi: float) -> bool:
        r = self.retracement(price, direction)
        return r is not None and lo <= r <= hi


def dealing_range(
    bars: Sequence[Bar],
    swings: Sequence[Swing],
    last_break: StructuralBreak,
    as_of: datetime,
) -> DealingRange | None:
    """The range established by the most recent structural event.

    For an up break: the low is the swing low immediately preceding the displacement leg,
    and the high is the highest high since the break -- a running value, recomputed from
    closed bars only, so each version carries its own ``confirmed_at``.
    """
    eligible = [b for b in bars if b.close_time <= as_of]
    since = [b for b in eligible if b.open_time >= last_break.event_time]
    if not since:
        return None

    if last_break.direction is Direction.UP:
        lows = [
            s
            for s in swings
            if s.kind is SwingKind.LOW
            and s.event_time < last_break.event_time
            and s.confirmed_at <= as_of
        ]
        if not lows:
            return None
        low = max(lows, key=lambda s: s.event_time).price
        high = max(b.ohlc.h for b in since)
    else:
        highs = [
            s
            for s in swings
            if s.kind is SwingKind.HIGH
            and s.event_time < last_break.event_time
            and s.confirmed_at <= as_of
        ]
        if not highs:
            return None
        high = max(highs, key=lambda s: s.event_time).price
        low = min(b.ohlc.l for b in since)

    if high <= low:
        return None
    return DealingRange(
        instrument=last_break.instrument,
        timeframe=last_break.timeframe,
        direction=last_break.direction,
        low=low,
        high=high,
        event_time=last_break.event_time,
        confirmed_at=max(last_break.confirmed_at, since[-1].close_time),
    )
