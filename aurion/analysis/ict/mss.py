"""Market structure shift. §4.4.

MSS is assembled here rather than in `structure` because it needs sweeps:

    MSS = CHoCH  AND  a sweep of the opposite side completed within M bars before it

So MSS is a strict subset of CHoCH and is therefore rarer by construction, which is the
property that makes the BOS / CHoCH / MSS distinction worth maintaining. It is the
entry-grade event for the primary model.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from aurion.analysis.base import BreakKind, Direction
from aurion.analysis.ict.sweeps import Sweep
from aurion.analysis.structure.breaks import StructuralBreak
from aurion.core.timeframe import TimeFrame


@dataclass(frozen=True, slots=True)
class MarketStructureShift:
    instrument: str
    timeframe: TimeFrame
    direction: Direction
    event_time: datetime
    confirmed_at: datetime
    choch: StructuralBreak
    sweep: Sweep
    bars_since_sweep: int

    @property
    def kind(self) -> BreakKind:
        return BreakKind.MSS


def find_mss(
    breaks: Sequence[StructuralBreak],
    sweeps: Sequence[Sweep],
    window_bars: int,
) -> list[MarketStructureShift]:
    """Every CHoCH preceded by an opposite-side sweep within ``window_bars``.

    "Opposite side" means a sweep whose own implied direction agrees with the break: a
    buy-side sweep (stops above highs taken, then reclaimed) precedes a bearish MSS.
    """
    out: list[MarketStructureShift] = []
    for brk in breaks:
        if brk.kind is not BreakKind.CHOCH:
            continue
        candidates = [
            s
            for s in sweeps
            if s.against is brk.direction
            and s.reclaim_index <= brk.bar_index
            and brk.bar_index - s.reclaim_index <= window_bars
            # The sweep must be knowable before the break confirms.
            and s.confirmed_at <= brk.confirmed_at
        ]
        if not candidates:
            continue
        sweep = max(candidates, key=lambda s: s.reclaim_index)
        out.append(
            MarketStructureShift(
                instrument=brk.instrument,
                timeframe=brk.timeframe,
                direction=brk.direction,
                event_time=brk.event_time,
                confirmed_at=brk.confirmed_at,
                choch=brk,
                sweep=sweep,
                bars_since_sweep=brk.bar_index - sweep.reclaim_index,
            )
        )
    return out
