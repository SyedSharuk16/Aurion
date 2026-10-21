"""Resampler properties. `docs/02-definitions.md` §2.2."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from aurion.core.timeframe import UTC, Bucketer, TimeFrame
from aurion.data.resample import IncrementalResampler, ResamplerSet, expected_constituents
from tests.conftest import make_m1

T0 = datetime(2024, 6, 12, 9, 0, tzinfo=UTC)


def test_m1_cannot_be_resampled():
    with pytest.raises(ValueError, match="base timeframe"):
        IncrementalResampler("EURUSD", TimeFrame.M1)


def test_bar_is_emitted_only_when_the_next_bucket_starts(ramp_series):
    r = IncrementalResampler("EURUSD", TimeFrame.M5)
    emitted = [r.push(c) for c in ramp_series(n=5)]
    assert all(e is None for e in emitted), "no bar may be emitted while its bucket is open"
    sixth = r.push(make_m1("EURUSD", T0 + timedelta(minutes=5), 1.2))
    assert sixth is not None and sixth.open_time == T0


def test_ohlc_aggregation_is_correct(ramp_series):
    r = IncrementalResampler("EURUSD", TimeFrame.M5)
    candles = ramp_series(n=5)
    for c in candles:
        r.push(c)
    bar = r.advance_to(T0 + timedelta(minutes=5))
    assert bar is not None
    mids = [c.mid for c in candles]
    assert bar.ohlc.o == pytest.approx(mids[0].o)
    assert bar.ohlc.c == pytest.approx(mids[-1].c)
    assert bar.ohlc.h == pytest.approx(max(m.h for m in mids))
    assert bar.ohlc.l == pytest.approx(min(m.l for m in mids))
    assert bar.tick_volume == sum(c.tick_volume for c in candles)
    assert bar.constituents == 5


def test_advance_to_closes_the_final_bar_before_a_market_close(ramp_series):
    """Without this the last bar of every Friday would be lost: no candle follows it."""
    r = IncrementalResampler("EURUSD", TimeFrame.M15)
    for c in ramp_series(n=15):
        r.push(c)
    assert r.advance_to(T0 + timedelta(minutes=14)) is None  # not yet closed
    bar = r.advance_to(T0 + timedelta(minutes=15))
    assert bar is not None and bar.constituents == 15


def test_thin_bucket_is_flagged_incomplete_and_unusable():
    r = IncrementalResampler("EURUSD", TimeFrame.M15, min_constituents_frac=0.5)
    for i in range(3):
        r.push(make_m1("EURUSD", T0 + timedelta(minutes=i), 1.1))
    bar = r.advance_to(T0 + timedelta(minutes=15))
    assert bar is not None and bar.incomplete and not bar.usable


def test_bucket_at_the_frac_boundary_is_complete():
    r = IncrementalResampler("EURUSD", TimeFrame.M15, min_constituents_frac=0.5)
    for i in range(8):  # 8 of 15 >= 0.5 * 15
        r.push(make_m1("EURUSD", T0 + timedelta(minutes=i), 1.1))
    bar = r.advance_to(T0 + timedelta(minutes=15))
    assert bar is not None and not bar.incomplete


def test_duplicates_and_out_of_order_candles_are_dropped():
    """Backfill overlaps by one bar by design, so duplicates are expected, not exceptional."""
    r = IncrementalResampler("EURUSD", TimeFrame.M5)
    c = make_m1("EURUSD", T0, 1.1)
    r.push(c)
    assert r.partial is not None and r.partial.constituents == 1
    r.push(c)
    assert r.partial.constituents == 1
    r.push(make_m1("EURUSD", T0 - timedelta(minutes=5), 1.1))
    assert r.partial.constituents == 1


def test_wrong_instrument_is_a_programming_error():
    r = IncrementalResampler("EURUSD", TimeFrame.M5)
    with pytest.raises(ValueError, match="pushed to"):
        r.push(make_m1("XAUUSD", T0, 2300.0))


def test_partial_is_always_marked_incomplete(ramp_series):
    r = IncrementalResampler("EURUSD", TimeFrame.H1)
    for c in ramp_series(n=10):
        r.push(c)
    partial = r.partial
    assert partial is not None and partial.incomplete


def test_empty_buckets_are_never_emitted():
    """No synthetic zero-volume bars across weekends: the gap must stay real (§2.2 rule 4)."""
    r = IncrementalResampler("EURUSD", TimeFrame.H1)
    r.push(make_m1("EURUSD", T0, 1.1))
    # Jump three hours: only the bar that had data is emitted, not the two empty ones.
    first = r.advance_to(T0 + timedelta(hours=3))
    assert first is not None and first.open_time == T0.replace(minute=0)
    assert r.advance_to(T0 + timedelta(hours=4)) is None


def test_resampler_set_emits_low_timeframe_first(ramp_series):
    rs = ResamplerSet("EURUSD", (TimeFrame.M5, TimeFrame.M15, TimeFrame.H1))
    closed = []
    for c in ramp_series(n=65):
        closed.extend(rs.push(c))
    order = [b.timeframe for b in closed if b.open_time == T0]
    assert order == [TimeFrame.M5, TimeFrame.M15, TimeFrame.H1]


def test_weekly_expectation_uses_trading_minutes_not_wall_minutes():
    """A W1 bucket is 10,080 wall minutes but only ~7,200 trading minutes.

    Using wall duration would mark every weekly bar thin.
    """
    bucket = Bucketer(TimeFrame.W1).bucket(datetime(2024, 6, 12, tzinfo=UTC))
    assert expected_constituents(bucket) == 5 * 24 * 60
    h4 = Bucketer(TimeFrame.H4).bucket(datetime(2024, 6, 12, 14, tzinfo=UTC))
    assert expected_constituents(h4) == 240
