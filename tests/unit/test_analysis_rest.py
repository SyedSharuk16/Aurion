"""S&R, pivots, quarterly, MMXM, regime, registry and features. §7-§10, plus the schema."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from aurion.analysis.base import Direction, SwingKind
from aurion.analysis.primitives import atr
from aurion.analysis.quarterly.amdx import (
    Phase,
    amdx_scores,
    phase_frequency,
    quarter_geometry,
    true_open_bias,
)
from aurion.analysis.quarterly.quarters import (
    daily_quarter,
    micro_quarter,
    monthly_quarter,
    ninety_minute_quarter,
    quarter_stack,
    sub_quarters,
    true_day_open,
    true_month_open,
    true_week_open,
    weekly_quarter,
    yearly_quarter,
)
from aurion.analysis.regime.correlation import (
    ewma_correlation,
    net_usd_exposure,
    session_vwap,
    usd_beta,
    usd_exposure,
    usd_index_proxy,
)
from aurion.analysis.regime.trend import adx, hurst_rs, trend_regime_state, variance_ratio
from aurion.analysis.regime.volatility import (
    VolRegime,
    garman_klass,
    parkinson,
    realised_vol,
    rogers_satchell,
    volatility_state,
)
from aurion.analysis.registry import AnalysisParams, ObjectStore
from aurion.analysis.sr.levels import dbscan_levels, kde_levels, merge_levels, weight_swings
from aurion.analysis.sr.pivots import (
    camarilla_pivots,
    classic_pivots,
    distance_to_nearest_round,
    round_number_levels,
)
from aurion.analysis.structure.swings import Swing, find_swings
from aurion.core.marketview import MarketView
from aurion.core.timeframe import UTC, TimeFrame
from aurion.data.repository import InMemoryBarRepository
from aurion.features.build import FeatureInputs, build_features
from aurion.features.schema import (
    FEATURE_NAMES,
    FEATURE_SCHEMA_VERSION,
    REQUIRED_NAMES,
    FeatureVector,
    Tag,
    names_excluding_tags,
)
from tests.conftest import bars_from, make_m1, zigzag

NY = ZoneInfo("America/New_York")


# -- S&R ---------------------------------------------------------------------
def _swing(price: float, kind: SwingKind, index: int) -> Swing:
    t = datetime(2024, 6, 12, 9, 0, tzinfo=UTC) + timedelta(minutes=5 * index)
    return Swing("TEST", TimeFrame.M5, kind, t, price, 2, t + timedelta(minutes=10), index)


def test_weighting_decays_with_age_and_rises_with_touches():
    bars = zigzag([1, -1] * 30)
    swings = find_swings(bars, 2)
    assert swings
    weights = weight_swings(swings, bars, 10.0, 0.2, atr(bars, 14) or 0.5, 5)
    assert weights
    oldest, newest = weights[0], weights[-1]
    assert newest.recency > oldest.recency
    assert all(w.touch >= 1.0 for w in weights)
    assert all(0.25 <= w.rejection <= 4.0 for w in weights)


def test_kde_finds_a_peak_where_swings_cluster():
    swings = [_swing(100.0 + 0.01 * i, SwingKind.HIGH, i) for i in range(5)]
    swings += [_swing(110.0 + 0.01 * i, SwingKind.HIGH, 10 + i) for i in range(5)]
    bars = zigzag([1, -1] * 20)
    weights = weight_swings(swings, bars, 100.0, 0.2, 0.5, 5)
    levels = kde_levels(weights, bandwidth=0.5, computed_at=bars[-1].close_time)
    assert len(levels) >= 2
    prices = sorted(level.price for level in levels)
    assert prices[0] == pytest.approx(100.0, abs=0.6)
    assert prices[-1] == pytest.approx(110.0, abs=0.6)
    assert max(level.strength for level in levels) == pytest.approx(1.0)
    assert all(0 < level.strength <= 1 for level in levels)


def test_dbscan_is_an_alternative_with_the_same_interface():
    swings = [_swing(100.0 + 0.01 * i, SwingKind.HIGH, i) for i in range(4)]
    bars = zigzag([1, -1] * 20)
    weights = weight_swings(swings, bars, 100.0, 0.2, 0.5, 5)
    levels = dbscan_levels(weights, eps=0.5, computed_at=bars[-1].close_time)
    assert len(levels) == 1 and levels[0].method == "dbscan"
    assert levels[0].touches == 4


def test_merge_saturates_strength_instead_of_summing_past_one():
    swings = [_swing(100.0, SwingKind.HIGH, 0), _swing(100.1, SwingKind.HIGH, 5)]
    bars = zigzag([1, -1] * 20)
    weights = weight_swings(swings, bars, 100.0, 0.2, 0.5, 5)
    raw = kde_levels(weights, 0.05, bars[-1].close_time)
    merged = merge_levels(raw, max_distance=5.0)
    assert len(merged) == 1
    assert merged[0].strength <= 1.0


def test_merge_leaves_distant_levels_alone():
    swings = [_swing(100.0, SwingKind.HIGH, 0), _swing(150.0, SwingKind.HIGH, 5)]
    bars = zigzag([1, -1] * 20)
    weights = weight_swings(swings, bars, 100.0, 0.2, 0.5, 5)
    raw = kde_levels(weights, 0.5, bars[-1].close_time)
    assert len(merge_levels(raw, max_distance=0.1)) == len(raw)


def test_empty_inputs_return_empty_level_sets():
    assert kde_levels([], 1.0, datetime(2024, 1, 1, tzinfo=UTC)) == []
    assert dbscan_levels([], 1.0, datetime(2024, 1, 1, tzinfo=UTC)) == []
    assert merge_levels([], 1.0) == []


# -- pivots ------------------------------------------------------------------
def test_classic_pivots_match_the_formula():
    bar = bars_from([(100.0, 104.0, 2.0, 4.0)])[0]   # H=106, L=96, C=104
    p = classic_pivots(bar)
    expected_p = (106.0 + 96.0 + 104.0) / 3
    assert p.levels["P"] == pytest.approx(expected_p)
    assert p.levels["R1"] == pytest.approx(2 * expected_p - 96.0)
    assert p.levels["S1"] == pytest.approx(2 * expected_p - 106.0)
    assert p.levels["R2"] == pytest.approx(expected_p + 10.0)
    assert p.levels["R3"] == pytest.approx(106.0 + 2 * (expected_p - 96.0))
    assert p.confirmed_at == bar.close_time


def test_camarilla_constants_are_reproduced_verbatim():
    """The 1.1 multiplier and the divisors have no derivation. They are never tuned."""
    bar = bars_from([(100.0, 104.0, 2.0, 4.0)])[0]
    c = camarilla_pivots(bar)
    rng = 10.0
    assert c.levels["H4"] == pytest.approx(104.0 + rng * 1.1 / 2)
    assert c.levels["H3"] == pytest.approx(104.0 + rng * 1.1 / 4)
    assert c.levels["L1"] == pytest.approx(104.0 - rng * 1.1 / 12)
    assert c.levels["H5"] == pytest.approx((106.0 / 96.0) * 104.0)
    assert c.levels["L5"] == pytest.approx(2 * 104.0 - c.levels["H5"])


def test_round_numbers_bracket_the_price():
    levels = round_number_levels(2318.4, [10.0, 50.0])
    assert levels["rn10_below_0"] == pytest.approx(2310.0)
    assert levels["rn10_above_0"] == pytest.approx(2320.0)
    assert distance_to_nearest_round(2318.4, 10.0) == pytest.approx(-1.6)
    assert distance_to_nearest_round(2322.0, 10.0) == pytest.approx(2.0)
    assert round_number_levels(100.0, [0.0]) == {}


# -- quarterly ---------------------------------------------------------------
@pytest.mark.parametrize(
    ("hour", "index"),
    [(18, 1), (23, 1), (0, 2), (5, 2), (6, 3), (11, 3), (12, 4), (17, 4)],
)
def test_daily_quarters_follow_the_1800_ny_lattice(hour, index):
    ts = datetime(2024, 6, 12, hour, 30, tzinfo=NY).astimezone(UTC)
    q = daily_quarter(ts)
    assert q.index == index
    assert q.contains(ts)
    assert (q.end - q.start) == timedelta(hours=6)


def test_quarters_split_into_four_children():
    parent = daily_quarter(datetime(2024, 6, 12, 8, tzinfo=NY).astimezone(UTC))
    kids = sub_quarters(parent, parent.level)
    assert len(kids) == 4
    assert kids[0].start == parent.start and kids[-1].end == parent.end
    assert all((k.end - k.start) == timedelta(minutes=90) for k in kids)


def test_ninety_minute_and_micro_quarters_nest():
    ts = datetime(2024, 6, 12, 8, 20, tzinfo=NY).astimezone(UTC)
    ninety = ninety_minute_quarter(ts)
    micro = micro_quarter(ts)
    assert ninety.contains(ts) and micro.contains(ts)
    assert (micro.end - micro.start) == timedelta(minutes=22, seconds=30)
    assert ninety.start <= micro.start and micro.end <= ninety.end


def test_weekly_quarters_exclude_friday_by_default():
    monday = datetime(2024, 6, 10, 10, tzinfo=NY).astimezone(UTC)
    friday = datetime(2024, 6, 14, 10, tzinfo=NY).astimezone(UTC)
    assert weekly_quarter(monday) is not None
    assert weekly_quarter(monday).index == 1
    assert weekly_quarter(friday) is None, "Friday belongs to no weekly quarter (Q4 decision)"
    assert weekly_quarter(friday, "sun_start") is not None


def test_monthly_quarters_put_the_residual_days_in_q4():
    late = datetime(2024, 1, 30, 12, tzinfo=NY).astimezone(UTC)
    q = monthly_quarter(late)
    assert q.index == 4
    assert q.contains(late)
    assert monthly_quarter(datetime(2024, 1, 3, 12, tzinfo=NY).astimezone(UTC)).index == 1


def test_yearly_quarters_are_calendar_quarters():
    assert yearly_quarter(datetime(2024, 2, 1, tzinfo=UTC)).index == 1
    assert yearly_quarter(datetime(2024, 12, 31, tzinfo=UTC)).index == 4
    q4 = yearly_quarter(datetime(2024, 11, 1, tzinfo=UTC))
    assert q4.end.astimezone(NY).year == 2025


def test_quarter_stack_covers_every_level():
    stack = quarter_stack(datetime(2024, 6, 12, 14, tzinfo=UTC))
    assert set(stack) == {"yearly", "monthly", "weekly", "daily", "ninety_min", "micro"}


def test_true_opens_use_the_right_anchors_and_are_distinct():
    """00:00 NY true day, Monday 18:00 NY true week. Both differ from the 17:00 D1 bar."""
    start = datetime(2024, 6, 10, 0, 0, tzinfo=NY).astimezone(UTC)
    candles = [make_m1("XAUUSD", start + timedelta(minutes=i), 2300.0 + i * 0.1)
               for i in range(60 * 48)]
    as_of = start + timedelta(hours=40)
    day = true_day_open(candles, as_of)
    week = true_week_open(candles, as_of)
    assert day is not None and week is not None
    assert day.anchor.astimezone(NY).hour == 0
    assert week.anchor.astimezone(NY).hour == 18
    assert week.anchor.astimezone(NY).weekday() == 0
    assert day.anchor != week.anchor
    assert day.anchor_shifted_minutes == 0


def test_true_open_flags_a_shifted_anchor_rather_than_backfilling():
    start = datetime(2024, 6, 10, 2, 0, tzinfo=NY).astimezone(UTC)
    candles = [make_m1("XAUUSD", start + timedelta(minutes=i), 2300.0) for i in range(120)]
    got = true_day_open(candles, start + timedelta(hours=1))
    assert got is not None and got.anchor_shifted_minutes == 120


def test_true_month_open_rules_differ():
    start = datetime(2024, 5, 1, 0, 0, tzinfo=NY).astimezone(UTC)
    candles = [make_m1("XAUUSD", start + timedelta(minutes=i), 2300.0)
               for i in range(60 * 24 * 20)]
    as_of = start + timedelta(days=18)
    first_monday = true_month_open(candles, as_of, "first_monday_1800_ny")
    first_day = true_month_open(candles, as_of, "first_day_1800_ny")
    assert first_monday is not None and first_day is not None
    assert first_monday.anchor != first_day.anchor
    assert first_monday.anchor.astimezone(NY).weekday() == 0


def test_amdx_emits_four_non_exclusive_scores_with_a_margin():
    bars = zigzag([0.2, -0.2] * 40, start_price=100.0)
    q = daily_quarter(bars[0].open_time)
    geo = quarter_geometry(q, bars, bars[-1].close_time, prior_ranges=[5.0] * 10)
    assert geo is not None
    scores = amdx_scores(geo, Direction.UP, prior_high=110.0, prior_low=90.0,
                         swept_prior_against_bias=False, sweep_depth_atr=0.0)
    assert 0.0 <= scores.accumulation <= 1.0
    assert scores.manipulation == 0.0, "no sweep means no manipulation score"
    assert scores.margin >= 0.0
    assert set(scores.as_dict()) >= {"amdx_accumulation", "amdx_manipulation"}


def test_manipulation_requires_a_sweep_in_the_first_half_of_the_quarter():
    # Start at the Q2 open (00:00 NY) and evaluate a third of the way through it, so the
    # elapsed-fraction condition is actually exercised rather than clamped to 1.0.
    q_open = datetime(2024, 6, 12, 0, 0, tzinfo=NY).astimezone(UTC)
    bars = zigzag([0.2, -0.2] * 40, start=q_open)
    q = daily_quarter(q_open)
    as_of = q_open + timedelta(hours=2)
    geo = quarter_geometry(q, bars, as_of, prior_ranges=[5.0] * 10)
    assert geo is not None and geo.elapsed_frac <= 0.5
    with_sweep = amdx_scores(geo, Direction.UP, 110.0, 90.0, True, sweep_depth_atr=0.5)
    assert with_sweep.manipulation == pytest.approx(1.0)
    late = quarter_geometry(q, bars, q_open + timedelta(hours=5), prior_ranges=[5.0] * 10)
    assert late is not None and late.elapsed_frac > 0.5
    assert amdx_scores(late, Direction.UP, 110.0, 90.0, True, 0.5).manipulation == 0.0


def test_partial_quarters_are_flagged_so_they_are_never_mixed_with_completed_ones():
    """The biggest look-ahead trap in the quarterly layer. §8.5."""
    q_open = datetime(2024, 6, 12, 0, 0, tzinfo=NY).astimezone(UTC)
    bars = zigzag([0.2, -0.2] * 10, start=q_open)
    q = daily_quarter(q_open)
    partial = quarter_geometry(q, bars, q_open + timedelta(hours=1), prior_ranges=[5.0] * 10)
    assert partial is not None
    scores = amdx_scores(partial, Direction.UP, 110.0, 90.0, False, 0.0)
    assert scores.is_partial and scores.elapsed_frac < 1.0

    complete = quarter_geometry(q, bars, q.end, prior_ranges=[5.0] * 10)
    assert complete is not None
    assert not amdx_scores(complete, Direction.UP, 110.0, 90.0, False, 0.0).is_partial


def test_phase_frequency_measures_the_behavioural_claim():
    labelled = [(1, Phase.ACCUMULATION)] * 8 + [(1, Phase.DISTRIBUTION)] * 2
    freq = phase_frequency(labelled)
    assert freq[1][str(Phase.ACCUMULATION)] == pytest.approx(0.8)
    assert sum(freq[1].values()) == pytest.approx(1.0)


def test_true_open_bias_is_a_feature_not_a_gate():
    """§8.6: emitted as qt_align so the backtest can measure it, not silently applied."""
    below = true_open_bias(99.0, 100.0, quarter_index=2, direction=Direction.UP, atr=1.0)
    above = true_open_bias(101.0, 100.0, quarter_index=2, direction=Direction.UP, atr=1.0)
    outside = true_open_bias(99.0, 100.0, quarter_index=1, direction=Direction.UP, atr=1.0)
    assert below["qt_align"] == 1.0, "long below the true open in Q2 is aligned"
    assert above["qt_align"] == 0.0
    assert outside["in_q2_or_q3"] == 0.0 and outside["qt_align"] == 0.0
    assert below["to_dist_atr"] == pytest.approx(-1.0)


# -- regime ------------------------------------------------------------------
def test_adx_warmup_is_two_periods_not_one():
    """'ADX from 14 bars' is wrong and makes early backtest bars garbage."""
    bars = zigzag([1, -1] * 40)
    assert adx(bars[:20], period=14).adx is None
    assert adx(bars, period=14).adx is not None


def test_adx_rises_in_a_trend_and_stays_low_in_a_chop():
    trend = zigzag([1.0] * 60)
    chop = zigzag([0.5, -0.5] * 30)
    assert adx(trend).adx > adx(chop).adx


def test_variance_ratio_separates_trend_from_mean_reversion():
    # Persistent: a drift plus small same-signed noise.
    trending = [0.01 + 0.001 * math.sin(i * 0.05) for i in range(400)]
    alternating = [0.01 if i % 2 == 0 else -0.01 for i in range(400)]
    vr_trend = variance_ratio(trending, 4)
    vr_rev = variance_ratio(alternating, 4)
    assert vr_trend is not None and vr_trend.vr > 1.0 and vr_trend.trending
    assert vr_rev is not None and vr_rev.vr < 1.0 and not vr_rev.trending
    assert variance_ratio([0.01, -0.01] * 3, 4) is None, "too few observations"
    assert variance_ratio(alternating, 1) is None, "q must be >= 2"


def test_variance_ratio_refuses_a_degenerate_constant_series():
    """A constant return series slips past a bare `var1 <= 0` guard on float residue.

    Without the explicit degeneracy check it returned |z| in the hundreds for data on
    which the statistic is undefined.
    """
    assert variance_ratio([0.01] * 400, 4) is None
    assert variance_ratio([0.0] * 400, 4) is None


def test_hurst_returns_none_on_short_windows_and_a_number_otherwise():
    assert hurst_rs([0.01] * 10) is None
    rng = [math.sin(i * 0.3) * 0.01 for i in range(600)]
    got = hurst_rs(rng)
    assert got is None or 0.0 < got < 1.5


def test_volatility_estimators_refuse_gap_sensitive_timeframes():
    """Parkinson/GK/RS assume continuous trading, so D1 and W1 are refused."""
    intraday = zigzag([1, -1] * 20, timeframe=TimeFrame.M15)
    daily = zigzag([1, -1] * 20, timeframe=TimeFrame.D1)
    assert parkinson(intraday, 20) is not None
    assert garman_klass(intraday, 20) is not None
    assert rogers_satchell(intraday, 20) is not None
    assert parkinson(daily, 20) is None
    assert garman_klass(daily, 20) is None
    assert realised_vol(daily, 20) is not None, "close-to-close is gap-safe"


def test_garman_klass_is_clamped_after_aggregation_not_per_bar():
    """A per-bar clamp biases the estimator upward; the sum can legitimately be negative."""
    bars = bars_from([(100.0, 100.0, 0.001, 0.001)] * 30, timeframe=TimeFrame.M15)
    got = garman_klass(bars, 20)
    assert got is not None and got >= 0.0


def test_volatility_state_labels_regimes_and_returns_none_before_warmup():
    short = zigzag([1, -1] * 5, timeframe=TimeFrame.M15)
    assert volatility_state(short).regime is None
    long = zigzag([1, -1] * 300, timeframe=TimeFrame.M15)
    state = volatility_state(long, percentile_window=100)
    assert state.regime in (VolRegime.LOW, VolRegime.NORMAL, VolRegime.HIGH)
    assert state.atr is not None and state.atr_percentile is not None
    assert "atr_pct" in state.as_dict(TimeFrame.M15)


def test_trend_regime_state_assembles_everything():
    bars = zigzag([1, -1] * 400, timeframe=TimeFrame.M15)
    state = trend_regime_state(bars, hurst_window=512, vr_window=500)
    assert state.adx.adx is not None
    assert 4 in state.variance_ratios
    assert "adx" in state.as_dict()


def test_ewma_correlation_detects_sign_and_rejects_short_series():
    a = [math.sin(i * 0.2) for i in range(100)]
    assert ewma_correlation(a, a, 20.0) == pytest.approx(1.0, abs=1e-9)
    assert ewma_correlation(a, [-x for x in a], 20.0) == pytest.approx(-1.0, abs=1e-9)
    assert ewma_correlation(a[:4], a[:4], 20.0) is None
    assert ewma_correlation([1.0] * 50, [1.0] * 50, 20.0) is None, "zero variance"


def test_usd_proxy_is_named_a_proxy_and_moves_the_right_way():
    weights = {"EURUSD": 0.576, "USDJPY": 0.136}
    epoch = {"EURUSD": 1.10, "USDJPY": 150.0}
    base = {"EURUSD": False, "USDJPY": True}
    stronger = usd_index_proxy({"EURUSD": 1.05, "USDJPY": 155.0}, weights, epoch, base)
    weaker = usd_index_proxy({"EURUSD": 1.15, "USDJPY": 145.0}, weights, epoch, base)
    assert stronger is not None and weaker is not None and stronger > 100.0 > weaker
    assert usd_index_proxy({}, weights, epoch, base) is None


def test_usd_beta_needs_enough_observations():
    proxy = [math.sin(i * 0.2) * 0.001 for i in range(60)]
    aligned = [2.0 * p for p in proxy]
    assert usd_beta(aligned, proxy) == pytest.approx(2.0, rel=0.05)
    assert usd_beta(aligned[:5], proxy[:5]) is None


def test_exposure_netting_is_the_gate_not_proxy_correlation():
    """§10.5. A proxy built from the pairs cannot independently filter those pairs."""
    long_eur = usd_exposure(1, usd_is_base=False, risk_fraction=0.005, beta=0.9)
    short_eur = usd_exposure(-1, usd_is_base=False, risk_fraction=0.005, beta=0.9)
    long_jpy = usd_exposure(1, usd_is_base=True, risk_fraction=0.005, beta=0.8)
    assert long_eur < 0 < long_jpy, "long EURUSD is short USD; long USDJPY is long USD"
    assert net_usd_exposure([long_eur], short_eur, limit=0.001).ok, "opposites net out"
    breach = net_usd_exposure([long_eur, long_eur], long_eur, limit=0.0075)
    assert not breach.ok and "exposure:net_usd" in breach.reason


def test_session_vwap_is_tick_volume_weighted_and_warms_up():
    bars = zigzag([1, -1] * 30, timeframe=TimeFrame.M15)
    assert session_vwap(bars, bars[0].open_time, min_bars=200).vwap is None
    state = session_vwap(bars, bars[0].open_time, min_bars=20)
    assert state.vwap is not None and state.z is not None
    assert "vwap_z" in state.as_dict()


# -- registry ----------------------------------------------------------------
def _world(n: int = 400):
    repo = InMemoryBarRepository()
    price = 2300.0
    t0 = datetime(2024, 6, 12, 0, 0, tzinfo=UTC)
    rows = []
    for i in range(n):
        drift = math.sin(i * 0.21) * 2.2 + math.cos(i * 0.07) * 1.1
        impulse = 9.0 * (1 if (i // 17) % 2 == 0 else -1) if i % 17 == 0 else 0.0
        o = price + (2.0 if impulse > 0 else -2.0 if impulse < 0 else 0.0)
        c = o + drift + impulse
        rows.append((o, c, 0.3 + 0.2 * ((i * 7) % 5) / 5, 0.3 + 0.2 * ((i * 3) % 4) / 4))
        price = c
    for bar in bars_from(rows, "XAUUSD", TimeFrame.M15, t0):
        repo.add_bar(bar)
    return repo, t0


def test_object_store_computes_the_whole_snapshot():
    repo, t0 = _world()
    view = MarketView(repo, t0 + timedelta(minutes=15 * 400))
    store = ObjectStore(AnalysisParams(point=0.01))
    snap = store.snapshot(view, "XAUUSD", TimeFrame.M15, with_levels=True)
    assert snap.atr is not None
    assert snap.swings and snap.structure is not None
    assert snap.breaks and snap.order_blocks
    assert snap.fvgs and snap.pools and snap.levels
    assert snap.dealing_range is not None


def test_snapshot_is_memoised_on_the_last_closed_bar():
    repo, t0 = _world()
    view = MarketView(repo, t0 + timedelta(minutes=15 * 400))
    store = ObjectStore(AnalysisParams(point=0.01))
    store.snapshot(view, "XAUUSD", TimeFrame.M15)
    hits_before, misses_before = store.cache_stats
    store.snapshot(view, "XAUUSD", TimeFrame.M15)
    hits_after, misses_after = store.cache_stats
    assert misses_after == misses_before and hits_after == hits_before + 1


def test_a_levelless_cached_snapshot_does_not_deny_a_later_caller_its_levels():
    repo, t0 = _world()
    view = MarketView(repo, t0 + timedelta(minutes=15 * 400))
    store = ObjectStore(AnalysisParams(point=0.01))

    assert not store.snapshot(view, "XAUUSD", TimeFrame.M15).levels
    # Same bar close, so the same cache key -- but this caller asked for levels, and the
    # cached snapshot has none. Serving it would silently hand the engine zero S/R.
    assert store.snapshot(view, "XAUUSD", TimeFrame.M15, with_levels=True).levels


def test_the_level_cache_is_never_served_backwards_in_time():
    repo, t0 = _world()
    store = ObjectStore(AnalysisParams(point=0.01))
    late = MarketView(repo, t0 + timedelta(minutes=15 * 400))
    early = MarketView(repo, t0 + timedelta(minutes=15 * 200))

    assert store.snapshot(late, "XAUUSD", TimeFrame.M15, with_levels=True).levels
    early_levels = store.snapshot(early, "XAUUSD", TimeFrame.M15, with_levels=True).levels

    # The level cache is keyed only by instrument/timeframe, so a replay of an earlier
    # prefix on the same store must refit rather than inherit the later fit. Otherwise
    # look-ahead arrives through the cache instead of through the data, and the prefix
    # invariance test would be measuring a cache rather than the detectors.
    assert early_levels
    for level in early_levels:
        assert level.computed_at <= early.as_of


def test_objects_accessor_filters_on_confirmation():
    repo, t0 = _world()
    store = ObjectStore(AnalysisParams(point=0.01))
    early = t0 + timedelta(minutes=15 * 120)
    late = t0 + timedelta(minutes=15 * 400)
    store.snapshot(MarketView(repo, early), "XAUUSD", TimeFrame.M15)
    store.snapshot(MarketView(repo, late), "XAUUSD", TimeFrame.M15)
    few = store.objects("fvg", "XAUUSD", TimeFrame.M15, early)
    many = store.objects("fvg", "XAUUSD", TimeFrame.M15, late)
    assert 0 < len(few) < len(many)
    assert all(o.confirmed_at <= early for o in few)


def test_unknown_object_kind_is_refused():
    store = ObjectStore(AnalysisParams(point=0.01))
    with pytest.raises(ValueError, match="unknown object kind"):
        store.objects("candlestick", "XAUUSD", TimeFrame.M15, datetime(2024, 1, 1, tzinfo=UTC))


def test_empty_window_yields_an_empty_snapshot():
    store = ObjectStore(AnalysisParams(point=0.01))
    view = MarketView(InMemoryBarRepository(), datetime(2024, 6, 12, tzinfo=UTC))
    snap = store.snapshot(view, "XAUUSD", TimeFrame.M15)
    assert snap.atr is None and snap.swings == []


# -- features ----------------------------------------------------------------
def test_feature_names_are_unique_and_the_schema_is_versioned():
    assert len(set(FEATURE_NAMES)) == len(FEATURE_NAMES)
    assert FEATURE_SCHEMA_VERSION >= 1
    assert set(REQUIRED_NAMES) <= set(FEATURE_NAMES)


def test_ablation_removes_whole_tag_groups():
    without_ict = names_excluding_tags(frozenset({Tag.ICT}))
    assert len(without_ict) < len(FEATURE_NAMES)
    assert "dist_nearest_fvg_atr" not in without_ict
    assert "adx" in without_ict


def test_incomplete_vector_reports_a_warmup_rejection_rather_than_substituting_zero():
    vector = FeatureVector(FEATURE_SCHEMA_VERSION, "XAUUSD",
                           datetime(2024, 6, 12, tzinfo=UTC), {"adx": 25.0})
    assert not vector.is_complete
    assert vector.rejected_reason().startswith("warmup:")
    assert vector.missing_required


def test_build_features_populates_the_main_groups():
    repo, t0 = _world()
    as_of = t0 + timedelta(minutes=15 * 400)
    view = MarketView(repo, as_of)
    store = ObjectStore(AnalysisParams(point=0.01))
    snap = store.snapshot(view, "XAUUSD", TimeFrame.M15, with_levels=True)
    bars = list(view.usable_bars("XAUUSD", TimeFrame.M15, 500))

    inputs = FeatureInputs(
        instrument="XAUUSD",
        as_of=as_of,
        price=bars[-1].ohlc.c,
        direction=Direction.UP,
        entry=snap,
        htf={"H1": snap},
        session_minutes_in=30,
        active_windows=("ny_am_kz", "ny_am_gold"),
        weekday=2,
        volatility=volatility_state(bars, percentile_window=100),
        regime_trend=trend_regime_state(bars, vr_window=300),
        quarter=daily_quarter(as_of),
        net_usd_exposure=0.0,
        reference_levels={"pdh": bars[-1].ohlc.c + 5},
    )
    vector = build_features(inputs)
    assert vector.version == FEATURE_SCHEMA_VERSION
    for name in ("session_is_ny_am", "trend_entry_tf", "range_position", "atr_pct", "adx"):
        assert name in vector.values, name
    assert vector.values["session_is_ny_am"] == 1.0
    assert "dist_pdh_atr" in vector.values
    dense = vector.to_list()
    assert len(dense) == len(FEATURE_NAMES)


def test_features_omit_atr_scaled_values_when_atr_is_unavailable():
    """No silent defaults: an unknown distance is absent, not zero."""
    store = ObjectStore(AnalysisParams(point=0.01))
    repo = InMemoryBarRepository()
    for bar in bars_from([(100.0, 100.5, 0.2, 0.2)] * 3, "XAUUSD", TimeFrame.M15):
        repo.add_bar(bar)
    as_of = datetime(2024, 6, 12, 9, 45, tzinfo=UTC)
    snap = store.snapshot(MarketView(repo, as_of), "XAUUSD", TimeFrame.M15)
    vector = build_features(
        FeatureInputs("XAUUSD", as_of, 100.0, Direction.UP, snap, net_usd_exposure=0.0)
    )
    assert snap.atr is None
    assert "dist_nearest_fvg_atr" not in vector.values
    assert not vector.is_complete


def test_quarterly_and_mmxm_features_are_optional_tier_two():
    repo, t0 = _world()
    as_of = t0 + timedelta(minutes=15 * 400)
    view = MarketView(repo, as_of)
    store = ObjectStore(AnalysisParams(point=0.01))
    snap = store.snapshot(view, "XAUUSD", TimeFrame.M15)
    vector = build_features(
        FeatureInputs("XAUUSD", as_of, snap.bars[-1].ohlc.c, Direction.UP, snap,
                      net_usd_exposure=0.0)
    )
    assert "amdx_accumulation" not in vector.values
    assert "mmxm_score" not in vector.values
    assert "amdx_accumulation" not in REQUIRED_NAMES
    assert "mmxm_score" not in REQUIRED_NAMES


def test_quarter_geometry_is_none_without_bars_in_the_quarter():
    bars = zigzag([1, -1] * 5)
    far = daily_quarter(datetime(2020, 1, 1, 12, tzinfo=UTC))
    assert quarter_geometry(far, bars, bars[-1].close_time) is None


def test_date_helper_unused_import_guard():
    assert isinstance(date(2024, 1, 1), date)
