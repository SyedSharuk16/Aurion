"""Composite admissibility gate G1-G7. `docs/02-definitions.md` §3.2.

Every gate records a boolean and a reason on the candidate, so the backtest can report how
many candidates each one killed. A gate that kills 99% of candidates is a finding, not a
success -- which is why the reasons are structured strings rather than log text.

The posture throughout is **fail closed**: where a gate cannot be evaluated (spread
warm-up, missing ATR, calendar outage) the candidate is rejected and the reason names the
cause. The one exception is a configuration that explicitly sets ``fail_safe: allow``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from aurion.analysis.primitives import atr
from aurion.config.models import AurionConfig
from aurion.core.marketview import MarketView
from aurion.core.timeframe import UTC, Bucketer, TimeFrame, _zone
from aurion.sessions.calendar import CalendarProvider, blackout_for, currencies_of
from aurion.sessions.spread import SpreadTracker
from aurion.sessions.windows import SessionWindow, WindowSet


@dataclass(frozen=True, slots=True)
class GateResult:
    admissible: bool
    reasons: tuple[str, ...]
    window_label: str
    windows: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def first_reason(self) -> str:
        return self.reasons[0] if self.reasons else ""


class AdmissibilityGate:
    """Evaluates all seven gates for one instrument at one instant."""

    __slots__ = (
        "_calendar",
        "_cfg",
        "_has_active",
        "_max_blackout",
        "_on_calendar_outage",
        "_spread",
        "_w1_bucketer",
        "_windows",
    )

    def __init__(
        self,
        cfg: AurionConfig,
        windows: WindowSet,
        spread_tracker: SpreadTracker,
        calendar: CalendarProvider,
        has_active: Callable[[str], bool] | None = None,
        on_calendar_outage: Callable[[datetime], None] | None = None,
    ) -> None:
        self._cfg = cfg
        self._windows = windows
        self._spread = spread_tracker
        self._calendar = calendar
        self._has_active = has_active or (lambda _inst: False)
        self._on_calendar_outage = on_calendar_outage
        self._w1_bucketer = Bucketer(TimeFrame.W1, cfg.anchors().get(TimeFrame.W1))
        longest_override = max(cfg.news.event_overrides.values(), default=0)
        self._max_blackout = timedelta(
            minutes=max(cfg.news.blackout_minutes, longest_override)
        )

    # -- G1 ----------------------------------------------------------------
    def _g1_session(self, instrument: str, as_of: datetime) -> tuple[list[SessionWindow], str]:
        return self._windows.signal_windows_for(as_of, instrument), "session:no_signal_window"

    # -- G2 ----------------------------------------------------------------
    def _g2_friday(self, as_of: datetime) -> bool:
        local = as_of.astimezone(_zone(self._cfg.sessions.default_tz))
        return local.weekday() == 4 and local.time() >= self._cfg.blocks.friday_cutoff

    # -- G3 ----------------------------------------------------------------
    def _g3_rollover(self, as_of: datetime) -> bool:
        local = as_of.astimezone(_zone(self._cfg.sessions.default_tz)).time()
        r = self._cfg.blocks.rollover
        return r.start <= local < r.end

    # -- G4 ----------------------------------------------------------------
    def _g4_news(self, instrument: str, as_of: datetime) -> tuple[bool, str, dict[str, Any]]:
        """Returns (blocked, reason, details)."""
        if not self._calendar.available():
            if self._on_calendar_outage is not None:
                self._on_calendar_outage(as_of)
            blocked = self._cfg.news.fail_safe == "block"
            return blocked, "news:calendar_unavailable", {"calendar_available": False}

        inst = self._cfg.instruments[instrument]
        ccys = currencies_of(instrument, inst.base_ccy, inst.quote_ccy)
        window_start = as_of - self._max_blackout
        window_end = as_of + self._max_blackout
        as_published = as_of if self._calendar.supports_point_in_time else None
        events = self._calendar.events(window_start, window_end, as_published)

        for ev in events:
            if ev.impact not in self._cfg.news.blocked_impacts:
                continue
            if ev.currency not in ccys:
                continue
            blackout = blackout_for(
                ev, self._cfg.news.blackout_minutes, self._cfg.news.event_overrides
            )
            if abs(as_of - ev.scheduled_utc) <= blackout:
                return (
                    True,
                    f"news:{ev.event_id}",
                    {
                        "event": ev.title,
                        "scheduled": ev.scheduled_utc.isoformat(),
                        "blackout_min": int(blackout // timedelta(minutes=1)),
                        "point_in_time": self._calendar.supports_point_in_time,
                    },
                )
        return False, "", {"calendar_available": True, "events_checked": len(events)}

    # -- G5 ----------------------------------------------------------------
    def _g5_weekend_gap(
        self, instrument: str, as_of: datetime, view: MarketView
    ) -> tuple[bool, str, dict[str, Any]]:
        week = self._w1_bucketer.bucket(as_of)
        first = view.m1_between(instrument, week.open_time, as_of)
        if not first:
            return True, "weekend_gap:no_data_this_week", {}

        week_open_price = first[0].mid.o
        minutes_in = (as_of - first[0].open_time) // timedelta(minutes=1)
        if minutes_in < self._cfg.blocks.weekend_gap_block_minutes:
            return (
                True,
                "weekend_gap:settling",
                {"minutes_into_week": minutes_in},
            )

        prior = view.bars(instrument, TimeFrame.W1, 1)
        if not prior:
            return False, "", {"prior_week": None}

        d1 = view.usable_bars(instrument, TimeFrame.D1, self._cfg.analysis.atr_period + 1)
        atr_d1 = atr(d1, self._cfg.analysis.atr_period)
        if atr_d1 is None:
            # Fail closed: the gap magnitude cannot be verified, so it is not assumed small.
            return True, "weekend_gap:atr_warmup", {"d1_bars": len(d1)}

        gap = abs(week_open_price - prior[-1].ohlc.c)
        threshold = self._cfg.blocks.weekend_gap_atr_mult * atr_d1
        if gap > threshold:
            h1_after = [
                b
                for b in view.bars(instrument, TimeFrame.H1, 4)
                if b.close_time > first[0].open_time
            ]
            if not h1_after:
                return (
                    True,
                    "weekend_gap:large_awaiting_h1",
                    {"gap": gap, "threshold": threshold, "atr_d1": atr_d1},
                )
        return False, "", {"gap": gap, "threshold": threshold, "atr_d1": atr_d1}

    # -- G6 ----------------------------------------------------------------
    def _g6_spread(
        self, instrument: str, as_of: datetime, view: MarketView, spread: float | None
    ) -> tuple[bool, str, dict[str, Any]]:
        if spread is None:
            quote = view.last_quote(instrument)
            if quote is not None:
                spread = quote.spread
            else:
                m1 = view.m1(instrument, 1)
                spread = m1[-1].spread_close if m1 else None
        if spread is None:
            return False, "spread:no_observation", {}
        check = self._spread.check(instrument, as_of, spread)
        return (
            check.ok,
            check.reason,
            {"spread": check.spread, "median": check.median, "obs": check.observations},
        )

    # -- composite ---------------------------------------------------------
    def evaluate(
        self,
        instrument: str,
        as_of: datetime,
        view: MarketView,
        spread: float | None = None,
    ) -> GateResult:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        as_of = as_of.astimezone(UTC)
        reasons: list[str] = []
        details: dict[str, Any] = {}

        active, g1_reason = self._g1_session(instrument, as_of)
        if not active:
            reasons.append(g1_reason)

        if self._g2_friday(as_of):
            reasons.append("friday_cutoff")
        if self._g3_rollover(as_of):
            reasons.append("rollover")

        news_blocked, news_reason, news_details = self._g4_news(instrument, as_of)
        details |= news_details
        if news_blocked:
            reasons.append(news_reason)

        gap_blocked, gap_reason, gap_details = self._g5_weekend_gap(instrument, as_of, view)
        details |= gap_details
        if gap_blocked:
            reasons.append(gap_reason)

        spread_ok, spread_reason, spread_details = self._g6_spread(
            instrument, as_of, view, spread
        )
        details |= spread_details
        if not spread_ok:
            reasons.append(spread_reason)

        if self._has_active(instrument):
            reasons.append("duplicate:active_signal")

        return GateResult(
            admissible=not reasons,
            reasons=tuple(reasons),
            window_label=self._windows.label(as_of, instrument),
            windows=tuple(w.name for w in active),
            details=details,
        )


def build_window_set(cfg: AurionConfig) -> WindowSet:
    windows = {
        name: SessionWindow(
            name=name,
            tz=w.tz,
            start=w.start,
            end=w.end,
            emits_signals=w.emits_signals,
            weekdays=tuple(w.weekdays),
        )
        for name, w in cfg.sessions.windows.items()
    }
    whitelist = {sym: tuple(inst.sessions) for sym, inst in cfg.instruments.items()}
    return WindowSet(windows, whitelist)


def build_spread_tracker(cfg: AurionConfig) -> SpreadTracker:
    name_to_weekday = {
        "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    }
    classes = tuple(
        frozenset(name_to_weekday[n.lower()] for n in group)
        for group in cfg.spread.weekday_classes
    )
    return SpreadTracker(
        tz=cfg.sessions.default_tz,
        bucket_minutes=cfg.spread.bucket_minutes,
        window_obs=cfg.spread.bucket_window_obs,
        max_median_mult=cfg.spread.max_median_mult,
        fail_closed_on_warmup=cfg.spread.fail_closed_on_warmup,
        weekday_classes=classes,
    )
