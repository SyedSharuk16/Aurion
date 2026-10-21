"""The one place an `EvaluationContext` is built.

`docs/01-architecture.md` §4 claims live and backtest differ only in the clock, the bar
source and the signal sink. That claim was false in the context: the backtest runner set
`silver_bullet_window_start` and no macro values, the live app set macro values and no
silver bullet window. So the backtester measured a two-entry-model system with no macro
conditioners while the live process ran a one-model system with them -- a validated model
the live system could not reproduce, in both directions.

Two builders for the same object will always drift. There is now one, and both callers pass
only what genuinely differs between them (the macro cache is live-only until the backtest
replays ALFRED vintages; everything else is derived here).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from aurion.analysis.regime.correlation import session_vwap
from aurion.analysis.regime.trend import trend_regime_state
from aurion.analysis.regime.volatility import volatility_state
from aurion.analysis.registry import AnalysisSnapshot, ObjectStore
from aurion.core.marketview import MarketView
from aurion.core.timeframe import UTC, TimeFrame
from aurion.sessions.windows import WindowSet
from aurion.signals.engine import EvaluationContext

#: Both paths must use the same window and the same higher timeframes or they are not one
#: code path, whatever the clock and bar source are. They were separately hard-coded as 400
#: and (H1, H4, D1, W1) in the runner config and in the live bar handler -- identical today,
#: free to diverge on the next edit.
DEFAULT_ANALYSIS_WINDOW = 400
DEFAULT_HTF_TIMEFRAMES: tuple[TimeFrame, ...] = (
    TimeFrame.H1,
    TimeFrame.H4,
    TimeFrame.D1,
    TimeFrame.W1,
)

#: Any window whose configured name contains this is a Silver Bullet window. Matching on the
#: name keeps `silver_bullet_a/b/c` configurable without a second list to keep in step.
SILVER_BULLET_PREFIX = "silver_bullet"


def silver_bullet_start(
    windows: WindowSet | None, as_of: datetime, instrument: str
) -> datetime | None:
    """Start of the Silver Bullet occurrence containing ``as_of``, if any.

    Whitelist-aware on purpose: `active_for` rather than `active`. A proposal in a window
    the instrument is not whitelisted for is killed by gate G1 a few steps later, so
    generating it only inflates the entry-model count in the funnel with candidates that
    could never emit. The gate remains the authority; this just stops asking it a question
    whose answer is already known.
    """
    if windows is None:
        return None
    for window in windows.active_for(as_of, instrument):
        if SILVER_BULLET_PREFIX in window.name:
            occurrence = window.current_occurrence(as_of)
            if occurrence is not None:
                return occurrence[0]
    return None


def build_context(
    view: MarketView,
    instrument: str,
    as_of: datetime,
    store: ObjectStore,
    *,
    entry_timeframe: TimeFrame,
    htf_timeframes: Sequence[TimeFrame],
    analysis_window: int,
    windows: WindowSet | None = None,
    macro: dict[str, float] | None = None,
    reference_levels: dict[str, float] | None = None,
    pivot_levels: dict[str, float] | None = None,
    round_tier: float | None = None,
    active_usd_exposure: Sequence[float] = (),
    usd_beta: float | None = None,
) -> EvaluationContext:
    """Everything the engine does not compute itself, derived from one `MarketView`.

    The spread comes from the last closed M1 bar rather than a live quote, in both paths. A
    live quote is observed after the bar closed and a backtest cannot reproduce it, so using
    one live would put a value in the feature vector that no backtest row can contain.
    """
    htf: dict[str, AnalysisSnapshot] = {
        str(tf): store.snapshot(view, instrument, tf, analysis_window) for tf in htf_timeframes
    }
    entry_bars = list(view.usable_bars(instrument, entry_timeframe, analysis_window))
    m1 = view.m1(instrument, 1)
    return EvaluationContext(
        entry_timeframe=entry_timeframe,
        htf=htf,
        volatility=volatility_state(entry_bars) if entry_bars else None,
        regime_trend=trend_regime_state(entry_bars) if entry_bars else None,
        vwap=session_vwap(entry_bars, entry_bars[0].open_time) if entry_bars else None,
        macro=dict(macro or {}),
        reference_levels=dict(reference_levels or {}),
        pivot_levels=dict(pivot_levels or {}),
        round_tier=round_tier,
        active_usd_exposure=active_usd_exposure,
        usd_beta=usd_beta,
        silver_bullet_window_start=silver_bullet_start(windows, as_of, instrument),
        spread=m1[-1].spread_close if m1 else None,
        weekday=as_of.astimezone(UTC).weekday(),
    )
