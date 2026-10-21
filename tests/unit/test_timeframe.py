"""Bucketing, including the DST transitions where this logic is most likely to be wrong."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from aurion.core.timeframe import UTC, Anchor, Bucketer, TimeFrame, parse_anchor

NY = ZoneInfo("America/New_York")

# 2024 US transitions: spring forward 03-10 (02:00 EST -> 03:00 EDT),
# fall back 11-03 (02:00 EDT -> 01:00 EST).
SPRING = datetime(2024, 3, 8, tzinfo=UTC)
FALL = datetime(2024, 11, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("tf", "expected"),
    [
        (TimeFrame.M1, timedelta(minutes=1)),
        (TimeFrame.M5, timedelta(minutes=5)),
        (TimeFrame.H4, timedelta(hours=4)),
        (TimeFrame.W1, timedelta(weeks=1)),
    ],
)
def test_durations(tf, expected):
    assert tf.duration == expected


def test_naive_datetime_refused():
    with pytest.raises(ValueError, match="timezone-aware"):
        Bucketer(TimeFrame.M5).bucket(datetime(2024, 6, 12, 9, 0))


def test_utc_anchored_buckets_divide_the_day_evenly():
    b = Bucketer(TimeFrame.M15)
    bucket = b.bucket(datetime(2024, 6, 12, 9, 37, 12, tzinfo=UTC))
    assert bucket.open_time == datetime(2024, 6, 12, 9, 30, tzinfo=UTC)
    assert bucket.close_time == datetime(2024, 6, 12, 9, 45, tzinfo=UTC)
    assert not bucket.dst_irregular


def test_h4_anchored_to_1700_ny():
    b = Bucketer(TimeFrame.H4)
    bucket = b.bucket(datetime(2024, 6, 12, 14, 37, tzinfo=UTC))  # 10:37 NY
    assert bucket.open_time.astimezone(NY).hour == 9
    assert bucket.close_time.astimezone(NY).hour == 13


def test_d1_is_the_broker_fx_day_not_midnight():
    """D1 runs 17:00 NY -> 17:00 NY. Distinct from the 00:00 NY 'true day' of §8.3."""
    b = Bucketer(TimeFrame.D1)
    bucket = b.bucket(datetime(2024, 6, 12, 14, 0, tzinfo=UTC))
    assert bucket.open_time.astimezone(NY).hour == 17
    assert bucket.open_time.astimezone(NY).day == 11
    assert bucket.close_time.astimezone(NY).day == 12


def test_w1_opens_sunday_1700_ny():
    b = Bucketer(TimeFrame.W1)
    bucket = b.bucket(datetime(2024, 6, 12, 14, 0, tzinfo=UTC))  # a Wednesday
    opened = bucket.open_time.astimezone(NY)
    assert opened.weekday() == 6  # Sunday
    assert opened.hour == 17


@pytest.mark.parametrize(
    "tf", [TimeFrame.M5, TimeFrame.M15, TimeFrame.H1, TimeFrame.H4, TimeFrame.D1]
)
@pytest.mark.parametrize("start", [SPRING, FALL], ids=["spring", "fall"])
def test_buckets_tile_contiguously_across_dst(tf, start):
    """No gaps, no overlaps, and every bucket contains the instant it was asked about.

    This is the property that matters: a gap would drop bars and an overlap would
    double-count them, and either would be invisible in normal operation.
    """
    b = Bucketer(tf)
    ts = start
    prev = None
    while ts < start + timedelta(days=5):
        bucket = b.bucket(ts)
        assert bucket.contains(ts)
        if prev is not None and bucket.open_time != prev.open_time:
            assert bucket.open_time == prev.close_time, (
                f"{tf}: prev closes {prev.close_time}, next opens {bucket.open_time}"
            )
        prev = bucket
        ts += timedelta(minutes=1)


def test_dst_irregular_bars_are_flagged_not_normalised():
    """Local-time boundaries mean one short and one long bar per year. Both are flagged."""
    h4 = Bucketer(TimeFrame.H4)
    spring = h4.bucket(datetime(2024, 3, 10, 7, 0, tzinfo=UTC))  # the 01:00 NY bar
    assert spring.dst_irregular
    assert spring.wall_duration == timedelta(hours=3)

    fall = h4.bucket(datetime(2024, 11, 3, 6, 0, tzinfo=UTC))
    assert fall.dst_irregular
    assert fall.wall_duration == timedelta(hours=5)


def test_dst_irregular_daily_bars():
    d1 = Bucketer(TimeFrame.D1)
    assert d1.bucket(datetime(2024, 3, 10, 12, tzinfo=UTC)).wall_duration == timedelta(hours=23)
    assert d1.bucket(datetime(2024, 11, 3, 12, tzinfo=UTC)).wall_duration == timedelta(hours=25)


def test_exactly_one_irregular_bar_per_transition():
    h4 = Bucketer(TimeFrame.H4)
    for start in (SPRING, FALL):
        seen, ts, prev = [], start, None
        while ts < start + timedelta(days=5):
            bucket = h4.bucket(ts)
            if prev is None or bucket.open_time != prev.open_time:
                seen.append(bucket)
                prev = bucket
            ts += timedelta(minutes=1)
        assert sum(1 for s in seen if s.dst_irregular) == 1


# -- anchor parsing ----------------------------------------------------------
def test_anchor_specs_parse_in_the_three_shapes_config_uses():
    assert parse_anchor("00:00 UTC") == Anchor("UTC", time(0, 0))
    assert parse_anchor("17:00 America/New_York") == Anchor("America/New_York", time(17, 0))
    assert parse_anchor("Sun 17:00 America/New_York") == Anchor(
        "America/New_York", time(17, 0), weekday=6
    )


@pytest.mark.parametrize(
    "spec",
    ["17:00", "Xyz 17:00 UTC", "25:00 UTC", "17:61 UTC", "17:aa UTC", "17:00 Mars/Olympus", ""],
)
def test_a_malformed_anchor_is_rejected_at_parse_time(spec):
    """A bad anchor must stop the process before it ingests a bar, not at the first D1 close."""
    with pytest.raises(ValueError):
        parse_anchor(spec)
