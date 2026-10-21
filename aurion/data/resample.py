"""Incremental M1 -> higher-timeframe resampling.

Implements `docs/02-definitions.md` §2.2. Vendor-resampled bars are never used, for the
reason in §2.1 and one more: vendor H1/H4 bars are built on the vendor's own day
convention, so swapping adapters would silently change every structural object.

Four properties this module must have, in order of how badly their absence would corrupt
results:

1. **Bars are emitted only at close.** A partial bar is reachable solely through
   ``partial``, which `MarketView` exposes only under an explicit opt-in. The T4 causality
   test asserts ``confirmed_at == close_time`` for every emitted bar.
2. **Incremental, O(1) per M1 candle.** One running accumulator per timeframe. The target
   box is 1 vCPU, and recomputing a rolling window per bar would not fit.
3. **Thin buckets are flagged, not silently accepted.** A bucket with too few M1
   constituents is emitted with ``incomplete=True`` and excluded from analysis -- including
   from ATR -- rather than passed off as a normal bar.
4. **Empty buckets are never emitted.** No synthetic zero-volume bars across weekends or
   holidays. The weekend-gap logic in `sessions/gates.py` depends on the gap being real.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from aurion.core.timeframe import Anchor, Bucket, Bucketer, TimeFrame
from aurion.data.types import OHLC, Bar, M1Candle

_MINUTE = timedelta(minutes=1)
#: Trading minutes in a normal FX week: Sun 17:00 NY -> Fri 17:00 NY.
_WEEK_TRADING_MINUTES = 5 * 24 * 60


def expected_constituents(bucket: Bucket) -> int:
    """How many M1 bars a full bucket should contain.

    Intraday buckets are continuously open within the trading week, so the expectation is
    their wall duration in minutes. Daily and weekly buckets span closed periods, so a
    naive wall-duration expectation would mark every weekly bar thin: a W1 bucket is 10,080
    wall minutes but only ~7,200 trading minutes. The expectation is therefore the trading
    content, adjusted by whatever DST did to the bucket.
    """
    wall_minutes = int(bucket.wall_duration // _MINUTE)
    if bucket.timeframe is TimeFrame.W1:
        dst_adjust = wall_minutes - 7 * 24 * 60
        return _WEEK_TRADING_MINUTES + dst_adjust
    return wall_minutes


@dataclass(slots=True)
class _Accumulator:
    bucket: Bucket
    o: float
    h: float
    l: float  # noqa: E741 - matches the OHLC notation used throughout the docs
    c: float
    volume: int
    count: int
    last_open: datetime

    def add(self, candle: M1Candle) -> None:
        mid = candle.mid
        self.h = max(self.h, mid.h)
        self.l = min(self.l, mid.l)
        self.c = mid.c
        self.volume += candle.tick_volume
        self.count += 1
        self.last_open = candle.open_time


class IncrementalResampler:
    """Resamples one instrument to one timeframe.

    Usage is push-driven: feed M1 candles in time order; a closed `Bar` is returned at the
    moment a candle belonging to a later bucket arrives, or when `advance_to` is told that
    time has passed the current bucket's close.
    """

    __slots__ = ("_acc", "_bucketer", "_last_seen", "_min_frac", "instrument", "timeframe")

    def __init__(
        self,
        instrument: str,
        timeframe: TimeFrame,
        anchor: Anchor | None = None,
        min_constituents_frac: float = 0.5,
    ) -> None:
        if timeframe.is_base:
            raise ValueError("M1 is the base timeframe and is not resampled")
        self.instrument = instrument
        self.timeframe = timeframe
        self._bucketer = Bucketer(timeframe, anchor)
        self._acc: _Accumulator | None = None
        self._min_frac = min_constituents_frac
        self._last_seen: datetime | None = None

    # -- ingest ------------------------------------------------------------
    def push(self, candle: M1Candle) -> Bar | None:
        """Add an M1 candle. Returns the previous bar if this candle closed it."""
        if candle.instrument != self.instrument:
            raise ValueError(f"candle for {candle.instrument} pushed to {self.instrument}")
        if self._last_seen is not None and candle.open_time <= self._last_seen:
            # Duplicate or out-of-order. Backfill overlaps by one bar by design, so
            # duplicates are expected and dropped; a true regression is a feed bug and is
            # dropped too rather than corrupting the accumulator.
            return None
        self._last_seen = candle.open_time

        bucket = self._bucketer.bucket(candle.open_time)
        emitted: Bar | None = None

        if self._acc is not None and bucket.open_time != self._acc.bucket.open_time:
            emitted = self._close()
        if self._acc is None:
            mid = candle.mid
            self._acc = _Accumulator(
                bucket=bucket,
                o=mid.o,
                h=mid.h,
                l=mid.l,
                c=mid.c,
                volume=candle.tick_volume,
                count=1,
                last_open=candle.open_time,
            )
        else:
            self._acc.add(candle)
        return emitted

    def advance_to(self, ts: datetime) -> Bar | None:
        """Close the forming bar if ``ts`` has passed its close.

        Needed because the last bar before a market close has no following candle to push
        it out. Without this, every Friday's final bar would be lost.
        """
        if self._acc is not None and ts >= self._acc.bucket.close_time:
            return self._close()
        return None

    def _close(self) -> Bar:
        assert self._acc is not None
        acc = self._acc
        self._acc = None
        expected = expected_constituents(acc.bucket)
        incomplete = expected > 0 and acc.count < self._min_frac * expected
        return Bar(
            instrument=self.instrument,
            timeframe=self.timeframe,
            open_time=acc.bucket.open_time,
            close_time=acc.bucket.close_time,
            ohlc=OHLC(acc.o, acc.h, acc.l, acc.c),
            tick_volume=acc.volume,
            constituents=acc.count,
            incomplete=incomplete,
            dst_irregular=acc.bucket.dst_irregular,
        )

    # -- inspection --------------------------------------------------------
    @property
    def partial(self) -> Bar | None:
        """The forming bar. Never exposed except through ``MarketView.partial()``."""
        if self._acc is None:
            return None
        acc = self._acc
        return Bar(
            instrument=self.instrument,
            timeframe=self.timeframe,
            open_time=acc.bucket.open_time,
            close_time=acc.bucket.close_time,
            ohlc=OHLC(acc.o, acc.h, acc.l, acc.c),
            tick_volume=acc.volume,
            constituents=acc.count,
            incomplete=True,
            dst_irregular=acc.bucket.dst_irregular,
        )


class ResamplerSet:
    """All configured timeframes for one instrument, driven by one M1 stream."""

    __slots__ = ("_resamplers", "instrument")

    def __init__(
        self,
        instrument: str,
        timeframes: tuple[TimeFrame, ...],
        anchors: dict[TimeFrame, Anchor] | None = None,
        min_constituents_frac: float = 0.5,
    ) -> None:
        self.instrument = instrument
        anchors = anchors or {}
        self._resamplers = {
            tf: IncrementalResampler(instrument, tf, anchors.get(tf), min_constituents_frac)
            for tf in timeframes
            if not tf.is_base
        }

    def push(self, candle: M1Candle) -> list[Bar]:
        """Returns bars closed by this candle, ordered low timeframe to high."""
        out: list[Bar] = []
        for tf in sorted(self._resamplers, key=lambda t: t.duration):
            bar = self._resamplers[tf].push(candle)
            if bar is not None:
                out.append(bar)
        return out

    def advance_to(self, ts: datetime) -> list[Bar]:
        out: list[Bar] = []
        for tf in sorted(self._resamplers, key=lambda t: t.duration):
            bar = self._resamplers[tf].advance_to(ts)
            if bar is not None:
                out.append(bar)
        return out

    def partial(self, timeframe: TimeFrame) -> Bar | None:
        r = self._resamplers.get(timeframe)
        return r.partial if r is not None else None
