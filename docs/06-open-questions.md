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

**Why not definable at acceptable quality:** there is no free, reliable, intraday real-yield
series. TIPS-ETF prices and nominal-minus-breakeven constructions are low-frequency,
instrument-specific, and carry their own basis.

**Substitute:** `regime.real_yield_source: none` by default. If you supply a feed it becomes
a feature; absent one, it is **absent**, not approximated (§10.3). I will not ship a fake
proxy and call it a real-yield correlation.

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

---

## Part B — Decisions I need before Phase 2

Each has my recommendation. If you accept all recommendations, reply "accept defaults" and
I will proceed on them.

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
| **Q14** | London Close window (10:00–12:00 NY, "reversal setups only") | (a) build a third entry model; (b) run the existing two models with `bias` inverted in that window; (c) drop the window | **(c) drop for now.** "Reversal setups only" is not yet an objective entry model, a third model multiplies the candidate population and the multiple-testing burden, and (b) is an untested belief dressed as a configuration. Revisit after the first ablation, with a written definition. |

Note that Q14 is me declining to implement a part of your brief until it has a definition.
"Reversal setups only" is the one item in the original specification that I could not turn
into something testable without inventing content. Tell me what a reversal setup is and I
will specify and build it; otherwise it stays out, visibly, rather than being quietly
approximated.

---

## Part C — Pre-registered predictions

Recorded now so the ablation is a test rather than a search. I will report these against
the actual results in the Phase 4 report, including where I was wrong.

| Prediction | Confidence |
|---|---|
| Rolling Hurst contributes nothing detectable and is ablated out | high |
| Camarilla pivots contribute nothing detectable | high |
| The `volume_proxy` group (VWAP z, rejection weights) contributes nothing detectable | moderate |
| Quarterly Theory's running AMDX scores contribute nothing detectable **on the in-progress quarter** (the retrospective labels will look informative, which is the look-ahead artefact) | moderate-high |
| The empirical AMDX phase distribution per quarter index will be closer to uniform than to the A→M→D→X ordering | moderate |
| MMXM score contributes nothing detectable, because Stage-3 confirmation is rare enough that the sub-sample is too small to measure | moderate |
| The liquidity-sweep features (depth, recency) **do** carry detectable information — this is the most mechanically plausible part of the ICT set, since penetration-then-reclaim is a genuine observable | moderate |
| Session/time-of-day features carry detectable information, mostly as a volatility proxy rather than as "kill zones" | moderate |
| The `rr_min = 2` gate rejects the majority of structurally valid setups, and relaxing it improves expectancy | moderate |
| Fewer than 800 completed holdout trades will be available across all six instruments, leaving most individual module effects below the detectable threshold | high |

If most of the high-confidence predictions hold, the honest summary of this system will be
"a session-filtered, sweep-and-MSS entry model with ATR-scaled risk, plus eight modules that
could not be shown to help." That is a worse-sounding and more useful result than a
confluence score built from everything.
