"""Order blocks and breakers. §5.3, §5.4.

An order block does not exist until the break it caused is confirmed, which is why one can
never be drawn in advance. The invalidation trigger is close-based, consistent with the
inverse-FVG trigger in §5.2.

Mitigation blocks (§5.5) are **not implemented**: dropped at review (Q6) because a module
whose affected fraction is small needs a per-affected-trade effect of roughly 1R to be
resolvable at the projected sample size, which is not plausible for a variant order block.
The definition is retained in the docs for the record.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from aurion.analysis.base import ArrayState, BreakKind, Direction
from aurion.analysis.structure.breaks import StructuralBreak
from aurion.core.timeframe import TimeFrame
from aurion.data.types import Bar


@dataclass(frozen=True, slots=True)
class OrderBlock:
    instrument: str
    timeframe: TimeFrame
    direction: Direction
    lo: float
    hi: float
    event_time: datetime
    confirmed_at: datetime
    caused_break: BreakKind
    bars_to_break: int
    state: ArrayState = ArrayState.UNTOUCHED
    fill_frac: float = 0.0
    invalidated_at: datetime | None = None
    breaker_at: datetime | None = None
    origin_index: int = -1

    @property
    def size(self) -> float:
        return self.hi - self.lo

    @property
    def available(self) -> bool:
        """Usable as a PD array, in ``effective_direction``.

        Invalidation is the precondition for becoming a breaker (§5.4), so every breaker
        carries ``state = INVALIDATED``. Rejecting that state outright therefore rejected
        every breaker, leaving the detector, the `breakers()` helper and the definition in
        place while nothing could ever use one. An invalidated block is usable only when the
        opposing break confirmed it as a breaker, and then only in the opposite polarity,
        which `effective_direction` already carries.
        """
        if self.state is ArrayState.MITIGATED:
            return False
        if self.state is ArrayState.INVALIDATED:
            return self.breaker_at is not None
        return True

    @property
    def effective_direction(self) -> Direction:
        """A breaker trades as the opposite polarity (§5.4)."""
        return self.direction.opposite if self.breaker_at is not None else self.direction

    def proximal_edge(self) -> float:
        return self.hi if self.direction is Direction.UP else self.lo

    def contains(self, price: float) -> bool:
        return self.lo <= price <= self.hi


def find_order_blocks(
    bars: Sequence[Bar],
    breaks: Sequence[StructuralBreak],
    lookback_bars: int,
    point: float,
    extent: str = "wick",
    breaker_window_bars: int = 20,
) -> list[OrderBlock]:
    """One order block per structural break, where a qualifying candle exists.

    For a bullish break the block is the nearest preceding *down-close* candle, and vice
    versa. If none lies within ``lookback_bars`` the break produces no order block rather
    than reaching arbitrarily far back for one.
    """
    if extent not in ("wick", "body"):
        raise ValueError("extent must be 'wick' or 'body'")
    tol = 0.5 * point
    out: list[OrderBlock] = []

    for brk in breaks:
        t = brk.bar_index
        want_down = brk.direction is Direction.UP
        origin = None
        for j in range(t - 1, max(t - 1 - lookback_bars, -1), -1):
            candle = bars[j].ohlc
            if (candle.is_down if want_down else candle.is_up):
                origin = j
                break
        if origin is None:
            continue

        candle = bars[origin].ohlc
        if extent == "wick":
            lo, hi = candle.l, candle.h
        else:
            lo, hi = min(candle.o, candle.c), max(candle.o, candle.c)

        block = OrderBlock(
            instrument=bars[origin].instrument,
            timeframe=bars[origin].timeframe,
            direction=brk.direction,
            lo=lo,
            hi=hi,
            event_time=bars[origin].open_time,
            confirmed_at=brk.confirmed_at,
            caused_break=brk.kind,
            bars_to_break=t - origin,
            origin_index=origin,
        )
        out.append(_track_block(block, bars, t, tol, breaker_window_bars, breaks))
    out.sort(key=lambda b: b.confirmed_at)
    return out


def _track_block(
    block: OrderBlock,
    bars: Sequence[Bar],
    break_index: int,
    tol: float,
    breaker_window_bars: int,
    breaks: Sequence[StructuralBreak],
) -> OrderBlock:
    later = bars[break_index + 1 :]
    if not later:
        return block

    size = block.size
    if block.direction is Direction.UP:
        deepest = min(b.ohlc.l for b in later)
        fill = (block.hi - deepest) / size if size > 0 else 0.0
        invalid = next((b for b in later if b.ohlc.c < block.lo - tol), None)
    else:
        highest = max(b.ohlc.h for b in later)
        fill = (highest - block.lo) / size if size > 0 else 0.0
        invalid = next((b for b in later if b.ohlc.c > block.hi + tol), None)
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

    if invalid is None:
        return replace(block, state=state, fill_frac=fill)

    # Invalidated. A breaker additionally needs an opposing break within the window.
    opposing = [
        b
        for b in breaks
        if b.direction is block.direction.opposite
        and b.confirmed_at > invalid.close_time
        and b.bar_index - break_index <= breaker_window_bars
    ]
    return replace(
        block,
        state=ArrayState.INVALIDATED,
        fill_frac=fill,
        invalidated_at=invalid.close_time,
        breaker_at=min(b.confirmed_at for b in opposing) if opposing else None,
    )


def available_blocks(
    blocks: Sequence[OrderBlock], as_of: datetime, direction: Direction | None = None
) -> list[OrderBlock]:
    out = [b for b in blocks if b.confirmed_at <= as_of and b.available]
    if direction is not None:
        out = [b for b in out if b.effective_direction is direction]
    return out


def breakers(blocks: Sequence[OrderBlock], as_of: datetime) -> list[OrderBlock]:
    """Former blocks whose polarity inverted and whose inversion is confirmed."""
    return [
        b for b in blocks if b.breaker_at is not None and b.breaker_at <= as_of
    ]
