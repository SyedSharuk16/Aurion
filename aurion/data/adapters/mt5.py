"""MetaTrader 5 adapter stub.

Not implemented. The symbol map and the divergence list below are the point of the stub:
they record why swapping adapters is not free, which is the thing a future reader needs.

Known divergences from OANDA, each of which changes results rather than just plumbing:

* **Broker-dependent server time.** MT5 bar timestamps are in the broker's server
  timezone, which is neither UTC nor fixed, and brokers change it. Every timestamp needs a
  per-broker offset that must be discovered, not assumed.
* **Broker-dependent bar alignment.** The broker's own daily rollover decides where D1 and
  H4 bars fall, so the structural object graph differs between brokers on identical price
  action. This is the reason `docs/02-definitions.md` §2.1 resamples from M1 locally.
* **No native async, Windows-only terminal.** The MT5 Python package is synchronous and
  requires a running terminal, so it needs a thread executor and cannot be hosted on the
  target Linux VPS without Wine or a separate Windows host.
* **Tick volume is differently constructed**, so every `volume_proxy` feature
  (`docs/02-definitions.md` §0.5) shifts distribution on an adapter swap.
* **No bid/ask candles.** MT5 provides bid-based bars plus a spread column in points, so
  ask must be reconstructed as bid + spread, which is an approximation the OANDA path does
  not need.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Literal

from aurion.data.adapters.base import AdapterHealth
from aurion.data.types import Heartbeat, M1Candle, Quote

SYMBOL_MAP = {
    "XAUUSD": "XAUUSD",
    "EURUSD": "EURUSD",
    "GBPUSD": "GBPUSD",
    "USDJPY": "USDJPY",
    "AUDUSD": "AUDUSD",
    "USDCAD": "USDCAD",
}

_NOT_IMPLEMENTED = (
    "MT5 adapter is a stub. Remaining work: per-broker server-time offset discovery, "
    "ask reconstruction from the spread column, a thread executor for the synchronous "
    "MT5 API, and a Windows host or Wine layer. See this module's docstring."
)


class Mt5Adapter:
    name = "mt5"

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def to_vendor_symbol(self, instrument: str) -> str:
        return SYMBOL_MAP[instrument]

    def to_canonical(self, vendor_symbol: str) -> str:
        return vendor_symbol

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
        yield  # type: ignore[unreachable]  # pragma: no cover - makes this a generator - makes this an async generator for the protocol

    async def latest_quote(self, instrument: str) -> Quote:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def health(self) -> AdapterHealth:
        raise NotImplementedError(_NOT_IMPLEMENTED)

    async def aclose(self) -> None:
        return None
