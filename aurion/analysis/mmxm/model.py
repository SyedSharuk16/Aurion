"""MMXM: the ICT market maker buy/sell model. §9. **Entirely heuristic.**

MMXM is a narrative template over four stages with no canonical numeric specification. It
is scored 0-1 rather than flagged, which is the right call: a binary flag on a template
this loose would be noise.

It is **Tier 2** (`docs/07` §7): built and measured, not selection-eligible in v1. It fails
the resolvability test on its own terms -- Stage-3 confirmation is rare, so its affected
fraction is small, and §3 of that document shows a module firing on 5% of candidates would
need a ~1.4R per-trade effect to be detected at the projected sample size.

Described for the SELL model; the buy model is the mirror. Stage 2 is a run of higher lows
during markup, each leaving resting sell-side liquidity beneath it for the markdown to
harvest.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from aurion.analysis.base import Direction, Side, SwingKind
from aurion.analysis.ict.mss import MarketStructureShift
from aurion.analysis.ict.pools import LiquidityPool
from aurion.analysis.structure.breaks import StructuralBreak
from aurion.analysis.structure.swings import Swing
from aurion.core.timeframe import TimeFrame
from aurion.data.types import Bar

#: Prior weights (§9.3). Declared, NOT fitted. Fitting weights inside MMXM and also
#: learning a weight on MMXM in the outer model would double-fit the same data: one layer
#: of learning, one place.
STAGE_WEIGHTS: tuple[float, float, float, float, float] = (0.15, 0.20, 0.25, 0.30, 0.10)


@dataclass(frozen=True, slots=True)
class Consolidation:
    lo: float
    hi: float
    start_index: int
    end_index: int
    confirmed_at: datetime
    pools_each_side: int

    @property
    def span(self) -> float:
        return self.hi - self.lo


@dataclass(frozen=True, slots=True)
class MmxmScore:
    instrument: str
    timeframe: TimeFrame
    direction: Direction
    stage_reached: int
    score: float
    s_consolidation: float
    s_engineering: float
    s_pd_array: float
    s_mss: float
    s_progress: float
    confirmed_at: datetime
    consolidation: Consolidation | None

    def as_dict(self) -> dict[str, float]:
        return {
            "mmxm_score": self.score,
            "mmxm_stage": float(self.stage_reached),
            "mmxm_s_consolidation": self.s_consolidation,
            "mmxm_s_engineering": self.s_engineering,
            "mmxm_s_pd_array": self.s_pd_array,
            "mmxm_s_mss": self.s_mss,
            "mmxm_s_progress": self.s_progress,
        }


def _clamp(x: float) -> float:
    return min(max(x, 0.0), 1.0)


def find_consolidation(
    bars: Sequence[Bar],
    pools: Sequence[LiquidityPool],
    min_bars: int,
    max_range_atr: float,
    atr: float,
) -> Consolidation | None:
    """Stage 1. The most recent window of at least ``min_bars`` whose range is tight.

    Requires at least two pools on each side inside the window: a range without equal
    highs and equal lows has engineered nothing, which is the whole point of the stage.
    """
    if atr <= 0 or len(bars) < min_bars:
        return None
    limit = max_range_atr * atr
    best: Consolidation | None = None

    for end in range(len(bars) - 1, min_bars - 2, -1):
        start = end - min_bars + 1
        window = bars[start : end + 1]
        hi = max(b.ohlc.h for b in window)
        lo = min(b.ohlc.l for b in window)
        if hi - lo > limit:
            continue
        buy = sum(
            1 for p in pools if p.side is Side.BUY_SIDE and lo <= p.level <= hi
        )
        sell = sum(
            1 for p in pools if p.side is Side.SELL_SIDE and lo <= p.level <= hi
        )
        if buy < 2 or sell < 2:
            continue
        best = Consolidation(
            lo=lo,
            hi=hi,
            start_index=start,
            end_index=end,
            confirmed_at=window[-1].close_time,
            pools_each_side=min(buy, sell),
        )
        break
    return best


def count_engineering_legs(
    swings: Sequence[Swing], after: datetime, direction: Direction
) -> int:
    """Stage 2. Consecutive higher lows (sell model) or lower highs (buy model).

    A sell model is distributed at the top, so the markup leaves higher lows whose stops sit
    beneath them. Counting stops at the first break in the run.
    """
    kind = SwingKind.LOW if direction is Direction.DOWN else SwingKind.HIGH
    seq = sorted(
        (s for s in swings if s.kind is kind and s.event_time > after),
        key=lambda s: s.event_time,
    )
    if len(seq) < 2:
        return 0
    run = 0
    for prev, nxt in itertools.pairwise(seq):
        rising = nxt.price > prev.price
        wanted = rising if direction is Direction.DOWN else not rising
        if wanted:
            run += 1
        else:
            run = 0
    return run


def score_mmxm(
    bars: Sequence[Bar],
    swings: Sequence[Swing],
    pools: Sequence[LiquidityPool],
    mss_events: Sequence[MarketStructureShift],
    breaks: Sequence[StructuralBreak],
    direction: Direction,
    atr: float,
    range_position: float | None,
    pd_array_strength: float,
    as_of: datetime,
    consolidation_min_bars: int = 20,
    consolidation_max_range_atr: float = 2.0,
    engineering_min_legs: int = 2,
    premium_discount_min: float = 0.6,
    weights: tuple[float, float, float, float, float] = STAGE_WEIGHTS,
) -> MmxmScore:
    """Score 0-1 with the furthest stage reached.

    ``stage_reached`` is reported alongside the score because the score alone cannot
    distinguish "all five components weakly present" from "three strongly present".
    """
    consolidation = find_consolidation(
        bars, pools, consolidation_min_bars, consolidation_max_range_atr, atr
    )
    stage = 0
    s_cons = 0.0
    if consolidation is not None:
        stage = 1
        tight = _clamp(1.0 - consolidation.span / max(consolidation_max_range_atr * atr, 1e-12))
        s_cons = tight * _clamp(consolidation.pools_each_side / 4.0)

    legs = (
        count_engineering_legs(swings, bars[consolidation.end_index].open_time, direction)
        if consolidation is not None
        else 0
    )
    s_eng = _clamp(legs / (engineering_min_legs + 2))
    if legs >= engineering_min_legs:
        stage = max(stage, 2)

    in_zone = (
        range_position is not None
        and (
            range_position >= premium_discount_min
            if direction is Direction.DOWN
            else range_position <= 1.0 - premium_discount_min
        )
    )
    s_pd = _clamp(pd_array_strength) if in_zone else 0.0

    relevant_mss = [
        m for m in mss_events if m.direction is direction and m.confirmed_at <= as_of
    ]
    if relevant_mss and in_zone:
        s_mss = 1.0
        stage = max(stage, 3)
    else:
        plain = [
            b
            for b in breaks
            if b.direction is direction and b.confirmed_at <= as_of
        ]
        s_mss = 0.5 if plain else 0.0

    s_prog = 0.0
    if consolidation is not None and relevant_mss:
        smr_price = bars[relevant_mss[-1].choch.bar_index].ohlc.c
        current = bars[-1].ohlc.c
        edge = consolidation.hi if direction is Direction.DOWN else consolidation.lo
        travelled = abs(smr_price - current)
        total = abs(smr_price - edge)
        s_prog = _clamp(travelled / total) if total > 0 else 0.0
        if (
            current <= consolidation.hi
            if direction is Direction.DOWN
            else current >= consolidation.lo
        ):
            stage = max(stage, 4)

    components = (s_cons, s_eng, s_pd, s_mss, s_prog)
    score = sum(w * c for w, c in zip(weights, components, strict=True))

    return MmxmScore(
        instrument=bars[-1].instrument,
        timeframe=bars[-1].timeframe,
        direction=direction,
        stage_reached=stage,
        score=score,
        s_consolidation=s_cons,
        s_engineering=s_eng,
        s_pd_array=s_pd,
        s_mss=s_mss,
        s_progress=s_prog,
        confirmed_at=as_of,
        consolidation=consolidation,
    )
