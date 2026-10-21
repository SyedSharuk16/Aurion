"""Shared types for derived objects.

Every derived object carries ``(event_time, confirmed_at)``. `MarketView` filters on
``confirmed_at``, which is what makes no-repaint a property of the type boundary rather
than of discipline (`docs/01-architecture.md` §3).

Module dependency order inside `analysis/` is a DAG and must stay one:

    primitives -> structure -> ict -> {sr, quarterly, mmxm, regime} -> features

Two places where the obvious import would make it a cycle, and how they are resolved:

* Displacement (§4.3) requires a fair value gap, but FVG objects live in `ict`. The
  three-candle imbalance *primitive* therefore lives in `primitives.py`, and `structure`
  uses the primitive while `ict` builds the tracked object on top of it.
* MSS (§4.4) is a CHoCH preceded by a liquidity sweep, but sweeps live in `ict`. So
  `structure` detects BOS and CHoCH, and `ict.mss` adds the sweep conjunct. That matches
  the definition's own hierarchy: MSS is a strict subset of CHoCH.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum

from aurion.core.timeframe import TimeFrame


class Direction(IntEnum):
    """Signed direction. Integer-valued so it can multiply prices and scores directly."""

    UP = 1
    DOWN = -1

    @property
    def opposite(self) -> Direction:
        return Direction.DOWN if self is Direction.UP else Direction.UP

    @property
    def label(self) -> str:
        return "bullish" if self is Direction.UP else "bearish"


class Side(StrEnum):
    """Which side of the book a liquidity pool represents.

    Stops of shorts rest ABOVE highs, so a cluster of highs is buy-side liquidity.
    """

    BUY_SIDE = "buy_side"
    SELL_SIDE = "sell_side"


class SwingKind(StrEnum):
    HIGH = "high"
    LOW = "low"


class TrendState(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    RANGING = "ranging"


class BreakKind(StrEnum):
    BOS = "bos"
    CHOCH = "choch"
    MSS = "mss"


class ArrayState(StrEnum):
    """Mitigation state of a price-delivery array (§5.1)."""

    UNTOUCHED = "untouched"
    TOUCHED = "touched"
    CE_TRADED = "ce_traded"
    MITIGATED = "mitigated"
    INVALIDATED = "invalidated"
    INVERTED = "inverted"


@dataclass(frozen=True, slots=True)
class DerivedObject:
    """Common header. Subclasses add their own geometry."""

    instrument: str
    timeframe: TimeFrame
    event_time: datetime
    confirmed_at: datetime

    def __post_init__(self) -> None:
        if self.confirmed_at < self.event_time:
            raise ValueError(
                f"confirmed_at {self.confirmed_at} precedes event_time {self.event_time}: "
                "an object cannot be visible before it happened"
            )


#: Price tolerance for "beyond" comparisons, in points (§4.4).
BREAK_TOLERANCE_POINTS = 0.5
