"""The single place features are computed. `docs/05-data-contracts.md` §4 rule 3.

No module computes a feature inline for a gate; gates consume this vector. That rule exists
to prevent the most insidious backtest/live divergence: the same quantity computed twice
with slightly different edge handling.

Missing optional inputs simply omit their features. Missing *required* inputs leave the
vector incomplete, which `FeatureVector.is_complete` reports and the engine turns into a
``warmup:<field>`` rejection -- never a zero substituted for an unknown.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from aurion.analysis.base import Direction, TrendState
from aurion.analysis.ict.fvg import FVG
from aurion.analysis.ict.order_block import OrderBlock
from aurion.analysis.quarterly.amdx import AmdxScores
from aurion.analysis.quarterly.quarters import Quarter
from aurion.analysis.regime.correlation import VwapState
from aurion.analysis.regime.trend import TrendState as RegimeTrendState
from aurion.analysis.regime.volatility import VolatilityState
from aurion.analysis.registry import AnalysisSnapshot
from aurion.analysis.sr.levels import SRLevel
from aurion.features.schema import FEATURE_SCHEMA_VERSION, FeatureVector

_TREND_SIGN = {TrendState.BULLISH: 1.0, TrendState.BEARISH: -1.0, TrendState.RANGING: 0.0}


@dataclass(slots=True)
class FeatureInputs:
    """Everything the builder needs, gathered by the caller.

    Passed as one object rather than twenty arguments so that adding an input is a visible
    schema change rather than a silently-defaulted keyword.
    """

    instrument: str
    as_of: datetime
    price: float
    direction: Direction
    entry: AnalysisSnapshot
    htf: dict[str, AnalysisSnapshot] = field(default_factory=dict)
    session_minutes_in: int | None = None
    active_windows: tuple[str, ...] = ()
    weekday: int = 0
    volatility: VolatilityState | None = None
    regime_trend: RegimeTrendState | None = None
    vwap: VwapState | None = None
    quarter: Quarter | None = None
    amdx: AmdxScores | None = None
    true_open: float | None = None
    quarterly_bias: dict[str, float] = field(default_factory=dict)
    mmxm: dict[str, float] = field(default_factory=dict)
    macro: dict[str, float] = field(default_factory=dict)
    reference_levels: dict[str, float] = field(default_factory=dict)
    pivot_levels: dict[str, float] = field(default_factory=dict)
    round_tier: float | None = None
    net_usd_exposure: float | None = None
    usd_beta: float | None = None


def _nearest_fvg(fvgs: Sequence[FVG], price: float) -> FVG | None:
    available = [f for f in fvgs if f.available]
    if not available:
        return None
    return min(available, key=lambda f: abs(f.proximal_edge() - price))


def _nearest_block(blocks: Sequence[OrderBlock], price: float) -> OrderBlock | None:
    available = [b for b in blocks if b.available]
    if not available:
        return None
    return min(available, key=lambda b: abs(b.proximal_edge() - price))


def _nearest_level(levels: Sequence[SRLevel], price: float) -> SRLevel | None:
    return min(levels, key=lambda level: abs(level.price - price)) if levels else None


def build_features(
    inputs: FeatureInputs, ote_lo: float = 0.62, ote_hi: float = 0.79
) -> FeatureVector:
    """Build the vector. Every ATR-scaled distance omits itself when ATR is unavailable."""
    v: dict[str, float] = {}
    snap = inputs.entry
    atr = snap.atr
    price = inputs.price

    # -- time ---------------------------------------------------------------
    if inputs.session_minutes_in is not None:
        v["session_minutes_in"] = float(inputs.session_minutes_in)
    windows = set(inputs.active_windows)
    v["session_is_london"] = 1.0 if any("london" in w for w in windows) else 0.0
    v["session_is_ny_am"] = 1.0 if any("ny_am" in w for w in windows) else 0.0
    v["session_is_silver_bullet"] = 1.0 if any("silver" in w for w in windows) else 0.0
    v["weekday"] = float(inputs.weekday)

    # -- structure ----------------------------------------------------------
    for name, key in (("trend_d1", "D1"), ("trend_h4", "H4"), ("trend_h1", "H1")):
        htf = inputs.htf.get(key)
        if htf is not None and htf.structure is not None:
            v[name] = _TREND_SIGN[htf.structure.state]
    if snap.structure is not None:
        v["trend_entry_tf"] = _TREND_SIGN[snap.structure.state]

    if snap.breaks:
        last = snap.breaks[-1]
        bars_since = len(snap.bars) - 1 - last.bar_index
        v["bars_since_break"] = float(max(bars_since, 0))
        v["displacement_body_atr"] = last.displacement.body_atr
        latest_mss = snap.last_mss()
        v["break_is_mss"] = (
            1.0
            if latest_mss is not None and latest_mss.choch.bar_index == last.bar_index
            else 0.0
        )

    # -- ICT arrays ---------------------------------------------------------
    if atr is not None and atr > 0:
        fvg = _nearest_fvg(snap.fvgs, price)
        if fvg is not None:
            v["dist_nearest_fvg_atr"] = (price - fvg.proximal_edge()) / atr
            v["nearest_fvg_fill"] = fvg.fill_frac
            v["nearest_fvg_size_atr"] = fvg.size_atr
            v["in_fvg"] = 1.0 if fvg.contains(price) else 0.0
        block = _nearest_block(snap.order_blocks, price)
        if block is not None:
            v["dist_nearest_ob_atr"] = (price - block.proximal_edge()) / atr
            v["nearest_ob_fill"] = block.fill_frac
            v["in_ob"] = 1.0 if block.contains(price) else 0.0
        if snap.bprs:
            bpr = min(snap.bprs, key=lambda r: abs((r.lo + r.hi) / 2 - price))
            v["dist_nearest_bpr_atr"] = (price - (bpr.lo + bpr.hi) / 2) / atr

        v["n_available_arrays"] = float(
            sum(1 for f in snap.fvgs if f.available)
            + sum(1 for b in snap.order_blocks if b.available)
        )

        htf_dists = []
        for htf in inputs.htf.values():
            if htf.atr is None or htf.atr <= 0:
                continue
            candidate = _nearest_fvg(htf.fvgs, price) or _nearest_block(
                htf.order_blocks, price
            )
            if candidate is not None:
                htf_dists.append(abs(price - candidate.proximal_edge()) / htf.atr)
        if htf_dists:
            v["htf_array_dist_atr"] = min(htf_dists)

        # -- liquidity ------------------------------------------------------
        if snap.sweeps:
            sweep = snap.sweeps[-1]
            v["sweep_depth_atr"] = sweep.depth_atr
            v["bars_since_sweep"] = float(max(len(snap.bars) - 1 - sweep.reclaim_index, 0))
            v["sweep_bars_to_reclaim"] = float(sweep.bars_to_reclaim)
        available_pools = [p for p in snap.pools if p.available]
        if available_pools:
            pool = min(available_pools, key=lambda p: abs(p.level - price))
            v["nearest_pool_dist_atr"] = (price - pool.level) / atr
            v["nearest_pool_touches"] = float(pool.touches)
        for name, key in (
            ("dist_pdh_atr", "pdh"),
            ("dist_pdl_atr", "pdl"),
            ("dist_asian_high_atr", "asian_high"),
            ("dist_asian_low_atr", "asian_low"),
        ):
            level = inputs.reference_levels.get(key)
            if level is not None:
                v[name] = (price - level) / atr

        # -- premium / discount --------------------------------------------
        dr = snap.dealing_range
        if dr is not None and dr.span > 0:
            position = dr.position(price)
            if position is not None:
                v["range_position"] = position
            v["in_ote"] = 1.0 if dr.in_ote(price, inputs.direction, ote_lo, ote_hi) else 0.0
            v["dist_equilibrium_atr"] = (price - dr.equilibrium) / atr
            v["range_span_atr"] = dr.span / atr

        # -- support / resistance ------------------------------------------
        level_obj = _nearest_level(snap.levels, price)
        if level_obj is not None:
            v["dist_nearest_level_atr"] = (price - level_obj.price) / atr
            v["nearest_level_strength"] = level_obj.strength
        if inputs.pivot_levels:
            nearest_pivot = min(
                inputs.pivot_levels.values(), key=lambda p: abs(p - price)
            )
            v["dist_nearest_pivot_atr"] = (price - nearest_pivot) / atr
        if inputs.round_tier:
            nearest_round = round(price / inputs.round_tier) * inputs.round_tier
            v["dist_round_tier1_atr"] = (price - nearest_round) / atr

    # -- quarterly (Tier 2) -------------------------------------------------
    if inputs.quarter is not None:
        v["quarter_index_daily"] = float(inputs.quarter.index)
        v["quarter_elapsed_frac"] = inputs.quarter.elapsed_frac(inputs.as_of)
    if inputs.amdx is not None:
        scores = inputs.amdx.as_dict()
        for name in (
            "amdx_accumulation",
            "amdx_manipulation",
            "amdx_distribution",
            "amdx_continuation",
            "amdx_reversal",
        ):
            v[name] = scores[name]
    v.update({k: val for k, val in inputs.quarterly_bias.items() if k in
              ("to_rel", "to_dist_atr", "qt_align")})

    # -- MMXM (Tier 2) ------------------------------------------------------
    for name in ("mmxm_score", "mmxm_stage"):
        if name in inputs.mmxm:
            v[name] = inputs.mmxm[name]

    # -- regime -------------------------------------------------------------
    if inputs.volatility is not None:
        if inputs.volatility.atr_percentile is not None:
            v["atr_pct"] = inputs.volatility.atr_percentile
        if inputs.volatility.realised_vol is not None:
            v["rv_m15"] = inputs.volatility.realised_vol
        if inputs.volatility.garman_klass is not None:
            v["gk_m15"] = inputs.volatility.garman_klass
    if inputs.regime_trend is not None:
        if inputs.regime_trend.adx.adx is not None:
            v["adx"] = inputs.regime_trend.adx.adx
        if inputs.regime_trend.hurst is not None:
            v["hurst"] = inputs.regime_trend.hurst
        vr4 = inputs.regime_trend.variance_ratios.get(4)
        if vr4 is not None:
            v["vr_4_z"] = vr4.z_star

    # -- volume proxy (Tier 2) ---------------------------------------------
    if inputs.vwap is not None and inputs.vwap.z is not None:
        v["vwap_z"] = inputs.vwap.z

    # -- macro --------------------------------------------------------------
    for name in (
        "us10y_real_d",
        "us10y_real_chg20",
        "us02y_nominal_d",
        "dxy_official_chg1",
        "corr_xau_dxy",
    ):
        if name in inputs.macro:
            v[name] = inputs.macro[name]

    # -- exposure -----------------------------------------------------------
    if inputs.net_usd_exposure is not None:
        v["net_usd_exposure"] = inputs.net_usd_exposure
    if inputs.usd_beta is not None:
        v["usd_beta"] = inputs.usd_beta

    return FeatureVector(
        version=FEATURE_SCHEMA_VERSION,
        instrument=inputs.instrument,
        as_of=inputs.as_of,
        values=v,
    )
