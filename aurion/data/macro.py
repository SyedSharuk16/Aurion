"""Daily macro conditioners. `docs/02-definitions.md` §10.3a, Q7 (corrected).

My Phase 1 position -- "no real-yield feed, so nothing" -- was a false binary and was
withdrawn. Real yields, nominal yields and breakevens are all free and daily from FRED.
What is unavailable free is an *intraday* series.

Point-in-time is not optional here. Treasury yields are published with a lag and are
occasionally revised, so a feature keyed on the observation date alone would let a Tuesday
signal read Tuesday's close before it existed. FRED exposes vintages through
``realtime_start``/``realtime_end``, which is what ``as_published_before`` maps onto.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

import httpx

from aurion.core.timeframe import UTC
from aurion.data.adapters.base import AdapterError

#: Canonical feature name -> FRED series id. Verified against FRED's published catalogue;
#: a wrong id fails loudly at fetch time rather than producing a plausible wrong series.
FRED_SERIES: dict[str, str] = {
    "US02Y_nominal_d": "DGS2",
    "US10Y_nominal_d": "DGS10",
    "US10Y_real_d": "DFII10",      # 10-year TIPS yield: an actual real yield, not a proxy
    "US10Y_breakeven_d": "T10YIE",
}


@dataclass(frozen=True, slots=True)
class MacroObs:
    series_id: str
    observation_date: date
    value: float
    published_at: datetime
    revision: int = 0

    @property
    def confirmed_at(self) -> datetime:
        """When this value could first have been read. Not when it describes."""
        return self.published_at


@runtime_checkable
class MacroProvider(Protocol):
    name: str
    supports_point_in_time: bool

    async def series(
        self,
        series_id: str,
        start: date,
        end: date,
        as_published_before: datetime | None = None,
    ) -> list[MacroObs]: ...


class FredProvider:
    """FRED/ALFRED daily series with vintage support."""

    name = "fred"
    supports_point_in_time = True
    BASE = "https://api.stlouisfed.org/fred/series/observations"

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None) -> None:
        self._key = api_key
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        self._owns_client = client is None

    async def series(
        self,
        series_id: str,
        start: date,
        end: date,
        as_published_before: datetime | None = None,
    ) -> list[MacroObs]:
        params: dict[str, str] = {
            "series_id": series_id,
            "api_key": self._key,
            "file_type": "json",
            "observation_start": start.isoformat(),
            "observation_end": end.isoformat(),
        }
        if as_published_before is not None:
            # Vintage as of that date: what the series looked like to someone reading it
            # then, including values later revised.
            vintage = (as_published_before.astimezone(UTC).date()).isoformat()
            params["realtime_start"] = vintage
            params["realtime_end"] = vintage
        try:
            response = await self._client.get(self.BASE, params=params)
        except httpx.HTTPError as exc:
            raise AdapterError("fred", "http", f"{series_id}: {exc}") from exc
        if response.status_code != 200:
            raise AdapterError(
                "fred", "http", f"{series_id}: HTTP {response.status_code} {response.text[:160]}"
            )
        return self._parse(series_id, response.json())

    @staticmethod
    def _parse(series_id: str, payload: dict[str, Any]) -> list[MacroObs]:
        raw = payload.get("observations")
        if raw is None:
            raise AdapterError("fred", "parse", f"no 'observations' in {list(payload)}")
        out: list[MacroObs] = []
        for item in raw:
            value = item.get("value", ".")
            if value == ".":
                continue  # FRED's missing marker: holidays and non-trading days
            obs_date = date.fromisoformat(item["date"])
            # realtime_start is when this vintage became available. Absent it, the honest
            # fallback is the day AFTER the observation: a daily yield close is not
            # readable during the day it describes.
            rt = item.get("realtime_start")
            published = (
                datetime.fromisoformat(rt).replace(tzinfo=UTC)
                if rt
                else datetime.combine(obs_date + timedelta(days=1), datetime.min.time(), UTC)
            )
            out.append(
                MacroObs(
                    series_id=series_id,
                    observation_date=obs_date,
                    value=float(value),
                    published_at=published,
                )
            )
        return sorted(out, key=lambda o: o.observation_date)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class StaticMacroProvider:
    """In-memory provider for tests and frozen vintages."""

    name = "static"
    supports_point_in_time = True

    def __init__(self, observations: list[MacroObs]) -> None:
        self._obs = sorted(observations, key=lambda o: (o.series_id, o.observation_date))

    async def series(
        self,
        series_id: str,
        start: date,
        end: date,
        as_published_before: datetime | None = None,
    ) -> list[MacroObs]:
        out = [
            o
            for o in self._obs
            if o.series_id == series_id and start <= o.observation_date <= end
        ]
        if as_published_before is not None:
            out = [o for o in out if o.published_at <= as_published_before]
        return out


class MacroCache:
    """As-of-filtered read side for the feature builder.

    A daily series is a **step function**: within a day it holds the last value published
    before ``as_of`` and does not interpolate toward the next one. Interpolating would
    invent an intraday macro series, which §10.3a explicitly forbids.
    """

    __slots__ = ("_by_series",)

    def __init__(self) -> None:
        self._by_series: dict[str, list[MacroObs]] = {}

    def add(self, obs: MacroObs) -> None:
        series = self._by_series.setdefault(obs.series_id, [])
        series.append(obs)
        series.sort(key=lambda o: (o.published_at, o.observation_date))

    def extend(self, observations: list[MacroObs]) -> None:
        for o in observations:
            self.add(o)

    def latest(self, series_id: str, as_of: datetime) -> MacroObs | None:
        visible = [o for o in self._by_series.get(series_id, ()) if o.published_at <= as_of]
        return visible[-1] if visible else None

    def value(self, series_id: str, as_of: datetime) -> float | None:
        got = self.latest(series_id, as_of)
        return got.value if got is not None else None

    def change(self, series_id: str, as_of: datetime, lookback_days: int) -> float | None:
        """Change over ``lookback_days`` observation days, using visible vintages only."""
        visible = [o for o in self._by_series.get(series_id, ()) if o.published_at <= as_of]
        if len(visible) < 2:
            return None
        current = visible[-1]
        cutoff = current.observation_date - timedelta(days=lookback_days)
        earlier = [o for o in visible if o.observation_date <= cutoff]
        if not earlier:
            return None
        return current.value - earlier[-1].value

    def series_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_series))
