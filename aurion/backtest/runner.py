"""Event-driven backtest runner. `docs/04-validation-protocol.md` §1.

**Single code path.** This replays stored M1 bars through the *same* `analysis/`,
`features/` and `signals/` code the live runtime uses. The only differences are the three
injected objects named in `docs/01-architecture.md` §4: the clock, the bar source and the
signal sink. There is no branch on mode anywhere below `signals/`.

Resolution is always on M1, never on the signal's own timeframe: resolving a 15-minute
trade on 15-minute bars cannot tell whether the stop or the target came first, and
systematically favours the target.

Determinism is a requirement, not a nicety: two runs over the same data at the same config
hash must produce byte-identical ledgers, because a non-deterministic backtest cannot be
debugged and its results cannot be compared. Every RNG is seeded from the config hash.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from aurion import BACKTEST_DISCLAIMER
from aurion.analysis.regime.correlation import session_vwap
from aurion.analysis.regime.trend import trend_regime_state
from aurion.analysis.regime.volatility import volatility_state
from aurion.analysis.registry import AnalysisParams, ObjectStore
from aurion.backtest.costs import CostParams, commission_cost, holds_through_rollover
from aurion.backtest.metrics import FunnelReport
from aurion.core.clock import SimClock
from aurion.core.marketview import MarketView
from aurion.core.timeframe import UTC, Anchor, Bucketer, TimeFrame
from aurion.data.repository import InMemoryBarRepository
from aurion.data.resample import ResamplerSet
from aurion.data.types import M1Candle
from aurion.sessions.gates import AdmissibilityGate
from aurion.sessions.windows import WindowSet
from aurion.signals.engine import Candidate, EvaluationContext, SignalEngine
from aurion.signals.lifecycle import LifecycleManager, SignalState, TrackedSignal


@dataclass(frozen=True, slots=True)
class Trade:
    signal_id: str
    instrument: str
    direction: int
    entry: float
    stop_loss: float
    targets: tuple[float, ...]
    rr1: float
    p_hat: float
    expected_r: float
    opened_at: datetime
    closed_at: datetime
    final_state: SignalState
    realised_r: float
    commission: float
    tie_break_decided: bool
    held_through_rollover: bool
    session_label: str
    entry_model: str
    fold_id: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "signal_id": self.signal_id,
            "instrument": self.instrument,
            "direction": self.direction,
            "entry": self.entry,
            "stop_loss": self.stop_loss,
            "rr1": self.rr1,
            "p_hat": self.p_hat,
            "expected_r": self.expected_r,
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat(),
            "final_state": str(self.final_state),
            "realised_r": self.realised_r,
            "tie_break_decided": self.tie_break_decided,
            "held_through_rollover": self.held_through_rollover,
            "session_label": self.session_label,
            "entry_model": self.entry_model,
            "fold_id": self.fold_id,
        }


@dataclass(slots=True)
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    funnel: FunnelReport = field(default_factory=FunnelReport)
    bars_processed: int = 0
    evaluations: int = 0
    config_hash: str = ""
    disclaimer: str = BACKTEST_DISCLAIMER

    @property
    def returns(self) -> list[float]:
        return [t.realised_r for t in self.trades]

    @property
    def tie_break_share(self) -> float:
        """Above 5% the results are substantially the tie-break assumption (§4.5)."""
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.tie_break_decided) / len(self.trades)

    @property
    def rollover_share(self) -> float:
        """Above 10% overnight financing must be modelled before trusting the results."""
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.held_through_rollover) / len(self.trades)

    def daily_returns(self) -> list[float]:
        """R per calendar day, for the deflated Sharpe."""
        by_day: dict[str, float] = {}
        for t in self.trades:
            key = t.closed_at.date().isoformat()
            by_day[key] = by_day.get(key, 0.0) + t.realised_r
        return [by_day[k] for k in sorted(by_day)]

    def ledger_digest(self) -> str:
        """Hash of the trade ledger. Two runs at the same config must match exactly."""
        payload = json.dumps([t.as_dict() for t in self.trades], sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    entry_timeframe: TimeFrame = TimeFrame.M15
    #: Needed by the SILVER_BULLET_FVG model: without it that model can never fire, which
    #: silently reduced a two-model design to one in the first full-year run.
    windows: WindowSet | None = None
    #: From `AurionConfig.anchors()`. Drives both the resamplers and the FX-day boundary the
    #: rollover check uses, so a run on an alternative D1 anchor (the Q2 robustness run)
    #: moves both together instead of resampling on one definition and reporting on another.
    anchors: dict[TimeFrame, Anchor] | None = None
    htf_timeframes: tuple[TimeFrame, ...] = (
        TimeFrame.H1,
        TimeFrame.H4,
        TimeFrame.D1,
        TimeFrame.W1,
    )
    analysis_window: int = 400
    optimistic_tie_break: bool = False
    assume_be_after_tp1: bool = True
    tp_allocation: tuple[float, ...] = (0.5, 0.3, 0.2)
    fold_id: str = ""


class BacktestRunner:
    """Replays M1 candles through the live signal code."""

    __slots__ = ("_cfg", "_costs", "_engine", "_fx_day", "_gate", "_meta", "_params", "_store")

    def __init__(
        self,
        runner_cfg: RunnerConfig,
        engine: SignalEngine,
        gate: AdmissibilityGate,
        store: ObjectStore,
        cost_params: CostParams,
        analysis_params: AnalysisParams,
        instrument_points: dict[str, float],
    ) -> None:
        self._cfg = runner_cfg
        self._fx_day = Bucketer(
            TimeFrame.D1, (runner_cfg.anchors or {}).get(TimeFrame.D1)
        )
        self._engine = engine
        self._gate = gate
        self._store = store
        self._costs = cost_params
        self._params = analysis_params
        self._meta = instrument_points

    def run(
        self,
        candles: dict[str, list[M1Candle]],
        config_hash: str = "",
        max_evaluations: int | None = None,
    ) -> BacktestResult:
        result = BacktestResult(config_hash=config_hash)
        if not candles:
            return result

        instruments = sorted(candles)
        timeframes = (self._cfg.entry_timeframe, *self._cfg.htf_timeframes)
        repo = InMemoryBarRepository()
        resamplers = {
            i: ResamplerSet(i, timeframes, self._cfg.anchors) for i in instruments
        }
        lifecycle = LifecycleManager()

        merged: list[M1Candle] = sorted(
            (c for series in candles.values() for c in series),
            key=lambda c: (c.open_time, c.instrument),
        )
        if not merged:
            return result
        clock = SimClock(merged[0].open_time)

        for candle in merged:
            clock.advance_to(candle.close_time)
            repo.add_m1(candle)
            result.bars_processed += 1

            # Lifecycle first: a signal resolves on the bar that moved, before any new
            # candidate is evaluated against the same bar.
            for signal, transition in lifecycle.step(candle, self._cfg.optimistic_tie_break):
                if transition.state in (
                    SignalState.SL_HIT, SignalState.BE_STOP, SignalState.SL_AFTER_TP,
                    SignalState.TP3_HIT, SignalState.EXPIRED, SignalState.CANCELLED,
                    SignalState.FORCE_CLOSED,
                ):
                    result.trades.append(self._close_trade(signal, transition.at))

            closed = resamplers[candle.instrument].push(candle)
            for bar in closed:
                repo.add_bar(bar)

            entry_closed = [b for b in closed if b.timeframe is self._cfg.entry_timeframe]
            if not entry_closed:
                continue
            if max_evaluations is not None and result.evaluations >= max_evaluations:
                continue

            as_of = entry_closed[-1].close_time
            view = MarketView(repo, as_of)
            # The KDE S&R fit is recomputed on D1 close, not per bar (docs/01 §8).
            refresh_levels = any(b.timeframe is TimeFrame.D1 for b in closed)
            candidate = self._evaluate(
                view, candle.instrument, as_of, refresh_levels=refresh_levels
            )
            result.evaluations += 1
            result.candidates.append(candidate)
            result.funnel.record(candidate.reached_step.name.lower(), candidate.rejected_reason)

            if candidate.emitted and candidate.geometry is not None:
                lifecycle.add(
                    TrackedSignal(
                        signal_id=candidate.signal_id,
                        instrument=candidate.instrument,
                        direction=candidate.geometry.direction,
                        entry=candidate.geometry.entry,
                        stop_loss=candidate.geometry.stop_loss,
                        targets=tuple(t.price for t in candidate.geometry.targets),
                        expires_at=candidate.expires_at or as_of + timedelta(hours=4),
                        created_at=as_of,
                        allocation=self._cfg.tp_allocation,
                        assume_be_after_tp1=self._cfg.assume_be_after_tp1,
                    )
                )
                self._remember(candidate)
        return result

    # -- internals ---------------------------------------------------------
    def _evaluate(
        self,
        view: MarketView,
        instrument: str,
        as_of: datetime,
        refresh_levels: bool = False,
    ) -> Candidate:
        htf = {
            str(tf): self._store.snapshot(view, instrument, tf, self._cfg.analysis_window)
            for tf in self._cfg.htf_timeframes
        }
        entry_bars = list(
            view.usable_bars(instrument, self._cfg.entry_timeframe, self._cfg.analysis_window)
        )
        m1 = view.m1(instrument, 1)
        context = EvaluationContext(
            entry_timeframe=self._cfg.entry_timeframe,
            htf=htf,
            volatility=volatility_state(entry_bars) if entry_bars else None,
            regime_trend=trend_regime_state(entry_bars) if entry_bars else None,
            vwap=(
                session_vwap(entry_bars, entry_bars[0].open_time) if entry_bars else None
            ),
            active_usd_exposure=[],
            weekday=as_of.astimezone(UTC).weekday(),
            spread=m1[-1].spread_close if m1 else None,
            silver_bullet_window_start=self._silver_bullet_start(as_of),
        )
        return self._engine.evaluate(
            view, instrument, as_of, context, self._cfg.analysis_window, refresh_levels
        )

    def _silver_bullet_start(self, as_of: datetime) -> datetime | None:
        """Start of the Silver Bullet window containing ``as_of``, if any.

        The SILVER_BULLET_FVG model is time-conditioned and needs this. Leaving it unset
        disables the model entirely, which is how a two-model design quietly became a
        one-model design in the first full-year run.
        """
        if self._cfg.windows is None:
            return None
        for window in self._cfg.windows.active(as_of):
            if "silver_bullet" in window.name:
                occurrence = window.current_occurrence(as_of)
                if occurrence is not None:
                    return occurrence[0]
        return None

    def _remember(self, candidate: Candidate) -> None:
        """Hook for persisting the feature vector at decision time.

        The in-memory runner keeps the candidate object itself; the live path writes it to
        the `candidates` table. Either way the vector is stored as the model saw it and is
        never recomputed, which is what makes the training set and the audit trail the same
        object.
        """

    def _close_trade(self, signal: TrackedSignal, closed_at: datetime) -> Trade:
        opened = signal.triggered_at or signal.created_at
        return Trade(
            signal_id=signal.signal_id,
            instrument=signal.instrument,
            direction=int(signal.direction),
            entry=signal.entry,
            stop_loss=signal.stop_loss,
            targets=signal.targets,
            rr1=(
                abs(signal.targets[0] - signal.entry) / signal.risk
                if signal.targets and signal.risk > 0
                else 0.0
            ),
            p_hat=0.0,
            expected_r=0.0,
            opened_at=opened,
            closed_at=closed_at,
            final_state=signal.state,
            realised_r=signal.realised_r,
            commission=commission_cost(self._costs, 0.0),
            tie_break_decided=signal.tie_break_decided,
            held_through_rollover=holds_through_rollover(opened, closed_at, self._fx_day),
            session_label="",
            entry_model="",
            fold_id=self._cfg.fold_id,
        )


def config_hash(payload: dict[str, object]) -> str:
    """Stable hash recorded on every signal and every run (`docs/01` §9)."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def seed_from_hash(hash_hex: str) -> int:
    """Every RNG is seeded from the config hash, so replay is bit-identical."""
    return int(hash_hex[:8], 16) if hash_hex else 0
