"""Spread conditioning (§3.4, a correction to the brief) and point-in-time calendar (§3.5)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from aurion.core.timeframe import UTC
from aurion.sessions.calendar import (
    CalendarEvent,
    NullCalendarProvider,
    StaticCalendarProvider,
    blackout_for,
    calendar_from_env,
    currencies_of,
    is_template,
    load_calendar_events,
)
from aurion.sessions.spread import SpreadTracker

NY = ZoneInfo("America/New_York")


def ny(day: int, hour: int) -> datetime:
    return datetime(2024, 6, day, hour, tzinfo=NY).astimezone(UTC)


@pytest.fixture
def warmed_tracker() -> SpreadTracker:
    t = SpreadTracker(window_obs=5, max_median_mult=2.5)
    for day in (4, 5, 6, 11, 12):  # Tue, Wed, Thu, Tue, Wed -> one weekday class
        t.observe("XAUUSD", ny(day, 3), 0.60)   # thin session
        t.observe("XAUUSD", ny(day, 9), 0.18)   # liquid session
    return t


def test_the_same_spread_passes_in_a_thin_session_and_fails_in_a_liquid_one(warmed_tracker):
    """The whole point of bucketing. A global rolling median cannot do this."""
    thin = warmed_tracker.check("XAUUSD", ny(13, 3), 0.60)
    liquid = warmed_tracker.check("XAUUSD", ny(13, 9), 0.60)
    assert thin.ok and thin.median == pytest.approx(0.60)
    assert not liquid.ok and liquid.median == pytest.approx(0.18)
    assert "x_median" in liquid.reason


def test_threshold_is_the_configured_multiple(warmed_tracker):
    assert warmed_tracker.check("XAUUSD", ny(13, 9), 0.18 * 2.4).ok
    assert not warmed_tracker.check("XAUUSD", ny(13, 9), 0.18 * 2.6).ok


def test_warmup_fails_closed(warmed_tracker):
    got = warmed_tracker.check("EURUSD", ny(13, 9), 0.00002)
    assert not got.ok and got.reason == "spread:warmup" and got.median is None


def test_warmup_can_fail_open_when_configured():
    t = SpreadTracker(window_obs=5, fail_closed_on_warmup=False)
    assert t.check("EURUSD", ny(13, 9), 0.00002).ok


def test_weekday_classes_warm_independently(warmed_tracker):
    """Monday is its own class, so Tuesday's history does not vouch for it."""
    assert warmed_tracker.check("XAUUSD", ny(10, 9), 0.18).reason == "spread:warmup"


def test_current_observation_is_never_part_of_its_own_median(warmed_tracker):
    before = warmed_tracker.median_for("XAUUSD", ny(13, 9))
    warmed_tracker.check("XAUUSD", ny(13, 9), 99.0)
    assert warmed_tracker.median_for("XAUUSD", ny(13, 9)) == before


def test_negative_spread_is_rejected():
    t = SpreadTracker()
    with pytest.raises(ValueError, match="negative spread"):
        t.observe("EURUSD", ny(13, 9), -0.1)


def test_bucket_minutes_must_divide_the_day():
    with pytest.raises(ValueError, match="divide 1440"):
        SpreadTracker(bucket_minutes=7)


# -- calendar ----------------------------------------------------------------
PUB_EARLY = datetime(2024, 6, 1, tzinfo=UTC)
NFP = CalendarEvent("nfp", datetime(2024, 6, 7, 12, 30, tzinfo=UTC), "USD",
                    "US Non-Farm Payrolls", "high", PUB_EARLY)
CPI = CalendarEvent("cpi", datetime(2024, 6, 12, 12, 30, tzinfo=UTC), "USD",
                    "US Core CPI m/m", "high", datetime(2024, 6, 11, tzinfo=UTC))


def test_point_in_time_hides_events_published_later():
    """Replaying today's calendar over old data leaks retroactively added events."""
    p = StaticCalendarProvider([NFP, CPI])
    window = (datetime(2024, 6, 1, tzinfo=UTC), datetime(2024, 6, 30, tzinfo=UTC))
    early = p.events(*window, datetime(2024, 6, 5, tzinfo=UTC))
    later = p.events(*window, datetime(2024, 6, 11, 12, tzinfo=UTC))
    assert [e.event_id for e in early] == ["nfp"]
    assert [e.event_id for e in later] == ["nfp", "cpi"]


def test_latest_visible_revision_wins():
    r0 = CalendarEvent("x", datetime(2024, 6, 20, 12, tzinfo=UTC), "USD", "Thing", "low",
                       PUB_EARLY, revision=0)
    r1 = CalendarEvent("x", datetime(2024, 6, 20, 14, tzinfo=UTC), "USD", "Thing", "high",
                       datetime(2024, 6, 15, tzinfo=UTC), revision=1)
    p = StaticCalendarProvider([r0, r1])
    window = (datetime(2024, 6, 1, tzinfo=UTC), datetime(2024, 6, 30, tzinfo=UTC))
    assert p.events(*window, datetime(2024, 6, 10, tzinfo=UTC))[0].impact == "low"
    assert p.events(*window, datetime(2024, 6, 16, tzinfo=UTC))[0].impact == "high"


def test_without_point_in_time_every_revision_is_visible():
    p = StaticCalendarProvider([NFP, CPI], supports_point_in_time=False)
    got = p.events(datetime(2024, 6, 1, tzinfo=UTC), datetime(2024, 6, 30, tzinfo=UTC), None)
    assert len(got) == 2


def test_null_provider_reports_unavailable():
    """Which blocks everything under fail_safe=block. Deliberately loud, not convenient."""
    assert not NullCalendarProvider().available()
    assert NullCalendarProvider().supports_point_in_time is False


def test_blackout_longest_title_match_wins():
    overrides = {"CPI": 60, "CORE CPI": 90}
    assert blackout_for(CPI, 30, overrides) == timedelta(minutes=90)
    assert blackout_for(NFP, 30, overrides) == timedelta(minutes=30)
    assert blackout_for(NFP, 30, {"NON-FARM": 45}) == timedelta(minutes=45)


def test_metal_instruments_also_watch_usd_events():
    assert currencies_of("XAUUSD", "XAU", "USD") == frozenset({"XAU", "USD"})
    assert currencies_of("EURUSD", "EUR", "USD") == frozenset({"EUR", "USD"})


# -- calendar loading --------------------------------------------------------
def _event_json(**over):
    base = {
        "event_id": "nfp-2024-06",
        "scheduled_utc": "2024-06-07T12:30:00+00:00",
        "currency": "USD",
        "title": "Non-Farm Payrolls",
        "impact": "high",
        "published_utc": "2024-05-24T00:00:00+00:00",
    }
    return {**base, **over}


def _write(tmp_path, events):
    path = tmp_path / "calendar.json"
    path.write_text(json.dumps(events), encoding="utf-8")
    return path


def test_a_calendar_file_loads_into_a_point_in_time_provider(tmp_path):
    path = _write(tmp_path, [_event_json(), _event_json(event_id="cpi", impact="medium")])
    provider = calendar_from_env(
        {"AURION_CALENDAR_PROVIDER": "file", "AURION_CALENDAR_PATH": str(path)}
    )
    assert provider.available() and provider.supports_point_in_time
    window = (datetime(2024, 6, 1, tzinfo=UTC), datetime(2024, 6, 30, tzinfo=UTC))
    assert len(provider.events(*window)) == 2
    # Published after the evaluation instant: invisible, which is the whole point.
    assert provider.events(*window, as_published_before=datetime(2024, 5, 1, tzinfo=UTC)) == []


@pytest.mark.parametrize(
    "bad",
    [
        {"impact": "catastrophic"},
        {"scheduled_utc": "2024-06-07T12:30:00"},
        {"published_utc": "not-a-date"},
    ],
)
def test_a_malformed_event_names_the_row_rather_than_loading_partially(tmp_path, bad):
    """A naive timestamp is a silent hour-or-more error in a +/-30 minute gate."""
    path = _write(tmp_path, [_event_json(), _event_json(**bad)])
    with pytest.raises(ValueError, match="event 1"):
        load_calendar_events(path)


def test_a_json_object_instead_of_a_list_is_refused(tmp_path):
    path = tmp_path / "calendar.json"
    path.write_text('{"events": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="expected a JSON list"):
        load_calendar_events(path)


def test_no_provider_configured_yields_the_blocking_provider():
    """Which is correct, and is also why nothing could ever emit before this was wired."""
    for env in ({}, {"AURION_CALENDAR_PROVIDER": ""}, {"AURION_CALENDAR_PROVIDER": "none"}):
        provider = calendar_from_env(env)
        assert isinstance(provider, NullCalendarProvider)
        assert not provider.available()


def test_file_without_a_path_is_an_error_not_a_silent_fallback():
    """Falling back to 'none' here would suppress every signal with no stated reason."""
    with pytest.raises(ValueError, match="AURION_CALENDAR_PATH"):
        calendar_from_env({"AURION_CALENDAR_PROVIDER": "file"})


def test_an_unimplemented_provider_name_is_refused():
    with pytest.raises(ValueError, match="not implemented"):
        calendar_from_env({"AURION_CALENDAR_PROVIDER": "tradingeconomics"})


def test_a_missing_calendar_file_names_itself_rather_than_raising_filenotfound(tmp_path):
    """Under `restart: unless-stopped` this is an endless crash-loop, so the message has to
    say what is missing. The container shadowed its own data directory with a named volume
    and this was the exception nobody could read."""
    with pytest.raises(ValueError, match="calendar file not found"):
        load_calendar_events(tmp_path / "absent.json")
    with pytest.raises(ValueError, match="calendar file not found"):
        calendar_from_env(
            {"AURION_CALENDAR_PROVIDER": "file", "AURION_CALENDAR_PATH": str(tmp_path / "no.json")}
        )


def test_a_directory_is_not_a_calendar_file(tmp_path):
    """Docker creates a directory when a bind-mounted file is absent on the host, which is
    the shape this failure actually arrives in."""
    with pytest.raises(ValueError, match="calendar file not found"):
        load_calendar_events(tmp_path)


def test_the_shipped_template_is_recognised_as_one():
    """A file of invented release times is indistinguishable from a real schedule at runtime
    unless something checks, and it blocks the wrong minutes."""
    events = load_calendar_events("data/calendar.example.json")
    assert events, "the template should contain example rows"
    assert is_template(events)


def test_a_real_schedule_is_not_flagged_as_the_template(tmp_path):
    path = _write(tmp_path, [_event_json(title="Non-Farm Payrolls", event_id="nfp-2024-06")])
    assert not is_template(load_calendar_events(path))


def test_the_missing_file_error_names_the_remedy(tmp_path):
    """The default config points at data/calendar.json, which ships nowhere, so this is the
    first error every new deployment hits. A dead end here is a bad first experience."""
    with pytest.raises(ValueError, match=r"calendar\.example\.json data/calendar\.json") as exc:
        load_calendar_events(tmp_path / "absent.json")
    assert "data/README.md" in str(exc.value)
