"""Signal engine: sizing, geometry, model, lifecycle, entries, decision rule."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import numpy as np
import pytest

from aurion.analysis.base import Direction, TrendState
from aurion.analysis.ict.fvg import FVG
from aurion.analysis.ict.pd_range import DealingRange
from aurion.analysis.regime.correlation import session_vwap
from aurion.analysis.regime.trend import trend_regime_state
from aurion.analysis.regime.volatility import volatility_state
from aurion.analysis.registry import AnalysisParams, AnalysisSnapshot, ObjectStore
from aurion.analysis.sr.levels import SRLevel
from aurion.analysis.structure.trend import Structure
from aurion.core.marketview import MarketView
from aurion.core.timeframe import UTC, TimeFrame
from aurion.data.repository import InMemoryBarRepository
from aurion.sessions.gates import GateResult
from aurion.signals.bias import htf_bias
from aurion.signals.engine import (
    EngineParams,
    EvaluationContext,
    InstrumentMeta,
    SignalEngine,
    Step,
)
from aurion.signals.entries import (
    EntryModel,
    propose_silver_bullet_fvg,
    propose_sweep_mss_retrace,
)
from aurion.signals.geometry import (
    Target,
    TargetKind,
    build_geometry,
    collect_targets,
    stop_from_leg,
    stop_from_sweep,
)
from aurion.signals.lifecycle import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    LifecycleManager,
    SignalState,
    TrackedSignal,
)
from aurion.signals.model import (
    ConstantProbabilityModel,
    LogisticModel,
    ModelError,
    average_uniqueness,
    brier_score,
    fit_isotonic,
    fit_logistic,
    reliability_curve,
)
from aurion.signals.sizing import position_size, value_per_price_unit
from tests.conftest import make_m1, zigzag

T0 = datetime(2024, 6, 12, 9, 0, tzinfo=UTC)


# -- sizing ------------------------------------------------------------------
def test_sizing_matches_the_worked_examples_in_the_docs():
    """docs/03 §6. USDJPY at 150 with a 20-pip stop and $100 risk is 0.75 lots."""
    jpy = position_size(150.00, 149.80, 100_000, "USD", "JPY", "USD", 20_000, 0.5)
    assert jpy.value_per_price_unit == pytest.approx(666.667, rel=1e-3)
    assert jpy.lots == pytest.approx(0.75, abs=0.01)

    gold = position_size(2318.40, 2313.90, 100, "XAU", "USD", "USD", 20_000, 0.5)
    assert gold.value_per_price_unit == pytest.approx(100.0)
    assert gold.lots == pytest.approx(0.22, abs=0.01)


def test_value_per_price_unit_covers_only_the_three_real_cases():
    assert value_per_price_unit(100_000, "EUR", "USD", "USD", 1.10) == 100_000
    assert value_per_price_unit(100_000, "USD", "JPY", "USD", 150.0) == pytest.approx(
        666.67, rel=1e-3
    )
    # A cross where neither leg is the account currency needs an explicit rate.
    assert value_per_price_unit(100_000, "EUR", "GBP", "USD", 0.85) is None
    assert value_per_price_unit(
        100_000, "EUR", "GBP", "USD", 0.85, quote_to_account=1.27
    ) == pytest.approx(127_000)


def test_missing_conversion_rate_returns_none_rather_than_guessing():
    got = position_size(0.85, 0.84, 100_000, "EUR", "GBP", "USD", 10_000, 0.5)
    assert got.lots is None and "conversion rate" in got.note


def test_below_minimum_size_is_zero_and_never_rounded_up():
    got = position_size(1.10, 1.00, 100_000, "EUR", "USD", "USD", 100, 0.5)
    assert got.lots == 0.0 and got.below_minimum
    assert "below minimum" in got.note


def test_zero_stop_distance_is_refused():
    assert position_size(1.1, 1.1, 100_000, "EUR", "USD", "USD", 10_000, 0.5).lots is None


def test_lots_are_always_rounded_down():
    got = position_size(2300.0, 2290.0, 100, "XAU", "USD", "USD", 10_000, 1.0)
    assert got.lots is not None
    assert got.lots * 10.0 * 100 <= got.risk_amount + 1e-9


# -- geometry ----------------------------------------------------------------
def test_stop_sits_beyond_the_swept_extremity_plus_a_buffer():
    long_stop = stop_from_sweep(Direction.UP, sweep_extremity=100.0, atr=2.0, buffer_atr=0.25)
    short_stop = stop_from_sweep(Direction.DOWN, sweep_extremity=100.0, atr=2.0, buffer_atr=0.25)
    assert long_stop == pytest.approx(99.5)
    assert short_stop == pytest.approx(100.5)
    assert stop_from_leg(Direction.UP, 99.0, 103.0, 2.0, 0.5) == pytest.approx(98.0)


def test_targets_are_ordered_by_distance_and_only_ahead_of_entry():
    levels = [
        SRLevel("X", TimeFrame.M15, 104.0, 0.8, 3, "kde", T0),
        SRLevel("X", TimeFrame.M15, 96.0, 0.9, 3, "kde", T0),
    ]
    dealing = DealingRange("X", TimeFrame.M15, Direction.UP, 95.0, 110.0, T0, T0)
    got = collect_targets(Direction.UP, 100.0, [], [(106.0, "H1_fvg")], levels, dealing, 0.4)
    prices = [t.price for t in got]
    assert all(p > 100.0 for p in prices), "a target behind the entry is not a target"
    assert prices == sorted(prices, key=lambda p: abs(p - 100.0))


def test_weak_levels_are_not_targets():
    weak = [SRLevel("X", TimeFrame.M15, 104.0, 0.1, 1, "kde", T0)]
    assert collect_targets(Direction.UP, 100.0, [], [], weak, None, 0.4) == []


def test_tp1_is_the_nearest_target_meeting_the_rr_floor():
    candidates = [
        Target(101.0, TargetKind.POOL, "near"),
        Target(104.5, TargetKind.POOL, "ok"),
        Target(108.0, TargetKind.POOL, "far"),
    ]
    geometry = build_geometry(Direction.UP, 100.0, 98.0, candidates, 2.0, 1.0, 0.3)
    assert geometry.ok
    assert geometry.tp1 is not None and geometry.tp1.price == pytest.approx(104.5)
    assert geometry.rr1 is not None and geometry.rr1 >= 2.0
    assert len(geometry.targets) == 2


def test_no_target_at_min_rr_is_rejected_with_a_countable_reason():
    """The share rejected here is a headline metric: the floor may BE the system."""
    candidates = [Target(101.0, TargetKind.POOL, "near")]
    geometry = build_geometry(Direction.UP, 100.0, 98.0, candidates, 2.0, 1.0, 0.3)
    assert not geometry.ok and geometry.rejected_reason == "no_target_at_min_rr"


def test_a_stop_inside_the_noise_band_is_refused():
    """Otherwise the backtest shows huge R multiples and live is stopped out by spread."""
    candidates = [Target(110.0, TargetKind.POOL, "far")]
    geometry = build_geometry(
        Direction.UP, 100.0, 99.99, candidates, 2.0, atr=1.0, sl_min_atr=0.3
    )
    assert geometry.rejected_reason == "sl_below_min_atr"


def test_zero_risk_is_refused():
    got = build_geometry(Direction.UP, 100.0, 100.0, [], 2.0, 1.0, 0.3)
    assert got.rejected_reason == "zero_risk"


# -- bias --------------------------------------------------------------------
class _Struct:
    def __init__(self, state):
        self.state = state


class _Snap:
    def __init__(self, state):
        self.structure = _Struct(state) if state else None


def test_bias_requires_w1_d1_agreement_when_configured():
    from aurion.analysis.base import TrendState

    agree = htf_bias(_Snap(TrendState.BULLISH), _Snap(TrendState.BULLISH))
    disagree = htf_bias(_Snap(TrendState.BULLISH), _Snap(TrendState.BEARISH))
    assert agree.ok and agree.direction is Direction.UP
    assert not disagree.ok and disagree.reason == "bias:w1_d1_disagree"


def test_bias_is_unavailable_before_structure_exists():
    got = htf_bias(_Snap(None), _Snap(None))
    assert not got.ok and got.reason == "bias:htf_structure_unavailable"


def test_ranging_structure_gives_no_direction_even_without_agreement():
    from aurion.analysis.base import TrendState

    got = htf_bias(_Snap(TrendState.RANGING), _Snap(TrendState.RANGING), require_agreement=False)
    assert not got.ok and got.reason == "bias:no_directional_structure"


# -- model -------------------------------------------------------------------
def _synthetic(n: int = 400, seed: int = 7):
    rng = np.random.default_rng(seed)
    x1, x2 = rng.normal(size=n), rng.normal(size=n)
    p = 1.0 / (1.0 + np.exp(-(0.9 * x1 - 0.6 * x2 - 0.3)))
    y = (rng.uniform(size=n) < p).astype(int)
    rows = [{"a": float(a), "b": float(b)} for a, b in zip(x1, x2, strict=True)]
    return rows, list(y)


def test_logistic_recovers_the_coefficient_signs():
    rows, y = _synthetic()
    model = fit_logistic(rows, y, ["a", "b"], l2=0.5)
    assert model.coefficients[0] > 0 and model.coefficients[1] < 0


def test_isotonic_calibration_does_not_worsen_the_brier_score():
    rows, y = _synthetic()
    model = fit_logistic(rows, y, ["a", "b"], l2=0.5)
    raw = [1.0 / (1.0 + np.exp(-model.raw_score(r))) for r in rows]
    model.calibrator = fit_isotonic(raw, y)
    calibrated = [model.probability(r) for r in rows]
    assert brier_score(calibrated, y) <= brier_score(raw, y) + 1e-9
    assert all(0.0 <= p <= 1.0 for p in calibrated)
    assert reliability_curve(calibrated, y)


def test_events_per_feature_floor_is_a_hard_error():
    """At these sample sizes a model with more features than data is a fitted-noise model."""
    rows, y = _synthetic(n=10)
    with pytest.raises(ModelError, match="events per feature"):
        fit_logistic(rows, y, ["a", "b"])
    assert fit_logistic(rows, y, ["a", "b"], enforce_events_per_feature=False) is not None


def test_model_refuses_a_mismatched_schema_or_a_future_training_window():
    rows, y = _synthetic()
    model = fit_logistic(rows, y, ["a", "b"], l2=0.5)
    model.train_end = datetime(2024, 6, 1, tzinfo=UTC)
    model.check_usable(datetime(2024, 7, 1, tzinfo=UTC), model.schema_version)
    with pytest.raises(ModelError, match="leak the training window"):
        model.check_usable(datetime(2024, 5, 1, tzinfo=UTC), model.schema_version)
    with pytest.raises(ModelError, match="never coerced"):
        model.check_usable(datetime(2024, 7, 1, tzinfo=UTC), model.schema_version + 1)


def test_contributions_are_ordered_by_absolute_log_odds():
    rows, y = _synthetic()
    model = fit_logistic(rows, y, ["a", "b"], l2=0.5)
    got = model.contributions(rows[0])
    assert [abs(v) for _, v in got] == sorted((abs(v) for _, v in got), reverse=True)


def test_model_round_trips_through_json():
    rows, y = _synthetic()
    model = fit_logistic(rows, y, ["a", "b"], l2=0.5)
    model.calibrator = fit_isotonic(
        [1.0 / (1.0 + np.exp(-model.raw_score(r))) for r in rows], y
    )
    clone = LogisticModel.from_json(model.to_json())
    assert clone.probability(rows[0]) == pytest.approx(model.probability(rows[0]))


def test_average_uniqueness_downweights_overlapping_labels():
    spans = [
        (T0, T0 + timedelta(days=5)),
        (T0 + timedelta(days=1), T0 + timedelta(days=6)),
        (T0 + timedelta(days=20), T0 + timedelta(days=25)),
    ]
    weights = average_uniqueness(spans)
    assert weights[2] > weights[0]
    assert weights[2] == pytest.approx(1.0)


def test_constant_model_returns_no_fabricated_reasons():
    """The pilot's output must not look like a model's."""
    pilot = ConstantProbabilityModel(p=0.45)
    assert pilot.probability({"anything": 1.0}) == 0.45
    assert pilot.contributions({"anything": 1.0}) == []


# -- lifecycle ---------------------------------------------------------------
def _signal(**kwargs) -> TrackedSignal:
    base = {
        "signal_id": "s1",
        "instrument": "EURUSD",
        "direction": Direction.UP,
        "entry": 1.1000,
        "stop_loss": 1.0980,
        "targets": (1.1040, 1.1060, 1.1080),
        "expires_at": T0 + timedelta(hours=4),
        "created_at": T0,
    }
    base.update(kwargs)
    return TrackedSignal(**base)


def _drive(signal: TrackedSignal, path: list[tuple[float, float]], **kw) -> LifecycleManager:
    manager = LifecycleManager()
    manager.add(signal)
    for i, (o, c) in enumerate(path):
        manager.step(
            make_m1(
                signal.instrument,
                T0 + timedelta(minutes=i),
                o,
                high_off=max(c - o, 0.0) + 1e-4,
                low_off=max(o - c, 0.0) + 1e-4,
                close_off=c - o,
            ),
            **kw,
        )
    return manager


def test_full_run_to_tp3_accumulates_the_allocation_weighted_r():
    signal = _signal()
    _drive(signal, [(1.0999, 1.1001), (1.1001, 1.1041), (1.1041, 1.1061), (1.1061, 1.1081)])
    assert signal.state is SignalState.TP3_HIT
    # 0.5*2R + 0.3*3R + 0.2*4R
    assert signal.realised_r == pytest.approx(2.7, abs=0.05)


def test_stop_out_after_entry_is_minus_one_r():
    signal = _signal()
    _drive(signal, [(1.0999, 1.1001), (1.1001, 1.0975)])
    assert signal.state is SignalState.SL_HIT
    assert signal.realised_r == pytest.approx(-1.0, abs=0.02)


def test_tp1_then_reversal_stops_at_break_even():
    signal = _signal()
    _drive(signal, [(1.0999, 1.1001), (1.1001, 1.1041), (1.1041, 1.0995)])
    assert signal.state is SignalState.BE_STOP
    assert signal.realised_r == pytest.approx(1.0, abs=0.05)


def test_without_the_be_assumption_the_full_stop_is_taken():
    signal = _signal(assume_be_after_tp1=False)
    _drive(signal, [(1.0999, 1.1001), (1.1001, 1.1041), (1.1041, 1.0975)])
    assert signal.state is SignalState.SL_AFTER_TP
    assert signal.realised_r < 1.0


def test_unfilled_signal_expires():
    signal = _signal(expires_at=T0 + timedelta(minutes=2))
    _drive(signal, [(1.1010, 1.1012), (1.1012, 1.1015), (1.1015, 1.1016)])
    assert signal.state is SignalState.EXPIRED


def test_a_bar_that_spans_entry_and_stop_fills_then_stops_out():
    """Not a cancellation.

    Both entry models are retracement limits, so for a long the stop sits below the entry
    and price cannot reach the stop without first passing through the entry. A bar doing
    both fills and then stops.
    """
    signal = _signal()
    _drive(signal, [(1.1005, 1.0970)])
    assert signal.state is SignalState.SL_HIT
    assert signal.realised_r == pytest.approx(-1.0, abs=0.02)


def test_pre_entry_cancellation_comes_from_invalidation_not_from_price():
    """The real cancellation path: a mitigated entry array, an opposing MSS, or expiry."""
    signal = _signal()
    signal.invalidate(T0 + timedelta(minutes=5), 1.1010, "entry FVG fully mitigated")
    assert signal.state is SignalState.CANCELLED
    assert signal.history[-1].note == "entry FVG fully mitigated"

    # Invalidation after entry is a no-op: the position exists and must resolve on price.
    triggered = _signal(signal_id="t")
    _drive(triggered, [(1.0999, 1.1001)])
    triggered.invalidate(T0 + timedelta(minutes=5), 1.1010, "too late")
    assert triggered.state is SignalState.TRIGGERED


def test_same_bar_tie_break_is_conservative_and_counted():
    """SL assumed first. The share decided this way is a headline metric (§4.5)."""
    conservative = _signal(signal_id="c")
    manager = _drive(conservative, [(1.0999, 1.1001), (1.1001, 1.1001)])
    # A bar that spans both the stop and TP1.
    manager.step(
        make_m1("EURUSD", T0 + timedelta(minutes=5), 1.1001,
                high_off=0.0045, low_off=0.0025, close_off=0.0)
    )
    assert conservative.state is SignalState.SL_HIT
    assert conservative.tie_break_decided
    assert manager.tie_break_share == pytest.approx(1.0)

    optimistic = _signal(signal_id="o")
    manager2 = LifecycleManager()
    manager2.add(optimistic)
    for i, (o, c) in enumerate([(1.0999, 1.1001), (1.1001, 1.1001)]):
        manager2.step(make_m1("EURUSD", T0 + timedelta(minutes=i), o,
                              high_off=max(c-o,0)+1e-4, low_off=max(o-c,0)+1e-4, close_off=c-o),
                      optimistic_tie_break=True)
    manager2.step(
        make_m1("EURUSD", T0 + timedelta(minutes=5), 1.1001,
                high_off=0.0045, low_off=0.0025, close_off=0.0),
        optimistic_tie_break=True,
    )
    assert optimistic.state is SignalState.TP1_HIT


def test_force_close_closes_an_open_signal_and_cancels_a_pending_one():
    triggered = _signal(signal_id="t")
    _drive(triggered, [(1.0999, 1.1001)])
    triggered.force_close(T0 + timedelta(hours=1), 1.1020)
    assert triggered.state is SignalState.FORCE_CLOSED and triggered.realised_r > 0

    pending = _signal(signal_id="p")
    pending.force_close(T0 + timedelta(hours=1), 1.1020)
    assert pending.state is SignalState.CANCELLED


def test_state_sets_are_disjoint_and_cover_the_machine():
    assert not (ACTIVE_STATES & TERMINAL_STATES)
    assert set(SignalState) == ACTIVE_STATES | TERMINAL_STATES


def test_manager_tracks_active_signals_per_instrument():
    manager = LifecycleManager()
    manager.add(_signal(signal_id="a", instrument="EURUSD"))
    manager.add(_signal(signal_id="b", instrument="XAUUSD"))
    assert manager.has_active("EURUSD") and manager.has_active("XAUUSD")
    assert len(manager.active_for("EURUSD")) == 1

# -- the cascade -------------------------------------------------------------
# A real M15 series carrying a genuine sweep -> CHoCH -> MSS, found by search over
# deterministic zigzags: the three-candle, swing and displacement definitions are strict
# enough that a hand-written series almost never produces one, and a fixture that produces
# no MSS makes every assertion past step 3 vacuous (which is how the breaker test in
# test_analysis_ict ended up asserting nothing).
MSS_DELTAS = [
    6.0, 1.03, -0.29, -1.34, 0.61, -1.73, -1.29, -0.49, -2.05, -0.44, 1.28, -5.0, 0.0,
    0.58, -0.16, -1.58, 0.46, -0.42, 1.06, 1.8, -0.31, 0.33, -6.0, 1.52, 0.74, 0.67,
    1.66, 0.62, 0.37, -1.19, -1.4, -1.65, -0.3, 6.0, 0.56, -1.77, -0.35, 1.24, 0.94,
    0.57, -1.1, -0.34, -0.2, 0.53, -6.0, 0.08, 0.71, 1.64, 1.74, -0.76, -2.15, 1.46,
    1.8, -1.73, -1.09, -5.0, -1.49, 1.24, 1.94, 0.08,
]
CASCADE_PARAMS = AnalysisParams(
    point=0.01,
    swing_half_width=2,
    displacement_k=0.8,
    fvg_min_atr=0.05,
    sweep_min_penetration_atr=0.02,
)


class _StubGate:
    """Admissibility is tested in `test_gates.py`; here it is an input, not the subject."""

    def __init__(self, admissible: bool = True, reason: str = "") -> None:
        self._result = GateResult(
            admissible=admissible,
            reasons=() if admissible else (reason,),
            window_label="ny_am_kz",
            windows=("ny_am_kz",),
            details={"minutes_into_week": 600},
        )

    def evaluate(self, instrument, as_of, view, spread=None):
        return self._result


def _cascade_world():
    bars = zigzag(MSS_DELTAS, timeframe=TimeFrame.M15)
    repo = InMemoryBarRepository()
    for bar in bars:
        repo.add_bar(bar)
    as_of = bars[-1].close_time
    view = MarketView(repo, as_of)
    store = ObjectStore(CASCADE_PARAMS)
    snapshot = store.snapshot(view, "TEST", TimeFrame.M15, window=500, with_levels=True)
    assert snapshot.mss, "fixture no longer contains an MSS; every step past 3 would be vacuous"
    return view, store, snapshot, as_of


def _htf(snapshot, as_of, *, state: TrendState, with_target: bool = True):
    """An HTF snapshot with a chosen structure, and optionally a far opposing array.

    HTF snapshots arrive through the context, so this is the engine's intended seam: the
    alternative is searching for a multi-timeframe series that happens to trend, which would
    make the test about the search.
    """
    structure = Structure(
        state, as_of, False, False, True, True, snapshot.bars[-1].ohlc.h, snapshot.bars[-1].ohlc.l
    )
    fvgs = list(snapshot.fvgs)
    if with_target:
        # A bullish H1 gap far below: the only thing a short in this series can target at 2R.
        fvgs.append(
            FVG(
                "TEST",
                TimeFrame.H1,
                Direction.UP,
                73.0,
                74.0,
                2.0,
                as_of - timedelta(hours=8),
                as_of - timedelta(hours=7),
            )
        )
    return AnalysisSnapshot(
        "TEST",
        TimeFrame.H4,
        as_of,
        snapshot.bars,
        snapshot.atr,
        swings=snapshot.swings,
        structure=structure,
        breaks=snapshot.breaks,
        fvgs=fvgs,
        order_blocks=snapshot.order_blocks,
        pools=snapshot.pools,
        sweeps=snapshot.sweeps,
        mss=snapshot.mss,
        dealing_range=snapshot.dealing_range,
    )


def _engine(store, gate=None, **overrides):
    defaults = {
        "require_w1_d1_agreement": False,
        "rr_min": 2.0,
        # Permissive on purpose: the decision rule has its own tests, and a cascade test
        # that fails at step 8 tells you nothing about steps 1-7.
        "expected_r_min": -9.0,
        "p_floor": 0.0,
        "mss_window_bars": 20,
    }
    params = EngineParams(**{**defaults, **overrides})
    return SignalEngine(
        params,
        store,
        gate or _StubGate(),
        {"TEST": InstrumentMeta(0.01, 100.0, "XAU", "USD")},
        ConstantProbabilityModel(p=0.55),
    )


def _context(snapshot, as_of, **overrides):
    htf = overrides.pop("htf", None)
    bars = list(snapshot.bars)
    return EvaluationContext(
        entry_timeframe=TimeFrame.M15,
        htf=htf if htf is not None else dict.fromkeys(("W1", "D1", "H4", "H1")),
        volatility=volatility_state(bars, percentile_window=40),
        regime_trend=trend_regime_state(bars, vr_window=40),
        vwap=session_vwap(bars, bars[0].open_time),
        weekday=as_of.weekday(),
        spread=0.2,
        reference_levels={"pdh": bars[-1].ohlc.c + 5},
        **overrides,
    )


def _bearish_context(snapshot, as_of, **kwargs):
    htf_snap = _htf(snapshot, as_of, state=TrendState.BEARISH, **kwargs)
    return _context(snapshot, as_of, htf=dict.fromkeys(("W1", "D1", "H4", "H1"), htf_snap))


def test_the_cascade_emits_a_complete_signal():
    view, store, snapshot, as_of = _cascade_world()
    candidate = _engine(store).evaluate(
        view, "TEST", as_of, _bearish_context(snapshot, as_of), 500
    )

    assert candidate.emitted, candidate.rejected_reason
    assert candidate.rejected_reason is None
    assert candidate.reached_step is Step.EMITTED
    geometry = candidate.geometry
    assert geometry is not None
    assert geometry.direction is Direction.DOWN
    assert geometry.stop_loss > geometry.entry, "a short's stop sits above its entry"
    assert geometry.targets and all(t.price < geometry.entry for t in geometry.targets)
    assert geometry.rr1 is not None and geometry.rr1 >= 2.0
    assert candidate.p_hat == pytest.approx(0.55)
    assert candidate.expected_r == pytest.approx(0.55 * geometry.rr1 - 0.45)
    assert candidate.signal_id


def test_the_pilot_model_emits_no_reasons_even_on_an_emitted_signal():
    """A constant probability has no attribution. Inventing three would be the lie."""
    view, store, snapshot, as_of = _cascade_world()
    candidate = _engine(store).evaluate(
        view, "TEST", as_of, _bearish_context(snapshot, as_of), 500
    )
    assert candidate.emitted
    assert candidate.reasons == ()


def test_a_ranging_higher_timeframe_stops_the_cascade_at_step_one():
    view, store, snapshot, as_of = _cascade_world()
    htf_snap = _htf(snapshot, as_of, state=TrendState.RANGING)
    context = _context(snapshot, as_of, htf=dict.fromkeys(("W1", "D1", "H4", "H1"), htf_snap))
    candidate = _engine(store).evaluate(view, "TEST", as_of, context, 500)
    assert candidate.reached_step is Step.HTF_BIAS
    assert candidate.rejected_reason == "bias:no_directional_structure"
    assert candidate.geometry is None and candidate.p_hat is None


def test_no_higher_timeframe_array_in_range_stops_the_cascade_at_step_two():
    view, store, snapshot, as_of = _cascade_world()
    candidate = _engine(store, pd_max_dist_atr=0.0).evaluate(
        view, "TEST", as_of, _bearish_context(snapshot, as_of), 500
    )
    assert candidate.reached_step is Step.HTF_ARRAY
    assert candidate.rejected_reason == "pd:no_htf_array_in_range"
    assert candidate.direction is Direction.DOWN, "the bias it got is still recorded"


def test_no_entry_model_firing_stops_the_cascade_at_step_three():
    view, store, snapshot, as_of = _cascade_world()
    candidate = _engine(store, mss_window_bars=0).evaluate(
        view, "TEST", as_of, _bearish_context(snapshot, as_of), 500
    )
    assert candidate.reached_step is Step.ENTRY_MODEL
    assert candidate.rejected_reason == "entry:no_model_fired"
    assert candidate.htf_array is not None, "the array it found is still recorded"


def test_an_inadmissible_gate_stops_the_cascade_at_step_four():
    view, store, snapshot, as_of = _cascade_world()
    gate = _StubGate(admissible=False, reason="session:no_signal_window")
    candidate = _engine(store, gate=gate).evaluate(
        view, "TEST", as_of, _bearish_context(snapshot, as_of), 500
    )
    assert candidate.reached_step is Step.GATES
    assert candidate.rejected_reason == "session:no_signal_window"
    assert candidate.proposal is not None, "the proposal it built is still recorded"


def test_no_target_at_the_rr_floor_stops_the_cascade_at_step_five():
    """Without the far opposing array there is no level 2R away, so the geometry refuses."""
    view, store, snapshot, as_of = _cascade_world()
    candidate = _engine(store).evaluate(
        view, "TEST", as_of, _bearish_context(snapshot, as_of, with_target=False), 500
    )
    assert candidate.reached_step is Step.GEOMETRY
    assert candidate.rejected_reason == "no_target_at_min_rr"


def test_a_candidate_below_the_expectancy_floor_is_rejected_carrying_its_numbers():
    """The gate is on calibrated expectancy, not on a score, so the numbers must survive
    the rejection -- otherwise the funnel cannot say how far short it fell."""
    view, store, snapshot, as_of = _cascade_world()
    candidate = _engine(store, expected_r_min=5.0).evaluate(
        view, "TEST", as_of, _bearish_context(snapshot, as_of), 500
    )
    assert not candidate.emitted
    assert candidate.rejected_reason is not None
    assert candidate.p_hat == pytest.approx(0.55)
    assert candidate.expected_r is not None and candidate.expected_r < 5.0


def test_an_empty_view_is_rejected_rather_than_evaluated():
    _, store, snapshot, as_of = _cascade_world()
    empty = MarketView(InMemoryBarRepository(), as_of)
    candidate = _engine(store).evaluate(
        empty, "TEST", as_of, _bearish_context(snapshot, as_of), 500
    )
    assert candidate.rejected_reason == "data:no_bars"


# -- entry models ------------------------------------------------------------
def _sb_fvg(as_of, offset_minutes: int, direction=Direction.DOWN, lo=90.0, hi=92.0):
    confirmed = as_of + timedelta(minutes=offset_minutes)
    return FVG(
        "TEST", TimeFrame.M15, direction, lo, hi, 2.0, confirmed - timedelta(minutes=15), confirmed
    )


def test_the_silver_bullet_model_takes_the_most_recent_gap_inside_the_window():
    _, _, snapshot, as_of = _cascade_world()
    window_start = as_of - timedelta(minutes=45)
    old = _sb_fvg(as_of, -30, lo=80.0, hi=82.0)
    recent = _sb_fvg(as_of, -15, lo=90.0, hi=92.0)
    snap = replace(snapshot, fvgs=[old, recent])

    proposal = propose_silver_bullet_fvg(snap, Direction.DOWN, as_of, window_start)
    assert proposal is not None
    assert proposal.model is EntryModel.SILVER_BULLET_FVG
    assert proposal.entry == pytest.approx(91.0), "the CE of the most recent gap"
    assert proposal.sweep is None and proposal.mss is None
    assert not proposal.has_sweep
    assert "Silver Bullet window closes" in proposal.invalidation


def test_the_silver_bullet_model_ignores_a_gap_formed_before_the_window_opened():
    """The window IS the model. A gap from an hour earlier is the other model's setup."""
    _, _, snapshot, as_of = _cascade_world()
    snap = replace(snapshot, fvgs=[_sb_fvg(as_of, -90)])
    window_start = as_of - timedelta(minutes=45)
    assert propose_silver_bullet_fvg(snap, Direction.DOWN, as_of, window_start) is None


def test_the_silver_bullet_model_ignores_a_gap_confirmed_after_as_of():
    _, _, snapshot, as_of = _cascade_world()
    snap = replace(snapshot, fvgs=[_sb_fvg(as_of, +15)])
    window_start = as_of - timedelta(hours=1)
    assert propose_silver_bullet_fvg(snap, Direction.DOWN, as_of, window_start) is None


def test_the_silver_bullet_model_ignores_the_wrong_polarity():
    _, _, snapshot, as_of = _cascade_world()
    snap = replace(snapshot, fvgs=[_sb_fvg(as_of, -15, direction=Direction.UP)])
    window_start = as_of - timedelta(minutes=45)
    assert propose_silver_bullet_fvg(snap, Direction.DOWN, as_of, window_start) is None
    assert propose_silver_bullet_fvg(snap, Direction.UP, as_of, window_start) is not None


def test_the_entry_level_can_be_the_proximal_edge_instead_of_the_midpoint():
    _, _, snapshot, as_of = _cascade_world()
    snap = replace(snapshot, fvgs=[_sb_fvg(as_of, -15, lo=90.0, hi=92.0)])
    window_start = as_of - timedelta(minutes=45)
    ce = propose_silver_bullet_fvg(snap, Direction.DOWN, as_of, window_start, fvg_level="ce")
    edge = propose_silver_bullet_fvg(snap, Direction.DOWN, as_of, window_start, fvg_level="edge")
    assert ce is not None and edge is not None
    assert (ce.entry, ce.entry_array_label) == (pytest.approx(91.0), "fvg_ce")
    # A bearish gap is entered from below, so its proximal edge is the low.
    assert (edge.entry, edge.entry_array_label) == (pytest.approx(90.0), "fvg_edge")


def test_the_primary_model_refuses_an_mss_confirmed_after_as_of():
    """Causality at the model boundary, not only at the MarketView."""
    _, _, snapshot, _ = _cascade_world()
    mss = snapshot.mss[-1]
    assert propose_sweep_mss_retrace(snapshot, mss.direction, mss.confirmed_at, 20) is not None
    before = mss.confirmed_at - timedelta(minutes=1)
    assert propose_sweep_mss_retrace(snapshot, mss.direction, before, 20) is None


def test_the_primary_model_refuses_when_no_array_survives_the_filters():
    _, _, snapshot, as_of = _cascade_world()
    mss = snapshot.mss[-1]
    stripped = replace(snapshot, fvgs=[], order_blocks=[])
    assert propose_sweep_mss_retrace(stripped, mss.direction, as_of, 20) is None
