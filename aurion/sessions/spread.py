"""Spread filter with time-of-day conditioning. `docs/02-definitions.md` §3.4.

The brief specified a rolling median. A *global* rolling median is dominated by time of
day: XAUUSD's spread at 03:00 NY is several multiples of its 09:00 NY value, so a global
threshold either permits everything in the thin session or blocks everything in the liquid
one. The median must be conditioned on a time-of-day bucket. That is a correction to the
specification, not a refinement of it.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from statistics import median

from aurion.core.timeframe import _zone

#: Monday=0 .. Sunday=6, grouped per `spread.weekday_classes`.
_DEFAULT_CLASSES: tuple[frozenset[int], ...] = (
    frozenset({0}),           # Monday: the week's open behaves differently
    frozenset({1, 2, 3}),     # Tue-Thu: the stable core
    frozenset({4, 5, 6}),     # Friday and the weekend tail
)


@dataclass(frozen=True, slots=True)
class SpreadCheck:
    ok: bool
    spread: float
    median: float | None
    observations: int
    reason: str


class SpreadTracker:
    """Rolling per-bucket spread medians.

    Bucket key is ``(instrument, weekday_class, slot-of-day)`` where the slot is a
    fixed-width division of the local day in the tracker's timezone.
    """

    __slots__ = ("_bucket_minutes", "_classes", "_fail_closed", "_mult", "_obs", "_tz", "_window")

    def __init__(
        self,
        tz: str = "America/New_York",
        bucket_minutes: int = 15,
        window_obs: int = 20,
        max_median_mult: float = 2.5,
        fail_closed_on_warmup: bool = True,
        weekday_classes: tuple[frozenset[int], ...] = _DEFAULT_CLASSES,
    ) -> None:
        if bucket_minutes <= 0 or 1440 % bucket_minutes:
            raise ValueError("bucket_minutes must divide 1440")
        self._tz = tz
        self._bucket_minutes = bucket_minutes
        self._window = window_obs
        self._mult = max_median_mult
        self._fail_closed = fail_closed_on_warmup
        self._classes = weekday_classes
        self._obs: dict[tuple[str, int, int], deque[float]] = defaultdict(
            lambda: deque(maxlen=window_obs)
        )

    def _weekday_class(self, weekday: int) -> int:
        for i, group in enumerate(self._classes):
            if weekday in group:
                return i
        return len(self._classes)

    def bucket_key(self, instrument: str, ts: datetime) -> tuple[str, int, int]:
        local = ts.astimezone(_zone(self._tz))
        slot = (local.hour * 60 + local.minute) // self._bucket_minutes
        return (instrument, self._weekday_class(local.weekday()), slot)

    def observe(self, instrument: str, ts: datetime, spread: float) -> None:
        if spread < 0:
            raise ValueError(f"negative spread for {instrument} at {ts}: {spread}")
        self._obs[self.bucket_key(instrument, ts)].append(spread)

    def check(self, instrument: str, ts: datetime, spread: float) -> SpreadCheck:
        """Gate G6. Uses observations strictly BEFORE this one, so it is causal.

        ``observe`` is called after ``check`` by the caller; the current observation is
        therefore never part of its own median.
        """
        key = self.bucket_key(instrument, ts)
        obs = self._obs[key]
        if len(obs) < self._window:
            return SpreadCheck(
                ok=not self._fail_closed,
                spread=spread,
                median=None,
                observations=len(obs),
                reason="spread:warmup",
            )
        med = median(obs)
        if med <= 0:
            return SpreadCheck(False, spread, med, len(obs), "spread:degenerate_median")
        ok = spread <= self._mult * med
        return SpreadCheck(
            ok=ok,
            spread=spread,
            median=med,
            observations=len(obs),
            reason="" if ok else f"spread:{spread / med:.2f}x_median",
        )

    def median_for(self, instrument: str, ts: datetime) -> float | None:
        obs = self._obs[self.bucket_key(instrument, ts)]
        return median(obs) if len(obs) >= self._window else None
