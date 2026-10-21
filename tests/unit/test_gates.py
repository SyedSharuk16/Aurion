"""Composite admissibility gate G1-G7. `docs/02-definitions.md` §3.2."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from aurion.core.marketview import MarketView
from aurion.core.timeframe import UTC, TimeFrame
from aurion.data.repository import InMemoryBarRepository
from aurion.data.resample import ResamplerSet
from aurion.data.types import Quote
from aurion.sessions.calendar import CalendarEvent, NullCalendarProvider, StaticCalendarProvider
from aurion.sessions.gates import AdmissibilityGate, build_spread_tracker, build_window_set
from aurion.sessions.spread import SpreadTracker
from tests.conftest import make_m1

NY = ZoneInfo("America/New_York")
#: Sunday 2024-06-09 17:00 NY is the FX week open; feed a full week from there.
WEEK_OPEN = datetime(2024, 6, 9, 17, 0, tzinfo=NY).astimezone(UTC)


@pytest.fixture
def world(cfg):
    """A repository fed with two weeks of hourly-sampled M1 so D1 ATR can warm up."""
    repo = InMemoryBarRepository()
    rs = ResamplerSet("XAUUSD", (TimeFrame.H1, TimeFrame.D1, TimeFrame.W1))
    start = WEEK_OPEN - timedelta(days=21)
    minute = 0
    price = 2300.0
    t = start
    while t < WEEK_OPEN + timedelta(days=5):
        # Skip the weekend: Fri 17:00 NY -> Sun 17:00 NY has no data, by design.
        local = t.astimezone(NY)
        closed_market = (local.weekday() == 4 and local.hour >= 17) or local.weekday() == 5 or (
            local.weekday() == 6 and local.hour < 17
        )
        if not closed_market:
            candle = make_m1("XAUUSD", t, price, high_off=0.8, low_off=0.6,
                             close_off=0.2, spread=0.20)
            repo.add_m1(candle)
            for bar in rs.push(candle):
                repo.add_bar(bar)
            price += 0.05 if minute % 2 else -0.04
            minute += 1
        t += timedelta(minutes=1)
    return repo


def build_gate(cfg, calendar=None, has_active=None, warm_for=(), spread=0.20):
    """Gate with a small spread window, warmed only at the instants under test.

    The bucket key is (instrument, weekday class, 15-minute slot), so warming has to land
    in the bucket actually being evaluated. Observing the same NY weekday and time in prior
    weeks is the only thing that does. Tests use a 5-observation window because they are
    about gate composition, not about how long a real bucket takes to warm --
    `test_spread_and_calendar.py` covers the conditioning itself.
    """
    st = SpreadTracker(
        tz=cfg.sessions.default_tz,
        bucket_minutes=cfg.spread.bucket_minutes,
        window_obs=5,
        max_median_mult=cfg.spread.max_median_mult,
        fail_closed_on_warmup=cfg.spread.fail_closed_on_warmup,
    )
    for ts in warm_for:
        for weeks_back in range(1, 7):
            st.observe("XAUUSD", ts - timedelta(weeks=weeks_back), spread)
    return AdmissibilityGate(
        cfg, build_window_set(cfg), st, calendar or StaticCalendarProvider([]),
        has_active=has_active,
    )


def view(repo, as_of):
    return MarketView(repo, as_of)


def at(day_offset: int, hour: int, minute: int = 0) -> datetime:
    """A time in the trading week that opened at WEEK_OPEN, in NY hours."""
    base = (WEEK_OPEN + timedelta(days=day_offset)).astimezone(NY)
    return base.replace(hour=hour, minute=minute).astimezone(UTC)


def test_admissible_inside_a_signal_window(cfg, world):
    ts = at(1, 9)  # Monday 09:00 NY -> NY AM kill zone + gold window
    gate = build_gate(cfg, warm_for=[ts])
    got = gate.evaluate("XAUUSD", ts, view(world, ts), spread=0.20)
    assert got.admissible, got.reasons
    assert "ny_am_kz" in got.windows


def test_g1_outside_every_window(cfg, world):
    gate = build_gate(cfg)
    ts = at(1, 13)
    got = gate.evaluate("XAUUSD", ts, view(world, ts), spread=0.20)
    assert not got.admissible and "session:no_signal_window" in got.reasons


def test_g2_friday_cutoff(cfg, world):
    gate = build_gate(cfg)
    ts = at(5, 14)  # Friday 14:00 NY, past the 12:00 cutoff
    got = gate.evaluate("XAUUSD", ts, view(world, ts), spread=0.20)
    assert "friday_cutoff" in got.reasons


def test_g3_rollover(cfg, world):
    gate = build_gate(cfg)
    ts = at(1, 17, 0)  # 17:00 NY is inside [16:55, 17:10)
    got = gate.evaluate("XAUUSD", ts, view(world, ts), spread=0.20)
    assert "rollover" in got.reasons


def test_g4_news_blackout_blocks_and_names_the_event(cfg, world):
    event_time = at(1, 9)
    cal = StaticCalendarProvider(
        [CalendarEvent("cpi", event_time, "USD", "US CPI m/m", "high",
                       WEEK_OPEN - timedelta(days=10))]
    )
    gate = build_gate(cfg, calendar=cal)
    got = gate.evaluate("XAUUSD", event_time, view(world, event_time), spread=0.20)
    assert not got.admissible
    assert any(r.startswith("news:") for r in got.reasons)
    assert got.details["blackout_min"] == 60  # CPI override, not the 30-minute default


def test_g4_outside_the_blackout_is_fine(cfg, world):
    event_time = at(1, 9)
    cal = StaticCalendarProvider(
        [CalendarEvent("cpi", event_time - timedelta(hours=3), "USD", "US CPI m/m", "high",
                       WEEK_OPEN - timedelta(days=10))]
    )
    gate = build_gate(cfg, calendar=cal, warm_for=[event_time])
    got = gate.evaluate("XAUUSD", event_time, view(world, event_time), spread=0.20)
    assert got.admissible, got.reasons


def test_g4_low_impact_events_do_not_block(cfg, world):
    ts = at(1, 9)
    cal = StaticCalendarProvider(
        [CalendarEvent("x", ts, "USD", "Minor thing", "low", WEEK_OPEN - timedelta(days=10))]
    )
    gate = build_gate(cfg, calendar=cal, warm_for=[ts])
    assert gate.evaluate("XAUUSD", ts, view(world, ts), spread=0.20).admissible


def test_g4_calendar_outage_fails_closed_and_fires_the_alert(cfg, world):
    """The liveness hazard: fail-safe blocking must be loud, not silent."""
    alerts: list[datetime] = []
    gate = AdmissibilityGate(
        cfg,
        build_window_set(cfg),
        build_spread_tracker(cfg),
        NullCalendarProvider(),
        on_calendar_outage=alerts.append,
    )
    ts = at(1, 9)
    got = gate.evaluate("XAUUSD", ts, view(world, ts), spread=0.20)
    assert "news:calendar_unavailable" in got.reasons
    assert len(alerts) == 1, "an outage that blocks every signal must raise an alert"


def test_g5_weekend_settling_block(cfg, world):
    gate = build_gate(cfg)
    ts = WEEK_OPEN + timedelta(minutes=10)  # inside the 60-minute settling window
    got = gate.evaluate("XAUUSD", ts, view(world, ts), spread=0.20)
    assert "weekend_gap:settling" in got.reasons


def test_g5_atr_warmup_fails_closed(cfg):
    """The gap magnitude cannot be verified, so it is not assumed small.

    Needs a prior weekly bar to exist (so the check is reached) while D1 ATR is still cold,
    which is exactly one week of history: 5 daily bars against an ATR period of 14.
    """
    repo = InMemoryBarRepository()
    rs = ResamplerSet("XAUUSD", (TimeFrame.H1, TimeFrame.D1, TimeFrame.W1))
    t = WEEK_OPEN - timedelta(days=7)
    while t < WEEK_OPEN + timedelta(days=2):
        local = t.astimezone(NY)
        closed = (local.weekday() == 4 and local.hour >= 17) or local.weekday() == 5 or (
            local.weekday() == 6 and local.hour < 17
        )
        if not closed:
            c = make_m1("XAUUSD", t, 2300.0, high_off=0.8, low_off=0.6, close_off=0.2,
                        spread=0.20)
            repo.add_m1(c)
            for bar in rs.push(c):
                repo.add_bar(bar)
        t += timedelta(minutes=1)
    ts = at(1, 9)
    gate = build_gate(cfg, warm_for=[ts])
    got = gate.evaluate("XAUUSD", ts, view(repo, ts), spread=0.20)
    assert "weekend_gap:atr_warmup" in got.reasons, got.details


def test_g6_wide_spread_blocks(cfg, world):
    ts = at(1, 9)
    gate = build_gate(cfg, warm_for=[ts])
    got = gate.evaluate("XAUUSD", ts, view(world, ts), spread=5.0)
    assert not got.admissible and any("spread:" in r for r in got.reasons)


def test_g6_falls_back_to_the_last_quote_then_the_last_m1(cfg, world):
    ts = at(1, 9)
    gate = build_gate(cfg, warm_for=[ts])
    world.add_quote(Quote("XAUUSD", ts - timedelta(seconds=5), 2300.0, 2305.0))
    got = gate.evaluate("XAUUSD", ts, view(world, ts), spread=None)
    assert not got.admissible, "a 5.00 spread from the quote must be caught"


def test_g7_duplicate_active_signal(cfg, world):
    gate = build_gate(cfg, has_active=lambda _i: True)
    ts = at(1, 9)
    got = gate.evaluate("XAUUSD", ts, view(world, ts), spread=0.20)
    assert "duplicate:active_signal" in got.reasons


def test_every_failing_gate_is_reported_not_just_the_first(cfg, world):
    """The funnel needs all reasons: a gate that kills 99% of candidates is a finding."""
    gate = build_gate(cfg, has_active=lambda _i: True)
    ts = at(5, 17)  # Friday, past cutoff, in rollover, outside windows, duplicate
    got = gate.evaluate("XAUUSD", ts, view(world, ts), spread=99.0)
    assert len(got.reasons) >= 4


def test_naive_as_of_refused(cfg, world):
    gate = build_gate(cfg)
    with pytest.raises(ValueError, match="timezone-aware"):
        gate.evaluate("XAUUSD", datetime(2024, 6, 10, 9), view(world, at(1, 9)))
