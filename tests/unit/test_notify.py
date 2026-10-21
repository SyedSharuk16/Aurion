"""Telegram formatting, outbox idempotency, commands, health alerts, logging."""

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
from aurion.config.models import load_config
from aurion.core.timeframe import UTC
from aurion.data.store import Store
from aurion.notify.health import AlertKind, HealthMonitor
from aurion.notify.telegram.bot import (
    HttpTelegramTransport,
    OutboxSender,
    SendResult,
    TelegramError,
    TelegramSettings,
)
from aurion.notify.telegram.commands import (
    ClosedTrade,
    CommandContext,
    HealthView,
    active,
    levels,
    sessions,
    stats,
    status,
)
from aurion.notify.telegram.formatters import (
    both_zones,
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
from aurion.runtime.logging import JsonFormatter, configure, log_candidate, redact
from aurion.sessions.gates import build_window_set
from aurion.signals.engine import Candidate, Step
from aurion.signals.geometry import Target, TargetKind, TradeGeometry
from aurion.signals.lifecycle import LifecycleManager, SignalState, TrackedSignal
from aurion.signals.sizing import PositionSize

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
    assert price(2318.4049, 0.01) == "2318\\.40"
    assert price(1.1000049, 0.00001) == "1\\.10000"
    assert price(150.123, 0.001) == "150\\.123"


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
    assert "0\\.41" in text
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
    assert "TP1 HIT" in text and "1\\.04" in text and "break\\-even" in text


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
