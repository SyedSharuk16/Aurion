"""Event bus ordering, clock monotonicity, and the event contract."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest

from aurion.core.bus import EventBus
from aurion.core.clock import Clock, SimClock, WallClock
from aurion.core.events import (
    AdapterState,
    AdapterStateChanged,
    BarClosed,
    FeedStale,
    GapDetected,
    M1Ingested,
    QuoteUpdate,
)
from aurion.core.timeframe import UTC, TimeFrame
from aurion.data.types import OHLC, Bar, Quote
from tests.conftest import make_m1

T0 = datetime(2024, 6, 12, 9, 0, tzinfo=UTC)


def a_bar(i: int = 0) -> Bar:
    o = T0 + timedelta(minutes=5 * i)
    return Bar("EURUSD", TimeFrame.M5, o, o + timedelta(minutes=5),
               OHLC(1.1, 1.2, 1.0, 1.15), 50, 5)


# -- clocks ------------------------------------------------------------------
def test_wallclock_is_utc_aware():
    now = WallClock().now()
    assert now.tzinfo is not None
    assert isinstance(WallClock(), Clock)


def test_simclock_advances_and_refuses_to_go_backwards():
    """A backwards step is how replay silently reveals future data."""
    clock = SimClock(T0)
    assert clock.now() == T0
    clock.advance_to(T0 + timedelta(minutes=5))
    assert clock.now() == T0 + timedelta(minutes=5)
    with pytest.raises(ValueError, match="cannot move backwards"):
        clock.advance_to(T0)


def test_simclock_requires_aware_datetimes():
    with pytest.raises(ValueError, match="timezone-aware"):
        SimClock(datetime(2024, 6, 12, 9, 0))
    with pytest.raises(ValueError, match="timezone-aware"):
        SimClock(T0).advance_to(datetime(2024, 6, 12, 10, 0))


def test_simclock_normalises_to_utc():
    from zoneinfo import ZoneInfo

    clock = SimClock(datetime(2024, 6, 12, 5, 0, tzinfo=ZoneInfo("America/New_York")))
    assert clock.now().utcoffset() == timedelta(0)


# -- events ------------------------------------------------------------------
def test_bar_closed_exposes_as_of_at_the_close():
    bar = a_bar()
    assert BarClosed(bar).as_of == bar.close_time == bar.confirmed_at


def test_event_types_are_frozen():
    event = QuoteUpdate(Quote("EURUSD", T0, 1.1, 1.1002))
    with pytest.raises((AttributeError, TypeError)):
        event.quote = None  # type: ignore[misc]


def test_event_constructors():
    assert M1Ingested(make_m1("EURUSD", T0, 1.1)).candle.instrument == "EURUSD"
    assert AdapterStateChanged("oanda", AdapterState.RECONNECTING, T0).state == "reconnecting"
    assert FeedStale("EURUSD", T0, 180.0).age_seconds == 180.0
    gap = GapDetected("EURUSD", T0, T0 + timedelta(minutes=3), 3)
    assert gap.missing_minutes == 3


# -- bus ---------------------------------------------------------------------
async def test_handlers_receive_events_in_submission_order():
    """Ordering is a correctness property: otherwise the engine can observe a spread from
    t+1 while evaluating t."""
    bus = EventBus()
    seen: list[int] = []

    async def handler(event: BarClosed) -> None:
        seen.append(event.bar.open_time.minute)

    bus.subscribe(BarClosed, handler)
    for i in range(5):
        await bus.publish(BarClosed(a_bar(i)))
    await bus.drain()
    assert seen == [0, 5, 10, 15, 20]


async def test_multiple_handlers_all_fire():
    bus = EventBus()
    calls: list[str] = []

    async def first(_e: BarClosed) -> None:
        calls.append("first")

    async def second(_e: BarClosed) -> None:
        calls.append("second")

    bus.subscribe(BarClosed, first)
    bus.subscribe(BarClosed, second)
    await bus.publish(BarClosed(a_bar()))
    await bus.drain()
    assert calls == ["first", "second"]


async def test_unsubscribed_event_types_are_ignored():
    bus = EventBus()
    await bus.publish(QuoteUpdate(Quote("EURUSD", T0, 1.1, 1.1002)))
    await bus.drain()
    assert bus.processed == 1


async def test_a_raising_handler_does_not_stop_the_loop(caplog):
    """One bad handler must not drop later events; the supervisor surfaces it instead."""
    bus = EventBus()
    delivered: list[int] = []

    async def bad(_e: BarClosed) -> None:
        raise RuntimeError("boom")

    async def good(event: BarClosed) -> None:
        delivered.append(event.bar.open_time.minute)

    bus.subscribe(BarClosed, bad)
    bus.subscribe(BarClosed, good)
    with caplog.at_level(logging.ERROR):
        await bus.publish(BarClosed(a_bar(0)))
        await bus.publish(BarClosed(a_bar(1)))
        await bus.drain()
    assert delivered == [0, 5]
    assert "handler failed" in caplog.text


async def test_run_and_stop_lifecycle():
    import asyncio

    bus = EventBus()
    seen: list[int] = []

    async def handler(event: BarClosed) -> None:
        seen.append(event.bar.open_time.minute)

    bus.subscribe(BarClosed, handler)
    bus.start()
    await bus.publish(BarClosed(a_bar(3)))
    for _ in range(50):
        if seen:
            break
        await asyncio.sleep(0.005)
    await bus.stop()
    assert seen == [15]
