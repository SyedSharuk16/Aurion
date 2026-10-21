"""Signal engine: sizing, geometry, model, lifecycle, entries, decision rule."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from aurion.analysis.base import Direction
from aurion.analysis.ict.pd_range import DealingRange
from aurion.analysis.sr.levels import SRLevel
from aurion.core.timeframe import UTC, TimeFrame
from aurion.signals.bias import htf_bias
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
from tests.conftest import make_m1

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
