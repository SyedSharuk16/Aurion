"""Clocks. The only difference between live and backtest, together with the bar source.

`docs/01-architecture.md` §4: `analysis/`, `features/` and `signals/` contain no branch on
mode. They receive a `MarketView`, which receives its `as_of` from whichever clock the
composition root injected.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from aurion.core.timeframe import UTC


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


class WallClock:
    """System UTC time. Live runtime only."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class SimClock:
    """Backtest clock. Advances only when explicitly set, never spontaneously.

    Monotonicity is enforced: an attempt to move the simulation clock backwards raises,
    because a backwards step is how replay silently reveals future data.
    """

    __slots__ = ("_now",)

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("SimClock start must be timezone-aware")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance_to(self, ts: datetime) -> None:
        if ts.tzinfo is None:
            raise ValueError("advance_to requires a timezone-aware datetime")
        ts = ts.astimezone(UTC)
        if ts < self._now:
            raise ValueError(f"SimClock cannot move backwards: {self._now} -> {ts}")
        self._now = ts
