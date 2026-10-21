"""Data adapter protocol. `docs/05-data-contracts.md` §1.

**Deliberately absent: any method that submits, amends or cancels an order.** Execution
cannot be added by extending an implementation; it would require widening this protocol,
which is a visible diff in review.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from aurion.data.types import Heartbeat, M1Candle, Quote


@dataclass(frozen=True, slots=True)
class AdapterHealth:
    name: str
    connected: bool
    last_message_at: datetime | None
    detail: str = ""


class AdapterError(RuntimeError):
    """Adapter-level failure. Carries enough context to diagnose without the traceback."""

    def __init__(self, adapter: str, operation: str, detail: str) -> None:
        super().__init__(f"[{adapter}] {operation}: {detail}")
        self.adapter = adapter
        self.operation = operation
        self.detail = detail


@runtime_checkable
class DataAdapter(Protocol):
    name: str

    def to_vendor_symbol(self, instrument: str) -> str: ...
    def to_canonical(self, vendor_symbol: str) -> str: ...

    async def fetch_candles(
        self,
        instrument: str,
        granularity: Literal["M1"],
        start_utc: datetime,
        end_utc: datetime,
    ) -> list[M1Candle]:
        """Complete bars only. Incomplete candles are dropped, never stored and corrected."""
        ...

    def stream_prices(
        self, instruments: Sequence[str]
    ) -> AsyncIterator[Quote | Heartbeat]:
        """Async generator, so calling this returns an iterator rather than a coroutine."""
        ...

    async def latest_quote(self, instrument: str) -> Quote: ...

    async def health(self) -> AdapterHealth: ...

    async def aclose(self) -> None: ...
