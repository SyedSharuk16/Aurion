"""Displacement, BOS and CHoCH. `docs/02-definitions.md` §4.3, §4.4.

MSS is deliberately absent: it is a CHoCH preceded by a liquidity sweep, and sweeps live in
`ict`, so MSS is assembled in `ict/mss.py`. That keeps the module graph a DAG and matches
the definition's own hierarchy -- MSS is a strict subset of CHoCH.

The naming in the ICT source material is inconsistent (CHoCH and MSS are used
interchangeably by some authors, hierarchically by others). The hierarchy here is chosen so
the three are mutually distinguishable and individually testable:

* **BOS** -- break in the direction of the prevailing structure. Continuation.
* **CHoCH** -- the first break against it, or out of a range.
* **MSS** -- a CHoCH that followed a sweep. Strictly rarer by construction, which is the
  property that makes the distinction worth having.

``trend_before`` is evaluated from swings confirmed strictly before the breaking bar. A
single bar can be both a CHoCH and the start of a new regime; the event is labelled with
the *prior* state, because the alternative is self-referential.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from aurion.analysis.base import (
    BREAK_TOLERANCE_POINTS,
    BreakKind,
    Direction,
    SwingKind,
    TrendState,
)
from aurion.analysis.primitives import Imbalance, atr_series, three_candle_imbalance
from aurion.analysis.structure.swings import Swing
from aurion.analysis.structure.trend import trend_state
from aurion.core.timeframe import TimeFrame
from aurion.data.types import Bar


@dataclass(frozen=True, slots=True)
class Displacement:
    """A body of at least ``k x ATR`` that also leaves an imbalance (§4.3).

    The imbalance conjunct is from the brief and is kept. Its consequence is worth
    stating: a large body that does *not* gap, because the prior bar's range was wide, is
    not displacement under this definition. That is the intended ICT meaning and it
    excludes some large moves; `body OR gap` is a robustness variant, not the default.
    """

    instrument: str
    timeframe: TimeFrame
    event_time: datetime
    confirmed_at: datetime
    direction: Direction
    body: float
    atr: float
    body_atr: float
    imbalance: Imbalance


def find_displacements(
    bars: Sequence[Bar],
    k: float,
    atr_period: int = 14,
    atrs: Sequence[float | None] | None = None,
) -> dict[int, Displacement]:
    """Displacement bars keyed by index into ``bars``.

    The ATR used is the value at the displacement bar itself and is frozen there -- the
    object must not change as ATR evolves, or it would repaint.
    """
    # Shared across detectors when the caller already has it: recomputing the true-range
    # series in each of find_displacements / find_fvgs / find_sweeps was 10% of runtime.
    atrs = atr_series(bars, atr_period) if atrs is None else atrs
    out: dict[int, Displacement] = {}
    for i in range(1, len(bars) - 1):
        atr_i = atrs[i]
        if atr_i is None or atr_i <= 0:
            continue  # warm-up: no silent default
        bar = bars[i]
        body = bar.ohlc.body
        if body < k * atr_i:
            continue
        imbalance = three_candle_imbalance(bars[i - 1], bars[i + 1])
        if imbalance is None:
            continue
        direction = Direction.UP if bar.ohlc.is_up else Direction.DOWN
        if (direction is Direction.UP) != imbalance.bullish:
            # A bullish body that leaves a bearish gap is not a coherent displacement leg.
            continue
        out[i] = Displacement(
            instrument=bar.instrument,
            timeframe=bar.timeframe,
            event_time=bar.open_time,
            # The imbalance needs bar i+1 to close, so the displacement is not confirmed
            # until then -- not at the close of the big bar itself.
            confirmed_at=bars[i + 1].close_time,
            direction=direction,
            body=body,
            atr=atr_i,
            body_atr=body / atr_i,
            imbalance=imbalance,
        )
    return out


@dataclass(frozen=True, slots=True)
class StructuralBreak:
    instrument: str
    timeframe: TimeFrame
    kind: BreakKind
    direction: Direction
    event_time: datetime
    confirmed_at: datetime
    broken_level: float
    close: float
    trend_before: TrendState
    displacement: Displacement
    bar_index: int
    swing_event_time: datetime


def find_breaks(
    bars: Sequence[Bar],
    swings: Sequence[Swing],
    k: float,
    point: float,
    atr_period: int = 14,
    atrs: Sequence[float | None] | None = None,
) -> list[StructuralBreak]:
    """BOS and CHoCH events, oldest first.

    A break requires a close beyond the most recent *confirmed* swing on the relevant side
    plus displacement on the same bar. Using the confirmed swing, not the latest extreme,
    is what prevents a break being declared against a level that was not yet knowable.
    """
    displacements = find_displacements(bars, k, atr_period, atrs)
    tol = BREAK_TOLERANCE_POINTS * point
    out: list[StructuralBreak] = []
    last_structural: datetime | None = None

    for i, bar in enumerate(bars):
        disp = displacements.get(i)
        if disp is None:
            continue
        # Only swings confirmed strictly before this bar's close are knowable here.
        knowable = [
            s
            for s in swings
            if s.confirmed_at <= bar.open_time
            and (last_structural is None or s.event_time > last_structural)
        ]
        if not knowable:
            continue
        prior = trend_state(swings, bar.open_time)
        trend_before = prior.state if prior is not None else TrendState.RANGING

        if disp.direction is Direction.UP:
            candidates = [s for s in knowable if s.kind is SwingKind.HIGH]
            if not candidates:
                continue
            target = max(candidates, key=lambda s: s.event_time)
            if bar.ohlc.c <= target.price + tol:
                continue
            kind = BreakKind.BOS if trend_before is TrendState.BULLISH else BreakKind.CHOCH
        else:
            candidates = [s for s in knowable if s.kind is SwingKind.LOW]
            if not candidates:
                continue
            target = max(candidates, key=lambda s: s.event_time)
            if bar.ohlc.c >= target.price - tol:
                continue
            kind = BreakKind.BOS if trend_before is TrendState.BEARISH else BreakKind.CHOCH

        out.append(
            StructuralBreak(
                instrument=bar.instrument,
                timeframe=bar.timeframe,
                kind=kind,
                direction=disp.direction,
                event_time=bar.open_time,
                # Close-based, so it confirms on the breaking bar -- but the displacement
                # it requires confirms one bar later, so the break inherits that.
                confirmed_at=disp.confirmed_at,
                broken_level=target.price,
                close=bar.ohlc.c,
                trend_before=trend_before,
                displacement=disp,
                bar_index=i,
                swing_event_time=target.event_time,
            )
        )
        last_structural = bar.open_time
    return out


def last_break(
    breaks: Sequence[StructuralBreak], as_of: datetime
) -> StructuralBreak | None:
    eligible = [b for b in breaks if b.confirmed_at <= as_of]
    return max(eligible, key=lambda b: b.event_time) if eligible else None
