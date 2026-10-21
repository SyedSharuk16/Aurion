"""Timeframes and bar bucketing.

Implements `docs/02-definitions.md` §2.2 (resampling rule) and §2.3 (anchors).

The bucketer is the highest-risk component in the system: every structural object, every
array and every level is defined on bars it produces, so an off-by-one-hour error here
silently changes every downstream result. Two decisions are therefore made explicitly.

**Bucket boundaries are generated in the anchor's LOCAL time, not by absolute-time stepping
from a fixed instant.** For H4 anchored at 17:00 America/New_York this puts boundaries at
local 17:00, 21:00, 01:00, 05:00, 09:00 and 13:00 every day, which is what the session
labels mean and what a practitioner expects to see on a chart. The cost is that DST
transition days are irregular: the bar spanning the spring-forward hour is 3 hours of wall
time and the bar spanning the fall-back hour is 5. Those bars carry ``dst_irregular=True``
rather than being silently normalised. Absolute-time stepping would keep every bar exactly
4 hours and instead drift the local labels by an hour for half the year, which is the worse
trade for session-based logic.

**Bars are left-labelled by ``open_time`` but may only be exposed at ``close_time``.** A
left-labelled bar treated as known at its label is the most common source of future leakage
in resampled data; see `MarketView` and the T4 causality test.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum
from functools import lru_cache
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")


class TimeFrame(StrEnum):
    """Supported timeframes. M1 is the base; everything else is resampled locally."""

    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"
    W1 = "W1"

    @property
    def duration(self) -> timedelta:
        return _DURATIONS[self]

    @property
    def is_base(self) -> bool:
        return self is TimeFrame.M1

    @property
    def minutes(self) -> int:
        return int(self.duration.total_seconds() // 60)


_DURATIONS: dict[TimeFrame, timedelta] = {
    TimeFrame.M1: timedelta(minutes=1),
    TimeFrame.M5: timedelta(minutes=5),
    TimeFrame.M15: timedelta(minutes=15),
    TimeFrame.H1: timedelta(hours=1),
    TimeFrame.H4: timedelta(hours=4),
    TimeFrame.D1: timedelta(days=1),
    TimeFrame.W1: timedelta(weeks=1),
}

#: Resampling order: each timeframe is built from M1, but evaluation fires low to high.
RESAMPLED: tuple[TimeFrame, ...] = (
    TimeFrame.M5,
    TimeFrame.M15,
    TimeFrame.H1,
    TimeFrame.H4,
    TimeFrame.D1,
    TimeFrame.W1,
)


@dataclass(frozen=True, slots=True)
class Anchor:
    """Where a timeframe's buckets start.

    ``tz`` is the zone the anchor is expressed in. ``at`` is the local time of day. For
    weekly timeframes ``weekday`` names the local weekday the week opens on (Monday=0).
    """

    tz: str
    at: time
    weekday: int | None = None

    @property
    def zone(self) -> ZoneInfo:
        return _zone(self.tz)


@lru_cache(maxsize=32)
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


#: Defaults from `docs/02-definitions.md` §2.3. H4/D1 at 17:00 NY is the broker FX day;
#: it is contested (Q2) and frozen as `prior` rather than searched.
DEFAULT_ANCHORS: dict[TimeFrame, Anchor] = {
    TimeFrame.M1: Anchor("UTC", time(0, 0)),
    TimeFrame.M5: Anchor("UTC", time(0, 0)),
    TimeFrame.M15: Anchor("UTC", time(0, 0)),
    TimeFrame.H1: Anchor("UTC", time(0, 0)),
    TimeFrame.H4: Anchor("America/New_York", time(17, 0)),
    TimeFrame.D1: Anchor("America/New_York", time(17, 0)),
    TimeFrame.W1: Anchor("America/New_York", time(17, 0), weekday=6),  # Sunday
}


@dataclass(frozen=True, slots=True)
class Bucket:
    """A half-open bar interval ``[open_time, close_time)``, both UTC and tz-aware."""

    timeframe: TimeFrame
    open_time: datetime
    close_time: datetime
    dst_irregular: bool = False

    @property
    def wall_duration(self) -> timedelta:
        return self.close_time - self.open_time

    def contains(self, ts: datetime) -> bool:
        return self.open_time <= ts < self.close_time


def _require_aware(ts: datetime, name: str = "ts") -> datetime:
    if ts.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware; naive datetimes are a bug here")
    return ts.astimezone(UTC)


def _localise(naive: datetime, zone: ZoneInfo) -> datetime:
    """Attach ``zone`` to a naive local datetime, resolving DST ambiguity explicitly.

    Ambiguous local times (the repeated hour at fall-back) resolve to the FIRST occurrence
    (``fold=0``), per `docs/02-definitions.md` §0.2. Nonexistent local times (the skipped
    hour at spring-forward) are shifted forward to the first instant that does exist, which
    is what makes a boundary at a skipped local time still produce a usable bucket edge.
    """
    attached = naive.replace(tzinfo=zone, fold=0)
    # A nonexistent local time round-trips to a different local time; detect and shift.
    round_tripped = attached.astimezone(UTC).astimezone(zone)
    if round_tripped.replace(tzinfo=None) != naive:
        # Skipped hour: step forward in absolute time until the local time exists.
        probe = attached + timedelta(hours=1)
        return probe.astimezone(UTC)
    return attached.astimezone(UTC)


class Bucketer:
    """Maps timestamps to bar buckets for one timeframe.

    Intraday timeframes anchored to UTC midnight divide the day evenly and are computed
    arithmetically. Everything else generates boundaries in local time, so DST is handled by
    ``zoneinfo`` rather than by offset arithmetic.
    """

    def __init__(self, timeframe: TimeFrame, anchor: Anchor | None = None) -> None:
        self.timeframe = timeframe
        self.anchor = anchor if anchor is not None else DEFAULT_ANCHORS[timeframe]
        self._delta = timeframe.duration

    # -- public ------------------------------------------------------------
    def bucket(self, ts: datetime) -> Bucket:
        ts = _require_aware(ts)
        if self.timeframe is TimeFrame.W1:
            return self._bucket_weekly(ts)
        if self.timeframe is TimeFrame.D1:
            return self._bucket_daily(ts)
        if self._is_utc_midnight_anchored():
            return self._bucket_arithmetic(ts)
        return self._bucket_intraday_local(ts)

    def open_time(self, ts: datetime) -> datetime:
        return self.bucket(ts).open_time

    def next_open(self, ts: datetime) -> datetime:
        return self.bucket(ts).close_time

    # -- internals ---------------------------------------------------------
    def _is_utc_midnight_anchored(self) -> bool:
        return self.anchor.tz == "UTC" and self.anchor.at == time(0, 0)

    def _bucket_arithmetic(self, ts: datetime) -> Bucket:
        """Even division of the UTC day. Exact, no DST involvement."""
        day = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed = ts - day
        k = int(elapsed // self._delta)
        start = day + k * self._delta
        return Bucket(self.timeframe, start, start + self._delta)

    def _anchor_instant(self, local_date: datetime, zone: ZoneInfo) -> datetime:
        naive = datetime.combine(local_date.date(), self.anchor.at)
        return _localise(naive, zone)

    def _bucket_daily(self, ts: datetime) -> Bucket:
        zone = self.anchor.zone
        local = ts.astimezone(zone)
        # The anchor day is the previous calendar day when the local time precedes the anchor.
        base = local if local.time() >= self.anchor.at else local - timedelta(days=1)
        start = self._anchor_instant(base, zone)
        end = self._anchor_instant(base + timedelta(days=1), zone)
        irregular = (end - start) != timedelta(days=1)
        return Bucket(self.timeframe, start, end, irregular)

    def _bucket_weekly(self, ts: datetime) -> Bucket:
        zone = self.anchor.zone
        weekday = self.anchor.weekday if self.anchor.weekday is not None else 0
        local = ts.astimezone(zone)
        # Walk back to the most recent anchor weekday at or before the anchor time.
        days_back = (local.weekday() - weekday) % 7
        candidate = local - timedelta(days=days_back)
        if days_back == 0 and local.time() < self.anchor.at:
            candidate = candidate - timedelta(days=7)
        start = self._anchor_instant(candidate, zone)
        end = self._anchor_instant(candidate + timedelta(days=7), zone)
        irregular = (end - start) != timedelta(weeks=1)
        return Bucket(self.timeframe, start, end, irregular)

    def _day_boundaries(self, anchor_day: datetime, zone: ZoneInfo) -> list[datetime]:
        """Local-time boundaries from the day's anchor to the next day's anchor."""
        start = self._anchor_instant(anchor_day, zone)
        end = self._anchor_instant(anchor_day + timedelta(days=1), zone)
        edges = [start]
        naive = datetime.combine(anchor_day.date(), self.anchor.at)
        step = 1
        while True:
            nxt = _localise(naive + step * self._delta, zone)
            if nxt >= end:
                break
            edges.append(nxt)
            step += 1
        edges.append(end)
        return edges

    def _bucket_intraday_local(self, ts: datetime) -> Bucket:
        zone = self.anchor.zone
        local = ts.astimezone(zone)
        base = local if local.time() >= self.anchor.at else local - timedelta(days=1)
        edges = self._day_boundaries(base, zone)
        if ts < edges[0]:  # DST edge case: fall back one anchor day
            base = base - timedelta(days=1)
            edges = self._day_boundaries(base, zone)
        idx = bisect_right(edges, ts) - 1
        idx = min(max(idx, 0), len(edges) - 2)
        start, end = edges[idx], edges[idx + 1]
        return Bucket(self.timeframe, start, end, (end - start) != self._delta)


@lru_cache(maxsize=64)
def bucketer(timeframe: TimeFrame, anchor: Anchor | None = None) -> Bucketer:
    return Bucketer(timeframe, anchor)
