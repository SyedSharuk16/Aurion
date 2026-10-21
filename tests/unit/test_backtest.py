"""Backtester: costs, metrics, walk-forward, ablation, report, the replay runner."""

from __future__ import annotations

import math
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path

import numpy as np
import pytest

from aurion.analysis.base import Direction
from aurion.analysis.registry import AnalysisParams, ObjectStore
from aurion.backtest.ablation import (
    TIER1_ARM_ORDER,
    Branch,
    SealBreach,
    Tier2Seal,
    affordable_trials,
    determine_branch,
    estimate_sigma_d,
    evaluate_arm,
    holm_adjust,
    holm_critical_z,
    mde_paired_crn,
    mde_per_affected_trade,
    required_annual_sharpe,
    trades_required,
)
from aurion.backtest.costs import (
    CostParams,
    SpreadModel,
    commission_cost,
    effective_spread,
    holds_through_rollover,
    limit_filled,
    news_widening,
    stop_fill_price,
)
from aurion.backtest.metrics import (
    FunnelReport,
    bootstrap_ci,
    breakout,
    cluster_bootstrap,
    deflated_sharpe,
    drawdown_distribution,
    effective_trials,
    expected_max_sharpe_factor,
    iid_bootstrap,
    max_drawdown_r,
    probabilistic_sharpe,
    stationary_block_bootstrap,
    trade_stats,
    widest,
)
from aurion.backtest.report import MIN_OOS_TRADES, ValidationReport, monte_carlo_sections
from aurion.backtest.runner import BacktestResult, BacktestRunner, RunnerConfig
from aurion.backtest.walkforward import (
    LabelledEvent,
    build_plan,
    calibration_split,
    max_label_horizon,
    purge_and_embargo,
)
from aurion.core.timeframe import DEFAULT_ANCHORS, UTC, Anchor, TimeFrame, bucketer
from aurion.data.resample import ResamplerSet
from aurion.data.types import OHLC, M1Candle
from aurion.features.schema import Tag
from aurion.sessions.calendar import StaticCalendarProvider
from aurion.sessions.gates import AdmissibilityGate, build_window_set
from aurion.sessions.spread import SpreadTracker
from aurion.signals.engine import (
    Candidate,
    EngineParams,
    InstrumentMeta,
    SignalEngine,
    Step,
)
from aurion.signals.model import ConstantProbabilityModel

T0 = datetime(2024, 6, 12, 9, 0, tzinfo=UTC)


# -- costs -------------------------------------------------------------------
def test_news_widening_decays_with_distance_from_the_event():
    params = CostParams(news_spread_mult=4.0, news_spread_tau_min=5.0)
    at_event = news_widening(params, T0, T0)
    five_min = news_widening(params, T0 + timedelta(minutes=5), T0)
    far = news_widening(params, T0 + timedelta(hours=2), T0)
    assert at_event == pytest.approx(4.0)
    assert 1.0 < five_min < 4.0
    assert far == pytest.approx(1.0, abs=1e-6)
    assert news_widening(params, T0, None) == 1.0


def test_historical_spread_is_used_when_available_and_falls_back_otherwise():
    historical = CostParams(spread_model=SpreadModel.HISTORICAL, fixed_spread=0.5)
    assert effective_spread(historical, 0.18, T0) == pytest.approx(0.18)
    assert effective_spread(historical, None, T0) == pytest.approx(0.5)
    fixed = CostParams(spread_model=SpreadModel.FIXED, fixed_spread=0.3)
    assert effective_spread(fixed, 0.18, T0) == pytest.approx(0.3)


def test_a_limit_fills_only_on_a_trade_through_not_a_touch():
    """Touch-equals-fill flatters exactly the setups this system looks for."""
    params = CostParams(limit_fill_margin_points=1.0, point=0.01)
    assert not limit_filled(params, Direction.UP, 100.00, extreme=100.00)
    assert limit_filled(params, Direction.UP, 100.00, extreme=99.98)
    assert not limit_filled(params, Direction.DOWN, 100.00, extreme=100.00)
    assert limit_filled(params, Direction.DOWN, 100.00, extreme=100.02)


def test_stop_fills_slip_adversely_in_both_directions():
    params = CostParams(slippage_atr_m1=0.1, spread_model=SpreadModel.HISTORICAL)
    long_fill = stop_fill_price(params, Direction.UP, 100.0, 2.0, 0.2, T0)
    short_fill = stop_fill_price(params, Direction.DOWN, 100.0, 2.0, 0.2, T0)
    assert long_fill < 100.0 and short_fill > 100.0
    assert long_fill == pytest.approx(100.0 - (0.2 + 0.2))


def test_optimistic_variant_removes_slippage_and_the_fill_margin():
    base = CostParams(slippage_atr_m1=0.1, limit_fill_margin_points=1.0, news_spread_mult=4.0)
    optimistic = base.with_optimistic_fills()
    assert optimistic.slippage_atr_m1 == 0.0
    assert optimistic.limit_fill_margin_points == 0.0
    assert optimistic.news_spread_mult == 1.0


def test_commission_charges_both_sides():
    assert commission_cost(CostParams(commission_per_lot_per_side=3.5), 2.0) == pytest.approx(14.0)
    assert commission_cost(CostParams(), 2.0) == 0.0


def test_rollover_crossing_is_detected():
    """The share of trades crossing it decides whether financing must be modelled."""
    # June: EDT, so 17:00 NY is 21:00 UTC.
    assert holds_through_rollover(
        datetime(2024, 6, 12, 20, 0, tzinfo=UTC), datetime(2024, 6, 12, 22, 0, tzinfo=UTC)
    )
    assert not holds_through_rollover(
        datetime(2024, 6, 12, 9, 0, tzinfo=UTC), datetime(2024, 6, 12, 12, 0, tzinfo=UTC)
    )


def test_rollover_follows_new_york_rather_than_a_fixed_utc_hour():
    """January is EST: 17:00 NY is 22:00 UTC, not 21:00.

    A fixed 21:00 UTC boundary got both of these wrong in opposite directions for the ~4.5
    months a year the US is on standard time, and `rollover_share` is an acceptance
    criterion rather than a diagnostic.
    """
    assert not holds_through_rollover(
        datetime(2024, 1, 10, 20, 0, tzinfo=UTC), datetime(2024, 1, 10, 21, 30, tzinfo=UTC)
    )
    assert holds_through_rollover(
        datetime(2024, 1, 10, 21, 30, tzinfo=UTC), datetime(2024, 1, 10, 22, 30, tzinfo=UTC)
    )


def test_a_zero_length_trade_crosses_nothing():
    t = datetime(2024, 1, 10, 22, 0, tzinfo=UTC)
    assert not holds_through_rollover(t, t)


def test_modelled_spread_refuses_rather_than_impersonating_the_historical_one():
    """A setting that silently does something other than its name is worse than a crash."""
    params = CostParams(spread_model=SpreadModel.MODELLED, fixed_spread=0.2)
    with pytest.raises(NotImplementedError, match="fitted spread distribution"):
        effective_spread(params, 0.3, datetime(2024, 6, 12, 12, 0, tzinfo=UTC))


# -- metrics -----------------------------------------------------------------
def test_trade_stats_on_a_known_sequence():
    returns = [2.0, -1.0, 2.0, -1.0, -1.0]
    st = trade_stats(returns)
    assert st.n == 5
    assert st.expectancy == pytest.approx(0.2)
    assert st.win_rate == pytest.approx(0.4)
    assert st.profit_factor == pytest.approx(4.0 / 3.0)
    assert st.total_r == pytest.approx(1.0)


def test_max_drawdown_is_peak_to_trough_of_the_r_curve():
    assert max_drawdown_r([1.0, -2.0, -1.0, 3.0]) == pytest.approx(3.0)
    assert max_drawdown_r([]) == 0.0
    assert max_drawdown_r(np.array([1.0, -2.0, -1.0])) == pytest.approx(3.0)


def test_sortino_is_unbounded_not_zero_when_there_is_no_downside_dispersion():
    """0.0 would read as 'no edge', the opposite of what it means."""
    assert trade_stats([2.0, -1.0, -1.0, 2.0]).sortino == math.inf
    assert trade_stats([-1.0, -1.0]).sortino == 0.0


def test_block_bootstrap_preserves_clustering_that_iid_destroys():
    rng = np.random.default_rng(3)
    # A serially correlated series: runs of wins then runs of losses.
    series: list[float] = []
    for _ in range(40):
        series += [2.0] * 5 if rng.random() < 0.5 else [-1.0] * 5
    block = stationary_block_bootstrap(series, paths=600, expected_block=10, seed=1)
    iid = iid_bootstrap(series, paths=600, seed=1)
    assert drawdown_distribution(block)["p95"] > drawdown_distribution(iid)["p95"]


def test_cluster_bootstrap_resamples_whole_folds():
    folds = [[1.0, -1.0], [2.0, -1.0, 2.0], [-1.0]]
    paths = cluster_bootstrap(folds, paths=50, seed=1)
    assert paths.shape[0] == 50 and paths.shape[1] >= 1


def test_widest_interval_wins_and_zero_exclusion_is_reported():
    a = bootstrap_ci(np.array([[0.1, 0.2], [0.15, 0.25]]), 0.2, method="a")
    b = bootstrap_ci(np.array([[-0.5, 0.9], [0.0, 1.0]]), 0.2, method="b")
    assert widest(a, b).method == "b"
    assert a.excludes_zero


def test_expected_max_sharpe_factor_grows_with_the_trial_count():
    values = [expected_max_sharpe_factor(n) for n in (2, 4, 8, 64, 256)]
    assert values == sorted(values)
    assert expected_max_sharpe_factor(1) == float("-inf")


def test_psr_cannot_reach_095_on_one_year_at_sharpe_one():
    """docs/07 Correction 2: the original holdout DSR criterion was unmeetable."""
    daily_sharpe = 1.0 / math.sqrt(252)
    assert probabilistic_sharpe(daily_sharpe, 0.0, 252) < 0.90


def test_deflated_sharpe_falls_as_trials_rise():
    rng = np.random.default_rng(5)
    daily = list(rng.normal(0.05, 0.6, size=1260))
    values = [deflated_sharpe(daily, n).dsr for n in (1, 4, 16, 64)]
    assert values == sorted(values, reverse=True)


def test_deflated_sharpe_refuses_a_degenerate_constant_series():
    """A constant series has std ~1e-17, not 0.

    Without a relative guard it reported a Sharpe of 3.5e15 and DSR = 1.0 -- a perfect
    score on data for which the statistic is undefined.
    """
    assert math.isnan(deflated_sharpe([0.1] * 50, 4).dsr)
    assert math.isnan(deflated_sharpe([0.0] * 50, 4).dsr)
    assert math.isnan(deflated_sharpe([0.1, 0.2], 4).dsr)


def test_effective_trials_collapses_identical_series():
    rng = np.random.default_rng(1)
    a = list(rng.normal(size=200))
    assert effective_trials([a, a, a]) == 1
    b, c = list(rng.normal(size=200)), list(rng.normal(size=200))
    assert effective_trials([a, b, c]) >= 2


def test_breakout_hides_thin_cells_rather_than_reporting_noise():
    cells = breakout([("london", 1.0)] * 11 + [("ny", 1.0)] * 40)
    thin = next(c for c in cells if c.key == "london")
    thick = next(c for c in cells if c.key == "ny")
    assert thin.stats is None and "minimum" in thin.note
    assert thick.stats is not None


def test_funnel_counts_rejections_by_step_and_reason():
    funnel = FunnelReport()
    for step, reason in [("htf_bias", "bias:disagree")] * 40 + [("emitted", None)] * 3:
        funnel.record(step, reason)
    assert funnel.evaluated == 43 and funnel.emitted == 3
    assert funnel.heaviest_filter() == ("htf_bias", 40)


# -- walk-forward ------------------------------------------------------------
def test_adopted_design_has_no_training_overlap_and_more_oos():
    """docs/07 §9 Correction 4: 24/3/3 gave N_eff 51; 12/12/12 gives 414."""
    start, end = datetime(2019, 1, 1, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC)
    original = build_plan(start, end, 24, 3, 3)
    revised = build_plan(start, end, 12, 12, 12)
    assert original.training_overlap == pytest.approx(0.875)
    assert original.variance_inflation() == pytest.approx(5.38, abs=0.05)
    assert revised.training_overlap == 0.0
    assert revised.variance_inflation() == 1.0
    assert revised.oos_months > original.oos_months
    assert revised.effective_n(414) == pytest.approx(414)
    assert original.effective_n(276) == pytest.approx(51, abs=1)


def test_a_smaller_step_buys_nothing():
    """It raises nominal N and inflation in near-equal measure."""
    start, end = datetime(2019, 1, 1, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC)
    per_month = 11.5
    effective = []
    for step in (3, 6, 12):
        plan = build_plan(start, end, 24, step, step)
        if len(plan.folds) < 2:
            continue
        effective.append(plan.effective_n(int(plan.oos_months * per_month)))
    assert max(effective) / min(effective) < 4.0


def test_holdout_is_carved_off_the_end_and_never_in_a_fold():
    plan = build_plan(
        datetime(2019, 1, 1, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC), 12, 12, 12
    )
    assert plan.holdout_start == datetime(2023, 1, 1, tzinfo=UTC)
    assert all(f.test_end <= plan.holdout_start for f in plan.folds)


def test_purging_drops_training_events_whose_label_reaches_the_test_block():
    plan = build_plan(
        datetime(2019, 1, 1, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC), 12, 12, 12
    )
    fold = plan.folds[0]
    safe = LabelledEvent(fold.train_start + timedelta(days=5),
                         fold.train_start + timedelta(days=6), 1, {})
    leaky = LabelledEvent(fold.train_end - timedelta(days=1),
                          fold.test_start + timedelta(days=3), 1, {})
    kept = purge_and_embargo([safe, leaky], fold, timedelta(days=1))
    assert safe in kept and leaky not in kept


def test_embargo_drops_events_right_up_against_the_test_block():
    plan = build_plan(
        datetime(2019, 1, 1, tzinfo=UTC), datetime(2024, 1, 1, tzinfo=UTC), 12, 12, 12
    )
    fold = plan.folds[0]
    adjacent = LabelledEvent(fold.train_end - timedelta(hours=2),
                             fold.train_end - timedelta(hours=1), 1, {})
    assert purge_and_embargo([adjacent], fold, timedelta(days=7)) == []
    assert purge_and_embargo([adjacent], fold, timedelta(minutes=1)) == [adjacent]


def test_calibration_split_takes_the_tail_of_the_training_window():
    events = [
        LabelledEvent(T0 + timedelta(days=i), T0 + timedelta(days=i, hours=1), 1, {})
        for i in range(10)
    ]
    fit, calibrate = calibration_split(events, 0.2)
    assert len(fit) == 8 and len(calibrate) == 2
    assert all(c.decision_at > max(f.decision_at for f in fit) for c in calibrate)
    assert calibration_split([], 0.2) == ([], [])


def test_max_label_horizon_sets_the_embargo():
    events = [
        LabelledEvent(T0, T0 + timedelta(hours=3), 1, {}),
        LabelledEvent(T0, T0 + timedelta(days=2), 1, {}),
    ]
    assert max_label_horizon(events) == timedelta(days=2)
    assert max_label_horizon([]) == timedelta(days=1)


# -- ablation ----------------------------------------------------------------
def test_required_sharpe_rises_with_the_trial_count():
    values = [required_annual_sharpe(n, 1260) for n in (1, 4, 16, 64)]
    assert values == sorted(values)


def test_doubling_the_trials_costs_roughly_a_sixth_of_a_sharpe_point():
    """docs/07 §4: the exchange rate between search and demonstrable performance."""
    delta = required_annual_sharpe(16, 1260) - required_annual_sharpe(8, 1260)
    assert 0.05 < delta < 0.25


def test_c3_prediction_is_branch_a():
    """Predicted baseline Sharpe 0.6 over 756 OOS days affords no selection."""
    decision = determine_branch(0.6, 756, 1.0, now=T0)
    assert decision.branch is Branch.A_NO_SELECTION
    assert decision.arms == ()
    assert "NOT run as selection" in decision.rationale


def test_partial_selection_of_nothing_is_reported_as_branch_a():
    decision = determine_branch(1.52, 756, 1.0, param_settings=3, now=T0)
    assert decision.branch is Branch.A_NO_SELECTION


def test_full_branch_names_the_arms_in_the_pre_declared_order():
    decision = determine_branch(1.8, 2016, 0.5, param_settings=1, now=T0)
    assert decision.branch is Branch.C_FULL
    assert decision.arms == tuple(name for name, _ in TIER1_ARM_ORDER)


def test_saturated_budget_is_flagged_rather_than_reported_as_a_number():
    decision = determine_branch(3.0, 2016, 0.2, now=T0)
    assert decision.budget_unbounded
    assert "not" in decision.rationale and "meaningful" in decision.rationale


def test_affordable_trials_is_zero_when_the_sharpe_is_too_low():
    assert affordable_trials(252, 0.5) == 0


def test_low_affected_fraction_demands_an_implausible_per_trade_effect():
    """The number to read is the per-affected-trade MDE, which grows as 1/sqrt(f)."""
    overall = [mde_paired_crn(414, f, 1.0, 6) for f in (0.05, 0.25, 1.0)]
    per_trade = [mde_per_affected_trade(414, f, 1.0, 6) for f in (0.05, 0.25, 1.0)]
    assert overall == sorted(overall)
    assert per_trade == sorted(per_trade, reverse=True)
    assert per_trade[0] > 3 * per_trade[-1]


def test_trades_required_inverts_the_mde():
    needed = trades_required(0.10, 1.0, 1.0, 6)
    assert mde_paired_crn(needed, 1.0, 1.0, 6) == pytest.approx(0.10, rel=0.02)


def test_holm_correction_raises_the_critical_z_with_more_tests():
    assert holm_critical_z(0.05, 6) > holm_critical_z(0.05, 1)
    assert holm_adjust([]) == []
    assert holm_adjust([0.01, 0.5]) == [pytest.approx(0.02), pytest.approx(0.5)]


def test_sigma_d_is_measured_on_the_affected_subset_only():
    diffs = [0.0] * 90 + [1.0, -1.0, 2.0, -2.0] * 5
    assert estimate_sigma_d(diffs) > 0
    assert math.isnan(estimate_sigma_d([0.0] * 50))


def test_an_unresolved_arm_is_not_scored_in_either_direction():
    """Counting it as 'contributes nothing' is the self-confirming move."""
    rng = np.random.default_rng(9)
    full = list(rng.normal(0.2, 1.4, 300))
    diffs = [0.0] * 250 + list(rng.normal(-0.01, 1.0, 50))
    arm = evaluate_arm(
        "sr", frozenset({Tag.SR}), full, [a + b for a, b in zip(full, diffs, strict=True)],
        diffs, 5, True, bootstrap_paths=200,
    )
    assert not arm.resolvable
    assert "unresolved" in arm.verdict and "cannot tell" in arm.verdict
    assert arm.features_retained < 66


def test_a_large_effect_is_resolvable_and_named_as_helping():
    rng = np.random.default_rng(4)
    full = list(rng.normal(0.2, 0.3, 400))
    diffs = [-1.5] * 400
    arm = evaluate_arm(
        "sessions", frozenset({Tag.TIME}), full,
        [a + b for a, b in zip(full, diffs, strict=True)], diffs, 5, True,
        bootstrap_paths=200,
    )
    assert arm.resolvable and "it helps" in arm.verdict


# -- seal --------------------------------------------------------------------
def test_seal_round_trip():
    with tempfile.TemporaryDirectory() as td:
        seal = Tier2Seal(Path(td))
        tier1 = {"expectancy": 0.21}
        seal.commit_tier1(tier1, now=T0)
        seal.seal_tier2({"quarterly": -0.01})
        assert seal.unseal(tier1, now=T0 + timedelta(hours=1)) == {"quarterly": -0.01}
        assert seal.unsealed_at is not None
        assert not seal.breaches


def test_revising_tier1_after_sealing_is_refused():
    with tempfile.TemporaryDirectory() as td:
        seal = Tier2Seal(Path(td))
        seal.commit_tier1({"expectancy": 0.21}, now=T0)
        seal.seal_tier2({"quarterly": -0.01})
        with pytest.raises(SealBreach, match="changed after it was committed"):
            seal.unseal({"expectancy": 0.99})


def test_unsealing_without_a_commit_is_refused():
    with tempfile.TemporaryDirectory() as td, pytest.raises(SealBreach, match="never committed"):
        Tier2Seal(Path(td)).unseal({"x": 1})


def test_sealing_early_is_recorded_as_a_breach_with_a_trial_penalty():
    with tempfile.TemporaryDirectory() as td:
        seal = Tier2Seal(Path(td))
        seal.seal_tier2({"x": 1})
        assert seal.breaches and seal.penalty_trials() == 4


# -- report ------------------------------------------------------------------
def _report(**overrides) -> ValidationReport:
    rng = np.random.default_rng(13)
    oos = [2.0 if rng.random() < 0.42 else -1.0 for _ in range(400)]
    daily = list(rng.normal(0.06, 0.5, size=1260))
    st = trade_stats(oos)
    ci = bootstrap_ci(
        stationary_block_bootstrap(oos, paths=200, seed=1), st.expectancy, method="block"
    )
    block, iid = monte_carlo_sections(oos, paths=200, seed=2)
    defaults = {
        "generated_at": T0,
        "git_sha": "abcdef123456",
        "config_hash": "cfg1",
        "data_start": datetime(2019, 1, 1, tzinfo=UTC),
        "data_end": datetime(2024, 1, 1, tzinfo=UTC),
        "oos_stats": st,
        "oos_ci": ci,
        "holdout_stats": trade_stats(oos[:69]),
        "dsr": deflated_sharpe(daily, n_trials=4),
        "branch": determine_branch(1.0, 1260, 1.0, now=T0),
        "monte_carlo_block": block,
        "monte_carlo_iid": iid,
        "tie_break_share": 0.02,
        "rollover_share": 0.03,
        "walkforward_inflation": 1.0,
        "effective_n": 400.0,
        "trials_counted": 4,
        "calendar_point_in_time": True,
    }
    defaults.update(overrides)
    return ValidationReport(**defaults)


def test_report_carries_the_mandatory_disclaimer_in_both_formats():
    """A module constant, asserted here so it cannot be removed by editing a template."""
    from aurion import BACKTEST_DISCLAIMER

    report = _report()
    assert BACKTEST_DISCLAIMER in report.to_markdown()
    assert BACKTEST_DISCLAIMER in report.to_json()


def test_thin_oos_sample_fails_the_revised_trade_criterion():
    report = _report(oos_stats=trade_stats([2.0, -1.0] * 50))
    criterion = next(c for c in report.criteria() if c.name == "oos_trades")
    assert not criterion.passed and str(MIN_OOS_TRADES) in criterion.detail


def test_a_tie_break_heavy_run_fails():
    report = _report(tie_break_share=0.12)
    assert not next(c for c in report.criteria() if c.name == "tie_break_share").passed
    assert not report.passes


def test_a_seal_breach_fails_the_report():
    report = _report(seal_breaches=["tier2 sealed before tier1 was committed"])
    criterion = next(c for c in report.criteria() if c.name == "seal_intact")
    assert not criterion.passed and "SEAL BREACH" in criterion.detail


def test_financing_and_calendar_are_warnings_not_blockers():
    report = _report(rollover_share=0.5, calendar_point_in_time=False)
    names = {c.name for c in report.criteria() if not c.passed}
    assert {"financing_not_material", "calendar_point_in_time"} <= names
    assert all(not c.blocking for c in report.criteria() if c.name in names)


def test_branch_a_report_states_that_no_module_was_selected():
    report = _report(branch=determine_branch(0.6, 756, 1.0, now=T0))
    assert "No module was selected on evidence" in report.to_markdown()


def test_report_writes_both_files():
    with tempfile.TemporaryDirectory() as td:
        md, js = _report().write(td)
        assert md.exists() and js.exists()
        assert md.read_text(encoding="utf-8").startswith("# Aurion validation report")


# -- the replay runner -------------------------------------------------------
# These are the only tests that exercise the class producing every validated number. They
# assert the structural properties -- accounting, determinism, which config reaches the
# replay -- not profitability: a series short enough for a unit test cannot contain the
# two confirmed W1 swings per side the bias gate needs, so zero emissions is the expected
# outcome and an emission count would be the wrong thing to assert.
_INST = "XAUUSD"


def _replay_m1(minutes: int = 60 * 24 * 3) -> list[M1Candle]:
    """Deterministic M1 with a weekend hole, in instrument units rather than FX pips."""
    out: list[M1Candle] = []
    t = datetime(2024, 6, 12, 0, 0, tzinfo=UTC)  # a Wednesday
    price = 2300.0
    for i in range(minutes):
        step = math.sin(i * 0.017) * 0.45 + math.cos(i * 0.0031) * 0.2
        price += step
        o, c = price, price + step * 0.4
        hi, lo = max(o, c) + 0.12, min(o, c) - 0.12
        half = 0.09
        out.append(
            M1Candle(
                _INST,
                t,
                t + timedelta(minutes=1),
                OHLC(o - half, hi - half, lo - half, c - half),
                OHLC(o + half, hi + half, lo + half, c + half),
                60 + (i % 40),
            )
        )
        t += timedelta(minutes=1)
    return out


def _runner(cfg, anchors: dict[TimeFrame, Anchor] | None = None) -> BacktestRunner:
    tracker = SpreadTracker(
        tz="America/New_York", bucket_minutes=15, window_obs=20, max_median_mult=99.0
    )
    windows = build_window_set(cfg)
    gate = AdmissibilityGate(cfg, windows, tracker, StaticCalendarProvider([]))
    params = AnalysisParams(point=0.01, swing_half_width=2)
    store = ObjectStore(params)
    engine = SignalEngine(
        EngineParams(require_w1_d1_agreement=False, rr_min=2.0, expected_r_min=0.0, p_floor=0.0),
        store,
        gate,
        {_INST: InstrumentMeta(0.01, 100.0, "XAU", "USD")},
        ConstantProbabilityModel(p=0.45),
    )
    return BacktestRunner(
        RunnerConfig(entry_timeframe=TimeFrame.M15, windows=windows, anchors=anchors),
        engine,
        gate,
        store,
        CostParams(point=0.01),
        params,
        {_INST: 0.01},
    )


def test_the_funnel_accounts_for_every_evaluation(cfg):
    """Rejections plus emissions must equal evaluations, or the funnel is not a measurement.

    An off-by-one here would be invisible in any single percentage and would silently
    misattribute which gate is doing the filtering -- the quantity the module cut rests on.
    """
    result = _runner(cfg).run({_INST: _replay_m1()}, config_hash="t")
    assert result.evaluations > 0
    assert sum(result.funnel.by_step.values()) + result.funnel.emitted == result.funnel.evaluated
    assert result.funnel.evaluated == result.evaluations
    assert sum(result.funnel.by_reason.values()) == sum(result.funnel.by_step.values())


def test_a_replay_is_deterministic(cfg):
    """Compared on the decision digest, not the ledger digest.

    With no resolved trades the ledger digest is the hash of `[]` -- a constant -- so a
    comparison on it would pass even if the engine were non-deterministic. Every run so far
    has resolved zero trades, and a live system inside the W1 warm-up resolves none either,
    so that is the normal case rather than an edge one.
    """
    candles = _replay_m1()
    first = _runner(cfg).run({_INST: candles}, config_hash="t")
    second = _runner(cfg).run({_INST: candles}, config_hash="t")

    assert first.decision_digest() == second.decision_digest()
    assert first.funnel.by_reason == second.funnel.by_reason
    assert (first.evaluations, first.funnel.emitted) == (second.evaluations, second.funnel.emitted)


def test_the_decision_digest_is_sensitive_to_a_changed_decision():
    """Otherwise the determinism check above is as vacuous as the one it replaced."""
    base = BacktestResult(config_hash="t")
    base.candidates.append(Candidate("XAUUSD", T0, Step.HTF_BIAS, "bias:unavailable"))
    other = BacktestResult(config_hash="t")
    other.candidates.append(Candidate("XAUUSD", T0, Step.HTF_BIAS, "bias:no_direction"))
    empty = BacktestResult(config_hash="t")

    assert base.decision_digest() != other.decision_digest()
    assert base.decision_digest() != empty.decision_digest()
    # And the point of the change: the ledger digest cannot tell these two apart at all.
    assert base.ledger_digest() == empty.ledger_digest()


def test_the_disclaimer_travels_with_the_result(cfg):
    """No code path produces numbers without it."""
    result = _runner(cfg).run({_INST: _replay_m1(600)}, config_hash="t")
    assert "not predictions" in result.disclaimer


def test_an_empty_input_is_an_empty_result_not_a_crash(cfg):
    result = _runner(cfg).run({}, config_hash="t")
    assert (result.evaluations, result.trades, result.funnel.evaluated) == (0, [], 0)


def test_max_evaluations_bounds_the_run(cfg):
    result = _runner(cfg).run({_INST: _replay_m1()}, config_hash="t", max_evaluations=25)
    assert result.evaluations == 25


def test_the_rollover_boundary_follows_the_configured_anchor():
    """`data.anchors` was declared, validated, and then ignored by every bucketer.

    Resampling on one definition of a day while reporting on another is the failure this
    pins: the H4/D1 anchor is a contested prior with a planned robustness run (Q2), so an
    override that changed nothing would have tested nothing.
    """
    opened = datetime(2024, 6, 12, 23, 0, tzinfo=UTC)
    closed = datetime(2024, 6, 13, 1, 0, tzinfo=UTC)
    utc_day = bucketer(TimeFrame.D1, Anchor("UTC", time(0, 0)))
    # 17:00 NY is 21:00 UTC in June, so both instants are in the same FX day ...
    assert not holds_through_rollover(opened, closed)
    # ... and in different UTC days.
    assert holds_through_rollover(opened, closed, utc_day)


def test_a_configured_anchor_reaches_the_resampler():
    candles = _replay_m1()
    ny = ResamplerSet(_INST, (TimeFrame.D1,))
    utc = ResamplerSet(_INST, (TimeFrame.D1,), {TimeFrame.D1: Anchor("UTC", time(0, 0))})
    ny_bars = [b for c in candles for b in ny.push(c)]
    utc_bars = [b for c in candles for b in utc.push(c)]
    assert ny_bars and utc_bars
    # The default anchor closes the FX day at 21:00 UTC in June; the override at midnight.
    assert {b.open_time.hour for b in ny_bars} == {21}
    assert {b.open_time.hour for b in utc_bars} == {0}


def test_the_declared_anchors_parse_and_match_the_defaults(cfg):
    """A behaviour-preserving check: wiring the config must not have moved any boundary."""
    anchors = cfg.anchors()
    assert anchors[TimeFrame.D1] == DEFAULT_ANCHORS[TimeFrame.D1]
    assert anchors[TimeFrame.W1] == DEFAULT_ANCHORS[TimeFrame.W1]
    assert anchors[TimeFrame.M15] == DEFAULT_ANCHORS[TimeFrame.M15]
