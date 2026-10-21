"""AMDX phase scoring. §8.5, §8.6. **Heuristic, and the biggest look-ahead trap.**

The textbook AMDX label of a quarter is read off the *completed* quarter. Using that label
to trade inside the same quarter is look-ahead, full stop. Two distinct objects are
therefore produced and never mixed:

* **Retrospective label** -- on a completed quarter, confirmed at its end. For research,
  for measuring the behavioural claim, and as a feature about *prior* quarters only.
* **Running scores** -- on the in-progress quarter from closed bars only, carrying
  ``elapsed_frac``. These are what a live signal may use. They are weaker and differently
  distributed, so the model treats them as separate features and never reuses coefficients
  fitted on the retrospective ones.

The four scores are **not mutually exclusive** by construction, which is why four are
emitted rather than one label. A quarter that both ranges narrowly and sweeps is genuinely
ambiguous; forcing a single label would discard that and manufacture false precision.

The AMDX ordering claim -- Q1 accumulation, Q2 manipulation, Q3 distribution, Q4
continuation or reversal -- is an unvalidated assertion, not a definition. `phase_frequency`
measures it. A near-uniform result falsifies the claim and the module is disabled.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from aurion.analysis.base import Direction
from aurion.analysis.quarterly.quarters import Quarter
from aurion.data.types import Bar


class Phase(StrEnum):
    ACCUMULATION = "accumulation"
    MANIPULATION = "manipulation"
    DISTRIBUTION = "distribution"
    CONTINUATION = "continuation"
    REVERSAL = "reversal"


@dataclass(frozen=True, slots=True)
class QuarterGeometry:
    """§8.4. Every quantity uses only bars closed at or before ``as_of``."""

    quarter: Quarter
    open: float
    high: float
    low: float
    close: float
    rel_range: float
    efficiency: float
    close_position: float
    elapsed_frac: float
    bar_count: int

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def direction(self) -> Direction:
        return Direction.UP if self.close >= self.open else Direction.DOWN


def quarter_geometry(
    quarter: Quarter,
    bars: Sequence[Bar],
    as_of: datetime,
    prior_ranges: Sequence[float] = (),
) -> QuarterGeometry | None:
    """Geometry of a quarter from closed bars only, partial or complete."""
    inside = [b for b in bars if quarter.contains(b.open_time) and b.close_time <= as_of]
    if not inside:
        return None
    o = inside[0].ohlc.o
    c = inside[-1].ohlc.c
    hi = max(b.ohlc.h for b in inside)
    lo = min(b.ohlc.l for b in inside)
    span = hi - lo
    median_prior = (
        sorted(prior_ranges)[len(prior_ranges) // 2] if prior_ranges else 0.0
    )
    return QuarterGeometry(
        quarter=quarter,
        open=o,
        high=hi,
        low=lo,
        close=c,
        rel_range=span / median_prior if median_prior > 0 else 1.0,
        efficiency=abs(c - o) / span if span > 0 else 0.0,
        close_position=(c - lo) / span if span > 0 else 0.5,
        elapsed_frac=quarter.elapsed_frac(as_of),
        bar_count=len(inside),
    )


@dataclass(frozen=True, slots=True)
class AmdxScores:
    accumulation: float
    manipulation: float
    distribution: float
    continuation: float
    reversal: float
    label: Phase
    margin: float
    is_partial: bool
    elapsed_frac: float

    def as_dict(self) -> dict[str, float]:
        return {
            "amdx_accumulation": self.accumulation,
            "amdx_manipulation": self.manipulation,
            "amdx_distribution": self.distribution,
            "amdx_continuation": self.continuation,
            "amdx_reversal": self.reversal,
            "amdx_margin": self.margin,
            "amdx_elapsed_frac": self.elapsed_frac,
        }


def _clamp(x: float) -> float:
    return min(max(x, 0.0), 1.0)


def amdx_scores(
    geometry: QuarterGeometry,
    htf_bias: Direction,
    prior_high: float | None,
    prior_low: float | None,
    swept_prior_against_bias: bool,
    sweep_depth_atr: float,
    alpha_range: float = 0.8,
    beta_efficiency: float = 0.35,
    gamma_efficiency: float = 0.6,
    zeta_sweep_depth: float = 0.5,
) -> AmdxScores:
    """All four scores, plus the argmax and its margin.

    The four constants are **not tunable**: they define what the words mean rather than
    being thresholds to fit, and fitting them would make the module unfalsifiable -- any
    quarter could be relabelled to match its outcome.
    """
    s_acc = _clamp(1.0 - geometry.rel_range / alpha_range) * _clamp(
        1.0 - geometry.efficiency / beta_efficiency
    )
    s_man = (
        _clamp(sweep_depth_atr / zeta_sweep_depth)
        if swept_prior_against_bias and geometry.elapsed_frac <= 0.5
        else 0.0
    )
    aligned = geometry.direction is htf_bias
    s_dist = _clamp(geometry.efficiency / gamma_efficiency) if aligned else 0.0

    beyond_with_bias = False
    beyond_against_bias = False
    if htf_bias is Direction.UP and prior_high is not None:
        beyond_with_bias = geometry.close > prior_high
    if htf_bias is Direction.UP and prior_low is not None:
        beyond_against_bias = geometry.close < prior_low
    if htf_bias is Direction.DOWN and prior_low is not None:
        beyond_with_bias = geometry.close < prior_low
    if htf_bias is Direction.DOWN and prior_high is not None:
        beyond_against_bias = geometry.close > prior_high

    s_cont = geometry.efficiency if beyond_with_bias else 0.0
    s_rev = geometry.efficiency if beyond_against_bias else 0.0

    pairs = [
        (Phase.ACCUMULATION, s_acc),
        (Phase.MANIPULATION, s_man),
        (Phase.DISTRIBUTION, s_dist),
        (Phase.CONTINUATION, s_cont),
        (Phase.REVERSAL, s_rev),
    ]
    pairs.sort(key=lambda p: p[1], reverse=True)
    label, best = pairs[0]
    margin = best - pairs[1][1]

    return AmdxScores(
        accumulation=s_acc,
        manipulation=s_man,
        distribution=s_dist,
        continuation=s_cont,
        reversal=s_rev,
        label=label,
        margin=margin,
        is_partial=geometry.elapsed_frac < 1.0,
        elapsed_frac=geometry.elapsed_frac,
    )


def phase_frequency(labelled: Sequence[tuple[int, Phase]]) -> dict[int, dict[str, float]]:
    """Empirical phase frequency by quarter index -- the test of the behavioural claim.

    §8.0 commits to reporting this. A distribution statistically indistinguishable from
    uniform means Quarterly Theory carries no information and the module is disabled.
    """
    by_index: dict[int, Counter[str]] = {}
    for index, phase in labelled:
        by_index.setdefault(index, Counter())[str(phase)] += 1
    out: dict[int, dict[str, float]] = {}
    for index, counts in by_index.items():
        total = sum(counts.values())
        out[index] = {k: v / total for k, v in counts.items()}
    return out


def true_open_bias(
    price: float, true_open: float, quarter_index: int, direction: Direction, atr: float
) -> dict[str, float]:
    """§8.6 as FEATURES, not as a gate.

    The brief's rule -- longs preferred below the relevant true open in Q2/Q3, shorts above
    -- is emitted as a 0/1 ``qt_align`` feature so the backtest can measure its
    contribution, instead of silently discarding half the candidate population on an
    untested prior. If it earns a large coefficient, promoting it to a gate is a later,
    evidence-backed decision.
    """
    rel = 0.0 if price == true_open else (1.0 if price > true_open else -1.0)
    in_q23 = 1.0 if quarter_index in (2, 3) else 0.0
    aligned = 1.0 if (in_q23 and float(direction) == -rel) else 0.0
    return {
        "to_rel": rel,
        "to_dist_atr": (price - true_open) / atr if atr > 0 else 0.0,
        "in_q2_or_q3": in_q23,
        "qt_align": aligned,
    }
