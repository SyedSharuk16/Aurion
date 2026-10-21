# 07 — Power, trial capacity, and the module cut

Inserted before Phase 2 at your request. Everything here is computed by
`tools/power.py`, which is committed and re-runnable, so no number in this document is
hand-arithmetic and every one of them moves when its assumptions do.

Two of the results contradict the Phase 1 specification I wrote. Both are stated as
corrections, not refinements.

---

## 1. What I can and cannot do right now

You asked me to *run* the base model on one instrument and report trades/year. I cannot,
and the reason is not reluctance:

- There is no market data in this environment and no broker credential.
- The base model is sweep → MSS → FVG/OB retrace. Generating one candidate requires the
  resampler (Phase 2) plus `analysis/structure`, `analysis/ict` and the liquidity module
  (Phase 3a). The pilot is therefore **downstream of Phase 2**, not upstream of it.

What I have done instead, so the pilot answers your question the moment it can run:

1. Derived the full capacity framework, with the module cap and the tunable cap falling
   out of sample size and trial count rather than being chosen (§3, §4).
2. Produced an a-priori trade-rate projection from an explicit funnel, clearly labelled as
   assumption arithmetic (§2).
3. Specified the pilot exactly — what it runs, on what, and the five quantities it must
   output (§5).
4. Pre-committed the decision rules that convert those five numbers into the Q10 data
   purchase and the Q13 trial budget, so neither is a judgement call after the fact (§6).
5. Cut the module list to what the budget supports, under the projection, with the cut
   re-derived automatically when the pilot replaces the projection (§7).

The projection in §2 is the weakest link in this document and I am not going to disguise
that. It is a product of eight assumed conditional probabilities. Treat it as establishing
an **order of magnitude** — hundreds, not thousands of trades — which is enough to settle
the design questions, and not enough to settle the data purchase. That is why §6 gates the
purchase on the pilot and not on §2.

---

## 2. Projected trade count (assumption arithmetic)

Funnel, per instrument, in-window only. Every factor is a declared assumption:

```
sweeps of some pool per in-window day      1.5
P(MSS within 10 bars, in HTF-bias dir)     0.33
P(W1/D1 bias agree)                        0.55
P(HTF PD array within 2 ATR)               0.50
P(target exists at R >= 2)                 0.50
P(spread ok)                               0.90
P(not news-blocked)                        0.93
P(survives one-active-per-instrument)      0.80
```

```
scenario    | /instr/yr | total(6 instr, 5y) | 12m holdout | concat WF OOS (4y)
------------|-----------|--------------------|-------------|-------------------
pessimistic |       4.6 |                139 |          28 |               111
base        |      11.5 |                345 |          69 |               276
optimistic  |      36.2 |              1,086 |         217 |               869
```

**Correction 1 to Phase 1.** `docs/04` §8 criterion 5 required ≥200 trades in the 12-month
holdout. The base case yields **69**. That criterion is unmeetable as written, and keeping
it would mean the holdout is declared inconclusive by construction.

The fix is not to lower the threshold. It is to change which set is the primary evidence:

- **Primary out-of-sample set: the concatenation of all walk-forward test blocks**
  (≈4 years, ≈276 trades base case). This is genuine out-of-sample data — each test block
  was never fitted — and it is four times the size of the holdout.
- **The 12-month holdout becomes a degradation check, not a significance test.** It is the
  only entirely untouched data, so it remains valuable for detecting gross breakage, and it
  is reported with its power stated explicitly. At 69 trades it cannot establish
  significance and will not be presented as doing so.

---

## 3. What an ablation arm can resolve

Paired comparison under common random numbers, restricted to the affected subset, exactly
as you specified. The module changes the outcome on a fraction `f` of candidates and leaves
the rest byte-identical, so

```
overall effect       Δ  = f · d̄                 (d̄ = mean diff on the affected subset)
SE(Δ̂)                   = σ_d · √(f / N)
MDE(Δ)                  = (z_{1−α/2M} + z_power) · σ_d · √(f / N)
```

At N = 276 (base-case concatenated OOS), σ_d = 1.5R, a family of 6 tests, α = 0.05,
power 0.80:

```
f     | MDE on Δ (R) | implied per-affected-trade effect
------|--------------|----------------------------------
0.05  |        0.070 | 1.405 R
0.10  |        0.099 | 0.994 R
0.25  |        0.157 | 0.628 R
0.50  |        0.222 | 0.444 R
1.00  |        0.314 | 0.314 R
```

Read the **right-hand column**, not the middle one. A low `f` makes the detectable
*overall* effect look small, but it is small only because the module barely fires; the
per-trade effect it must have on the trades it does touch grows as `1/√f`. A module firing
on 5% of candidates needs a 1.4R per-trade effect to be resolvable — which is not a
plausible effect size for a confluence filter.

**Consequence: only high-`f` modules are resolvable at this sample size.** That is a
structural fact about the design, and it decides the cut in §7: rare-pattern modules
(MMXM's Stage-3 confirmation, mitigation blocks) are unresolvable in principle here, not
merely unproven.

Inverting, for a target overall MDE at `f = 0.5`:

```
MDE target (R) | trades required
---------------|----------------
0.20           |            341
0.15           |            606
0.10           |          1,363
0.05           |          5,450
0.03           |         15,137
```

Unpaired comparison, for contrast: MDE at N=276 is roughly **0.44R**. Your instinct on
paired-CRN-on-the-affected-subset is worth a factor of 2–6 in resolvable effect size and is
the only reason this design can resolve anything at all.

---

## 4. The trial budget, derived

Your Q13 challenge was correct: 8 tunable parameters was chosen, not derived. The derivation
runs through the deflated Sharpe ratio, and it does not produce a cap on *parameters* — it
produces a cap on **trials**, where a trial is any configuration from whose results a
selection decision is made. Ablation arms are trials. Parameter settings are trials.

For DSR ≥ 0.95 with `T` out-of-sample daily observations and `N` trials:

```
SR*      = √(V[ŜR_n]) · [ (1−γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)) ]      γ = 0.5772157
required ŜR_daily ≥ SR* + Φ⁻¹(0.95)/√(T−1)
required SR_ann   = √252 · required ŜR_daily
```

`√(V[ŜR_n])` is the dispersion of the Sharpe **across the trials actually run**. Setting it
to `1/√T` treats trials as independent draws under the null, which is the worst case;
correlated trials (the normal situation — ablating a weak module barely moves the series)
make it smaller and the penalty milder. Both are reported because the difference is large.

Required observed annualised Sharpe, T = 1,260 OOS days (5 years), independent trials:

```
trials N | required SR_ann | example composition
---------|-----------------|---------------------------
       1 |            0.74 | no search at all
       4 |            1.21 | 1 param setting + 3 arms
       6 |            1.32 | 1 param setting + 5 arms
       8 |            1.39 | 3 param settings + 5 arms
      12 |            1.48 | 3 param settings + 9 arms
      20 |            1.59 | 5 param settings + 15 arms
      64 |            1.80 |
     256 |            2.01 |
```

**The exchange rate: each doubling of the trial count costs about 0.15 of annualised
Sharpe.** That is the single most useful number here. It prices every additional module and
every additional parameter level in the currency the system is trying to produce.

Largest affordable `N` by out-of-sample length and attainable Sharpe:

```
independent trials (worst case)
OOS days     | SR=0.8 | SR=1.0 | SR=1.2 | SR=1.5 | SR=2.0
-------------|--------|--------|--------|--------|-------
252   (1y)   |      0 |      0 |      0 |      0 |      1
756   (3y)   |      0 |      1 |      1 |      3 |     16
1260  (5y)   |      1 |      2 |      3 |     13 |    241

trial correlation √V = 0.5/√T
252   (1y)   |      0 |      0 |      0 |      0 |      2
756   (3y)   |      0 |      1 |      3 |     20 |  4,091
1260  (5y)   |      1 |      4 |     30 |  1,790 |      —
```

**Correction 2 to Phase 1.** `docs/04` §8 criterion 7 required DSR ≥ 0.95 **on the 12-month
holdout**. The T=252 row shows that is unattainable at any plausible Sharpe — zero trials
are affordable below SR_ann ≈ 1.65, and PSR(0) alone caps out at ≈0.84 for SR_ann = 1.0.
The criterion as written could never have been met, independent of how good the system is.

Fix: **DSR is computed on the concatenated walk-forward out-of-sample series**, not the
holdout. This also aligns the DSR evaluation set with the primary evidence set from §2.

Note what the table does *not* say. It is a statement about what the DSR *threshold* can
certify, not about what is true. A system with a real Sharpe of 0.9 is a useful system and
this test will refuse to certify it on five years of data. That is the cost of controlling
selection bias at this sample size, and the right response is to report the DSR honestly
alongside the raw statistics rather than to abandon either.

---

## 5. The pilot specification

Runs at the end of Phase 2 plus the minimum Phase 3 slice (`structure`, `ict`, liquidity).
Single instrument: **XAUUSD** — highest priority, widest session coverage, and the
instrument whose history is most likely to be short.

Fixed configuration: all parameters at their `prior` defaults, no tuning, no model. The
decision rule for the pilot is the hard gates only (G1–G7, `rr_min`, `sl_min_atr`) with
`p̂` replaced by a constant, so the pilot measures the **candidate and trade rate of the
geometry**, not of a fitted model.

Required outputs — these five numbers, and nothing about them is optional:

| # | Quantity | Used for |
|---|---|---|
| P1 | Candidates and completed trades per instrument-year, with the full rejection funnel by gate | Q10 purchase sizing; sanity-checking §2 |
| P2 | Realised per-trade R dispersion `σ_R` | unpaired MDE; replaces the assumed 1.4 |
| P3 | Baseline annualised Sharpe on the concatenated OOS, with `γ̂₃`, `γ̂₄` | trial budget via §4 |
| P4 | Per module: affected fraction `f` and paired difference dispersion `σ_d,aff` | per-arm MDE via §3 |
| P5 | Trial-return correlation → measured `√(V[ŜR_n])` | whether the mild or worst-case budget column applies |

Also reported, because they bound credibility independently: share of trades resolved by the
same-M1-bar SL/TP tie-break, share held through 17:00 NY, share of candidates rejected for
`no_target_at_min_rr`, and the realised M1 bid/ask history depth per instrument.

---

## 6. Pre-committed decision rules

Written now so that neither decision is made after seeing results it could be bent to fit.

### Q10 — data purchase

```
target     := trades needed to resolve a 0.15 R overall effect at f = 0.5  =  606   (§3)
available  := P1_trades_per_instrument_year × instruments × (free_years − 1)

if available >= target:                      do not buy history
elif more history exists for purchase:       buy ceil(target/(P1 × instruments)) + 1 years, capped at what exists
else:                                        do not buy; instead declare the design resolves
                                             only effects >= 0.25 R, cut arms to 3, and say so
                                             in the report header
```
`0.15R` is itself a choice, so it is declared here rather than derived: it is roughly the
smallest per-trade edge that survives the cost model's own uncertainty, and anything smaller
is not worth building an alert system around.

### Q13 — trial budget and tunables

```
N_affordable := max_trials(T = concat OOS days, SR_ann = P3, √V = P5)        (§4)
arms         := N_affordable − param_settings
param_settings := 3     (one collapsed `selectivity` dimension; see below)

if arms < 3:  freeze selectivity too (param_settings = 1) and re-solve
if arms < 2:  report that the design cannot support module selection at this sample size,
              ship the base model with every module disabled, and stop
```

`max_tunable_params: 8` is **deleted** from `config.yaml` and replaced by
`validation.max_trials`, derived as above.

**On the non-orthogonality you flagged.** `displacement_k`, `fvg_min_atr` and
`sweep_min_penetration_atr` jointly move the candidate population in the same direction —
all three trade candidate count against candidate quality. Varying them independently
spends three dimensions of a budget that can afford one. They are therefore collapsed into
a single ordinal `selectivity ∈ {loose, base, tight}` with declared joint settings:

```
            displacement_k   fvg_min_atr   sweep_min_penetration_atr
loose                 0.75          0.05                        0.02
base                  1.00          0.10                        0.05
tight                 1.50          0.20                        0.10
```

Everything else previously tagged `tunable` — `spread.max_median_mult`,
`pool_tolerance_atr`, `kde_bandwidth_atr`, `sl_buffer_atr`, `expected_r_min` — is reclassified
`prior`. `expected_r_min` deserves a specific note: it is a decision threshold applied to a
fixed candidate-and-prediction set, so sweeping it costs no extra backtest run, but
*choosing* a value from the sweep is still selection. It is therefore frozen at 0.15 a
priori and the full expectancy-versus-threshold curve is reported as a diagnostic that was
explicitly not used for selection.

---

## 7. The module cut

Under the base-case projection (N≈276, so §3 says only `f`≈1 modules are resolvable) and a
trial budget of 8 (3 selectivity settings + 5 arms, requiring SR_ann ≈ 1.39):

### Tier 1 — selection-eligible, 5 arms

| Arm | `f` | Why it is in the budget |
|---|---|---|
| Session gating (windows on/off) | ≈1.0 | The backbone of the design; if it does not pay, little else matters |
| ICT arrays (FVG/OB/breaker/BPR features) | ≈1.0 | The entry model is built on them; highest-leverage test |
| Liquidity pools and sweeps | ≈1.0 | Penetration-then-reclaim is the most mechanically plausible ICT observable |
| S&R levels (KDE + pivots + round numbers) | ≈1.0 | Documented prior outside the ICT tradition |
| Regime (volatility + trend/MR) | ≈1.0 | Best-supported prior of the lot; volatility conditioning is well established |

### Tier 2 — built and measured, **not** selection-eligible in v1

Quarterly Theory · MMXM · `volume_proxy` (VWAP z, rejection weights) · the
`SILVER_BULLET_FVG` entry model.

These are implemented and their effects are measured and reported with confidence intervals,
which costs **no trial budget** — building and observing is free; only *selecting* on a
result spends budget. Their v1 results are frozen as observations. Acting on any of them
requires a new pre-registered test on data not used here. MMXM additionally fails §3 on its
own terms: Stage-3 confirmation is rare, so `f` is small and the per-affected-trade effect
it would need is implausible. Silver Bullet is demoted because its `f` is the share of
trades it originates (projected 0.2–0.3), which puts its resolvable effect near 0.6R.

### Tier 3 — not built

| Dropped | Reason |
|---|---|
| Mitigation block | Q6, your call — unresolvable at this sample size, so the 40 lines buy nothing |
| Camarilla as a standalone arm | Folded into the S&R arm; its constants are never tuned |
| Rolling Hurst as a standalone arm | Folded into the regime arm; its own standard error exceeds the effect it would measure |
| London Close / reversal model | Q14, permanent — not revisited |

### Which constraint actually binds

Two separate constraints had to be satisfied, and I guessed wrong about which one binds.

- **Trial budget** (deflation, §4) affords **5 arms** alongside 3 selectivity settings.
- **Power** (§3) affords, at `f = 1.0` and N = 276, an MDE that depends entirely on `σ_d` —
  the dispersion of the paired difference on the affected subset, which is **unmeasured**:

```
σ_d  | MDE at N=276 | at N=606 | at N=1211 | at N=2725
-----|--------------|----------|-----------|----------
0.4  |        0.084 |    0.057 |     0.040 |     0.027
0.6  |        0.126 |    0.085 |     0.060 |     0.040
1.0  |        0.209 |    0.141 |     0.100 |     0.067
1.5  |        0.314 |    0.212 |     0.150 |     0.100
```

The effects I actually predict for these modules are **0.03 R to 0.15 R** per trade
(`docs/06` Part C). Against that:

- at `σ_d = 0.6`, MDE is 0.126R and the larger predicted effects are marginally resolvable;
- at `σ_d = 1.5`, MDE is 0.314R and **none of them are resolvable at all**.

So the honest answer to "how many modules can this sample resolve" is: **the trial budget
affords five, and whether power affords even one is unknown until `σ_d` is measured.** Power
is the binding constraint, not deflation — the reverse of what I assumed when I set the
original cap of 8. At the pessimistic end this design cannot support module *selection* in
any form, and the correct response would be to ship the base model with every module
disabled and report the ablation as description only.

Trades required at `f = 1.0` to resolve a given effect:

```
effect | σ_d=0.6 | σ_d=1.0 | σ_d=1.5
-------|---------|---------|--------
0.20 R |     109 |     303 |     682
0.15 R |     194 |     539 |   1,211
0.10 R |     436 |   1,211 |   2,725
0.05 R |   1,744 |   4,844 |  10,899
```

Against a base-case projection of 276, closing the gap to the 0.10R column is a 1.6× to 10×
shortfall. The levers, in descending order of how much they cost you:

| Lever | Multiplier | Cost |
|---|---|---|
| More history (5y → 10y) | ×2 | money (Q10), and older regimes may not be representative |
| More instruments (6 → 12) | ×2 | worse spreads on minors; correlated exposure; more adapter work |
| Looser `rr_min` (2.0 → 1.5) | ×1.5 | contradicts the brief's R:R floor; changes the label |
| Looser bias agreement (drop W1/D1 concordance) | ×1.8 | removes a gate you specified |

All four together reach ~3,000 trades. **None of them is mine to choose** — each trades a
constraint you set for statistical power, so they go to you as the first decision after the
pilot reports (§6).

---

## 8. Amendments to earlier documents

| Document | Change |
|---|---|
| `docs/04` §3 | Primary OOS set is the concatenated walk-forward test blocks; the 12-month holdout is a degradation check with its power stated |
| `docs/04` §6.3 | DSR computed on the concatenated OOS, not the holdout |
| `docs/04` §8 | Criterion 5 (≥200 holdout trades) and criterion 7 (DSR on holdout) rewritten per Corrections 1 and 2 |
| `docs/04` §4.2 | Module selection runs under **one** declared exit policy (Q9): all-out-at-TP1, matching the training label. The scaled policy is reported descriptively and never used for enable/disable |
| `docs/02` §5.5 | Mitigation block marked DROPPED (Q6), definition retained for the record |
| `docs/02` §10.3 | Rewritten (Q7): daily real yields and true DXY are available free and are used as daily regime conditioners under honest names |
| `docs/06` | Decisions marked resolved; A13 corrected; A16 added; Part C rewritten with effect sizes and falsification thresholds |
| `config.yaml` | `max_tunable_params` → `max_trials`; `selectivity` ordinal replaces three tunables; mitigation blocks removed; yield/DXY conditioners added |
