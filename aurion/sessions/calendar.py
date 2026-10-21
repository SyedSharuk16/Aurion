"""Economic calendar. `docs/02-definitions.md` §3.5, `docs/05-data-contracts.md` §2.

Two hazards are handled here rather than left to the caller.

**Fail-safe blocking is a liveness hazard.** ``fail_safe = block`` means a calendar outage
silently stops every signal. The provider therefore reports its availability, and the gate
raises an admin alert on entering the blocked-on-unavailable state and re-raises on an
interval. A system that emitted nothing for a week because of a dead API must be loud.

**Point-in-time integrity.** Replaying today's calendar over 2019 data leaks events that
were added, rescheduled or re-graded afterwards. ``as_published_before`` is the mechanism;
a provider that cannot supply ``published_at`` marks the run
``calendar_point_in_time: false``, which the validation report prints in its header rather
than hiding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Protocol, runtime_checkable

Impact = Literal["low", "medium", "high"]


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    event_id: str
    scheduled_utc: datetime
    currency: str
    title: str
    impact: Impact
    published_utc: datetime
    revision: int = 0


@runtime_checkable
class CalendarProvider(Protocol):
    name: str
    #: False means this provider cannot answer point-in-time queries; the run is marked.
    supports_point_in_time: bool

    def available(self) -> bool: ...

    def events(
        self,
        start_utc: datetime,
        end_utc: datetime,
        as_published_before: datetime | None = None,
    ) -> list[CalendarEvent]: ...


class NullCalendarProvider:
    """No calendar configured.

    Reports itself unavailable, which under ``fail_safe = block`` blocks every signal. That
    is the specified behaviour and it is deliberately loud rather than convenient: a system
    with no news feed should not quietly trade through NFP.
    """

    name = "null"
    supports_point_in_time = False

    def available(self) -> bool:
        return False

    def events(
        self,
        start_utc: datetime,
        end_utc: datetime,
        as_published_before: datetime | None = None,
    ) -> list[CalendarEvent]:
        return []


class StaticCalendarProvider:
    """In-memory provider for tests and for a frozen point-in-time snapshot."""

    name = "static"

    def __init__(self, events: list[CalendarEvent], supports_point_in_time: bool = True) -> None:
        self._events = sorted(events, key=lambda e: e.scheduled_utc)
        self.supports_point_in_time = supports_point_in_time
        self._available = True

    def set_available(self, value: bool) -> None:
        self._available = value

    def available(self) -> bool:
        return self._available

    def events(
        self,
        start_utc: datetime,
        end_utc: datetime,
        as_published_before: datetime | None = None,
    ) -> list[CalendarEvent]:
        out = [e for e in self._events if start_utc <= e.scheduled_utc <= end_utc]
        if as_published_before is not None:
            # Point-in-time: an event is visible only with the fields it had as published
            # before the evaluation instant. Later revisions are invisible, and the latest
            # visible revision of each event wins.
            visible: dict[str, CalendarEvent] = {}
            for e in out:
                if e.published_utc <= as_published_before:
                    prev = visible.get(e.event_id)
                    if prev is None or e.revision > prev.revision:
                        visible[e.event_id] = e
            out = sorted(visible.values(), key=lambda e: e.scheduled_utc)
        return out


def currencies_of(symbol: str, base: str, quote: str) -> frozenset[str]:
    """Currencies whose news affects an instrument.

    Metals are reported as their own code plus USD: a gold-specific event matters to XAUUSD
    and a USD event matters to everything quoted in USD.
    """
    out = {base, quote}
    if base.startswith("X"):  # XAU, XAG
        out.add("USD")
    return frozenset(out)


def blackout_for(
    event: CalendarEvent, default_minutes: int, overrides: dict[str, int]
) -> timedelta:
    """Per-event blackout, longest matching override wins.

    Matching is on a case-insensitive substring of the title so that "US CPI m/m" picks up
    a "CPI" override. The longest match wins, so a "CORE CPI" override beats "CPI".
    """
    title = event.title.upper()
    best: tuple[int, int] | None = None
    for key, minutes in overrides.items():
        k = key.upper()
        if k in title and (best is None or len(k) > best[0]):
            best = (len(k), minutes)
    return timedelta(minutes=best[1] if best else default_minutes)
