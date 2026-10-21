# 01 — Architecture

Phase 1 deliverable. No code exists yet; this document is the contract Phase 2–5 implement.

---

## 1. Scope boundary

The system ingests market data, evaluates it on bar close, and emits **alerts**. It has no
order-placement surface. The OANDA adapter uses `/v3/instruments/*/candles`,
`/v3/accounts/*/pricing`, and `/v3/accounts/*/pricing/stream` only. The adapter interface
has no `submit`, `amend`, or `cancel` method, so execution cannot be added by accident —
only by extending the interface, which is a visible change.

Position sizing output is a **suggestion in lots**, computed from a configured notional
account size. It is not connected to an account balance query.

---

## 2. Repository layout

```
aurion/
  config/            pydantic models for config.yaml + .env; validation at import time
  core/              Clock, MarketView, EventBus, typed events, Instrument, Price, TimeFrame
  data/
    adapters/        DataAdapter protocol; oanda.py (first), mt5.py (stub), twelvedata.py (stub)
    store.py         SQLAlchemy models + async repositories
    resample.py      incremental M1 -> M5/M15/H1/H4/D1/W1
    backfill.py      gap detection and repair
  sessions/
    windows.py       session window algebra (NY/London tz-aware, DST-correct)
    calendar.py      economic-calendar provider protocol + point-in-time snapshot reader
    gates.py         composite admissibility gate (session, news, rollover, Friday, spread)
  analysis/
    primitives.py    TR, ATR(Wilder), body, wick, log return, percentile rank
    structure/       fractal swings, trend state machine, BOS / CHoCH / MSS, displacement
    ict/             fvg, order_block, breaker, bpr, liquidity_pools, sweep, pd_range
    sr/              kde.py, dbscan.py, pivots.py, round_numbers.py, merge.py
    quarterly/       quarters.py, true_opens.py, amdx.py
    mmxm/            stages.py, score.py
    regime/          volatility.py, trend.py, correlation.py, vwap.py, usd_exposure.py
  features/
    schema.py        the feature vector: ordered, versioned, immutable
    build.py         MarketView -> FeatureVector (the single place features are computed)
  signals/
    bias.py          W1/D1 -> H4/H1 -> M15/M5 cascade
    model.py          calibrated probability model; load-only at runtime
    engine.py        candidate generation, decision rule, de-duplication
    lifecycle.py     signal state machine
    sizing.py        risk % -> lots
  notify/
    telegram/        bot, formatters, charting, commands, outbox
    health.py        staleness, disconnect, exception, heartbeat alerts
  backtest/
    runner.py        event-driven loop (same analysis + signals code as live)
    costs.py         spread, slippage, commission models
    fills.py         intrabar resolution rules
    walkforward.py   purged/embargoed folds, model refitting
    ablation.py      module on/off matrix
    metrics.py       expectancy, PF, DSR, Monte Carlo
    report.py        validation report generation
  runtime/
    app.py           asyncio composition root
    supervisor.py    reconnect, backfill-on-reconnect, graceful shutdown
    logging.py       structured JSON + rotation
tests/
  unit/              one test module per definition section
  fixtures/synthetic/ hand-annotated OHLC series with ground truth
  causality/         the four causality tests (§3.3)
  integration/       adapter + resampler against cached real data (gitignored)
deploy/
  Dockerfile  docker-compose.yml  aurion.service  healthcheck.py
docs/  config.yaml  .env.example
```

Dependency direction is strictly inward: `analysis/` imports from `core/` and
`primitives.py` only. It never imports `data/`, `sessions/`, `signals/`, or `notify/`.
That is what makes every analysis function a pure function of a `MarketView`.

---

## 3. The causality mechanism

This is the most important decision in the document. Look-ahead bias is prevented by
construction; the tests only confirm it.

### 3.1 `MarketView`

```
MarketView(as_of: datetime)          # UTC, always
  .bars(tf, n)        -> last n bars with bar.close_time <= as_of          (closed only)
  .partial(tf)        -> the forming bar, or None                           (opt-in, flagged)
  .objects(kind, tf)  -> derived objects with obj.confirmed_at <= as_of
  .spread_now()       -> last observed spread with observed_at <= as_of
  .calendar(window)   -> calendar snapshot as published at or before as_of
```

There is no accessor that returns a bar, object, or quote later than `as_of`. Analysis
functions receive a `MarketView`, not a DataFrame. `partial()` exists because a few
quantities (live spread, forming-bar high for sweep detection within the current bar) are
legitimately needed, but any function calling it must be decorated
`@uses_forming_bar` and is excluded from the prefix-invariance test, which is then applied
to the *decision* rather than the intermediate.

Default evaluation is **bar-close-driven**: the engine evaluates only when a bar of the
relevant timeframe has closed. Forming-bar evaluation is off by default
(`runtime.evaluate_on_forming_bar: false`, `fixed`).

### 3.2 The two formal properties

Let `f` be any analysis function, `D` the full bar history, `D|τ` its restriction to bars
closed at or before `τ`.

- **(C) Causality.** `f(MarketView(D, τ)) = f(MarketView(D|τ, τ))` for all τ.
  The output cannot depend on data after τ because it is identical when that data is absent.
- **(S) Stability / no repaint.** For τ₁ < τ₂, every object `o ∈ f(MarketView(D, τ₂))`
  with `o.confirmed_at ≤ τ₁` is equal to the corresponding object in
  `f(MarketView(D, τ₁))`. Objects never change after confirmation.

### 3.3 The four causality tests

Your brief specified one test and it is incorrect as stated. "Shift the data by one bar and
assert identical signals" cannot hold: shifting the input by one bar must shift the output
by one bar. Four distinct properties are needed, and they catch different bugs:

| Test | Property | Procedure | Bug class caught |
|---|---|---|---|
| **T1 Prefix invariance** | (C) | For every evaluation time `t` in the test set, run the full pipeline on `D[0:t]` and on `D[0:T]`; assert the decision *at* `t` is byte-identical. | Look-ahead: any use of future bars |
| **T2 Future nulling** | (C) | Replace all bars after `t` with NaN / adversarial garbage; assert the decision at `t` is unchanged. | Look-ahead via vectorised ops that silently span the array (`.rolling(center=True)`, `bfill`, full-series `zscore`, `groupby.transform`) |
| **T3 Time-translation equivariance** | — | Shift the entire series forward by `k` bars (and `k` × bar duration in time, preserving session alignment); assert every signal shifts by exactly `k`. | Absolute-index and absolute-date leakage; accidental dependence on dataset length or position |
| **T4 Resample boundary** | (C) | Assert no resampled bar of timeframe `X` has `confirmed_at` earlier than the close of its last constituent M1 bar; assert `confirmed_at == close_time` for all resampled bars. | Future-leaking resampling (left-labelled bars treated as known at their open) |

T3 cannot be a pure shift for session-dependent logic, so it is run in two variants:
`k` = whole trading days (session alignment preserved, full pipeline) and `k` = arbitrary
bars (session logic disabled, analysis modules only).

T1 is O(T) full pipeline runs and is therefore run on a bounded window (default 5,000
evaluation points sampled across regimes), not the whole history. T2 is cheap and runs on
every evaluation point in CI.

### 3.4 Warm-up and the no-silent-default rule

Any indicator with a warm-up requirement returns `None` until it has the required closed
bars — never 0, never a partial value, never a forward-filled value. A module receiving
`None` for a required input must return `None` for its own output and record the reason.
A `FeatureVector` containing any `None` in a required field cannot produce a signal; the
candidate is logged with `rejected_reason="warmup:<field>"`. This makes warm-up visible in
the backtest rather than silently biasing early results.

---

## 4. Single code path: live and backtest

Live and backtest differ in exactly three injected objects:

| Dependency | Live | Backtest |
|---|---|---|
| `Clock` | `WallClock` (UTC from system, NTP-checked) | `SimClock` (advances to the next event timestamp) |
| `BarSource` | `StreamingSource` (OANDA stream + REST backfill) | `ReplaySource` (ordered bars from the archive) |
| `SignalSink` | `TelegramSink` + `DbSink` | `DbSink` + `TradeLedger` |

`analysis/`, `features/`, and `signals/` receive only `MarketView` and config. They contain
no branch on mode. The acceptance test for this property: running the backtester over a
window for which live signals were recorded must reproduce those signals exactly
(`docs/04-validation-protocol.md` §9, live-vs-replay reconciliation).

---

## 5. Event flow

```
                 ┌────────────── OANDA pricing stream (ticks, heartbeats) ──────────────┐
                 │                                                                      v
  REST candles ──┴─> BarIngest ──> M1Store ──> IncrementalResampler ──> BarClosed(tf)   SpreadTracker
                                        │                                    │               │
                                   GapDetector ──> Backfill                  │               │
                                                                             v               │
                                                            ObjectBuilders (swings, FVG,     │
                                                             OB, pools, levels, quarters)    │
                                                                             │               │
                                                                             v               │
   Economic calendar snapshot ─> SessionGate ──────────────> admissible? ────┤<──────────────┘
                                                                             │
                                                                             v
                                           BiasCascade (W1/D1 -> H4/H1) -> CandidateGen (M15/M5)
                                                                             │
                                                                   FeatureVector (persisted)
                                                                             │
                                                                 CalibratedModel -> p̂
                                                                             │
                                                            DecisionRule (expectancy gate)
                                                                             │
                                                     SignalProposed ──> LifecycleManager
                                                                             │
                                                           ┌─────────────────┴────────────────┐
                                                           v                                  v
                                                 TelegramOutbox (idempotent)            DbSink / Ledger
```

Events are typed dataclasses on an in-process `asyncio` bus. There is no cross-process
queue: at this scale (7 instruments, ~1 evaluation/minute worst case) a broker is pure
overhead and an additional failure mode.

Ordering guarantee: the bus is a single consumer loop processing events in timestamp order.
A tick that arrives out of order relative to a bar close is buffered until the bar-close
event for its minute has been emitted. This matters because otherwise the live system can
observe a spread from `t+1` while evaluating `t`.

---

## 6. State and restart

All mutable state is in the database, none in memory-only structures:

- M1 bars (rolling window on the VPS), resampled bars (cached, recomputable)
- Derived objects with `(event_time, confirmed_at, invalidated_at)`
- Signals and their full state-transition history
- Feature vectors as computed at decision time
- Telegram outbox rows with a unique dedupe key
- Adapter cursors (last bar time per instrument, last stream heartbeat)

On restart the supervisor: loads cursors → backfills the gap via REST → recomputes
resampled bars for the gap → replays object builders over the gap → reconciles signal
lifecycle against current prices → resumes streaming. Derived objects are **recomputed, not
trusted**, and the recomputed set must match the stored set for the overlapping region;
a mismatch is a hard health alert, because it means a non-deterministic analysis function
exists.

---

## 7. Runtime and deployment

- `asyncio` single event loop. Blocking work (KDE fit, mplfinance render) goes to a
  `ThreadPoolExecutor` with `max_workers=1` to bound memory on a 1 vCPU box.
- Streaming auto-reconnect with exponential backoff and jitter; on reconnect, always
  REST-backfill from the last stored bar rather than assuming continuity.
- Structured JSON logs to stdout, rotated by size; one line per event with
  `instrument`, `as_of`, `event`, `decision`, `rejected_reason`.
- Graceful shutdown on SIGTERM: stop accepting events, flush outbox, checkpoint cursors.
- Docker + compose with `restart: unless-stopped` and a `healthcheck` that fails when the
  newest M1 bar is older than `health.max_bar_staleness_s` during an open session.
- `deploy/aurion.service` systemd unit as the non-Docker alternative
  (`Restart=always`, `RestartSec=10`, `MemoryMax=` set from the budget below).

---

## 8. Resource budget (explicit arithmetic, because the target box is small)

M1 bars, 7 instruments (6 tradable + USD-index proxy inputs), ~1,440 bars/day × 5 days/week
≈ 375,000 bars/instrument/year ≈ 2.6M rows/year.

Row cost in SQLite with bid/ask OHLC (8 REAL), tick volume, timestamp, PK index:
≈ 130–150 bytes. So **≈ 350 MB per year of full M1 history**, ~1.75 GB for five years.

That does not fit a 1–2 GB VPS alongside the process and a backtest. Therefore:

| Role | Store | Retention | Size |
|---|---|---|---|
| Live VPS | SQLite | M1: `data.m1_retention_days` (default 180); M5–W1: full | ~180 MB |
| Offline archive | Parquet, partitioned `instrument/year-month`, zstd | full | ~35 MB/instrument/year |
| Backtesting | Parquet archive on a dev machine | full | — |

This satisfies "persist candles and state in SQLite, Postgres-ready via SQLAlchemy" for the
live system while keeping backtests feasible. If you want a single store for both, the
answer is Postgres on a bigger box, not SQLite on this one — flagged as
`docs/06-open-questions.md` Q12.

Compute: resampling is **incremental** (one running partial bar per timeframe, O(1) per M1
bar). Analysis results are memoised on `(instrument, tf, last_closed_bar_time, config_hash)`.
The KDE S&R fit is the only expensive step; it is recomputed on D1 close, not per M1 bar.

Steady-state estimate: < 5% of one vCPU, < 350 MB RSS. To be measured in Phase 5, not
assumed.

---

## 9. Configuration and parameter discipline

`config.yaml` + `.env`, validated by pydantic at import; the process refuses to start on a
validation error rather than defaulting. Every numeric value carries a provenance tag:

- `fixed` — structural, not a free parameter (e.g. FVG is 3 candles).
- `prior` — chosen from first principles or neutrality, **frozen before optimisation**, not
  tuned.
- `tunable` — exposed to walk-forward optimisation with a declared search range. Every
  `tunable` parameter's range enters the trial count used by the deflated Sharpe ratio.

The search budget is on **trials**, not parameters, and it is derived rather than chosen:
a trial is any configuration a selection decision is taken from — parameter settings and
ablation arms alike — because that is what the deflated Sharpe ratio penalises. Each
doubling of the trial count costs roughly 0.15 of annualised Sharpe, so
`validation.max_trials` is solved from the pilot's measured baseline Sharpe and trial
correlation (`docs/07-power-and-capacity.md` §4, §6). Only one dimension is searched — an
ordinal `analysis.selectivity` — because the three thresholds it collapses
(`displacement_k`, `fvg_min_atr`, `sweep_min_penetration_atr`) move the candidate
population along a single axis, and varying them independently would spend three
dimensions of budget on one. Everything else is `prior` or `fixed`. The config hash is
recorded on every signal and every backtest run.
