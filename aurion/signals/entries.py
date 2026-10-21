"""Entry models. `docs/03-signal-engine.md` §2.

Two models only. A third multiplies the candidate population and the trial burden in a
design that `docs/07` §7 shows cannot resolve the modules it already carries -- which is
why the London Close "reversal setups only" window was dropped permanently (Q14) rather
than approximated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from aurion.analysis.base import Direction
from aurion.analysis.ict.fvg import FVG
from aurion.analysis.ict.mss import MarketStructureShift
from aurion.analysis.ict.order_block import OrderBlock
from aurion.analysis.ict.sweeps import Sweep
from aurion.analysis.registry import AnalysisSnapshot


class EntryModel(StrEnum):
    SWEEP_MSS_RETRACE = "sweep_mss_retrace"
    SILVER_BULLET_FVG = "silver_bullet_fvg"


@dataclass(frozen=True, slots=True)
class EntryProposal:
    model: EntryModel
    direction: Direction
    entry: float
    entry_array_label: str
    sweep: Sweep | None
    mss: MarketStructureShift | None
    leg_low: float
    leg_high: float
    confirmed_at: datetime
    invalidation: str

    @property
    def has_sweep(self) -> bool:
        return self.sweep is not None


def _array_entry(
    array: FVG | OrderBlock, fvg_level: str
) -> tuple[float, str]:
    """Entry price inside the retrace array.

    An FVG is entered at its consequent encroachment by default -- the gap midpoint -- and
    an order block at its proximal edge. ``fvg_level: edge`` is the alternative.
    """
    if isinstance(array, FVG):
        if fvg_level == "ce":
            return array.consequent_encroachment, "fvg_ce"
        return array.proximal_edge(), "fvg_edge"
    return array.proximal_edge(), "ob_edge"


def propose_sweep_mss_retrace(
    snapshot: AnalysisSnapshot,
    bias: Direction,
    as_of: datetime,
    mss_window_bars: int,
    fvg_level: str = "ce",
) -> EntryProposal | None:
    """Primary model: sweep -> MSS -> retrace into the array the MSS left behind.

    Requires the MSS, which already requires the sweep (§4.4), so the sweep conjunct is
    inherited rather than re-checked.
    """
    mss = snapshot.last_mss(bias)
    if mss is None or mss.confirmed_at > as_of:
        return None
    bars_since = len(snapshot.bars) - 1 - mss.choch.bar_index
    if bars_since > mss_window_bars:
        return None

    arrays: list[FVG | OrderBlock] = [
        f
        for f in snapshot.fvgs
        if f.available
        and f.effective_direction is bias
        and f.confirmed_at <= as_of
        and f.middle_index >= mss.choch.bar_index - 1
    ]
    arrays += [
        b
        for b in snapshot.order_blocks
        if b.available
        and b.effective_direction is bias
        and b.confirmed_at <= as_of
        and b.origin_index >= mss.choch.bar_index - mss_window_bars
    ]
    if not arrays:
        return None

    price = snapshot.bars[-1].ohlc.c
    array = min(arrays, key=lambda a: abs(a.proximal_edge() - price))
    entry, label = _array_entry(array, fvg_level)

    leg = snapshot.bars[max(mss.sweep.penetration_index, 0) : mss.choch.bar_index + 1]
    leg = leg or snapshot.bars[-2:]
    return EntryProposal(
        model=EntryModel.SWEEP_MSS_RETRACE,
        direction=bias,
        entry=entry,
        entry_array_label=label,
        sweep=mss.sweep,
        mss=mss,
        leg_low=min(b.ohlc.l for b in leg),
        leg_high=max(b.ohlc.h for b in leg),
        confirmed_at=max(mss.confirmed_at, array.confirmed_at),
        invalidation=(
            f"{snapshot.timeframe} closes beyond the stop, or the {label} fully mitigates "
            "without a fill, or an opposing MSS prints"
        ),
    )


def propose_silver_bullet_fvg(
    snapshot: AnalysisSnapshot,
    bias: Direction,
    as_of: datetime,
    window_start: datetime,
    fvg_level: str = "ce",
) -> EntryProposal | None:
    """Time-conditioned variant: an FVG forming inside a Silver Bullet window.

    Shares all machinery with the primary model and is distinguished only by the
    ``entry_model`` feature, which lets the ablation remove it cleanly. It is Tier 2: its
    affected fraction is the share of trades it originates, which puts its resolvable
    effect near 0.6R (`docs/07` §7).
    """
    fresh = [
        f
        for f in snapshot.fvgs
        if f.available
        and f.effective_direction is bias
        and window_start <= f.confirmed_at <= as_of
    ]
    if not fresh:
        return None
    array = max(fresh, key=lambda f: f.confirmed_at)
    entry, label = _array_entry(array, fvg_level)
    leg = snapshot.bars[max(array.middle_index - 1, 0) : array.middle_index + 2]
    leg = leg or snapshot.bars[-2:]
    return EntryProposal(
        model=EntryModel.SILVER_BULLET_FVG,
        direction=bias,
        entry=entry,
        entry_array_label=label,
        sweep=None,
        mss=None,
        leg_low=min(b.ohlc.l for b in leg),
        leg_high=max(b.ohlc.h for b in leg),
        confirmed_at=array.confirmed_at,
        invalidation=(
            f"{snapshot.timeframe} closes beyond the stop, or the {label} fully mitigates "
            "without a fill, or the Silver Bullet window closes"
        ),
    )
