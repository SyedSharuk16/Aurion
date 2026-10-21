"""Stop placement and target selection. `docs/03-signal-engine.md` §4.1, §4.2."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from aurion.analysis.base import Direction
from aurion.analysis.ict.pd_range import DealingRange
from aurion.analysis.ict.pools import LiquidityPool
from aurion.analysis.sr.levels import SRLevel


class TargetKind(StrEnum):
    POOL = "pool"
    ARRAY = "array"
    EQUILIBRIUM = "equilibrium"
    RANGE_EXTREME = "range_extreme"
    LEVEL = "level"


@dataclass(frozen=True, slots=True)
class Target:
    price: float
    kind: TargetKind
    label: str

    def r_multiple(self, entry: float, stop: float) -> float:
        risk = abs(entry - stop)
        return abs(self.price - entry) / risk if risk > 0 else 0.0


@dataclass(frozen=True, slots=True)
class TradeGeometry:
    direction: Direction
    entry: float
    stop_loss: float
    targets: tuple[Target, ...]
    rejected_reason: str | None = None

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def tp1(self) -> Target | None:
        return self.targets[0] if self.targets else None

    @property
    def rr1(self) -> float | None:
        tp = self.tp1
        return tp.r_multiple(self.entry, self.stop_loss) if tp is not None else None

    @property
    def ok(self) -> bool:
        return self.rejected_reason is None and len(self.targets) >= 1

    def r_ladder(self) -> tuple[float, ...]:
        return tuple(t.r_multiple(self.entry, self.stop_loss) for t in self.targets)


def stop_from_sweep(
    direction: Direction, sweep_extremity: float, atr: float, buffer_atr: float
) -> float:
    """SL beyond the swept liquidity plus an ATR buffer (§4.1)."""
    pad = buffer_atr * atr
    return sweep_extremity - pad if direction is Direction.UP else sweep_extremity + pad


def stop_from_leg(
    direction: Direction, leg_low: float, leg_high: float, atr: float, buffer_atr: float
) -> float:
    """Fallback when no sweep preceded the setup, e.g. Silver Bullet."""
    pad = buffer_atr * atr
    return leg_low - pad if direction is Direction.UP else leg_high + pad


def collect_targets(
    direction: Direction,
    entry: float,
    pools: Sequence[LiquidityPool],
    arrays: Sequence[tuple[float, str]],
    levels: Sequence[SRLevel],
    dealing: DealingRange | None,
    min_level_strength: float,
) -> list[Target]:
    """Candidate targets ahead of the trade, ordered by distance from entry.

    Ordering is by distance only. Choosing TP1 by "the best-looking" target rather than the
    nearest qualifying one would be a free parameter hiding in the geometry.
    """
    ahead: list[Target] = []
    forward = 1 if direction is Direction.UP else -1

    for pool in pools:
        if not pool.available:
            continue
        if (pool.level - entry) * forward > 0:
            ahead.append(Target(pool.level, TargetKind.POOL, str(pool.kind)))
    for price, label in arrays:
        if (price - entry) * forward > 0:
            ahead.append(Target(price, TargetKind.ARRAY, label))
    for level in levels:
        if level.strength < min_level_strength:
            continue
        if (level.price - entry) * forward > 0:
            ahead.append(Target(level.price, TargetKind.LEVEL, f"sr{level.strength:.2f}"))
    if dealing is not None and dealing.span > 0:
        for price, kind, label in (
            (dealing.equilibrium, TargetKind.EQUILIBRIUM, "equilibrium"),
            (dealing.high if direction is Direction.UP else dealing.low,
             TargetKind.RANGE_EXTREME, "range_extreme"),
        ):
            if (price - entry) * forward > 0:
                ahead.append(Target(price, kind, label))

    ahead.sort(key=lambda t: abs(t.price - entry))
    return ahead


def build_geometry(
    direction: Direction,
    entry: float,
    stop_loss: float,
    candidates: Sequence[Target],
    rr_min: float,
    atr: float,
    sl_min_atr: float,
) -> TradeGeometry:
    """Assemble the geometry, or reject with a reason the funnel can count.

    The share of candidates rejected for ``no_target_at_min_rr`` is a headline metric: if
    the R:R floor rejects most structurally valid setups, then the constraint -- not the
    analysis -- is the system.
    """
    risk = abs(entry - stop_loss)
    if risk <= 0:
        return TradeGeometry(direction, entry, stop_loss, (), "zero_risk")
    if atr > 0 and risk < sl_min_atr * atr:
        # A stop inside the noise band produces fictitious R multiples in backtest and a
        # live system stopped out by spread.
        return TradeGeometry(direction, entry, stop_loss, (), "sl_below_min_atr")

    tp1 = next((t for t in candidates if t.r_multiple(entry, stop_loss) >= rr_min), None)
    if tp1 is None:
        return TradeGeometry(direction, entry, stop_loss, (), "no_target_at_min_rr")

    forward = 1 if direction is Direction.UP else -1
    beyond = [t for t in candidates if (t.price - tp1.price) * forward > 0]
    tp2 = beyond[0] if beyond else None
    tp3 = next(
        (t for t in beyond[1:] if tp2 is not None and (t.price - tp2.price) * forward > 0),
        None,
    )
    targets = tuple(t for t in (tp1, tp2, tp3) if t is not None)
    return TradeGeometry(direction, entry, stop_loss, targets)
