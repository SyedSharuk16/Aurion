"""Volatility estimators and regime labels. §10.1.

Three practical points that are usually omitted and cause silent bugs:

1. **GK and RS summands can be negative for a single bar.** The sum is clamped at zero
   *after* aggregation, never per bar, or the estimator is biased upward.
2. **Parkinson and GK assume no overnight gap.** They are computed on intraday timeframes
   only and never on D1/W1 bars whose open gaps from the prior close.
3. **Realised vol is sampling-frequency dependent.** The feature name always carries the
   timeframe, and M1 realised vol is not used at all: bid-ask bounce dominates it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from aurion.analysis.primitives import atr_series, log_returns, percentile_rank
from aurion.core.timeframe import TimeFrame
from aurion.data.types import Bar

_LN2 = math.log(2.0)
#: Estimators that assume continuous trading are refused above this timeframe (point 2).
_GAP_SENSITIVE_MAX = TimeFrame.H4
_INTRADAY_ORDER = (TimeFrame.M1, TimeFrame.M5, TimeFrame.M15, TimeFrame.H1, TimeFrame.H4)


class VolRegime(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


def _gap_sensitive_allowed(timeframe: TimeFrame) -> bool:
    """Parkinson/GK/RS assume continuous trading, so D1 and W1 are refused (point 2)."""
    if timeframe not in _INTRADAY_ORDER:
        return False
    return _INTRADAY_ORDER.index(timeframe) <= _INTRADAY_ORDER.index(_GAP_SENSITIVE_MAX)


def realised_vol(bars: Sequence[Bar], window: int) -> float | None:
    """Root sum of squared close-to-close log returns over the window.

    Not annualised here: annualisation needs bars-per-year, which differs per timeframe and
    per instrument calendar, and burying that conversion in the estimator is how two
    "realised vol" numbers end up incomparable.
    """
    if len(bars) < window + 1:
        return None
    rets = log_returns(bars[-(window + 1) :])
    return math.sqrt(sum(r * r for r in rets))


def parkinson(bars: Sequence[Bar], window: int) -> float | None:
    """``sqrt( 1/(4 n ln2) * sum (ln(H/L))^2 )``. Intraday only."""
    if len(bars) < window or not _gap_sensitive_allowed(bars[-1].timeframe):
        return None
    sample = bars[-window:]
    acc = 0.0
    for b in sample:
        if b.ohlc.l <= 0:
            return None
        acc += math.log(b.ohlc.h / b.ohlc.l) ** 2
    return math.sqrt(acc / (4.0 * window * _LN2))


def garman_klass(bars: Sequence[Bar], window: int) -> float | None:
    """Garman-Klass. Clamped at zero AFTER aggregation, never per bar. Intraday only."""
    if len(bars) < window or not _gap_sensitive_allowed(bars[-1].timeframe):
        return None
    sample = bars[-window:]
    acc = 0.0
    for b in sample:
        if b.ohlc.l <= 0 or b.ohlc.o <= 0:
            return None
        hl = math.log(b.ohlc.h / b.ohlc.l)
        co = math.log(b.ohlc.c / b.ohlc.o)
        acc += 0.5 * hl * hl - (2.0 * _LN2 - 1.0) * co * co
    return math.sqrt(max(acc / window, 0.0))


def rogers_satchell(bars: Sequence[Bar], window: int) -> float | None:
    """Rogers-Satchell, which tolerates drift. Clamped after aggregation. Intraday only."""
    if len(bars) < window or not _gap_sensitive_allowed(bars[-1].timeframe):
        return None
    sample = bars[-window:]
    acc = 0.0
    for b in sample:
        o, h, low, c = b.ohlc.o, b.ohlc.h, b.ohlc.l, b.ohlc.c
        if min(o, h, low, c) <= 0:
            return None
        acc += math.log(h / c) * math.log(h / o) + math.log(low / c) * math.log(low / o)
    return math.sqrt(max(acc / window, 0.0))


@dataclass(frozen=True, slots=True)
class VolatilityState:
    atr: float | None
    atr_percentile: float | None
    realised_vol: float | None
    parkinson: float | None
    garman_klass: float | None
    rogers_satchell: float | None
    regime: VolRegime | None

    def as_dict(self, timeframe: TimeFrame) -> dict[str, float]:
        tf = str(timeframe).lower()
        out: dict[str, float] = {}
        for name, value in (
            ("atr", self.atr),
            ("atr_pct", self.atr_percentile),
            (f"rv_{tf}", self.realised_vol),
            (f"parkinson_{tf}", self.parkinson),
            (f"gk_{tf}", self.garman_klass),
            (f"rs_{tf}", self.rogers_satchell),
        ):
            if value is not None:
                out[name] = value
        return out


def volatility_state(
    bars: Sequence[Bar],
    atr_period: int = 14,
    percentile_window: int = 500,
    rv_window: int = 20,
    low_cut: float = 0.33,
    high_cut: float = 0.67,
) -> VolatilityState:
    """Everything at once. Each component is ``None`` before its own warm-up."""
    atrs = atr_series(bars, atr_period)
    current = atrs[-1] if atrs else None
    history = [a for a in atrs if a is not None]
    pct = percentile_rank(history, percentile_window) if history else None
    regime = (
        None
        if pct is None
        else VolRegime.LOW
        if pct < low_cut
        else VolRegime.HIGH
        if pct >= high_cut
        else VolRegime.NORMAL
    )
    return VolatilityState(
        atr=current,
        atr_percentile=pct,
        realised_vol=realised_vol(bars, rv_window),
        parkinson=parkinson(bars, rv_window),
        garman_klass=garman_klass(bars, rv_window),
        rogers_satchell=rogers_satchell(bars, rv_window),
        regime=regime,
    )
