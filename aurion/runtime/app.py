"""Composition root for the always-on runtime. `docs/01-architecture.md` §4, §5, §7.

This is the only module that knows how every piece is wired. `analysis/`, `features/` and
`signals/` receive a `MarketView` and config and contain no branch on mode: live and
backtest differ **only** in the clock, the bar source and the signal sink constructed here.

Shutdown is graceful by design: stop accepting events, flush the outbox, checkpoint the
adapter cursors. A SIGTERM that drops queued Telegram messages would lose the alerts the
system exists to produce.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import signal
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from aurion.analysis.registry import AnalysisParams, ObjectStore, params_from_config
from aurion.config.models import AurionConfig, load_config
from aurion.core.bus import EventBus
from aurion.core.clock import Clock, WallClock
from aurion.core.events import AdapterStateChanged, BarClosed, FeedStale
from aurion.core.marketview import MarketView
from aurion.core.timeframe import UTC, TimeFrame
from aurion.data.adapters.base import DataAdapter
from aurion.data.adapters.oanda import OandaAdapter
from aurion.data.macro import MacroCache
from aurion.data.repository import InMemoryBarRepository
from aurion.data.store import Store
from aurion.data.supervisor import IngestSupervisor
from aurion.notify.health import AlertKind, HealthMonitor
from aurion.notify.telegram.bot import (
    HttpTelegramTransport,
    OutboxSender,
    TelegramSettings,
    TelegramTransport,
)
from aurion.notify.telegram.charts import ChartAnnotation, render_signal_chart
from aurion.notify.telegram.formatters import (
    format_signal,
    format_signal_compact,
    format_transition,
)
from aurion.notify.telegram.outbox import Outbox, OutboxKind, OutboxMessage, dedupe_key
from aurion.runtime.env import load_dotenv
from aurion.runtime.logging import configure, log_candidate
from aurion.sessions.calendar import (
    CalendarProvider,
    NullCalendarProvider,
    StaticCalendarProvider,
    calendar_from_env,
    is_template,
)
from aurion.sessions.gates import AdmissibilityGate, build_spread_tracker, build_window_set
from aurion.signals.context import (
    DEFAULT_ANALYSIS_WINDOW,
    DEFAULT_HTF_TIMEFRAMES,
    build_context,
)
from aurion.signals.engine import (
    Candidate,
    EngineParams,
    InstrumentMeta,
    SignalEngine,
)
from aurion.signals.lifecycle import LifecycleManager, TrackedSignal
from aurion.signals.model import ConstantProbabilityModel, LogisticModel

_log = logging.getLogger(__name__)

#: Bars drawn on a signal chart. Matches `render_signal_chart`'s own default so the
#: window fetched and the window drawn cannot drift apart.
CHART_BARS = 120

PAUSE_KEY = "notify.paused"


@dataclass(slots=True)
class RuntimeState:
    started_at: datetime
    paused: bool = False
    evaluations: int = 0
    emitted: int = 0
    transitions: int = 0
    last_heartbeat: datetime | None = None
    blocked_states: dict[str, datetime] = field(default_factory=dict)

    def uptime_s(self, now: datetime) -> float:
        return (now - self.started_at).total_seconds()


def _instrument_meta(cfg: AurionConfig) -> dict[str, InstrumentMeta]:
    return {
        symbol: InstrumentMeta(
            point=inst.point,
            contract_size=inst.contract_size,
            base_ccy=inst.base_ccy,
            quote_ccy=inst.quote_ccy,
        )
        for symbol, inst in cfg.enabled_instruments.items()
    }


def _engine_params(cfg: AurionConfig) -> EngineParams:
    signal = cfg.signal
    sizing = cfg.sizing
    entry = cfg.entry
    pd_cfg = (cfg.analysis.model_extra or {}).get("pd_range", {})
    return EngineParams(
        require_w1_d1_agreement=bool(cfg.bias.get("require_w1_d1_agreement", True)),
        pd_max_dist_atr=float(pd_cfg.get("max_dist_atr", 2.0)),
        mss_window_bars=int(entry.get("mss_window_bars", 10)),
        fvg_level=str(entry.get("fvg_level", "ce")),
        rr_min=float(signal.get("rr_min", 2.0)),
        expected_r_min=float(signal.get("expected_r_min", 0.15)),
        p_floor=float(signal.get("p_floor", 0.25)),
        sl_buffer_atr=float(signal.get("sl_buffer_atr", 0.25)),
        sl_min_atr=float(signal.get("sl_min_atr", 0.3)),
        tp_min_level_strength=float(signal.get("tp_min_level_strength", 0.4)),
        max_pending_bars=int(signal.get("max_pending_bars", 12)),
        expiry_grace_minutes=int(signal.get("expiry_grace_minutes", 0)),
        ote_lo=float(pd_cfg.get("ote_lo", 0.62)),
        ote_hi=float(pd_cfg.get("ote_hi", 0.79)),
        max_usd_risk_fraction=float(cfg.exposure.get("max_usd_risk_fraction", 1.5)),
        risk_pct=float(sizing.get("risk_pct", 0.5)),
        account_size=float(sizing.get("account_size", 10_000)),
        account_ccy=str(sizing.get("account_ccy", "USD")),
        lot_step=float(sizing.get("lot_step", 0.01)),
        min_lot=float(sizing.get("min_lot", 0.01)),
    )


class AurionApp:
    """The live application. One asyncio loop, one event bus, one consumer."""

    def __init__(
        self,
        cfg: AurionConfig,
        adapter: DataAdapter,
        store: Store,
        transport: TelegramTransport | None = None,
        calendar: CalendarProvider | None = None,
        clock: Clock | None = None,
        model: LogisticModel | ConstantProbabilityModel | None = None,
        telegram: TelegramSettings | None = None,
        entry_timeframe: TimeFrame = TimeFrame.M15,
    ) -> None:
        self.cfg = cfg
        self.clock: Clock = clock or WallClock()
        self.store = store
        self.bus = EventBus()
        self.repo = InMemoryBarRepository(
            max_bars_per_series=2000, max_m1_per_instrument=60 * 24 * 10
        )
        self.supervisor = IngestSupervisor(cfg, adapter, store, self.repo, self.bus, self.clock)
        self.calendar = calendar or NullCalendarProvider()
        self.windows = build_window_set(cfg)
        self.spread = build_spread_tracker(cfg)
        self.health = HealthMonitor(
            cfg.health.max_bar_staleness_s, cfg.health.calendar_outage_realert_min
        )
        self.gate = AdmissibilityGate(
            cfg,
            self.windows,
            self.spread,
            self.calendar,
            has_active=self._has_active,
            on_calendar_outage=self._on_calendar_outage,
        )
        points = {s: i.point for s, i in cfg.enabled_instruments.items()}
        self.params: AnalysisParams = params_from_config(
            cfg, next(iter(points.values()), 0.01)
        )
        self.objects = ObjectStore(self.params)
        self.lifecycle = LifecycleManager()
        self.macro = MacroCache()
        self.meta = _instrument_meta(cfg)
        self.engine = SignalEngine(
            _engine_params(cfg), self.objects, self.gate, self.meta, model
        )
        self.telegram = telegram or TelegramSettings.from_env()
        self.outbox = Outbox()
        self.transport = transport
        self.sender = (
            OutboxSender(store, self.outbox, transport, self.telegram)
            if transport is not None
            else None
        )
        self.state = RuntimeState(started_at=self.clock.now())
        self._entry_tf = entry_timeframe
        self._stop = asyncio.Event()
        self._points = points
        # Charts are transient artefacts of an alert, not state: they live beside the
        # logs, are gitignored, and nothing reads them back.
        self._chart_dir = Path(os.environ.get("AURION_CHART_DIR", "./reports/charts"))
        self._pending: set[asyncio.Task[None]] = set()

    # -- wiring ------------------------------------------------------------
    def _has_active(self, instrument: str) -> bool:
        return self.lifecycle.has_active(instrument)

    def _on_calendar_outage(self, at: datetime) -> None:
        alert = self.health.calendar_outage(at, available=False)
        if alert is None:
            return
        # Held in a set until it finishes: a bare create_task reference can be collected
        # mid-flight, and this is the alert that tells you the bot has gone silent.
        task = asyncio.create_task(self._alert(alert.render(), alert.dedupe_key))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _alert(self, text: str, key: str) -> None:
        # Falls back to the signal chat rather than dropping the alert. A calendar outage
        # blocks every signal under fail_safe=block, so an unconfigured admin chat would make
        # the one message that explains the silence the one message nobody receives.
        chat = self.telegram.admin_chat_id or self.telegram.signal_chat_id
        if not chat:
            _log.warning("health alert with no chat configured", extra={"alert": key})
            return
        if not self.telegram.admin_chat_id:
            _log.warning("health alert routed to the signal chat", extra={"alert": key})
        async with self.store.session() as session, session.begin():
            await self.outbox.enqueue(
                session,
                OutboxMessage(key, chat, OutboxKind.HEALTH, text),
                self.clock.now(),
            )

    def subscribe(self) -> None:
        self.bus.subscribe(BarClosed, self._on_bar_closed)
        self.bus.subscribe(FeedStale, self._on_feed_stale)
        self.bus.subscribe(AdapterStateChanged, self._on_adapter_state)

    # -- handlers ----------------------------------------------------------
    async def _on_adapter_state(self, event: AdapterStateChanged) -> None:
        alert = self.health.adapter_state(
            event.adapter, event.state == "connected", event.at, event.detail
        )
        await self._alert(alert.render(), alert.dedupe_key)

    async def _on_feed_stale(self, event: FeedStale) -> None:
        alert = self.health.feed_stale(event.instrument, event.age_seconds, self.clock.now())
        if alert is not None:
            await self._alert(alert.render(), alert.dedupe_key)

    async def _on_bar_closed(self, event: BarClosed) -> None:
        """Bar-close-driven evaluation. The only trigger by default."""
        if event.bar.timeframe is not self._entry_tf:
            return
        self.health.counters.bars_ingested += 1
        if self.state.paused:
            return
        instrument = event.bar.instrument
        as_of = event.as_of
        view = MarketView(self.repo, as_of)

        refresh = event.bar.timeframe is TimeFrame.D1
        context = build_context(
            view,
            instrument,
            as_of,
            self.objects,
            entry_timeframe=self._entry_tf,
            htf_timeframes=DEFAULT_HTF_TIMEFRAMES,
            analysis_window=DEFAULT_ANALYSIS_WINDOW,
            # Was missing: without it the SILVER_BULLET_FVG model can never fire live, so
            # the backtest validated two entry models and the live process ran one.
            windows=self.windows,
            macro={
                name: value
                for name, value in (
                    (n, self.macro.value(n, as_of)) for n in self.macro.series_ids()
                )
                if value is not None
            },
        )
        candidate = self.engine.evaluate(
            view, instrument, as_of, context, DEFAULT_ANALYSIS_WINDOW, refresh
        )
        self.state.evaluations += 1
        self.health.counters.candidates += 1
        if candidate.rejected_reason:
            self.health.counters.record_rejection(candidate.rejected_reason)
        log_candidate(
            _log,
            instrument,
            as_of,
            candidate.reached_step.name.lower(),
            candidate.rejected_reason,
            p_hat=candidate.p_hat,
            expected_r=candidate.expected_r,
        )
        if not candidate.emitted or candidate.geometry is None:
            return

        signal = TrackedSignal(
            signal_id=candidate.signal_id,
            instrument=instrument,
            direction=candidate.geometry.direction,
            entry=candidate.geometry.entry,
            stop_loss=candidate.geometry.stop_loss,
            targets=tuple(t.price for t in candidate.geometry.targets),
            expires_at=candidate.expires_at or as_of + timedelta(hours=4),
            created_at=as_of,
            assume_be_after_tp1=bool(self.cfg.lifecycle.get("assume_be_after_tp1", True)),
        )
        self.lifecycle.add(signal)
        self.state.emitted += 1
        self.health.counters.signals_emitted += 1

        point = self._points.get(instrument, 0.01)
        paper = self.cfg.meta.mode != "live_alerts"
        text = (
            format_signal_compact(candidate, point, paper=paper)
            if self.cfg.notify.get("verbosity", "compact") == "compact"
            else format_signal(candidate, point, paper=paper)
        )
        chart = await self._render_chart(candidate, view, instrument)
        async with self.store.session() as session, session.begin():
            await self.outbox.enqueue(
                session,
                OutboxMessage(
                    dedupe_key(candidate.signal_id, "proposed"),
                    self.telegram.signal_chat_id,
                    OutboxKind.SIGNAL,
                    text,
                    chart_path=str(chart) if chart is not None else None,
                ),
                as_of,
            )

    async def _render_chart(
        self, candidate: Candidate, view: MarketView, instrument: str
    ) -> Path | None:
        """Annotated PNG for an emitted signal, or ``None``.

        `docs/03` §7.3 promises a chart under `notify.send_charts`, rendered off the event
        loop, and the text sent without it past `notify.chart_timeout_s`. Nothing called
        `render_signal_chart` at all, so every alert was text-only and the timeout the
        renderer's own docstring described had no mechanism behind it. The chart is a
        convenience and the alert is the product, so every failure path here returns None.
        """
        if not self.cfg.notify.get("send_charts", True) or candidate.geometry is None:
            return None
        bars = list(view.usable_bars(instrument, self._entry_tf, CHART_BARS))
        if not bars:
            return None
        geometry = candidate.geometry
        annotation = ChartAnnotation(
            entry=geometry.entry,
            stop_loss=geometry.stop_loss,
            targets=tuple(t.price for t in geometry.targets),
            swept_level=candidate.proposal.sweep.pool_level
            if candidate.proposal is not None and candidate.proposal.sweep is not None
            else None,
            sweep_extremity=candidate.proposal.sweep.extremity
            if candidate.proposal is not None and candidate.proposal.sweep is not None
            else None,
            title=f"{instrument} {geometry.direction.label} {candidate.signal_id}",
            direction=geometry.direction,
            # `reasons` are (feature, log-odds) pairs: rendered with the signed
            # contribution, because "session_is_ny_am" alone says nothing about which way
            # it pushed.
            notes=[f"{name} {value:+.2f}" for name, value in candidate.reasons],
        )
        out = self._chart_dir / f"{candidate.signal_id}.png"
        timeout = float(self.cfg.notify.get("chart_timeout_s", 10.0))
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(render_signal_chart, bars, annotation, out, CHART_BARS),
                timeout=timeout,
            )
        except (TimeoutError, OSError, ValueError) as exc:
            # Never fatal: a missing chart is a smaller problem than a missing alert.
            _log.warning("chart rendering failed, sending text only: %s", exc)
            return None

    async def step_lifecycle(self, instrument: str) -> None:
        """Advance open signals on the newest M1 bar and enqueue one update each.

        Reads through a `MarketView` rather than the repository directly: the as_of
        boundary is the mechanism that makes look-ahead impossible, and a handler that
        reaches past it would be the one place it does not hold.
        """
        view = MarketView(self.repo, self.clock.now())
        m1 = view.m1(instrument, 1)
        if not m1:
            return
        # Named `tracked`, not `signal`: the stdlib `signal` module is imported above and
        # shadowing it here would break the SIGTERM handler installation.
        for tracked, transition in self.lifecycle.step(m1[-1]):
            self.state.transitions += 1
            self.health.counters.transitions += 1
            text = format_transition(
                tracked,
                transition,
                self._points.get(instrument, 0.01),
                paper=self.cfg.meta.mode != "live_alerts",
            )
            async with self.store.session() as session, session.begin():
                await self.outbox.enqueue(
                    session,
                    OutboxMessage(
                        dedupe_key(tracked.signal_id, str(transition.state), 0),
                        self.telegram.signal_chat_id,
                        OutboxKind.TRANSITION,
                        text,
                    ),
                    transition.at,
                )

    # -- pause -------------------------------------------------------------
    async def set_paused(self, paused: bool) -> None:
        """Persisted, so a restart does not silently resume a paused bot."""
        self.state.paused = paused
        async with self.store.session() as session, session.begin():
            from aurion.data.store import KVRow

            row = await session.get(KVRow, PAUSE_KEY)
            if row is None:
                session.add(KVRow(key=PAUSE_KEY, value_json="true" if paused else "false"))
            else:
                row.value_json = "true" if paused else "false"

    async def load_paused(self) -> None:
        from aurion.data.store import KVRow

        async with self.store.session() as session:
            row = await session.get(KVRow, PAUSE_KEY)
        self.state.paused = bool(row is not None and row.value_json == "true")

    # -- lifecycle ---------------------------------------------------------
    async def startup(self) -> None:
        await self.store.create_all()
        await self.load_paused()
        self.subscribe()
        for instrument in self.supervisor.instruments:
            await self.supervisor.rehydrate(instrument)
            if self.supervisor.stats.determinism_mismatches:
                alert = self.health.determinism_mismatch(
                    instrument, "resampled", self.clock.now()
                )
                await self._alert(alert.render(), alert.dedupe_key)
        _log.info(
            "startup complete",
            extra={
                "event": "startup",
                "instruments": list(self.supervisor.instruments),
                "paused": self.state.paused,
                "mode": self.cfg.meta.mode,
            },
        )

    async def shutdown(self) -> None:
        """Stop accepting events, flush the outbox, checkpoint cursors."""
        self._stop.set()
        self.supervisor.stop()
        await self.bus.stop()
        if self.sender is not None:
            with contextlib.suppress(Exception):
                await self.sender.drain()
        for instrument in self.supervisor.instruments:
            last = self.supervisor.stats.last_candle_at.get(instrument)
            if last is not None:
                await self.store.set_cursor(instrument, last_bar_open_time=last)
        _log.info("shutdown complete", extra={"event": "shutdown"})

    async def heartbeat_due(self, now: datetime) -> bool:
        target = self.cfg.health.heartbeat_time
        local = now.astimezone(UTC)
        last = self.state.last_heartbeat
        if last is not None and last.date() == local.date():
            return False
        return (local.hour, local.minute) >= (target.hour, target.minute)

    async def maybe_heartbeat(self, now: datetime) -> None:
        if not await self.heartbeat_due(now):
            return
        alert = self.health.heartbeat(now, self.state.uptime_s(now))
        await self._alert(alert.render(), alert.dedupe_key)
        self.state.last_heartbeat = now
        self.health.counters.reset()
        _ = AlertKind.HEARTBEAT

    async def run(self) -> None:
        await self.startup()
        self.bus.start()
        tasks = [asyncio.create_task(self.supervisor.run_stream())]
        if self.sender is not None:
            tasks.append(asyncio.create_task(self.sender.run(stop=self._stop)))
        tasks.append(asyncio.create_task(self._housekeeping()))
        try:
            await self._stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await self.shutdown()

    async def _housekeeping(self) -> None:
        while not self._stop.is_set():
            now = self.clock.now()
            try:
                await self.supervisor.check_staleness(now)
                await self.maybe_heartbeat(now)
                for instrument in self.supervisor.instruments:
                    await self.step_lifecycle(instrument)
            except Exception as exc:  # pragma: no cover - the loop must survive
                alert = self.health.exception("housekeeping", exc, now)
                await self._alert(alert.render(), alert.dedupe_key)
            await asyncio.sleep(10.0)

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(signum, self._stop.set)


#: Variables the process cannot do its job without, each with the thing that silently stops
#: working when it is missing. A missing variable must not yield a process that looks healthy
#: and delivers nothing: that is worse than a crash, because nothing ever asks why it is
#: quiet. The third field marks the ones that are fatal outside `backtest` mode -- the only
#: mode that legitimately has neither a price stream nor a delivery channel.
_REQUIRED_ENV: tuple[tuple[str, str, bool], ...] = (
    ("AURION_OANDA_API_TOKEN", "the price stream cannot authenticate, so no bar ever closes", True),
    ("AURION_OANDA_ACCOUNT_ID", "OANDA rejects the stream request", True),
    ("AURION_TELEGRAM_BOT_TOKEN", "no signal, update or health alert can be delivered", True),
    ("AURION_TELEGRAM_SIGNAL_CHAT_ID", "signals have nowhere to go", True),
    (
        "AURION_TELEGRAM_ADMIN_CHAT_ID",
        "health alerts have nowhere to go, so a stalled feed is invisible",
        False,
    ),
    (
        "AURION_TELEGRAM_ALLOWED_USER_IDS",
        "the allow-list fails closed, so every command is ignored without reply",
        False,
    ),
)


#: The v20 account ID shape, e.g. 101-001-1234567-001. Checked because this is a value
#: people type by hand, and a transcription error produces an opaque 404 from the streaming
#: endpoint that reads like a network fault rather than a typo. A warning rather than a
#: refusal: the pattern is OANDA's convention, not something this code should bet a working
#: deployment on being able to recognise forever.
_ACCOUNT_ID = re.compile(r"^\d{3}-\d{3}-\d+-\d{3}$")


@dataclass(frozen=True, slots=True)
class PreflightFinding:
    variable: str
    problem: str
    consequence: str
    fatal: bool

    def render(self) -> str:
        return f"{self.variable} {self.problem}: {self.consequence}"


def preflight(env: Mapping[str, str], mode: str) -> list[PreflightFinding]:
    """Missing-variable findings, in declaration order.

    Does not raise and does not read `os.environ` itself, so the startup contract is
    testable without mutating the process environment.
    """
    outside_backtest = mode != "backtest"
    out = [
        PreflightFinding(name, "is unset or empty", consequence, fatal and outside_backtest)
        for name, consequence, fatal in _REQUIRED_ENV
        if not env.get(name, "").strip()
    ]
    account = env.get("AURION_OANDA_ACCOUNT_ID", "").strip()
    if account and not _ACCOUNT_ID.match(account):
        out.append(
            PreflightFinding(
                "AURION_OANDA_ACCOUNT_ID",
                "does not look like an OANDA v20 account ID",
                "expected four dash-separated groups such as 101-001-1234567-001; a typo "
                "here returns 404 from the stream, which reads as a network fault",
                False,
            )
        )
    return out


def build_app(config_path: str | Path | None = None) -> AurionApp:
    """Construct the live app from config and the environment."""
    # Before anything reads os.environ. Nothing used to load `.env`, so running the module
    # directly saw an empty environment and refused to start with a filled-in file beside it.
    env_warnings = load_dotenv(os.environ.get("AURION_ENV_FILE", ".env"))
    cfg = load_config(config_path or os.environ.get("AURION_CONFIG_PATH", "config.yaml"))
    configure(
        level=os.environ.get("AURION_LOG_LEVEL", "INFO"),
        log_dir=os.environ.get("AURION_LOG_DIR"),
        rotate_mb=cfg.runtime.log_rotate_mb,
        json_format=cfg.runtime.log_format == "json",
    )
    store = Store(os.environ.get("AURION_DB_URL", "sqlite+aiosqlite:///./data/aurion.sqlite"))
    adapter = OandaAdapter(
        api_token=os.environ.get("AURION_OANDA_API_TOKEN", ""),
        account_id=os.environ.get("AURION_OANDA_ACCOUNT_ID", ""),
        environment="live" if os.environ.get("AURION_OANDA_ENV") == "live" else "practice",
        stream_idle_timeout_s=cfg.adapter.stream_idle_timeout_s,
        points={s: i.point for s, i in cfg.enabled_instruments.items()},
    )
    for warning in env_warnings:
        # Reported rather than silently cleaned: docker compose's env_file parser keeps the
        # whitespace and the inline comment, so an uncorrected file behaves differently
        # depending on how the process was launched.
        _log.warning(".env %s", warning.render())

    findings = preflight(os.environ, cfg.meta.mode)
    for finding in findings:
        _log.log(
            logging.ERROR if finding.fatal else logging.WARNING, "preflight: %s", finding.render()
        )
    fatal = [f.variable for f in findings if f.fatal]
    if fatal:
        raise RuntimeError(
            f"refusing to start in mode {cfg.meta.mode!r} without " + ", ".join(fatal)
        )

    settings = TelegramSettings.from_env()
    transport = HttpTelegramTransport(settings.token) if settings.token else None
    # Was always NullCalendarProvider, which reports itself unavailable, which under
    # `news.fail_safe = block` suppresses every signal -- so the process could not emit at
    # all, and said so in exactly one admin alert.
    calendar = calendar_from_env(os.environ)
    if isinstance(calendar, StaticCalendarProvider) and is_template(
        calendar.events(datetime.min.replace(tzinfo=UTC), datetime.max.replace(tzinfo=UTC))
    ):
        # Every start, not once: a file of invented release times blocks the wrong minutes,
        # and the only thing distinguishing it from a real schedule at runtime is this check.
        _log.warning(
            "the calendar is the shipped TEMPLATE: its release times are invented, so the "
            "news gate is blocking the wrong minutes. Replace it before acting on an alert "
            "(see data/README.md)."
        )
    if isinstance(calendar, NullCalendarProvider) and cfg.news.fail_safe == "block":
        _log.warning(
            "no calendar provider configured and news.fail_safe=block: EVERY signal will be "
            "suppressed. Set AURION_CALENDAR_PROVIDER=file and AURION_CALENDAR_PATH."
        )
    return AurionApp(cfg, adapter, store, transport=transport, calendar=calendar)


async def main() -> None:  # pragma: no cover - process entry point
    app = build_app()
    app.install_signal_handlers()
    started = time.monotonic()
    try:
        await app.run()
    finally:
        _log.info("ran for %.1fs", time.monotonic() - started)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
