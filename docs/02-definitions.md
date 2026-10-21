# 02 — Exact Definitions

**This is the approval artifact.** Every analysis function in Phase 3 is a transcription of
one subsection here, and each subsection's text becomes that function's docstring verbatim.

Reading guide for each definition:

- **Formula** — unambiguous, computable from closed bars.
- **Parameters** — name, default, provenance (`fixed` / `prior` / `tunable`), search range.
- **Confirmed at** — the earliest time the object may be exposed by `MarketView`. This is
  the no-repaint contract.
- **Status** — `exact` (fully determined), `operational` (a defensible choice among several
  with no canonical answer), or **`heuristic`** (encodes an unvalidated belief; expected to
  be settled by ablation, not argument).

A `heuristic` tag is not a disclaimer. It marks a definition whose *content*, not just its
parameters, is a hypothesis under test.

---

## 0. Conventions

### 0.1 Notation

`O_t, H_t, L_t, C_t` — open/high/low/close of bar `t` on the stated timeframe.
`V_t` — tick volume (see §0.5). `body_t = |C_t − O_t|`. `range_t = H_t − L_t`.
`upper_wick_t = H_t − max(O_t, C_t)`. `lower_wick_t = min(O_t, C_t) − L_t`.
`r_t = ln(C_t / C_{t−1})`. Bars are indexed so that `t−1` is the previously closed bar.
`𝟙[·]` is the indicator function. All intervals are closed unless stated.

### 0.2 Time — `exact`

- Every stored and internal timestamp is **UTC**, timezone-aware, microsecond precision.
- Session and quarter logic converts to the window's declared timezone via `zoneinfo`
  (`America/New_York` default). DST is handled by `zoneinfo`, never by fixed offsets.
- A bar is identified by `(instrument, timeframe, open_time)` and carries `close_time`.
  `close_time` is the **exclusive** end instant: an M5 bar with `open_time = 09:00:00` has
  `close_time = 09:05:00` and contains ticks in `[09:00:00, 09:05:00)`.
- `confirmed_at` for a bar equals its `close_time`. A bar is never visible before then.
- Ambiguous local times (DST fall-back) are resolved with `fold=0`; nonexistent local times
  (spring-forward) shift forward to the first existing instant. A session window whose
  boundary falls in a nonexistent hour is logged once per occurrence.
- Telegram renders both `America/New_York` and `Asia/Singapore`; stored values stay UTC.

### 0.3 Price basis — `exact`

OANDA serves bid, ask, and mid candles. This distinction is normally skipped and it
materially changes backtest results, so it is fixed here:

| Use | Basis |
|---|---|
| All analysis (structure, arrays, levels, regime) | **mid** = (bid + ask) / 2 |
| Long entry trigger, short stop-loss trigger | **ask** |
| Short entry trigger, long stop-loss trigger | **bid** |
| Take-profit trigger (long) | **bid**; (short) **ask** |
| Spread | `ask − bid` at the same instant |

Storage keeps bid and ask OHLC; mid is derived, not fetched, so mid is always exactly
consistent with the stored bid/ask. Analysis on mid keeps definitions spread-independent;
triggering on the correct side prevents the classic "entry filled at mid" inflation.

`point` (tick size) per instrument from config: XAUUSD 0.01, non-JPY FX 0.00001 (5-digit),
JPY FX 0.001. A **pip** is 10 points for 5-digit FX and 3-digit JPY FX; for XAUUSD "pip"
is not used — all distances are quoted in price units (USD) and in ATR multiples.

### 0.4 ATR — `exact`

Wilder's true range and Wilder's smoothing (RMA), on **mid**, on the timeframe of the
object being measured, from closed bars only:

```
TR_t  = max( H_t − L_t , |H_t − C_{t−1}| , |L_t − C_{t−1}| )
ATR_n = mean(TR_1..TR_n)                                  (seed, at bar index n)
ATR_t = ATR_{t−1} + (TR_t − ATR_{t−1}) / n                (t > n)
```

Parameters: `n = 14` (`fixed`; Wilder's convention, not tuned — tuning ATR period and also
every ATR multiple double-counts the same degree of freedom).

Warm-up: requires `n + 1` closed bars (TR needs `C_{t−1}`). Returns `None` before that.
Every threshold expressed "in ATR units" means `k × ATR_t` on the same timeframe as the
object, evaluated at the object's own confirmation bar and **frozen** there — it is not
re-evaluated later, or objects would repaint as ATR changes.

### 0.5 Tick volume — `operational`

`V_t` is the broker's count of price updates in the bar. It is **not** traded volume: FX
spot has no consolidated tape. Consequences, stated once and referenced thereafter:

- `V` is broker-specific and not comparable across venues or across adapters.
- A change in the broker's tick-throttling changes `V`'s distribution with no change in the
  market. Any model feature derived from `V` is therefore fragile across time.
- All `V`-derived quantities are **normalised** against a rolling median of `V` over the
  same time-of-day bucket (§3.4), never used raw.
- Features whose only input is `V` are tagged `volume_proxy` in the feature schema and are
  reported separately in the ablation, so their contribution can be removed wholesale if the
  broker changes.

### 0.6 Percentile rank — `exact`

```
pctrank(x_t ; W) = #{ i ∈ [t−W+1, t] : x_i < x_t } / W        ∈ [0, 1)
```
Strictly backward-looking, inclusive of `t`, requires `W` observations else `None`.

### 0.7 Rounding and equality — `exact`

Price comparisons use absolute tolerance `0.5 × point` of the instrument. Two prices are
"equal" iff `|p1 − p2| ≤ 0.5 × point`. No floating-point `==` on prices anywhere. All
stored prices are rounded to the instrument's point before persistence so that replay is
bit-identical.

---

## 1. Instruments and the USD index proxy

### 1.1 Tradable set — `fixed` (config-driven)

`XAUUSD` (priority), `EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`, `USDCAD`. Adding or removing
an instrument is a config edit; per-instrument session whitelists, point size, contract
size, and pip value all come from the instrument registry in `config.yaml`.

### 1.2 `USDX_PROXY` — `operational`

ICE's DXY requires EUR, JPY, GBP, CAD, SEK, CHF with fixed weights
(57.6 / 13.6 / 11.9 / 9.1 / 4.2 / 3.6 %) and a scaling constant. SEK and CHF are not in the
traded set, so **DXY cannot be reconstructed exactly**. Definition used:

```
USDX_PROXY_t = 100 × Π_i ( S_i,t / S_i,0 ) ^ (w_i × s_i)
```
where `S_i` is the mid of pair `i`, `s_i = −1` if USD is the quote currency (EURUSD, GBPUSD,
AUDUSD) and `+1` if USD is the base (USDJPY, USDCAD), `w_i` are the available-pair weights
renormalised to sum to 1, and `S_i,0` is the mid at the proxy epoch (config,
`prior`: first bar of the backtest window).

Status note: it is a *proxy*, named as one, and Phase 4 reports its realised correlation
with true DXY over the test window so the reader can judge its adequacy. It is used for
**context features only**. It is explicitly **not** used for conflicting-exposure control,
because a proxy built from EURUSD/GBPUSD/USDJPY cannot be an independent filter on
EURUSD/GBPUSD/USDJPY signals — that is circular. Exposure control uses direct USD-beta
netting (§10.5).

---

## 2. Bars and resampling

### 2.1 Base timeframe — `fixed`

M1 is the base. All higher timeframes are resampled locally. Vendor-resampled bars are not
used, for the reason you gave (inconsistent session anchoring) and one more: vendor H1/H4
bars are built on the vendor's own day convention, which differs between vendors, so
switching adapters would silently change every structural object.

### 2.2 Resampling rule — `exact`

For target timeframe `X` with duration `Δ` and anchor `a` (an instant in the anchor
timezone):

```
bucket(t)   = a + Δ × floor( (t − a) / Δ )
open_time   = bucket(start of first constituent M1 bar)
O = O of first constituent;  C = C of last constituent
H = max H;  L = min L;  V = Σ V
close_time  = open_time + Δ
confirmed_at = close_time
```

Rules that make this non-leaking:

1. Bars are **left-labelled** (`open_time`) but only **exposed at `close_time`**. A
   left-labelled bar treated as known at its label is the single most common source of
   future leakage in resampled data; T4 (§01 §3.3) tests exactly this.
2. A bucket with missing M1 constituents (market closed, feed gap) produces a bar only if
   it has at least `data.min_constituents_frac` (default 0.5, `prior`) of the expected M1
   count **or** the gap is a known market closure. Otherwise the bar is marked
   `incomplete=True` and is excluded from analysis (and from ATR), rather than being
   silently thin.
3. Resampling is incremental: one running partial bar per `(instrument, timeframe)`,
   emitted on the first M1 bar belonging to the next bucket. Equivalently, `close_time`
   has passed. A partial bar is never exposed except via `MarketView.partial()`.
4. The W1/D1 weekend boundary: buckets containing no market data are not emitted at all.
   No synthetic zero-volume bars. Gap across the weekend is explicit, which the
   weekend-gap logic (§3.6) depends on.

### 2.3 Anchors — `operational`, and this is a real free parameter

| TF | Δ | Default anchor | Note |
|---|---|---|---|
| M5, M15 | 5m, 15m | 00:00 UTC | No ambiguity; divides the hour evenly |
| H1 | 1h | 00:00 UTC | |
| **H4** | 4h | **17:00 America/New_York** | Produces 17:00, 21:00, 01:00, 05:00, 09:00, 13:00 NY |
| **D1** | 1d | **17:00 America/New_York** | Broker/FX day convention: 17:00 NY → 17:00 NY |
| W1 | 1w | **Sunday 17:00 America/New_York** | FX week open |

The H4 anchor is genuinely contested — 00:00 UTC, 18:00 NY and 17:00 NY are all in common
use and they produce *different* swings, *different* FVGs, and therefore different signals.
It is therefore `tunable` over `{17:00 NY, 18:00 NY, 00:00 UTC}` **or**, preferably, fixed
`prior` at 17:00 NY and tested as a robustness check rather than optimised, to conserve the
8-parameter budget (`docs/06-open-questions.md` Q2 — your decision).

**D1 for OHLC/ATR is 17:00-anchored. This is a different object from the "true day open"
of §8.3, which is 00:00 NY.** Both exist, both are used, and conflating them is a bug.
Previous-day high/low (§6.2) has its own day convention, also a decision (Q3).

---

## 3. Sessions, gates and spread

### 3.1 Session window — `exact`

A window is `(name, tz, start_local_time, end_local_time, weekdays, emits_signals)`.
Membership for instant `u` (UTC): convert to `tz`; `u ∈ W` iff the local weekday is in
`weekdays` and the local time is in `[start, end)`. Windows that cross local midnight are
stored as two intervals. `[start, end)` half-open so adjacent windows do not overlap.

Defaults (all `America/New_York`, all `prior`, all config-editable):

| Window | Local time | Emits signals |
|---|---|---|
| Asian range build | 20:00–00:00 | no (marking only) |
| London Kill Zone | 02:00–05:00 | yes |
| NY AM Kill Zone | 07:00–10:00 | yes |
| NY AM gold window | 08:30–11:00 | yes (XAUUSD priority) |
| London Close | 10:00–12:00 | optional, reversal setups only |
| Silver Bullet A/B/C | 03:00–04:00, 10:00–11:00, 14:00–15:00 | yes |

Per-instrument whitelist, default: `XAUUSD: [London KZ, NY AM, gold window, SB-B]`,
`USDJPY: [Asian, London KZ, NY AM]`, others `[London KZ, NY AM]`.

**Caveat, flagged because it is structural not cosmetic:** anchoring "London Kill Zone" to
02:00–05:00 New York means that for the ~3 weeks per year when UK and US DST transitions are
out of step, the window sits one hour away from the London session it is named after. Each
window therefore declares its own `tz`; the default follows the ICT convention (NY) and
`tz: Europe/London` for the London windows is a robustness variant, not a fix. Decision Q1.

### 3.2 Admissibility gate — `exact`

A candidate may become a signal only if **all** hold at the evaluation instant:

```
G1  u ∈ (some enabled window with emits_signals) ∧ instrument whitelisted for it
G2  ¬ friday_block(u)
G3  ¬ rollover_block(u)
G4  ¬ news_block(u)
G5  ¬ weekend_gap_block(u)
G6  spread_ok(u)
G7  no active/pending signal on the same instrument (§03 §4.4)
```

Each gate records a boolean plus its reason on the candidate row, so the backtest can
report how many candidates each gate killed. A gate that kills 99% of candidates is a
finding, not a success.

### 3.3 Hard blocks — `exact`

- **Friday block:** `u_local(NY) ≥ friday_cutoff` (default 12:00, `prior`) on a Friday.
  Applies to new signals; existing signals follow §03 §4.5 (force-expire at
  `friday_force_close`, default 16:00 NY).
- **Rollover block:** local NY time in `[16:55, 17:10)` (`prior`). Rationale: spread blowout
  and the D1 boundary; both make level logic unreliable rather than merely expensive.
- **Weekend gap block:** for `weekend_gap_block_minutes` (default 60, `prior`) after the
  first M1 bar of the week, and additionally whenever
  `|open_of_week − close_of_prior_week| > gap_atr_mult × ATR_D1` (default 0.5, `prior`),
  in which case the block extends until an H1 bar has closed. Reason: every level, FVG and
  pool computed before the gap is referenced to a price the market has jumped past.

### 3.4 Spread filter — `exact`, with a correction to the brief

```
spread_t      = ask_t − bid_t                      (at the evaluation instant)
bucket(t)     = (instrument, weekday_class, 15-min-of-day in the window's tz)
med_t         = median{ spread_s : s ∈ bucket(t), s < t, last W occurrences }
spread_ok(t)  = spread_t ≤ N × med_t
```

Parameters: `N` default 2.5 (`tunable`, range [1.5, 5.0]); `W` default 20 occurrences
(`prior`); `weekday_class ∈ {Mon, Tue–Thu, Fri}` (`prior`).

The brief said "rolling median". A *global* rolling median is dominated by time of day:
XAUUSD spread at 03:00 NY is several multiples of its 09:00 NY value, so a global median
either permits everything in the liquid session or blocks everything in the thin one. The
median must be conditioned on time-of-day bucket. This is a correction, not a refinement.

Warm-up: fewer than `W` observations in the bucket → `spread_ok = False` (fail closed) and
`rejected_reason="spread:warmup"`.

### 3.5 News blackout — `operational`, with two hazards

```
news_block(u) = ∃ e ∈ Calendar : e.impact ∈ blocked_impacts
                              ∧ e.currency ∈ currencies(instrument)
                              ∧ |u − e.scheduled_time| ≤ blackout
```
Parameters: `blackout` default 30 min each side (`prior`); `blocked_impacts` default
`{high}` (`prior`); XAUUSD's currency set is `{USD}` plus optional `{XAU}` for
gold-specific events. Per-event overrides allowed (NFP, CPI, FOMC → 60 min, `prior`).

Hazard 1 — **fail-safe blocking is a liveness hazard.** `fail_safe = block` (your
specification, kept) means a calendar outage silently stops all signals. Mitigation,
required not optional: entering the blocked-on-unavailable state raises an admin alert
immediately, re-raises every `health.calendar_outage_realert_min` (default 30), and the
daily heartbeat reports total minutes spent in it. A system that emitted nothing for a week
because of a dead API must be loud about it.

Hazard 2 — **point-in-time integrity.** Replaying today's calendar over 2019 data leaks
events that were added, rescheduled or re-graded after the fact. The backtest therefore
reads a **frozen snapshot keyed by publication date**: an event is visible to the simulation
only with the fields it had as published before the evaluation instant. If no point-in-time
calendar is available, the backtest runs the news gate in two modes — `off` and
`today's-calendar` — and reports both, so the reader can see the size of the effect rather
than inherit a hidden bias. Decision Q5.

### 3.6 Session-derived marks — `exact`

For session window `W` on day `d`: `session_high = max H_t`, `session_low = min L_t` over
closed M1 bars with `open_time ∈ W(d)`; `confirmed_at = end of W(d)`. A *running*
session high/low is also exposed for in-window logic, with `confirmed_at = close_time of the
latest contributing bar`, so it never includes the forming bar.

Asian range: `asian_high / asian_low` over the Asian window; `confirmed_at = 00:00 NY`.
A running Asian range exists during the window for marking only; it may not feed a signal
emitted *inside* the Asian window unless the Asian window is whitelisted for the
instrument, because the range is then not yet complete.

---

## 4. Market structure

### 4.1 Fractal swing — `exact`

Bar `i` on timeframe `X` is a **swing high** with half-width `N` iff

```
H_i > H_j  for all j ∈ [i−N, i−1]   and   H_i > H_k  for all k ∈ [i+1, i+N]
```

Swing low: same with `L_i < L_j`, `L_i < L_k`. Strict inequalities on both sides.

- **Confirmed at:** `close_time(bar i+N)`. The swing is invisible before then — this is the
  no-repaint mechanism, and it is enforced by `MarketView` filtering on `confirmed_at`, not
  by discipline.
- Parameters: `N` per timeframe. Defaults `prior`: M5 = 2, M15 = 2, H1 = 3, H4 = 3, D1 = 3,
  W1 = 2. One `tunable` scalar `swing_N_scale ∈ {1, 2}` multiplies all of them, if you want
  structure granularity in the search (costs 1 of 8 budget slots).
- Plateaus: equal highs at `i` and `i±1` yield **no** swing under strict inequality. That is
  intentional, not an oversight — equal highs are the defining feature of a liquidity pool
  (§6.1) and are captured there, where they belong. A plateau being both "a swing" and
  "equal highs" would double-count the same price action in the feature vector.

### 4.2 Structural trend state — `exact`

Over the sequence of confirmed swings on timeframe `X`, ordered by `event_time`:

```
HH = latest swing high > previous swing high
HL = latest swing low  > previous swing low
LH = latest swing high < previous swing high
LL = latest swing low  < previous swing low

trend = BULLISH   if HH ∧ HL
        BEARISH   if LH ∧ LL
        RANGING   otherwise
```
Requires ≥2 confirmed highs and ≥2 confirmed lows, else `None`. `confirmed_at` = the later
of the two most recent contributing swings' confirmations.

### 4.3 Displacement — `exact`

Bar `t` on timeframe `X` is a **displacement** bar iff

```
body_t ≥ k × ATR14_t        and        bar t is the middle bar of an FVG (§5.1)
```
Direction = `sign(C_t − O_t)`. Parameters: `k` default 1.0 (`tunable`, range [0.5, 2.5]).

`k = 1.0` is a deliberately neutral prior — a body equal to average true range — chosen so
the default is not a fitted value masquerading as a definition. The ATR used is the value at
bar `t`, frozen.

The FVG conjunct is from your brief and is kept. Note its consequence: a large body that
does *not* gap (because the prior bar's range was wide) is not displacement under this
definition, which is the intended ICT meaning but excludes some large moves. The
alternative (body OR gap) is a robustness variant.

### 4.4 BOS / CHoCH / MSS — `exact` hierarchy, `operational` naming

The naming in the source material is inconsistent (CHoCH and MSS are used
interchangeably by some, hierarchically by others). The following is a strict hierarchy,
chosen so that the three are mutually distinguishable and individually testable.

Let `S_H` be the most recent confirmed swing high with `confirmed_at ≤ as_of` and
`event_time` after the previous structural event; `S_L` symmetrically.

```
break_up(t)    = C_t > H(S_H) + tol        tol = 0.5 × point
break_down(t)  = C_t < L(S_L) − tol

BOS_up(t)   = break_up(t)   ∧ displacement_up(t)   ∧ trend_before(t) = BULLISH
BOS_down(t) = break_down(t) ∧ displacement_down(t) ∧ trend_before(t) = BEARISH

CHoCH_up(t)   = break_up(t)   ∧ displacement_up(t)   ∧ trend_before(t) ∈ {BEARISH, RANGING}
CHoCH_down(t) = break_down(t) ∧ displacement_down(t) ∧ trend_before(t) ∈ {BULLISH, RANGING}

MSS_up(t)   = CHoCH_up(t)   ∧ ∃ sweep_down in (t − M_mss, t]  of a sell-side pool (§6.4)
MSS_down(t) = CHoCH_down(t) ∧ ∃ sweep_up   in (t − M_mss, t]  of a buy-side pool (§6.4)
```

So: **BOS = continuation break. CHoCH = first break against the prevailing structure.
MSS ⊂ CHoCH = a CHoCH preceded by a liquidity sweep.** All three require close-beyond
(your specification) and displacement (your specification). MSS is the ICT entry-grade
event and is strictly rarer than CHoCH by construction, which is the property that makes
the distinction worth having.

Parameters: `M_mss` default 10 bars (`prior`), `tol` `fixed`.
`confirmed_at = close_time(t)` — these are close-based so they confirm on the breaking bar,
unlike swings.

`trend_before(t)` is evaluated from swings confirmed strictly before `t`. A single bar can
be both a CHoCH and, after the trend state updates, the start of a BULLISH regime; the
event is labelled with the *prior* state. This ordering is part of the definition because
the alternative is self-referential.

---

## 5. ICT price-delivery arrays

**Blanket status note for §5–§8.** These concepts come from a discretionary teaching
tradition, not a specification. Each definition below is objective and therefore testable;
none can claim to be *the* definition. Where I had to choose, I chose the variant that is
(a) most commonly described and (b) cheapest to falsify. Ablation, not debate, settles
whether each earns inclusion.

### 5.1 Fair value gap (FVG) — `exact`

Three consecutive closed bars `i−1, i, i+1` on timeframe `X`:

```
bullish FVG   iff  L_{i+1} > H_{i−1}
   gap interval = [ H_{i−1} , L_{i+1} ]
bearish FVG   iff  H_{i+1} < L_{i−1}
   gap interval = [ H_{i+1} , L_{i−1} ]
size          = gap_high − gap_low
admitted      iff size ≥ g × ATR14_i
CE ("consequent encroachment") = (gap_high + gap_low) / 2
```

Parameters: `g` default 0.10 (`tunable`, range [0.0, 0.5]). `fixed`: three candles, and the
use of wick extremes (`H`/`L`) rather than bodies.

**Confirmed at:** `close_time(bar i+1)`.

Fill tracking, for a bullish FVG, over bars after confirmation:
```
deepest  = min L_s  for s > i+1
fill     = clamp( (gap_high − deepest) / size , 0, 1 )
state    = UNTOUCHED  (fill = 0)
         | TOUCHED    (0 < fill < 0.5)
         | CE_TRADED  (0.5 ≤ fill < 1)
         | MITIGATED  (fill ≥ 1)
```
Mirror for bearish. `mitigated_at` = `close_time` of the first bar reaching `fill ≥ 1`.
A mitigated FVG is retained in the database (it is a feature input: "price is at a
previously mitigated FVG") but is excluded from "available PD array" queries.

### 5.2 Inverse FVG — `exact`

A bullish FVG becomes an **inverse FVG (bearish polarity)** at the first bar `t > i+1` with
`C_t < gap_low − tol`. It then behaves as a bearish array over the same interval. Mirror for
bearish → bullish. `confirmed_at = close_time(t)`. Rationale for using close, not wick: a
wick through the gap is the mitigation case (§5.1); a *close* beyond it is the polarity
inversion. Using the same trigger for both would make the two states indistinguishable.

### 5.3 Order block (OB) — `operational`

Given a BOS or MSS at bar `t` in direction `d` on timeframe `X`:

```
d = up:   j = max{ s < t : C_s < O_s }   (nearest preceding down-close bar)
d = down: j = max{ s < t : C_s > O_s }   (nearest preceding up-close bar)
admitted iff  t − j ≤ W_ob
extent   = [L_j, H_j]        if ob_extent = "wick"   (default)
         = [min(O_j,C_j), max(O_j,C_j)]  if "body"
```

Parameters: `W_ob` default 10 bars (`prior`); `ob_extent` default `wick` (`prior`;
`body` is a robustness variant). `confirmed_at = close_time(t)` — the OB does not exist
until the break it caused is confirmed, which is why an OB can never be drawn in advance.

Invalidation: a bullish OB is `INVALIDATED` at the first bar with `C_s < L_j − tol`
(close-based, consistent with §5.2). Mitigation states mirror §5.1 using the OB extent.

### 5.4 Breaker block — `operational`

A **breaker** is a former OB that failed and whose polarity inverted, confirmed by an
opposing structural break:

```
bullish OB at j  →  bearish breaker  iff
     ∃ u > t : C_u < L_j − tol                       (OB invalidated)
   ∧ ∃ v ∈ (u, u + W_brk] : BOS_down(v) ∨ CHoCH_down(v)
```
`confirmed_at = close_time(v)`. Parameters: `W_brk` default 20 bars (`prior`).

### 5.5 Mitigation block — **DROPPED** (Q6)

> **Not built.** Dropped on review. The reasoning in `docs/07-power-and-capacity.md` §3 is
> decisive: a module whose affected fraction is small needs a per-affected-trade effect of
> ~1R to be resolvable at the projected sample size, which is not a plausible effect for a
> variant order block. The definition is retained below for the record, so that the rejected
> option and its reason are auditable, but no code implements it.

ICT's distinction between a breaker and a mitigation block is not consistently stated in the
source material. I am unwilling to pretend otherwise. The testable distinction adopted:

- **Breaker** (§5.4): the OB was invalidated *and* an opposing break followed.
- **Mitigation block**: an OB formed at a **failure swing** — a swing that did *not* exceed
  the prior same-side extreme — with no invalidation required.

```
bullish mitigation block: an OB at j whose causing break at t followed a swing low S
  with L(S) > L(previous swing low)        (a higher low: failure to take sell-side liquidity)
```
`confirmed_at = close_time(t)`. Dropped per Q6: at this sample size the ablation could never
separate it from an ordinary order block, so the 40 lines buy an unanswerable question. If
power is the binding constraint, every marginal module is net negative.

### 5.6 Balanced price range (BPR) — `exact`

```
BPR = [max(lo_bull, lo_bear), min(hi_bull, hi_bear)]
  for a bullish FVG (lo_bull, hi_bull) and a bearish FVG (lo_bear, hi_bear)
  with  non-empty intersection
  and   |confirm_bull − confirm_bear| ≤ W_bpr bars
```
Parameters: `W_bpr` default 10 (`prior`). `confirmed_at` = the later of the two FVG
confirmations. Polarity: the BPR is bidirectional by construction; the signal engine treats
it as support in a bullish dealing range and resistance in a bearish one, which is a
`heuristic` choice recorded as such.

---

## 6. Liquidity

### 6.1 Equal highs / equal lows pool — `exact`

Over confirmed swing highs on timeframe `X` within lookback `L_pool`:

```
cluster = maximal set of confirmed swing highs { p_1..p_m }, m ≥ 2,
          with  max p − min p ≤ τ × ATR14 (at the newest member's confirmation)
pool_level = max{ p_1..p_m }          (the extreme — where resting stops sit)
pool_width = max p − min p
pool_touches = m
side = BUY_SIDE                       (stops of shorts rest above highs)
```
Mirror for lows → `SELL_SIDE`, `pool_level = min{...}`.

Parameters: `τ` default 0.15 (`tunable`, range [0.05, 0.5]); `L_pool` default 100 bars
(`prior`). `confirmed_at` = confirmation of the newest member.

Using `max` rather than the cluster mean is deliberate: a stop-run is triggered by the
extreme, not by the average. The mean would systematically place pool levels inside the
range where no stops are.

### 6.2 Reference-period extremes — `operational`

`PDH/PDL` (previous day), `PWH/PWL` (previous week), `PMH/PML` (previous month):
the high/low of the preceding period's bars, `confirmed_at` = that period's end.

The day convention is a genuine fork and must be decided (Q3):

| Convention | Day boundary | Argument |
|---|---|---|
| `broker` | 17:00 NY → 17:00 NY | Matches the D1 bar used for ATR and pivots; internally consistent |
| `ny_midnight` (default `prior`) | 00:00 NY → 00:00 NY | Matches ICT's "true day"; matches the quarterly model's day (§8.2) |

Default `ny_midnight`, because every other ICT-derived object in this system uses the
00:00 NY day and mixing conventions inside one model is worse than either choice. The D1
*bar* stays 17:00-anchored for ATR and pivots, and the two are separately named
(`d1_bar_*` vs `prev_day_*`) so they cannot be confused in code.

### 6.3 Asian range and session extremes — see §3.6.

### 6.4 Liquidity sweep — `exact`

Your brief: "wick beyond the pool and close back inside within M bars." Made precise for a
buy-side pool at level `P`:

```
penetration bar:  first bar b with  H_b > P + ε × ATR14_b
reclaim bar:      first bar c ≥ b with  C_c < P − δ × ATR14_c
sweep_up  = exists such b, c with  c − b ≤ M_sweep
                              and  no bar in [b, c] has  C < P  before c   (the reclaim is the first)
extremity = max{ H_s : s ∈ [b, c] }
depth     = (extremity − P) / ATR14_b
```
Mirror for sell-side pools (`L_b < P − ε·ATR`, `C_c > P + δ·ATR`).

Parameters: `ε` default 0.05 (`tunable`, range [0.0, 0.3]) — the minimum penetration that
distinguishes a sweep from noise; `δ` default 0.0 (`prior`) — reclaim requires only a close
back inside; `M_sweep` default 3 bars (`tunable`, range [1, 10]).

`confirmed_at = close_time(c)`. **A sweep is not a sweep until the reclaim closes.** This is
the single most important no-repaint point in the ICT layer: a penetration that has not yet
reclaimed looks identical, in real time, to a genuine breakout. Any definition that labels
the penetration bar as a sweep is using the future.

Once swept, a pool's state becomes `SWEPT` with `swept_at = close_time(c)` and is excluded
from "available target" queries, but retained as a feature ("a sweep occurred `n` bars ago
at `depth` ATR").

### 6.5 Dealing range, premium/discount, OTE — `operational`

Let `e` be the most recent structural event (BOS/CHoCH/MSS, §4.4) on timeframe `X`, with
direction `d`.

```
d = up:   L = L(swing low immediately preceding the displacement leg that broke structure)
          H = max{ H_s : s ≥ event bar }           (running, closed bars only)
d = down: H = H(swing high immediately preceding the leg);  L = min{ L_s : s ≥ event bar }

equilibrium  = (H + L) / 2
position p   = (price − L) / (H − L)                   ∈ [0, 1] (clipped)
premium      = p > 0.5 ;  discount = p < 0.5
retracement for a long in a bullish range:  r = (H − price) / (H − L)
OTE_long     = r ∈ [0.62, 0.79]       →  price ∈ [H − 0.79(H−L), H − 0.62(H−L)]
OTE_short    = mirrored
```
`confirmed_at` of the range = the structural event's confirmation; `H`/`L` update on each
bar close (the range is a running object, versioned, and every version carries its own
`confirmed_at`).

**Honest statement on 0.62 / 0.705 / 0.79.** These are Fibonacci-adjacent conventions with
no theoretical justification in market microstructure, and 0.62 is not even a Fibonacci
ratio (0.618 is). They are `tunable` as a single pair `(ote_lo, ote_hi)` over
`[0.50, 0.90]` with `ote_hi − ote_lo ≥ 0.10`, costing 1 budget slot. If the fitted band
drifts far from [0.62, 0.79], that is a result worth reporting, not a parameter to pin back.

---

## 7. Support & resistance, pivots, round numbers

### 7.1 Weighted KDE level extraction — `exact` (primary method)

Input: confirmed swing points (highs and lows, both) on timeframe `X` within lookback
`L_sr`, each a `(price p_i, age a_i in bars, touches c_i, rejection v_i)`.

```
recency:    w_rec,i  = exp( − ln2 × a_i / halflife )
touch:      w_tch,i  = 1 + ln(1 + c_i)
rejection:  w_rej,i  = clamp( V_i / median(V over bucket, §0.5) , 0.25, 4.0 )
weight:     w_i      = w_rec,i × w_tch,i × w_rej,i

density:    f(p) = Σ_i w_i × K( (p − p_i) / h )  /  Σ_i w_i ,      K = standard normal pdf
bandwidth:  h = h_mult × ATR14_D1
levels:     local maxima of f on a grid of step h/8 over [min p_i − h, max p_i + h]
strength:   s_level = f(peak) / max_peak f            ∈ (0, 1]
```

`touches c_i`: the number of distinct later bars whose `H` (for a high) came within
`τ_touch × ATR` of `p_i`, where distinct means separated by ≥ `m_touch` bars (default 5,
`prior`) to avoid counting one consolidation as many touches.

Parameters: `halflife` default 250 bars (`prior`); `h_mult` default 0.25
(`tunable`, range [0.1, 0.6]); `L_sr` default 500 bars (`prior`);
`τ_touch` default 0.15 (`prior`, tied to §6.1's τ).

`confirmed_at` = the newest contributing swing's confirmation. The level set is recomputed
on each D1 close (cost control, §01 §8) and on each timeframe's close for intraday levels.

### 7.2 DBSCAN alternative — `exact`

`DBSCAN(eps = eps_mult × ATR14_D1, min_samples = 2, sample_weight = w_i)` on the 1-D price
vector; level = weighted mean of the cluster, strength = `Σ w_i` in the cluster normalised
by the max cluster weight. Selected by `sr.method: kde | dbscan` (default `kde`, `prior`).
Both behind one interface; the choice is a robustness variant, not a tuned parameter.

KDE is primary because it yields a continuous strength score as a by-product, whereas DBSCAN
requires a separate scoring rule, and because DBSCAN's `eps` creates hard cluster boundaries
that make the level set discontinuous in `eps` — bad for sensitivity analysis.

### 7.3 Merging — `exact`

Iteratively merge the two nearest levels while their distance `≤ x_merge × ATR14_D1`:
merged price = `Σ(s_i p_i)/Σ s_i`, merged strength = `1 − Π(1 − s_i)` (probabilistic OR, so
strength saturates at 1 instead of summing past it). `x_merge` default 0.3 (`prior`).

### 7.4 Classic pivots — `exact`

From the previous D1 bar (17:00 NY anchored, §2.3):
```
P  = (H + L + C) / 3
R1 = 2P − L        S1 = 2P − H
R2 = P + (H − L)   S2 = P − (H − L)
R3 = H + 2(P − L)  S3 = L − 2(H − P)
```

### 7.5 Camarilla pivots — `exact` formula, **`heuristic`** content

```
H4 = C + (H−L) × 1.1/2      L4 = C − (H−L) × 1.1/2
H3 = C + (H−L) × 1.1/4      L3 = C − (H−L) × 1.1/4
H2 = C + (H−L) × 1.1/6      L2 = C − (H−L) × 1.1/6
H1 = C + (H−L) × 1.1/12     L1 = C − (H−L) × 1.1/12
H5 = (H/L) × C              L5 = 2C − H5
```
The `1.1` multiplier and the `2, 4, 6, 12` divisors have no published derivation. The
formula is reproduced exactly as conventionally stated; the constants are **not** tuned
(tuning undocumented folklore constants is how a 4-parameter indicator becomes a
4-parameter overfit). Camarilla enters only as a secondary confluence feature and is
expected to be ablated out.

### 7.6 Round numbers — `exact`

```
XAUUSD:  tier1 = multiples of 10 ; tier2 = multiples of 50 ; tier3 = multiples of 100
FX 5-digit:  tier1 = multiples of 0.0050 ; tier2 = 0.0100   (i.e. x.xx50 and x.xx00)
FX JPY:      tier1 = multiples of 0.50 ;   tier2 = 1.00
```
Feature: signed distance to the nearest level of each tier, in ATR units. Round numbers are
secondary confluence only.

---

## 8. Quarterly Theory (Daye)

### 8.0 Status, stated before the definitions

Quarterly Theory makes two separable claims:

1. **Structural claim** — time can be partitioned into nested quarters anchored at specific
   instants. This is `exact`: it is arithmetic on a calendar and is either implemented
   correctly or not.
2. **Behavioural claim** — the four quarters at a given level tend to carry the phases
   Accumulation → Manipulation → Distribution → Continuation/Reversal in that order. This is
   an **unvalidated empirical assertion**, and it is the part that would make the theory
   useful.

The system implements (1) exactly and **measures** (2) rather than assuming it. Phase 4
reports, per level and per instrument, the empirical frequency of each phase label by
quarter index, with confidence intervals. If that distribution is statistically
indistinguishable from uniform, the behavioural claim carries no information and the module
is disabled. That is the honest test, and I will report the result either way.

### 8.1 Quarter lattice — `exact` structure, `operational` anchors

| Level | Partition | Anchor |
|---|---|---|
| Yearly | 4 calendar quarters (Jan–Mar, Apr–Jun, Jul–Sep, Oct–Dec) | `true_year_open` (§8.3) |
| Monthly | 4 blocks of 7 days from the month's anchor; residual days (1–3) join Q4 | `true_month_open` |
| Weekly | Q1 = Monday, Q2 = Tuesday, Q3 = Wednesday, Q4 = Thursday; **Friday excluded** | `true_week_open` |
| Daily | Q1 18:00–00:00, Q2 00:00–06:00, Q3 06:00–12:00, Q4 12:00–18:00 (NY) | `true_day_open` |
| 6h→90m | each 6h quarter splits into four 90-minute quarters | its parent's start |
| 90m→22.5m | each 90m quarter splits into four 22.5-minute micro-quarters | its parent's start |

Each quarter is `(level, index ∈ {1,2,3,4}, start_utc, end_utc, parent_id)`, with
`[start, end)` half-open. `confirmed_at` of a *completed* quarter is its `end`; the
in-progress quarter is exposed separately and flagged `is_partial = True`.

Two conventions are forks I cannot resolve from the source material (Q4):

- **Weekly:** Monday–Thursday as Q1–Q4 (adopted) versus a Sunday-18:00 start with Friday as
  Q4. Friday-exclusion is adopted because the Friday-12:00 signal block (§3.3) already
  removes most of Friday, so including it as a quarter would produce a quarter that is
  mostly unusable.
- **Monthly:** 7-day blocks from the month anchor (adopted) versus calendar weeks 1–4.
  The 5-week-month residual is assigned to Q4 by the adopted rule; stated explicitly because
  every implementation handles it differently and silently.

### 8.2 Daily quarters — `exact`

Exactly as specified: Q1 18:00–00:00, Q2 00:00–06:00, Q3 06:00–12:00, Q4 12:00–18:00,
`America/New_York`, DST-aware. Note the consequence: the "day" of the daily-quarter lattice
runs 18:00→18:00 NY, which is **neither** the 17:00 D1 bar (§2.3) **nor** the 00:00 NY true
day (§8.3). Three different day boundaries coexist in this system by necessity. They are
separately named (`d1_bar_day`, `quarter_day`, `true_day`) and never interchanged.

### 8.3 True opens — `exact` given the anchor choice

```
true_day_open    = O of the M1 bar whose open_time is 00:00:00 America/New_York
true_week_open   = O of the M1 bar whose open_time is Monday 18:00:00 America/New_York
true_month_open  = O of the M1 bar at 18:00:00 NY on the Monday of the month's first
                   full trading week                             (default; see below)
true_year_open   = O of the first M1 bar of the first trading day of the calendar year
```

If the exact M1 bar is missing (holiday, feed gap), the open of the **first available** M1
bar after the anchor instant is used, and the record is flagged `anchor_shifted = True` with
the shift in minutes. It is never back-filled from an earlier bar, which would be
look-ahead-free but would reference a pre-anchor price.

`true_month_open` has at least three conventions in circulation (1st day 18:00 NY; first
Monday 18:00 NY; second Monday 18:00 NY). The default is a config enum
(`quarterly.true_month_open_rule`, `prior`) and the alternatives are a robustness variant.
Decision Q4.

### 8.4 Quarter geometry features — `exact`

For any quarter `q` (completed or partial, using closed bars only):
```
q.open   = O of first bar in q           q.close = C of last closed bar in q
q.high   = max H                         q.low   = min L
q.range  = q.high − q.low
q.eff    = |q.close − q.open| / q.range          directional efficiency ∈ [0, 1]
q.pos    = (q.close − q.low) / q.range           close position in range ∈ [0, 1]
q.rel_range = q.range / median{ range of previous K same-level quarters }
q.swept_prior_high = 𝟙[ sweep_up of parent-prior-quarter's high occurred in q ]   (§6.4)
q.swept_prior_low  = 𝟙[ sweep_down ... ]
q.elapsed_frac     = (as_of − q.start) / (q.end − q.start)        (0 < · ≤ 1)
```
`K` default 20 (`prior`). For a partial quarter every quantity uses only bars closed at or
before `as_of`, and `elapsed_frac < 1` flags it.

### 8.5 AMDX phase scoring — **`heuristic`**, and this is the biggest look-ahead trap

The textbook AMDX label of a quarter is read off the **completed** quarter. Using that label
to trade *inside* the same quarter is look-ahead, full stop. Two distinct objects are
therefore produced:

**(a) Retrospective label** — computed on a completed quarter, `confirmed_at = q.end`.
Used for research, for measuring the behavioural claim of §8.0, and as a feature about
*prior* quarters only.

**(b) Running phase scores** — computed on the in-progress quarter from closed bars only,
`confirmed_at = close_time of the latest bar`, carrying `elapsed_frac`. These are what a
live signal may use. They are weaker and **differently distributed** from (a), so the model
treats them as separate features and never reuses coefficients fitted on (a).

Scores in [0, 1], all four computed, all four emitted (no hard label at decision time):

```
HTF bias b ∈ {+1, −1}  from the parent quarter's level structure (§4.2 on the parent TF)

s_accumulation  = clamp( 1 − q.rel_range / α , 0, 1 ) × clamp( 1 − q.eff / β , 0, 1 )
s_manipulation  = 𝟙[ sweep against b of the prior same-level quarter's extreme
                     occurred in q with q.elapsed_frac ≤ 0.5 ]
                  × clamp( sweep.depth / ζ , 0, 1 )
s_distribution  = clamp( q.eff / γ , 0, 1 ) × 𝟙[ sign(q.close − q.open) = b ]
s_continuation  = 𝟙[ q.close beyond prior quarter's extreme in direction b ] × q.eff
s_reversal      = 𝟙[ q.close beyond prior quarter's extreme against b ] × q.eff
label           = argmax of the four (A, M, D, X=max(cont,rev)), reported with its margin
```
Parameters: `α` = 0.8 (`prior`), `β` = 0.35 (`prior`), `γ` = 0.6 (`prior`),
`ζ` = 0.5 ATR (`prior`). None is `tunable`: these four constants define *what the words mean*
rather than being thresholds to fit, and fitting them would make the module unfalsifiable
(any quarter could be relabelled to fit the outcome).

The scores are **not mutually exclusive** by construction, which is why four scores are
emitted rather than one label. A quarter that both ranges narrowly and sweeps is genuinely
ambiguous; forcing a single label would discard that information and manufacture false
precision.

### 8.6 Quarterly bias filter — `exact` mechanism, `heuristic` content

Your rule: "longs preferred below the relevant True Open in Q2/Q3, shorts above."
Implemented as **features**, not as a hard gate:

```
to_rel  = sign(price − true_open(level))                            ∈ {−1, 0, +1}
to_dist = (price − true_open(level)) / ATR14(level TF)
q_idx   = the current quarter index at the relevant level           ∈ {1,2,3,4}
qt_align = 𝟙[ q_idx ∈ {2,3} ] × 𝟙[ sign(direction) = −to_rel ]
```
`qt_align` is exactly your rule as a 0/1 feature. It is a feature rather than a gate so that
the backtest can *measure* its contribution instead of silently discarding half the
candidate population on an untested prior. If it earns a large positive coefficient,
promoting it to a gate is a later, evidence-backed decision.

---

## 9. MMXM — ICT Market Maker Buy/Sell Model

### 9.1 Status

MMXM is a narrative template over four stages. There is no canonical numeric specification
of any stage. The whole of §9 is **`heuristic`**. It is scored 0–1 as you specified, which is
the right call: a binary flag on a template this loose would be noise.

### 9.2 Stages — `operational` definitions

Described for the **sell** model (market maker distributes at the top); the buy model is the
mirror. All on a designated HTF (`mmxm.htf`, default H1, `prior`) with the LTF entry read
on `mmxm.ltf` (default M15, `prior`).

```
Stage 1 — ORIGINAL CONSOLIDATION (OC)
  A maximal window [u, v] of ≥ C_min bars with
      range(u..v) ≤ α_oc × ATR14_v
  containing ≥ 2 buy-side pools and ≥ 2 sell-side pools (§6.1).
  OC = [OC_low, OC_high] = [min L, max H] over the window.
  Confirmed at close_time(v).

Stage 2 — ENGINEERING LIQUIDITY
  After price closes above OC_high (sell model: markup away from OC),
  a run of ≥ E_min consecutive confirmed HIGHER LOWS (§4.2) with no intervening LL.
  Rationale: each higher low leaves resting sell-side liquidity beneath it — the stops
  that the markdown will later harvest. (Buy model: lower highs during markdown.)
  Confirmed at the E_min-th higher low's confirmation.

Stage 3 — SMART MONEY REVERSAL (SMR)
  Price reaches premium in the HTF dealing range ( p ≥ p_min , §6.5 )
  AND trades into an HTF bearish PD array (bearish FVG / OB / breaker / BPR, §5)
  AND an MSS_down occurs on the LTF (§4.4) while inside that array.
  Confirmed at the MSS confirmation.

Stage 4 — RE-DELIVERY
  Price closes back inside OC:  C_t ≤ OC_high.
  Progress measured continuously (§9.3) rather than waiting for completion.
```
Parameters: `C_min` 20 bars, `α_oc` 2.0 ATR, `E_min` 2, `p_min` 0.6 — all `prior`.

### 9.3 Score — `exact` given the stages

```
s_oc     = clamp( 1 − range(OC) / (α_oc × ATR) , 0, 1 ) × clamp( pools / 4 , 0, 1 )
s_eng    = clamp( n_higher_lows / (E_min + 2) , 0, 1 )
s_pd     = max over tagged HTF arrays of ( array.strength )          0 if none tagged
s_mss    = 1 if LTF MSS in the array, else 0.5 if plain CHoCH, else 0
s_prog   = clamp( (entry_px − OC_high) / (SMR_px − OC_high) , 0, 1 )   distance still to travel

MMXM_score = Σ w_i s_i  with Σ w_i = 1
stage_reached = max stage whose confirmation time ≤ as_of          ∈ {0,1,2,3,4}
```
Prior weights `w = (0.15, 0.20, 0.25, 0.30, 0.10)` — declared as a `prior`, **not** fitted
separately. The outer confluence model (§03 §3) receives `MMXM_score` and `stage_reached` as
features and learns their contribution there. Fitting weights inside MMXM *and* learning a
weight on MMXM in the outer model is double-fitting the same data; one layer of learning,
one place.

`confirmed_at` of the score = the latest confirmed stage component. A score computed before
Stage 3 confirms cannot be used to emit a signal (`mmxm.min_stage_for_signal = 3`, `prior`).

---

## 10. Quantitative / regime layer

### 10.1 Volatility — `exact`

```
ATR percentile:      atr_pct = pctrank( ATR14_t ; W_vol )              §0.6
Realised vol:        rv = sqrt( Σ_{i=t−n+1}^{t} r_i² )                 r = log return on the TF
                     annualised: rv × sqrt( bars_per_year(TF) )
Parkinson:           σ_P  = sqrt( (1/(4 n ln2)) Σ ( ln(H_i/L_i) )² )
Garman–Klass:        σ_GK = sqrt( max( (1/n) Σ [ 0.5(ln(H_i/L_i))² − (2ln2 − 1)(ln(C_i/O_i))² ] , 0 ) )
Rogers–Satchell:     σ_RS = sqrt( max( (1/n) Σ [ ln(H/C)ln(H/O) + ln(L/C)ln(L/O) ] , 0 ) )
```
Parameters: `W_vol` 500 bars (`prior`), `n` 20 bars (`prior`).

Three practical points that are usually omitted and cause silent bugs:

1. The GK and RS summands can be **negative for a single bar**. The sum is clamped at 0
   *after* aggregation, never per bar, or the estimator is biased upward.
2. Parkinson and GK assume continuous trading with no jumps and **no overnight gap**. They
   are therefore computed on **intraday** timeframes only (≤ H4) and never on D1/W1 bars
   whose open gaps from the prior close. On D1 use `rv` or close-to-close.
3. `rv` is sampling-frequency dependent. `rv` on M1 and `rv` on M15 over the same wall-clock
   window are different numbers with different microstructure-noise content, so the feature
   name always carries the timeframe (`rv_m15_20`), and M1 realised vol is **not** used
   (bid-ask bounce dominates).

Regime label: `vol_regime = LOW | NORMAL | HIGH` by `atr_pct` tertiles
(cut points 0.33 / 0.67, `prior`). Emitted as both the label and the raw percentile; the
model gets the percentile, the Telegram message gets the label.

### 10.2 Trend vs mean-reversion — `exact`

**ADX** (Wilder, period `n` = 14, `fixed`):
```
+DM_t = (H_t − H_{t−1})  if  (H_t − H_{t−1}) > max(L_{t−1} − L_t, 0)  else 0
−DM_t = (L_{t−1} − L_t)  if  (L_{t−1} − L_t) > max(H_t − H_{t−1}, 0)  else 0
+DI   = 100 × RMA(+DM, n) / RMA(TR, n)        −DI = 100 × RMA(−DM, n) / RMA(TR, n)
DX    = 100 × |+DI − (−DI)| / (+DI + (−DI))
ADX   = RMA(DX, n)
```
Warm-up: first ADX at bar index `2n` (n for the DI smoothing, n for the ADX smoothing);
`None` before. The common "ADX from 14 bars" is wrong and makes early backtest bars garbage.

**Rolling Hurst exponent** (rescaled range, `operational`):
```
window of N closed log returns; block sizes n ∈ {N/2, N/4, N/8, N/16} with n ≥ 16
for each n: split into ⌊N/n⌋ non-overlapping blocks; per block
    Y_k = cumulative deviation from the block mean
    R   = max Y_k − min Y_k ;   S = sample stdev of the block
    (R/S) averaged over blocks → RS(n)
H   = OLS slope of ln RS(n) on ln n
```
Parameters: `N` default 512 (`prior`).

**Honest statement:** rolling Hurst on 512 observations has a standard error of roughly
0.05–0.10 and a known small-sample bias toward 0.5–0.6 even for pure random walks. The
difference between `H = 0.55` and `H = 0.48` on this window is not statistically meaningful.
I expect this feature to contribute nothing and to be ablated out. It is implemented because
you asked for it and because showing it contributes nothing is itself a useful result — but
it will not be presented as a trend detector.

**Variance ratio** (Lo–MacKinlay 1988, heteroskedasticity-consistent — `exact`):
```
X_k = cumulative log price; nq observations; μ̂ = (X_nq − X_0)/nq
σ̂²(1) = (1/(nq−1)) Σ_{k=1}^{nq} (X_k − X_{k−1} − μ̂)²
m      = q (nq − q + 1)(1 − q/nq)
σ̂²(q) = (1/m) Σ_{k=q}^{nq} (X_k − X_{k−q} − qμ̂)²
VR(q)  = σ̂²(q) / σ̂²(1)

δ̂(j)  = [ Σ_{k=j+1}^{nq} (X_k−X_{k−1}−μ̂)² (X_{k−j}−X_{k−j−1}−μ̂)² ]
         / [ Σ_{k=1}^{nq} (X_k−X_{k−1}−μ̂)² ]²
θ̂(q)  = Σ_{j=1}^{q−1} [ 2(q−j)/q ]² δ̂(j)
z*(q)  = sqrt(nq) (VR(q) − 1) / sqrt( θ̂(q) )        ~ N(0,1) under the null
```
`VR > 1` → trending/positively autocorrelated; `VR < 1` → mean-reverting.
Parameters: `q ∈ {2, 4, 8}` (`prior`), window `nq` = 500 (`prior`). The robust `z*` is used
rather than the homoskedastic statistic because FX returns are strongly heteroskedastic —
the homoskedastic version rejects the random walk far too often, which would make this
feature look informative when it is not.

Composite: `trend_regime = TRENDING | MIXED | MEAN_REVERTING` from
`(ADX ≥ adx_hi) ∧ (z*(4) > 1.96)` / neither / `(ADX ≤ adx_lo) ∧ (z*(4) < −1.96)`.
`adx_hi` 25, `adx_lo` 20 (`prior`, Wilder's conventional cuts, not tuned).

### 10.3 Correlation — `exact`

EWMA correlation on overlapping closed H1 log returns:
```
λ = 2^(−1/halflife)
EWMA_cov(x,y)_t = λ·EWMA_cov_{t−1} + (1−λ)·(x_t − μ̂x)(y_t − μ̂y)
ρ_t = cov / sqrt(var_x · var_y)
```
`halflife` 120 H1 bars (≈ 1 trading week, `prior`). Pairs tracked: each instrument against
`USDX_PROXY`, and XAUUSD against each FX pair. Reported in `/status`; used as a feature.
**Not** used as the exposure gate — see §10.5.

### 10.3a Daily macro conditioners — `operational` (revised, Q7)

My Phase 1 position — "no real-yield feed, so nothing" — was a false binary and is
withdrawn. Real yields, nominal yields and the true dollar index are all available free at
**daily** frequency. What is unavailable free is an *intraday* series. Those are different
claims and I conflated them.

Implemented as daily conditioners, updated on the D1 close, under names that state exactly
what they are:

| Feature | Source series | What it is |
|---|---|---|
| `US02Y_nominal_d` | FRED `DGS2` | 2-year nominal Treasury yield, constant maturity |
| `US10Y_nominal_d` | FRED `DGS10` | 10-year nominal Treasury yield |
| `US10Y_real_d` | FRED `DFII10` | 10-year TIPS yield — an actual real yield, not a proxy |
| `US10Y_breakeven_d` | FRED `T10YIE` | 10-year breakeven inflation (`DGS10 − DFII10`) |
| `DXY_official_d` | daily ICE DXY close | the real index, distinct from `USDX_PROXY` (§1.2) |

Derived features: level, 1-day and 20-day change, and the EWMA correlation of XAUUSD daily
returns against `US10Y_real_d` changes and against `DXY_official_d` returns.

Constraints that keep this honest:

- **Daily only.** These are `d`-suffixed and carry the prior day's published value; an
  intraday signal sees the value as of the last completed D1 close, never a same-day figure
  that had not yet been published. Treasury yields are published with a lag and are
  occasionally revised; the adapter stores `published_at` and the backtest reads
  point-in-time, exactly as for the economic calendar (§3.5).
- **Naming.** `US10Y_real_d` is a measured real yield. `USDX_PROXY` remains a proxy and
  keeps that name. `DXY_official_d` is the real index and is used to report the proxy's
  realised correlation, which is the check §1.2 promised.
- **No intraday interpolation.** A daily series is not forward-filled into an intraday
  feature with an implied intraday value. It is a step function, flat within the day.
- Series identifiers above are from FRED's published catalogue and are to be verified in
  Phase 2 against the live API rather than trusted from memory. A free API key goes in
  `.env` (`AURION_FRED_API_KEY`).

These enter as `macro`-tagged features and are ablated with the `regime` arm, not as a
separate arm (`docs/07` §7).

Legacy setting: `regime.real_yield_source` is removed; `regime.macro_daily.enabled`
replaces it.

### 10.4 Session VWAP and z-score — `exact`, with the §0.5 caveat

```
typical_i = (H_i + L_i + C_i) / 3
VWAP_t    = Σ_{i ∈ session, i ≤ t} typical_i V_i / Σ V_i
σ_vw,t    = sqrt( Σ V_i (typical_i − VWAP_t)² / Σ V_i )
z_t       = (C_t − VWAP_t) / σ_vw,t
```
Session = the containing enabled window, or the 00:00-NY true day if none. Warm-up:
`≥ vwap_min_bars` (default 20 M5 bars, `prior`) and `σ_vw > 0`, else `None`.
Weighted by **tick volume**, with every consequence in §0.5. On a venue whose tick
throttling changes, this feature's distribution shifts with no change in the market; it is
tagged `volume_proxy` for exactly that reason.

### 10.5 Conflicting-exposure control by USD-beta netting — `exact`

This replaces "correlation filter blocks conflicting USD-pair signals", which as specified
is circular: `USDX_PROXY` is a function of EURUSD/GBPUSD/USDJPY, so using it to filter
EURUSD signals filters a variable against itself. Direct netting is both objective and
non-circular:

```
For a signal s on instrument i with direction d_s ∈ {+1 long, −1 short}:
  usd_sign(i) = −1 if USD is the quote currency (EURUSD, GBPUSD, AUDUSD, XAUUSD)
              = +1 if USD is the base currency  (USDJPY, USDCAD)
  usd_exposure(s) = d_s × usd_sign(i) × risk_fraction(s) × beta_usd(i)

  beta_usd(i) = OLS slope of r_i on r_USDX_PROXY over the trailing W_beta H1 bars
                (sign-corrected so beta is reported w.r.t. USD strength)

Gate:  |  Σ over active+pending signals of usd_exposure  +  usd_exposure(candidate) |
         ≤ exposure.max_usd_risk_fraction
```
Parameters: `W_beta` 500 H1 bars (`prior`); `max_usd_risk_fraction` default 1.5 × the
per-trade risk (`prior`) — i.e. at most ~1.5 trades' worth of net USD risk at once.

`beta_usd` still uses the proxy, but here the proxy is a *regression basis* for measuring
exposure magnitude, not an independent signal, so the circularity objection does not apply.
XAUUSD is included: gold is traded against USD and its USD beta is measured, not assumed.

For XAUUSD specifically an additional gate is available
(`exposure.block_gold_vs_usd_conflict`, default `false`, `prior`): block a long-gold
candidate while a net-long-USD position set is active. Default off because it is a belief
about the gold/USD relationship, and the beta netting above already measures it.

---

## 11. Feature vector

The feature vector is **the** interface between analysis and the model. It is ordered,
versioned (`feature_schema_version`), and immutable once a version is released; adding a
feature creates a new version and invalidates every stored model fitted on the old one.
Persisted at decision time for every candidate — not recomputed later — so that the training
set and the audit trail are the same object.

Groups, with the count of features each contributes (exact list in Phase 2):

| Group | Examples | Tag |
|---|---|---|
| Session / time | window id, minutes into window, weekday, quarter index at 4 levels | `time` |
| Structure | trend state per TF, bars since BOS/CHoCH/MSS, displacement magnitude in ATR | `structure` |
| ICT arrays | distance to nearest unmitigated FVG/OB/breaker/BPR in ATR, their fill state | `ict` |
| Liquidity | sweep depth/recency, pool touches, distance to PDH/PDL/PWH/PWL/Asian H/L | `liquidity` |
| PD / range | position in dealing range, in-OTE flag, distance to equilibrium | `pd` |
| S&R | distance to nearest level in ATR, level strength, pivot/round-number distances | `sr` |
| Quarterly | running AMDX scores + elapsed_frac, true-open relations, `qt_align` | `quarterly` |
| MMXM | score, stage_reached | `mmxm` |
| Regime | atr_pct, rv, GK/Parkinson, ADX, Hurst, VR z*, vol/trend labels | `regime` |
| Volume proxy | VWAP z, rejection weights | `volume_proxy` |
| Macro daily | real/nominal yields, breakeven, official DXY: level, 1d and 20d change, XAU correlations (§10.3a) | `macro` |
| Exposure | net USD exposure, correlations | `exposure` |

Tags matter operationally: the ablation study removes whole tag groups
(`docs/04-validation-protocol.md` §4), and `volume_proxy` can be dropped wholesale if the
broker's tick aggregation changes.
