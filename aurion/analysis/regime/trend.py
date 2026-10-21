"""Trend versus mean-reversion. §10.2.

Honest statements kept with the code rather than only in the docs:

* **ADX warm-up is ``2n`` bars, not ``n``.** One ``n`` for the DI smoothing and another for
  the ADX smoothing. "ADX from 14 bars" is wrong and makes early backtest bars garbage.
* **Rolling Hurst on 512 observations has a standard error of roughly 0.05-0.10** and a
  known small-sample bias toward 0.5-0.6 even for a pure random walk. The difference
  between H = 0.55 and H = 0.48 on this window is not meaningful. It is implemented because
  it was asked for, and because showing it contributes nothing is itself a result -- but it
  will not be presented as a trend detector.
* **The variance-ratio statistic is the heteroskedasticity-robust one** (Lo-MacKinlay 1988).
  FX returns are strongly heteroskedastic, and the homoskedastic version rejects the random
  walk far too often, which would make this feature look informative when it is not.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from aurion.analysis.primitives import log_returns, true_range_series
from aurion.data.types import Bar


class TrendRegime(StrEnum):
    TRENDING = "trending"
    MIXED = "mixed"
    MEAN_REVERTING = "mean_reverting"


def _rma(values: Sequence[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = prev + (values[i] - prev) / period
        out[i] = prev
    return out


@dataclass(frozen=True, slots=True)
class AdxState:
    adx: float | None
    plus_di: float | None
    minus_di: float | None


def adx(bars: Sequence[Bar], period: int = 14) -> AdxState:
    """Wilder's ADX. Returns ``None`` until ``2 * period`` bars are available."""
    if len(bars) < 2 * period + 1:
        return AdxState(None, None, None)
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, len(bars)):
        up = bars[i].ohlc.h - bars[i - 1].ohlc.h
        down = bars[i - 1].ohlc.l - bars[i].ohlc.l
        plus_dm.append(up if up > max(down, 0.0) else 0.0)
        minus_dm.append(down if down > max(up, 0.0) else 0.0)
    trs = true_range_series(bars)

    smooth_plus = _rma(plus_dm, period)
    smooth_minus = _rma(minus_dm, period)
    smooth_tr = _rma(trs, period)

    dx: list[float] = []
    plus_di = minus_di = None
    for sp, sm, st in zip(smooth_plus, smooth_minus, smooth_tr, strict=True):
        if sp is None or sm is None or st is None or st <= 0:
            continue
        pdi = 100.0 * sp / st
        mdi = 100.0 * sm / st
        plus_di, minus_di = pdi, mdi
        total = pdi + mdi
        dx.append(100.0 * abs(pdi - mdi) / total if total > 0 else 0.0)

    smoothed = _rma(dx, period)
    value = next((v for v in reversed(smoothed) if v is not None), None)
    return AdxState(value, plus_di, minus_di)


def hurst_rs(returns: Sequence[float], min_block: int = 16) -> float | None:
    """Rescaled-range Hurst exponent. See the module docstring on its standard error."""
    n = len(returns)
    if n < 4 * min_block:
        return None
    sizes: list[int] = []
    size = n // 2
    while size >= min_block and len(sizes) < 4:
        sizes.append(size)
        size //= 2
    if len(sizes) < 2:
        return None

    xs: list[float] = []
    ys: list[float] = []
    arr = np.asarray(returns, dtype=float)
    for block in sizes:
        count = n // block
        ratios: list[float] = []
        for k in range(count):
            chunk = arr[k * block : (k + 1) * block]
            mean = chunk.mean()
            deviations = np.cumsum(chunk - mean)
            spread = float(deviations.max() - deviations.min())
            sd = float(chunk.std(ddof=1))
            if sd > 0 and spread > 0:
                ratios.append(spread / sd)
        if ratios:
            xs.append(math.log(block))
            ys.append(math.log(sum(ratios) / len(ratios)))
    if len(xs) < 2:
        return None
    slope, _ = np.polyfit(np.array(xs), np.array(ys), 1)
    return float(slope)


@dataclass(frozen=True, slots=True)
class VarianceRatio:
    q: int
    vr: float
    z_star: float

    @property
    def trending(self) -> bool:
        return self.vr > 1.0


def variance_ratio(returns: Sequence[float], q: int) -> VarianceRatio | None:
    """Lo-MacKinlay variance ratio with the heteroskedasticity-consistent statistic.

        VR(q) = sigma^2(q) / sigma^2(1),  z*(q) = sqrt(nq)(VR-1)/sqrt(theta(q))

    ``VR > 1`` indicates positive autocorrelation (trending); ``VR < 1`` mean reversion.
    """
    nq = len(returns)
    if q < 2 or nq < 4 * q:
        return None
    r = np.asarray(returns, dtype=float)
    scale = float(np.abs(r).mean())
    if scale == 0.0 or float(r.std()) <= 1e-12 * max(scale, 1.0):
        # Effectively constant returns. The variance ratio is undefined here, and the
        # `var1 <= 0` test below does not catch it: mu carries floating-point residue, so
        # var1 lands near 1e-36 and the statistic returns a spurious |z| in the hundreds.
        return None
    x = np.concatenate([[0.0], np.cumsum(r)])
    mu = (x[-1] - x[0]) / nq

    diffs = np.diff(x) - mu
    var1 = float((diffs**2).sum() / (nq - 1))
    if var1 <= 0:
        return None

    m = q * (nq - q + 1) * (1.0 - q / nq)
    if m <= 0:
        return None
    q_diffs = x[q:] - x[:-q] - q * mu
    varq = float((q_diffs**2).sum() / m)
    vr = varq / var1

    denom = float((diffs**2).sum()) ** 2
    if denom <= 0:
        return None
    theta = 0.0
    for j in range(1, q):
        prod = (diffs[j:] ** 2) * (diffs[:-j] ** 2)
        delta = float(prod.sum()) / denom
        theta += ((2.0 * (q - j) / q) ** 2) * delta
    if theta <= 0:
        return None
    z = math.sqrt(nq) * (vr - 1.0) / math.sqrt(theta)
    return VarianceRatio(q=q, vr=vr, z_star=z)


@dataclass(frozen=True, slots=True)
class TrendState:
    adx: AdxState
    hurst: float | None
    variance_ratios: dict[int, VarianceRatio]
    regime: TrendRegime | None

    def as_dict(self) -> dict[str, float]:
        out: dict[str, float] = {}
        if self.adx.adx is not None:
            out["adx"] = self.adx.adx
        if self.adx.plus_di is not None:
            out["plus_di"] = self.adx.plus_di
        if self.adx.minus_di is not None:
            out["minus_di"] = self.adx.minus_di
        if self.hurst is not None:
            out["hurst"] = self.hurst
        for q, vr in self.variance_ratios.items():
            out[f"vr_{q}"] = vr.vr
            out[f"vr_{q}_z"] = vr.z_star
        return out


def trend_regime_state(
    bars: Sequence[Bar],
    adx_period: int = 14,
    adx_hi: float = 25.0,
    adx_lo: float = 20.0,
    hurst_window: int = 512,
    vr_qs: Sequence[int] = (2, 4, 8),
    vr_window: int = 500,
) -> TrendState:
    adx_state = adx(bars, adx_period)
    rets = log_returns(bars)
    hurst = hurst_rs(rets[-hurst_window:]) if len(rets) >= hurst_window else None
    vrs: dict[int, VarianceRatio] = {}
    sample = rets[-vr_window:]
    for q in vr_qs:
        got = variance_ratio(sample, q)
        if got is not None:
            vrs[q] = got

    regime: TrendRegime | None = None
    anchor = vrs.get(4)
    if adx_state.adx is not None and anchor is not None:
        if adx_state.adx >= adx_hi and anchor.z_star > 1.96:
            regime = TrendRegime.TRENDING
        elif adx_state.adx <= adx_lo and anchor.z_star < -1.96:
            regime = TrendRegime.MEAN_REVERTING
        else:
            regime = TrendRegime.MIXED
    return TrendState(adx=adx_state, hurst=hurst, variance_ratios=vrs, regime=regime)
