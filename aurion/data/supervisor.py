"""Ingest supervisor: backfill, gap repair, reconnect. `docs/01-architecture.md` §6-7.

Three rules, each from a specific failure mode:

1. **Reconnect never assumes continuity.** On every (re)connect the supervisor
   REST-backfills from the last stored bar before trusting the stream. A stream that
   resumes mid-gap would otherwise leave a hole that no later pass fills.
2. **Gaps are repaired, never interpolated.** A missing minute is fetched or stays missing.
   A synthesised bar would be indistinguishable from real data downstream.
3. **Derived state is recomputed on restart, not trusted.** Resamplers are rebuilt by
   replaying stored M1, and the recomputed bars must match what was stored for the
   overlapping region. A mismatch is a hard health alert, because it means a
   non-deterministic analysis path exists.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aurion.config.models import AurionConfig
from aurion.core.bus import EventBus
from aurion.core.clock import Clock, WallClock
from aurion.core.events import (
    AdapterState,
    AdapterStateChanged,
    BarClosed,
    FeedStale,
    GapDetected,
    M1Ingested,
    QuoteUpdate,
)
from aurion.core.timeframe import Bucketer, TimeFrame
from aurion.data.adapters.base import AdapterError, DataAdapter
from aurion.data.repository import InMemoryBarRepository
from aurion.data.resample import ResamplerSet
from aurion.data.store import Store
from aurion.data.types import Heartbeat, M1Candle, Quote

_log = logging.getLogger(__name__)

#: Slice width for a backfill. Bounded memory and resumable progress, not a tunable:
#: `docs/01-architecture.md` §8 budgets the box at 1-2 GB and a whole warm-up window of
#: M1 does not fit in one in-memory page list.
COLD_START_SLICE_DAYS = 7


@dataclass(slots=True)
class IngestStats:
    candles_ingested: int = 0
    bars_closed: int = 0
    gaps_repaired: int = 0
    reconnects: int = 0
    duplicates_dropped: int = 0
    already_stored: int = 0
    determinism_mismatches: int = 0
    last_candle_at: dict[str, datetime] = field(default_factory=dict)


class IngestSupervisor:
    """Owns the M1 pipeline for all configured instruments."""

    def __init__(
        self,
        cfg: AurionConfig,
        adapter: DataAdapter,
        store: Store,
        repo: InMemoryBarRepository,
        bus: EventBus | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._cfg = cfg
        self._adapter = adapter
        self._store = store
        self._repo = repo
        self._bus = bus
        # Injected, never `datetime.now()` inline: the supervisor must behave identically
        # under SimClock during replay (docs/01-architecture.md §4).
        self._clock: Clock = clock or WallClock()
        self._timeframes = tuple(TimeFrame(t) for t in cfg.data.timeframes)
        self._anchors = cfg.anchors()
        self._resamplers: dict[str, ResamplerSet] = {
            sym: ResamplerSet(
                sym,
                self._timeframes,
                self._anchors,
                min_constituents_frac=cfg.data.min_constituents_frac,
            )
            for sym in cfg.enabled_instruments
        }
        self.stats = IngestStats()
        self._backfilling: set[str] = set()
        self._stop = asyncio.Event()

    @property
    def instruments(self) -> tuple[str, ...]:
        return tuple(self._cfg.enabled_instruments)

    def resamplers(self, instrument: str) -> ResamplerSet:
        return self._resamplers[instrument]

    # -- ingest ------------------------------------------------------------
    async def _publish(self, event: object) -> None:
        if self._bus is not None:
            await self._bus.publish(event)

    async def ingest(self, candles: Sequence[M1Candle], persist: bool = True) -> list[BarClosed]:
        """Push candles through store, hot window and resamplers, in that order."""
        closed: list[BarClosed] = []
        for candle in candles:
            try:
                self._repo.add_m1(candle)
            except ValueError:
                # Already in the hot window. Backfill overlaps by one bar at every seam by
                # design, so this is expected rather than exceptional. Counted once, and
                # the store write is skipped because it would be a no-op round trip.
                self.stats.duplicates_dropped += 1
                continue
            if persist:
                written = await self._store.upsert_m1([candle])
                if written == 0:
                    # In the store but not in the hot window: a restart or a prune. Still
                    # resampled, because the hot window is being rebuilt.
                    self.stats.already_stored += 1
            self.stats.candles_ingested += 1
            self.stats.last_candle_at[candle.instrument] = candle.close_time
            await self._publish(M1Ingested(candle))

            for bar in self._resamplers[candle.instrument].push(candle):
                self._repo.add_bar(bar)
                if persist:
                    await self._store.upsert_bars([bar])
                self.stats.bars_closed += 1
                event = BarClosed(bar)
                closed.append(event)
                await self._publish(event)
        return closed

    async def close_stale_bars(self, instrument: str, now: datetime) -> list[BarClosed]:
        """Flush bars whose close has passed, e.g. the last bar before a market close.

        Without this, every Friday's final bar on each timeframe is never emitted.
        """
        out: list[BarClosed] = []
        for bar in self._resamplers[instrument].advance_to(now):
            self._repo.add_bar(bar)
            await self._store.upsert_bars([bar])
            self.stats.bars_closed += 1
            event = BarClosed(bar)
            out.append(event)
            await self._publish(event)
        return out

    # -- backfill ----------------------------------------------------------
    async def backfill(
        self, instrument: str, start: datetime, end: datetime, persist: bool = True
    ) -> int:
        candles = await self._adapter.fetch_candles(instrument, "M1", start, end)
        if not candles:
            return 0
        last = self.stats.last_candle_at.get(instrument)
        if last is not None:
            candles = [c for c in candles if c.open_time >= last - timedelta(minutes=1)]
        await self.ingest(candles, persist=persist)
        await self._store.set_cursor(instrument, last_bar_open_time=candles[-1].open_time)
        return len(candles)

    async def repair_gaps(self, instrument: str, start: datetime, end: datetime) -> int:
        """Fetch every hole in the stored M1 series.

        Market closures appear as gaps too. Refetching a closure is cheap and idempotent,
        so the supervisor does not try to distinguish them -- guessing which gaps are
        "legitimate" is how a real feed failure gets ignored.
        """
        gaps = await self._store.m1_gaps(instrument, start, end)
        if not gaps:
            return 0
        repaired = 0
        earliest = min(g[0] for g in gaps)
        for gap_start, gap_end in gaps:
            await self._publish(
                GapDetected(
                    instrument,
                    gap_start,
                    gap_end,
                    int((gap_end - gap_start) // timedelta(minutes=1)),
                )
            )
            got = await self._adapter.fetch_candles(instrument, "M1", gap_start, gap_end)
            if got:
                # Straight to the store. The hot window is append-only and has already
                # moved past these timestamps, and every resampled bar covering the gap was
                # aggregated from an incomplete set -- so those are dropped and rebuilt
                # rather than patched.
                await self._store.upsert_m1(got)
                repaired += len(got)
                self.stats.gaps_repaired += 1
        if repaired:
            # Delete from the BUCKET CONTAINING the earliest gap, per timeframe, not from
            # the gap instant: an M5 bar opening at 09:10 covers a gap starting at 09:11,
            # and deleting from 09:11 would leave that thin bar in place forever.
            for tf in self._timeframes:
                if tf.is_base:
                    continue
                aligned = Bucketer(tf, self._anchors.get(tf)).bucket(earliest).open_time
                await self._store.delete_bars_from(instrument, aligned, tf)
            self._repo.clear(instrument)
            self.stats.last_candle_at.pop(instrument, None)
            await self.rehydrate(
                instrument,
                verify_determinism=False,
                persist_bars=True,
                since=await self._store.first_m1_open(instrument),
            )
        return repaired

    # -- restart -----------------------------------------------------------
    async def rehydrate(
        self,
        instrument: str,
        verify_determinism: bool = True,
        persist_bars: bool = False,
        since: datetime | None = None,
    ) -> int:
        """Rebuild the hot window and resamplers by replaying stored M1.

        Derived bars are recomputed rather than loaded. When ``verify_determinism`` is set,
        the recomputed bars are compared against the stored ones; a mismatch means a
        non-deterministic path exists and is raised as a health alert rather than tolerated.
        """
        # Default: the live restart path, bounded by retention to keep the hot window
        # small. A caller that must replay a specific range -- gap repair -- passes
        # `since` explicitly, because bounding a REBUILD by wall-clock retention would
        # silently rebuild nothing for any data older than the window.
        if since is None:
            since = self._clock.now() - timedelta(days=self._cfg.data.m1_retention_days)
        candles = await self._store.load_m1(instrument, start=since)
        if not candles:
            return 0

        stored_bars = {
            (b.timeframe, b.open_time): b
            for tf in self._timeframes
            if not tf.is_base
            for b in await self._store.load_bars(instrument, tf)
        }

        self._resamplers[instrument] = ResamplerSet(
            instrument, self._timeframes, min_constituents_frac=self._cfg.data.min_constituents_frac
        )
        for candle in candles:
            self._repo.add_m1(candle)
            self.stats.last_candle_at[instrument] = candle.close_time
            for bar in self._resamplers[instrument].push(candle):
                self._repo.add_bar(bar)
                if persist_bars:
                    await self._store.upsert_bars([bar], replace=True)
                prior = stored_bars.get((bar.timeframe, bar.open_time))
                if verify_determinism and prior is not None and prior != bar:
                    self.stats.determinism_mismatches += 1
                    _log.error(
                        "recomputed bar differs from stored bar",
                        extra={
                            "instrument": instrument,
                            "timeframe": str(bar.timeframe),
                            "open_time": bar.open_time.isoformat(),
                        },
                    )
        self.stats.candles_ingested += len(candles)
        return len(candles)

    # -- streaming ---------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    async def run_stream(self, max_reconnects: int | None = None) -> None:
        """Stream with auto-reconnect, backfilling the gap on every reconnect."""
        backoff = list(self._cfg.adapter.reconnect_backoff_s) or [1.0]
        attempt = 0
        while not self._stop.is_set():
            if max_reconnects is not None and attempt > max_reconnects:
                return
            try:
                await self._publish(
                    AdapterStateChanged(
                        self._adapter.name, AdapterState.CONNECTED, self._clock.now()
                    )
                )
                await self._backfill_all_to_now()
                async for message in self._adapter.stream_prices(self.instruments):
                    if self._stop.is_set():
                        return
                    await self._on_stream_message(message)
                attempt = 0
            except (AdapterError, OSError) as exc:
                attempt += 1
                self.stats.reconnects += 1
                await self._publish(
                    AdapterStateChanged(
                        self._adapter.name, AdapterState.RECONNECTING,
                        self._clock.now(), str(exc),
                    )
                )
                delay = backoff[min(attempt - 1, len(backoff) - 1)]
                # Jitter so several instruments do not reconnect in lockstep.
                await asyncio.sleep(delay * (0.5 + random.random()))
            else:
                if max_reconnects is not None:
                    attempt += 1

    async def _on_stream_message(self, message: Quote | Heartbeat) -> None:
        if isinstance(message, Quote):
            self._repo.add_quote(message)
            await self._publish(QuoteUpdate(message))
        else:
            await self._store.set_cursor(
                self.instruments[0], last_stream_heartbeat=message.time
            )

    async def _backfill_all_to_now(self) -> None:
        now = self._clock.now()
        for instrument in self.instruments:
            last = await self._store.last_m1_open(instrument)
            if last is not None:
                # Reconnect: fetch the gap and nothing more.
                await self._backfill_sliced(instrument, last + timedelta(minutes=1), now)
                continue
            # Cold database. One day of history was the old behaviour here and it was
            # silently fatal: the W1 bias gate needs two confirmed weekly swings per side,
            # so with a day of bars the cascade rejects EVERY evaluation at
            # `bias:htf_structure_unavailable` -- not for a while, but for as long as the
            # process runs, with no error anywhere. A first start needs the warm-up window,
            # which is the same window the VPS is sized to retain.
            start = now - timedelta(days=self._cfg.data.m1_retention_days)
            _log.info(
                "cold start: backfilling %s days of M1 for %s in %s-day slices",
                self._cfg.data.m1_retention_days,
                instrument,
                COLD_START_SLICE_DAYS,
            )
            await self._backfill_sliced(instrument, start, now)

    def is_backfilling(self, instrument: str) -> bool:
        """Whether a backfill is in flight. Health checks ask before judging a feed."""
        return instrument in self._backfilling

    async def _backfill_sliced(self, instrument: str, start: datetime, end: datetime) -> int:
        """Backfill in slices, persisting each before fetching the next.

        `fetch_candles` paginates but accumulates every page in memory before returning, and
        the warm-up window is ~259k M1 candles per instrument. On the 1-2 GB target box that
        is the difference between a working cold start and an OOM kill. Slicing also means an
        interrupted backfill resumes from the stored cursor instead of starting over.
        """
        total = 0
        cursor = start
        self._backfilling.add(instrument)
        try:
            while cursor < end:
                slice_end = min(cursor + timedelta(days=COLD_START_SLICE_DAYS), end)
                total += await self.backfill(instrument, cursor, slice_end)
                cursor = slice_end
        finally:
            # In a finally so a failed backfill does not leave the instrument permanently
            # exempt from staleness checks, which would hide a genuinely dead feed.
            self._backfilling.discard(instrument)
        return total

    # -- health ------------------------------------------------------------
    def staleness(self, instrument: str, now: datetime) -> float | None:
        last = self.stats.last_candle_at.get(instrument)
        return None if last is None else (now - last).total_seconds()

    async def check_staleness(self, now: datetime) -> list[FeedStale]:
        """Instruments whose newest bar is older than the limit, excluding ones backfilling.

        `last_candle_at` is the close time of the newest bar ingested, so during a cold
        start it sits ~180 days in the past and every check reports the feed as stale by
        that whole margin. The first run of this alerted at 15,367,137 seconds against a
        120-second limit, which is the backfill working exactly as intended.
        """
        out: list[FeedStale] = []
        limit = self._cfg.health.max_bar_staleness_s
        for instrument in self.instruments:
            if instrument in self._backfilling:
                continue
            age = self.staleness(instrument, now)
            last = self.stats.last_candle_at.get(instrument)
            if age is not None and last is not None and age > limit:
                event = FeedStale(instrument, last, age)
                out.append(event)
                await self._publish(event)
        return out
