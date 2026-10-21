"""The shared `EvaluationContext` builder: the live/backtest parity boundary.

`docs/01-architecture.md` §4 says live and backtest differ only in the clock, the bar source
and the signal sink. These tests exist because that was false: two separate context builders
had drifted, so the backtest measured two entry models with no macro conditioners while the
live process ran one entry model with them.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from aurion.analysis.registry import AnalysisParams, ObjectStore
from aurion.core.marketview import MarketView
from aurion.core.timeframe import UTC, TimeFrame
from aurion.data.repository import InMemoryBarRepository
from aurion.sessions.gates import build_window_set
from aurion.signals.context import (
    DEFAULT_ANALYSIS_WINDOW,
    DEFAULT_HTF_TIMEFRAMES,
    build_context,
    silver_bullet_start,
)
from tests.conftest import make_m1

# June, so America/New_York is UTC-4: Silver Bullet A (03:00-04:00 NY) is 07:00-08:00 UTC
# and B (10:00-11:00 NY) is 14:00-15:00 UTC.
IN_SB_A = datetime(2024, 6, 12, 7, 30, tzinfo=UTC)
IN_SB_B = datetime(2024, 6, 12, 14, 30, tzinfo=UTC)
OUTSIDE = datetime(2024, 6, 12, 12, 30, tzinfo=UTC)


def _view(as_of: datetime) -> MarketView:
    repo = InMemoryBarRepository()
    for i in range(5):
        repo.add_m1(make_m1("XAUUSD", as_of - timedelta(minutes=5 - i), 2300.0 + i, spread=0.2))
    return MarketView(repo, as_of)


def _context(as_of: datetime, **kwargs):
    return build_context(
        _view(as_of),
        "XAUUSD",
        as_of,
        ObjectStore(AnalysisParams(point=0.01)),
        entry_timeframe=TimeFrame.M15,
        htf_timeframes=DEFAULT_HTF_TIMEFRAMES,
        analysis_window=DEFAULT_ANALYSIS_WINDOW,
        **kwargs,
    )


def test_the_silver_bullet_window_is_resolved_inside_it_and_not_outside(cfg):
    windows = build_window_set(cfg)
    assert silver_bullet_start(windows, IN_SB_B, "XAUUSD") == datetime(
        2024, 6, 12, 14, 0, tzinfo=UTC
    )
    assert silver_bullet_start(windows, OUTSIDE, "XAUUSD") is None
    assert silver_bullet_start(None, IN_SB_B, "XAUUSD") is None


def test_the_window_lookup_respects_the_instrument_whitelist(cfg):
    """XAUUSD is whitelisted for silver_bullet_b only (config.yaml).

    Proposing inside A would only produce candidates that gate G1 kills a few steps later,
    inflating the entry-model count in the funnel with setups that could never emit.
    """
    windows = build_window_set(cfg)
    assert "silver_bullet_b" in cfg.instruments["XAUUSD"].sessions
    assert "silver_bullet_a" not in cfg.instruments["XAUUSD"].sessions
    assert silver_bullet_start(windows, IN_SB_A, "XAUUSD") is None
    assert silver_bullet_start(windows, IN_SB_B, "XAUUSD") is not None


def test_the_context_carries_the_window_that_the_live_path_used_to_omit(cfg):
    """Without this field the SILVER_BULLET_FVG model cannot fire. The live app never set
    it, so the backtest validated a two-model design the live process could not run."""
    assert _context(IN_SB_B, windows=build_window_set(cfg)).silver_bullet_window_start == (
        datetime(2024, 6, 12, 14, 0, tzinfo=UTC)
    )
    assert _context(OUTSIDE, windows=build_window_set(cfg)).silver_bullet_window_start is None


def test_the_spread_comes_from_the_last_closed_bar_in_both_paths():
    """A live quote is observed after the bar closed and no backtest row can contain it."""
    context = _context(IN_SB_B)
    assert context.spread == pytest.approx(0.2)
    assert context.weekday == 2  # 2024-06-12 is a Wednesday


def test_macro_values_are_passed_through_not_invented():
    assert _context(IN_SB_B).macro == {}
    assert _context(IN_SB_B, macro={"DGS10": 4.25}).macro == {"DGS10": 4.25}


def test_only_the_shared_builder_constructs_an_evaluation_context():
    """A structural test, like the adapter's no-order-methods test.

    Two builders for one object drift; this one failed in production and in the backtester in
    opposite directions. A second construction site is the defect, so it fails here rather
    than silently in a year's results.
    """
    root = Path(__file__).resolve().parents[2] / "aurion"
    offenders = [
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "EvaluationContext(" in path.read_text(encoding="utf-8")
        and path.name != "context.py"
    ]
    assert offenders == [], f"construct the context via build_context, not inline: {offenders}"
