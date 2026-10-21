"""Fractal swing points. `docs/02-definitions.md` §4.1.

Bar ``i`` is a swing high with half-width ``N`` iff its high strictly exceeds the highs of
the ``N`` bars on each side. **Confirmed at the close of bar ``i+N``** -- the swing is
invisible before then, which is the no-repaint mechanism.

Strict inequalities on both sides are deliberate. An equal-high plateau yields no swing,
which looks like an omission and is not: equal highs are the defining feature of a
liquidity pool (§6.1) and are captured there. A plateau that was both "a swing" and "equal
highs" would double-count the same price action in the feature vector.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from aurion.analysis.base import SwingKind
from aurion.core.timeframe import TimeFrame
from aurion.data.types import Bar


@dataclass(frozen=True, slots=True)
class Swing:
    instrument: str
    timeframe: TimeFrame
    kind: SwingKind
    event_time: datetime
    price: float
    half_width: int
    confirmed_at: datetime
    bar_index: int

    @property
    def is_high(self) -> bool:
        return self.kind is SwingKind.HIGH


def find_swings(bars: Sequence[Bar], half_width: int) -> list[Swing]:
    """All confirmed swings in ``bars``, oldest first.

    ``bars`` must be closed, usable bars in time order -- the output of
    ``MarketView.usable_bars``. Only indices with ``half_width`` bars on each side can be
    evaluated, so the first and last ``half_width`` bars never produce a swing from this
    window; they may once more bars close.
    """
    if half_width < 1:
        raise ValueError("half_width must be >= 1")
    out: list[Swing] = []
    n = len(bars)
    for i in range(half_width, n - half_width):
        bar = bars[i]
        left = bars[i - half_width : i]
        right = bars[i + 1 : i + 1 + half_width]
        confirmed_at = bars[i + half_width].close_time

        if all(bar.ohlc.h > b.ohlc.h for b in left) and all(
            bar.ohlc.h > b.ohlc.h for b in right
        ):
            out.append(
                Swing(
                    instrument=bar.instrument,
                    timeframe=bar.timeframe,
                    kind=SwingKind.HIGH,
                    event_time=bar.open_time,
                    price=bar.ohlc.h,
                    half_width=half_width,
                    confirmed_at=confirmed_at,
                    bar_index=i,
                )
            )
        if all(bar.ohlc.l < b.ohlc.l for b in left) and all(
            bar.ohlc.l < b.ohlc.l for b in right
        ):
            out.append(
                Swing(
                    instrument=bar.instrument,
                    timeframe=bar.timeframe,
                    kind=SwingKind.LOW,
                    event_time=bar.open_time,
                    price=bar.ohlc.l,
                    half_width=half_width,
                    confirmed_at=confirmed_at,
                    bar_index=i,
                )
            )
    out.sort(key=lambda s: (s.confirmed_at, s.event_time, s.kind))
    return out


def visible_swings(swings: Sequence[Swing], as_of: datetime) -> list[Swing]:
    """Swings confirmed at or before ``as_of``. The filter MarketView also applies."""
    return [s for s in swings if s.confirmed_at <= as_of]


def last_swing(swings: Sequence[Swing], kind: SwingKind, as_of: datetime) -> Swing | None:
    """Most recent confirmed swing of one kind, by event time."""
    eligible = [s for s in swings if s.kind is kind and s.confirmed_at <= as_of]
    return max(eligible, key=lambda s: s.event_time) if eligible else None
