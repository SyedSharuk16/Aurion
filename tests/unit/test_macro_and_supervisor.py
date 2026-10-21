"""Macro point-in-time semantics and the ingest supervisor."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import httpx
import pytest

from aurion.core.bus import EventBus
from aurion.core.clock import SimClock
from aurion.core.events import BarClosed, GapDetected
from aurion.core.timeframe import UTC, TimeFrame
from aurion.data.adapters.base import AdapterError
from aurion.data.macro import FredProvider, MacroCache, MacroObs, StaticMacroProvider
from aurion.data.repository import InMemoryBarRepository
from aurion.data.store import Store
from aurion.data.supervisor import IngestSupervisor
from tests.conftest import make_m1

T0 = datetime(2024, 6, 12, 9, 0, tzinfo=UTC)


# -- macro -------------------------------------------------------------------
def obs(series: str, day: int, value: float, published_day: int | None = None) -> MacroObs:
    d = date(2024, 6, day)
    pub = datetime(2024, 6, published_day or (day + 1), tzinfo=UTC)
    return MacroObs(series, d, value, pub)


def test_fred_parses_and_skips_the_missing_marker():
    payload = {
        "observations": [
            {"date": "2024-06-10", "value": "4.28", "realtime_start": "2024-06-11"},
            {"date": "2024-06-11", "value": ".", "realtime_start": "2024-06-12"},
            {"date": "2024-06-12", "value": "4.31", "realtime_start": "2024-06-13"},
        ]
    }
    got = FredProvider._parse("DGS10", payload)
    assert [o.value for o in got] == [4.28, 4.31]
    assert got[0].published_at == datetime(2024, 6, 11, tzinfo=UTC)


def test_fred_without_realtime_publishes_the_next_day():
    """A daily yield close is not readable during the day it describes."""
    got = FredProvider._parse("DGS10", {"observations": [{"date": "2024-06-10", "value": "4.28"}]})
    assert got[0].published_at.date() == date(2024, 6, 11)


def test_fred_missing_observations_key_is_loud():
    with pytest.raises(AdapterError, match="no 'observations'"):
        FredProvider._parse("DGS10", {"error_message": "bad key"})


async def test_fred_requests_a_vintage_for_point_in_time():
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"observations": []})

    provider = FredProvider("key", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await provider.series(
        "DFII10", date(2024, 1, 1), date(2024, 6, 1), datetime(2024, 3, 15, tzinfo=UTC)
    )
    assert seen["realtime_start"] == "2024-03-15" == seen["realtime_end"]
    await provider.aclose()


async def test_fred_http_error_is_loud():
    provider = FredProvider(
        "key", client=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(400, text="bad request")))
    )
    with pytest.raises(AdapterError, match="HTTP 400"):
        await provider.series("DGS10", date(2024, 1, 1), date(2024, 6, 1))
    await provider.aclose()


async def test_static_provider_filters_on_publication():
    p = StaticMacroProvider([obs("DGS10", 10, 4.28), obs("DGS10", 12, 4.31)])
    early = await p.series("DGS10", date(2024, 6, 1), date(2024, 6, 30),
                           datetime(2024, 6, 12, tzinfo=UTC))
    assert [o.observation_date.day for o in early] == [10]


def test_cache_is_a_step_function_and_never_interpolates():
    """§10.3a forbids inventing an intraday macro series."""
    cache = MacroCache()
    cache.extend([obs("DGS10", 10, 4.28), obs("DGS10", 12, 4.31)])
    midday = datetime(2024, 6, 12, 12, tzinfo=UTC)
    assert cache.value("DGS10", midday) == pytest.approx(4.28), "the 06-12 close is not yet out"
    assert cache.value("DGS10", datetime(2024, 6, 13, 1, tzinfo=UTC)) == pytest.approx(4.31)
    assert cache.value("DGS10", datetime(2024, 6, 1, tzinfo=UTC)) is None


def test_cache_change_uses_visible_vintages_only():
    cache = MacroCache()
    for day, value in ((3, 4.20), (4, 4.22), (10, 4.28), (11, 4.30)):
        cache.add(obs("DGS10", day, value))
    as_of = datetime(2024, 6, 12, tzinfo=UTC)
    assert cache.change("DGS10", as_of, 1) == pytest.approx(4.30 - 4.28)
    assert cache.change("DGS10", as_of, 20) is None, "no visible observation that far back"
    assert cache.series_ids() == ("DGS10",)


def test_real_yield_series_is_named_honestly():
    from aurion.data.macro import FRED_SERIES

    assert FRED_SERIES["US10Y_real_d"] == "DFII10"
    assert "proxy" not in "".join(FRED_SERIES)


# -- supervisor --------------------------------------------------------------
class FakeAdapter:
    """Serves a prepared M1 series and counts fetches."""

    name = "fake"

    def __init__(self, candles: list) -> None:
        self._candles = candles
        self.fetches: list[tuple[datetime, datetime]] = []

    def to_vendor_symbol(self, instrument: str) -> str:
        return instrument

    def to_canonical(self, vendor_symbol: str) -> str:
        return vendor_symbol

    async def fetch_candles(self, instrument, granularity="M1", start_utc=None, end_utc=None):
        self.fetches.append((start_utc, end_utc))
        return [
            c for c in self._candles
            if c.instrument == instrument and start_utc <= c.open_time < end_utc
        ]

    def stream_prices(self, instruments):  # pragma: no cover - not used here
        raise NotImplementedError

    async def latest_quote(self, instrument):  # pragma: no cover
        raise NotImplementedError

    async def health(self):  # pragma: no cover
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


@pytest.fixture
async def rig(cfg):
    store = Store("sqlite+aiosqlite:///:memory:")
    await store.create_all()
    repo = InMemoryBarRepository()
    candles = [make_m1("EURUSD", T0 + timedelta(minutes=i), 1.1 + i * 1e-4) for i in range(40)]
    adapter = FakeAdapter(candles)
    bus = EventBus()
    sup = IngestSupervisor(cfg, adapter, store, repo, bus)
    yield sup, store, repo, adapter, bus, candles
    await store.dispose()


async def test_ingest_stores_resamples_and_publishes(rig):
    sup, store, _repo, _, bus, candles = rig
    closed_events: list[BarClosed] = []
    bus.subscribe(BarClosed, lambda e: _collect(closed_events, e))
    out = await sup.ingest(candles)
    await bus.drain()
    assert sup.stats.candles_ingested == 40
    assert len(await store.load_m1("EURUSD")) == 40
    m5 = [e.bar for e in out if e.bar.timeframe is TimeFrame.M5]
    assert len(m5) == 7  # 40 minutes -> seven closed M5 bars, the eighth still forming
    assert len(closed_events) == len(out)


async def _collect(sink: list, event: object) -> None:
    sink.append(event)


async def test_duplicate_ingest_is_counted_once_not_twice(rig):
    sup, _, _, _, _, candles = rig
    await sup.ingest(candles)
    await sup.ingest(candles)
    assert sup.stats.duplicates_dropped == 40, "each duplicate counts once, not per store"
    assert sup.stats.candles_ingested == 40


async def test_backfill_sets_the_cursor(rig):
    sup, store, _, _, _, _ = rig
    n = await sup.backfill("EURUSD", T0, T0 + timedelta(minutes=40))
    assert n == 40
    cursor = await store.get_cursor("EURUSD")
    assert cursor is not None and cursor.last_bar_open_time is not None


async def test_gap_repair_rebuilds_the_invalidated_resampled_bars(rig):
    """Repair cannot be an append.

    The hot window is append-only and has already moved past the gap, and every resampled
    bar covering the gap was aggregated from an incomplete set. So repair writes to the
    store, drops those bars, and replays -- which is also what makes the thin M5 bar over
    the hole come back complete.
    """
    sup, store, _repo, _, bus, candles = rig
    gaps_seen: list[GapDetected] = []
    bus.subscribe(GapDetected, lambda e: _collect(gaps_seen, e))
    # A PARTIAL hole inside one M5 bucket. A hole covering a whole bucket produces no bar
    # at all (empty buckets are never emitted), which is a different case.
    await sup.ingest([c for c in candles if c.open_time.minute not in (11, 12, 13)])

    before = [
        b for b in await store.load_bars("EURUSD", TimeFrame.M5)
        if b.open_time.minute == 10
    ]
    assert before and before[0].incomplete, "the bar over the hole must be flagged thin"

    repaired = await sup.repair_gaps("EURUSD", T0, T0 + timedelta(minutes=40))
    await bus.drain()

    assert repaired == 3
    assert gaps_seen and gaps_seen[0].missing_minutes == 3
    assert await store.m1_gaps("EURUSD", T0, T0 + timedelta(minutes=40)) == []
    after = [
        b for b in await store.load_bars("EURUSD", TimeFrame.M5)
        if b.open_time.minute == 10
    ]
    assert after and not after[0].incomplete, "the repaired bar must no longer be thin"
    assert after[0].constituents == 5


async def test_close_stale_bars_flushes_the_final_bar(rig):
    """Without this every Friday's last bar on each timeframe is lost."""
    sup, _, _, _, _, candles = rig
    await sup.ingest(candles[:38])
    flushed = await sup.close_stale_bars("EURUSD", T0 + timedelta(hours=2))
    assert any(e.bar.timeframe is TimeFrame.H1 for e in flushed)


async def test_rehydrate_recomputes_bars_and_verifies_determinism(rig):
    sup, store, _repo, _, _, candles = rig
    await sup.ingest(candles)
    stored_before = len(await store.load_bars("EURUSD", TimeFrame.M5))
    fresh_repo = InMemoryBarRepository()
    # SimClock anchored to the data, which is what makes retention deterministic and the
    # whole path testable; a wall-clock now() would silently filter the fixture out.
    fresh = IngestSupervisor(
        sup._cfg, sup._adapter, store, fresh_repo,
        clock=SimClock(candles[-1].close_time),
    )  # SimClock anchors retention to the data instead of to wall-clock time
    n = await fresh.rehydrate("EURUSD")
    assert n == 40
    assert fresh.stats.determinism_mismatches == 0, "resampling must be deterministic"
    assert len(fresh_repo.bars("EURUSD", TimeFrame.M5, 50, T0 + timedelta(days=1))) == stored_before


async def test_staleness_detection(rig):
    sup, _, _, _, _bus, candles = rig
    await sup.ingest(candles)
    fresh = await sup.check_staleness(candles[-1].close_time + timedelta(seconds=30))
    stale = await sup.check_staleness(candles[-1].close_time + timedelta(minutes=10))
    assert fresh == []
    assert stale and stale[0].instrument == "EURUSD" and stale[0].age_seconds > 120


async def test_staleness_is_none_before_any_candle(cfg):
    store = Store("sqlite+aiosqlite:///:memory:")
    await store.create_all()
    sup = IngestSupervisor(cfg, FakeAdapter([]), store, InMemoryBarRepository())
    assert sup.staleness("EURUSD", T0) is None
    assert await sup.check_staleness(T0) == []
    await store.dispose()
