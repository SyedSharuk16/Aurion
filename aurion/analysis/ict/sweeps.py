"""Liquidity sweeps. §6.4.

**A sweep is not a sweep until the reclaim closes.** This is the single most important
no-repaint point in the ICT layer: a penetration that has not yet reclaimed looks identical,
in real time, to a genuine breakout. Any definition that labels the penetration bar as a
sweep is using the future.

    penetration: first bar with  high > level + eps*ATR      (buy-side)
    reclaim:     first bar at or after it with  close < level - delta*ATR
    sweep:       both exist and  reclaim_index - penetration_index <= M
                 and no earlier bar in the span already closed back inside
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from aurion.analysis.base import Direction, Side
from aurion.analysis.ict.pools import LiquidityPool
from aurion.analysis.primitives import atr_series
from aurion.core.timeframe import TimeFrame
from aurion.data.types import Bar


@dataclass(frozen=True, slots=True)
class Sweep:
    instrument: str
    timeframe: TimeFrame
    side: Side
    direction: Direction
    pool_level: float
    pool_kind: str
    extremity: float
    depth_atr: float
    bars_to_reclaim: int
    event_time: datetime
    confirmed_at: datetime
    penetration_index: int
    reclaim_index: int

    @property
    def against(self) -> Direction:
        """The direction a sweep implies for the move that follows it.

        A buy-side sweep (above highs) is a failed up-move, so it implies DOWN; the entry
        model reads it as liquidity taken before a move the other way.
        """
        return Direction.DOWN if self.side is Side.BUY_SIDE else Direction.UP


def find_sweeps(
    bars: Sequence[Bar],
    pools: Sequence[LiquidityPool],
    min_penetration_atr: float,
    reclaim_buffer_atr: float,
    max_bars: int,
    atr_period: int = 14,
) -> list[Sweep]:
    """Completed sweeps of each pool, oldest first.

    Only bars that closed at or after a pool's confirmation are eligible: a pool cannot be
    swept before it exists.
    """
    atrs = atr_series(bars, atr_period)
    out: list[Sweep] = []

    for pool in pools:
        start = next(
            (i for i, b in enumerate(bars) if b.close_time > pool.confirmed_at), None
        )
        if start is None:
            continue
        buy_side = pool.side is Side.BUY_SIDE

        for i in range(start, len(bars)):
            atr_i = atrs[i]
            if atr_i is None or atr_i <= 0:
                continue
            eps = min_penetration_atr * atr_i
            penetrated = (
                bars[i].ohlc.h > pool.level + eps
                if buy_side
                else bars[i].ohlc.l < pool.level - eps
            )
            if not penetrated:
                continue

            reclaim_index: int | None = None
            for j in range(i, min(i + max_bars + 1, len(bars))):
                atr_j = atrs[j] or atr_i
                delta = reclaim_buffer_atr * atr_j
                reclaimed = (
                    bars[j].ohlc.c < pool.level - delta
                    if buy_side
                    else bars[j].ohlc.c > pool.level + delta
                )
                if reclaimed:
                    reclaim_index = j
                    break
            if reclaim_index is None:
                break  # penetrated and never reclaimed: a breakout, not a sweep

            span = bars[i : reclaim_index + 1]
            extremity = (
                max(b.ohlc.h for b in span) if buy_side else min(b.ohlc.l for b in span)
            )
            depth = (
                (extremity - pool.level) / atr_i
                if buy_side
                else (pool.level - extremity) / atr_i
            )
            out.append(
                Sweep(
                    instrument=bars[i].instrument,
                    timeframe=bars[i].timeframe,
                    side=pool.side,
                    direction=Direction.UP if buy_side else Direction.DOWN,
                    pool_level=pool.level,
                    pool_kind=str(pool.kind),
                    extremity=extremity,
                    depth_atr=depth,
                    bars_to_reclaim=reclaim_index - i,
                    event_time=bars[i].open_time,
                    # The reclaim close is what makes it a sweep.
                    confirmed_at=bars[reclaim_index].close_time,
                    penetration_index=i,
                    reclaim_index=reclaim_index,
                )
            )
            break  # one sweep per pool: after it, the pool is marked swept

    out.sort(key=lambda s: s.confirmed_at)
    return out


def recent_sweeps(
    sweeps: Sequence[Sweep], as_of: datetime, within_bars: int, bar_minutes: int
) -> list[Sweep]:
    span = within_bars * bar_minutes * 60
    return [
        s
        for s in sweeps
        if s.confirmed_at <= as_of and (as_of - s.confirmed_at).total_seconds() <= span
    ]
