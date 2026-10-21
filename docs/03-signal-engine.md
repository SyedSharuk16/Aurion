# 03 — Signal engine, lifecycle, sizing, Telegram

Depends on `docs/02-definitions.md`. Section references `§n.m` without a file prefix point
there.

---

## 1. Top-down bias cascade — `exact` pipeline, evaluated on bar close only

```
Step 1  HTF bias          on W1 and D1:  trend state (§4.2) + dealing-range position (§6.5)
                          bias_htf ∈ {+1, 0, −1};  0 if W1 and D1 disagree
                          (config bias.require_w1_d1_agreement, default true, prior)

Step 2  HTF PD array      on H4 and H1:  the nearest unmitigated array (§5) in the
                          direction of bias_htf, within pd.max_dist_atr (default 2.0, prior)
                          of current price.  None -> no candidate.

Step 3  Entry model       on M15 and M5.  Exactly one of the models in §2 must fire.

Step 4  Gates             G1..G7 (§3.2)

Step 5  Trade geometry    SL (§4.1), TP1/2/3 (§4.2), R:R

Step 6  Features          FeatureVector built and persisted (§11)

Step 7  Model             calibrated p̂ = P(TP1 before SL)            (§3)

Step 8  Decision          expectancy gate (§3.3)

Step 9  Emit              SignalProposed -> lifecycle + Telegram
```

A candidate row is written at **every** step that rejects, with `rejected_at_step` and
`rejected_reason`. The backtest reports the funnel. A funnel where Step 2 kills 98% of
candidates tells you the PD-array distance threshold is the real model, which is worth
knowing before you believe the rest.

---

## 2. Entry models

Two models only. Adding a third is a scope decision, not a code decision, because each one
multiplies the candidate population and therefore the multiple-testing burden.

### 2.1 `SWEEP_MSS_RETRACE` (primary)

```
1. A liquidity sweep (§6.4) of a pool against bias_htf completes on M15 or M5.
2. Within mss_window bars (default 10, prior) an MSS (§4.4) occurs in the direction
   of bias_htf.
3. The MSS displacement leaves an FVG (§5.1) or creates an OB (§5.3) on the entry TF.
4. Entry = limit at the retrace array:
      fvg  -> entry at CE (§5.1) by default (entry.fvg_level: edge | ce, default ce, prior)
      ob   -> entry at the proximal edge of the OB extent
5. Optional confluence requirement: price in OTE (§6.5) — off by default
   (entry.require_ote, default false, prior; it is a feature, not a gate, until measured).
```

### 2.2 `SILVER_BULLET_FVG`

```
1. Evaluation instant inside a Silver Bullet window (§3.1).
2. An FVG (§5.1) forms on M5 or M15 inside the window, in the direction of bias_htf.
3. Entry = limit at the FVG CE.
4. SL per §4.1 using the window's own extreme as the swept reference if no pool sweep
   occurred.
```
Silver Bullet is a *time-conditioned* variant of the same mechanics, so it shares all
machinery and is distinguished only by the `entry_model` feature. That lets the ablation
remove it cleanly.

---

## 3. The decision rule

### 3.1 Why not a weighted sum

Your brief specifies a weighted sum of module outputs with learned weights, gated by
`R:R ≥ 2`. That combination has an objective mismatch: if weights are fitted to predict
*whether the trade wins*, the score is an (uncalibrated) monotone function of win
probability, while the gate you actually care about is **expectancy**, which depends on win
probability *and* payoff. Two candidates with the same score and R:R of 2.0 and 4.5 have
very different expectancies, and a score threshold is indifferent between them.

The fix is small and strictly more rigorous: make the model output a **calibrated
probability**, then gate on expectancy computed from that probability and the candidate's
own payoff.

### 3.2 Model

```
Label (triple-barrier, López de Prado):
  upper barrier = TP1,  lower barrier = SL,  vertical barrier = expiry (§4.3)
  y = 1  if TP1 touched first
      0  if SL touched first
      sign(realised R at expiry) if neither touched   -> flagged resolved_at_expiry

Primary model:   L2-regularised logistic regression on the feature vector (§11),
                 probability calibrated by isotonic regression on the validation fold.
Cross-check:     gradient-boosted trees, same folds, reported but not used live unless it
                 beats logistic regression out-of-sample on log loss AND the sample size
                 passes the events-per-feature floor below.
Calibration metric: Brier score + reliability curve, reported per walk-forward fold.
```

Three constraints that the sample size imposes, not preferences:

1. **Events-per-feature floor of 20.** With `N_eff` effective training events, at most
   `N_eff / 20` features enter the model. Features are selected by group (§11 tags) using
   the ablation result, not by per-feature p-value fishing.
2. **Fit on candidates, not on emitted signals.** Training uses the full candidate
   population generated at a deliberately low threshold (all of Steps 1–5 passing, Step 8
   ignored), which is 10–50× larger than the emitted set. This is the only way to get a
   usable sample. The live decision then applies the gate that training did not.
3. **Sample-uniqueness weighting.** Overlapping label horizons make events non-i.i.d.
   Each event's weight is its average uniqueness (the reciprocal of the number of
   concurrently open label windows it overlaps), and folds are purged and embargoed
   (`docs/04-validation-protocol.md` §3). Without this, cross-validation scores are
   inflated and the model looks far better than it is.

The live runtime **loads** a model artifact; it never fits. The artifact records its
training window, feature schema version, config hash, and fold id, and the runtime refuses
to load a model whose training window ends after the current simulation/live time — which
makes model leakage in the backtest impossible rather than merely unlikely.

### 3.3 Emission condition

```
R1   = |TP1 − entry| / |entry − SL|                        (payoff at TP1, in R)
E[R] = p̂ · R1 − (1 − p̂) · 1                               (all-out-at-TP1 policy)

emit  iff  G1..G7 all pass                                 (§3.2)
      and  R1 ≥ rr_min                 (default 2.0, prior — your specification)
      and  E[R] ≥ e_min                (default 0.15 R, tunable, range [0.0, 0.5])
      and  p̂ ≥ p_floor                (default 0.25, prior — guards against the
                                        degenerate "tiny probability, huge payoff" corner)
      and  SL beyond the swept liquidity extremity + ATR buffer   (§4.1, structural)
```

`confidence` reported to Telegram **is** `p̂`, a calibrated probability, with its
out-of-sample Brier score from the fold that produced the live model shown in `/stats`. It
is not a 0–100 points score, because a points score invites reading a precision it does not
have.

### 3.4 Top-3 reasons — objective, not narrated

For logistic regression, the contribution of feature `j` to the log-odds of this candidate
is `c_j = β_j (x_j − x̄_j)` where `x̄` is the training-set mean. The reasons shown are the
three largest `|c_j|` with their sign, mapped to fixed human strings per feature id. For the
GBM cross-check, TreeSHAP values replace `c_j`.

This matters: "top 3 reasons" written by hand is marketing. Derived from the model's own
attribution it is an auditable statement about why *this model* raised *this* probability,
and it changes when the model changes.

---

## 4. Trade geometry

### 4.1 Stop loss — `exact`

```
long after a sell-side sweep with extremity E (the sweep low, §6.4):
    SL = E − sl_buffer × ATR14(entry TF)
long with no sweep (Silver Bullet):
    SL = min(L over the FVG-forming leg) − sl_buffer × ATR14
short: mirrored
```
`sl_buffer` default 0.25 (`tunable`, range [0.1, 1.0]).

Additional structural constraint: `|entry − SL| ≥ sl_min_atr × ATR14`
(default 0.3, `prior`). A stop closer than that is inside the noise band and the resulting
R:R is fictitious — it produces a backtest with enormous R multiples and a live system that
is stopped out by spread.

### 4.2 Targets — `exact`

Candidate targets, in the direction of the trade, ordered by distance from entry:

1. Unswept opposing liquidity pools (§6.1), PDH/PDL/PWH/PWL (§6.2), Asian/session extremes
2. Unmitigated opposing PD arrays (§5) on the entry TF and one TF up
3. Dealing-range equilibrium and the range extreme (§6.5)
4. S&R levels with `strength ≥ tp_min_level_strength` (default 0.4, `prior`)

```
TP1 = nearest candidate with R1 ≥ rr_min
TP2 = nearest candidate beyond TP1
TP3 = nearest candidate beyond TP2, else the dealing-range extreme
if no candidate satisfies TP1 -> reject, rejected_reason = "no_target_at_min_rr"
```
Reporting requirement: the share of candidates rejected for `no_target_at_min_rr` is a
headline number in the validation report. If the `rr_min = 2` constraint rejects most
structurally valid setups, then the constraint — not the analysis — is the system.

### 4.3 Expiry and invalidation — `exact`

```
expiry = min( end of the current enabled window + grace,
              decision_time + max_pending_bars × bar duration )
grace            default 0 min (prior)
max_pending_bars default 12 M15 bars (prior)

invalidation (while PENDING), any of:
  - close beyond SL before entry is touched            ("structure invalidated")
  - the entry array becomes MITIGATED (§5.1) without the entry price trading
  - an opposing MSS (§4.4) on the entry TF
  - the window closes (expiry)
```
The invalidation condition is rendered verbatim in the Telegram message, because a signal
without a stated invalidation cannot be audited after the fact.

### 4.4 De-duplication — `exact`

No new signal on an instrument while any signal on that instrument is `PENDING` or
`TRIGGERED`. Additionally the exposure gate of §10.5 applies across instruments.
A post-SL cooldown exists (`signal.cooldown_bars_after_sl`, default 0 = off, `prior`) and is
measured rather than assumed helpful.

### 4.5 Intrabar resolution — `exact`, and it must be reported

Backtest fills and live lifecycle both resolve on **M1** bars (the base timeframe), which
removes most ambiguity. When SL and a TP are both touched within the *same M1 bar*:

```
assume the SL was hit first.
```
This is the conservative choice. The validation report must state the **fraction of resolved
trades decided by this rule**. If it exceeds 5%, the backtest's results are substantially a
function of this assumption and must be reported as such, with a sensitivity run under the
optimistic rule to bound the effect.

---

## 5. Lifecycle state machine — `exact`

```
                     ┌──────────────► CANCELLED (invalidated pre-entry)   [terminal]
                     │
   PENDING ──────────┼──────────────► EXPIRED   (window/expiry reached)   [terminal]
      │              │
      │ entry touched│
      ▼              │
   TRIGGERED ────────┴──────────────► SL_HIT                              [terminal]
      │
      ├─► TP1_HIT  ──► TP2_HIT ──► TP3_HIT                                [terminal]
      │      │            │
      │      │            └────────► BE_STOP / SL_HIT_AFTER_TP           [terminal]
      │      └─────────────────────► BE_STOP                              [terminal]
      │
      └─► FORCE_CLOSED (Friday force-close, §3.3)                         [terminal]
```

- On `TP1_HIT` the system **suggests** moving the stop to break-even. It does not and cannot
  move anything. Subsequent accounting assumes the suggestion was followed
  (`lifecycle.assume_be_after_tp1`, default `true`, `prior`); the backtest also reports the
  no-BE variant, since this assumption changes expectancy materially and should not be
  buried in a default.
- Partial-exit accounting: `lifecycle.tp_allocation` default `[0.5, 0.3, 0.2]` (`prior`).
  Realised R is the allocation-weighted sum.
- **Module selection runs under exactly one declared exit policy (Q9): all-out-at-TP1.**
  That policy is chosen because it is the one the model's label is defined on
  (`docs/03` §3.2), so the selection criterion and the training objective agree. The scaled
  policy is reported descriptively alongside it and is **never** used for an enable/disable
  decision, a parameter choice, or any other selection. Reporting both is description;
  selecting under both is a second fork in the garden of forking paths, and it would double
  the effective trial count without appearing in the trial ledger.
- Every transition writes a row to `signal_states` with `(signal_id, state, at_utc, price,
  revision)` and enqueues exactly one outbox row.

---

## 6. Position sizing — `exact`

Suggestion only. No account query, no order.

```
risk_amount      = account_size × risk_pct                       (both config)
stop_distance    = |entry − SL|                                   price units
V(instrument)    = contract_size × fx(quote_ccy → account_ccy)    account ccy per price unit per lot
lots_raw         = risk_amount / (stop_distance × V)
lots             = floor(lots_raw / lot_step) × lot_step
```

| Instrument | contract_size | quote | `V` with a USD account |
|---|---|---|---|
| XAUUSD | 100 oz | USD | 100 |
| EURUSD, GBPUSD, AUDUSD | 100,000 | USD | 100,000 |
| USDJPY | 100,000 | JPY | 100,000 / price(USDJPY) |
| USDCAD | 100,000 | CAD | 100,000 / price(USDCAD) |

Worked check, USDJPY at 150.00, risk \$100, stop 20 pips (= 0.200 price):
`V = 100,000/150 = 666.67`; `lots = 100 / (0.200 × 666.67) = 0.75`. Equivalently
\$6.67 per pip per lot × 20 pips × 0.75 = \$100. ✔

Worked check, XAUUSD, risk \$100, stop \$4.50: `lots = 100 / (4.50 × 100) = 0.222 → 0.22`. ✔

Rules:
- If `fx(quote → account)` is unavailable at decision time, sizing returns `None` and the
  message says "size unavailable" rather than guessing a rate.
- If `lots < min_lot`, the signal is still emitted with `lots = 0.00` and an explicit note:
  *"below minimum size at the configured risk and this stop distance."* It is never rounded
  **up**, which would silently exceed the risk limit.
- `account_size`, `risk_pct`, `lot_step`, `min_lot` are config. Defaults: 10,000, 0.5%,
  0.01, 0.01.

---

## 7. Telegram

### 7.1 Transport

`python-telegram-bot` (async). Token and chat ids from env only (§`.env.example`). Commands
are accepted only from `AURION_TELEGRAM_ALLOWED_USER_IDS`; anything else is logged and
dropped.

Rate limiting: a token-bucket limiter sized below Telegram's documented limits
(global 30 msg/s, ~20 msg/min per group) with `notify.global_rate` and `notify.per_chat_rate`
in config. Retries on 429/5xx with exponential backoff and jitter, honouring `retry_after`
when the API supplies it. A message that exhausts retries goes to a dead-letter table and
raises an admin alert; it is not dropped silently.

### 7.2 Idempotency

```
outbox(dedupe_key UNIQUE, chat_id, payload_json, state, attempts, sent_at, message_id)
dedupe_key = f"{signal_id}:{state}:{revision}"
```
Enqueue is inside the same DB transaction as the state transition, so a crash between
"state changed" and "message sent" cannot lose or duplicate a notification. The sender is
at-least-once on the wire and exactly-once observably, because Telegram de-duplication is
done by the unique key before the API call.

### 7.3 Message format

Markdown (`MarkdownV2`, with escaping — unescaped user/instrument strings in MarkdownV2
cause silent send failures, so all interpolation goes through an escaper). Emoji for
direction only. Both timezones. Example shape:

```
🟢 *XAUUSD — LONG*  `[PAPER]`

Entry  2318.40 (limit, FVG CE)
SL     2311.80   (−6.60 | 0.9 ATR)
TP1    2332.20   (+13.80 | R 2.09)
TP2    2341.00   (R 3.42)
TP3    2355.50   (R 5.62)

Confidence  0.41  (calibrated; fold Brier 0.213)
E[R]        0.19
Size        0.15 lots  (0.5% of 10,000)

Session  NY AM Kill Zone · gold window
Quarter  Daily Q3 · running phase: Manipulation 0.62 (42% elapsed)
Regime   vol pct 0.71 (HIGH) · ADX 28 · VR z* +2.4

Why
 • swept PDL by 0.38 ATR, reclaimed in 2 bars   (+0.44 log-odds)
 • M15 MSS into H1 bearish-to-bullish OB        (+0.31)
 • discount, p=0.28 of D1 dealing range        (+0.22)

Invalid if  M15 closes below 2311.80, or the FVG fully mitigates without a fill
Expires     13:00 NY / 01:00 SGT
NY 09:42 · SGT 21:42
```

Chart PNG (optional, `notify.send_charts`, default `true`): mplfinance, entry TF plus one
TF up, annotated with the swept pool, the sweep extremity, the MSS bar, the entry array, and
entry/SL/TP lines. Rendered in the single-worker thread pool; if rendering exceeds
`notify.chart_timeout_s` (default 10) the text message is sent without it rather than
delaying the alert.

### 7.4 Commands

| Command | Returns |
|---|---|
| `/status` | uptime, adapter state, last bar age per instrument, active session windows, pause state, model artifact id, config hash |
| `/active` | all `PENDING`/`TRIGGERED` signals with current unrealised R |
| `/stats` | win rate, avg R, expectancy, profit factor, max DD in R, trades/week — overall and broken out by session, instrument, weekday; **always suffixed with the mandatory disclaimer** |
| `/pause` | stop emitting new signals; lifecycle tracking continues |
| `/resume` | resume emission |
| `/sessions` | today's windows in NY and SGT with enabled/disabled and per-instrument whitelist |
| `/levels <symbol>` | S&R levels with strength, pivots, round numbers, pools with swept state, unmitigated arrays |

`/pause` state is persisted, so a restart does not silently resume a paused bot.

### 7.5 Admin channel

Separate chat id. Alerts: data feed stale > `health.max_bar_staleness_s` (default 120) during
an open session; adapter disconnect and each reconnect attempt; unhandled exception with
traceback; calendar-unavailable state entry and re-alert (§3.5); DB write failure; outbox
dead-letter; daily heartbeat at a configured time with 24h counts (bars ingested, candidates,
rejections by reason, signals, transitions, errors, minutes in each blocked state).
