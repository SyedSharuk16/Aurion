"""Signal engine: the cascade and the decision rule. `docs/03-signal-engine.md` §1, §3.

A candidate row is written at **every** step that rejects, with the step and the reason.
The backtest reports that funnel, because a step that kills 98% of candidates is the real
model whatever the rest of the pipeline does -- and that is only visible if the rejections
are counted rather than discarded.

The decision rule gates on **expectancy**, not on a score:

    E[R] = p_hat * R1 - (1 - p_hat)

with ``p_hat`` a calibrated probability. A score threshold is indifferent between a
candidate paying 2.0R and one paying 4.5R at the same win probability; expectancy is not.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum

from aurion.analysis.base import Direction
from aurion.analysis.regime.correlation import VwapState, net_usd_exposure
from aurion.analysis.regime.trend import TrendState as RegimeTrendState
from aurion.analysis.regime.volatility import VolatilityState
from aurion.analysis.registry import AnalysisSnapshot, ObjectStore
from aurion.core.marketview import MarketView
from aurion.core.timeframe import TimeFrame
from aurion.features.build import FeatureInputs, build_features
from aurion.features.schema import FEATURE_SCHEMA_VERSION, FeatureVector
from aurion.sessions.gates import AdmissibilityGate, GateResult
from aurion.signals.bias import HtfArray, HtfBias, htf_bias, nearest_htf_array
from aurion.signals.entries import (
    EntryModel,
    EntryProposal,
    propose_silver_bullet_fvg,
    propose_sweep_mss_retrace,
)
from aurion.signals.geometry import (
    TradeGeometry,
    build_geometry,
    collect_targets,
    stop_from_leg,
    stop_from_sweep,
)
from aurion.signals.model import ConstantProbabilityModel, LogisticModel
from aurion.signals.sizing import PositionSize, position_size


class Step(IntEnum):
    HTF_BIAS = 1
    HTF_ARRAY = 2
    ENTRY_MODEL = 3
    GATES = 4
    GEOMETRY = 5
    FEATURES = 6
    MODEL = 7
    DECISION = 8
    EMITTED = 9


@dataclass(frozen=True, slots=True)
class InstrumentMeta:
    """Sizing and exposure facts about one instrument, resolved from config once."""

    point: float
    contract_size: float
    base_ccy: str
    quote_ccy: str

    @property
    def usd_is_base(self) -> bool:
        return self.base_ccy == "USD"


@dataclass(frozen=True, slots=True)
class EngineParams:
    require_w1_d1_agreement: bool = True
    pd_max_dist_atr: float = 2.0
    mss_window_bars: int = 10
    fvg_level: str = "ce"
    entry_models: tuple[EntryModel, ...] = (
        EntryModel.SWEEP_MSS_RETRACE,
        EntryModel.SILVER_BULLET_FVG,
    )
    rr_min: float = 2.0
    expected_r_min: float = 0.15
    p_floor: float = 0.25
    sl_buffer_atr: float = 0.25
    sl_min_atr: float = 0.3
    tp_min_level_strength: float = 0.4
    max_pending_bars: int = 12
    expiry_grace_minutes: int = 0
    ote_lo: float = 0.62
    ote_hi: float = 0.79
    max_usd_risk_fraction: float = 1.5
    risk_pct: float = 0.5
    account_size: float = 10_000.0
    account_ccy: str = "USD"
    lot_step: float = 0.01
    min_lot: float = 0.01


@dataclass(slots=True)
class Candidate:
    """Every evaluation produces one, accepted or rejected."""

    instrument: str
    as_of: datetime
    reached_step: Step
    rejected_reason: str | None = None
    direction: Direction | None = None
    entry_model: EntryModel | None = None
    bias: HtfBias | None = None
    htf_array: HtfArray | None = None
    proposal: EntryProposal | None = None
    gate: GateResult | None = None
    geometry: TradeGeometry | None = None
    features: FeatureVector | None = None
    p_hat: float | None = None
    expected_r: float | None = None
    reasons: tuple[tuple[str, float], ...] = ()
    size: PositionSize | None = None
    expires_at: datetime | None = None
    config_hash: str = ""
    feature_schema_version: int = FEATURE_SCHEMA_VERSION

    @property
    def emitted(self) -> bool:
        return self.reached_step is Step.EMITTED and self.rejected_reason is None

    @property
    def signal_id(self) -> str:
        raw = f"{self.instrument}|{self.as_of.isoformat()}|{self.entry_model}|{self.direction}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


@dataclass(slots=True)
class EvaluationContext:
    """Per-evaluation inputs the engine does not compute itself."""

    entry_timeframe: TimeFrame = TimeFrame.M15
    htf: dict[str, AnalysisSnapshot] = field(default_factory=dict)
    volatility: VolatilityState | None = None
    regime_trend: RegimeTrendState | None = None
    vwap: VwapState | None = None
    quarterly_bias: dict[str, float] = field(default_factory=dict)
    quarter_index: int | None = None
    macro: dict[str, float] = field(default_factory=dict)
    reference_levels: dict[str, float] = field(default_factory=dict)
    pivot_levels: dict[str, float] = field(default_factory=dict)
    round_tier: float | None = None
    active_usd_exposure: Sequence[float] = ()
    usd_beta: float | None = None
    silver_bullet_window_start: datetime | None = None
    spread: float | None = None
    weekday: int = 0


class SignalEngine:
    """Runs the cascade for one instrument at one instant."""

    __slots__ = ("_gate", "_instrument_meta", "_model", "_params", "_store")

    def __init__(
        self,
        params: EngineParams,
        store: ObjectStore,
        gate: AdmissibilityGate,
        instrument_meta: dict[str, InstrumentMeta],
        model: LogisticModel | ConstantProbabilityModel | None = None,
    ) -> None:
        self._params = params
        self._store = store
        self._gate = gate
        self._model: LogisticModel | ConstantProbabilityModel | None = model
        self._instrument_meta: dict[str, InstrumentMeta] = instrument_meta

    def evaluate(
        self,
        view: MarketView,
        instrument: str,
        as_of: datetime,
        context: EvaluationContext,
        window: int = 500,
        refresh_levels: bool = False,
    ) -> Candidate:
        p = self._params
        snapshot = self._store.snapshot(
            view,
            instrument,
            context.entry_timeframe,
            window,
            with_levels=True,
            refresh_levels=refresh_levels,
        )
        if not snapshot.bars:
            return Candidate(instrument, as_of, Step.HTF_BIAS, "data:no_bars")
        price = snapshot.bars[-1].ohlc.c

        # -- step 1: HTF bias ---------------------------------------------
        bias = htf_bias(
            context.htf.get("W1"), context.htf.get("D1"), p.require_w1_d1_agreement
        )
        if not bias.ok or bias.direction is None:
            return Candidate(instrument, as_of, Step.HTF_BIAS, bias.reason, bias=bias)
        direction = bias.direction

        # -- step 2: HTF PD array -----------------------------------------
        array = nearest_htf_array(
            {k: v for k, v in context.htf.items() if k in ("H4", "H1")},
            price,
            direction,
            p.pd_max_dist_atr,
            as_of,
        )
        if array is None:
            return Candidate(
                instrument, as_of, Step.HTF_ARRAY, "pd:no_htf_array_in_range",
                direction=direction, bias=bias,
            )

        # -- step 3: entry model ------------------------------------------
        proposal = self._propose(snapshot, direction, as_of, context)
        if proposal is None:
            return Candidate(
                instrument, as_of, Step.ENTRY_MODEL, "entry:no_model_fired",
                direction=direction, bias=bias, htf_array=array,
            )

        # -- step 4: gates -------------------------------------------------
        gate = self._gate.evaluate(instrument, as_of, view, context.spread)
        if not gate.admissible:
            return Candidate(
                instrument, as_of, Step.GATES, gate.first_reason,
                direction=direction, entry_model=proposal.model, bias=bias,
                htf_array=array, proposal=proposal, gate=gate,
            )

        # -- step 5: geometry ----------------------------------------------
        geometry = self._geometry(snapshot, proposal, direction, context)
        if not geometry.ok:
            return Candidate(
                instrument, as_of, Step.GEOMETRY,
                geometry.rejected_reason or "geometry:unknown",
                direction=direction, entry_model=proposal.model, bias=bias,
                htf_array=array, proposal=proposal, gate=gate, geometry=geometry,
            )

        # -- exposure ------------------------------------------------------
        meta = self._instrument_meta[instrument]
        beta = context.usd_beta if context.usd_beta is not None else 1.0
        candidate_exposure = (
            int(direction)
            * (1.0 if meta.usd_is_base else -1.0)
            * (p.risk_pct / 100.0)
            * beta
        )
        exposure = net_usd_exposure(
            list(context.active_usd_exposure),
            candidate_exposure,
            p.max_usd_risk_fraction * (p.risk_pct / 100.0),
        )
        if not exposure.ok:
            return Candidate(
                instrument, as_of, Step.GATES, exposure.reason,
                direction=direction, entry_model=proposal.model, bias=bias,
                htf_array=array, proposal=proposal, gate=gate, geometry=geometry,
            )

        # -- step 6: features ----------------------------------------------
        features = build_features(
            FeatureInputs(
                instrument=instrument,
                as_of=as_of,
                price=geometry.entry,
                direction=direction,
                entry=snapshot,
                htf=context.htf,
                session_minutes_in=self._minutes_in(gate),
                active_windows=gate.windows,
                weekday=context.weekday,
                volatility=context.volatility,
                regime_trend=context.regime_trend,
                vwap=context.vwap,
                quarterly_bias=context.quarterly_bias,
                macro=context.macro,
                reference_levels=context.reference_levels,
                pivot_levels=context.pivot_levels,
                round_tier=context.round_tier,
                net_usd_exposure=exposure.combined,
                usd_beta=context.usd_beta,
            ),
            p.ote_lo,
            p.ote_hi,
        )
        if not features.is_complete:
            return Candidate(
                instrument, as_of, Step.FEATURES, features.rejected_reason(),
                direction=direction, entry_model=proposal.model, bias=bias,
                htf_array=array, proposal=proposal, gate=gate, geometry=geometry,
                features=features,
            )

        # -- step 7: model --------------------------------------------------
        if self._model is None:
            return Candidate(
                instrument, as_of, Step.MODEL, "model:not_loaded",
                direction=direction, entry_model=proposal.model, bias=bias,
                htf_array=array, proposal=proposal, gate=gate, geometry=geometry,
                features=features,
            )
        self._model.check_usable(as_of, features.version)
        p_hat = self._model.probability(features.values)
        reasons = tuple(self._model.contributions(features.values)[:3])

        # -- step 8: decision -----------------------------------------------
        rr1 = geometry.rr1 or 0.0
        expected_r = p_hat * rr1 - (1.0 - p_hat)

        def scored(
            step: Step,
            reason: str | None,
            size: PositionSize | None = None,
            expires_at: datetime | None = None,
        ) -> Candidate:
            """Build the candidate with every field populated so far.

            A closure rather than a dict splat: a `**kwargs` dict erases the field types
            and mypy can no longer check that a rejection carries the same payload as an
            emission, which is exactly what the funnel report depends on.
            """
            return Candidate(
                instrument=instrument,
                as_of=as_of,
                reached_step=step,
                rejected_reason=reason,
                direction=direction,
                entry_model=proposal.model,
                bias=bias,
                htf_array=array,
                proposal=proposal,
                gate=gate,
                geometry=geometry,
                features=features,
                p_hat=p_hat,
                expected_r=expected_r,
                reasons=reasons,
                size=size,
                expires_at=expires_at,
            )

        if rr1 < p.rr_min:
            return scored(Step.DECISION, f"decision:rr_{rr1:.2f}_below_{p.rr_min}")
        if p_hat < p.p_floor:
            return scored(Step.DECISION, f"decision:p_{p_hat:.3f}_below_floor")
        if expected_r < p.expected_r_min:
            return scored(
                Step.DECISION, f"decision:er_{expected_r:.3f}_below_{p.expected_r_min}"
            )

        # -- step 9: emit ---------------------------------------------------
        size = position_size(
            entry=geometry.entry,
            stop_loss=geometry.stop_loss,
            contract_size=meta.contract_size,
            base_ccy=meta.base_ccy,
            quote_ccy=meta.quote_ccy,
            account_ccy=p.account_ccy,
            account_size=p.account_size,
            risk_pct=p.risk_pct,
            lot_step=p.lot_step,
            min_lot=p.min_lot,
        )
        return scored(Step.EMITTED, None, size=size, expires_at=self._expiry(as_of, context))

    # -- helpers -----------------------------------------------------------
    def _propose(
        self,
        snapshot: AnalysisSnapshot,
        direction: Direction,
        as_of: datetime,
        context: EvaluationContext,
    ) -> EntryProposal | None:
        p = self._params
        if EntryModel.SWEEP_MSS_RETRACE in p.entry_models:
            got = propose_sweep_mss_retrace(
                snapshot, direction, as_of, p.mss_window_bars, p.fvg_level
            )
            if got is not None:
                return got
        if (
            EntryModel.SILVER_BULLET_FVG in p.entry_models
            and context.silver_bullet_window_start is not None
        ):
            return propose_silver_bullet_fvg(
                snapshot, direction, as_of, context.silver_bullet_window_start, p.fvg_level
            )
        return None

    def _geometry(
        self,
        snapshot: AnalysisSnapshot,
        proposal: EntryProposal,
        direction: Direction,
        context: EvaluationContext,
    ) -> TradeGeometry:
        p = self._params
        atr = snapshot.atr or 0.0
        if proposal.sweep is not None:
            stop = stop_from_sweep(direction, proposal.sweep.extremity, atr, p.sl_buffer_atr)
        else:
            stop = stop_from_leg(
                direction, proposal.leg_low, proposal.leg_high, atr, p.sl_buffer_atr
            )

        arrays: list[tuple[float, str]] = []
        for key in ("H1", "H4"):
            snap = context.htf.get(key)
            if snap is None:
                continue
            arrays += [
                (f.proximal_edge(), f"{key}_fvg")
                for f in snap.fvgs
                if f.available and f.effective_direction is direction.opposite
            ]
        targets = collect_targets(
            direction=direction,
            entry=proposal.entry,
            pools=snapshot.pools,
            arrays=arrays,
            levels=snapshot.levels,
            dealing=snapshot.dealing_range,
            min_level_strength=p.tp_min_level_strength,
        )
        return build_geometry(
            direction, proposal.entry, stop, targets, p.rr_min, atr, p.sl_min_atr
        )

    @staticmethod
    def _minutes_in(gate: GateResult) -> int | None:
        raw = gate.details.get("minutes_into_week")
        return int(raw) if isinstance(raw, int) else 0

    def _expiry(self, as_of: datetime, context: EvaluationContext) -> datetime:
        p = self._params
        return as_of + timedelta(
            minutes=p.max_pending_bars * context.entry_timeframe.minutes
            + p.expiry_grace_minutes
        )
