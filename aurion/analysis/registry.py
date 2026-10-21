"""Analysis snapshots and the object repository. `docs/01-architecture.md` §3, §8.

One place computes derived objects for one ``(instrument, timeframe)`` as of one instant,
and caches the result on ``(instrument, timeframe, last_closed_bar_time, params_hash)``.
Memoising on the last closed bar is what keeps the cost O(1) per bar close on a 1 vCPU box;
including the params hash is what stops a config change silently reusing stale objects.

`ObjectStore` implements the `ObjectRepository` protocol, so `MarketView.objects` filters
every query on ``confirmed_at`` -- there is no path that returns an unconfirmed object.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from aurion.analysis.base import Direction
from aurion.analysis.ict.fvg import FVG, BalancedPriceRange, find_bprs, find_fvgs
from aurion.analysis.ict.mss import MarketStructureShift, find_mss
from aurion.analysis.ict.order_block import OrderBlock, find_order_blocks
from aurion.analysis.ict.pd_range import DealingRange, dealing_range
from aurion.analysis.ict.pools import LiquidityPool, find_equal_pools, mark_swept
from aurion.analysis.ict.sweeps import Sweep, find_sweeps
from aurion.analysis.primitives import atr_series
from aurion.analysis.sr.levels import SRLevel, kde_levels, merge_levels, weight_swings
from aurion.analysis.structure.breaks import StructuralBreak, find_breaks, last_break
from aurion.analysis.structure.swings import Swing, find_swings
from aurion.analysis.structure.trend import Structure, trend_state
from aurion.core.marketview import Confirmable, MarketView
from aurion.core.timeframe import TimeFrame
from aurion.data.types import Bar


@dataclass(frozen=True, slots=True)
class AnalysisParams:
    """Everything the detectors need, resolved from config once.

    ``selectivity`` has already collapsed ``displacement_k``, ``fvg_min_atr`` and
    ``sweep_min_penetration_atr`` into one ordinal (`docs/07` §6), so those three arrive
    here already consistent rather than as three independent knobs.
    """

    atr_period: int = 14
    swing_half_width: int = 2
    displacement_k: float = 1.0
    fvg_min_atr: float = 0.10
    sweep_min_penetration_atr: float = 0.05
    sweep_reclaim_buffer_atr: float = 0.0
    sweep_max_bars: int = 3
    pool_tolerance_atr: float = 0.15
    pool_lookback_bars: int = 100
    ob_lookback_bars: int = 10
    ob_extent: str = "wick"
    breaker_window_bars: int = 20
    bpr_window_bars: int = 10
    mss_sweep_window_bars: int = 10
    sr_halflife_bars: float = 250.0
    sr_touch_tolerance_atr: float = 0.15
    sr_touch_min_separation: int = 5
    sr_bandwidth_atr: float = 0.25
    sr_merge_distance_atr: float = 0.3
    point: float = 0.00001

    def signature(self) -> tuple[object, ...]:
        return tuple(getattr(self, f) for f in self.__slots__)


@dataclass(frozen=True, slots=True)
class AnalysisSnapshot:
    """All derived objects for one instrument/timeframe as of one instant."""

    instrument: str
    timeframe: TimeFrame
    as_of: datetime
    bars: Sequence[Bar]
    atr: float | None
    swings: list[Swing] = field(default_factory=list)
    structure: Structure | None = None
    breaks: list[StructuralBreak] = field(default_factory=list)
    fvgs: list[FVG] = field(default_factory=list)
    order_blocks: list[OrderBlock] = field(default_factory=list)
    bprs: list[BalancedPriceRange] = field(default_factory=list)
    pools: list[LiquidityPool] = field(default_factory=list)
    sweeps: list[Sweep] = field(default_factory=list)
    mss: list[MarketStructureShift] = field(default_factory=list)
    dealing_range: DealingRange | None = None
    levels: list[SRLevel] = field(default_factory=list)

    @property
    def last_close(self) -> float | None:
        return self.bars[-1].ohlc.c if self.bars else None

    def last_mss(self, direction: Direction | None = None) -> MarketStructureShift | None:
        eligible = [
            m for m in self.mss if direction is None or m.direction is direction
        ]
        return max(eligible, key=lambda m: m.confirmed_at) if eligible else None


_KINDS = (
    "swing",
    "break",
    "fvg",
    "order_block",
    "bpr",
    "pool",
    "sweep",
    "mss",
    "level",
)


class ObjectStore:
    """Computes, caches and serves derived objects."""

    __slots__ = ("_cache", "_hits", "_levels", "_misses", "_params")

    def __init__(self, params: AnalysisParams) -> None:
        self._params = params
        self._cache: dict[
            tuple[str, TimeFrame, datetime, tuple[object, ...]], AnalysisSnapshot
        ] = {}
        # The KDE fit is the only expensive step and `docs/01-architecture.md` §8 says to
        # recompute it on D1 close rather than per bar. Levels are therefore cached
        # separately from the snapshot and reused until a refresh is requested.
        self._levels: dict[tuple[str, TimeFrame], tuple[datetime, list[SRLevel]]] = {}
        self._hits = 0
        self._misses = 0

    @property
    def cache_stats(self) -> tuple[int, int]:
        return self._hits, self._misses

    def snapshot(
        self,
        view: MarketView,
        instrument: str,
        timeframe: TimeFrame,
        window: int = 500,
        with_levels: bool = False,
        refresh_levels: bool = False,
    ) -> AnalysisSnapshot:
        bars = list(view.usable_bars(instrument, timeframe, window))
        if not bars:
            return AnalysisSnapshot(instrument, timeframe, view.as_of, [], None)

        key = (instrument, timeframe, bars[-1].close_time, self._params.signature())
        cached = self._cache.get(key)
        # Serve the cached snapshot unless the caller wants levels this one does not carry.
        # `refresh_levels` is deliberately not part of this test: a refresh is requested on a
        # D1 close, which is a new `close_time` and so a new key, and reaches `_compute`
        # anyway. Including it here would let a `refresh_levels=False` caller be served a
        # level-less snapshot cached by an earlier `with_levels=False` call.
        if cached is not None and not (with_levels and not cached.levels):
            self._hits += 1
            return cached
        self._misses += 1

        snapshot = self._compute(
            view.as_of, bars, instrument, timeframe, with_levels, refresh_levels
        )
        self._cache[key] = snapshot
        if len(self._cache) > 512:
            # Bounded: the hot set is one key per instrument/timeframe per bar close.
            for stale in list(self._cache)[:128]:
                del self._cache[stale]
        return snapshot

    def _compute(
        self,
        as_of: datetime,
        bars: list[Bar],
        instrument: str,
        timeframe: TimeFrame,
        with_levels: bool,
        refresh_levels: bool = False,
    ) -> AnalysisSnapshot:
        p = self._params
        # One true-range pass shared by every detector that needs ATR.
        atrs = atr_series(bars, p.atr_period)
        atr_value = atrs[-1] if atrs else None

        swings = find_swings(bars, p.swing_half_width)
        structure = trend_state(swings, as_of)
        breaks = find_breaks(bars, swings, p.displacement_k, p.point, p.atr_period, atrs)
        fvgs = find_fvgs(bars, p.fvg_min_atr, p.point, p.atr_period, atrs=atrs)
        blocks = find_order_blocks(
            bars, breaks, p.ob_lookback_bars, p.point, p.ob_extent, p.breaker_window_bars
        )
        bprs = find_bprs(fvgs, p.bpr_window_bars, timeframe.minutes)

        pools: list[LiquidityPool] = []
        sweeps: list[Sweep] = []
        if atr_value is not None:
            pools = find_equal_pools(
                swings, p.pool_tolerance_atr, atr_value, p.pool_lookback_bars
            )
            sweeps = find_sweeps(
                bars,
                pools,
                p.sweep_min_penetration_atr,
                p.sweep_reclaim_buffer_atr,
                p.sweep_max_bars,
                p.atr_period,
                atrs,
            )
            swept = {(s.pool_level, s.side): s for s in sweeps}
            pools = [
                mark_swept(pool, hit.confirmed_at, hit.depth_atr)
                if (hit := swept.get((pool.level, pool.side))) is not None
                else pool
                for pool in pools
            ]

        mss = find_mss(breaks, sweeps, p.mss_sweep_window_bars)
        last = last_break(breaks, as_of)
        range_ = dealing_range(bars, swings, last, as_of) if last is not None else None

        levels: list[SRLevel] = []
        if with_levels and atr_value is not None and swings:
            cached_levels = self._levels.get((instrument, timeframe))
            # The stored timestamp is a causality guard, not a diagnostic. This cache is keyed
            # only by instrument/timeframe, so without it a replay of an earlier prefix on the
            # same store would be served levels fitted at a later `as_of` -- look-ahead
            # arriving through the cache instead of through the data.
            if cached_levels is not None and cached_levels[0] <= as_of and not refresh_levels:
                levels = cached_levels[1]
            else:
                weights = weight_swings(
                    swings,
                    bars,
                    p.sr_halflife_bars,
                    p.sr_touch_tolerance_atr,
                    atr_value,
                    p.sr_touch_min_separation,
                )
                levels = merge_levels(
                    kde_levels(weights, p.sr_bandwidth_atr * atr_value, as_of),
                    p.sr_merge_distance_atr * atr_value,
                )
                self._levels[(instrument, timeframe)] = (as_of, levels)

        return AnalysisSnapshot(
            instrument=instrument,
            timeframe=timeframe,
            as_of=as_of,
            bars=bars,
            atr=atr_value,
            swings=swings,
            structure=structure,
            breaks=breaks,
            fvgs=fvgs,
            order_blocks=blocks,
            bprs=bprs,
            pools=pools,
            sweeps=sweeps,
            mss=mss,
            dealing_range=range_,
            levels=levels,
        )

    # -- ObjectRepository --------------------------------------------------
    def objects(
        self, kind: str, instrument: str, timeframe: TimeFrame, as_of: datetime
    ) -> Sequence[Confirmable]:
        """Serve cached objects of one kind, filtered on ``confirmed_at``.

        Serves only what a prior `snapshot` call already computed: this accessor must not
        trigger computation, or a query at time ``t`` could build objects from a window the
        caller never asked for.
        """
        if kind not in _KINDS:
            raise ValueError(f"unknown object kind {kind!r}; expected one of {_KINDS}")
        best: AnalysisSnapshot | None = None
        for (inst, tf, close_time, _), snap in self._cache.items():
            if inst != instrument or tf is not timeframe or close_time > as_of:
                continue
            if best is None or close_time > best.bars[-1].close_time:
                best = snap
        if best is None:
            return ()
        by_kind: dict[str, Sequence[Confirmable]] = {
            "swing": best.swings,
            "break": best.breaks,
            "fvg": best.fvgs,
            "order_block": best.order_blocks,
            "bpr": best.bprs,
            "pool": best.pools,
            "sweep": best.sweeps,
            "mss": best.mss,
            "level": best.levels,
        }
        return [o for o in by_kind[kind] if o.confirmed_at <= as_of]


def params_from_config(cfg: object, point: float) -> AnalysisParams:
    """Build params from an `AurionConfig`, resolving the selectivity ordinal."""
    analysis = cfg.analysis  # type: ignore[attr-defined]
    sel = analysis.active_selectivity
    structure = analysis.model_extra.get("structure", {}) if analysis.model_extra else {}
    ict = analysis.model_extra.get("ict", {}) if analysis.model_extra else {}
    liquidity = analysis.model_extra.get("liquidity", {}) if analysis.model_extra else {}
    sr = analysis.model_extra.get("sr", {}) if analysis.model_extra else {}
    widths = structure.get("swing_half_width", {}) or {}
    return AnalysisParams(
        atr_period=analysis.atr_period,
        swing_half_width=int(widths.get("M15", 2)),
        displacement_k=sel.displacement_k,
        fvg_min_atr=sel.fvg_min_atr,
        sweep_min_penetration_atr=sel.sweep_min_penetration_atr,
        sweep_reclaim_buffer_atr=float(liquidity.get("sweep_reclaim_buffer_atr", 0.0)),
        sweep_max_bars=int(liquidity.get("sweep_max_bars", 3)),
        pool_tolerance_atr=float(liquidity.get("pool_tolerance_atr", 0.15)),
        pool_lookback_bars=int(liquidity.get("pool_lookback_bars", 100)),
        ob_lookback_bars=int(ict.get("ob_lookback_bars", 10)),
        ob_extent=str(ict.get("ob_extent", "wick")),
        breaker_window_bars=int(ict.get("breaker_window_bars", 20)),
        bpr_window_bars=int(ict.get("bpr_window_bars", 10)),
        mss_sweep_window_bars=int(structure.get("mss_sweep_window_bars", 10)),
        sr_halflife_bars=float(sr.get("recency_halflife_bars", 250)),
        sr_touch_tolerance_atr=float(sr.get("touch_tolerance_atr", 0.15)),
        sr_touch_min_separation=int(sr.get("touch_min_separation_bars", 5)),
        sr_bandwidth_atr=float(sr.get("kde_bandwidth_atr", 0.25)),
        sr_merge_distance_atr=float(sr.get("merge_distance_atr", 0.3)),
        point=point,
    )
