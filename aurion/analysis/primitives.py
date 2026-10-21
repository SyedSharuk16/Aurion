"""Primitive indicators. `docs/02-definitions.md` §0.4, §0.6.

Two rules hold throughout `analysis/`:

**No silent defaults.** Anything with a warm-up requirement returns ``None`` until it has
the required closed bars -- never 0, never a partial value, never forward-filled. A module
receiving ``None`` for a required input returns ``None`` itself. This makes warm-up visible
in the backtest rather than quietly biasing the early results.

**Mid prices, closed bars, usable bars.** Callers pass the output of
``MarketView.usable_bars``, so thin bars are already excluded -- including from ATR, per
§2.2 rule 2.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from aurion.data.types import OHLC, Bar


def true_range(current: OHLC, prev_close: float) -> float:
    """``TR_t = max(H-L, |H-C_{t-1}|, |L-C_{t-1}|)`` (Wilder)."""
    return max(
        current.h - current.l,
        abs(current.h - prev_close),
        abs(current.l - prev_close),
    )


def true_range_series(bars: Sequence[Bar]) -> list[float]:
    """TR for bars[1:]. The first bar has no previous close, so TR is undefined there."""
    return [
        true_range(bars[i].ohlc, bars[i - 1].ohlc.c) for i in range(1, len(bars))
    ]


def atr_series(bars: Sequence[Bar], period: int = 14) -> list[float | None]:
    """Wilder's ATR, aligned to ``bars``.

    ``ATR_n = mean(TR_1..TR_n)`` seeds the recursion at bar index ``period``; thereafter
    ``ATR_t = ATR_{t-1} + (TR_t - ATR_{t-1}) / period``.

    Warm-up needs ``period + 1`` bars because TR needs a previous close. Entries before
    that are ``None``, not zero: a zero ATR would make every ATR-scaled threshold trivially
    satisfiable, which is the failure mode this rule exists to prevent.
    """
    if period < 2:
        raise ValueError("ATR period must be >= 2")
    out: list[float | None] = [None] * len(bars)
    if len(bars) < period + 1:
        return out
    trs = true_range_series(bars)  # index i corresponds to bars[i+1]
    seed = sum(trs[:period]) / period
    out[period] = seed
    prev = seed
    for i in range(period, len(trs)):
        prev = prev + (trs[i] - prev) / period
        out[i + 1] = prev
    return out


def atr(bars: Sequence[Bar], period: int = 14) -> float | None:
    """ATR at the most recent bar, or ``None`` before warm-up."""
    series = atr_series(bars, period)
    return series[-1] if series else None


def percentile_rank(values: Sequence[float], window: int) -> float | None:
    """``#{i in window : v_i < v_t} / window`` over the trailing window, inclusive of t.

    Strictly backward-looking (§0.6). Returns ``None`` with fewer than ``window``
    observations rather than ranking against a short sample.
    """
    if window < 1:
        raise ValueError("window must be >= 1")
    if len(values) < window:
        return None
    tail = values[-window:]
    current = tail[-1]
    return sum(1 for v in tail if v < current) / window


def log_returns(bars: Sequence[Bar]) -> list[float]:
    """Close-to-close log returns for bars[1:]."""
    import math

    return [math.log(bars[i].ohlc.c / bars[i - 1].ohlc.c) for i in range(1, len(bars))]


def prices_equal(a: float, b: float, point: float) -> bool:
    """Price equality within half a point (§0.7). No float ``==`` on prices anywhere."""
    return abs(a - b) <= 0.5 * point


def round_to_point(price: float, point: float) -> float:
    """Round to the instrument's point so replay is bit-identical (§0.7)."""
    return round(price / point) * point


@dataclass(frozen=True, slots=True)
class Imbalance:
    """A three-candle gap: the primitive behind both FVG objects and displacement.

    Lives here rather than in `ict` so that `structure` can use it without importing
    `ict`, which would make the module graph a cycle (see `analysis/base.py`).

    Bullish: ``low(i+1) > high(i-1)``, leaving the interval ``[high(i-1), low(i+1)]``
    untraded by the three-bar sequence. Bearish is the mirror. Wick extremes are used
    rather than bodies, per `docs/02-definitions.md` §5.1.
    """

    lo: float
    hi: float
    bullish: bool

    @property
    def size(self) -> float:
        return self.hi - self.lo

    @property
    def consequent_encroachment(self) -> float:
        """Midpoint. ICT's 'CE'; the 50% level of the gap."""
        return (self.hi + self.lo) / 2.0


def three_candle_imbalance(prev_bar: Bar, next_bar: Bar) -> Imbalance | None:
    """Gap between the bar BEFORE and the bar AFTER a middle bar, or None.

    The middle bar is not an input: its own range is irrelevant to whether the outer two
    leave an untraded interval, which is the whole content of the definition.
    """
    if next_bar.ohlc.l > prev_bar.ohlc.h:
        return Imbalance(lo=prev_bar.ohlc.h, hi=next_bar.ohlc.l, bullish=True)
    if next_bar.ohlc.h < prev_bar.ohlc.l:
        return Imbalance(lo=next_bar.ohlc.h, hi=prev_bar.ohlc.l, bullish=False)
    return None
