"""MarketView: the mechanism that makes look-ahead structurally impossible.

`docs/01-architecture.md` §3. Analysis functions receive a `MarketView`, not a series. The
view exposes only bars whose ``close_time <= as_of`` and only derived objects whose
``confirmed_at <= as_of``. There is no accessor that returns anything later. Causality is
therefore a property of the type boundary; the four causality tests confirm it rather than
establish it.

The two formal properties the tests check:

* **(C) Causality.** ``f(MarketView(D, t)) == f(MarketView(D|t, t))`` -- output cannot
  depend on data after ``t`` because it is identical when that data is absent.
* **(S) Stability.** For ``t1 < t2``, any object returned at ``t2`` with
  ``confirmed_at <= t1`` equals the object returned at ``t1``. Confirmed objects never
  change.

``partial()`` exists because a few quantities genuinely need the forming bar -- the live
spread, an intra-bar sweep extreme. It requires an explicit opt-in at construction, and
callers must be marked with `uses_forming_bar` so the prefix-invariance test can exclude
the intermediate and assert on the decision instead.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, Protocol, TypeVar, runtime_checkable

from aurion.core.timeframe import UTC, TimeFrame
from aurion.data.repository import BarRepository
from aurion.data.types import Bar, M1Candle, Quote

F = TypeVar("F", bound=Callable[..., Any])


def uses_forming_bar(fn: F) -> F:
    """Mark a function as reading the forming bar.

    Marked functions are excluded from the T1 prefix-invariance test at the intermediate
    level, because a forming bar legitimately changes within its own interval. The test
    then applies to the decision the function feeds, not to the function.
    """
    fn.__aurion_uses_forming_bar__ = True  # type: ignore[attr-defined]

    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        return fn(*args, **kwargs)

    _wrapped.__aurion_uses_forming_bar__ = True  # type: ignore[attr-defined]
    return _wrapped  # type: ignore[return-value]


@runtime_checkable
class Confirmable(Protocol):
    """Anything with a confirmation time: swings, arrays, pools, levels, quarters."""

    @property
    def confirmed_at(self) -> datetime: ...


@runtime_checkable
class ObjectRepository(Protocol):
    def objects(
        self, kind: str, instrument: str, timeframe: TimeFrame, as_of: datetime
    ) -> Sequence[Confirmable]: ...


class _EmptyObjectRepository:
    def objects(
        self, kind: str, instrument: str, timeframe: TimeFrame, as_of: datetime
    ) -> Sequence[Confirmable]:
        return ()


class PartialSource(Protocol):
    def partial(self, instrument: str, timeframe: TimeFrame) -> Bar | None: ...


class MarketView:
    """A read-only, as-of-bounded window onto market state."""

    __slots__ = ("_allow_forming", "_as_of", "_bars_repo", "_objects_repo", "_partial_src")

    def __init__(
        self,
        bars: BarRepository,
        as_of: datetime,
        *,
        objects: ObjectRepository | None = None,
        partial_source: PartialSource | None = None,
        allow_forming: bool = False,
    ) -> None:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        self._bars_repo = bars
        self._objects_repo = objects if objects is not None else _EmptyObjectRepository()
        self._partial_src = partial_source
        self._as_of = as_of.astimezone(UTC)
        self._allow_forming = allow_forming

    @property
    def as_of(self) -> datetime:
        return self._as_of

    # -- bars --------------------------------------------------------------
    def bars(self, instrument: str, timeframe: TimeFrame, n: int) -> Sequence[Bar]:
        """Closed bars only, oldest first. Never includes the forming bar."""
        if n <= 0:
            raise ValueError("n must be positive")
        return self._bars_repo.bars(instrument, timeframe, n, self._as_of)

    def usable_bars(self, instrument: str, timeframe: TimeFrame, n: int) -> Sequence[Bar]:
        """Closed bars excluding thin ones (`docs/02-definitions.md` §2.2 rule 2).

        Fetches extra and filters, so a run of incomplete bars cannot silently shorten the
        window below what an indicator's warm-up requires.
        """
        raw = self._bars_repo.bars(instrument, timeframe, n * 2 + 8, self._as_of)
        return [b for b in raw if b.usable][-n:]

    def last_bar(self, instrument: str, timeframe: TimeFrame) -> Bar | None:
        got = self.bars(instrument, timeframe, 1)
        return got[-1] if got else None

    def m1(self, instrument: str, n: int) -> Sequence[M1Candle]:
        return self._bars_repo.m1(instrument, n, self._as_of)

    def m1_between(self, instrument: str, start: datetime, end: datetime) -> Sequence[M1Candle]:
        return self._bars_repo.m1_between(instrument, start, end, self._as_of)

    # -- derived objects ---------------------------------------------------
    def objects(self, kind: str, instrument: str, timeframe: TimeFrame) -> Sequence[Confirmable]:
        """Objects confirmed at or before ``as_of``. Unconfirmed objects are unreachable."""
        got = self._objects_repo.objects(kind, instrument, timeframe, self._as_of)
        # Defence in depth: the repository filters, and so do we. A repository bug would
        # otherwise be a silent look-ahead rather than a loud one.
        return [o for o in got if o.confirmed_at <= self._as_of]

    # -- quotes and the forming bar ----------------------------------------
    def last_quote(self, instrument: str) -> Quote | None:
        return self._bars_repo.last_quote(instrument, self._as_of)

    @uses_forming_bar
    def partial(self, instrument: str, timeframe: TimeFrame) -> Bar | None:
        if not self._allow_forming:
            raise PermissionError(
                "forming-bar access requires allow_forming=True; default evaluation is "
                "bar-close-driven (runtime.evaluate_on_forming_bar)"
            )
        if self._partial_src is None:
            return None
        return self._partial_src.partial(instrument, timeframe)

    # -- derivation --------------------------------------------------------
    def at(self, as_of: datetime) -> MarketView:
        """A view at a different instant over the same stores."""
        return MarketView(
            self._bars_repo,
            as_of,
            objects=self._objects_repo,
            partial_source=self._partial_src,
            allow_forming=self._allow_forming,
        )

    def __repr__(self) -> str:
        return f"MarketView(as_of={self._as_of.isoformat()})"
