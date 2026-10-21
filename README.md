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
| `docs/06-open-questions.md` | The 16 concepts that **cannot** be defined objectively and their testable substitutes; the 14 decisions (all resolved); pre-registered predictions with effect sizes |
| `docs/07-power-and-capacity.md` | Derived trial budget, minimum detectable effects, the module cut, the pilot spec, and two corrections to Phase 1 |
| `tools/power.py` | Runnable calculator behind every number in `docs/07` |
| `config.yaml` | Every threshold in the system, each tagged `fixed` / `prior` / `derived` |

---

## Running it

```bash
make install     # editable install with dev extras
make causality   # the four causality tests -- run these first
make all         # lint + types + coverage + causality
make power       # regenerate every table in docs/07
```

Current state: **408 tests**, `ruff` clean, `mypy --strict` clean on 83 source files.
Branch coverage by package, because the aggregate hides the distribution:

| Package | Coverage | |
|---|---|---|
| `analysis/` | **91%** | the contractual gate is 80% on analysis modules (`docs/04` §8) |
| `core/` | 93% | |
| `sessions/` | 92% | |
| `data/` | 88% | |
| `signals/` | 76% | `engine.py` 49%, `entries.py` 42% -- see below |
| `backtest/` | 72% | |
| `notify/` | 72% | |
| `runtime/` | **15%** | `app.py` is the composition root: asyncio wiring, reconnect loops, signal handlers |
| Total | 81% | |

The two weak numbers are stated rather than papered over. `signals/engine.py` at 49% and
`runtime/app.py` at 15% are both covered end-to-end by the backtest runner replaying M1
through the real engine, but a unit-coverage counter does not see that, and unit tests for an
asyncio composition root mostly assert that mocks were called. The honest reading is that the
engine's *decision* logic is well covered and its *wiring* is not, and the wiring is what the
Phase 2E pilot exercises first.

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
| `tests/causality/` | T1 prefix invariance (on the gate decision), T2 future nulling, T3 time-translation equivariance, T4 resample boundary |

The adapter protocol has **no order-submission method**, and a test asserts it
(`test_adapter_protocol_has_no_order_methods`). Adding execution would require widening the
protocol, which is a visible diff. The Telegram layer is send-only: it has no handler that
can reach `signals/` or `data/`, and the only commands are read-only plus `/pause`.

## Design commitments

These are properties of the architecture, not aspirations:

1. **Causality is structural, not tested.** Analysis functions cannot reach future data
   because they are not given access to it. `MarketView(as_of=τ)` exposes only bars whose
   `close_time ≤ τ`, and only derived objects whose `confirmed_at ≤ τ`. Tests verify the
   property; the type boundary enforces it. (`docs/01-architecture.md` §3)
2. **One code path.** The backtester and the live runtime differ only in which `Clock` and
   which `BarSource` they are constructed with. There is no `if backtest:` anywhere in
   `analysis/` or `signals/`. (`docs/01-architecture.md` §4)
3. **No repainting.** Every derived object carries `(event_time, confirmed_at)`. A swing
   that occurred at `t` is invisible until `t + N` bars have closed, forever, including on
   restart and replay. (`docs/02-definitions.md` §4.1)
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
  unmeasured.** A full synthetic year on XAUUSD produced 26,879 evaluations and **zero
  emitted signals**: 51.4% died at the HTF bias gate, 45.8% at the entry model, 2.8% at the
  HTF array check. Two causes, only one of them fixable. The synthetic generator produces
  drift and noise but not the sweep-then-displacement structure the entry models require, so
  a zero there is close to expected. The other was a real defect: the runner never set
  `silver_bullet_window_start`, which silently collapsed a two-entry-model design to one
  across the entire run. That is fixed and pinned by a test. But every trade-count figure in
  `docs/07` remains a projection from assumed frequencies, and the pilot's first obligation
  is to replace it with a measurement. If the measured rate is materially below the base
  case, the module cut in `docs/07` §7 tightens further, possibly to zero
  selection-eligible modules.

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
- **Backtest throughput is ~5.7 seconds per instrument-week of M1** after optimisation
  (down from 16s: suffix extrema for FVG fill tracking, bisected BPR pairing, one shared ATR
  pass, KDE refit on D1 close only). A single-instrument five-year walk-forward is therefore
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
