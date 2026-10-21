"""Top-down bias cascade. `docs/03-signal-engine.md` §1 steps 1-2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aurion.analysis.base import Direction, TrendState
from aurion.analysis.ict.fvg import FVG
from aurion.analysis.ict.order_block import OrderBlock
from aurion.analysis.registry import AnalysisSnapshot

_SIGN = {TrendState.BULLISH: Direction.UP, TrendState.BEARISH: Direction.DOWN}


@dataclass(frozen=True, slots=True)
class HtfBias:
    direction: Direction | None
    w1: TrendState | None
    d1: TrendState | None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.direction is not None


def htf_bias(
    w1: AnalysisSnapshot | None,
    d1: AnalysisSnapshot | None,
    require_agreement: bool = True,
) -> HtfBias:
    """W1 and D1 structure. ``None`` when they disagree and agreement is required.

    Requiring concordance is a gate the brief specified. It is also the second-largest
    trade-count lever in `docs/07` §11 (1.82x), so its cost is measured rather than
    assumed: the funnel counts how many candidates it kills.
    """
    w1_state = w1.structure.state if w1 is not None and w1.structure is not None else None
    d1_state = d1.structure.state if d1 is not None and d1.structure is not None else None
    if w1_state is None or d1_state is None:
        return HtfBias(None, w1_state, d1_state, "bias:htf_structure_unavailable")

    w1_dir = _SIGN.get(w1_state)
    d1_dir = _SIGN.get(d1_state)
    if require_agreement:
        if w1_dir is None or d1_dir is None or w1_dir is not d1_dir:
            return HtfBias(None, w1_state, d1_state, "bias:w1_d1_disagree")
        return HtfBias(w1_dir, w1_state, d1_state)
    chosen = d1_dir or w1_dir
    if chosen is None:
        return HtfBias(None, w1_state, d1_state, "bias:no_directional_structure")
    return HtfBias(chosen, w1_state, d1_state)


@dataclass(frozen=True, slots=True)
class HtfArray:
    price: float
    label: str
    distance_atr: float
    strength: float
    timeframe: str


def nearest_htf_array(
    snapshots: dict[str, AnalysisSnapshot],
    price: float,
    direction: Direction,
    max_distance_atr: float,
    as_of: datetime,
) -> HtfArray | None:
    """Nearest unmitigated higher-timeframe PD array in the bias direction.

    Step 2 of the cascade, and in the projected funnel one of the heaviest filters. The
    backtest reports how many candidates it rejects, because a step that kills 98% of them
    is the real model whatever the rest of the pipeline does.
    """
    best: HtfArray | None = None
    for timeframe, snap in snapshots.items():
        atr = snap.atr
        if atr is None or atr <= 0:
            continue
        arrays: list[FVG | OrderBlock] = [
            f
            for f in snap.fvgs
            if f.available and f.effective_direction is direction and f.confirmed_at <= as_of
        ]
        arrays += [
            b
            for b in snap.order_blocks
            if b.available and b.effective_direction is direction and b.confirmed_at <= as_of
        ]
        for array in arrays:
            distance = abs(price - array.proximal_edge()) / atr
            if distance > max_distance_atr:
                continue
            strength = 1.0 - min(distance / max_distance_atr, 1.0)
            label = "fvg" if isinstance(array, FVG) else "ob"
            if best is None or distance < best.distance_atr:
                best = HtfArray(
                    array.proximal_edge(), f"{timeframe}_{label}", distance, strength, timeframe
                )
    return best
