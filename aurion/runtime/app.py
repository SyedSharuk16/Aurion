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
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from aurion.analysis.regime.correlation import session_vwap
from aurion.analysis.regime.trend import trend_regime_state
from aurion.analysis.regime.volatility import volatility_state
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
from aurion.notify.telegram.formatters import format_signal, format_transition
from aurion.notify.telegram.outbox import Outbox, OutboxKind, OutboxMessage, dedupe_key
from aurion.runtime.logging import configure, log_candidate
from aurion.sessions.calendar import CalendarProvider, NullCalendarProvider
from aurion.sessions.gates import AdmissibilityGate, build_spread_tracker, build_window_set
from aurion.signals.engine import (
    EngineParams,
    EvaluationContext,
    InstrumentMeta,
    SignalEngine,
)
from aurion.signals.lifecycle import LifecycleManager, TrackedSignal
from aurion.signals.model import ConstantProbabilityModel, LogisticModel

_log = logging.getLogger(__name__)

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
        if not self.telegram.admin_chat_id:
            _log.warning("admin alert with no chat configured", extra={"alert": key})
            return
        async with self.store.session() as session, session.begin():
            await self.outbox.enqueue(
                session,
                OutboxMessage(key, self.telegram.admin_chat_id, OutboxKind.HEALTH, text),
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

        htf_frames = (TimeFrame.H1, TimeFrame.H4, TimeFrame.D1, TimeFrame.W1)
        refresh = event.bar.timeframe is TimeFrame.D1
        htf = {
            str(tf): self.objects.snapshot(view, instrument, tf, 400) for tf in htf_frames
        }
        entry_bars = list(view.usable_bars(instrument, self._entry_tf, 400))
        quote = view.last_quote(instrument)
        context = EvaluationContext(
            entry_timeframe=self._entry_tf,
            htf=htf,
            volatility=volatility_state(entry_bars) if entry_bars else None,
            regime_trend=trend_regime_state(entry_bars) if entry_bars else None,
            vwap=session_vwap(entry_bars, entry_bars[0].open_time) if entry_bars else None,
            macro={
                name: value
                for name, value in (
                    (n, self.macro.value(n, as_of)) for n in self.macro.series_ids()
                )
                if value is not None
            },
            weekday=as_of.weekday(),
            spread=quote.spread if quote is not None else None,
            active_usd_exposure=[],
        )
        candidate = self.engine.evaluate(view, instrument, as_of, context, 400, refresh)
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

        text = format_signal(
            candidate,
            self._points.get(instrument, 0.01),
            paper=self.cfg.meta.mode != "live_alerts",
        )
        async with self.store.session() as session, session.begin():
            await self.outbox.enqueue(
                session,
                OutboxMessage(
                    dedupe_key(candidate.signal_id, "proposed"),
                    self.telegram.signal_chat_id,
                    OutboxKind.SIGNAL,
                    text,
                ),
                as_of,
            )

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


def build_app(config_path: str | Path | None = None) -> AurionApp:
    """Construct the live app from config and the environment."""
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
    settings = TelegramSettings.from_env()
    transport = HttpTelegramTransport(settings.token) if settings.token else None
    return AurionApp(cfg, adapter, store, transport=transport)


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
