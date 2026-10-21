"""Store persistence, gap detection, and MarketView as-of filtering."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from aurion.core.marketview import MarketView
from aurion.core.timeframe import UTC, TimeFrame
from aurion.data.repository import InMemoryBarRepository
from aurion.data.store import Store
from aurion.data.types import OHLC, Bar, Quote
from tests.conftest import make_m1

T0 = datetime(2024, 6, 12, 9, 0, tzinfo=UTC)


@pytest.fixture
async def store():
    s = Store("sqlite+aiosqlite:///:memory:")
    await s.create_all()
    yield s
    await s.dispose()


def m1_range(indices):
    return [make_m1("EURUSD", T0 + timedelta(minutes=i), 1.1000 + i * 1e-4) for i in indices]


async def test_m1_upsert_is_idempotent(store):
    candles = m1_range(range(10))
    assert await store.upsert_m1(candles) == 10
    assert await store.upsert_m1(candles) == 0, "backfill overlaps by design"
    assert len(await store.load_m1("EURUSD")) == 10


async def test_loaded_timestamps_are_timezone_aware(store):
    await store.upsert_m1(m1_range(range(3)))
    loaded = await store.load_m1("EURUSD")
    assert all(c.open_time.tzinfo is not None for c in loaded)
    assert loaded[0].open_time == T0


async def test_store_persists_verbatim_so_recomputed_bars_match_stored_ones(store):
    """Rounding on write made a recomputed bar differ from its stored twin.

    That broke the determinism check in rehydrate, which is how the bug was found.
    Normalisation now happens once at the adapter boundary; the store is verbatim.
    """
    candle = make_m1("XAUUSD", T0, 2318.40491, high_off=0.8, low_off=0.6, close_off=0.2,
                     spread=0.2)
    await store.upsert_m1([candle])
    loaded = (await store.load_m1("XAUUSD"))[0]
    assert loaded.bid == candle.bid and loaded.ask == candle.ask


async def test_gap_detection(store):
    await store.upsert_m1(m1_range(list(range(5)) + list(range(8, 15))))
    gaps = await store.m1_gaps("EURUSD", T0, T0 + timedelta(minutes=20))
    assert gaps == [(T0 + timedelta(minutes=5), T0 + timedelta(minutes=8))]


async def test_retention_pruning(store):
    await store.upsert_m1(m1_range(range(20)))
    pruned = await store.prune_m1("EURUSD", T0 + timedelta(minutes=10))
    assert pruned == 10
    assert len(await store.load_m1("EURUSD")) == 10


async def test_cursor_roundtrip(store):
    await store.set_cursor("EURUSD", last_bar_open_time=T0)
    await store.set_cursor("EURUSD", last_stream_heartbeat=T0 + timedelta(seconds=5))
    cursor = await store.get_cursor("EURUSD")
    assert cursor is not None
    assert cursor.last_bar_open_time.replace(tzinfo=UTC) == T0
    assert cursor.last_stream_heartbeat is not None, "a second write must not clear the first"


async def test_bar_roundtrip_preserves_flags(store):
    bar = Bar("EURUSD", TimeFrame.H4, T0, T0 + timedelta(hours=4),
              OHLC(1.1, 1.2, 1.0, 1.15), 500, 180, incomplete=True, dst_irregular=True)
    await store.upsert_bars([bar])
    loaded = (await store.load_bars("EURUSD", TimeFrame.H4))[0]
    assert loaded.incomplete and loaded.dst_irregular
    assert loaded.timeframe is TimeFrame.H4
    assert not loaded.usable


async def test_last_m1_open(store):
    await store.upsert_m1(m1_range(range(7)))
    assert await store.last_m1_open("EURUSD") == T0 + timedelta(minutes=6)
    assert await store.last_m1_open("GBPUSD") is None


# -- MarketView --------------------------------------------------------------
def bars_repo(n=10, thin_at=None):
    repo = InMemoryBarRepository()
    for i in range(n):
        o = T0 + timedelta(minutes=5 * i)
        repo.add_bar(
            Bar("EURUSD", TimeFrame.M5, o, o + timedelta(minutes=5),
                OHLC(1.1, 1.1 + 1e-3 * (i + 1), 1.1 - 1e-3, 1.1 + 5e-4 * i),
                50, 5, incomplete=(i == thin_at))
        )
    return repo


def test_marketview_exposes_only_closed_bars():
    repo = bars_repo()
    view = MarketView(repo, T0 + timedelta(minutes=25))
    got = view.bars("EURUSD", TimeFrame.M5, 10)
    assert len(got) == 5
    assert all(b.close_time <= view.as_of for b in got)


def test_marketview_usable_bars_drops_thin_ones():
    view = MarketView(bars_repo(thin_at=2), T0 + timedelta(hours=2))
    assert len(view.bars("EURUSD", TimeFrame.M5, 10)) == 10
    assert len(view.usable_bars("EURUSD", TimeFrame.M5, 10)) == 9


def test_marketview_rejects_naive_as_of():
    with pytest.raises(ValueError, match="timezone-aware"):
        MarketView(bars_repo(), datetime(2024, 6, 12, 9, 0))


def test_marketview_rejects_non_positive_n():
    with pytest.raises(ValueError, match="positive"):
        MarketView(bars_repo(), T0 + timedelta(hours=2)).bars("EURUSD", TimeFrame.M5, 0)


def test_forming_bar_requires_explicit_opt_in():
    view = MarketView(bars_repo(), T0 + timedelta(hours=2))
    with pytest.raises(PermissionError, match="allow_forming"):
        view.partial("EURUSD", TimeFrame.M5)


def test_stability_property_s_confirmed_objects_never_change():
    repo = bars_repo()
    early = MarketView(repo, T0 + timedelta(minutes=25)).bars("EURUSD", TimeFrame.M5, 1)[-1]
    later = next(
        b
        for b in MarketView(repo, T0 + timedelta(hours=2)).bars("EURUSD", TimeFrame.M5, 20)
        if b.open_time == early.open_time
    )
    assert early == later


def test_quotes_are_also_as_of_filtered():
    repo = bars_repo()
    repo.add_quote(Quote("EURUSD", T0 + timedelta(minutes=1), 1.1, 1.1002))
    repo.add_quote(Quote("EURUSD", T0 + timedelta(minutes=99), 9.9, 9.9002))
    view = MarketView(repo, T0 + timedelta(minutes=30))
    quote = view.last_quote("EURUSD")
    assert quote is not None and quote.bid == pytest.approx(1.1)


def test_out_of_order_appends_are_rejected():
    repo = bars_repo(3)
    with pytest.raises(ValueError, match="out-of-order"):
        repo.add_bar(Bar("EURUSD", TimeFrame.M5, T0, T0 + timedelta(minutes=5),
                         OHLC(1.1, 1.2, 1.0, 1.15), 50, 5))


def test_hot_window_retention_keeps_the_tail():
    repo = InMemoryBarRepository(max_bars_per_series=5)
    for i in range(20):
        o = T0 + timedelta(minutes=5 * i)
        repo.add_bar(Bar("EURUSD", TimeFrame.M5, o, o + timedelta(minutes=5),
                         OHLC(1.1, 1.2, 1.0, 1.15), 50, 5))
    got = repo.bars("EURUSD", TimeFrame.M5, 100, T0 + timedelta(days=1))
    assert len(got) == 5
    assert got[-1].open_time == T0 + timedelta(minutes=95)


def test_objects_accessor_defaults_to_empty_until_phase_3():
    view = MarketView(bars_repo(), T0 + timedelta(hours=2))
    assert list(view.objects("fvg", "EURUSD", TimeFrame.M5)) == []
