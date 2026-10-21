"""Typed events on the in-process bus. `docs/01-architecture.md` §5."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from aurion.data.types import Bar, M1Candle, Quote


@dataclass(frozen=True, slots=True)
class M1Ingested:
    candle: M1Candle


@dataclass(frozen=True, slots=True)
class BarClosed:
    """The only event analysis reacts to by default (`runtime.evaluate_on_forming_bar`)."""

    bar: Bar

    @property
    def as_of(self) -> datetime:
        return self.bar.close_time


@dataclass(frozen=True, slots=True)
class QuoteUpdate:
    quote: Quote


class AdapterState(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    RECONNECTING = "reconnecting"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class AdapterStateChanged:
    adapter: str
    state: AdapterState
    at: datetime
    detail: str = ""


@dataclass(frozen=True, slots=True)
class FeedStale:
    instrument: str
    last_bar_close: datetime
    age_seconds: float


@dataclass(frozen=True, slots=True)
class GapDetected:
    """A hole in the M1 series. Always repaired by REST backfill, never interpolated."""

    instrument: str
    gap_start: datetime
    gap_end: datetime
    missing_minutes: int
