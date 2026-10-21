"""Health alerts on a separate admin channel. `docs/03-signal-engine.md` §7.5.

The calendar-outage alert is the one that matters most. ``news.fail_safe = block`` means a
dead calendar API silently stops every signal, so entering that state raises immediately,
re-raises on an interval, and the daily heartbeat reports total minutes spent in it. A
system that emitted nothing for a week because of a dead API must be loud about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from aurion.notify.telegram.formatters import code, escape


class AlertKind(StrEnum):
    FEED_STALE = "feed_stale"
    ADAPTER_DISCONNECT = "adapter_disconnect"
    ADAPTER_RECONNECT = "adapter_reconnect"
    CALENDAR_OUTAGE = "calendar_outage"
    EXCEPTION = "exception"
    DB_FAILURE = "db_failure"
    OUTBOX_DEAD_LETTER = "outbox_dead_letter"
    DETERMINISM_MISMATCH = "determinism_mismatch"
    HEARTBEAT = "heartbeat"


_EMOJI = {
    AlertKind.FEED_STALE: "\U0001f4e1",
    AlertKind.ADAPTER_DISCONNECT: "\U0001f50c",
    AlertKind.ADAPTER_RECONNECT: "\U0001f501",
    AlertKind.CALENDAR_OUTAGE: "\U0001f4c5",
    AlertKind.EXCEPTION: "\U0001f525",
    AlertKind.DB_FAILURE: "\U0001f4be",
    AlertKind.OUTBOX_DEAD_LETTER: "\U0001f4ea",
    AlertKind.DETERMINISM_MISMATCH: "⚠️",
    AlertKind.HEARTBEAT: "\U0001f493",
}


@dataclass(frozen=True, slots=True)
class Alert:
    kind: AlertKind
    at: datetime
    detail: str
    dedupe_key: str

    def render(self) -> str:
        emoji = _EMOJI.get(self.kind, "ℹ")
        label = escape(str(self.kind).replace("_", " ").upper())
        # `escape` for the prose detail, `code` for the timestamp: prose escaping inside
        # a code span renders its backslashes literally and can make Telegram reject the
        # message, which for a health alert means losing the message that explains an
        # outage.
        return f"{emoji} *{label}*\n\n{escape(self.detail)}\n\n{code(self.at.isoformat())}"


@dataclass(slots=True)
class DailyCounters:
    """What the heartbeat reports for the last 24 hours."""

    bars_ingested: int = 0
    candidates: int = 0
    signals_emitted: int = 0
    transitions: int = 0
    errors: int = 0
    rejections_by_reason: dict[str, int] = field(default_factory=dict)
    blocked_minutes: dict[str, float] = field(default_factory=dict)

    def record_rejection(self, reason: str) -> None:
        self.rejections_by_reason[reason] = self.rejections_by_reason.get(reason, 0) + 1

    def add_blocked(self, state: str, minutes: float) -> None:
        self.blocked_minutes[state] = self.blocked_minutes.get(state, 0.0) + minutes

    def reset(self) -> None:
        self.bars_ingested = 0
        self.candidates = 0
        self.signals_emitted = 0
        self.transitions = 0
        self.errors = 0
        self.rejections_by_reason.clear()
        self.blocked_minutes.clear()


class HealthMonitor:
    """Decides when to alert, and suppresses repeats without hiding a standing problem."""

    __slots__ = (
        "_last_sent",
        "_max_staleness",
        "_realert",
        "_seen_connected",
        "_states_since",
        "counters",
    )

    def __init__(
        self,
        max_bar_staleness_s: float = 120.0,
        calendar_realert_minutes: int = 30,
    ) -> None:
        self._max_staleness = max_bar_staleness_s
        self._realert = timedelta(minutes=calendar_realert_minutes)
        self._seen_connected: dict[str, bool] = {}
        self._last_sent: dict[str, datetime] = {}
        self._states_since: dict[str, datetime] = {}
        self.counters = DailyCounters()

    def _should_send(self, key: str, at: datetime) -> bool:
        last = self._last_sent.get(key)
        if last is None or at - last >= self._realert:
            self._last_sent[key] = at
            return True
        return False

    def enter_state(self, state: str, at: datetime) -> None:
        self._states_since.setdefault(state, at)

    def leave_state(self, state: str, at: datetime) -> float:
        since = self._states_since.pop(state, None)
        self._last_sent.pop(state, None)
        if since is None:
            return 0.0
        minutes = (at - since).total_seconds() / 60.0
        self.counters.add_blocked(state, minutes)
        return minutes

    def minutes_in_state(self, state: str, at: datetime) -> float:
        since = self._states_since.get(state)
        return 0.0 if since is None else (at - since).total_seconds() / 60.0

    # -- specific alerts ---------------------------------------------------
    def feed_stale(self, instrument: str, age_s: float, at: datetime) -> Alert | None:
        if age_s <= self._max_staleness:
            self.leave_state(f"stale:{instrument}", at)
            return None
        self.enter_state(f"stale:{instrument}", at)
        if not self._should_send(f"stale:{instrument}", at):
            return None
        return Alert(
            AlertKind.FEED_STALE,
            at,
            f"{instrument} has no new M1 bar for {age_s:.0f}s "
            f"(limit {self._max_staleness:.0f}s) during an open session.",
            f"stale:{instrument}:{at.isoformat(timespec='minutes')}",
        )

    def calendar_outage(self, at: datetime, available: bool) -> Alert | None:
        key = "calendar_outage"
        if available:
            minutes = self.leave_state(key, at)
            if minutes > 0:
                return Alert(
                    AlertKind.CALENDAR_OUTAGE,
                    at,
                    f"Economic calendar is available again after {minutes:.0f} minutes. "
                    "Signals were suppressed for that whole period.",
                    f"{key}:recovered:{at.isoformat(timespec='minutes')}",
                )
            return None
        self.enter_state(key, at)
        if not self._should_send(key, at):
            return None
        minutes = self.minutes_in_state(key, at)
        return Alert(
            AlertKind.CALENDAR_OUTAGE,
            at,
            "Economic calendar unavailable. Under fail_safe=block EVERY signal is "
            f"suppressed, and has been for {minutes:.0f} minutes. This is deliberate, and "
            "deliberately loud: a silent dead bot is worse than a noisy one.",
            f"{key}:{at.isoformat(timespec='minutes')}",
        )

    def adapter_state(self, adapter: str, connected: bool, at: datetime, detail: str = "") -> Alert:
        kind = AlertKind.ADAPTER_RECONNECT if connected else AlertKind.ADAPTER_DISCONNECT
        if connected:
            # "reconnected" on the very first connection is a small lie that reads as a
            # fault on a healthy start. The first one is a connection.
            verb = "reconnected" if self._seen_connected.get(adapter) else "connected"
            self._seen_connected[adapter] = True
        else:
            verb = "disconnected"
        return Alert(
            kind,
            at,
            f"{adapter} {verb}. {detail}".strip(),
            f"adapter:{adapter}:{verb}:{at.isoformat(timespec='seconds')}",
        )

    def exception(self, where: str, exc: BaseException, at: datetime) -> Alert:
        self.counters.errors += 1
        return Alert(
            AlertKind.EXCEPTION,
            at,
            f"Unhandled exception in {where}: {type(exc).__name__}: {exc}",
            f"exception:{where}:{at.isoformat(timespec='seconds')}",
        )

    def determinism_mismatch(self, instrument: str, timeframe: str, at: datetime) -> Alert:
        return Alert(
            AlertKind.DETERMINISM_MISMATCH,
            at,
            f"A recomputed {timeframe} bar for {instrument} differs from the stored one. "
            "That means a non-deterministic analysis path exists, so replay and live can "
            "diverge. This is a hard failure, not a warning.",
            f"determinism:{instrument}:{timeframe}:{at.isoformat(timespec='seconds')}",
        )

    def dead_letter(self, count: int, at: datetime) -> Alert:
        return Alert(
            AlertKind.OUTBOX_DEAD_LETTER,
            at,
            f"{count} Telegram message(s) exhausted their retries and were dead-lettered. "
            "They are not lost, but they were not delivered.",
            f"dead_letter:{at.isoformat(timespec='minutes')}",
        )

    def heartbeat(self, at: datetime, uptime_s: float) -> Alert:
        c = self.counters
        top = sorted(c.rejections_by_reason.items(), key=lambda kv: -kv[1])[:5]
        blocked = ", ".join(f"{k} {v:.0f}m" for k, v in sorted(c.blocked_minutes.items()))
        detail = (
            f"24h: {c.bars_ingested} bars, {c.candidates} candidates, "
            f"{c.signals_emitted} signals, {c.transitions} transitions, {c.errors} errors. "
            f"Uptime {uptime_s / 3600:.1f}h."
        )
        if top:
            detail += " Top rejections: " + ", ".join(f"{k} x{v}" for k, v in top) + "."
        if blocked:
            detail += f" Minutes blocked: {blocked}."
        return Alert(
            AlertKind.HEARTBEAT, at, detail, f"heartbeat:{at.date().isoformat()}"
        )
