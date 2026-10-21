"""Session windows. `docs/02-definitions.md` §3.1.

A window is ``(name, tz, start, end, weekdays, emits_signals)``. Membership converts the
instant to the window's own timezone and tests ``[start, end)`` -- half-open, so adjacent
windows cannot both claim the boundary minute.

Each window declares its own timezone deliberately. Anchoring "London Kill Zone" to
02:00-05:00 New York means that for the ~3 weeks a year when UK and US DST transitions are
out of step, the window sits an hour away from the London session it is named after. The
default follows the ICT convention (NY) because that is what the literature means; running
the London windows on ``Europe/London`` is a robustness variant, not a bug fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from aurion.core.timeframe import UTC, _localise, _zone


@dataclass(frozen=True, slots=True)
class SessionWindow:
    name: str
    tz: str
    start: time
    end: time
    emits_signals: bool
    weekdays: tuple[int, ...] = ()  # empty means every weekday

    @property
    def crosses_midnight(self) -> bool:
        """``end <= start`` means the window runs past local midnight.

        A window written ``20:00-00:00`` therefore spans ``[20:00, 24:00)`` and an empty
        ``[00:00, 00:00)`` on the following day, which is the intended 4 hours.
        """
        return self.end <= self.start

    def _weekday_ok(self, weekday: int) -> bool:
        return not self.weekdays or weekday in self.weekdays

    def contains(self, ts: datetime) -> bool:
        if ts.tzinfo is None:
            raise ValueError("contains() requires a timezone-aware datetime")
        local = ts.astimezone(_zone(self.tz))
        t = local.time()
        if not self.crosses_midnight:
            return self._weekday_ok(local.weekday()) and self.start <= t < self.end
        # Crossing: the tail belongs to the window that OPENED on the previous local day.
        if t >= self.start:
            return self._weekday_ok(local.weekday())
        if t < self.end:
            prev = (local - timedelta(days=1)).weekday()
            return self._weekday_ok(prev)
        return False

    def occurrence(self, local_day: date) -> tuple[datetime, datetime]:
        """UTC bounds of the occurrence that OPENS on ``local_day``."""
        zone = _zone(self.tz)
        start = _localise(datetime.combine(local_day, self.start), zone)
        end_day = local_day + timedelta(days=1) if self.crosses_midnight else local_day
        end = _localise(datetime.combine(end_day, self.end), zone)
        return start, end

    def current_occurrence(self, ts: datetime) -> tuple[datetime, datetime] | None:
        """UTC bounds of the occurrence containing ``ts``, or ``None`` if outside."""
        if not self.contains(ts):
            return None
        local = ts.astimezone(_zone(self.tz))
        day = local.date()
        if self.crosses_midnight and local.time() < self.end:
            day = day - timedelta(days=1)
        return self.occurrence(day)

    def minutes_into(self, ts: datetime) -> int | None:
        occ = self.current_occurrence(ts)
        if occ is None:
            return None
        return int((ts.astimezone(UTC) - occ[0]) // timedelta(minutes=1))


class WindowSet:
    """All configured windows, with per-instrument whitelists."""

    __slots__ = ("_whitelist", "_windows")

    def __init__(
        self,
        windows: dict[str, SessionWindow],
        whitelist: dict[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._windows = windows
        self._whitelist = whitelist or {}

    def __getitem__(self, name: str) -> SessionWindow:
        return self._windows[name]

    def __contains__(self, name: str) -> bool:
        return name in self._windows

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._windows)

    def active(self, ts: datetime) -> list[SessionWindow]:
        return [w for w in self._windows.values() if w.contains(ts)]

    def active_for(self, ts: datetime, instrument: str) -> list[SessionWindow]:
        """Active windows this instrument is whitelisted for."""
        allowed = self._whitelist.get(instrument)
        active = self.active(ts)
        if allowed is None:
            return active
        return [w for w in active if w.name in allowed]

    def signal_windows_for(self, ts: datetime, instrument: str) -> list[SessionWindow]:
        """Gate G1: active, whitelisted, and permitted to emit."""
        return [w for w in self.active_for(ts, instrument) if w.emits_signals]

    def label(self, ts: datetime, instrument: str) -> str:
        active = self.active_for(ts, instrument)
        return " + ".join(w.name for w in active) if active else "none"
