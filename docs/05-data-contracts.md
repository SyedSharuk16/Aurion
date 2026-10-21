# 05 — Data contracts: adapter interface, schema, stores

Interface shapes are given as signatures for review. They are not implementations.

---

## 1. Adapter interface

```python
class DataAdapter(Protocol):
    name: str
    def to_vendor_symbol(self, instrument: str) -> str: ...
    def to_canonical(self, vendor_symbol: str) -> str: ...

    async def fetch_candles(
        self,
        instrument: str,
        granularity: Literal["M1"],          # M1 only: everything else is local (§2.1)
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[Candle]: ...                   # complete bars only; incomplete are dropped

    async def stream_prices(
        self, instruments: Sequence[str]
    ) -> AsyncIterator[Quote | Heartbeat]: ...

    async def latest_quote(self, instrument: str) -> Quote: ...
    async def health(self) -> AdapterHealth: ...
```

Deliberately absent: any method that submits, amends or cancels an order. Execution cannot
be added without widening this protocol, which is a visible diff.

```python
@dataclass(frozen=True, slots=True)
class Candle:
    instrument: str
    open_time: datetime      # UTC, tz-aware, == bucket start
    close_time: datetime     # UTC, exclusive end
    bid: OHLC
    ask: OHLC
    tick_volume: int
    complete: bool           # False candles are never stored

@dataclass(frozen=True, slots=True)
class Quote:
    instrument: str
    time: datetime           # UTC
    bid: float
    ask: float

@dataclass(frozen=True, slots=True)
class Heartbeat:
    time: datetime
```

`mid` is a derived property, never a stored or fetched field, so mid can never disagree with
the stored bid/ask (`docs/02-definitions.md` §0.3).

### 1.1 OANDA v20 adapter (first implementation)

| Need | Endpoint |
|---|---|
| Historical M1 | `GET /v3/instruments/{instrument}/candles` with `granularity=M1`, `price=BA` |
| Live prices | `GET /v3/accounts/{accountID}/pricing/stream?instruments=…` |
| Spot snapshot | `GET /v3/accounts/{accountID}/pricing?instruments=…` |

Symbols are underscored (`XAU_USD`, `EUR_USD`, `USD_JPY`); canonical internal names are not
(`XAUUSD`). Mapping lives in the adapter, nowhere else.

Implementation requirements:

- `price=BA` (bid and ask), never `M`. Mid-only candles make the spread unknowable and
  force `spread_model: fixed`, which degrades the cost model (`docs/04` §2.1).
- Candles with `complete: false` are discarded on ingest, not stored and later corrected.
  Storing an incomplete bar and updating it is exactly the repaint the design forbids.
- Request pagination: the endpoint caps candles per request (documented as 5000 at time of
  writing — **verify against the current OANDA docs during Phase 2 rather than trusting this
  number**). Backfill walks `from`/`to` windows with overlap of one bar and asserts
  continuity at the seams.
- `dailyAlignment` / `alignmentTimezone` / `weeklyAlignment` are **not** used. They are the
  vendor-resampling feature this design rejects; M1 has no alignment ambiguity, and using
  them for M1 would be a no-op that invites later misuse.
- The streaming endpoint allows a limited number of concurrent connections per account
  (also to be verified in Phase 2). One stream carries all instruments.
- Heartbeats gap-detect: no `PRICE` or `HEARTBEAT` for `stream_idle_timeout_s`
  (default 15) ⇒ tear down and reconnect with backoff + jitter, then REST-backfill from the
  last stored bar. Reconnect never assumes continuity.
- Practice and live environments have different hostnames; `AURION_OANDA_ENV` selects. Only
  read endpoints are called in either.

### 1.2 Stubs

`mt5.py` and `twelvedata.py` implement the protocol and raise `NotImplementedError` with a
message naming what remains, plus a complete symbol mapping table and a note of each
vendor's known divergences (MT5: broker-dependent server time and bar alignment, no native
async, Windows-only terminal dependency; TwelveData: no bid/ask candles on standard plans, so
`spread_model` would be forced to `modelled`). Writing those divergences down now is the
point of the stubs — they are the reasons an adapter swap is not free.

---

## 2. Economic calendar provider

```python
class CalendarProvider(Protocol):
    async def events(
        self, start_utc: datetime, end_utc: datetime, as_published_before: datetime | None
    ) -> list[CalendarEvent]: ...

@dataclass(frozen=True, slots=True)
class CalendarEvent:
    event_id: str
    scheduled_utc: datetime
    currency: str
    title: str
    impact: Literal["low", "medium", "high"]
    published_utc: datetime        # when this version of the row became known
    revision: int
```

`as_published_before` is what makes the point-in-time backtest possible
(`docs/02-definitions.md` §3.5). A provider that cannot supply `published_utc` is usable live
but marks every backtest run `calendar_point_in_time: false`, which the validation report
prints in its header rather than hiding.

---

## 3. Database schema

SQLAlchemy 2.x declarative, async driver (`aiosqlite` / `asyncpg`). No SQLite-only types, no
raw SQL outside migrations, so the Postgres path is a URL change. Alembic from the first
migration.

```
instruments(symbol PK, point, pip_factor, contract_size, quote_ccy, base_ccy,
            sessions_whitelist_json, enabled)

candles_m1(instrument FK, open_time, close_time,
           bid_o, bid_h, bid_l, bid_c, ask_o, ask_h, ask_l, ask_c,
           tick_volume, source_adapter,
           PK(instrument, open_time))
           -- rolling retention on the VPS (docs/01 §8)

candles_tf(instrument FK, timeframe, open_time, close_time,
           o, h, l, c, tick_volume, incomplete BOOL, constituents INT,
           PK(instrument, timeframe, open_time))
           -- cache; recomputable from candles_m1, and recomputed on restart

swings(id PK, instrument, timeframe, kind, bar_time, price,
       half_width, confirmed_at,
       UNIQUE(instrument, timeframe, kind, bar_time))

arrays(id PK, instrument, timeframe, kind,          -- fvg|ob|breaker|mitigation|bpr|ifvg
       lo, hi, direction, origin_bar_time, size_atr,
       confirmed_at, state, fill_frac, mitigated_at, invalidated_at)

pools(id PK, instrument, timeframe, side, level, width, touches,
      kind,                                          -- equal|pdh|pdl|pwh|pwl|asian|session
      confirmed_at, swept_at, sweep_depth_atr)

levels(id PK, instrument, timeframe, price, strength, method, touches,
       computed_at, expires_at)                      -- recomputed, not mutated

quarters(id PK, level, idx, start_utc, end_utc, parent_id FK,
         open, high, low, close, eff, pos, rel_range,
         amdx_scores_json, label, is_partial, elapsed_frac, confirmed_at)

news_events(event_id, revision, scheduled_utc, currency, title, impact,
            published_utc, PK(event_id, revision))

candidates(id PK, instrument, as_of, entry_model, passed_step, rejected_reason,
           feature_schema_version, features_json, config_hash, run_id)
           -- EVERY candidate, accepted or rejected: this is the training set

signals(id PK, candidate_id FK, instrument, direction, entry, entry_type,
        sl, tp1, tp2, tp3, rr1, p_hat, expected_r, lots,
        session_label, quarter_label, reasons_json, invalidation_text,
        expires_at, model_artifact_id, config_hash, created_at, is_paper)

signal_states(id PK, signal_id FK, state, at_utc, price, realised_r, revision,
              UNIQUE(signal_id, state, revision))

outbox(dedupe_key PK, chat_id, payload_json, state, attempts, next_attempt_at,
       sent_at, telegram_message_id, dead_lettered BOOL)

model_artifacts(id PK, kind, feature_schema_version, train_start, train_end,
                fold_id, metrics_json, blob_path, created_at)

runs(run_id PK, kind, config_hash, git_sha, data_start, data_end,
     started_at, finished_at, headline_metrics_json)   -- the trial ledger (docs/04 §6.1)

adapter_cursors(instrument PK, last_bar_open_time, last_stream_heartbeat, adapter)

kv_state(key PK, value_json)                           -- pause flag, heartbeat marks
```

Design points worth flagging:

- `candidates` stores rejected candidates with their full feature vector. This is what makes
  the training set 10–50× larger than the signal set (`docs/03` §3.2) and what makes the
  gate funnel auditable. It is also the largest mutable table; retention is config.
- `levels` rows are **replaced**, never updated in place, so a historical query can
  reconstruct the level set as of any past time.
- `signal_states` has `UNIQUE(signal_id, state, revision)`, which makes a duplicate
  transition a constraint violation rather than a duplicate Telegram message.
- `arrays`, `pools`, `swings` all carry `confirmed_at`. Every `MarketView` query filters on
  it. There is no index path that returns an unconfirmed object.
- Indices: `(instrument, open_time)` on candles; `(instrument, confirmed_at)` on every
  derived table; `(instrument, state)` on signals; `(next_attempt_at)` on outbox.

---

## 4. Feature store

`candidates.features_json` holds the ordered feature vector exactly as the model saw it,
with `feature_schema_version`. Rules:

1. A released `feature_schema_version` is immutable. Adding, removing or redefining a
   feature creates a new version.
2. Every `model_artifact` records the version it was fitted on. The runtime refuses to apply
   a model to a different version — it does not coerce or pad.
3. Features are computed in exactly one place (`features/build.py`). No module computes a
   feature "inline" for a gate; gates consume the same vector.

(3) is what prevents the most insidious form of backtest/live divergence: the same quantity
computed twice with slightly different edge handling.

---

## 5. Fixture data

- **Unit tests** run on synthetic, hand-constructed OHLC series committed under
  `tests/fixtures/synthetic/`, each with a YAML ground-truth file: e.g. a 60-bar series
  containing exactly two bullish FVGs at known indices with known sizes, one swept pool, one
  MSS. Every definition in `docs/02-definitions.md` gets at least one positive and one
  near-miss negative case (a gap one point below the `g × ATR` threshold; a penetration that
  never reclaims; a plateau that must *not* be a swing).
- **Integration tests** use a locally cached real dataset fetched by
  `scripts/fetch_fixture_data.py` into `tests/fixtures/real/`, which is gitignored. Vendor
  market data is not redistributed in this repository. Decision Q11.
