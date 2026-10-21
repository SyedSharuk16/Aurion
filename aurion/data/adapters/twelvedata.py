"""TwelveData adapter stub.

Known divergences, which decide whether this adapter is usable at all:

* **No bid/ask candles on standard plans.** Only mid OHLC is available, which forces
  ``costs.spread_model: modelled`` and removes the per-leg bid/ask basis that
  `docs/02-definitions.md` §0.3 depends on. That degrades the cost model materially and is
  the single reason this is not the primary adapter.
* **Request-rate limits by plan tier**, so a full M1 backfill needs pacing and may take
  days on a free tier.
* **WebSocket streaming is a separate product** from the REST history, with its own symbol
  conventions.
* **Gold is quoted as ``XAU/USD``** with a different precision convention.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Literal

from aurion.data.adapters.base import AdapterHealth
from aurion.data.types import Heartbeat, M1Candle, Quote

SYMBOL_MAP = {
    "XAUUSD": "XAU/USD",
    "EURUSD": "EUR/USD",
    "GBPUSD": "GBP/USD",
    "USDJPY": "USD/JPY",
    "AUDUSD": "AUD/USD",
    "USDCAD": "USD/CAD",
}

_NOT_IMPLEMENTED = (
    "TwelveData adapter is a stub. Remaining work: REST history pagination with rate-limit "
    "pacing, WebSocket streaming, and a decision on costs.spread_model since bid/ask "
    "candles are unavailable on standard plans. See this module's docstring."
)


class TwelveDataAdapter:
    name = "twelvedata"

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def to_vendor_symbol(self, instrument: str) -> str:
        return SYMBOL_MAP[instrument]

    def to_canonical(self, vendor_symbol: str) -> str:
        return vendor_symbol.replace("/", "")

    async def fetch_candles(
        self,
        instrument: str,
        granularity: Literal["M1"] = "M1",
        start_utc: datetime | None = None,
        end_utc: datetime | None = None,
    ) -> list[M1Candle]:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def stream_prices(
        self, instruments: Sequence[str]
    ) -> AsyncIterator[Quote | Heartbeat]:
        raise NotImplementedError(_NOT_IMPLEMENTED)
        yield  # type: ignore[unreachable]  # pragma: no cover - makes this a generator

    async def latest_quote(self, instrument: str) -> Quote:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def health(self) -> AdapterHealth:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def aclose(self) -> None:
        return None
