"""OANDA v20 adapter. Read-only. `docs/05-data-contracts.md` §1.1.

Endpoints used, and nothing else:

* ``GET /v3/instruments/{instrument}/candles`` -- historical M1, ``price=BA``
* ``GET /v3/accounts/{id}/pricing``            -- spot snapshot
* ``GET /v3/accounts/{id}/pricing/stream``     -- live prices

Implementation commitments, each preventing a specific defect:

* **``price=BA``, never ``M``.** Mid-only candles make the spread unknowable and force
  ``spread_model: fixed``, which degrades the cost model (`docs/04` §2.1).
* **Candles with ``complete: false`` are discarded on ingest.** Storing one and updating it
  later is the repaint the design forbids.
* **``dailyAlignment`` / ``alignmentTimezone`` / ``weeklyAlignment`` are not used.** They
  are the vendor-resampling feature this design rejects. M1 has no alignment ambiguity, so
  passing them would be a no-op that invites later misuse.
* **Backfill asserts seam continuity.** Pages overlap by one bar and the overlap is checked
  rather than assumed.

The per-request candle cap is documented as 5000 at the time of writing. It is a named
constant with a verification note rather than an inline literal, because trusting a
remembered API limit is how a backfill silently truncates.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta
from typing import Any, Literal

import httpx

from aurion.core.timeframe import UTC
from aurion.data.adapters.base import AdapterError, AdapterHealth
from aurion.data.types import OHLC, Heartbeat, M1Candle, Quote

#: Documented as 5000 per request. VERIFY against the current OANDA docs before relying on
#: a full-history backfill; a wrong value here truncates silently rather than erroring.
MAX_CANDLES_PER_REQUEST = 5000

_HOSTS = {
    "practice": ("https://api-fxpractice.oanda.com", "https://stream-fxpractice.oanda.com"),
    "live": ("https://api-fxtrade.oanda.com", "https://stream-fxtrade.oanda.com"),
}

#: Canonical -> vendor. Mapping lives in the adapter and nowhere else.
_SYMBOLS = {
    "XAUUSD": "XAU_USD",
    "XAGUSD": "XAG_USD",
    "EURUSD": "EUR_USD",
    "GBPUSD": "GBP_USD",
    "USDJPY": "USD_JPY",
    "AUDUSD": "AUD_USD",
    "USDCAD": "USD_CAD",
    "USDCHF": "USD_CHF",
    "NZDUSD": "NZD_USD",
}
_REVERSE = {v: k for k, v in _SYMBOLS.items()}


def _parse_time(raw: str) -> datetime:
    """OANDA RFC3339 with nanosecond precision, which `fromisoformat` rejects."""
    txt = raw.replace("Z", "+00:00")
    if "." in txt:
        head, rest = txt.split(".", 1)
        frac, _, tz = rest.partition("+")
        txt = f"{head}.{frac[:6]}+{tz}" if tz else f"{head}.{frac[:6]}"
    out = datetime.fromisoformat(txt)
    return out if out.tzinfo else out.replace(tzinfo=UTC)


def _ohlc(block: dict[str, Any], where: str) -> OHLC:
    try:
        return OHLC(
            o=float(block["o"]), h=float(block["h"]), l=float(block["l"]), c=float(block["c"])
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AdapterError("oanda", "parse_candle", f"malformed {where} block: {block!r}") from exc


class OandaAdapter:
    """Read-only OANDA v20 client."""

    name = "oanda"

    def __init__(
        self,
        api_token: str,
        account_id: str,
        environment: Literal["practice", "live"] = "practice",
        client: httpx.AsyncClient | None = None,
        stream_idle_timeout_s: float = 15.0,
    ) -> None:
        if environment not in _HOSTS:
            raise AdapterError("oanda", "init", f"unknown environment {environment!r}")
        self._rest_base, self._stream_base = _HOSTS[environment]
        self._account_id = account_id
        self._token = api_token
        self._idle_timeout = stream_idle_timeout_s
        self._last_message_at: datetime | None = None
        self._connected = False
        self._client = client or httpx.AsyncClient(
            headers=self._headers(), timeout=httpx.Timeout(30.0, connect=10.0)
        )
        self._owns_client = client is None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept-Datetime-Format": "RFC3339",
            "Content-Type": "application/json",
        }

    # -- symbols -----------------------------------------------------------
    def to_vendor_symbol(self, instrument: str) -> str:
        try:
            return _SYMBOLS[instrument]
        except KeyError as exc:
            raise AdapterError("oanda", "symbol", f"no vendor symbol for {instrument!r}") from exc

    def to_canonical(self, vendor_symbol: str) -> str:
        try:
            return _REVERSE[vendor_symbol]
        except KeyError as exc:
            raise AdapterError(
                "oanda", "symbol", f"unknown vendor symbol {vendor_symbol!r}"
            ) from exc

    # -- candles -----------------------------------------------------------
    async def fetch_candles(
        self,
        instrument: str,
        granularity: Literal["M1"] = "M1",
        start_utc: datetime | None = None,
        end_utc: datetime | None = None,
    ) -> list[M1Candle]:
        if granularity != "M1":
            raise AdapterError(
                "oanda",
                "fetch_candles",
                f"granularity {granularity!r} refused: all higher timeframes are resampled "
                "locally (docs/02-definitions.md §2.1)",
            )
        if start_utc is None or end_utc is None:
            raise AdapterError("oanda", "fetch_candles", "start_utc and end_utc are required")

        vendor = self.to_vendor_symbol(instrument)
        out: list[M1Candle] = []
        cursor = start_utc
        page = 0
        while cursor < end_utc:
            window_end = min(cursor + timedelta(minutes=MAX_CANDLES_PER_REQUEST), end_utc)
            params = {
                "price": "BA",
                "granularity": "M1",
                "from": cursor.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                "to": window_end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            }
            payload = await self._get(f"/v3/instruments/{vendor}/candles", params)
            candles = self._parse_candles(instrument, payload)
            if out and candles:
                # Seam continuity: pages overlap by one bar by construction, so the first
                # candle of a later page must not predate the last of the previous one.
                if candles[0].open_time < out[-1].open_time:
                    raise AdapterError(
                        "oanda",
                        "fetch_candles",
                        f"page {page} regressed: {candles[0].open_time} < {out[-1].open_time}",
                    )
                candles = [c for c in candles if c.open_time > out[-1].open_time]
            out.extend(candles)
            page += 1
            if not candles and window_end >= end_utc:
                break
            cursor = window_end
        return out

    def _parse_candles(self, instrument: str, payload: dict[str, Any]) -> list[M1Candle]:
        raw = payload.get("candles")
        if raw is None:
            raise AdapterError("oanda", "parse", f"response has no 'candles': {list(payload)}")
        out: list[M1Candle] = []
        for item in raw:
            if not item.get("complete", False):
                continue  # never stored and later corrected
            if "bid" not in item or "ask" not in item:
                raise AdapterError(
                    "oanda",
                    "parse",
                    "candle lacks bid/ask; the request must use price=BA, not price=M",
                )
            open_time = _parse_time(item["time"])
            out.append(
                M1Candle(
                    instrument=instrument,
                    open_time=open_time,
                    close_time=open_time + timedelta(minutes=1),
                    bid=_ohlc(item["bid"], "bid"),
                    ask=_ohlc(item["ask"], "ask"),
                    tick_volume=int(item.get("volume", 0)),
                )
            )
        return out

    # -- pricing -----------------------------------------------------------
    async def latest_quote(self, instrument: str) -> Quote:
        vendor = self.to_vendor_symbol(instrument)
        payload = await self._get(
            f"/v3/accounts/{self._account_id}/pricing", {"instruments": vendor}
        )
        prices = payload.get("prices") or []
        if not prices:
            raise AdapterError("oanda", "latest_quote", f"no price returned for {instrument}")
        return self._parse_price(prices[0])

    def _parse_price(self, item: dict[str, Any]) -> Quote:
        try:
            bid = float(item["bids"][0]["price"])
            ask = float(item["asks"][0]["price"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise AdapterError("oanda", "parse_price", f"malformed price: {item!r}") from exc
        return Quote(
            instrument=self.to_canonical(item["instrument"]),
            time=_parse_time(item["time"]),
            bid=bid,
            ask=ask,
        )

    async def stream_prices(
        self, instruments: Sequence[str]
    ) -> AsyncIterator[Quote | Heartbeat]:
        """One stream carries all instruments.

        Heartbeats are yielded, not swallowed: the supervisor uses them to detect a silent
        connection. Idle longer than ``stream_idle_timeout_s`` raises so the caller tears
        down and reconnects rather than waiting on a dead socket. On reconnect the caller
        always REST-backfills from the last stored bar; continuity is never assumed.
        """
        vendor = ",".join(self.to_vendor_symbol(i) for i in instruments)
        url = f"{self._stream_base}/v3/accounts/{self._account_id}/pricing/stream"
        try:
            async with self._client.stream(
                "GET", url, params={"instruments": vendor}, timeout=None
            ) as response:
                if response.status_code != 200:
                    raise AdapterError("oanda", "stream", f"HTTP {response.status_code}")
                self._connected = True
                lines = response.aiter_lines()
                while True:
                    try:
                        line = await asyncio.wait_for(
                            lines.__anext__(), timeout=self._idle_timeout
                        )
                    except StopAsyncIteration:
                        break
                    except TimeoutError as exc:
                        raise AdapterError(
                            "oanda", "stream", f"idle > {self._idle_timeout}s"
                        ) from exc
                    if not line.strip():
                        continue
                    msg = json.loads(line)
                    self._last_message_at = datetime.now(tz=UTC)
                    kind = msg.get("type")
                    if kind == "HEARTBEAT":
                        yield Heartbeat(time=_parse_time(msg["time"]))
                    elif kind == "PRICE":
                        yield self._parse_price(msg)
        finally:
            self._connected = False

    # -- plumbing ----------------------------------------------------------
    async def _get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        try:
            response = await self._client.get(f"{self._rest_base}{path}", params=params)
        except httpx.HTTPError as exc:
            raise AdapterError("oanda", "http", f"{path}: {exc}") from exc
        if response.status_code != 200:
            raise AdapterError(
                "oanda", "http", f"{path}: HTTP {response.status_code} {response.text[:200]}"
            )
        self._last_message_at = datetime.now(tz=UTC)
        result: dict[str, Any] = response.json()
        return result

    async def health(self) -> AdapterHealth:
        return AdapterHealth(
            name=self.name,
            connected=self._connected,
            last_message_at=self._last_message_at,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
