# 06 — What cannot be defined objectively, and the decisions I need

Your brief asked me to say so where a concept cannot be defined objectively and to propose
the closest testable definition rather than hand-wave. Part A does that. Part B is the list
of forks I cannot resolve without you. Part C pre-registers what I expect to fail, so the
ablation is a test and not a search.

---

## Part A — Concepts with no objective definition

For each: what the concept asserts, why it is not objectively definable, and the closest
testable substitute actually implemented.

### A1. "Smart money" / institutional intent

**Asserts:** that observed price behaviour is caused by the deliberate actions of large
informed participants.

**Why not definable:** participant identity and intent are unobservable in spot FX and gold
CFDs. There is no consolidated tape, no order book from a retail broker, no counterparty
attribution, and no public record of who transacted. No dataset available to this system can
distinguish "a bank engineered that move" from "that is what the price did".

**Substitute:** the system never refers to intent. Every definition in
`docs/02-definitions.md` is pure price-and-time geometry. Where the source material explains
a pattern by intent, I implement the *geometry* and drop the *explanation*. The explanation
is unfalsifiable; the geometry is testable. Names like "smart money reversal" are retained
only as labels for a geometric configuration (§9.2), and that is stated in the docstring.

### A2. "Liquidity" and "liquidity pools"

**Asserts:** resting stop orders accumulate above equal highs and below equal lows, and
price seeks them.

**Why not definable:** we cannot see resting orders. We observe only that price sometimes
moves beyond a prior extreme and reverses.

**Substitute:** `pool` (§6.1) is defined as a *price-geometry object* — a cluster of
confirmed swing extremes within a tolerance — explicitly a **proxy** for where stops may
rest, not a measurement of them. `sweep` (§6.4) is defined as penetration-then-reclaim, a
purely observable event. The inference "stops were there" is never made. If pools carry no
predictive information, the ablation shows it, and the correct conclusion is "this geometry
is uninformative", not "the liquidity was elsewhere" — which is the unfalsifiable move.

### A3. Breaker versus mitigation block

**Why not definable:** the source material uses the two terms inconsistently, sometimes
interchangeably. There is no stable distinguishing criterion to recover.

**Substitute:** I imposed one (§5.4, §5.5): breaker = invalidated OB + opposing structural
break; mitigation block = OB at a failure swing. These are distinguishable and individually
testable. If the ablation cannot separate mitigation blocks from ordinary order blocks, the
concept is dropped. See decision Q6.

### A4. AMDX phase of an in-progress quarter

**Why not definable:** the phase is a description of a completed quarter's shape. At 30%
elapsed, a quarter that will turn out to be "manipulation" is observationally identical to
one that will turn out to be "accumulation". Any confident in-progress label uses the
future.

**Substitute:** two separate objects (§8.5) — a retrospective label on completed quarters
(research and prior-quarter features only) and *running scores* on the in-progress quarter
from closed bars, carrying `elapsed_frac`, treated as different features with separately
fitted coefficients. This is strictly weaker than the textbook construct, and that weakness
is the honest cost of removing the look-ahead.

### A5. The AMDX ordering claim

**Asserts:** Q1 accumulation, Q2 manipulation, Q3 distribution, Q4 continuation/reversal.

**Why not definable:** it is not a definition, it is an empirical claim, and it is
unvalidated in any source I can point to.

**Substitute:** measure it. §8.0 commits to reporting the empirical phase-frequency
distribution per quarter index with confidence intervals. A near-uniform distribution
falsifies the claim and the module is disabled.

### A6. "Original consolidation" and "engineering liquidity" (MMXM)

**Why not definable:** both are described qualitatively ("a range", "a series of higher
lows that traps buyers"). Boundaries are drawn by eye in the source material, and
different analysts mark different ranges on the same chart.

**Substitute:** §9.2 imposes numeric criteria (`C_min` bars, `α_oc` ATR range, ≥2 pools per
side, `E_min` consecutive higher lows). These are arbitrary in their specific values and
are `prior`-tagged, not tuned. The whole of §9 is marked `heuristic`, and MMXM is scored
0–1 rather than flagged, exactly as you specified.

### A7. "True" month open and year open

**Why not definable:** at least three conventions circulate for the month (1st day 18:00 NY,
first Monday 18:00 NY, second Monday 18:00 NY) with no authority between them. The day and
week opens are, by contrast, consistently stated and are implemented as given.

**Substitute:** a config enum with one default (§8.3) and the alternatives run as a
robustness variant. Decision Q4.

### A8. The OTE band (62–79%) and Camarilla's 1.1 divisors

**Why not definable:** these are conventions with no derivation. 0.62 is not even the
Fibonacci ratio it alludes to (0.618).

**Substitute:** the OTE band is a single `tunable` pair with a declared range and the fitted
value reported, so a large drift from [0.62, 0.79] becomes a finding (§6.5). The Camarilla
constants are reproduced exactly and **never tuned** — fitting undocumented folklore
constants is how a formula becomes an overfit (§7.5).

### A9. Displacement magnitude `k`

**Why not definable:** "a large candle" has no canonical threshold. Published examples range
from roughly 1 to 3 ATR.

**Substitute:** `k` is `tunable` with default 1.0 — a deliberately neutral prior (body equal
to average true range) so the default is not a fitted number pretending to be a definition
(§4.3).

### A10. Support/resistance "strength"

**Why not definable:** there is no ground truth for the strength of a level. Nothing can be
measured against.

**Substitute:** `strength` (§7.1) is explicitly defined as a *model output* — a normalised
KDE peak height under stated weights — not a measurement of a market property. Its only
validation is predictive: does a feature derived from it improve out-of-sample expectancy.
It is never described as "how strong the level is".

### A11. The H4 and D1 bar anchor

**Why not definable:** 00:00 UTC, 17:00 NY and 18:00 NY are all in common use and produce
genuinely different swings, FVGs and signals. There is no fact of the matter.

**Substitute:** explicit config with a stated default (§2.3) and a robustness variant.
Decision Q2. This one deserves emphasis: it is not a cosmetic setting. Changing the H4
anchor changes the entire structural object graph, so a result that holds only at one anchor
is an artefact, and the robustness run is how you find that out.

### A12. News "impact" grading

**Why not definable:** vendor-assigned, subjective, inconsistent between providers, and
revised after the fact.

**Substitute:** the gate consumes the vendor's grade as given, records the provider and
revision, and the backtest reports results with the news gate both on and off so the gate's
contribution is visible rather than assumed (§3.5). Decision Q5.

### A13. Real-yield proxy for gold

**Corrected on review. My Phase 1 position was a false binary and is withdrawn.**

I wrote "fake proxy or nothing". That conflated two different claims: there is no free
*intraday* real-yield series, which is true, and there is no free real-yield series at all,
which is false. FRED publishes `DFII10` — the 10-year TIPS constant-maturity yield, an
actual real yield, free, daily — along with `DGS2`, `DGS10` and `T10YIE`. True daily DXY is
likewise freely available.

**Substitute (revised):** these are implemented as **daily regime conditioners** under
honest names — `US10Y_real_d`, `US02Y_nominal_d`, `US10Y_nominal_d`, `US10Y_breakeven_d`,
`DXY_official_d` (`docs/02-definitions.md` §10.3a). Constraints: daily frequency only, flat
within the day, no intraday interpolation, point-in-time by `published_at`, and
`DXY_official_d` is used to report `USDX_PROXY`'s realised correlation — which is the check
§1.2 promised and could not previously perform. What remains genuinely unavailable is an
intraday real-yield series, and no proxy for one will be faked.

### A14. "Rejection volume" and VWAP weights

**Why not definable:** FX spot has no consolidated volume. Tick volume is a broker-specific
update count.

**Substitute:** `docs/02-definitions.md` §0.5 — all tick-volume quantities are normalised
against a time-of-day-bucketed rolling median, tagged `volume_proxy` in the feature schema,
and reported separately in the ablation so they can be removed wholesale if the broker's
aggregation changes.

### A15. The DXY proxy

**Why not definable from the available instruments:** DXY requires SEK and CHF, which are
not in the traded set, so it cannot be reconstructed.

**Substitute:** `USDX_PROXY` (§1.2), named as a proxy, with its realised correlation to true
DXY reported. And critically, it is **not** used as the conflicting-exposure filter you
specified, because a proxy built from EURUSD/GBPUSD/USDJPY cannot independently filter
EURUSD/GBPUSD/USDJPY signals — that is circular. Exposure control uses direct USD-beta
netting instead (§10.5), which is both objective and non-circular.

### A16. Execution and cost assumptions

Promoted to Part A on review. These were specified as config, which understated them: they
are not tunable settings but **unverifiable assumptions**, and they are the assumptions most
capable of manufacturing an edge that does not exist.

**Asserts:** that a modelled fill price is what you would have received.

**Why not definable:** no retail trader can observe the counterfactual. Specifically —

- *Stop-loss fills.* A stop is a market order at a moment of adverse movement. Realised
  slippage depends on the broker's liquidity provider, its internalisation, and its
  discretion during fast markets. There is no public record of retail stop fills, and the
  distribution is venue-specific and changes without notice.
- *Limit-order fill priority.* Whether a limit at the FVG consequent encroachment would have
  filled when price wicked exactly to it is unobservable. It depends on queue position in a
  book we never see — and for a CFD, on whether the broker had an interest in filling it.
- *Bid/ask basis per leg.* Which side of the book each leg transacts on is a modelling
  choice. Mid-price entries and exits are the single largest source of backtest inflation in
  retail FX research, and the error compounds across four legs (entry, SL, TP, force-close).
- *Spread during news.* The widening multiple is broker-specific and heavy-tailed; the mean
  is not the relevant statistic and the tail is what fills stops.

**Substitutes, all of which are assumptions declared as assumptions:**

| Assumption | Treatment | Honest status |
|---|---|---|
| Bid/ask basis per leg | Frozen in `docs/02` §0.3: analysis on mid; long entry and short stop on ask; short entry and long stop on bid; TP on the side that pays | `fixed` — a modelling commitment, not a measurement |
| Stop slippage | `slip_atr × ATR14(M1) + spread`, default 0.10 ATR | `prior`; to be **measured** against real fills if the account ever produces any, and until then an assumption that may be optimistic in fast markets |
| Limit fill | Requires trade-*through* by `fill_margin` points, not touch | `prior`; chosen to be conservative because touch-equals-fill flatters exactly the setups this system looks for |
| Same-M1-bar SL/TP tie | SL assumed first | `fixed` conservative; the **share of trades decided by it is a headline metric**, and a sensitivity run under the optimistic rule bounds the effect |
| News spread widening | `×4.0` for XAUUSD, `×2.0` FX, exponential decay τ=5min | `prior`, and the weakest item here: it is a guess at a heavy-tailed quantity. Replacing it with a measured distribution from ≥6 months of stored bid/ask is a Phase 4 deliverable, not a footnote |

**The reason this belongs in Part A:** if the measured results are sensitive to any of these,
the system's apparent edge is a property of my cost assumptions rather than of the market.
The validation report therefore reports every headline statistic under both the conservative
and optimistic variants of the tie-break and the slippage multiple, and if the sign of the
expectancy changes between them, the honest conclusion is that there is no demonstrated edge.

---

### A17. Three liquidity features are required, which makes a sweep a de-facto global gate

**Found while auditing Phase 4, not specified anywhere.** `sweep_depth_atr`,
`bars_since_sweep` and `sweep_bars_to_reclaim` are marked `required` in the feature schema,
and `features/build.py` populates them only when the entry snapshot contains at least one
sweep. A vector missing a required feature cannot produce a signal (§3.4), so:

> **If no sweep exists anywhere in the entry-timeframe window, no signal can be emitted by
> any entry model, whatever else is true.**

That is a gate. It is not declared as one in `docs/03`'s decision rule, it is not one of
G1–G7, and it does not appear in the funnel as a gate rejection -- it appears as
`warmup:sweep_depth_atr`, which reads as a warm-up problem rather than a structural
precondition. In the synthetic year it terminated 246 of 16,811 post-warm-up evaluations.

**Why this matters beyond tidiness.** The SILVER_BULLET_FVG model is defined by a *time
window* and an FVG; it carries no sweep (`EntryProposal.sweep is None` by construction).
It nonetheless inherits this precondition through the feature vector, so it can only fire in
windows that happen to follow a sweep. That couples a Tier 2 entry model to the liquidity
module, and the coupling runs the wrong way for the ablation: removing the liquidity arm
would change the silver-bullet model's firing rate, confounding both arms. An ablation whose
arms are not separable does not measure what it reports.

**Three resolutions, and I am not choosing between them unilaterally** because each changes
the validation design rather than the code:

1. **Declare it.** Keep the features required and add "a confirmed sweep exists in the entry
   window" to the decision rule as an explicit gate, counted in the funnel as a gate. Honest,
   and it narrows the system to liquidity-driven setups by design. The silver-bullet model
   then needs its definition amended to say so, or dropping.
2. **Make the three optional** (`required=False`). The models decouple, and the fitted model
   must then handle a missing-feature pattern that correlates with the entry model -- which
   at the projected sample sizes is a missingness problem, not a free lunch.
3. **Make them required only for the model that claims them.** Per-entry-model required
   sets. The cleanest semantically and the most code, and it splits the feature schema
   version by model, which the model-usability check would have to carry.

**Pending.** The code is unchanged and the behaviour is as described above. Whichever is
chosen, the measured difference is small in absolute terms (1.7% of post-warm-up
evaluations on synthetic data) and unknown on real data, so this should be decided before
the pilot measures a firing rate the design then has to live with.

## Part B — Decisions I need before Phase 2

**Status: resolved on review.** Verdicts recorded below; the table retains the original
options so the reasoning stays auditable.

| Decision | Verdict |
|---|---|
| Q1, Q2, Q3, Q4, Q5, Q8, Q11, Q12 | **Accepted as proposed.** |
| Q6 Mitigation block | **Dropped.** Unresolvable at this sample size; see `docs/02` §5.5 (definition retained, not built). |
| Q7 Context data | **Accepted, corrected.** Proxy-only for intraday, plus daily real/nominal yields and true DXY under honest names. A13 withdrawn; `docs/02` §10.3a. |
| Q9 Exit policy | **Accepted with the condition applied.** Module selection runs under one declared policy (all-out-at-TP1). Both policies reported; only one selects. |
| Q10 History | **Pilot first.** Purchase sized from measured trades/year by the pre-committed rule in `docs/07` §6. |
| Q13 Tunables | **Re-derived.** The cap of 8 was chosen, not derived, and is deleted. Replaced by a trial budget solved from the deflated Sharpe ratio, with `k`/`g`/`ε` collapsed into one `selectivity` ordinal. `docs/07` §4, §6. |
| Q14 London Close | **Dropped permanently.** Not revisited; no definition will be invented to unblock it. |

Original options and reasoning:

| # | Decision | Options | My recommendation |
|---|---|---|---|
| **Q1** | Session-window timezone anchoring | (a) all windows `America/New_York` (ICT convention); (b) London windows anchored `Europe/London` | **(a)**, with (b) as a robustness variant. The ICT literature means NY clock time; the ~3 weeks/year of DST divergence is a known cost, not a bug. |
| **Q2** | H4 / D1 anchor | (a) freeze at 17:00 NY as `prior`; (b) make it `tunable` over {17:00 NY, 18:00 NY, 00:00 UTC} | **(a)** freeze, and run the other two as robustness checks. Spending a budget slot on it is worse than knowing whether results survive it. |
| **Q3** | Previous day/week extreme convention | (a) `ny_midnight` (00:00 NY day); (b) `broker` (17:00 NY day) | **(a)** — consistent with every other ICT object here. The D1 *bar* stays 17:00-anchored for ATR/pivots and is separately named. |
| **Q4** | Quarterly conventions: weekly partition, monthly partition, `true_month_open` | weekly: Mon–Thu vs Sun-18:00 start; monthly: 7-day blocks vs calendar weeks; month open: 1st-day vs first-Monday vs second-Monday 18:00 NY | **Mon–Thu**, **7-day blocks with residual into Q4**, **first-Monday 18:00 NY**. All three are config enums; all three get robustness runs. |
| **Q5** | Economic calendar | Which provider? Does it expose `published_utc` (point-in-time)? Confirm fail-safe = block | A provider with point-in-time history if you have one; otherwise accept `calendar_point_in_time: false` in the report header and run the news gate both on and off. Keep fail-safe = block, **with** the mandatory alerting of §3.5. |
| **Q6** | Mitigation block | (a) implement and test; (b) drop as indistinguishable from an OB | **(a)**, but on the condition that it is dropped if the ablation cannot separate it from ordinary OBs. It costs ~40 lines; the information value of settling it is worth that. |
| **Q7** | Context data | Real-yield feed: have one? True DXY feed, or proxy only? | Proxy only, `real_yield_source: none`, unless you have a feed. I will report the proxy-vs-DXY correlation so the adequacy is visible. |
| **Q8** | Sizing inputs | account currency, notional account size, risk % | USD, 10,000, 0.5%. Sizing is advisory; these only scale the displayed lots. |
| **Q9** | Exit policy | BE-after-TP1 assumed? TP allocation across TP1/2/3? | Assume BE after TP1 = **true**, allocation **[0.5, 0.3, 0.2]** — and report headline expectancy under *both* the all-out-at-TP1 policy (what the model is trained on) and the scaled policy. |
| **Q10** | History depth | How far back, and from where? OANDA M1 history is paginated and may be shallow for some instruments | Target **≥5 years of M1** with bid/ask. If OANDA cannot supply it for XAUUSD, this is the one place I would spend money on data — the sample size is the binding constraint on everything in `docs/04` §5. |
| **Q11** | Fixture data | May a small real dataset be committed? | **No.** Synthetic hand-annotated fixtures for unit tests; a gitignored cached download for integration tests. Avoids any redistribution question. |
| **Q12** | Storage | (a) SQLite rolling window + Parquet archive; (b) SQLite only; (c) Postgres | **(a)**. (b) does not fit the 1–2 GB VPS with 5 years of M1 (`docs/01` §8 arithmetic). (c) is right if you would rather run one store and pay for a larger box. |
| **Q13** | Tunable budget overflow | The `tunable` set is currently **10**; the cap is **8** | Freeze **`sweep M_sweep` at 3** and the **OTE band at [0.62, 0.79]** as `prior`, testing both as robustness variants. Leaves: spread `N`, displacement `k`, FVG `g`, pool `τ`, sweep `ε`, KDE `h_mult`, SL buffer, `e_min`. |
| **Q14** | London Close window (10:00–12:00 NY, "reversal setups only") | (a) build a third entry model; (b) run the existing two models with `bias` inverted in that window; (c) drop the window | **(c) DROPPED PERMANENTLY.** Not "revisit". A third entry model multiplies the candidate population and the trial burden in a design that `docs/07` §7 shows cannot resolve the modules it already has. No definition will be invented to unblock it. |

Q14 is me declining to implement a part of your brief because it has no definition, and the
refusal is now permanent rather than deferred. "Reversal setups only" was the one item in
the original specification I could not turn into something testable without inventing
content, and the capacity analysis removed the argument for trying: the design cannot
resolve the five modules it already carries.

---

## Part C — Pre-registered predictions, with effect sizes and thresholds

**Rewritten on review.** My first version listed eight direction-only predictions of the
form "contributes nothing detectable". You were right that this is self-confirming: in a
design whose MDE is 0.13–0.31R (`docs/07` §7), a prediction of "no detectable effect" is
satisfied by the design's own weakness and cannot be wrong. It was unfalsifiable and should
not have been written.

The fix has two parts: pre-register **point estimates with intervals** so that being wrong
is possible in both directions, and move the weight of the pre-registration onto the
**pilot's measurable outputs**, which this design genuinely can resolve, rather than onto
ablation effects it mostly cannot.

### C1 — Ablation effects

`Δ` is the change in overall expectancy (R per trade) when the module is **removed**.
Negative Δ means removal hurts, i.e. the module helps. Intervals are my subjective 80%
intervals. "Resolvable" is whether `|Δ_predicted|` exceeds the MDE at the projected
N = 276 (`docs/07` §7).

| Arm | Δ predicted | 80% interval | Falsified if | Resolvable at N=276? |
|---|---|---|---|---|
| Session gating | −0.18 | [−0.45, −0.02] | Δ ≥ +0.05, or Δ ≤ −0.60 | only if σ_d ≤ 0.9 |
| ICT arrays | −0.10 | [−0.30, +0.03] | Δ ≥ +0.10, or Δ ≤ −0.40 | only if σ_d ≤ 0.5 |
| Liquidity pools + sweeps | −0.12 | [−0.35, +0.02] | Δ ≥ +0.10, or Δ ≤ −0.45 | only if σ_d ≤ 0.6 |
| S&R levels | −0.03 | [−0.15, +0.08] | Δ ≤ −0.25 | **no** |
| Regime | −0.08 | [−0.22, +0.04] | Δ ≥ +0.12, or Δ ≤ −0.32 | only if σ_d ≤ 0.4 |
| Quarterly Theory *(Tier 2)* | −0.01 | [−0.10, +0.08] | Δ ≤ −0.20 | **no** |
| MMXM *(Tier 2)* | 0.00 | [−0.08, +0.08] | Δ ≤ −0.18 | **no** |
| `volume_proxy` *(Tier 2)* | −0.01 | [−0.10, +0.08] | Δ ≤ −0.20 | **no** |
| Silver Bullet model *(Tier 2)* | −0.04 | [−0.20, +0.10] | Δ ≤ −0.30 | **no** |

Each "falsified if" clause has a lower bound as well as an upper one. That is the point: a
null prediction is only a prediction if a *large* effect would refute it. Where the
resolvable column says no, the result **cannot be scored in either direction** and will be
reported as unresolved. Counting those as confirmations is precisely the move you flagged,
and the table is built so that it is visible rather than available.

### C2 — Pilot outputs

These are the genuinely falsifiable predictions, because the pilot measures each directly
and none depends on the ablation's power.

| Quantity | Predicted | 80% interval |
|---|---|---|
| P1 XAUUSD completed trades per year | 12 | [4, 35] |
| P1 total completed trades, concatenated OOS, 6 instruments | 276 | [110, 870] |
| P2 per-trade R dispersion `σ_R` | 1.4 | [1.1, 1.8] |
| P3 baseline annualised Sharpe, concatenated OOS | 0.6 | [0.0, 1.3] |
| P4 paired difference dispersion `σ_d` for feature-group arms | 0.9 | [0.5, 1.6] |
| P5 measured trial-Sharpe dispersion, as a ratio to `1/√T` | 0.45 | [0.20, 0.80] |
| Share of trades decided by the same-M1-bar SL/TP tie-break | 3% | [1%, 8%] |
| Share of candidates rejected for `no_target_at_min_rr` | 45% | [25%, 65%] |
| OANDA M1 bid/ask depth available for XAUUSD | < 5 years | — |

### C3 — The consequence prediction

Taking C2 at its point estimates and pushing them through `docs/07` §4: a baseline
annualised Sharpe of 0.6 on 1,260 out-of-sample days affords **one** trial under the
worst-case assumption and about **two** at the predicted trial correlation.

So the prediction I am actually committing to is this: **the pilot will show that this
design cannot afford module selection at all.** The five Tier-1 arms will have to ship on
prior plausibility, with the ablation reported as description rather than used as a decision
procedure, unless at least one of the trade-count levers in `docs/07` §7 is pulled.

If that is right, the honest summary of the finished system is: *a session-filtered,
sweep-and-MSS entry model with ATR-scaled risk and a calibrated probability gate, carrying
five module groups whose individual contributions this quantity of data cannot establish.*
That is a worse-sounding and more useful statement than a confluence score assembled from
everything, and it is the statement I expect to be making.

I would be wrong, and glad to be, if P1 lands at the top of its interval and P4 at the
bottom — roughly 800 trades with σ_d near 0.5 gives an MDE around 0.05R, which resolves
every effect in C1.

### C4 — Scoring rule

Pre-committed so the scoring is not negotiated afterwards:

1. Report every C1 and C2 realised value against its prediction.
2. Count how many 80% intervals contained the realised value. **Calibration is tested in
   both directions:** materially fewer than 80% means I was overconfident; essentially all
   of them means the intervals were too wide to be informative, which is the same failure
   wearing a different hat.
3. C1 rows marked unresolvable are excluded from the count and listed separately as
   unresolved. They are not scored as correct.
4. C3 is scored on whether the affordable trial count derived from the measured P3 and P5
   is ≤ 2.
