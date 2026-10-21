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

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from aurion.core.timeframe import UTC

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

def load_calendar_events(path: str | Path) -> list[CalendarEvent]:
    """Read a calendar snapshot from JSON.

    Each entry needs `event_id`, `scheduled_utc`, `currency`, `title`, `impact` and
    `published_utc`; `revision` defaults to 0. Timestamps must be ISO 8601 and
    timezone-aware, because a naive one here is a silent hour-or-more error in a gate whose
    whole job is a +/-30 minute window.

    `published_utc` is not decoration: it is what makes the snapshot point-in-time, so the
    backtest sees an event as it was published rather than as it was later revised. A file
    that sets it equal to `scheduled_utc` is asserting the event was known at its own
    release instant, which is false for anything added or revised afterwards.
    """
    file = Path(path)
    if not file.is_file():
        # A bare FileNotFoundError here reads as a bug. Under `restart: unless-stopped`
        # it is also an endless crash-loop, so it names what is missing and why.
        raise ValueError(
            f"calendar file not found: {file}\n"
            "  news.fail_safe is 'block', so the gate cannot be left unfed.\n"
            "  To start on the TEMPLATE, whose dates are invented and which will block the\n"
            "  wrong minutes:  cp data/calendar.example.json data/calendar.json\n"
            "  Then replace it with a real schedule; see data/README.md for the schema."
        )
    raw = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a JSON list of events, got {type(raw).__name__}")
    out: list[CalendarEvent] = []
    for i, item in enumerate(raw):
        try:
            scheduled = _require_aware(datetime.fromisoformat(item["scheduled_utc"]))
            published = _require_aware(datetime.fromisoformat(item["published_utc"]))
            impact = item["impact"]
            if impact not in ("low", "medium", "high"):
                raise ValueError(f"impact {impact!r} is not low|medium|high")
            out.append(
                CalendarEvent(
                    event_id=str(item["event_id"]),
                    scheduled_utc=scheduled,
                    currency=str(item["currency"]),
                    title=str(item["title"]),
                    impact=impact,
                    published_utc=published,
                    revision=int(item.get("revision", 0)),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}: event {i} is invalid: {exc}") from exc
    return out


def _require_aware(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError(f"{ts.isoformat()} has no timezone")
    return ts.astimezone(UTC)


#: Markers the shipped template carries in every title. A calendar is checked for them
#: because the file is otherwise indistinguishable from a real schedule at runtime, and a
#: system running indefinitely on invented release times should say so on every start.
TEMPLATE_MARKERS = ("REPLACE-ME", "NOT A REAL SCHEDULE")


def is_template(events: Sequence[CalendarEvent]) -> bool:
    """Whether these events came from the shipped template rather than a real schedule."""
    return any(
        marker in event.title or marker in event.event_id
        for event in events
        for marker in TEMPLATE_MARKERS
    )


def calendar_from_env(env: Mapping[str, str]) -> CalendarProvider:
    """The provider named by `AURION_CALENDAR_PROVIDER`, or the blocking Null provider.

    `provider_env` was declared in `NewsCfg` and read by nothing: `build_app` always
    constructed `NullCalendarProvider`, which reports itself unavailable, which under
    `news.fail_safe = block` suppresses every signal. So the system could not emit at all,
    and the only sign of it was one admin alert.

    `file` is the supported provider. It is deliberately the only one: a snapshot on disk is
    reproducible, has real `published_utc` stamps, works in a backtest unchanged, and cannot
    quietly start returning different history for a past date the way a live API can.
    """
    name = env.get("AURION_CALENDAR_PROVIDER", "").strip().lower()
    if name in ("", "none", "null"):
        return NullCalendarProvider()
    if name == "file":
        path = env.get("AURION_CALENDAR_PATH", "").strip()
        if not path:
            raise ValueError(
                "AURION_CALENDAR_PROVIDER=file needs AURION_CALENDAR_PATH. Without it the "
                "news gate fails closed and every signal is suppressed."
            )
        return StaticCalendarProvider(load_calendar_events(path))
    raise ValueError(
        f"AURION_CALENDAR_PROVIDER={name!r} is not implemented. Supported: file, none. "
        "A name that silently falls back to 'none' would suppress every signal."
    )

