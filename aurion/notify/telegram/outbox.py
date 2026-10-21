"""Idempotent Telegram outbox. `docs/03-signal-engine.md` §7.2.

The dedupe key is ``{signal_id}:{state}:{revision}`` with a UNIQUE constraint, and the
enqueue happens **inside the same database transaction as the state transition**. That is
what makes a crash between "state changed" and "message sent" unable to lose or duplicate a
notification: the send is at-least-once on the wire and exactly-once observably, because
duplicates are rejected by the key before the API call.

A message that exhausts its retries goes to a dead-letter row and raises an admin alert. It
is never dropped silently.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Integer, String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from aurion.core.timeframe import UTC
from aurion.data.store import Base


class OutboxKind(StrEnum):
    SIGNAL = "signal"
    TRANSITION = "transition"
    HEALTH = "health"
    COMMAND_REPLY = "command_reply"
    HEARTBEAT = "heartbeat"


class OutboxRow(Base):
    __tablename__ = "outbox"

    dedupe_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[str] = mapped_column(String(8192))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    telegram_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dead_lettered: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str] = mapped_column(String(512), default="")


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    dedupe_key: str
    chat_id: str
    kind: OutboxKind
    text: str
    chart_path: str | None = None
    attachments: dict[str, str] = field(default_factory=dict)

    def payload(self) -> str:
        return json.dumps(
            {"text": self.text, "chart_path": self.chart_path, **self.attachments},
            sort_keys=True,
        )


def dedupe_key(signal_id: str, state: str, revision: int = 0) -> str:
    """``{signal_id}:{state}:{revision}``. The UNIQUE constraint does the rest."""
    return f"{signal_id}:{state}:{revision}"


class Outbox:
    """Durable, idempotent message queue."""

    __slots__ = ("_backoff", "_max_attempts")

    def __init__(
        self,
        max_attempts: int = 5,
        backoff_seconds: Sequence[float] = (1, 2, 4, 8, 16),
    ) -> None:
        self._max_attempts = max_attempts
        self._backoff = tuple(backoff_seconds)

    async def enqueue(
        self, session: AsyncSession, message: OutboxMessage, now: datetime | None = None
    ) -> bool:
        """Add a message. Returns False if the key already exists.

        Call this inside the transaction that records the state change, so the two commit
        together.
        """
        existing = await session.get(OutboxRow, message.dedupe_key)
        if existing is not None:
            return False
        session.add(
            OutboxRow(
                dedupe_key=message.dedupe_key,
                chat_id=message.chat_id,
                kind=str(message.kind),
                payload_json=message.payload(),
                attempts=0,
                next_attempt_at=now or datetime.now(tz=UTC),
            )
        )
        try:
            await session.flush()
        except IntegrityError:
            # A concurrent enqueue of the same key. The constraint is the authority.
            await session.rollback()
            return False
        return True

    async def due(
        self, session: AsyncSession, now: datetime, limit: int = 20
    ) -> list[OutboxRow]:
        result = await session.execute(
            select(OutboxRow)
            .where(
                OutboxRow.sent_at.is_(None),
                OutboxRow.dead_lettered.is_(False),
                OutboxRow.next_attempt_at <= now,
            )
            .order_by(OutboxRow.next_attempt_at)
            .limit(limit)
        )
        return list(result.scalars())

    async def mark_sent(
        self, session: AsyncSession, row: OutboxRow, message_id: int | None, now: datetime
    ) -> None:
        row.sent_at = now
        row.telegram_message_id = message_id
        await session.flush()

    async def mark_failed(
        self, session: AsyncSession, row: OutboxRow, error: str, now: datetime
    ) -> bool:
        """Record a failure. Returns True when the row has been dead-lettered."""
        row.attempts += 1
        row.last_error = error[:512]
        if row.attempts >= self._max_attempts:
            row.dead_lettered = True
            await session.flush()
            return True
        delay = self._backoff[min(row.attempts - 1, len(self._backoff) - 1)]
        row.next_attempt_at = now + timedelta(seconds=delay)
        await session.flush()
        return False

    async def dead_letters(self, session: AsyncSession) -> list[OutboxRow]:
        result = await session.execute(
            select(OutboxRow).where(OutboxRow.dead_lettered.is_(True))
        )
        return list(result.scalars())


class TokenBucket:
    """Rate limiter sized below Telegram's documented limits.

    Documented as 30 messages/second globally and roughly 20/minute per group. The defaults
    sit under both, because being rate-limited by the API costs a retry round trip and, on a
    lifecycle transition, delays the alert that matters most.
    """

    __slots__ = ("_capacity", "_rate", "_tokens", "_updated")

    def __init__(self, rate_per_second: float, capacity: float | None = None) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate must be positive")
        self._rate = rate_per_second
        self._capacity = capacity if capacity is not None else max(rate_per_second, 1.0)
        self._tokens = self._capacity
        self._updated: float | None = None

    def take(self, at: float, tokens: float = 1.0) -> float:
        """Consume a token, returning the seconds to wait before sending."""
        if self._updated is None:
            self._updated = at
        elapsed = max(at - self._updated, 0.0)
        self._updated = at
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        if self._tokens >= tokens:
            self._tokens -= tokens
            return 0.0
        deficit = tokens - self._tokens
        self._tokens = 0.0
        return deficit / self._rate
