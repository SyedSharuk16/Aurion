"""ICT arrays, pools, sweeps, MSS and the dealing range. §5, §6.

Near-miss negatives throughout: a gap one step below the admission threshold, a penetration
that never reclaims, a wick that fills but does not invert.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from aurion.analysis.base import ArrayState, BreakKind, Direction, Side, SwingKind
from aurion.analysis.ict.fvg import available_fvgs, find_bprs, find_fvgs
from aurion.analysis.ict.mss import find_mss
from aurion.analysis.ict.order_block import breakers, find_order_blocks
from aurion.analysis.ict.pd_range import dealing_range
from aurion.analysis.ict.pools import PoolKind, find_equal_pools, mark_swept, reference_pool
from aurion.analysis.ict.sweeps import find_sweeps
from aurion.analysis.primitives import atr
from aurion.analysis.structure.breaks import find_breaks
from aurion.analysis.structure.swings import Swing, find_swings
from aurion.core.timeframe import UTC, TimeFrame
from tests.conftest import bars_from, zigzag

WARMUP = [(100.0, 100.2, 0.3, 0.3), (100.2, 100.0, 0.3, 0.3), (100.0, 100.3, 0.2, 0.3)]
IMPULSE = [(100.3, 105.5, 0.5, 0.3), (106.0, 107.5, 0.5, 0.0)]


# -- FVG ---------------------------------------------------------------------
def test_bullish_fvg_is_detected_with_its_interval_and_ce():
    bars = bars_from([*WARMUP, *IMPULSE])
    fvgs = find_fvgs(bars, min_atr=0.0, point=0.01, atr_period=2)
    assert len(fvgs) == 1
    fvg = fvgs[0]
    assert fvg.direction is Direction.UP
    assert fvg.lo == pytest.approx(100.5) and fvg.hi == pytest.approx(106.0)
    assert fvg.consequent_encroachment == pytest.approx(103.25)
    assert fvg.confirmed_at == bars[4].close_time


def test_gap_below_the_admission_threshold_is_rejected():
    """The near-miss that matters: a real gap that is simply too small."""
    bars = bars_from([*WARMUP, (100.3, 100.6, 0.1, 0.1), (100.8, 101.0, 0.1, 0.0)])
    assert find_fvgs(bars, min_atr=0.0, point=0.01, atr_period=2), "the gap exists"
    assert find_fvgs(bars, min_atr=10.0, point=0.01, atr_period=2) == []


def test_fvg_fill_states_progress_through_touch_ce_and_mitigated():
    base = [*WARMUP, *IMPULSE]
    untouched = find_fvgs(bars_from(base), 0.0, 0.01, 2)[0]
    assert untouched.state is ArrayState.UNTOUCHED and untouched.fill_frac == 0.0

    touched = find_fvgs(bars_from([*base, (107.5, 107.0, 0.2, 2.0)]), 0.0, 0.01, 2)[0]
    assert touched.state is ArrayState.TOUCHED and 0 < touched.fill_frac < 0.5

    ce = find_fvgs(bars_from([*base, (107.5, 107.0, 0.2, 4.2)]), 0.0, 0.01, 2)[0]
    assert ce.state is ArrayState.CE_TRADED and ce.fill_frac >= 0.5

    mitigated = find_fvgs(bars_from([*base, (107.5, 107.0, 0.2, 7.0)]), 0.0, 0.01, 2)[0]
    assert mitigated.state is ArrayState.MITIGATED
    assert mitigated.fill_frac == 1.0 and mitigated.mitigated_at is not None
    assert not mitigated.available


def test_a_close_beyond_the_gap_inverts_polarity_but_a_wick_only_fills_it():
    """§5.2 uses CLOSE for inversion and WICK for mitigation, on purpose.

    One trigger for both would make the two states indistinguishable.
    """
    base = [*WARMUP, *IMPULSE]
    wick_through = find_fvgs(bars_from([*base, (107.5, 107.0, 0.2, 8.0)]), 0.0, 0.01, 2)[0]
    assert wick_through.state is ArrayState.MITIGATED
    assert wick_through.inverted_at is None

    closed_below = find_fvgs(bars_from([*base, (107.5, 99.0, 0.2, 0.5)]), 0.0, 0.01, 2)[0]
    assert closed_below.state is ArrayState.INVERTED
    assert closed_below.inverted_at is not None
    assert closed_below.effective_direction is Direction.DOWN
    assert not closed_below.available


def test_available_fvgs_filters_on_confirmation_and_polarity():
    bars = bars_from([*WARMUP, *IMPULSE])
    fvgs = find_fvgs(bars, 0.0, 0.01, 2)
    assert available_fvgs(fvgs, bars[3].close_time) == [], "not yet confirmed"
    assert len(available_fvgs(fvgs, bars[4].close_time, Direction.UP)) == 1
    assert available_fvgs(fvgs, bars[4].close_time, Direction.DOWN) == []


def test_bpr_is_the_overlap_of_opposite_polarity_gaps():
    up = [*WARMUP, *IMPULSE]
    down = [(107.5, 103.0, 0.2, 0.3), (102.0, 101.5, 0.0, 0.3)]
    bars = bars_from([*up, *down])
    fvgs = find_fvgs(bars, 0.0, 0.01, 2)
    bprs = find_bprs(fvgs, window_bars=10, bar_minutes=5)
    assert bprs, "a bullish and a bearish gap that overlap must produce a BPR"
    assert all(b.hi > b.lo for b in bprs)


def test_no_bpr_when_the_gaps_do_not_overlap_in_price():
    bars = bars_from([*WARMUP, *IMPULSE])
    assert find_bprs(find_fvgs(bars, 0.0, 0.01, 2), 10, 5) == []


# -- order blocks ------------------------------------------------------------
def _break_series():
    bars = zigzag([1, 1, 2, -1, -1, 1, 3, 1, -2, -1, 2, 2, 1, -1, 1, 6, 1, -1, -2, 1, 2, 1, -1, 5])
    swings = find_swings(bars, 2)
    breaks = find_breaks(bars, swings, k=0.8, point=0.01)
    return bars, swings, breaks


def test_order_block_is_the_nearest_opposite_colour_candle_before_the_break():
    bars, _, breaks = _break_series()
    assert breaks
    blocks = find_order_blocks(bars, breaks, lookback_bars=10, point=0.01)
    assert blocks
    for block, brk in zip(blocks, breaks, strict=False):
        origin = bars[block.origin_index].ohlc
        if brk.direction is Direction.UP:
            assert origin.is_down, "a bullish break's block is a down-close candle"
        assert block.confirmed_at == brk.confirmed_at, "no block before its break confirms"


def test_no_order_block_when_no_qualifying_candle_is_within_lookback():
    bars, _, breaks = _break_series()
    assert find_order_blocks(bars, breaks, lookback_bars=0, point=0.01) == []


def test_order_block_extent_can_be_body_instead_of_wick():
    bars, _, breaks = _break_series()
    wick = find_order_blocks(bars, breaks, 10, 0.01, extent="wick")[0]
    body = find_order_blocks(bars, breaks, 10, 0.01, extent="body")[0]
    assert body.size <= wick.size


def test_invalid_extent_is_refused():
    bars, _, breaks = _break_series()
    with pytest.raises(ValueError, match="wick"):
        find_order_blocks(bars, breaks, 10, 0.01, extent="midpoint")


def test_breaker_requires_invalidation_and_an_opposing_break():
    bars, _, breaks = _break_series()
    blocks = find_order_blocks(bars, breaks, 10, 0.01, breaker_window_bars=40)
    for block in breakers(blocks, bars[-1].close_time):
        assert block.invalidated_at is not None
        assert block.effective_direction is block.direction.opposite


# -- pools -------------------------------------------------------------------
def _swing(price: float, kind: SwingKind, minute: int) -> Swing:
    t = datetime(2024, 6, 12, 9, 0, tzinfo=UTC) + timedelta(minutes=minute)
    return Swing("TEST", TimeFrame.M5, kind, t, price, 2, t + timedelta(minutes=10), minute // 5)


def test_equal_highs_cluster_into_a_buy_side_pool_at_the_extreme():
    """The level is the cluster EXTREME, not its mean: that is where stops sit."""
    swings = [
        _swing(110.00, SwingKind.HIGH, 0),
        _swing(110.04, SwingKind.HIGH, 20),
        _swing(110.02, SwingKind.HIGH, 40),
        _swing(100.00, SwingKind.LOW, 10),
        _swing(100.03, SwingKind.LOW, 30),
    ]
    pools = find_equal_pools(swings, tolerance_atr=0.5, atr=0.2)
    buy = [p for p in pools if p.side is Side.BUY_SIDE]
    sell = [p for p in pools if p.side is Side.SELL_SIDE]
    assert buy and buy[0].level == pytest.approx(110.04)
    assert buy[0].touches == 3
    assert sell and sell[0].level == pytest.approx(100.00), "sell-side takes the minimum"


def test_prices_further_apart_than_the_tolerance_do_not_cluster():
    swings = [_swing(110.0, SwingKind.HIGH, 0), _swing(115.0, SwingKind.HIGH, 20)]
    assert find_equal_pools(swings, tolerance_atr=0.5, atr=0.2) == []


def test_a_single_swing_is_not_a_pool():
    assert find_equal_pools([_swing(110.0, SwingKind.HIGH, 0)], 0.5, 0.2) == []


def test_reference_pool_sides_follow_the_kind():
    now = datetime(2024, 6, 12, tzinfo=UTC)
    assert reference_pool("X", TimeFrame.D1, PoolKind.PREV_DAY_HIGH, 1.0, now).side is Side.BUY_SIDE
    assert reference_pool("X", TimeFrame.D1, PoolKind.PREV_DAY_LOW, 1.0, now).side is Side.SELL_SIDE
    assert reference_pool("X", TimeFrame.D1, PoolKind.ASIAN_LOW, 1.0, now).side is Side.SELL_SIDE


# -- sweeps ------------------------------------------------------------------
def _pool_at(level: float, side: Side, confirmed_minute: int = 0):
    t = datetime(2024, 6, 12, 9, 0, tzinfo=UTC) + timedelta(minutes=confirmed_minute)
    return reference_pool("TEST", TimeFrame.M5, PoolKind.PREV_DAY_HIGH
                          if side is Side.BUY_SIDE else PoolKind.PREV_DAY_LOW, level, t)


def test_sweep_requires_penetration_and_a_close_back_inside():
    rows = [*WARMUP, *WARMUP, (100.3, 101.5, 1.5, 0.2), (101.5, 99.8, 0.3, 0.5)]
    bars = bars_from(rows)
    pool = _pool_at(101.0, Side.BUY_SIDE)
    sweeps = find_sweeps(bars, [pool], 0.0, 0.0, max_bars=3, atr_period=2)
    assert len(sweeps) == 1
    sweep = sweeps[0]
    assert sweep.side is Side.BUY_SIDE
    assert sweep.extremity >= 101.0
    assert sweep.against is Direction.DOWN, "a failed up-move implies a move down"
    assert sweep.confirmed_at == bars[-1].close_time, "confirmed by the RECLAIM close"


def test_penetration_that_never_reclaims_is_a_breakout_not_a_sweep():
    """The most important no-repaint case in the ICT layer."""
    rows = [*WARMUP, *WARMUP, (100.3, 102.0, 0.5, 0.2), (102.0, 103.5, 0.5, 0.2),
            (103.5, 105.0, 0.5, 0.2)]
    bars = bars_from(rows)
    assert find_sweeps(bars, [_pool_at(101.0, Side.BUY_SIDE)], 0.0, 0.0, 3, 2) == []


def test_reclaim_outside_the_window_is_not_a_sweep():
    rows = [*WARMUP, *WARMUP, (100.3, 101.5, 1.5, 0.2), (101.5, 101.4, 0.3, 0.2),
            (101.4, 101.3, 0.3, 0.2), (101.3, 101.2, 0.3, 0.2), (101.2, 99.0, 0.3, 0.5)]
    bars = bars_from(rows)
    pool = _pool_at(101.0, Side.BUY_SIDE)
    assert find_sweeps(bars, [pool], 0.0, 0.0, max_bars=1, atr_period=2) == []
    assert find_sweeps(bars, [pool], 0.0, 0.0, max_bars=5, atr_period=2)


def test_penetration_below_epsilon_does_not_count():
    rows = [*WARMUP, *WARMUP, (100.3, 101.01, 0.0, 0.2), (101.01, 99.8, 0.3, 0.5)]
    bars = bars_from(rows)
    pool = _pool_at(101.0, Side.BUY_SIDE)
    assert find_sweeps(bars, [pool], min_penetration_atr=5.0, reclaim_buffer_atr=0.0,
                       max_bars=3, atr_period=2) == []


def test_a_pool_cannot_be_swept_before_it_is_confirmed():
    bars = bars_from([*WARMUP, *WARMUP, (100.3, 101.5, 1.5, 0.2), (101.5, 99.8, 0.3, 0.5)])
    late_pool = _pool_at(101.0, Side.BUY_SIDE, confirmed_minute=500)
    assert find_sweeps(bars, [late_pool], 0.0, 0.0, 3, 2) == []


def test_mark_swept_removes_a_pool_from_availability():
    pool = _pool_at(101.0, Side.BUY_SIDE)
    assert pool.available
    assert not mark_swept(pool, datetime(2024, 6, 12, 10, tzinfo=UTC), 0.4).available


# -- MSS ---------------------------------------------------------------------
def test_mss_is_a_choch_preceded_by_a_sweep_and_is_rarer_than_choch():
    bars, swings, breaks = _break_series()
    atr_value = atr(bars, 14) or 0.5
    pools = find_equal_pools(swings, 0.3, atr_value)
    sweeps = find_sweeps(bars, pools, 0.0, 0.0, 5)
    mss = find_mss(breaks, sweeps, window_bars=20)
    chochs = [b for b in breaks if b.kind is BreakKind.CHOCH]
    assert len(mss) <= len(chochs), "MSS is a strict subset of CHoCH by construction"
    for m in mss:
        assert m.choch.kind is BreakKind.CHOCH
        assert m.sweep.against is m.direction
        assert m.sweep.confirmed_at <= m.confirmed_at


def test_no_mss_without_sweeps():
    _, _, breaks = _break_series()
    assert find_mss(breaks, [], window_bars=20) == []


def test_bos_never_becomes_an_mss():
    bars, swings, breaks = _break_series()
    pools = find_equal_pools(swings, 0.3, atr(bars, 14) or 0.5)
    sweeps = find_sweeps(bars, pools, 0.0, 0.0, 5)
    for m in find_mss(breaks, sweeps, 50):
        assert m.choch.kind is not BreakKind.BOS


# -- dealing range -----------------------------------------------------------
def test_dealing_range_gives_equilibrium_position_and_the_ote_band():
    bars, swings, breaks = _break_series()
    assert breaks
    dr = dealing_range(bars, swings, breaks[-1], bars[-1].close_time)
    assert dr is not None and dr.span > 0
    assert dr.equilibrium == pytest.approx((dr.high + dr.low) / 2)
    assert dr.position(dr.low) == pytest.approx(0.0)
    assert dr.position(dr.high) == pytest.approx(1.0)
    assert dr.is_premium(dr.high) and not dr.is_premium(dr.low)

    lo, hi = dr.ote_band(Direction.UP, 0.62, 0.79)
    assert lo < hi < dr.high
    midpoint = (lo + hi) / 2
    assert dr.in_ote(midpoint, Direction.UP, 0.62, 0.79)
    assert not dr.in_ote(dr.high, Direction.UP, 0.62, 0.79)


def test_retracement_is_measured_from_the_extreme_in_the_trade_direction():
    bars, swings, breaks = _break_series()
    dr = dealing_range(bars, swings, breaks[-1], bars[-1].close_time)
    assert dr is not None
    assert dr.retracement(dr.high, Direction.UP) == pytest.approx(0.0)
    assert dr.retracement(dr.low, Direction.UP) == pytest.approx(1.0)
    assert dr.retracement(dr.low, Direction.DOWN) == pytest.approx(0.0)
