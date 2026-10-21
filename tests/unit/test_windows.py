"""Session windows: membership, midnight crossing, and the documented DST drift."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from aurion.core.timeframe import UTC
from aurion.sessions.gates import build_window_set
from aurion.sessions.windows import SessionWindow, WindowSet

NY = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")


def ny(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=NY).astimezone(UTC)


LKZ = SessionWindow("london_kz", "America/New_York", time(2), time(5), True)
ASIAN = SessionWindow("asian", "America/New_York", time(20), time(0), False)


def test_window_is_half_open():
    assert LKZ.contains(ny(2024, 6, 12, 2, 0))
    assert LKZ.contains(ny(2024, 6, 12, 4, 59))
    assert not LKZ.contains(ny(2024, 6, 12, 5, 0)), "end is exclusive"
    assert not LKZ.contains(ny(2024, 6, 12, 1, 59))


def test_naive_datetime_refused():
    with pytest.raises(ValueError, match="timezone-aware"):
        LKZ.contains(datetime(2024, 6, 12, 3, 0))


def test_midnight_crossing_window_spans_four_hours():
    assert ASIAN.crosses_midnight
    assert not ASIAN.contains(ny(2024, 6, 12, 19, 59))
    assert ASIAN.contains(ny(2024, 6, 12, 20, 0))
    assert ASIAN.contains(ny(2024, 6, 12, 23, 59))
    assert not ASIAN.contains(ny(2024, 6, 13, 0, 0))
    start, end = ASIAN.occurrence(datetime(2024, 6, 12).date())
    assert (end - start).total_seconds() == 4 * 3600


def test_current_occurrence_attributes_the_tail_to_the_opening_day():
    occ = ASIAN.current_occurrence(ny(2024, 6, 12, 22))
    assert occ is not None
    assert occ[0].astimezone(NY).day == 12
    assert ASIAN.current_occurrence(ny(2024, 6, 12, 12)) is None


def test_minutes_into_window():
    assert LKZ.minutes_into(ny(2024, 6, 12, 3, 30)) == 90
    assert LKZ.minutes_into(ny(2024, 6, 12, 12)) is None


def test_weekday_restriction():
    mon_only = SessionWindow("mon", "America/New_York", time(9), time(10), True, (0,))
    assert mon_only.contains(ny(2024, 6, 10, 9, 30))   # Monday
    assert not mon_only.contains(ny(2024, 6, 11, 9, 30))  # Tuesday


def test_ny_anchored_window_drifts_against_london_across_dst():
    """A documented limitation, asserted so it cannot regress into a silent surprise.

    For the ~3 weeks a year when UK and US transitions are out of step, a window named for
    the London session sits an hour away from it. §3.1 keeps the NY anchor because that is
    what the ICT literature means; this test records the cost.
    """
    before = ny(2024, 3, 9, 3).astimezone(LONDON).hour
    after = ny(2024, 3, 11, 3).astimezone(LONDON).hour
    assert before == 8 and after == 7
    assert LKZ.contains(ny(2024, 3, 9, 3)) and LKZ.contains(ny(2024, 3, 11, 3))


def test_whitelist_and_signal_windows(cfg):
    ws = build_window_set(cfg)
    at_10 = ny(2024, 6, 12, 10, 30)
    assert "silver_bullet_b" in {w.name for w in ws.signal_windows_for(at_10, "XAUUSD")}
    assert ws.signal_windows_for(at_10, "USDJPY") == []


def test_asian_window_marks_but_does_not_emit(cfg):
    """USDJPY whitelists the Asian window, but the window itself is marking-only."""
    ws = build_window_set(cfg)
    at_22 = ny(2024, 6, 12, 22)
    assert "asian" in {w.name for w in ws.active_for(at_22, "USDJPY")}
    assert ws.signal_windows_for(at_22, "USDJPY") == []


def test_label_reports_overlapping_windows(cfg):
    ws = build_window_set(cfg)
    label = ws.label(ny(2024, 6, 12, 9), "XAUUSD")
    assert "ny_am_kz" in label and "ny_am_gold" in label


def test_unknown_instrument_sees_every_active_window():
    ws = WindowSet({"lkz": LKZ})
    assert len(ws.active_for(ny(2024, 6, 12, 3), "UNKNOWN")) == 1
