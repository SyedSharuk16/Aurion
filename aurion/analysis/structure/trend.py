"""Structural trend state. `docs/02-definitions.md` §4.2."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from aurion.analysis.base import SwingKind, TrendState
from aurion.analysis.structure.swings import Swing


@dataclass(frozen=True, slots=True)
class Structure:
    state: TrendState
    confirmed_at: datetime
    higher_high: bool
    higher_low: bool
    lower_high: bool
    lower_low: bool
    last_high: float
    last_low: float


def trend_state(swings: Sequence[Swing], as_of: datetime) -> Structure | None:
    """HH+HL is bullish, LH+LL is bearish, anything else is ranging.

    Needs two confirmed highs and two confirmed lows; returns ``None`` otherwise rather
    than guessing from one leg.
    """
    highs = sorted(
        (s for s in swings if s.kind is SwingKind.HIGH and s.confirmed_at <= as_of),
        key=lambda s: s.event_time,
    )
    lows = sorted(
        (s for s in swings if s.kind is SwingKind.LOW and s.confirmed_at <= as_of),
        key=lambda s: s.event_time,
    )
    if len(highs) < 2 or len(lows) < 2:
        return None

    hh = highs[-1].price > highs[-2].price
    lh = highs[-1].price < highs[-2].price
    hl = lows[-1].price > lows[-2].price
    ll = lows[-1].price < lows[-2].price

    if hh and hl:
        state = TrendState.BULLISH
    elif lh and ll:
        state = TrendState.BEARISH
    else:
        state = TrendState.RANGING

    return Structure(
        state=state,
        confirmed_at=max(highs[-1].confirmed_at, lows[-1].confirmed_at),
        higher_high=hh,
        higher_low=hl,
        lower_high=lh,
        lower_low=ll,
        last_high=highs[-1].price,
        last_low=lows[-1].price,
    )
