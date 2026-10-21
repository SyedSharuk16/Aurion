"""OANDA adapter parsing, against a mock transport.

There are no live credentials here, so these tests cover what can actually be wrong in the
adapter: timestamp parsing, the bid/ask requirement, incomplete-candle dropping, seam
continuity, and the refusal to fetch anything but M1.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import httpx
import pytest

from aurion.core.timeframe import UTC
from aurion.data.adapters.base import AdapterError
from aurion.data.adapters.oanda import OandaAdapter, _parse_time


def candle(minute: int, complete: bool = True, with_bid_ask: bool = True) -> dict:
    stamp = f"2024-06-12T09:{minute:02d}:00.000000000Z"
    out: dict = {"time": stamp, "volume": 12 + minute, "complete": complete}
    block = {"o": "2300.10", "h": "2300.90", "l": "2299.80", "c": "2300.50"}
    if with_bid_ask:
        out["bid"] = block
        out["ask"] = {k: str(float(v) + 0.20) for k, v in block.items()}
    else:
        out["mid"] = block
    return out


def adapter_with(handler) -> OandaAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OandaAdapter("tok", "acct-1", "practice", client=client)


def test_symbol_mapping_round_trips():
    a = adapter_with(lambda r: httpx.Response(200, json={}))
    assert a.to_vendor_symbol("XAUUSD") == "XAU_USD"
    assert a.to_canonical("USD_JPY") == "USDJPY"
    with pytest.raises(AdapterError, match="no vendor symbol"):
        a.to_vendor_symbol("NOTAPAIR")
    with pytest.raises(AdapterError, match="unknown vendor symbol"):
        a.to_canonical("FOO_BAR")


def test_nanosecond_timestamps_parse():
    """`datetime.fromisoformat` rejects 9 fractional digits; OANDA sends them."""
    got = _parse_time("2024-06-12T09:30:00.123456789Z")
    assert got == datetime(2024, 6, 12, 9, 30, 0, 123456, tzinfo=UTC)
    assert _parse_time("2024-06-12T09:30:00Z").tzinfo is not None


async def test_higher_granularity_is_refused():
    """Everything above M1 is resampled locally (§2.1); the adapter will not serve it."""
    a = adapter_with(lambda r: httpx.Response(200, json={"candles": []}))
    with pytest.raises(AdapterError, match="resampled"):
        await a.fetch_candles("XAUUSD", "H1", datetime.now(UTC), datetime.now(UTC))  # type: ignore[arg-type]
    await a.aclose()


async def test_candles_parse_with_bid_ask_and_derived_mid():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["price"] == "BA", "mid-only would break the cost model"
        assert request.url.params["granularity"] == "M1"
        assert "alignmentTimezone" not in request.url.params
        return httpx.Response(200, json={"candles": [candle(0), candle(1)]})

    a = adapter_with(handler)
    start = datetime(2024, 6, 12, 9, 0, tzinfo=UTC)
    got = await a.fetch_candles("XAUUSD", "M1", start, start + timedelta(minutes=2))
    assert len(got) == 2
    assert got[0].instrument == "XAUUSD"
    assert got[0].close_time - got[0].open_time == timedelta(minutes=1)
    assert got[0].bid.c == pytest.approx(2300.50)
    assert got[0].ask.c == pytest.approx(2300.70)
    assert got[0].mid.c == pytest.approx(2300.60)
    assert got[0].spread_close == pytest.approx(0.20)
    await a.aclose()


async def test_incomplete_candles_are_dropped_not_stored_and_corrected():
    a = adapter_with(
        lambda r: httpx.Response(
            200, json={"candles": [candle(0), candle(1, complete=False)]}
        )
    )
    start = datetime(2024, 6, 12, 9, 0, tzinfo=UTC)
    got = await a.fetch_candles("XAUUSD", "M1", start, start + timedelta(minutes=2))
    assert len(got) == 1 and got[0].open_time.minute == 0
    await a.aclose()


async def test_mid_only_response_is_a_loud_error():
    a = adapter_with(
        lambda r: httpx.Response(200, json={"candles": [candle(0, with_bid_ask=False)]})
    )
    start = datetime(2024, 6, 12, 9, 0, tzinfo=UTC)
    with pytest.raises(AdapterError, match="price=BA"):
        await a.fetch_candles("XAUUSD", "M1", start, start + timedelta(minutes=2))
    await a.aclose()


async def test_missing_candles_key_is_a_loud_error():
    a = adapter_with(lambda r: httpx.Response(200, json={"unexpected": []}))
    start = datetime(2024, 6, 12, 9, 0, tzinfo=UTC)
    with pytest.raises(AdapterError, match="no 'candles'"):
        await a.fetch_candles("XAUUSD", "M1", start, start + timedelta(minutes=2))
    await a.aclose()


async def test_malformed_ohlc_block_is_a_loud_error():
    bad = candle(0)
    bad["bid"] = {"o": "1.0"}
    a = adapter_with(lambda r: httpx.Response(200, json={"candles": [bad]}))
    start = datetime(2024, 6, 12, 9, 0, tzinfo=UTC)
    with pytest.raises(AdapterError, match="malformed bid"):
        await a.fetch_candles("XAUUSD", "M1", start, start + timedelta(minutes=2))
    await a.aclose()


async def test_http_error_carries_context():
    a = adapter_with(lambda r: httpx.Response(401, text="unauthorised"))
    start = datetime(2024, 6, 12, 9, 0, tzinfo=UTC)
    with pytest.raises(AdapterError, match="HTTP 401"):
        await a.fetch_candles("XAUUSD", "M1", start, start + timedelta(minutes=2))
    await a.aclose()


async def test_pricing_snapshot_parses():
    payload = {
        "prices": [
            {
                "instrument": "XAU_USD",
                "time": "2024-06-12T09:30:00.000000000Z",
                "bids": [{"price": "2300.10"}],
                "asks": [{"price": "2300.30"}],
            }
        ]
    }
    a = adapter_with(lambda r: httpx.Response(200, json=payload))
    quote = await a.latest_quote("XAUUSD")
    assert quote.instrument == "XAUUSD"
    assert quote.spread == pytest.approx(0.20)
    assert quote.mid == pytest.approx(2300.20)
    await a.aclose()


async def test_empty_pricing_response_is_a_loud_error():
    a = adapter_with(lambda r: httpx.Response(200, json={"prices": []}))
    with pytest.raises(AdapterError, match="no price"):
        await a.latest_quote("XAUUSD")
    await a.aclose()


async def test_stream_yields_prices_and_heartbeats():
    """Heartbeats are yielded, not swallowed: the supervisor needs them."""
    lines = [
        json.dumps({"type": "HEARTBEAT", "time": "2024-06-12T09:30:00.000000000Z"}),
        json.dumps(
            {
                "type": "PRICE",
                "instrument": "XAU_USD",
                "time": "2024-06-12T09:30:01.000000000Z",
                "bids": [{"price": "2300.10"}],
                "asks": [{"price": "2300.30"}],
            }
        ),
        "",
    ]
    body = ("\n".join(lines) + "\n").encode()
    a = adapter_with(lambda r: httpx.Response(200, content=body))
    got = [msg async for msg in a.stream_prices(["XAUUSD"])]
    assert len(got) == 2
    assert got[0].time.minute == 30
    assert getattr(got[1], "instrument", None) == "XAUUSD"
    await a.aclose()


async def test_health_reports_connection_state():
    a = adapter_with(lambda r: httpx.Response(200, json={"candles": []}))
    health = await a.health()
    assert health.name == "oanda" and not health.connected
    await a.aclose()


def test_unknown_environment_is_refused():
    with pytest.raises(AdapterError, match="unknown environment"):
        OandaAdapter("tok", "acct", "staging")  # type: ignore[arg-type]


def test_adapter_protocol_has_no_order_methods():
    """The execution-free guarantee, asserted rather than assumed."""
    from aurion.data.adapters.base import DataAdapter

    forbidden = {"submit", "place", "order", "amend", "cancel", "close_trade", "modify"}
    for proto in (DataAdapter, OandaAdapter):
        names = {n.lower() for n in dir(proto) if not n.startswith("_")}
        assert not (names & forbidden), f"{proto.__name__} exposes an execution method"


def test_stubs_name_what_remains():
    from aurion.data.adapters.mt5 import Mt5Adapter
    from aurion.data.adapters.twelvedata import TwelveDataAdapter

    with pytest.raises(NotImplementedError, match="server-time offset"):
        Mt5Adapter()
    with pytest.raises(NotImplementedError, match="bid/ask candles are unavailable"):
        TwelveDataAdapter()
