"""The quarter lattice and true opens. §8.1-§8.4.

Three different day boundaries coexist in this system by necessity and are never
interchanged:

* ``D1`` bar: 17:00 NY -> 17:00 NY (the broker FX day, used for ATR and pivots)
* quarter day: 18:00 NY -> 18:00 NY (the daily-quarter lattice)
* true day:    00:00 NY (ICT's "true day open")

They are separately named for exactly that reason.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from enum import StrEnum

from aurion.core.timeframe import _localise, _zone
from aurion.data.types import M1Candle

NY = "America/New_York"


class QuarterLevel(StrEnum):
    YEARLY = "yearly"
    MONTHLY = "monthly"
    WEEKLY = "weekly"
    DAILY = "daily"
    SIX_HOUR = "six_hour"
    NINETY_MIN = "ninety_min"
    MICRO = "micro"


#: Daily quarters, exactly as specified: Q1 18:00-00:00, Q2 00:00-06:00,
#: Q3 06:00-12:00, Q4 12:00-18:00 New York.
DAILY_QUARTER_STARTS: tuple[int, ...] = (18, 0, 6, 12)


@dataclass(frozen=True, slots=True)
class Quarter:
    level: QuarterLevel
    index: int
    start: datetime
    end: datetime
    is_partial: bool = False

    def contains(self, ts: datetime) -> bool:
        return self.start <= ts < self.end

    def elapsed_frac(self, as_of: datetime) -> float:
        span = (self.end - self.start).total_seconds()
        if span <= 0:
            return 1.0
        return min(max((as_of - self.start).total_seconds() / span, 0.0), 1.0)

    @property
    def confirmed_at(self) -> datetime:
        """A completed quarter confirms at its end; a partial one is flagged instead."""
        return self.end


def daily_quarter(ts: datetime) -> Quarter:
    """The 6-hour quarter containing ``ts``, on the 18:00 NY lattice."""
    zone = _zone(NY)
    local = ts.astimezone(zone)
    hour = local.hour
    # Index by which 6-hour block the local hour falls in, anchored at 18:00.
    if hour >= 18:
        idx, start_hour, day = 1, 18, local.date()
    elif hour < 6:
        idx, start_hour, day = 2, 0, local.date()
    elif hour < 12:
        idx, start_hour, day = 3, 6, local.date()
    else:
        idx, start_hour, day = 4, 12, local.date()
    start = _localise(datetime.combine(day, time(start_hour)), zone)
    return Quarter(QuarterLevel.DAILY, idx, start, start + timedelta(hours=6))


def sub_quarters(parent: Quarter, level: QuarterLevel) -> list[Quarter]:
    """Split a quarter into its four children."""
    span = (parent.end - parent.start) / 4
    return [
        Quarter(level, i + 1, parent.start + i * span, parent.start + (i + 1) * span)
        for i in range(4)
    ]


def ninety_minute_quarter(ts: datetime) -> Quarter:
    parent = daily_quarter(ts)
    for q in sub_quarters(parent, QuarterLevel.NINETY_MIN):
        if q.contains(ts):
            return q
    return sub_quarters(parent, QuarterLevel.NINETY_MIN)[-1]


def micro_quarter(ts: datetime) -> Quarter:
    parent = ninety_minute_quarter(ts)
    for q in sub_quarters(parent, QuarterLevel.MICRO):
        if q.contains(ts):
            return q
    return sub_quarters(parent, QuarterLevel.MICRO)[-1]


def weekly_quarter(ts: datetime, partition: str = "mon_thu") -> Quarter | None:
    """Monday-Thursday as Q1-Q4 by default; Friday belongs to no weekly quarter.

    Friday is excluded because the Friday 12:00 NY signal block already removes most of it,
    so including it would produce a quarter that is mostly unusable. ``sun_start`` is the
    alternative convention and is a robustness variant (Q4).
    """
    zone = _zone(NY)
    local = ts.astimezone(zone)
    weekday = local.weekday()
    if partition == "mon_thu":
        if weekday > 3:
            return None
        day = local.date()
        start = _localise(datetime.combine(day, time(0)), zone)
        return Quarter(QuarterLevel.WEEKLY, weekday + 1, start, start + timedelta(days=1))
    # sun_start: Sunday 18:00 opens Q1, each quarter one day long.
    days_from_sunday = (weekday + 1) % 7
    anchor_day = local.date() - timedelta(days=days_from_sunday)
    start = _localise(datetime.combine(anchor_day, time(18)), zone)
    offset = int((ts - start) // timedelta(days=1))
    idx = min(max(offset + 1, 1), 4)
    q_start = start + (idx - 1) * timedelta(days=1)
    return Quarter(QuarterLevel.WEEKLY, idx, q_start, q_start + timedelta(days=1))


def monthly_quarter(ts: datetime, partition: str = "seven_day_blocks") -> Quarter:
    """Four 7-day blocks from the month's first day; residual days join Q4.

    The 5-week-month residual is stated explicitly because every implementation handles it
    differently and silently.
    """
    zone = _zone(NY)
    local = ts.astimezone(zone)
    first = local.replace(day=1).date()
    start_of_month = _localise(datetime.combine(first, time(0)), zone)
    if partition == "calendar_weeks":
        block = min((local.day - 1) // 7, 3)
    else:
        block = min(int((ts - start_of_month) // timedelta(days=7)), 3)
    q_start = start_of_month + block * timedelta(days=7)
    q_end = (
        q_start + timedelta(days=7)
        if block < 3
        else _localise(
            datetime.combine(
                (first.replace(year=first.year + 1, month=1) if first.month == 12
                 else first.replace(month=first.month + 1)),
                time(0),
            ),
            zone,
        )
    )
    return Quarter(QuarterLevel.MONTHLY, block + 1, q_start, q_end)


def yearly_quarter(ts: datetime) -> Quarter:
    zone = _zone(NY)
    local = ts.astimezone(zone)
    idx = (local.month - 1) // 3 + 1
    start_month = 3 * (idx - 1) + 1
    start = _localise(
        datetime.combine(local.date().replace(month=start_month, day=1), time(0)), zone
    )
    end_year, end_month = (local.year + 1, 1) if idx == 4 else (local.year, start_month + 3)
    end = _localise(
        datetime.combine(local.date().replace(year=end_year, month=end_month, day=1), time(0)),
        zone,
    )
    return Quarter(QuarterLevel.YEARLY, idx, start, end)


@dataclass(frozen=True, slots=True)
class TrueOpen:
    name: str
    anchor: datetime
    price: float
    anchor_shifted_minutes: int
    confirmed_at: datetime


def _open_at(
    candles: Sequence[M1Candle], anchor: datetime, name: str
) -> TrueOpen | None:
    """Open of the M1 bar at ``anchor``, or of the first bar after it.

    Never back-filled from an earlier bar: that would be look-ahead-free but would
    reference a pre-anchor price, which is a different number from the true open.
    """
    at_or_after = [c for c in candles if c.open_time >= anchor]
    if not at_or_after:
        return None
    chosen = at_or_after[0]
    shift = int((chosen.open_time - anchor) // timedelta(minutes=1))
    return TrueOpen(
        name=name,
        anchor=anchor,
        price=chosen.mid.o,
        anchor_shifted_minutes=shift,
        confirmed_at=chosen.close_time,
    )


def true_day_open(candles: Sequence[M1Candle], as_of: datetime) -> TrueOpen | None:
    """00:00 New York. Distinct from the 17:00 D1 bar and the 18:00 quarter day."""
    zone = _zone(NY)
    local = as_of.astimezone(zone)
    anchor = _localise(datetime.combine(local.date(), time(0)), zone)
    if anchor > as_of:
        anchor = _localise(datetime.combine(local.date() - timedelta(days=1), time(0)), zone)
    return _open_at(candles, anchor, "true_day_open")


def true_week_open(candles: Sequence[M1Candle], as_of: datetime) -> TrueOpen | None:
    """Monday 18:00 New York."""
    zone = _zone(NY)
    local = as_of.astimezone(zone)
    days_since_monday = local.weekday()
    monday = local.date() - timedelta(days=days_since_monday)
    anchor = _localise(datetime.combine(monday, time(18)), zone)
    if anchor > as_of:
        anchor = anchor - timedelta(days=7)
    return _open_at(candles, anchor, "true_week_open")


def true_month_open(
    candles: Sequence[M1Candle], as_of: datetime, rule: str = "first_monday_1800_ny"
) -> TrueOpen | None:
    """At least three conventions circulate; the rule is a config enum (Q4).

    ``first_monday_1800_ny`` is the default. ``first_day_1800_ny`` and
    ``second_monday_1800_ny`` are the alternatives, run as robustness variants.
    """
    zone = _zone(NY)
    local = as_of.astimezone(zone)
    first = local.date().replace(day=1)
    if rule == "first_day_1800_ny":
        anchor_date = first
    else:
        offset = (0 - first.weekday()) % 7
        anchor_date = first + timedelta(days=offset)
        if rule == "second_monday_1800_ny":
            anchor_date = anchor_date + timedelta(days=7)
    anchor = _localise(datetime.combine(anchor_date, time(18)), zone)
    if anchor > as_of:
        prev = (first - timedelta(days=1)).replace(day=1)
        offset = (0 - prev.weekday()) % 7
        anchor = _localise(datetime.combine(prev + timedelta(days=offset), time(18)), zone)
    return _open_at(candles, anchor, "true_month_open")


def quarter_stack(ts: datetime, weekly_partition: str = "mon_thu") -> dict[str, Quarter | None]:
    """Every level at once, for the feature builder."""
    return {
        "yearly": yearly_quarter(ts),
        "monthly": monthly_quarter(ts),
        "weekly": weekly_quarter(ts, weekly_partition),
        "daily": daily_quarter(ts),
        "ninety_min": ninety_minute_quarter(ts),
        "micro": micro_quarter(ts),
    }
