"""MMXM stages and scoring. §9. Tier 2: built and measured, not selection-eligible.

It fails the resolvability test on its own terms -- Stage-3 confirmation is rare, so its
affected fraction is small, and `docs/07` §3 shows a module firing on 5% of candidates would
need a ~1.4R per-trade effect to be detected at the projected sample size. These tests
therefore check that it computes what it claims, not that it predicts anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from aurion.analysis.base import Direction, Side, SwingKind
from aurion.analysis.ict.pools import PoolKind, reference_pool
from aurion.analysis.mmxm.model import (
    STAGE_WEIGHTS,
    count_engineering_legs,
    find_consolidation,
    score_mmxm,
)
from aurion.analysis.primitives import atr
from aurion.analysis.structure.breaks import find_breaks
from aurion.analysis.structure.swings import Swing, find_swings
from aurion.core.timeframe import UTC, TimeFrame
from tests.conftest import bars_from, zigzag

T0 = datetime(2024, 6, 12, 9, 0, tzinfo=UTC)


def _pools_inside(lo: float, hi: float, n_each: int = 2):
    out = []
    for i in range(n_each):
        out.append(
            reference_pool("TEST", TimeFrame.M5, PoolKind.PREV_DAY_HIGH,
                           hi - 0.01 * i, T0 + timedelta(minutes=i))
        )
        out.append(
            reference_pool("TEST", TimeFrame.M5, PoolKind.PREV_DAY_LOW,
                           lo + 0.01 * i, T0 + timedelta(minutes=i))
        )
    return out


def test_consolidation_needs_a_tight_range_and_pools_on_both_sides():
    bars = zigzag([0.1, -0.1] * 15)
    lo = min(b.ohlc.l for b in bars)
    hi = max(b.ohlc.h for b in bars)
    found = find_consolidation(bars, _pools_inside(lo, hi), min_bars=20,
                               max_range_atr=10.0, atr=0.3)
    assert found is not None
    assert found.lo <= lo + 1e-9 and found.hi >= hi - 1e-9
    assert found.pools_each_side >= 2


def test_no_consolidation_without_pools_on_both_sides():
    """A range with no equal highs and lows has engineered nothing."""
    bars = zigzag([0.1, -0.1] * 15)
    one_sided = [
        reference_pool("TEST", TimeFrame.M5, PoolKind.PREV_DAY_HIGH, 100.5, T0),
        reference_pool("TEST", TimeFrame.M5, PoolKind.PREV_DAY_HIGH, 100.4, T0),
    ]
    assert find_consolidation(bars, one_sided, 20, 10.0, 0.3) is None


def test_no_consolidation_when_the_range_is_too_wide():
    bars = zigzag([2.0, -1.0] * 15)
    lo = min(b.ohlc.l for b in bars)
    hi = max(b.ohlc.h for b in bars)
    assert find_consolidation(bars, _pools_inside(lo, hi), 20, 0.5, 0.3) is None


def test_consolidation_needs_enough_bars_and_a_positive_atr():
    bars = zigzag([0.1, -0.1] * 3)
    pools = _pools_inside(99.0, 101.0)
    assert find_consolidation(bars, pools, min_bars=20, max_range_atr=10.0, atr=0.3) is None
    assert find_consolidation(bars, pools, min_bars=2, max_range_atr=10.0, atr=0.0) is None


def _swing(price: float, kind: SwingKind, minute: int) -> Swing:
    t = T0 + timedelta(minutes=minute)
    return Swing("TEST", TimeFrame.M5, kind, t, price, 2, t + timedelta(minutes=10),
                 minute // 5)


def test_engineering_legs_counts_higher_lows_for_a_sell_model():
    """A sell model is distributed at the top; the markup leaves higher lows."""
    swings = [
        _swing(100.0, SwingKind.LOW, 10),
        _swing(101.0, SwingKind.LOW, 20),
        _swing(102.0, SwingKind.LOW, 30),
    ]
    assert count_engineering_legs(swings, T0, Direction.DOWN) == 2
    assert count_engineering_legs(swings, T0, Direction.UP) == 0, "buy model wants lower highs"


def test_engineering_run_resets_on_a_break():
    swings = [
        _swing(100.0, SwingKind.LOW, 10),
        _swing(101.0, SwingKind.LOW, 20),
        _swing(99.0, SwingKind.LOW, 30),   # breaks the run
        _swing(99.5, SwingKind.LOW, 40),
    ]
    assert count_engineering_legs(swings, T0, Direction.DOWN) == 1


def test_engineering_legs_needs_at_least_two_swings():
    assert count_engineering_legs([_swing(100.0, SwingKind.LOW, 10)], T0, Direction.DOWN) == 0
    assert count_engineering_legs([], T0, Direction.DOWN) == 0


def test_score_is_bounded_and_reports_the_stage_reached():
    bars = zigzag([0.1, -0.1] * 20 + [1.0] * 10 + [-1.0] * 10)
    swings = find_swings(bars, 2)
    breaks = find_breaks(bars, swings, k=0.8, point=0.01)
    atr_value = atr(bars, 14) or 0.3
    lo = min(b.ohlc.l for b in bars[:40])
    hi = max(b.ohlc.h for b in bars[:40])
    score = score_mmxm(
        bars=bars,
        swings=swings,
        pools=_pools_inside(lo, hi),
        mss_events=[],
        breaks=breaks,
        direction=Direction.DOWN,
        atr=atr_value,
        range_position=0.8,
        pd_array_strength=0.7,
        as_of=bars[-1].close_time,
        consolidation_max_range_atr=20.0,
    )
    assert 0.0 <= score.score <= 1.0
    assert 0 <= score.stage_reached <= 4
    for component in (score.s_consolidation, score.s_engineering, score.s_pd_array,
                      score.s_mss, score.s_progress):
        assert 0.0 <= component <= 1.0
    assert set(score.as_dict()) >= {"mmxm_score", "mmxm_stage"}


def test_pd_array_score_is_zero_outside_the_premium_zone():
    """A sell model scores nothing on its PD array while price sits in discount."""
    bars = zigzag([0.1, -0.1] * 25)
    swings = find_swings(bars, 2)
    lo = min(b.ohlc.l for b in bars)
    hi = max(b.ohlc.h for b in bars)
    common = dict(
        bars=bars, swings=swings, pools=_pools_inside(lo, hi), mss_events=[], breaks=[],
        direction=Direction.DOWN, atr=0.3, pd_array_strength=0.9,
        as_of=bars[-1].close_time, consolidation_max_range_atr=20.0,
    )
    in_premium = score_mmxm(range_position=0.9, **common)
    in_discount = score_mmxm(range_position=0.1, **common)
    assert in_premium.s_pd_array == pytest.approx(0.9)
    assert in_discount.s_pd_array == 0.0


def test_stage_zero_when_nothing_is_present():
    bars = zigzag([2.0, -1.0] * 20)
    score = score_mmxm(
        bars=bars, swings=[], pools=[], mss_events=[], breaks=[],
        direction=Direction.DOWN, atr=0.3, range_position=None,
        pd_array_strength=0.0, as_of=bars[-1].close_time,
    )
    assert score.stage_reached == 0
    assert score.score == pytest.approx(0.0)
    assert score.consolidation is None


def test_stage_weights_are_a_declared_prior_that_sums_to_one():
    """Fitting weights inside MMXM and also learning a weight on MMXM double-fits."""
    assert sum(STAGE_WEIGHTS) == pytest.approx(1.0)
    assert len(STAGE_WEIGHTS) == 5


def test_side_enum_is_used_for_pool_filtering():
    pools = _pools_inside(99.0, 101.0)
    assert any(p.side is Side.BUY_SIDE for p in pools)
    assert any(p.side is Side.SELL_SIDE for p in pools)


def test_bars_from_helper_builds_valid_ohlc():
    bars = bars_from([(100.0, 101.0, 0.5, 0.5)])
    assert bars[0].ohlc.h == pytest.approx(101.5)
    assert bars[0].ohlc.l == pytest.approx(99.5)
