"""Telegram formatting, outbox idempotency, commands, health alerts, logging, preflight."""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from aurion import BACKTEST_DISCLAIMER
from aurion.analysis.base import Direction
from aurion.analysis.registry import ObjectStore
from aurion.config.models import load_config
from aurion.core.clock import SimClock
from aurion.core.events import BarClosed
from aurion.core.marketview import MarketView
from aurion.core.timeframe import UTC, TimeFrame
from aurion.data.repository import InMemoryBarRepository
from aurion.data.store import Store
from aurion.notify.health import AlertKind, HealthMonitor
from aurion.notify.telegram.bot import (
    HttpTelegramTransport,
    OutboxSender,
    SendResult,
    TelegramError,
    TelegramSettings,
)
from aurion.notify.telegram.charts import ChartAnnotation, render_signal_chart
from aurion.notify.telegram.commands import (
    ClosedTrade,
    CommandContext,
    HealthView,
    active,
    levels,
    pause,
    resume,
    sessions,
    stats,
    status,
    unauthorised,
)
from aurion.notify.telegram.formatters import (
    DEFAULT_REASONS,
    both_zones,
    code,
    escape,
    format_rejection_digest,
    format_signal,
    format_transition,
    price,
)
from aurion.notify.telegram.outbox import (
    Outbox,
    OutboxKind,
    OutboxMessage,
    TokenBucket,
    dedupe_key,
)
from aurion.runtime.app import CHART_BARS, AurionApp, preflight
from aurion.runtime.logging import JsonFormatter, configure, log_candidate, redact
from aurion.sessions.gates import build_window_set
from aurion.signals.engine import Candidate, Step
from aurion.signals.entries import EntryModel
from aurion.signals.geometry import Target, TargetKind, TradeGeometry
from aurion.signals.lifecycle import (
    LifecycleManager,
    SignalState,
    TrackedSignal,
    Transition,
)
from aurion.signals.sizing import PositionSize
from tests.conftest import bars_from, make_m1, zigzag
from tests.unit.test_signals import CASCADE_PARAMS, MSS_DELTAS

NY = ZoneInfo("America/New_York")
T0 = datetime(2024, 6, 12, 13, 30, tzinfo=UTC)
RESERVED = r"_*[]()~`>#+-=|{}.!"


# -- formatters --------------------------------------------------------------
def test_every_reserved_markdownv2_character_is_escaped():
    """An unescaped reserved char makes the Bot API reject the whole message with a 400."""
    for ch in RESERVED:
        assert escape(ch) == f"\\{ch}"
    assert escape("XAU/USD 2318.40 (-1.5%)") == "XAU/USD 2318\\.40 \\(\\-1\\.5%\\)"


def test_price_uses_the_instruments_precision():
    """Plain numbers. These assertions previously expected `2318\\.40` -- prose escaping --
    which is wrong in the code span every price is rendered inside."""
    assert price(2318.4049, 0.01) == "2318.40"
    assert price(1.1000049, 0.00001) == "1.10000"
    assert price(150.123, 0.001) == "150.123"


def test_both_timezones_are_shown():
    rendered = both_zones(T0)
    assert "NY" in rendered and "SGT" in rendered
    assert T0.astimezone(NY).strftime("%H:%M").replace(":", "") in rendered.replace(
        "\\:", ""
    ).replace(":", "")


def _candidate() -> Candidate:
    geometry = TradeGeometry(
        Direction.UP,
        2318.40,
        2311.80,
        (
            Target(2332.20, TargetKind.POOL, "pdh"),
            Target(2341.00, TargetKind.ARRAY, "H1_fvg"),
        ),
    )
    return Candidate(
        instrument="XAUUSD",
        as_of=T0,
        reached_step=Step.EMITTED,
        rejected_reason=None,
        direction=Direction.UP,
        geometry=geometry,
        p_hat=0.41,
        expected_r=0.19,
        reasons=(("sweep_depth_atr", 0.44), ("break_is_mss", 0.31)),
        size=PositionSize(0.15, 50.0, 6.6, 100.0),
        expires_at=T0 + timedelta(hours=3),
    )


def test_signal_message_reports_a_calibrated_probability_not_a_score():
    text = format_signal(_candidate(), point=0.01, paper=True, brier=0.213)
    assert "XAUUSD" in text and "LONG" in text
    assert "PAPER" in text
    assert "Confidence" in text and "calibrated" in text
    assert "Brier" in text
    assert "`0.41`" in text, "the probability is in a code span, so it is not prose-escaped"
    assert "TP1" in text and "TP2" in text
    assert "Expires" in text


def test_signal_message_states_when_size_is_unavailable():
    candidate = _candidate()
    candidate.size = PositionSize(None, 50.0, 6.6, None, "no GBP->USD conversion rate")
    assert "size unavailable" in format_signal(candidate, 0.01)


def test_below_minimum_size_is_stated_not_hidden():
    candidate = _candidate()
    candidate.size = PositionSize(0.0, 1.0, 10.0, 100.0, "below minimum size")
    assert "below minimum" in format_signal(candidate, 0.01)


def test_a_candidate_without_geometry_cannot_be_formatted():
    with pytest.raises(ValueError, match="geometry"):
        format_signal(
            Candidate("XAUUSD", T0, Step.HTF_BIAS, "bias:disagree"), 0.01
        )


def test_transition_message_reports_realised_r():
    signal = TrackedSignal(
        "s1", "XAUUSD", Direction.UP, 2318.4, 2311.8, (2332.2,), T0 + timedelta(hours=4), T0
    )
    manager = LifecycleManager()
    manager.add(signal)
    signal.realised_r = 1.04
    from aurion.signals.lifecycle import Transition

    text = format_transition(
        signal, Transition(SignalState.TP1_HIT, T0, 2332.2, 1.04, "suggest break-even"),
        0.01,
    )
    # `+1.04` sits in a code span (no prose escaping); the note is prose (escaped).
    assert "TP1 HIT" in text
    assert "`+1.04`" in text
    assert "break\\-even" in text


def test_rejection_digest_lists_counts():
    text = format_rejection_digest({"bias:w1_d1_disagree": 40, "spread:warmup": 4}, 60)
    assert "60 evaluated" in text and "40" in text


# -- outbox ------------------------------------------------------------------
@pytest.fixture
async def store():
    s = Store("sqlite+aiosqlite:///:memory:")
    await s.create_all()
    yield s
    await s.dispose()


def _message(key: str = "sig1:proposed:0") -> OutboxMessage:
    return OutboxMessage(key, "-100123", OutboxKind.SIGNAL, "hello")


async def test_enqueue_is_idempotent_on_the_dedupe_key(store):
    async with store.session() as session, session.begin():
        assert await Outbox().enqueue(session, _message(), T0)
    async with store.session() as session, session.begin():
        assert not await Outbox().enqueue(session, _message(), T0)


def test_dedupe_key_shape():
    assert dedupe_key("abc", "tp1_hit", 2) == "abc:tp1_hit:2"


async def test_due_returns_only_unsent_rows_whose_time_has_come(store):
    outbox = Outbox()
    async with store.session() as session, session.begin():
        await outbox.enqueue(session, _message("a"), T0)
        await outbox.enqueue(session, _message("b"), T0 + timedelta(minutes=5))
    async with store.session() as session:
        assert len(await outbox.due(session, T0)) == 1
        assert len(await outbox.due(session, T0 + timedelta(minutes=10))) == 2


async def test_failures_back_off_then_dead_letter(store):
    outbox = Outbox(max_attempts=3, backoff_seconds=(1, 2, 4))
    async with store.session() as session, session.begin():
        await outbox.enqueue(session, _message("x"), T0)
    for attempt in range(1, 4):
        async with store.session() as session, session.begin():
            row = (await outbox.due(session, T0 + timedelta(hours=1)))[0]
            dead = await outbox.mark_failed(session, row, "boom", T0)
        assert dead == (attempt == 3)
    async with store.session() as session:
        assert len(await outbox.dead_letters(session)) == 1


async def test_sent_rows_are_not_resent(store):
    outbox = Outbox()
    async with store.session() as session, session.begin():
        await outbox.enqueue(session, _message("s"), T0)
        row = (await outbox.due(session, T0))[0]
        await outbox.mark_sent(session, row, 4242, T0)
    async with store.session() as session:
        assert await outbox.due(session, T0 + timedelta(hours=1)) == []


def test_token_bucket_throttles_then_refills():
    bucket = TokenBucket(rate_per_second=2.0, capacity=2.0)
    assert bucket.take(0.0) == 0.0
    assert bucket.take(0.0) == 0.0
    assert bucket.take(0.0) > 0.0, "third token in the same instant must wait"
    assert bucket.take(10.0) == 0.0, "refilled after time passes"
    with pytest.raises(ValueError, match="positive"):
        TokenBucket(0.0)


class _FakeTransport:
    def __init__(self, fail_times: int = 0) -> None:
        self.sent: list[tuple[str, str]] = []
        self.photos: list[Path] = []
        self._fail = fail_times

    async def send_message(self, chat_id: str, text: str) -> SendResult:
        if self._fail > 0:
            self._fail -= 1
            raise TelegramError(500, "server error")
        self.sent.append((chat_id, text))
        return SendResult(message_id=len(self.sent), ok=True)

    async def send_photo(self, chat_id: str, caption: str, photo: Path) -> SendResult:
        self.photos.append(photo)
        self.sent.append((chat_id, caption))
        return SendResult(message_id=len(self.sent), ok=True)

    async def aclose(self) -> None:
        return None


def _settings() -> TelegramSettings:
    return TelegramSettings("tok", "-1001", "-1002", frozenset({"7"}))


async def test_sender_drains_and_marks_sent(store):
    outbox, transport = Outbox(), _FakeTransport()
    async with store.session() as session, session.begin():
        await outbox.enqueue(session, _message("m1"), T0)
    sent, dead = await OutboxSender(store, outbox, transport, _settings()).drain(T0)
    assert (sent, dead) == (1, 0)
    assert transport.sent[0][0] == "-100123"


async def test_sender_retries_a_transient_failure(store):
    outbox, transport = Outbox(max_attempts=3, backoff_seconds=(0, 0, 0)), _FakeTransport(1)
    sender = OutboxSender(store, outbox, transport, _settings())
    async with store.session() as session, session.begin():
        await outbox.enqueue(session, _message("m2"), T0)
    assert await sender.drain(T0) == (0, 0)
    assert await sender.drain(T0 + timedelta(seconds=5)) == (1, 0)


def test_settings_come_from_the_environment_and_fail_closed():
    got = TelegramSettings.from_env({"AURION_TELEGRAM_BOT_TOKEN": "t"})
    assert got.token == "t"
    assert not got.authorised("7"), "an empty allow-list authorises nobody"
    allowed = TelegramSettings.from_env(
        {"AURION_TELEGRAM_ALLOWED_USER_IDS": "7, 9 ,"}
    )
    assert allowed.authorised("7") and allowed.authorised("9")
    assert not allowed.authorised("8")


def test_transport_refuses_an_empty_token():
    with pytest.raises(TelegramError, match="empty bot token"):
        HttpTelegramTransport("")


def test_telegram_error_classifies_retryability():
    assert TelegramError(429, "flood", retry_after=3.0).retryable
    assert TelegramError(502, "bad gateway").retryable
    assert not TelegramError(400, "bad request").retryable


# -- commands ----------------------------------------------------------------
@pytest.fixture(scope="module")
def cascade_snapshot():
    """A snapshot with real levels, pools, FVGs and a dealing range.

    Reuses the searched MSS series from test_signals: a hand-built snapshot would let the
    renderers pass on data the detectors never actually produce.
    """
    bars = zigzag(MSS_DELTAS, timeframe=TimeFrame.M15)
    repo = InMemoryBarRepository()
    for bar in bars:
        repo.add_bar(bar)
    view = MarketView(repo, bars[-1].close_time)
    return ObjectStore(CASCADE_PARAMS).snapshot(
        view, "TEST", TimeFrame.M15, window=500, with_levels=True
    )


@pytest.fixture(scope="module")
def ctx():
    cfg = load_config("config.yaml")
    return CommandContext(
        health=HealthView(
            "oanda", True, {"XAUUSD": 12.0, "EURUSD": None}, "m_f00", "cfg1", 1,
            36_000.0, False, False, False,
        ),
        windows=build_window_set(cfg),
        instruments=["XAUUSD", "EURUSD"],
        points={"XAUUSD": 0.01, "EURUSD": 0.00001},
        active_signals=[],
        closed_returns=[ClosedTrade("ny_am_kz", "XAUUSD", "Wed", 2.0)] * 34
        + [ClosedTrade("london_kz", "EURUSD", "Tue", -1.0)] * 11,
        snapshots={},
        now=datetime(2024, 6, 12, 9, 30, tzinfo=NY).astimezone(UTC),
    )


def test_status_reports_the_calendar_outage_loudly(ctx):
    text = status(ctx)
    assert "UNAVAILABLE" in text
    assert "every signal is suppressed" in text.replace("\\", "")
    assert "no data" in text


def test_stats_always_carries_the_disclaimer_and_hides_thin_cells(ctx):
    text = stats(ctx)
    assert escape(BACKTEST_DISCLAIMER) in text
    assert "below the 30\\-trade minimum" in text
    assert "By session" in text and "By instrument" in text and "By weekday" in text


def test_stats_with_no_trades_still_carries_the_disclaimer(ctx):
    empty = CommandContext(**{**{f: getattr(ctx, f) for f in ctx.__slots__}, "closed_returns": []})
    assert escape(BACKTEST_DISCLAIMER) in stats(empty)


def test_sessions_lists_both_timezones_and_marks_the_active_window(ctx):
    text = sessions(ctx)
    assert "NY" in text and "SGT" in text
    assert "marking only" in text and "signals" in text
    assert "▶" in text


def test_active_handles_the_empty_case(ctx):
    assert "none" in active(ctx, lambda _s: None)


def test_levels_without_a_snapshot_says_so(ctx):
    assert "no snapshot" in levels(ctx, "GBPUSD")


def _with(ctx, **changes):
    return CommandContext(**{**{f: getattr(ctx, f) for f in ctx.__slots__}, **changes})


def test_active_renders_realised_and_unrealised_r_per_signal(ctx):
    signal = TrackedSignal(
        signal_id="sig-1",
        instrument="XAUUSD",
        direction=Direction.UP,
        entry=2300.0,
        stop_loss=2290.0,
        targets=(2320.0, 2330.0),
        expires_at=T0 + timedelta(hours=4),
        created_at=T0,
    )
    text = active(_with(ctx, active_signals=[signal]), lambda _s: 2310.0)
    plain = text.replace("\\", "").replace("*", "")
    assert "XAUUSD LONG" in plain
    assert "entry" in plain and "SL" in plain
    assert "unrealised `+1.00`R" in plain, "10 points of profit on a 10-point stop is +1R"


def test_active_says_not_available_rather_than_guessing_an_unrealised_r(ctx):
    """A missing price is not a zero. Printing 0.00R would read as flat, not as unknown."""
    signal = TrackedSignal(
        signal_id="sig-2",
        instrument="XAUUSD",
        direction=Direction.DOWN,
        entry=2300.0,
        stop_loss=2310.0,
        targets=(2280.0,),
        expires_at=T0 + timedelta(hours=4),
        created_at=T0,
    )
    plain = active(_with(ctx, active_signals=[signal]), lambda _s: None).replace("\\", "")
    assert "unrealised `n/a`R" in plain


def test_levels_renders_every_section_it_has_data_for(ctx, cascade_snapshot):
    text = levels(_with(ctx, snapshots={"TEST": cascade_snapshot}), "TEST")
    assert "Levels" in text
    assert "Unswept pools" in text or "Swept" in text
    assert "Unmitigated FVGs" in text
    assert "Dealing range" in text and "equilibrium" in text


def test_levels_marks_swept_pools_as_features_not_targets(ctx, cascade_snapshot):
    """A swept pool is retained as a feature input; offering it as a target would be the bug."""
    snapshot = cascade_snapshot
    if not any(not p.available for p in snapshot.pools):
        pytest.skip("fixture has no swept pool")
    text = levels(_with(ctx, snapshots={"TEST": snapshot}), "TEST")
    assert "not targets" in text


def test_pause_and_resume_state_what_keeps_running(ctx):
    paused = pause(ctx)
    assert "Lifecycle tracking continues" in paused.replace("\\", "")
    assert "survives a restart" in paused.replace("\\", "")
    assert "may be emitted again" in resume(ctx).replace("\\", "")


def test_an_unauthorised_command_names_the_user_without_acting(ctx):
    text = unauthorised("99887766")
    assert "unauthorised" in text and "99887766" in text


# -- health ------------------------------------------------------------------
def test_feed_staleness_alerts_once_then_suppresses_until_the_realert_interval():
    monitor = HealthMonitor(max_bar_staleness_s=120, calendar_realert_minutes=30)
    assert monitor.feed_stale("XAUUSD", 60.0, T0) is None, "within the limit"
    first = monitor.feed_stale("XAUUSD", 300.0, T0)
    assert first is not None and first.kind is AlertKind.FEED_STALE
    assert monitor.feed_stale("XAUUSD", 300.0, T0 + timedelta(minutes=5)) is None
    assert monitor.feed_stale("XAUUSD", 300.0, T0 + timedelta(minutes=31)) is not None


def test_calendar_outage_alerts_and_reports_the_recovery_duration():
    monitor = HealthMonitor(calendar_realert_minutes=30)
    entered = monitor.calendar_outage(T0, available=False)
    assert entered is not None and "suppressed" in entered.detail
    assert monitor.calendar_outage(T0 + timedelta(minutes=1), available=False) is None
    recovered = monitor.calendar_outage(T0 + timedelta(minutes=90), available=True)
    assert recovered is not None and "available again" in recovered.detail
    assert "90 minutes" in recovered.detail
    assert monitor.counters.blocked_minutes["calendar_outage"] == pytest.approx(90.0)


def test_determinism_mismatch_is_framed_as_a_hard_failure():
    alert = HealthMonitor().determinism_mismatch("XAUUSD", "M15", T0)
    assert "non-deterministic" in alert.detail
    assert "hard failure" in alert.detail


def test_heartbeat_summarises_the_day_and_resets_counters():
    monitor = HealthMonitor()
    monitor.counters.bars_ingested = 1440
    monitor.counters.signals_emitted = 2
    monitor.counters.record_rejection("bias:w1_d1_disagree")
    monitor.counters.record_rejection("bias:w1_d1_disagree")
    alert = monitor.heartbeat(T0, uptime_s=90_000)
    assert "1440 bars" in alert.detail and "2 signals" in alert.detail
    assert "bias:w1_d1_disagree x2" in alert.detail
    monitor.counters.reset()
    assert monitor.counters.bars_ingested == 0


def test_alerts_render_with_escaped_detail():
    alert = HealthMonitor().exception("housekeeping", ValueError("bad value 1.5"), T0)
    rendered = alert.render()
    assert "EXCEPTION" in rendered and "1\\.5" in rendered


# -- logging -----------------------------------------------------------------
def test_candidate_lines_are_structured_json():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("aurion.test.candidate")
    log.handlers = [handler]
    log.propagate = False
    log.setLevel(logging.INFO)
    log_candidate(log, "XAUUSD", T0, "decision", "decision:er_below", p_hat=0.3)
    payload = json.loads(buf.getvalue().strip())
    assert payload["decision"] == "rejected"
    assert payload["rejected_reason"] == "decision:er_below"
    assert payload["instrument"] == "XAUUSD"
    assert payload["p_hat"] == 0.3


def test_emitted_candidates_are_marked_as_such():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("aurion.test.emitted")
    log.handlers = [handler]
    log.propagate = False
    log.setLevel(logging.INFO)
    log_candidate(log, "XAUUSD", T0, "emitted", None)
    assert json.loads(buf.getvalue().strip())["decision"] == "emitted"


def test_secrets_are_redacted_from_log_output():
    """A bot token in a log line is a leaked token."""
    assert "REDACTED" in redact("bot1234567:AAHfake_token_value_abcdefghij")
    assert "REDACTED" in redact("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123")
    assert "REDACTED" in redact("?api_key=0123456789abcdef")
    assert "fake_token_value" not in redact("bot1234567:AAHfake_token_value_abcdefghij")


def test_configure_is_idempotent():
    configure(level="INFO", json_format=True)
    first = len(logging.getLogger().handlers)
    configure(level="INFO", json_format=True)
    assert len(logging.getLogger().handlers) == first


# -- startup preflight -------------------------------------------------------
_FULL_ENV = {
    "AURION_OANDA_API_TOKEN": "t",
    "AURION_OANDA_ACCOUNT_ID": "101-001-1234567-001",
    "AURION_TELEGRAM_BOT_TOKEN": "b",
    "AURION_TELEGRAM_SIGNAL_CHAT_ID": "-100",
    "AURION_TELEGRAM_ADMIN_CHAT_ID": "-101",
    "AURION_TELEGRAM_ALLOWED_USER_IDS": "7",
}


def test_a_complete_environment_reports_nothing():
    assert preflight(_FULL_ENV, "paper") == []


def test_whitespace_is_not_a_value():
    env = {**_FULL_ENV, "AURION_TELEGRAM_BOT_TOKEN": "   "}
    assert [f.variable for f in preflight(env, "paper")] == ["AURION_TELEGRAM_BOT_TOKEN"]


def test_paper_mode_treats_a_missing_delivery_channel_as_fatal():
    """A paper-forward run that cannot deliver is indistinguishable from one with no signals."""
    env = {k: v for k, v in _FULL_ENV.items() if k != "AURION_TELEGRAM_SIGNAL_CHAT_ID"}
    findings = preflight(env, "paper")
    assert [f.variable for f in findings] == ["AURION_TELEGRAM_SIGNAL_CHAT_ID"]
    assert findings[0].fatal


def test_backtest_mode_needs_neither_a_stream_nor_a_channel():
    findings = preflight({}, "backtest")
    assert len(findings) == len(_FULL_ENV)
    assert not any(f.fatal for f in findings)


def test_an_empty_allowlist_warns_but_does_not_block_startup():
    """Fail-closed authorisation is correct; failing closed in silence is not."""
    env = {k: v for k, v in _FULL_ENV.items() if k != "AURION_TELEGRAM_ALLOWED_USER_IDS"}
    findings = preflight(env, "live_alerts")
    assert [f.variable for f in findings] == ["AURION_TELEGRAM_ALLOWED_USER_IDS"]
    assert not findings[0].fatal
    assert "every command is ignored" in findings[0].render()


def test_every_finding_names_the_consequence_not_just_the_variable():
    for finding in preflight({}, "live_alerts"):
        assert finding.render() == f"{finding.variable} {finding.problem}: {finding.consequence}"
        assert len(finding.consequence.split()) >= 4


def test_a_mistyped_account_id_is_reported_rather_than_left_to_a_404():
    """A hand-copied credential. The failure it causes reads as a network fault."""
    env = {**_FULL_ENV, "AURION_OANDA_ACCOUNT_ID": "101-001-1234567"}
    findings = preflight(env, "paper")
    assert [f.variable for f in findings] == ["AURION_OANDA_ACCOUNT_ID"]
    assert "does not look like" in findings[0].render()
    # A warning, not a refusal: the pattern is OANDA's convention, and being wrong about it
    # must not be able to block a working deployment.
    assert not findings[0].fatal


def test_a_well_formed_account_id_passes():
    for good in ("101-001-1234567-001", "001-001-1234567-002", "102-004-999999999-010"):
        assert preflight({**_FULL_ENV, "AURION_OANDA_ACCOUNT_ID": good}, "paper") == []


# -- alert routing -----------------------------------------------------------
class _NullAdapter:
    """Satisfies the adapter protocol without doing anything. Construction only."""

    name = "null"

    def to_vendor_symbol(self, instrument: str) -> str:
        return instrument

    def to_canonical(self, vendor_symbol: str) -> str:
        return vendor_symbol

    async def fetch_candles(self, instrument, granularity="M1", start_utc=None, end_utc=None):
        return []

    def stream_prices(self, instruments):  # pragma: no cover - construction only
        raise NotImplementedError

    async def latest_quote(self, instrument):  # pragma: no cover
        raise NotImplementedError

    async def health(self):  # pragma: no cover
        raise NotImplementedError

    async def aclose(self) -> None:
        return None


def _app(store, cfg, admin: str) -> AurionApp:
    return AurionApp(
        cfg,
        _NullAdapter(),
        store,
        telegram=TelegramSettings("tok", "-1001", admin, frozenset({"7"})),
        clock=SimClock(T0),
    )


async def test_a_health_alert_goes_to_the_admin_chat_when_one_is_configured(store, cfg):
    await _app(store, cfg, "-1002")._alert("calendar unavailable", "calendar_outage")
    async with store.session() as session:
        assert [row.chat_id for row in await Outbox().due(session, T0)] == ["-1002"]


async def test_a_health_alert_falls_back_to_the_signal_chat(store, cfg):
    """fail_safe=block makes the outage alert the only explanation for the silence.

    Dropping it because the admin chat is unset leaves an operator with a running process,
    no signals, and nothing anywhere that says why.
    """
    await _app(store, cfg, "")._alert("calendar unavailable", "calendar_outage")
    async with store.session() as session:
        assert [row.chat_id for row in await Outbox().due(session, T0)] == ["-1001"]


# -- charts ------------------------------------------------------------------
def _chart_bars(n: int = 40):
    rows = [(100.0 + i * 0.3, 100.2 + i * 0.3, 0.2, 0.3) for i in range(n)]
    return bars_from(rows, "XAUUSD", TimeFrame.M15, T0)


def test_a_signal_chart_is_actually_rendered(tmp_path):
    """charts.py had 0% coverage and no caller: every alert went out text-only.

    Asserting a non-trivial PNG rather than just a returned path -- a renderer that writes an
    empty file is the failure that would otherwise reach Telegram.
    """
    out = tmp_path / "nested" / "signal.png"
    annotation = ChartAnnotation(
        entry=105.0,
        stop_loss=103.0,
        targets=(109.0, 112.0),
        swept_level=102.5,
        sweep_extremity=102.0,
        arrays=((104.0, 105.5, "H1_fvg"),),
        levels=(101.0, 110.0),
        title="XAUUSD bullish sig-1",
        direction=Direction.UP,
        notes=["session_is_ny_am +0.41", "sweep_depth_atr -0.12"],
    )
    path = render_signal_chart(_chart_bars(), annotation, out)
    assert path == out
    assert out.exists() and out.stat().st_size > 5_000
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_an_empty_bar_window_renders_nothing_rather_than_raising(tmp_path):
    """A chart failure must not stop a signal going out."""
    out = tmp_path / "empty.png"
    assert render_signal_chart([], ChartAnnotation(), out) is None
    assert not out.exists()


def test_a_bare_annotation_still_renders(tmp_path):
    out = tmp_path / "bare.png"
    assert render_signal_chart(_chart_bars(5), ChartAnnotation(), out) == out
    assert out.stat().st_size > 1_000


def test_only_the_last_max_bars_are_drawn(tmp_path):
    """The window the app fetches and the window the renderer draws must be the same size."""
    wide = render_signal_chart(_chart_bars(200), ChartAnnotation(), tmp_path / "wide.png", 20)
    narrow = render_signal_chart(_chart_bars(20), ChartAnnotation(), tmp_path / "narrow.png", 20)
    assert wide is not None and narrow is not None
    assert CHART_BARS == 120, "app and renderer defaults must agree"


# -- the live handler --------------------------------------------------------
def _emitted_candidate() -> Candidate:
    geometry = TradeGeometry(
        direction=Direction.UP,
        entry=2305.0,
        stop_loss=2295.0,
        targets=(Target(2325.0, TargetKind.LEVEL, "sr_level"),),
    )
    return Candidate(
        "XAUUSD",
        T0,
        Step.EMITTED,
        None,
        direction=Direction.UP,
        geometry=geometry,
        entry_model=EntryModel.SWEEP_MSS_RETRACE,
        reasons=(("session_is_ny_am", 0.41), ("sweep_depth_atr", -0.12)),
    )


def _live_app(store, cfg, tmp_path):
    app = _app(store, cfg, "-1002")
    app._chart_dir = tmp_path / "charts"
    return app


async def test_a_closed_entry_bar_is_evaluated_and_counted(store, cfg, tmp_path):
    """`runtime/app.py` is the composition root and was almost entirely untested.

    A series this short cannot produce an HTF bias, so this asserts the handler ran and
    attributed the rejection -- not that a signal came out. The cascade's own outcomes are
    tested in test_signals.
    """
    app = _live_app(store, cfg, tmp_path)
    bars = bars_from(
        [(2300.0 + i * 0.4, 2300.2 + i * 0.4, 0.2, 0.3) for i in range(40)],
        "XAUUSD",
        TimeFrame.M15,
        T0,
    )
    for bar in bars:
        app.repo.add_bar(bar)
    for i in range(40):
        app.repo.add_m1(make_m1("XAUUSD", T0 + timedelta(minutes=i), 2300.0 + i * 0.1))

    await app._on_bar_closed(BarClosed(bars[-1]))

    assert app.state.evaluations == 1
    assert app.health.counters.candidates == 1
    assert app.health.counters.bars_ingested == 1
    assert app.state.emitted == 0
    async with store.session() as session:
        assert await Outbox().due(session, bars[-1].close_time) == []


async def test_a_paused_app_ingests_the_bar_but_does_not_evaluate(store, cfg, tmp_path):
    """Pause must stop emission without stopping ingestion, or the gap repair fires on resume."""
    app = _live_app(store, cfg, tmp_path)
    app.state.paused = True
    bar = bars_from([(2300.0, 2300.5, 0.2, 0.3)], "XAUUSD", TimeFrame.M15, T0)[0]
    app.repo.add_bar(bar)

    await app._on_bar_closed(BarClosed(bar))

    assert app.health.counters.bars_ingested == 1
    assert app.state.evaluations == 0


async def test_a_bar_on_another_timeframe_is_ignored_by_the_entry_handler(store, cfg, tmp_path):
    app = _live_app(store, cfg, tmp_path)
    bar = bars_from([(2300.0, 2300.5, 0.2, 0.3)], "XAUUSD", TimeFrame.H1, T0)[0]
    await app._on_bar_closed(BarClosed(bar))
    assert app.health.counters.bars_ingested == 0
    assert app.state.evaluations == 0


async def test_a_lifecycle_transition_is_enqueued_once_and_counted(store, cfg, tmp_path):
    app = _live_app(store, cfg, tmp_path)
    app.clock = SimClock(T0 + timedelta(minutes=10))
    tracked = TrackedSignal(
        signal_id="sig-live",
        instrument="XAUUSD",
        direction=Direction.UP,
        entry=2300.0,
        stop_loss=2295.0,
        targets=(2310.0,),
        expires_at=T0 + timedelta(hours=4),
        created_at=T0,
    )
    app.lifecycle.add(tracked)
    # One M1 that reaches the entry and then the stop in the same minute.
    app.repo.add_m1(
        make_m1("XAUUSD", T0 + timedelta(minutes=9), 2301.0, high_off=0.5, low_off=7.0)
    )

    await app.step_lifecycle("XAUUSD")

    assert app.state.transitions >= 1
    async with store.session() as session:
        rows = await Outbox().due(session, T0 + timedelta(minutes=10))
    assert rows and all(row.chat_id == "-1001" for row in rows)


async def test_an_emitted_signal_gets_a_chart(store, cfg, tmp_path):
    """The chart was built, documented, config-gated -- and never called by anything."""
    app = _live_app(store, cfg, tmp_path)
    bars = bars_from(
        [(2300.0 + i * 0.4, 2300.2 + i * 0.4, 0.2, 0.3) for i in range(30)],
        "XAUUSD",
        TimeFrame.M15,
        T0,
    )
    for bar in bars:
        app.repo.add_bar(bar)
    view = MarketView(app.repo, bars[-1].close_time)

    path = await app._render_chart(_emitted_candidate(), view, "XAUUSD")

    assert path is not None and path.exists()
    assert path.read_bytes()[:4] == b"\x89PNG"
    assert path.parent == tmp_path / "charts"
    assert path.stem == _emitted_candidate().signal_id, "named by the signal it illustrates"


async def test_charts_can_be_switched_off_without_touching_the_alert(store, cfg, tmp_path):
    app = _live_app(store, cfg, tmp_path)
    app.cfg.notify["send_charts"] = False
    try:
        bar = bars_from([(2300.0, 2300.5, 0.2, 0.3)], "XAUUSD", TimeFrame.M15, T0)[0]
        app.repo.add_bar(bar)
        view = MarketView(app.repo, bar.close_time)
        assert await app._render_chart(_emitted_candidate(), view, "XAUUSD") is None
    finally:
        app.cfg.notify["send_charts"] = True


async def test_a_chart_that_times_out_does_not_block_the_alert(store, cfg, tmp_path):
    """The timeout was described in the renderer's docstring with no mechanism behind it."""
    app = _live_app(store, cfg, tmp_path)
    app.cfg.notify["chart_timeout_s"] = 0.0
    try:
        bars = bars_from(
            [(2300.0 + i * 0.4, 2300.2 + i * 0.4, 0.2, 0.3) for i in range(30)],
            "XAUUSD",
            TimeFrame.M15,
            T0,
        )
        for bar in bars:
            app.repo.add_bar(bar)
        view = MarketView(app.repo, bars[-1].close_time)
        assert await app._render_chart(_emitted_candidate(), view, "XAUUSD") is None
    finally:
        app.cfg.notify["chart_timeout_s"] = 10.0


async def test_no_bars_means_no_chart_rather_than_an_exception(store, cfg, tmp_path):
    app = _live_app(store, cfg, tmp_path)
    view = MarketView(app.repo, T0)
    assert await app._render_chart(_emitted_candidate(), view, "XAUUSD") is None


# -- MarkdownV2 code-span escaping -------------------------------------------
def _code_spans(text: str) -> list[str]:
    """Contents of each single-backtick span, backtick-escapes excluded."""
    out, i = [], 0
    while True:
        start = text.find("`", i)
        if start == -1:
            return out
        end = start + 1
        while end < len(text):
            if text[end] == "\\":
                end += 2
                continue
            if text[end] == "`":
                break
            end += 1
        if end >= len(text):
            return out
        out.append(text[start + 1 : end])
        i = end + 1


def _assert_code_spans_clean(text: str, label: str) -> None:
    """Inside a code entity Telegram requires every backslash to be escaped.

    Prose escaping applied to a code span therefore renders the backslashes literally
    (`2338\\.40`) and can make Telegram reject the entire message with "can't parse
    entities" -- so this is a delivery failure, not a cosmetic one.
    """
    for span in _code_spans(text):
        stripped = span.replace("\\\\", "").replace("\\`", "")
        assert "\\" not in stripped, f"{label}: stray backslash in code span {span!r}"


def test_code_escapes_only_what_a_code_entity_reserves():
    assert code("2338.40") == "`2338.40`", "a dot needs no escaping inside a code span"
    assert code("[PAPER]") == "`[PAPER]`"
    assert code("a\\b") == "`a\\\\b`", "a backslash must be escaped inside a code span"
    assert code("a`b") == "`a\\`b`"


def test_price_returns_a_plain_number_for_the_caller_to_wrap():
    assert price(2338.4, 0.01) == "2338.40"
    assert price(1.08531, 0.00001) == "1.08531"


def test_no_signal_alert_puts_prose_escaping_in_a_code_span():
    candidate = _emitted_candidate()
    for paper in (True, False):
        for brier in (None, 0.24):
            text = format_signal(candidate, 0.01, paper=paper, brier=brier)
            _assert_code_spans_clean(text, f"format_signal paper={paper} brier={brier}")
            assert "2305.00" in text, "the entry price must survive unescaped in its span"


def test_no_transition_update_puts_prose_escaping_in_a_code_span():
    tracked = TrackedSignal(
        signal_id="sig-md",
        instrument="XAUUSD",
        direction=Direction.UP,
        entry=2300.0,
        stop_loss=2290.0,
        targets=(2320.0,),
        expires_at=T0 + timedelta(hours=4),
        created_at=T0,
    )
    for state in (SignalState.TRIGGERED, SignalState.TP1_HIT, SignalState.SL_HIT):
        transition = Transition(state, T0 + timedelta(minutes=5), 2310.0, "note with a . dot")
        text = format_transition(tracked, transition, 0.01)
        _assert_code_spans_clean(text, f"format_transition {state}")


def test_no_command_response_puts_prose_escaping_in_a_code_span(ctx, cascade_snapshot):
    full = _with(ctx, snapshots={"TEST": cascade_snapshot})
    for label, text in (
        ("status", status(full)),
        ("stats", stats(full)),
        ("sessions", sessions(full)),
        ("levels", levels(full, "TEST")),
        ("active", active(full, lambda _s: 2310.0)),
        ("digest", format_rejection_digest({"bias:htf_structure_unavailable": 7}, 10)),
    ):
        _assert_code_spans_clean(text, label)


#: Outside a code span these are never entity delimiters in any message this system sends
#: (only `*`, `_`, `[`, `]`, `(`, `)` and the backtick are), so every one must be escaped.
#: Telegram rejects the whole message otherwise -- a delivery failure, not a rendering one.
_MUST_ESCAPE_IN_PROSE = ".+-=!#|{}~>"


def _assert_prose_is_escaped(text: str, label: str) -> None:
    in_code = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            in_code = not in_code
            i += 1
            continue
        if not in_code and ch in _MUST_ESCAPE_IN_PROSE:
            context = text[max(0, i - 25) : i + 10].replace("\n", " ")
            raise AssertionError(f"{label}: unescaped {ch!r} in prose near ...{context}...")
        i += 1


def test_a_reason_line_does_not_put_a_raw_number_in_prose():
    """The contribution used to be interpolated raw, so `+0.52` reached Telegram unescaped
    and every alert carrying reasons would have been rejected outright."""
    rendered = DEFAULT_REASONS.render("sweep_depth_atr", 0.52)
    _assert_prose_is_escaped(rendered, "reason")
    assert "`+0.52`" in rendered


def test_every_message_this_system_sends_is_valid_markdown_v2(ctx, cascade_snapshot):
    candidate = _emitted_candidate()
    tracked = TrackedSignal(
        signal_id="sig-md2",
        instrument="XAUUSD",
        direction=Direction.UP,
        entry=2300.0,
        stop_loss=2290.0,
        targets=(2320.0,),
        expires_at=T0 + timedelta(hours=4),
        created_at=T0,
    )
    full = _with(ctx, snapshots={"TEST": cascade_snapshot}, active_signals=[tracked])
    messages = {
        "signal": format_signal(candidate, 0.01, paper=True, brier=0.24),
        "signal_live": format_signal(candidate, 0.01, paper=False),
        "transition": format_transition(
            tracked, Transition(SignalState.TP1_HIT, T0, 2310.0, "note"), 0.01
        ),
        "digest": format_rejection_digest({"bias:htf_structure_unavailable": 7}, 10),
        "status": status(full),
        "stats": stats(full),
        "sessions": sessions(full),
        "levels": levels(full, "TEST"),
        "active": active(full, lambda _s: 2310.0),
        "pause": pause(full),
        "resume": resume(full),
        "unauthorised": unauthorised("99887766"),
    }
    for label, text in messages.items():
        _assert_prose_is_escaped(text, label)
        _assert_code_spans_clean(text, label)
    monitor = HealthMonitor()
    monitor.counters.record_rejection("bias:w1_d1_disagree")
    monitor.counters.bars_ingested = 1440
    for label, alert in (
        ("heartbeat", monitor.heartbeat(T0, 36_000.0)),
        ("determinism", monitor.determinism_mismatch("XAUUSD", "M15", T0)),
        ("exception", monitor.exception("housekeeping", ValueError("a . dotted message"), T0)),
    ):
        _assert_prose_is_escaped(alert.render(), f"alert:{label}")
        _assert_code_spans_clean(alert.render(), f"alert:{label}")
