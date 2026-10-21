"""Liquidity pools. §6.1, §6.2, §6.3.

A pool is a **price-geometry object**, explicitly a proxy for where stop orders may rest,
not a measurement of them. We cannot see resting orders in spot FX or gold CFDs; we observe
only that price sometimes moves beyond a prior extreme and reverses. The inference "stops
were there" is never made, and if pools carry no predictive information the honest
conclusion is that this geometry is uninformative -- not that the liquidity was elsewhere,
which is the unfalsifiable move.

The pool level is the cluster **extreme**, not its mean. A stop-run is triggered by the
extreme; the mean would systematically place the level inside the range where no stops are.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from aurion.analysis.base import Side, SwingKind
from aurion.analysis.structure.swings import Swing
from aurion.core.timeframe import TimeFrame


class PoolKind(StrEnum):
    EQUAL = "equal"
    PREV_DAY_HIGH = "pdh"
    PREV_DAY_LOW = "pdl"
    PREV_WEEK_HIGH = "pwh"
    PREV_WEEK_LOW = "pwl"
    ASIAN_HIGH = "asian_high"
    ASIAN_LOW = "asian_low"
    SESSION_HIGH = "session_high"
    SESSION_LOW = "session_low"


@dataclass(frozen=True, slots=True)
class LiquidityPool:
    instrument: str
    timeframe: TimeFrame
    side: Side
    kind: PoolKind
    level: float
    width: float
    touches: int
    event_time: datetime
    confirmed_at: datetime
    swept_at: datetime | None = None
    sweep_depth_atr: float | None = None

    @property
    def available(self) -> bool:
        """Usable as a target. A swept pool is retained as a feature, not deleted."""
        return self.swept_at is None


def find_equal_pools(
    swings: Sequence[Swing],
    tolerance_atr: float,
    atr: float,
    lookback_swings: int = 40,
    min_members: int = 2,
) -> list[LiquidityPool]:
    """Cluster confirmed swings whose prices lie within ``tolerance_atr x ATR``.

    Greedy single-link clustering over sorted prices. Each cluster needs at least
    ``min_members`` swings of the same kind; the level is the cluster extreme.
    """
    if atr <= 0:
        return []
    tol = tolerance_atr * atr
    out: list[LiquidityPool] = []

    for kind, side in ((SwingKind.HIGH, Side.BUY_SIDE), (SwingKind.LOW, Side.SELL_SIDE)):
        members = [s for s in swings if s.kind is kind][-lookback_swings:]
        if len(members) < min_members:
            continue
        members.sort(key=lambda s: s.price)
        cluster: list[Swing] = []
        for swing in members:
            if cluster and swing.price - cluster[0].price > tol:
                out.extend(_emit_cluster(cluster, side, min_members))
                cluster = []
            cluster.append(swing)
        out.extend(_emit_cluster(cluster, side, min_members))

    out.sort(key=lambda p: p.confirmed_at)
    return out


def _emit_cluster(
    cluster: list[Swing], side: Side, min_members: int
) -> list[LiquidityPool]:
    if len(cluster) < min_members:
        return []
    prices = [s.price for s in cluster]
    level = max(prices) if side is Side.BUY_SIDE else min(prices)
    newest = max(cluster, key=lambda s: s.confirmed_at)
    return [
        LiquidityPool(
            instrument=newest.instrument,
            timeframe=newest.timeframe,
            side=side,
            kind=PoolKind.EQUAL,
            level=level,
            width=max(prices) - min(prices),
            touches=len(cluster),
            event_time=newest.event_time,
            confirmed_at=newest.confirmed_at,
        )
    ]


def reference_pool(
    instrument: str,
    timeframe: TimeFrame,
    kind: PoolKind,
    level: float,
    confirmed_at: datetime,
) -> LiquidityPool:
    """A pool from a period extreme: PDH/PDL, PWH/PWL, Asian or session high/low.

    Confirmation is the end of the period that produced it, so a previous-day high is
    invisible until that day has closed.
    """
    side = Side.BUY_SIDE if kind.value.endswith(("high", "h")) else Side.SELL_SIDE
    return LiquidityPool(
        instrument=instrument,
        timeframe=timeframe,
        side=side,
        kind=kind,
        level=level,
        width=0.0,
        touches=1,
        event_time=confirmed_at,
        confirmed_at=confirmed_at,
    )


def available_pools(
    pools: Sequence[LiquidityPool], as_of: datetime, side: Side | None = None
) -> list[LiquidityPool]:
    out = [p for p in pools if p.confirmed_at <= as_of and p.available]
    if side is not None:
        out = [p for p in out if p.side is side]
    return out


def mark_swept(
    pool: LiquidityPool, swept_at: datetime, depth_atr: float
) -> LiquidityPool:
    return replace(pool, swept_at=swept_at, sweep_depth_atr=depth_atr)
