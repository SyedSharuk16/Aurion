# Aurion

Signal-only (alert-generating) trading analysis system for XAUUSD and FX majors.
**It never places, modifies, or cancels an order.** There is no execution code path and
no broker order endpoint is referenced anywhere in the design.

---

## Status: Phases 1-5 built. No validated edge. No live alerts.

| Phase | Contents | State |
|---|---|---|
| 1 | Architecture, exact definitions, config surface, validation protocol, open questions | **Approved** |
| 1.5 | Power, trial capacity, the module cut, pilot specification (`docs/07`, `tools/power.py`) | **Approved with four conditions, all met** |
| 2 | Data adapters (OANDA v20 first), resampler, session engine, schema, tests | **Built** |
| 2b | Macro daily series adapter (FRED/ALFRED vintages), ingest supervisor, gap repair | **Built** |
| 3 | Analysis modules: `structure`, `ict`, `sr`, `regime` (Tier 1) and `quarterly`, `mmxm` (Tier 2, observational) | **Built** |
| 4 | Signal engine, event-driven backtester, purged walk-forward, ablation, validation report | **Built** |
| 5 | Telegram outbox and commands, health channel, always-on runtime, Docker/systemd | **Built** |
| 2E | **Power pilot** (`docs/07` §5) — XAUUSD only, on purchased M1 history | **Not run. Needs data.** |

"Built" means the code exists, is `mypy --strict` clean, and is covered by tests against
synthetic fixtures. It does **not** mean the system has been shown to work. Everything that
would make that claim — expectancy, the ablation, the deflated Sharpe, the walk-forward —
requires real M1 history that has not been bought yet. Phase 2E is the next gate and it is a
data purchase decision, not a coding task.

**The phase order differs from the original brief, deliberately.** The brief put the
backtester in Phase 4 and the signal engine in Phase 5. The backtester replays the live
signal engine (design commitment 2 below), so the engine must exist before the backtester
can be written. Phase 4 here is `signals/` then `backtest/`; Phase 5 is delivery
(`notify/`, `runtime/`, `deploy/`).

Live alerts are gated behind the acceptance criteria in `docs/04-validation-protocol.md` §8
and `aurion/backtest/report.py`, and `[PAPER]`-tagged forward running for >=4 weeks before
the tag is removed. The tag is applied by the formatter, not by an operator's discretion.

---

## Documents (read in this order)

| File | What it settles |
|---|---|
| `docs/01-architecture.md` | Module layout, the `MarketView` causality mechanism, event flow, runtime, resource budget |
| `docs/02-definitions.md` | **The approval artifact.** Exact, testable definition of every concept, with parameter provenance and heuristic flags |
| `docs/03-signal-engine.md` | Top-down bias cascade, decision rule, signal lifecycle state machine, position sizing |
| `docs/04-validation-protocol.md` | Backtest mechanics, cost model, walk-forward, ablation, power analysis, deflated Sharpe, Monte Carlo |
| `docs/05-data-contracts.md` | Adapter interface, DB schema, feature store, Telegram outbox |
| `docs/06-open-questions.md` | The 17 concepts that **cannot** be defined objectively and their testable substitutes; the 14 decisions (all resolved); pre-registered predictions with effect sizes. **A17 is open and needs a decision before the pilot** |
| `docs/07-power-and-capacity.md` | Derived trial budget, minimum detectable effects, the module cut, the pilot spec, and two corrections to Phase 1 |
| `tools/power.py` | Runnable calculator behind every number in `docs/07` |
| `tools/oanda_whoami.py` | Prints the OANDA account ID and environment for a token |
| `scripts/setup.py` | First-run verification. Sends one real message. Any shell, any OS |
| `config.yaml` | Every threshold in the system, each tagged `fixed` / `prior` / `derived` |

---

## Running it

```bash
make install     # editable install with dev extras
make causality   # the four causality tests -- run these first
make all         # lint + types + coverage + causality
make power       # regenerate every table in docs/07
```

Current state: **493 tests**, `ruff` clean, `mypy --strict` clean on 84 source files.
Branch coverage by package, because the aggregate hides the distribution:

| Package | Coverage | |
|---|---|---|
| `core/` | 94% | |
| `signals/` | 94% | the cascade, both entry models, geometry, model, lifecycle |
| `analysis/` | **93%** | the contractual gate is 80% on analysis modules (`docs/04` §8) |
| `sessions/` | 92% | |
| `notify/` | 90% | |
| `data/` | 88% | |
| `backtest/` | 88% | |
| `features/` | 87% | |
| `runtime/` | 60% | `app.py` 55%: the asyncio run loop, rehydration, `build_app` |
| Total | 90% | |

`runtime/app.py` is the one that stays low and the reason is stated rather than hidden: what
is left uncovered is the asyncio run loop, the reconnect path and the process entry point,
where a unit test mostly asserts that a mock was called. Its *decision* path is covered
directly -- the bar-close handler, the lifecycle step, the chart render and the alert
routing all have tests -- and `signals/engine.py` is at 93% through a full-cascade test on a
real M15 series carrying a genuine sweep, CHoCH and MSS, asserting the outcome at each of
the eight steps. The uncovered remainder is process wiring, which is what the Phase 2E
pilot exercises first.

What exists and is tested:

| Component | Notes |
|---|---|
| `core/timeframe.py` | DST-correct bucketing. Boundaries are generated in the anchor's local time, so H4/D1/W1 produce exactly one irregular bar per DST transition (3h/5h on H4, 23h/25h on D1) and they are flagged, not normalised |
| `core/marketview.py` | The causality boundary. No accessor returns anything later than `as_of`; forming-bar access needs an explicit opt-in |
| `core/clock.py` | `SimClock` refuses to move backwards -- a backwards step is how replay reveals future data |
| `data/resample.py` | Incremental, O(1) per candle. Emits only at close, flags thin buckets, never emits empty ones |
| `data/store.py` | SQLAlchemy 2.x async, idempotent upserts, gap detection, retention pruning, trial ledger |
| `data/adapters/oanda.py` | Read-only, `price=BA`, drops incomplete candles, refuses any granularity above M1 |
| `data/macro.py` | FRED/ALFRED **vintage** reads, so a regime conditioner never sees a revision published after `as_of` |
| `sessions/` | Window algebra with per-window timezones, bucketed spread filter, point-in-time calendar, gates G1-G7 |
| `analysis/` | Tier 1 (`structure`, `ict`, `sr`, `regime`) and Tier 2 (`quarterly`, `mmxm`, measured but sealed); `registry.py` memoises per bar close and refits KDE levels on D1 close only |
| `signals/model.py` | L2 logistic + isotonic calibration in numpy; refuses to score on a mismatched feature schema or a training window that ends after `as_of`; hard-errors below 20 events per feature |
| `signals/engine.py` | The cascade as a `Step` enum, so every rejection is attributable to one gate and the funnel is a measurement rather than a guess |
| `signals/geometry.py` | R:R and stop construction; returns a named refusal (`no_target_at_min_rr`, `sl_below_min_atr`, `zero_risk`) instead of a silently degraded signal |
| `backtest/runner.py` | Replays M1 through the live engine, one code path, with a `ledger_digest()` determinism check |
| `backtest/ablation.py` | Pre-committed branch rule (A/B/C), Tier 1 arm order, and a SHA-256 `Tier2Seal` that makes reading Tier 2 before Tier 1 is committed a detectable event |
| `backtest/report.py` | Acceptance criteria evaluated in code, including the 341-trade resolution floor, the tie-break share and the financing-materiality check |
| `notify/telegram/` | MarkdownV2 escaping of all 18 reserved characters, outbox with a UNIQUE dedupe key written in the same transaction as the state change, token-bucket throttle with backoff and a dead-letter path |
| `runtime/` | JSON logging with secret redaction, graceful shutdown, state rehydrated from the DB |
| `tests/causality/` | T1 prefix invariance, applied twice: to the gate decision and to the **full signal cascade** through to an emitted signal's entry, stop, targets and probability. Plus T2 future nulling, T3 time-translation equivariance, T4 resample boundary |

The adapter protocol has **no order-submission method**, and a test asserts it
(`test_adapter_protocol_has_no_order_methods`). Adding execution would require widening the
protocol, which is a visible diff. The Telegram layer is send-only: it has no handler that
can reach `signals/` or `data/`, and the only commands are read-only plus `/pause`.

## Actually running it — and why no alerts have arrived

Nothing has ever been started. There is no `.env`, no database, no logs, and no market data
in this repository, so no Telegram message has been sent or could have been. The code is
built and tested; **the system has never run.** Four things stand between the current state
and a first alert, in order.

**1. Credentials.** Tokens come from the environment only, never from `config.yaml` or the
image. Copy `.env.example` to `.env` and fill it, then check it:

```bash
git clone -b claude/beautiful-fermi-lzdkb1 https://github.com/SyedSharuk16/Aurion.git
cd Aurion
pip install -e ".[dev]"
copy .env.example .env         # Windows cmd;  cp .env.example .env  on bash/macOS
python scripts/setup.py        # verifies the chain and sends you one real message
```

`scripts/setup.py` is Python rather than a shell script so that one tested implementation
runs from `cmd` on Windows, PowerShell, bash and a Linux VPS alike — a script per shell
drifts from whichever one actually gets exercised. It uses only the standard library and
puts the repo on `sys.path` itself, so it runs from a bare checkout before `pip install`
and can tell you *that* is what is missing.

Windows is fine for a first test, but a desktop that sleeps is not an always-on host: the
stream drops, and although the gap is backfilled on reconnect you lose the evaluations that
would have happened. A small Linux VPS is what `deploy/` targets.

`make setup` checks each link in order and stops at the first break, because a later check
passing tells you nothing if an earlier one failed: the env file, the OANDA account (which
it discovers from the token if missing), the preflight, `getMe`, **one real Telegram
message**, then the calendar. The message is the only proof delivery works end to end, and
if it fails on `chat not found` the script tells you to send the bot `/start` — a bot cannot
open a conversation with a user, so until you do that every send is refused.

**It is a long-running service, not a periodic job.** It holds a price stream and evaluates
on bar close; a cron job would miss closes and reconnect constantly. `docker compose` with
`restart: unless-stopped` plus the healthcheck, or `deploy/aurion.service` for systemd, is
what keeps it up across crashes and reboots. Nothing needs to re-trigger it.

The account ID is not derivable from the token and the token only authenticates against one
of OANDA's two hosts, so `make whoami` reads `/v3/accounts` and prints both as `.env` lines
to paste. **Send `/start` to your bot from the account that will receive alerts before you
run anything** — Telegram forbids a bot from opening a conversation, so until you do, every
send fails with 403 and the channel is silent for a reason that has nothing to do with this
system.

`make run` **refuses to start** without a price feed or a delivery channel rather than
running as a process that looks healthy and sends nothing:

```
RuntimeError: refusing to start in mode 'paper' without AURION_OANDA_API_TOKEN,
AURION_OANDA_ACCOUNT_ID, AURION_TELEGRAM_BOT_TOKEN, AURION_TELEGRAM_SIGNAL_CHAT_ID
```

**2. A news calendar.** `news.fail_safe: block` is fixed per the brief, so with no provider
the gate fails closed and **every** signal is suppressed — correct behaviour (a system with
no news feed should not quietly trade through NFP), announced once in the admin channel:

> 📅 Economic calendar unavailable. Under fail_safe=block EVERY signal is suppressed.

So the calendar is not optional if you want signals. Point it at a snapshot on disk:

```bash
AURION_CALENDAR_PROVIDER=file
AURION_CALENDAR_PATH=./data/calendar.json
```

`data/calendar.example.json` is a **template with invented dates**, marked as such in every
title — it loads, and it blocks the wrong minutes. `data/README.md` has the schema. The
field that matters is `published_utc`: it is what makes the snapshot point-in-time, and
setting it equal to `scheduled_utc` is a look-ahead leak through the calendar in any
backtest over past data. A run on a non-point-in-time calendar is marked as such in the
validation report rather than silently accepted. A file rather than a live API on purpose: a
snapshot is reproducible and cannot quietly return different history for a past date.

**3. History.** A cold start backfills the warm-up window (`data.m1_retention_days`, 180
days) in 7-day slices, which is ~259k M1 candles per instrument over ~26 paginated OANDA
requests. Until that completes there is no W1 structure, and the cascade correctly rejects
every evaluation at `bias:htf_structure_unavailable`.

**4. A real emission rate, which is still unknown.** Even running correctly with history and
a calendar, how often this system emits has never been measured on real data — the only
measurement is a synthetic year that emitted nothing, for reasons that are the generator's
(see the funnel under *Known limitations*). Every alert will carry `[PAPER]` until the
acceptance criteria in `docs/04` §8 pass, and they cannot be evaluated without purchased
history. **Expect long silences by design, and do not read silence as "it is working".**
`/status` reports the feed age, the evaluation count and the rejection breakdown, which is
how you tell a working-but-quiet system from a stalled one.

---

## Design commitments

These are properties of the architecture, not aspirations:

1. **Causality is structural, not tested.** Analysis functions cannot reach future data
   because they are not given access to it. `MarketView(as_of=τ)` exposes only bars whose
   `close_time ≤ τ`, and only derived objects whose `confirmed_at ≤ τ`. Tests verify the
   property; the type boundary enforces it. (`docs/01-architecture.md` §3)
2. **One code path, now enforced rather than intended.** The backtester and the live
   runtime differ only in which `Clock` and which `BarSource` they are constructed with.
   There is no `if backtest:` anywhere in `analysis/` or `signals/`
   (`docs/01-architecture.md` §4). This commitment was *violated* in the first build and the
   violation is instructive: both paths built their own `EvaluationContext`, and the two had
   drifted in opposite directions -- the backtester supplied the Silver Bullet window and no
   macro conditioners, the live app supplied macro conditioners and no Silver Bullet window.
   So the backtest would have validated a two-entry-model system with one feature
   distribution while the live process ran a one-model system with another. There is now a
   single `signals/context.py` builder, and a test fails if any other module constructs the
   object at all. A commitment with no mechanism behind it is a hope.
3. **No repainting.** Every derived object carries `(event_time, confirmed_at)`. A swing
   that occurred at `t` is invisible until `t + N` bars have closed, forever, including on
   restart and replay. (`docs/02-definitions.md` §4.1)
   The brief asked for one test of this -- "shift the data by one bar and assert identical
   signals" -- which is false as stated: shifting the input must shift the output. The
   property that does hold is *prefix invariance*: the decision at `t` computed from data
   up to `t` must equal the decision at `t` computed from the full series. That is now
   asserted on the whole cascade, at instants terminating at four different steps including
   an emission, so the entry, stop, targets, probability and expectancy of a real signal are
   all covered -- not just a rejection reason.
4. **No magic numbers, and a derived search budget.** Every threshold lives in
   `config.yaml` with a provenance tag. The budget is on **trials** — any configuration a
   selection decision is taken from — and it is solved from the deflated Sharpe ratio rather
   than chosen: each doubling of the trial count costs roughly 0.15 of annualised Sharpe
   (`docs/07` §4). Only one dimension is searched, an ordinal `selectivity`, because the
   three thresholds it collapses move the candidate population along a single axis.
5. **Ablation decides inclusion, where power allows.** Five module groups are
   selection-eligible; four are built and measured but not selection-eligible in v1; four
   are not built (`docs/07` §7). Whether the sample can resolve even one effect depends on a
   quantity that is still unmeasured, and the pilot measures it before any of this is
   treated as a decision procedure.

---

## Known limitations (current, and expected to persist)

Honest statement of what this system cannot do, written before any results exist so it
cannot be quietly revised afterwards.

**Epistemic**

- **ICT concepts have no canonical definition.** They originate in a discretionary
  pedagogy delivered by video, not a specification. Every definition in
  `docs/02-definitions.md` is *one* operationalisation. Making a concept objective makes it
  *testable*, not *correct*. A failed ablation may mean the concept is useless or that my
  formalisation of it is.
- **Quarterly Theory AMDX phase labelling is intrinsically retrospective.** A quarter's
  phase is identifiable from the completed quarter. Using that label to trade inside the
  same quarter is look-ahead. The system therefore emits partial-quarter phase *scores*
  from closed bars only, which are weaker and differently distributed than the textbook
  labels. See `docs/02-definitions.md` §8.5 and `docs/06-open-questions.md` Q4.
- **The AMDX-per-quarter prior is unvalidated.** "Q1 accumulation, Q2 manipulation,
  Q3 distribution, Q4 continuation/reversal" is an assertion. The system measures the
  empirical phase frequency per quarter index and reports it. If the distribution is near
  uniform, Quarterly Theory carries no information and is disabled.
- **MMXM is a pattern template, not a measurable object.** It is scored 0–1 over five
  stage sub-scores; the stage boundaries are heuristic and flagged as such.

**Statistical**

- **The sample size is the binding constraint, and it may bind completely.** The projected
  trade count is ~276 on the concatenated out-of-sample set (base case; `docs/07` §2). At
  that N, a paired ablation arm resolves effects of 0.13R to 0.31R depending on a dispersion
  term that has not been measured — while the effects I actually predict for these modules
  are 0.03R to 0.15R. **At the pessimistic end, this design cannot resolve any module
  contribution at all**, and the correct response is to ship the base model with the
  ablation reported as description rather than used as a decision procedure. Which end
  applies is the pilot's job to establish.
- **Two Phase 1 acceptance criteria were unmeetable as written and have been corrected.**
  The ≥200-trade holdout requirement (projection: ~69) and the DSR ≥ 0.95 requirement on a
  12-month holdout (unattainable below an annualised Sharpe of ~1.65, regardless of system
  quality) are both replaced; the primary out-of-sample set is now the concatenated
  walk-forward test blocks, and the holdout is a degradation check with its power stated
  (`docs/07` §2, §4).
- Learned confluence weights are therefore fit on **candidate setups** (a low-threshold
  superset, 10–50× larger) rather than on emitted signals alone, with a floor of 20 events
  per feature. Gradient boosting is not used as the primary model at these sample sizes.
- **Rare-pattern modules are unresolvable in principle here, not merely unproven.** A module
  firing on 5% of candidates needs a ~1.4R per-trade effect to be detected at the projected
  N. That is what removed MMXM from selection eligibility and mitigation blocks from the
  build entirely.
- **Two ablation arms are not currently separable, which an ablation requires.** Three
  liquidity features are marked required, and a vector missing a required feature cannot
  emit, so "a sweep exists in the entry window" is a precondition for *every* signal --
  including the Silver Bullet model, which is defined by a time window and carries no sweep
  at all. Removing the liquidity arm would therefore change the Silver Bullet model's firing
  rate, confounding both arms. On synthetic data this terminated 1.7% of post-warm-up
  evaluations; on real data the share is unknown. `docs/06` A17 states the three resolutions
  and why none of them is a code change I should make unilaterally: each one changes the
  validation design. **This needs deciding before the pilot measures a firing rate the
  design then has to live with.**
- **Execution and cost assumptions are assumptions, not settings.** Stop-fill slippage,
  limit-fill priority, the per-leg bid/ask basis and news spread widening are all
  unverifiable without real fills. Every headline statistic is reported under conservative
  and optimistic variants, and if the sign of expectancy changes between them, the honest
  conclusion is that no edge has been demonstrated (`docs/06` A16).
- Walk-forward folds are **purged and embargoed**; overlapping trade horizons otherwise
  leak label information across the fold boundary.
- The reported Sharpe is **deflated** for the number of effectively independent trials.
  Effective trial count is estimated, not assumed equal to the raw grid size.
- **The emission rate — the single number the whole power analysis rests on — is still
  unmeasured.** A full synthetic year on XAUUSD (403,200 M1 bars, 26,879 M15 evaluations)
  emitted **zero signals**. The funnel, after the W1 warm-up elapses (16,811 evaluations):

  | Terminal step | Count | Share of post-warm-up | Dominant reason |
  |---|---|---|---|
  | entry model | 11,670 | 69.4% | `entry:no_model_fired` |
  | HTF bias | 3,741 | 22.3% | `bias:no_directional_structure` |
  | HTF array | 747 | 4.4% | `pd:no_htf_array_in_range` |
  | features | 442 | 2.6% | `warmup:sweep_depth_atr` |
  | session gates | 211 | 1.3% | `session:no_signal_window` |

  A further 10,068 evaluations died earlier, inside the warm-up, at
  `bias:htf_structure_unavailable`. Two full replays of the year produce an identical
  decision digest and an identical funnel, so the replay is deterministic across all 26,879
  decisions — not merely across an empty trade ledger, which is all the earlier check
  compared. **This is a measurement of the generator, not of the
  market**: synthetic drift-plus-noise does not contain the sweep-then-displacement
  structure the entry models require, so a near-zero at the entry model is close to
  expected and tells us nothing about a real emission rate. The run's value was as a
  defect detector, and it found one: the runner never set `silver_bullet_window_start`,
  silently collapsing a two-entry-model design to one across the whole year. With that
  fixed, 653 evaluations now reach past the entry model that previously could not — the
  second model's entire contribution, which the earlier run reported as nothing. Every
  trade-count figure in `docs/07` therefore remains a projection from assumed frequencies,
  and the pilot's first obligation is to replace it with a measurement on purchased
  history. If the measured rate is materially below the base case, the module cut in
  `docs/07` §7 tightens further, possibly to zero selection-eligible modules.

**Operational**

- **Tick volume is not volume.** FX spot has no consolidated tape. Every "volume"-weighted
  quantity (S&R rejection weight, VWAP) uses broker tick counts, which are
  broker-specific, not comparable across venues, and change meaning when the broker changes
  aggregation. Conclusions resting on them are weaker than those resting on price.
- **No real-yield feed is assumed.** The gold/USD context uses a USD index *proxy* built
  from the traded pairs, which is partly self-referential; conflicting-exposure control is
  therefore done by direct USD-beta netting rather than by proxy correlation
  (`docs/02-definitions.md` §10.5).
- **News-calendar fail-safe blocking is a liveness hazard.** Blocking on calendar
  unavailability can silently kill all signals. It is rate-limited and alarmed, and the
  backtest uses a frozen point-in-time calendar snapshot, since replaying today's calendar
  over 2019 data leaks retroactively-added events.
- **Session windows anchored to America/New_York drift against London local time** for the
  ~3 weeks per year when UK and US DST transitions are out of step. Each window declares
  its own timezone for this reason; the default follows the ICT convention (NY) and the
  alternative is a robustness test, not a bug fix.
- Target hardware (1 vCPU / 1–2 GB) cannot hold the full M1 history and run backtests.
  The VPS carries a rolling window; backtesting is an offline activity
  (`docs/01-architecture.md` §8).
- **A flat deployment emits nothing for roughly six months, by construction.** W1 structure
  needs two confirmed fractal swings per side, and a W1 fractal is not confirmed until
  `swing_half_width` weeks have closed on both sides of it. Until that exists the HTF bias
  gate returns no bias and the cascade stops at step 1. This is a consequence of the swing
  definition (`docs/02-definitions.md` §4.1), not a startup bug, and the only remedies are
  backfilled history or a weaker bias definition — the second of which would be a parameter
  change requiring a trial.
- **Backtest throughput is ~5.5 seconds per instrument-week of M1** after optimisation
  (down from 16s: suffix extrema for FVG fill tracking, bisected BPR pairing, one shared ATR
  pass, KDE refit on D1 close only), re-measured after the array-availability fix enlarged
  the candidate array population. A single-instrument five-year walk-forward is therefore
  ~25 minutes of CPU, and the six-instrument grid is the cost that actually constrains how
  many trials are affordable in wall-clock terms — separately from how many are affordable
  statistically.

**Mandatory disclaimer, emitted with every `/stats` response and every validation report:**

> Backtested and paper results are measurements of past data under modelled costs.
> They are not predictions, forecasts, or expected future performance.

---

## Licence / data

Vendor market data is not redistributed. Unit tests run on synthetic, hand-annotated
fixtures committed to the repo; integration tests use a locally cached real dataset
fetched by script into a gitignored directory.
