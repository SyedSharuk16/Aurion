# Aurion

Signal-only (alert-generating) trading analysis system for XAUUSD and FX majors.
**It never places, modifies, or cancels an order.** There is no execution code path and
no broker order endpoint is referenced anywhere in the design.

---

## Status: Phase 1 approved; Phase 1.5 (power and capacity) delivered

| Phase | Contents | State |
|---|---|---|
| 1 | Architecture, exact definitions, config surface, validation protocol, open questions | **Approved** |
| 1.5 | Power, trial capacity, the module cut, pilot specification (`docs/07`, `tools/power.py`) | **Delivered — review required** |
| 2 | Data adapters (OANDA v20 first), resampler, session engine, schema, tests | **Delivered** |
| 2b | Macro daily series adapter (FRED), backfill supervisor, reconnect/gap repair | Next |
| 2E | **Power pilot** (`docs/07` §5) — XAUUSD only, gates data purchase and trial budget | Exit gate on Phase 2 + 3a |
| 3 | Analysis modules: `structure`, `ict`, `sr`, `regime` (Tier 1) and `quarterly`, `mmxm` (Tier 2, observational) + tests | Blocked |
| 4 | Event-driven backtester, purged walk-forward, ablation, validation report | Blocked |
| 5 | Signal engine, Telegram, always-on runtime, Docker/systemd | Blocked |

The pilot is deliberately placed *after* the data layer and a minimal analysis slice,
because generating a single candidate requires the resampler plus structure, arrays and
liquidity. It cannot precede Phase 2, and claiming otherwise would mean fabricating the
trade count it exists to measure.

Live alerts are gated behind Phase 4 passing its own acceptance criteria
(`docs/04-validation-protocol.md` §8), and `[PAPER]`-tagged forward running for ≥4 weeks
before the tag is removed.

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

## Running it (Phase 2)

```bash
make install     # editable install with dev extras
make causality   # the four causality tests -- run these first
make all         # lint + types + coverage + causality
make power       # regenerate every table in docs/07
```

Current state: 153 tests, 92% branch coverage, `ruff` clean, `mypy --strict` clean.
`analysis/primitives.py` is at 96%, above the 80% gate in `docs/04` §8.

What exists and is tested:

| Component | Notes |
|---|---|
| `core/timeframe.py` | DST-correct bucketing. Boundaries are generated in the anchor's local time, so H4/D1/W1 produce exactly one irregular bar per DST transition (3h/5h on H4, 23h/25h on D1) and they are flagged, not normalised |
| `core/marketview.py` | The causality boundary. No accessor returns anything later than `as_of`; forming-bar access needs an explicit opt-in |
| `core/clock.py` | `SimClock` refuses to move backwards -- a backwards step is how replay reveals future data |
| `data/resample.py` | Incremental, O(1) per candle. Emits only at close, flags thin buckets, never emits empty ones |
| `data/store.py` | SQLAlchemy 2.x async, idempotent upserts, gap detection, retention pruning, trial ledger |
| `data/adapters/oanda.py` | Read-only, `price=BA`, drops incomplete candles, refuses any granularity above M1 |
| `sessions/` | Window algebra with per-window timezones, bucketed spread filter, point-in-time calendar, gates G1-G7 |
| `tests/causality/` | T1 prefix invariance (on the gate decision), T2 future nulling, T3 time-translation equivariance, T4 resample boundary |

The adapter protocol has **no order-submission method**, and a test asserts it
(`test_adapter_protocol_has_no_order_methods`). Adding execution would require widening the
protocol, which is a visible diff.

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

**Mandatory disclaimer, emitted with every `/stats` response and every validation report:**

> Backtested and paper results are measurements of past data under modelled costs.
> They are not predictions, forecasts, or expected future performance.

---

## Licence / data

Vendor market data is not redistributed. Unit tests run on synthetic, hand-annotated
fixtures committed to the repo; integration tests use a locally cached real dataset
fetched by script into a gitignored directory.
