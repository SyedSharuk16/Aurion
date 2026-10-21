"""ATR and friends, against hand-computed values rather than a reimplementation."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from aurion.analysis.primitives import (
    atr,
    atr_series,
    log_returns,
    percentile_rank,
    prices_equal,
    round_to_point,
    true_range,
    true_range_series,
)
from aurion.core.timeframe import UTC, TimeFrame
from aurion.data.types import OHLC, Bar


def bar(i: int, o: float, h: float, low: float, c: float) -> Bar:
    t = datetime(2024, 6, 12, 9, 0, tzinfo=UTC) + timedelta(minutes=5 * i)
    return Bar("TEST", TimeFrame.M5, t, t + timedelta(minutes=5), OHLC(o, h, low, c), 10, 5)


#: Hand-computed fixture. TR1 = TR2 = 2, TR3 = 4.
#: ATR(2) seeds at index 2 = mean(2, 2) = 2.0, then 2.0 + (4 - 2.0)/2 = 3.0 at index 3.
HAND = [
    bar(0, 9, 10, 8, 9),
    bar(1, 9, 11, 9, 10),
    bar(2, 10, 12, 10, 11),
    bar(3, 11, 15, 11, 14),
]


def test_true_range_takes_the_largest_of_the_three_legs():
    assert true_range(OHLC(9, 11, 9, 10), prev_close=9.0) == 2.0
    # A gap down makes |L - prev_close| the binding leg.
    assert true_range(OHLC(5, 6, 4, 5), prev_close=10.0) == 6.0


def test_true_range_series_skips_the_first_bar():
    assert true_range_series(HAND) == [2.0, 2.0, 4.0]


def test_atr_matches_hand_computed_wilder_values():
    series = atr_series(HAND, period=2)
    assert series[0] is None and series[1] is None
    assert series[2] == pytest.approx(2.0)
    assert series[3] == pytest.approx(3.0)
    assert atr(HAND, period=2) == pytest.approx(3.0)


def test_atr_warmup_needs_period_plus_one_bars_and_returns_none_not_zero():
    """No silent defaults: a zero ATR would make every ATR-scaled threshold trivial."""
    assert atr(HAND[:2], period=2) is None
    assert atr(HAND[:3], period=2) == pytest.approx(2.0)
    assert atr(HAND, period=14) is None
    assert all(v is None for v in atr_series(HAND, period=14))


def test_atr_rejects_degenerate_period():
    with pytest.raises(ValueError, match="period"):
        atr_series(HAND, period=1)


def test_percentile_rank_is_backward_looking_and_inclusive():
    assert percentile_rank([1.0, 2.0, 3.0, 4.0], window=4) == pytest.approx(0.75)
    assert percentile_rank([4.0, 3.0, 2.0, 1.0], window=4) == pytest.approx(0.0)
    assert percentile_rank([1.0, 2.0], window=4) is None


def test_log_returns_length_and_sign():
    rets = log_returns(HAND)
    assert len(rets) == len(HAND) - 1
    assert all(r > 0 for r in rets)


def test_price_equality_uses_half_a_point():
    assert prices_equal(1.10000, 1.100004, point=1e-5)
    assert not prices_equal(1.10000, 1.10002, point=1e-5)


def test_round_to_point_makes_replay_bit_identical():
    assert round_to_point(2318.4049, 0.01) == pytest.approx(2318.40)
    assert round_to_point(1.1000049, 1e-5) == pytest.approx(1.10000)


def test_ohlc_rejects_inconsistent_values():
    with pytest.raises(ValueError, match="inconsistent OHLC"):
        OHLC(o=1.0, h=2.0, l=3.0, c=4.0)
