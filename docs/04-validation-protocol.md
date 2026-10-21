# 04 — Backtesting and validation protocol

This document is written **before** any results exist, so that the acceptance criteria
cannot be adjusted to fit whatever the first backtest produces. That is the point of
writing it now.

Live alerts untagged by `[PAPER]` are gated on §8. Until then, nothing goes out untagged.

---

## 1. Backtester mechanics

Event-driven, replaying stored M1 bars in timestamp order through the **same**
`analysis/`, `features/` and `signals/` code the live runtime uses (`docs/01-architecture.md`
§4). Differences are confined to the injected `Clock`, `BarSource` and `SignalSink`.

Loop:
```
for each M1 bar b in order:
    ingest b -> resampler -> emit BarClosed events for every timeframe that closed
    for each BarClosed(tf):
        advance MarketView.as_of = b.close_time
        rebuild derived objects (incremental)
        run the signal cascade (docs/03 §1)
    resolve open signals against b  (entry touch, SL, TP, invalidation, expiry)
```

Resolution is always on M1, never on the signal's own timeframe. Resolving a 15-minute
trade on 15-minute bars cannot tell whether the stop or the target came first and
systematically favours the target.

Intrabar SL/TP ambiguity: conservative rule and mandatory reporting, `docs/03` §4.5.

Determinism requirement: two runs over the same data with the same config hash must produce
byte-identical trade ledgers. Any RNG (bootstrap, KDE grid jitter) is seeded from the config
hash. A non-deterministic backtest cannot be debugged and its results cannot be compared.

---

## 2. Cost model

Costs are applied at resolution, never at signal generation, and are configured per
instrument.

### 2.1 Spread

Three modes, in increasing fidelity, all implemented:

| Mode | Source | Use |
|---|---|---|
| `fixed` | one number per instrument | sanity checks only |
| `historical` | the stored bid/ask spread at the fill instant | default when bid/ask M1 is available |
| `modelled` | time-of-day × volatility-bucket distribution, sampled | when only mid M1 is available for part of the history |

`historical` is the default and is why §0.3 of the definitions stores bid and ask rather
than mid. Fills use the correct side of the book (§0.3), so the spread is paid, not modelled
away.

### 2.2 Gold-around-news spread widening

```
spread_effective = spread_base × (1 + news_mult × decay(|t − t_event|))
decay(Δ) = exp(−Δ / news_tau)
```
Defaults for XAUUSD: `news_mult` 4.0, `news_tau` 5 min (`prior`, to be replaced with
measured values from the stored historical spread series once ≥6 months of bid/ask data
exists — and the measurement is a deliverable, not a footnote). For FX: `news_mult` 2.0.

Because the news blackout (§3.5) blocks signals ±30 min around high-impact events, this
widening mostly affects **exits** of trades already open, which is exactly where it matters
and where fixed-spread backtests are most wrong.

### 2.3 Slippage

```
entry (limit order):   0 by default; limit orders do not slip adversely, they fail to fill.
                       Modelled instead as a fill probability (§2.4).
stop loss (market):    slip = slip_atr × ATR14(M1) + spread, in the adverse direction
take profit (limit):   0
force-close (market):  as stop loss
```
`slip_atr` default 0.10 (`prior`). Treating a limit entry as slipping is a common modelling
error that makes a backtest pessimistic on entries and optimistic on exits simultaneously.

### 2.4 Limit-fill realism

A limit entry is filled only if the correct side of the book trades **through** the level,
not merely to it:
```
long limit at P filled iff  min(ask) over the bar ≤ P − fill_margin × point
```
`fill_margin` default 1.0 point (`prior`). Touch-equals-fill overstates fill rates on the
exact setups the system is designed to find — a wick that reaches the FVG CE to the tick and
reverses would otherwise be counted as a winning entry.

### 2.5 Commission / financing

Per-lot-per-side commission from config (default 0 for spread-only accounts). Overnight
financing is **not** modelled, because intraday-only signals with a same-session expiry
rarely cross rollover; the fraction of trades held through 17:00 NY is reported, and if it
exceeds 10% financing must be added before results are trusted.

---

## 3. Walk-forward design

### 3.1 Splits

```
Total history:  as available per instrument (target ≥ 5 years of M1)
Final holdout:  the most recent 12 months — UNTOUCHED until the single final test
Development:    everything before the holdout

Within development: rolling walk-forward
    train 24 months -> validate 3 months -> test 3 months, step 3 months
```
The validate block is where calibration (isotonic) and any hyperparameter choice happen;
the test block is scored and never fitted. Reported performance within development is the
concatenation of test blocks only.

### 3.2 Purging and embargo (non-optional)

Labels span time (a trade opened at `t` resolves at `t + h`). Without correction, a training
event whose label window reaches into the test block leaks the test outcome.

```
purge:    drop any training event whose [t, t+h] overlaps the test block
embargo:  additionally drop training events within `embargo` of the test block start
embargo = max observed h over the development set + 1 bar   (default: 1 trading day)
```

### 3.3 The holdout rule

The 12-month holdout is run **once**, at the end, with every parameter and every
module-inclusion decision already frozen. If the holdout result is poor, the honest
responses are: report it, or declare a new hypothesis and acquire new data. Re-tuning on the
holdout and re-running converts it into a training set and voids every statistic in this
document. This is recorded here so that doing it later is visibly a violation rather than a
judgement call.

---

## 4. Ablation study

### 4.1 Design

Each module group (`ict`, `sr`, `quarterly`, `mmxm`, `regime`, plus `volume_proxy`,
`exposure`, and the `SILVER_BULLET_FVG` entry model) is removed one at a time — its
features are dropped from the feature vector and, where the module gates candidates, its
gate is disabled.

Two comparison regimes, and the distinction decides which statistic applies:

- **Feature-only ablation** (module affects only the model's inputs): the candidate set is
  unchanged, so the comparison is **paired** on the same candidates. Use the per-candidate
  difference in realised R and a paired block bootstrap. Much higher power.
- **Gate ablation** (module changes which trades exist): the two trade sets differ, so the
  comparison is **unpaired**. Use a two-sample stationary block bootstrap on expectancy.
  Lower power.

Where a module does both, both comparisons are reported separately. Collapsing them into
one number hides where the effect comes from.

### 4.2 Decision rule

A module is **enabled by default only if** its removal reduces out-of-sample expectancy by
more than the minimum detectable effect for the available sample size (§5), after
Holm–Bonferroni correction across the ablation family. Otherwise it ships disabled and the
result is reported as "no detectable contribution", **not** as "slightly helpful".

The expected honest outcome, stated in advance so it is not a disappointment later: with a
few hundred to a couple of thousand trades, several of these modules will be
indistinguishable from zero. "Indistinguishable from zero" is a result. It is reported in
the summary table, not relegated to an appendix.

---

## 5. Statistical power — what this sample size can and cannot detect

Per-trade realised R has an empirical standard deviation of roughly 1.2–1.6 for a system
targeting 2R; the table uses `σ = 1.4R`. For the paired case, the standard deviation of the
*difference* is much smaller — `σ_d ≈ 0.5R` is used, to be replaced by the measured value.

Standard error of expectancy = `σ/√N`.
Minimum detectable effect at α = 0.05 two-sided, 80% power: unpaired `3.96·σ/√N`
(= 2.80·σ·√(2/N)), paired `2.80·σ_d/√N`.

| Completed trades N | SE of expectancy | MDE, unpaired | MDE, paired |
|---|---|---|---|
| 250 | 0.089 R | 0.351 R | 0.089 R |
| 500 | 0.063 R | 0.248 R | 0.063 R |
| 1,000 | 0.044 R | 0.175 R | 0.044 R |
| 2,000 | 0.031 R | 0.124 R | 0.031 R |

With 8 ablation tests and Holm–Bonferroni, the critical z rises from 1.96 to ≈2.73, which
inflates every MDE above by about **27%**.

What this means concretely, and it is the single most important number in this document:
**an unpaired ablation on 500 trades cannot detect a true contribution smaller than about
0.31R.** Most individual confluence modules plausibly contribute far less than that. The
study is therefore designed to maximise paired comparisons, and any claim of the form
"module X adds a little" is unsupportable at this sample size and will not be made.

`N` is also not free: it is set by the selectivity of the gates. The validation report
includes the realised trades/week, and if the configured selectivity yields fewer than 200
holdout trades, the holdout result is reported as **inconclusive** rather than as a pass.

---

## 6. Multiple testing

### 6.1 Trial ledger (architectural requirement)

Every backtest run — including throwaway exploratory runs — writes a row:
`(run_id, config_hash, data_window, git_sha, started_at, headline_metrics)`.
The number of trials `N` used in §6.3 is **counted from this table**, not estimated from the
declared grid, because the declared grid always undercounts: the real search includes every
abandoned idea.

Tunable-parameter budget: **8** (`validation.max_tunable_params`). The current `tunable`
set from `docs/02-definitions.md` is: `spread N`, `displacement k`, `FVG g`, `pool τ`,
`sweep ε`, `sweep M`, `KDE h_mult`, `SL buffer`, `OTE band`, `e_min`. That is **10** — two
over budget. Resolving this is decision Q13; my recommendation is to freeze `sweep M` at 3
and the `OTE band` at [0.62, 0.79] as `prior`, testing both only as robustness variants.
`config.yaml` ships that recommendation pre-applied, so the file as delivered declares
exactly 8 `tunable` parameters; overriding Q13 means re-tagging two values and re-checking
this count.

### 6.2 Effective trial count

Raw `N` overstates independence when parameter settings produce correlated return series.
Both are reported:
```
N_raw  = count of rows in the trial ledger
N_eff  = number of eigenvalues of the trial-return correlation matrix required to
         explain 95% of its variance   (floor 1)
```
The headline deflated Sharpe uses `N_raw`, because it is the more conservative of the two
(larger `N` ⇒ larger expected maximum Sharpe under the null ⇒ smaller DSR). `N_eff` is
reported alongside so the degree of conservatism is visible.

### 6.3 Deflated Sharpe ratio (Bailey & López de Prado)

Computed on **daily equity returns in R units** over the holdout.

Probabilistic Sharpe ratio, with `ŜR` the per-observation Sharpe, `T` observations,
`γ̂₃` skewness and `γ̂₄` (non-excess) kurtosis:
```
PSR(SR*) = Φ[ (ŜR − SR*)·√(T−1) / √( 1 − γ̂₃·ŜR + ((γ̂₄ − 1)/4)·ŜR² ) ]
```

Expected maximum Sharpe under the null of zero true Sharpe across `N` trials, with
`V[ŜR_n]` the variance of `ŜR` across the trials, `γ ≈ 0.5772156649` (Euler–Mascheroni),
`e ≈ 2.718281828`:
```
SR* = √( V[ŜR_n] ) · [ (1 − γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)) ]

DSR = PSR(SR*)
```
`DSR` is the probability that the true Sharpe exceeds zero *after* accounting for selection
across `N` trials and for non-normality. Reported with `ŜR`, `T`, `γ̂₃`, `γ̂₄`, `N_raw`,
`N_eff`, `V[ŜR_n]` and `SR*` all shown, so the number can be recomputed by hand.

### 6.4 Metrics reported

Overall and broken out by session, instrument, weekday, quarter index, and volatility
regime:

expectancy (R per trade, with 95% block-bootstrap CI) · profit factor · win rate ·
average win R / average loss R · max drawdown in R · Sharpe · Sortino · deflated Sharpe ·
trades per week · average holding time · fraction resolved at expiry · fraction resolved by
the same-M1-bar tie-break (§1) · fraction held through 17:00 NY · candidate funnel counts
by rejection reason.

A per-cell minimum of 30 trades applies to every breakout cell; cells below it are shown
with the count and no metrics. A "best session" conclusion drawn from 11 trades is noise,
and showing the number prevents reading it as a finding.

---

## 7. Monte Carlo

Two resamplings of the holdout trade sequence, 10,000 paths each:

1. **i.i.d. bootstrap** of per-trade R — the optimistic baseline; assumes no clustering.
2. **Stationary block bootstrap** (Politis–Romano, expected block length 10 trades) —
   preserves win/loss clustering, which is what actually produces drawdowns.

Reported: the distribution of max drawdown in R (median, 90th, 95th, 99th percentiles);
`P(max DD > 10R)`, `P(max DD > 20R)`; the distribution of terminal R; and
`P(a 12-month window is negative)`. The block-bootstrap figures are the headline, because
i.i.d. resampling systematically understates drawdown for any system with serially
correlated outcomes.

---

## 8. Acceptance criteria for removing the `[PAPER]` tag

All of the following, jointly. Any failure keeps the tag.

1. Causality tests T1–T4 pass (`docs/01-architecture.md` §3.3).
2. `pytest` coverage ≥ 80% on `analysis/` (statement **and** branch).
3. Determinism: identical ledgers across two runs at the same config hash.
4. Live-vs-replay reconciliation: 100% of recorded paper signals reproduced exactly by the
   backtester over the same window (instrument, direction, entry, SL, TP1–3, p̂, timestamps).
5. Holdout trades ≥ 200. Below that the holdout is reported as inconclusive.
6. Holdout expectancy > 0 with the 95% stationary-block-bootstrap lower bound > 0.
7. Deflated Sharpe ≥ 0.95 using `N_raw`.
8. The fraction of trades decided by the same-M1-bar tie-break < 5%, or the optimistic-rule
   sensitivity run also passes 6 and 7.
9. ≥ 4 weeks of `[PAPER]` forward running with: zero unhandled exceptions, zero missed
   bar-close evaluations during open sessions, and the live/replay reconciliation of (4)
   holding on the forward window.

**Honest limitation of criterion 9, stated now:** four weeks of paper trading yields on the
order of 10–40 trades. A Kolmogorov–Smirnov test of live versus backtest R distributions on
that sample has almost no power — it will fail to reject almost regardless of the truth. The
paper-forward period therefore tests **engineering correctness** (does it see the bars, fire
at the right instants, compute the same prices, track lifecycle correctly) and not
statistical agreement. It must not be reported as confirming the backtest. Detecting a
genuine distributional divergence needs hundreds of live trades, which is quarters, not
weeks.

---

## 9. Live-vs-replay reconciliation

Nightly, the backtester re-runs the previous day's window from stored M1 bars and compares
its signal set against what the live runtime actually emitted. Any difference is a hard
admin alert, because there are only three possible causes and all are serious: a
non-deterministic analysis function, state that was not restored correctly, or a live data
gap that the backfill repaired differently than the live stream saw it.

This is also the test that keeps "single code path" true over time. Without it, a
`if backtest:` branch can be introduced and go unnoticed for months.

---

## 10. Report artifact

`reports/validation_<git_sha>_<config_hash>.md` plus a machine-readable JSON sibling,
containing every table in §6.4, the ablation matrix with per-module paired/unpaired
statistics and corrected p-values, the Monte Carlo distributions, the DSR inputs, the trial
ledger summary, and the candidate funnel.

Every report begins with:

> Backtested and paper results are measurements of past data under modelled costs.
> They are not predictions, forecasts, or expected future performance.

The same string is a module-level constant, emitted by `/stats`, and asserted present by a
unit test — so it cannot be removed by editing a template.
