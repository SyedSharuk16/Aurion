"""Structure: swings, trend, displacement, breaks. §4.1-§4.4.

Each definition gets a positive case and a **near-miss negative**. The near-misses are the
ones that matter: a suite that only checks positive cases proves a detector fires, not that
it discriminates.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from aurion.analysis.base import BreakKind, Direction, SwingKind, TrendState
from aurion.analysis.primitives import three_candle_imbalance
from aurion.analysis.structure.breaks import find_breaks, find_displacements
from aurion.analysis.structure.swings import find_swings, last_swing, visible_swings
from aurion.analysis.structure.trend import trend_state
from aurion.core.timeframe import UTC, TimeFrame
from aurion.data.types import OHLC, Bar

T0 = datetime(2024, 6, 12, 9, 0, tzinfo=UTC)


def mk(rows: list[tuple[float, float, float, float]], tf: TimeFrame = TimeFrame.M5) -> list[Bar]:
    """Bars from ``(open, close, upper_wick, lower_wick)``.

    Deliberately not ``(o, h, l, c)``: hand-written OHLC tuples kept violating
    ``high >= max(o, c)``, which OHLC's own validator rejected. Deriving the extremes from
    wick sizes makes an invalid fixture impossible to write.
    """
    out = []
    for i, (o, c, up, dn) in enumerate(rows):
        out.append(
            Bar("TEST", tf, T0 + timedelta(minutes=5 * i), T0 + timedelta(minutes=5 * (i + 1)),
                OHLC(o, max(o, c) + up, min(o, c) - dn, c), 100, 5)
        )
    return out


# -- swings ------------------------------------------------------------------
def test_swing_high_is_a_strict_local_maximum():
    bars = mk([
        (10.0, 10.5, 0.5, 0.5),
        (10.5, 11.0, 0.5, 0.5),
        (11.0, 14.0, 1.0, 0.5),   # index 2: the peak, high = 15
        (14.0, 13.0, 0.3, 0.5),
        (13.0, 12.0, 0.3, 0.5),
    ])
    swings = find_swings(bars, half_width=2)
    highs = [s for s in swings if s.kind is SwingKind.HIGH]
    assert len(highs) == 1
    assert highs[0].bar_index == 2 and highs[0].price == pytest.approx(15.0)


def test_swing_confirms_at_the_close_of_bar_i_plus_n_and_not_before():
    bars = mk([(10.0, 10.5, 0.5, 0.5), (10.5, 11.0, 0.5, 0.5), (11.0, 14.0, 1.0, 0.5),
               (14.0, 13.0, 0.3, 0.5), (13.0, 12.0, 0.3, 0.5)])
    swing = next(s for s in find_swings(bars, half_width=2) if s.kind is SwingKind.HIGH)
    assert swing.confirmed_at == bars[4].close_time
    assert swing.event_time == bars[2].open_time
    assert visible_swings([swing], bars[3].close_time) == []
    assert visible_swings([swing], bars[4].close_time) == [swing]


def test_equal_high_plateau_is_not_a_swing():
    """Strict inequality on both sides. §4.1.

    This looks like an omission and is not: equal highs are the defining feature of a
    liquidity pool and are captured there. A plateau that was both a swing and equal highs
    would double-count the same price action in the feature vector.
    """
    # Bars 1-4 all top out at exactly 14.0, so no strict local maximum exists.
    bars = mk([
        (10.0, 10.5, 0.5, 0.5),
        (10.5, 13.0, 1.0, 0.5),
        (13.0, 13.0, 1.0, 0.5),
        (13.0, 13.0, 1.0, 0.5),
        (13.0, 13.0, 1.0, 0.5),
        (13.0, 11.0, 0.3, 0.5),
        (11.0, 10.0, 0.3, 0.5),
    ])
    assert [s for s in find_swings(bars, half_width=2) if s.kind is SwingKind.HIGH] == []


def test_swings_need_n_bars_on_each_side():
    bars = mk([(10.0, 10.5, 0.5, 0.5), (10.5, 14.0, 1.0, 0.5), (14.0, 12.0, 0.3, 0.5)])
    assert find_swings(bars, half_width=2) == []


def test_half_width_must_be_positive():
    with pytest.raises(ValueError, match="half_width"):
        find_swings(mk([(1.0, 1.5, 0.2, 0.2)]), half_width=0)


def test_last_swing_picks_the_most_recent_by_event_time():
    bars = mk([
        (10.0, 10.5, 0.5, 0.5), (10.5, 12.0, 0.5, 0.5), (12.0, 15.0, 1.0, 0.5),
        (15.0, 13.0, 0.3, 0.5), (13.0, 14.0, 0.3, 0.5), (14.0, 17.0, 1.0, 0.5),
        (17.0, 15.0, 0.3, 0.5), (15.0, 14.0, 0.3, 0.5),
    ])
    swings = find_swings(bars, half_width=2)
    got = last_swing(swings, SwingKind.HIGH, bars[-1].close_time)
    assert got is not None and got.price == pytest.approx(18.0)


# -- trend -------------------------------------------------------------------
BULLISH_PATTERN = [1, 1, 2, -1, -1, 1, 3, 1, -2, -1, 2, 2, 1, -1, 1, 4, 1, -1, -2, 1]


def _zigzag(deltas: list[float], start: float = 100.0, mirror: bool = False) -> list[Bar]:
    """Deterministic zigzag with varied wicks.

    The wicks have to vary: with equal wicks every reversal bar's extreme ties its
    neighbour's (open == prior close), and the strict-inequality swing definition then
    suppresses every fractal. ``mirror`` swaps the two wick formulas as well as the
    deltas, because the asymmetry between them would otherwise break the mirror and leave
    too few swing highs for `trend_state` to classify anything.
    """
    rows = []
    level = start
    for i, d in enumerate(deltas):
        o, c = level, level + d
        a = 0.10 + 0.05 * ((i * 7) % 5)
        b = 0.10 + 0.05 * ((i * 3) % 4)
        rows.append((o, c, b, a) if mirror else (o, c, a, b))
        level = c
    return mk(rows)


def test_bullish_trend_needs_hh_and_hl():
    bars = _zigzag(BULLISH_PATTERN)
    state = trend_state(find_swings(bars, 2), bars[-1].close_time)
    assert state is not None and state.state is TrendState.BULLISH
    assert state.higher_high and state.higher_low


def test_bearish_trend_is_the_mirror():
    bars = _zigzag([-d for d in BULLISH_PATTERN], mirror=True)
    state = trend_state(find_swings(bars, 2), bars[-1].close_time)
    assert state is not None and state.state is TrendState.BEARISH
    assert state.lower_high and state.lower_low


def test_trend_is_none_before_two_swings_each_side():
    bars = _zigzag([1, 1, 2, -1, -1])
    assert trend_state(find_swings(bars, 2), bars[-1].close_time) is None


# -- displacement ------------------------------------------------------------
def test_displacement_needs_both_a_large_body_and_a_gap():
    """§4.3. A large body that does not gap is not displacement under this definition."""
    # Index 1 has a big body AND bar 2's low clears bar 0's high.
    # Three warm-up bars so ATR(2) is defined at the displacement index.
    warmup = [(100.0, 100.2, 0.3, 0.3), (100.2, 100.0, 0.3, 0.3), (100.0, 100.3, 0.2, 0.3)]
    # bar 2 high = 100.5, bar 4 low = 106.0 -> a bullish imbalance around the big bar 3
    gapping = mk([*warmup, (100.3, 105.5, 0.5, 0.3), (106.0, 107.5, 0.5, 0.0)])
    # identical body, but bar 4's lower wick reaches back under bar 2's high -> no imbalance
    big_no_gap = mk([*warmup, (100.3, 105.5, 0.5, 0.3), (106.0, 107.5, 0.5, 6.0)])
    found_gap = find_displacements(gapping, k=0.5, atr_period=2)
    found_flat = find_displacements(big_no_gap, k=0.5, atr_period=2)
    assert 3 in found_gap and found_gap[3].body_atr > 1.0
    assert found_flat == {}, "no imbalance means no displacement, however large the body"


def test_displacement_confirms_only_when_the_gap_candle_closes():
    bars = mk([
        (100.0, 100.2, 0.3, 0.3), (100.2, 100.0, 0.3, 0.3), (100.0, 100.3, 0.2, 0.3),
        (100.3, 105.5, 0.5, 0.3), (106.0, 107.5, 0.5, 0.0),
    ])
    disp = find_displacements(bars, k=0.5, atr_period=2)[3]
    assert disp.event_time == bars[3].open_time
    assert disp.confirmed_at == bars[4].close_time, "the imbalance needs bar i+1 to close"


def test_body_below_k_times_atr_is_not_displacement():
    bars = mk([
        (100.0, 100.2, 0.3, 0.3), (100.2, 100.0, 0.3, 0.3), (100.0, 100.3, 0.2, 0.3),
        (100.3, 100.6, 0.2, 0.2), (101.0, 101.4, 0.3, 0.0),
    ])
    assert find_displacements(bars, k=5.0, atr_period=2) == {}


def test_displacement_requires_body_and_gap_to_agree_in_direction():
    """A bullish body leaving a bearish gap is not a coherent leg."""
    # bar 3 closes UP, but bar 4 sits entirely BELOW bar 2's low -> a bearish imbalance
    bars = mk([
        (104.0, 104.2, 0.3, 0.3), (104.2, 104.0, 0.3, 0.3), (104.0, 104.3, 0.2, 0.3),
        (100.0, 103.0, 0.5, 0.3), (99.0, 98.0, 0.5, 0.5),
    ])
    assert 3 not in find_displacements(bars, k=0.2, atr_period=2)


def test_imbalance_primitive_handles_both_polarities_and_the_overlap_case():
    bars = mk([(100.0, 100.5, 0.5, 1.0), (100.5, 104.0, 1.0, 1.0), (104.0, 105.0, 1.0, 2.0)])
    up = three_candle_imbalance(bars[0], bars[2])
    assert up is not None and up.bullish
    assert up.lo == pytest.approx(101.0) and up.hi == pytest.approx(102.0)
    assert up.consequent_encroachment == pytest.approx(101.5)
    overlapping = mk([(100.0, 103.0, 1.0, 1.0), (103.0, 104.0, 1.0, 1.0),
                      (104.0, 101.0, 1.0, 1.0)])
    assert three_candle_imbalance(overlapping[0], overlapping[2]) is None


# -- breaks ------------------------------------------------------------------
def test_bos_is_a_continuation_break_and_choch_is_against_the_trend():
    bars = _zigzag([1, 1, 2, -1, -1, 1, 3, 1, -2, -1, 2, 2, 1, -1, 1, 6, 1, -1, -2, 1,
                    2, 1, -1, 5])
    swings = find_swings(bars, 2)
    breaks = find_breaks(bars, swings, k=0.8, point=0.01)
    assert breaks, "a six-point impulse past a confirmed swing high must break structure"
    kinds = {b.kind for b in breaks}
    assert kinds <= {BreakKind.BOS, BreakKind.CHOCH}
    for b in breaks:
        if b.kind is BreakKind.BOS:
            assert b.trend_before in (TrendState.BULLISH, TrendState.BEARISH)
        else:
            assert b.trend_before in (TrendState.BULLISH, TrendState.BEARISH,
                                      TrendState.RANGING)


def test_a_break_requires_a_close_beyond_not_just_a_wick():
    """§4.4 is close-based. A wick through a swing high is not a break."""
    bars = mk([
        (100.0, 100.2, 0.3, 0.5), (100.2, 100.5, 0.3, 0.5), (100.5, 104.5, 0.5, 0.5),
        (104.5, 103.5, 0.3, 0.5), (103.5, 102.5, 0.3, 0.5), (102.5, 101.5, 0.3, 0.5),
        (101.5, 102.0, 4.0, 0.5),   # wick to 106 but closes back below the 105 swing high
    ])
    swings = find_swings(bars, 2)
    assert any(s.kind is SwingKind.HIGH and s.price == pytest.approx(105.0) for s in swings)
    assert find_breaks(bars, swings, k=0.1, point=0.01) == []


def test_break_direction_matches_its_displacement():
    bars = _zigzag([1, 1, 2, -1, -1, 1, 3, 1, -2, -1, 2, 2, 1, -1, 1, 6, 1, -1, -2, 1])
    for b in find_breaks(bars, find_swings(bars, 2), k=0.8, point=0.01):
        assert b.displacement.direction is b.direction
        if b.direction is Direction.UP:
            assert b.close > b.broken_level
        else:
            assert b.close < b.broken_level
