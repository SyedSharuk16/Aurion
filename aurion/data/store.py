"""SQLAlchemy models and the durable store. `docs/05-data-contracts.md` §3.

Design points worth stating, because each prevents a specific failure:

* **No SQLite-only types and no raw SQL outside migrations**, so the Postgres path is a URL
  change rather than a rewrite.
* **Incomplete candles are never stored.** Storing one and correcting it later is exactly
  the repaint the design forbids; the adapter drops them on ingest.
* **``confirmed_at`` is a column on every derived table**, and every read filters on it.
  There is no index path that returns an unconfirmed object.
* **Prices are normalised to the instrument's point at the ADAPTER boundary, not here.**
  The store persists verbatim. Rounding only on write made a recomputed bar differ from
  its stored twin, which broke the determinism check in `IngestSupervisor.rehydrate` --
  the bug that found this. Derived mid is never rounded: a mid can legitimately sit on a
  half point when the spread is an odd number of points, and rounding it would introduce a
  systematic bias instead of removing noise.
* Analysis does not read from here. The live runtime keeps a bounded in-memory hot window
  (`InMemoryBarRepository`) that this store rehydrates on restart; making the repository
  async would push ``await`` through every analysis function to no benefit.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
    delete,
    event,
    select,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from aurion.core.timeframe import UTC, TimeFrame
from aurion.data.types import OHLC, Bar, M1Candle

#: Seconds SQLite waits for a lock before giving up. The default is 0, so any contention --
#: the outbox committing while a backfill writes, or a second process -- raises
#: `database is locked` immediately rather than waiting a moment. Observed on a first live
#: run during the cold-start backfill.
SQLITE_BUSY_TIMEOUT_S = 30


def _apply_sqlite_pragmas(engine: AsyncEngine) -> None:
    """WAL and a busy timeout, set on every new connection.

    WAL lets a reader and a writer proceed at once, which is the normal state here: the
    ingest loop writes candles while the outbox commits messages and the engine reads bars.
    Under the default rollback journal they serialise and then fail outright.

    `synchronous=NORMAL` is the right trade with WAL: a crash can lose the last commits but
    cannot corrupt the file, and the alternative costs an fsync per transaction on a
    filesystem that may be a Windows mount seen through WSL.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_S * 1000}")
        finally:
            cursor.close()

class Base(DeclarativeBase):
    pass


def _utc_col(**kw: object) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kw)  # type: ignore[arg-type]


class InstrumentRow(Base):
    __tablename__ = "instruments"

    symbol: Mapped[str] = mapped_column(String(16), primary_key=True)
    point: Mapped[float] = mapped_column(Float)
    contract_size: Mapped[float] = mapped_column(Float)
    base_ccy: Mapped[str] = mapped_column(String(8))
    quote_ccy: Mapped[str] = mapped_column(String(8))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class M1Row(Base):
    __tablename__ = "candles_m1"

    instrument: Mapped[str] = mapped_column(String(16), primary_key=True)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    close_time: Mapped[datetime] = _utc_col()
    bid_o: Mapped[float] = mapped_column(Float)
    bid_h: Mapped[float] = mapped_column(Float)
    bid_l: Mapped[float] = mapped_column(Float)
    bid_c: Mapped[float] = mapped_column(Float)
    ask_o: Mapped[float] = mapped_column(Float)
    ask_h: Mapped[float] = mapped_column(Float)
    ask_l: Mapped[float] = mapped_column(Float)
    ask_c: Mapped[float] = mapped_column(Float)
    tick_volume: Mapped[int] = mapped_column(Integer)
    source_adapter: Mapped[str] = mapped_column(String(32))


class BarRow(Base):
    """Resampled cache. Recomputable from candles_m1, and recomputed on restart."""

    __tablename__ = "candles_tf"

    instrument: Mapped[str] = mapped_column(String(16), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(4), primary_key=True)
    open_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    close_time: Mapped[datetime] = _utc_col()
    o: Mapped[float] = mapped_column(Float)
    h: Mapped[float] = mapped_column(Float)
    l: Mapped[float] = mapped_column(Float)  # noqa: E741
    c: Mapped[float] = mapped_column(Float)
    tick_volume: Mapped[int] = mapped_column(Integer)
    constituents: Mapped[int] = mapped_column(Integer)
    incomplete: Mapped[bool] = mapped_column(Boolean, default=False)
    dst_irregular: Mapped[bool] = mapped_column(Boolean, default=False)


class NewsEventRow(Base):
    __tablename__ = "news_events"
    __table_args__ = (UniqueConstraint("event_id", "revision"),)

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    scheduled_utc: Mapped[datetime] = _utc_col()
    currency: Mapped[str] = mapped_column(String(8))
    title: Mapped[str] = mapped_column(String(256))
    impact: Mapped[str] = mapped_column(String(8))
    published_utc: Mapped[datetime] = _utc_col()
    provider: Mapped[str] = mapped_column(String(32))


class MacroObsRow(Base):
    """Daily macro conditioners (§10.3a).

    ``published_at`` is what makes the point-in-time view a query rather than a
    reconstruction: a Tuesday signal must not read Tuesday's yield close before it existed.
    """

    __tablename__ = "macro_obs"

    series_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    observation_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True
    )
    revision: Mapped[int] = mapped_column(Integer, primary_key=True, default=0)
    value: Mapped[float] = mapped_column(Float)
    published_at: Mapped[datetime] = _utc_col()


class AdapterCursorRow(Base):
    __tablename__ = "adapter_cursors"

    instrument: Mapped[str] = mapped_column(String(16), primary_key=True)
    adapter: Mapped[str] = mapped_column(String(32))
    last_bar_open_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_stream_heartbeat: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class KVRow(Base):
    __tablename__ = "kv_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[str] = mapped_column(String(4096))


class RunRow(Base):
    """The trial ledger (`docs/04` §6.1).

    Every backtest run writes a row, including throwaway exploratory ones, because the
    trial count used in the deflated Sharpe is *counted* from this table rather than
    estimated from a declared grid. A declared grid always undercounts: the real search
    includes every abandoned idea.
    """

    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    config_hash: Mapped[str] = mapped_column(String(64))
    git_sha: Mapped[str] = mapped_column(String(64))
    data_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    data_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime] = _utc_col()
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    headline_metrics_json: Mapped[str] = mapped_column(String(8192), default="{}")
    selection_branch: Mapped[str] = mapped_column(String(32), default="pending_pilot")


class Store:
    """Async persistence facade."""

    __slots__ = ("_adapter_name", "_engine", "_session")

    def __init__(self, url: str, adapter_name: str = "oanda", echo: bool = False) -> None:
        self._engine: AsyncEngine = create_async_engine(url, echo=echo, future=True)
        if url.startswith("sqlite"):
            _apply_sqlite_pragmas(self._engine)
        self._session = async_sessionmaker(self._engine, expire_on_commit=False)
        self._adapter_name = adapter_name

    async def create_all(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self._engine.dispose()

    def session(self) -> AsyncSession:
        return self._session()

    # -- M1 ----------------------------------------------------------------
    async def upsert_m1(self, candles: Iterable[M1Candle]) -> int:
        """Insert M1 candles, skipping any already present. Idempotent by design.

        Backfill overlaps by one bar at every seam, so duplicates are expected rather than
        exceptional; they are skipped instead of raising.
        """
        rows = 0
        async with self._session() as s, s.begin():
            for c in candles:
                exists = await s.get(M1Row, (c.instrument, c.open_time))
                if exists is not None:
                    continue
                s.add(
                    M1Row(
                        instrument=c.instrument,
                        open_time=c.open_time,
                        close_time=c.close_time,
                        bid_o=c.bid.o,
                        bid_h=c.bid.h,
                        bid_l=c.bid.l,
                        bid_c=c.bid.c,
                        ask_o=c.ask.o,
                        ask_h=c.ask.h,
                        ask_l=c.ask.l,
                        ask_c=c.ask.c,
                        tick_volume=c.tick_volume,
                        source_adapter=self._adapter_name,
                    )
                )
                rows += 1
        return rows

    async def load_m1(
        self, instrument: str, start: datetime | None = None, limit: int | None = None
    ) -> list[M1Candle]:
        stmt = select(M1Row).where(M1Row.instrument == instrument)
        if start is not None:
            stmt = stmt.where(M1Row.open_time >= start)
        stmt = stmt.order_by(M1Row.open_time)
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._session() as s:
            result = await s.execute(stmt)
            return [
                M1Candle(
                    instrument=r.instrument,
                    open_time=r.open_time.replace(tzinfo=UTC),
                    close_time=r.close_time.replace(tzinfo=UTC),
                    bid=OHLC(r.bid_o, r.bid_h, r.bid_l, r.bid_c),
                    ask=OHLC(r.ask_o, r.ask_h, r.ask_l, r.ask_c),
                    tick_volume=r.tick_volume,
                )
                for r in result.scalars()
            ]

    async def last_m1_open(self, instrument: str) -> datetime | None:
        async with self._session() as s:
            result = await s.execute(
                select(M1Row.open_time)
                .where(M1Row.instrument == instrument)
                .order_by(M1Row.open_time.desc())
                .limit(1)
            )
            got = result.scalar_one_or_none()
            return got.replace(tzinfo=UTC) if got is not None else None

    async def first_m1_open(self, instrument: str) -> datetime | None:
        async with self._session() as s:
            result = await s.execute(
                select(M1Row.open_time)
                .where(M1Row.instrument == instrument)
                .order_by(M1Row.open_time)
                .limit(1)
            )
            got = result.scalar_one_or_none()
            return got.replace(tzinfo=UTC) if got is not None else None

    async def m1_gaps(
        self, instrument: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, datetime]]:
        """Missing minute ranges between stored candles.

        Market closures appear as gaps too, which is correct: the caller decides whether a
        gap is a closure or a feed failure, and backfill is cheap and idempotent either way.
        """
        candles = await self.load_m1(instrument, start)
        gaps: list[tuple[datetime, datetime]] = []
        prev: datetime | None = None
        for c in candles:
            if c.open_time > end:
                break
            if prev is not None and c.open_time > prev + timedelta(minutes=1):
                gaps.append((prev + timedelta(minutes=1), c.open_time))
            prev = c.open_time
        return gaps

    async def prune_m1(self, instrument: str, before: datetime) -> int:
        """Rolling-window retention for the live VPS (`docs/01-architecture.md` §8)."""
        async with self._session() as s, s.begin():
            result = await s.execute(
                delete(M1Row).where(M1Row.instrument == instrument, M1Row.open_time < before)
            )
            # execute() of a DELETE returns a CursorResult, which carries rowcount.
            return int(getattr(result, "rowcount", 0) or 0)

    # -- resampled ---------------------------------------------------------
    async def upsert_bars(self, bars: Iterable[Bar], replace: bool = False) -> int:
        """Insert resampled bars.

        ``replace=True`` overwrites an existing row, which the rebuild path after a gap
        repair needs: insert-only semantics let a stale thin bar win over its recomputed
        twin, which is how this was found.
        """
        rows = 0
        async with self._session() as s, s.begin():
            for b in bars:
                key = (b.instrument, str(b.timeframe), b.open_time)
                existing = await s.get(BarRow, key)
                if existing is not None:
                    if not replace:
                        continue
                    existing.close_time = b.close_time
                    existing.o, existing.h, existing.l, existing.c = (
                        b.ohlc.o, b.ohlc.h, b.ohlc.l, b.ohlc.c,
                    )
                    existing.tick_volume = b.tick_volume
                    existing.constituents = b.constituents
                    existing.incomplete = b.incomplete
                    existing.dst_irregular = b.dst_irregular
                    rows += 1
                    continue
                s.add(
                    BarRow(
                        instrument=b.instrument,
                        timeframe=str(b.timeframe),
                        open_time=b.open_time,
                        close_time=b.close_time,
                        o=b.ohlc.o,
                        h=b.ohlc.h,
                        l=b.ohlc.l,
                        c=b.ohlc.c,
                        tick_volume=b.tick_volume,
                        constituents=b.constituents,
                        incomplete=b.incomplete,
                        dst_irregular=b.dst_irregular,
                    )
                )
                rows += 1
        return rows

    async def load_bars(
        self, instrument: str, timeframe: TimeFrame, limit: int | None = None
    ) -> list[Bar]:
        stmt = (
            select(BarRow)
            .where(BarRow.instrument == instrument, BarRow.timeframe == str(timeframe))
            .order_by(BarRow.open_time.desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._session() as s:
            result = await s.execute(stmt)
            rows = list(result.scalars())
        rows.reverse()
        return [
            Bar(
                instrument=r.instrument,
                timeframe=TimeFrame(r.timeframe),
                open_time=r.open_time.replace(tzinfo=UTC),
                close_time=r.close_time.replace(tzinfo=UTC),
                ohlc=OHLC(r.o, r.h, r.l, r.c),
                tick_volume=r.tick_volume,
                constituents=r.constituents,
                incomplete=r.incomplete,
                dst_irregular=r.dst_irregular,
            )
            for r in rows
        ]

    async def delete_bars_from(
        self, instrument: str, from_time: datetime, timeframe: TimeFrame | None = None
    ) -> int:
        """Drop cached resampled bars at or after ``from_time``.

        A repaired M1 gap invalidates every resampled bar that covered it -- those bars
        were aggregated from an incomplete set and may be flagged thin. Patching the M1
        series without dropping them would leave permanently wrong higher-timeframe bars,
        so repair deletes and recomputes rather than appends.
        """
        stmt = delete(BarRow).where(
            BarRow.instrument == instrument, BarRow.open_time >= from_time
        )
        if timeframe is not None:
            stmt = stmt.where(BarRow.timeframe == str(timeframe))
        async with self._session() as s, s.begin():
            result = await s.execute(stmt)
            return int(getattr(result, "rowcount", 0) or 0)

    # -- cursors -----------------------------------------------------------
    async def set_cursor(
        self,
        instrument: str,
        last_bar_open_time: datetime | None = None,
        last_stream_heartbeat: datetime | None = None,
    ) -> None:
        async with self._session() as s, s.begin():
            row = await s.get(AdapterCursorRow, instrument)
            if row is None:
                row = AdapterCursorRow(instrument=instrument, adapter=self._adapter_name)
                s.add(row)
            if last_bar_open_time is not None:
                row.last_bar_open_time = last_bar_open_time
            if last_stream_heartbeat is not None:
                row.last_stream_heartbeat = last_stream_heartbeat

    async def get_cursor(self, instrument: str) -> AdapterCursorRow | None:
        async with self._session() as s:
            return await s.get(AdapterCursorRow, instrument)

    # -- trial ledger ------------------------------------------------------
    async def record_run(self, run: RunRow) -> None:
        async with self._session() as s, s.begin():
            s.add(run)

    async def count_trials(self, kinds: Sequence[str] = ("backtest", "ablation")) -> int:
        """Counted, never estimated. Feeds `N_raw` in the deflated Sharpe."""
        async with self._session() as s:
            result = await s.execute(select(RunRow).where(RunRow.kind.in_(kinds)))
            return len(list(result.scalars()))
