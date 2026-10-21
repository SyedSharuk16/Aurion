"""The four causality tests. `docs/01-architecture.md` §3.3.

The brief specified one test -- "shift the data by one bar and assert identical signals" --
which is false as stated: shifting the input by one bar must shift the output by one bar.
Four distinct properties are needed and they catch different bugs.

| Test | Property | Bug class |
|------|----------|-----------|
| T1   | prefix invariance | look-ahead: any use of future bars in a decision |
| T2   | future nulling    | vectorised ops that silently span the array |
| T3   | time-translation  | absolute-index or absolute-date leakage |
| T4   | resample boundary | future-leaking resampling (left-labelled bars) |

T1 is applied twice: to the admissibility gate, and to the full signal-engine cascade --
the bias, the HTF arrays, the entry models, the geometry, the features and the decision
rule. The gate version alone covers one decision out of nine.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from aurion.analysis.base import Direction, TrendState
from aurion.analysis.ict.fvg import FVG
from aurion.analysis.primitives import atr, atr_series
from aurion.analysis.regime.correlation import session_vwap
from aurion.analysis.regime.trend import trend_regime_state
from aurion.analysis.regime.volatility import volatility_state
from aurion.analysis.registry import AnalysisSnapshot, ObjectStore
from aurion.analysis.structure.trend import Structure
from aurion.core.marketview import MarketView
from aurion.core.timeframe import UTC, TimeFrame
from aurion.data.repository import InMemoryBarRepository
from aurion.data.resample import ResamplerSet
from aurion.data.types import OHLC, Bar
from aurion.sessions.calendar import StaticCalendarProvider
from aurion.sessions.gates import AdmissibilityGate, GateResult, build_window_set
from aurion.sessions.spread import SpreadTracker
from aurion.signals.engine import (
    EngineParams,
    EvaluationContext,
    InstrumentMeta,
    SignalEngine,
    Step,
)
from aurion.signals.model import ConstantProbabilityModel
from tests.conftest import make_m1, zigzag
from tests.unit.test_signals import CASCADE_PARAMS, MSS_DELTAS

NY = ZoneInfo("America/New_York")


def pseudo_series(instrument: str, start: datetime, n: int, seed: float = 2300.0):
    """Deterministic non-monotonic price path. Deterministic so failures are reproducible."""
    out = []
    price = seed
    for i in range(n):
        price += math.sin(i * 0.37) * 0.9 + math.cos(i * 0.11) * 0.4
        out.append(
            make_m1(instrument, start + timedelta(minutes=i), price,
                    high_off=0.8, low_off=0.6, close_off=0.2, spread=0.20)
        )
    return out


def build_world(candles, timeframes=(TimeFrame.M5, TimeFrame.M15, TimeFrame.H1)):
    repo = InMemoryBarRepository()
    rs = ResamplerSet(candles[0].instrument, timeframes)
    bars = []
    for c in candles:
        repo.add_m1(c)
        for bar in rs.push(c):
            repo.add_bar(bar)
            bars.append(bar)
    return repo, rs, bars


# -- T4 ----------------------------------------------------------------------
def test_t4_every_bar_confirms_exactly_at_its_close():
    """A left-labelled bar treated as known at its label is the classic leak."""
    candles = pseudo_series("XAUUSD", datetime(2024, 6, 12, 9, tzinfo=UTC), 300)
    _, _, bars = build_world(candles)
    assert bars
    for bar in bars:
        assert bar.confirmed_at == bar.close_time
        assert bar.open_time < bar.close_time


def test_t4_no_bar_is_emitted_before_its_last_constituent_closes():
    candles = pseudo_series("XAUUSD", datetime(2024, 6, 12, 9, tzinfo=UTC), 120)
    rs = ResamplerSet("XAUUSD", (TimeFrame.M5, TimeFrame.M15))
    for candle in candles:
        for bar in rs.push(candle):
            # The emitting candle belongs to the NEXT bucket, so the closed bar's own
            # close_time cannot be later than the candle that triggered it.
            assert bar.close_time <= candle.open_time
            assert bar.close_time <= candle.close_time


def test_t4_partial_bars_never_reach_the_closed_series():
    candles = pseudo_series("XAUUSD", datetime(2024, 6, 12, 9, tzinfo=UTC), 47)
    repo, rs, _ = build_world(candles, (TimeFrame.M15,))
    partial = rs.partial(TimeFrame.M15)
    assert partial is not None
    as_of = candles[-1].close_time
    closed = repo.bars("XAUUSD", TimeFrame.M15, 100, as_of)
    assert all(b.open_time != partial.open_time for b in closed)


# -- T2 ----------------------------------------------------------------------
GARBAGE = OHLC(9_999_999.0, 9_999_999.0, 9_999_999.0, 9_999_999.0)


def test_t2_future_nulling_leaves_atr_unchanged():
    """Replace every bar after t with adversarial values; ATR at t must not move."""
    candles = pseudo_series("XAUUSD", datetime(2024, 6, 12, 9, tzinfo=UTC), 400)
    _, _, bars = build_world(candles, (TimeFrame.M5,))
    m5 = [b for b in bars if b.timeframe is TimeFrame.M5]
    assert len(m5) > 40

    for cut in (20, 35, 50):
        clean = atr(m5[:cut], period=14)
        poisoned = list(m5[:cut]) + [
            Bar(b.instrument, b.timeframe, b.open_time, b.close_time, GARBAGE,
                10**9, b.constituents)
            for b in m5[cut:]
        ]
        assert atr(poisoned[:cut], period=14) == clean
        assert atr_series(poisoned, 14)[cut - 1] == atr_series(m5, 14)[cut - 1]


def test_t2_marketview_cannot_reach_poisoned_future_bars():
    candles = pseudo_series("XAUUSD", datetime(2024, 6, 12, 9, tzinfo=UTC), 200)
    repo, _, bars = build_world(candles, (TimeFrame.M5,))
    m5 = [b for b in bars if b.timeframe is TimeFrame.M5]
    cut_time = m5[10].close_time

    poisoned_repo = InMemoryBarRepository()
    for b in m5:
        poisoned_repo.add_bar(
            b if b.close_time <= cut_time
            else Bar(b.instrument, b.timeframe, b.open_time, b.close_time, GARBAGE, 10**9, 5)
        )

    clean_view = MarketView(repo, cut_time)
    poisoned_view = MarketView(poisoned_repo, cut_time)
    assert list(clean_view.bars("XAUUSD", TimeFrame.M5, 50)) == list(
        poisoned_view.bars("XAUUSD", TimeFrame.M5, 50)
    )
    assert atr(clean_view.usable_bars("XAUUSD", TimeFrame.M5, 20), 14) == atr(
        poisoned_view.usable_bars("XAUUSD", TimeFrame.M5, 20), 14
    )


# -- T3 ----------------------------------------------------------------------
@pytest.mark.parametrize("days", [1, 7])
def test_t3_shifting_the_series_shifts_every_bar_by_exactly_the_same_amount(days):
    """Equivariance, not invariance.

    The brief asked for identical signals after a shift, which cannot hold. The correct
    property is that output shifts with input -- which catches dependence on absolute
    dates or on position within the dataset. The shift is whole days inside one DST regime
    so session anchoring is preserved.
    """
    base_start = datetime(2024, 6, 10, 9, tzinfo=UTC)
    shift = timedelta(days=days)

    _, _, bars_a = build_world(pseudo_series("XAUUSD", base_start, 600))
    _, _, bars_b = build_world(pseudo_series("XAUUSD", base_start + shift, 600))

    assert len(bars_a) == len(bars_b)
    for a, b in zip(bars_a, bars_b, strict=True):
        assert a.timeframe is b.timeframe
        assert b.open_time == a.open_time + shift
        assert b.close_time == a.close_time + shift
        assert a.ohlc == b.ohlc
        assert a.constituents == b.constituents


def test_t3_atr_is_invariant_to_the_shift_because_it_depends_only_on_price():
    base_start = datetime(2024, 6, 10, 9, tzinfo=UTC)
    _, _, a = build_world(pseudo_series("XAUUSD", base_start, 400), (TimeFrame.M5,))
    _, _, b = build_world(
        pseudo_series("XAUUSD", base_start + timedelta(days=7), 400), (TimeFrame.M5,)
    )
    assert atr(a, 14) == pytest.approx(atr(b, 14))


# -- T1 ----------------------------------------------------------------------
def test_t1_prefix_invariance_of_the_gate_decision(cfg):
    """The real anti-look-ahead test, applied to the only decision that exists in Phase 2.

    For every evaluation instant t, the decision computed from the prefix of the data up to
    t must be byte-identical to the decision computed from the full series. Extends to the
    signal engine unchanged in Phase 5.
    """
    week_open = datetime(2024, 6, 9, 17, 0, tzinfo=NY).astimezone(UTC)
    candles = []
    t = week_open - timedelta(days=21)
    price = 2300.0
    i = 0
    while t < week_open + timedelta(days=3):
        local = t.astimezone(NY)
        closed = (local.weekday() == 4 and local.hour >= 17) or local.weekday() == 5 or (
            local.weekday() == 6 and local.hour < 17
        )
        if not closed:
            price += math.sin(i * 0.31) * 0.7
            candles.append(
                make_m1("XAUUSD", t, price, high_off=0.8, low_off=0.6, close_off=0.2,
                        spread=0.20)
            )
            i += 1
        t += timedelta(minutes=1)

    timeframes = (TimeFrame.M5, TimeFrame.M15, TimeFrame.H1, TimeFrame.D1, TimeFrame.W1)
    full_repo, _, _ = build_world(candles, timeframes)

    def gate_for(evaluation_times):
        st = SpreadTracker(window_obs=5, max_median_mult=cfg.spread.max_median_mult)
        for ev in evaluation_times:
            for weeks_back in range(1, 7):
                st.observe("XAUUSD", ev - timedelta(weeks=weeks_back), 0.20)
        return AdmissibilityGate(
            cfg, build_window_set(cfg), st, StaticCalendarProvider([])
        )

    evaluation_times = [
        (week_open + timedelta(days=d)).astimezone(NY).replace(hour=h, minute=0).astimezone(UTC)
        for d in (1, 2)
        for h in (3, 9, 13, 17)
    ]

    for as_of in evaluation_times:
        prefix_repo, _, _ = build_world(
            [c for c in candles if c.close_time <= as_of], timeframes
        )
        full = gate_for([as_of]).evaluate("XAUUSD", as_of, MarketView(full_repo, as_of), 0.20)
        prefix = gate_for([as_of]).evaluate(
            "XAUUSD", as_of, MarketView(prefix_repo, as_of), 0.20
        )
        assert full.admissible == prefix.admissible, as_of
        assert full.reasons == prefix.reasons, as_of
        assert full.windows == prefix.windows, as_of


def test_t1_prefix_invariance_of_the_signal_engine_decision(cfg):
    """T1 extended to the full cascade, which is what the brief actually asked for.

    For every evaluation instant t, the candidate computed from the prefix of the data up to
    t must be identical to the candidate computed from the full series at t. The gate version
    above covers one decision out of nine; this covers the bias, the HTF arrays, the entry
    models, the geometry, the features and the decision rule -- every place a detector could
    reach past `as_of`.

    Separate `ObjectStore`s on purpose: one store would serve the full-series snapshot back
    from its own cache and the comparison would be with itself.
    """
    bars = zigzag(MSS_DELTAS, timeframe=TimeFrame.M15)
    full_repo = InMemoryBarRepository()
    for bar in bars:
        full_repo.add_bar(bar)

    def decide(repo, as_of):
        store = ObjectStore(CASCADE_PARAMS)
        snapshot = store.snapshot(
            MarketView(repo, as_of), "TEST", TimeFrame.M15, window=500, with_levels=True
        )
        structure = Structure(
            TrendState.BEARISH, as_of, False, False, True, True,
            snapshot.bars[-1].ohlc.h if snapshot.bars else 0.0,
            snapshot.bars[-1].ohlc.l if snapshot.bars else 0.0,
        )
        # A far bullish gap, identical in both runs, so that some instants reach an actual
        # emission: without a target at the R:R floor the cascade always stops at step 5 and
        # the geometry, model and decision steps are never compared.
        target = FVG(
            "TEST", TimeFrame.H1, Direction.UP, 73.0, 74.0, 2.0,
            as_of - timedelta(hours=8), as_of - timedelta(hours=7), middle_index=1,
        )
        htf = AnalysisSnapshot(
            "TEST", TimeFrame.H4, as_of, snapshot.bars, snapshot.atr,
            swings=snapshot.swings, structure=structure, breaks=snapshot.breaks,
            fvgs=[*snapshot.fvgs, target], order_blocks=snapshot.order_blocks,
            pools=snapshot.pools, sweeps=snapshot.sweeps, mss=snapshot.mss,
            dealing_range=snapshot.dealing_range,
        )
        engine = SignalEngine(
            EngineParams(
                require_w1_d1_agreement=False, rr_min=2.0, expected_r_min=-9.0,
                p_floor=0.0, mss_window_bars=20,
            ),
            store,
            _PermissiveGate(),
            {"TEST": InstrumentMeta(0.01, 100.0, "XAU", "USD")},
            ConstantProbabilityModel(p=0.55),
        )
        # The regime states and the reference level are derived from the same prefix, so they
        # are part of what is being compared rather than constants handed to both runs.
        entry_bars = list(snapshot.bars)
        context = EvaluationContext(
            entry_timeframe=TimeFrame.M15,
            htf=dict.fromkeys(("W1", "D1", "H4", "H1"), htf),
            volatility=volatility_state(entry_bars, percentile_window=40) if entry_bars else None,
            regime_trend=trend_regime_state(entry_bars, vr_window=40) if entry_bars else None,
            vwap=session_vwap(entry_bars, entry_bars[0].open_time) if entry_bars else None,
            reference_levels={"pdh": entry_bars[-1].ohlc.c + 5} if entry_bars else {},
            weekday=as_of.weekday(),
            spread=0.2,
        )
        return engine.evaluate(MarketView(repo, as_of), "TEST", as_of, context, 500)

    # Every fifth bar close from the point two confirmed swings exist, to the end.
    instants = [bar.close_time for bar in bars[12::5]]
    assert len(instants) >= 8

    differing = []
    steps = set()
    for as_of in instants:
        prefix_repo = InMemoryBarRepository()
        for bar in bars:
            if bar.close_time <= as_of:
                prefix_repo.add_bar(bar)
        full = decide(full_repo, as_of)
        prefix = decide(prefix_repo, as_of)
        steps.add(full.reached_step)
        if _decision_of(full) != _decision_of(prefix):
            differing.append((as_of, _decision_of(full), _decision_of(prefix)))

    assert not differing, f"the full series changed {len(differing)} decisions: {differing[:3]}"
    # A prefix-invariance test over instants that all stop at the same step proves almost
    # nothing, so the spread of terminal steps is itself asserted -- including one emission,
    # so that the property is verified on a full signal (entry, stop, targets, p_hat,
    # expected_r, id) and not only on rejections.
    assert len(steps) >= 3, f"only reached {sorted(s.name for s in steps)}"
    assert Step.EMITTED in steps, "no instant emitted; the decision rule is untested here"


def _decision_of(candidate):
    """Everything the funnel and the alert are built from."""
    geometry = candidate.geometry
    return (
        candidate.reached_step,
        candidate.rejected_reason,
        candidate.direction,
        candidate.entry_model,
        None if geometry is None else (geometry.entry, geometry.stop_loss),
        None if geometry is None else tuple(t.price for t in geometry.targets),
        candidate.p_hat,
        candidate.expected_r,
        candidate.signal_id if candidate.emitted else None,
    )


class _PermissiveGate:
    """Admissibility has its own T1 test above; here it must not vary with the prefix."""

    def evaluate(self, instrument, as_of, view, spread=None):
        return GateResult(
            admissible=True,
            reasons=(),
            window_label="ny_am_kz",
            windows=("ny_am_kz",),
            details={"minutes_into_week": 600},
        )
