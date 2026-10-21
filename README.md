# Aurion

Signal-only (alert-generating) trading analysis system for XAUUSD and FX majors.
**It never places, modifies, or cancels an order.** There is no execution code path and
no broker order endpoint is referenced anywhere in the design.

---

## Status: Phase 1 — architecture and definitions, awaiting review

Nothing in this repository is executable yet. That is deliberate: the agreed delivery
plan gates code behind approval of the written definitions, because every module in
Phase 3 is a literal transcription of a definition in `docs/02-definitions.md`. If a
definition is wrong, the code is wrong in a way tests cannot detect.

| Phase | Contents | State |
|---|---|---|
| 1 | Architecture, exact definitions, config surface, validation protocol, open questions | **Delivered — review required** |
| 2 | Data adapters (OANDA v20 first), resampler, session engine, tests | Blocked on Phase 1 approval |
| 3 | Analysis modules (`structure`, `ict`, `sr`, `quarterly`, `mmxm`, `regime`) + tests | Blocked |
| 4 | Event-driven backtester, walk-forward, ablation study, validation report | Blocked |
| 5 | Signal engine, Telegram, always-on runtime, Docker/systemd | Blocked |

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
| `docs/06-open-questions.md` | Everything that **cannot** be defined objectively, the closest testable substitute, and the 14 decisions I need from you |
| `config.yaml` | Every threshold in the system, each tagged `fixed` / `prior` / `tunable` |

---

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
4. **No magic numbers.** Every numeric threshold lives in `config.yaml` with a provenance
   tag and, if `tunable`, a declared search range that is counted toward the multiple-testing
   correction. The optimisation budget is capped at 8 tunable parameters
   (`docs/04-validation-protocol.md` §6).
5. **Ablation decides inclusion.** ICT arrays, S&R, Quarterly Theory, MMXM and the regime
   layer are hypotheses. Each ships **disabled** and is enabled only if it improves
   out-of-sample expectancy by more than the minimum detectable effect for the available
   sample size. Negative results are reported, not buried.

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

- **The sample size is the binding constraint, not the feature count.** A selective entry
  model across 6 instruments plausibly yields 300–2,000 completed trades over the full
  history. At a per-trade R standard deviation of ~1.4, the standard error of expectancy at
  N=500 is 0.063R, so effects smaller than roughly 0.18R are not detectable
  (`docs/04-validation-protocol.md` §5). Many module contributions are plausibly smaller
  than that. The honest outcome for several modules is "indistinguishable from zero", not
  "helps a little".
- Learned confluence weights are therefore fit on **candidate setups** (a low-threshold
  superset, 10–50× larger) rather than on emitted signals alone, with a floor of 20 events
  per feature. Gradient boosting is not used as the primary model at these sample sizes.
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
