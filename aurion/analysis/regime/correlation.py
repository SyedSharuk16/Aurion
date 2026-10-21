"""Correlation, the USD index proxy, VWAP, and exposure netting. §1.2, §10.3-§10.5."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from aurion.analysis.primitives import log_returns
from aurion.data.types import Bar


def ewma_correlation(x: Sequence[float], y: Sequence[float], halflife: float) -> float | None:
    """EWMA correlation of two return series. Recent observations weigh more."""
    n = min(len(x), len(y))
    if n < 8 or halflife <= 0:
        return None
    lam = 2.0 ** (-1.0 / halflife)
    weights = np.array([lam ** (n - 1 - i) for i in range(n)], dtype=float)
    weights /= weights.sum()
    a = np.asarray(x[-n:], dtype=float)
    b = np.asarray(y[-n:], dtype=float)
    ma, mb = float((weights * a).sum()), float((weights * b).sum())
    cov = float((weights * (a - ma) * (b - mb)).sum())
    va = float((weights * (a - ma) ** 2).sum())
    vb = float((weights * (b - mb) ** 2).sum())
    if va <= 0 or vb <= 0:
        return None
    return cov / math.sqrt(va * vb)


def usd_index_proxy(
    prices: dict[str, float],
    weights: dict[str, float],
    epoch: dict[str, float],
    usd_is_base: dict[str, bool],
) -> float | None:
    """A geometric USD index from the traded pairs. §1.2.

    DXY cannot be reconstructed: it needs SEK and CHF, which are not in the traded set. So
    this is a **proxy**, named as one, used for context features only -- never as the
    conflicting-exposure filter, because a proxy built from EURUSD/GBPUSD/USDJPY cannot
    independently filter EURUSD/GBPUSD/USDJPY signals. That is circular, and exposure
    control uses direct USD-beta netting instead (`net_usd_exposure`).
    """
    usable = [s for s in weights if s in prices and s in epoch and epoch[s] > 0]
    if not usable:
        return None
    total = sum(weights[s] for s in usable)
    if total <= 0:
        return None
    acc = 0.0
    for symbol in usable:
        sign = 1.0 if usd_is_base.get(symbol, False) else -1.0
        ratio = prices[symbol] / epoch[symbol]
        if ratio <= 0:
            return None
        acc += (weights[symbol] / total) * sign * math.log(ratio)
    return 100.0 * math.exp(acc)


def usd_beta(instrument_returns: Sequence[float], proxy_returns: Sequence[float]) -> float | None:
    """OLS slope of instrument returns on USD-proxy returns.

    The proxy appears here as a *regression basis* for measuring exposure magnitude, not as
    an independent signal, so the circularity objection to using it as a filter does not
    apply.
    """
    n = min(len(instrument_returns), len(proxy_returns))
    if n < 20:
        return None
    a = np.asarray(instrument_returns[-n:], dtype=float)
    b = np.asarray(proxy_returns[-n:], dtype=float)
    var = float(b.var())
    if var <= 0:
        return None
    return float(np.cov(a, b, ddof=1)[0, 1] / (var * n / (n - 1)))


@dataclass(frozen=True, slots=True)
class ExposureCheck:
    ok: bool
    current: float
    candidate: float
    combined: float
    limit: float
    reason: str


def usd_exposure(
    direction: int, usd_is_base: bool, risk_fraction: float, beta: float
) -> float:
    """Signed USD exposure of one position. §10.5.

    ``usd_sign`` is -1 when USD is the quote currency (long EURUSD is short USD) and +1
    when USD is the base (long USDJPY is long USD).
    """
    usd_sign = 1.0 if usd_is_base else -1.0
    return direction * usd_sign * risk_fraction * beta


def net_usd_exposure(
    active: Sequence[float], candidate: float, limit: float
) -> ExposureCheck:
    """Gate on total net USD risk rather than on proxy correlation.

    This replaces "correlation filter blocks conflicting USD-pair signals", which as
    specified is circular (see `usd_index_proxy`). Netting is objective and non-circular.
    """
    current = sum(active)
    combined = current + candidate
    ok = abs(combined) <= limit
    return ExposureCheck(
        ok=ok,
        current=current,
        candidate=candidate,
        combined=combined,
        limit=limit,
        reason="" if ok else f"exposure:net_usd_{combined:+.2f}_over_{limit:.2f}",
    )


@dataclass(frozen=True, slots=True)
class VwapState:
    vwap: float | None
    sigma: float | None
    z: float | None
    bars: int

    def as_dict(self) -> dict[str, float]:
        out: dict[str, float] = {"vwap_bars": float(self.bars)}
        if self.vwap is not None:
            out["vwap"] = self.vwap
        if self.z is not None:
            out["vwap_z"] = self.z
        return out


def session_vwap(
    bars: Sequence[Bar], session_start: datetime, min_bars: int = 20
) -> VwapState:
    """Tick-volume-weighted VWAP and z-score within a session. §10.4.

    Weighted by **tick volume**, with every consequence in §0.5: it is a broker-specific
    count of price updates, not traded volume. On a venue whose tick throttling changes,
    this feature's distribution shifts with no change in the market, which is why it is
    tagged ``volume_proxy`` and reported separately in the ablation.
    """
    inside = [b for b in bars if b.open_time >= session_start and b.tick_volume > 0]
    if len(inside) < min_bars:
        return VwapState(None, None, None, len(inside))
    typical = np.array([(b.ohlc.h + b.ohlc.l + b.ohlc.c) / 3.0 for b in inside])
    volume = np.array([float(b.tick_volume) for b in inside])
    total = float(volume.sum())
    if total <= 0:
        return VwapState(None, None, None, len(inside))
    vwap = float((typical * volume).sum() / total)
    variance = float((volume * (typical - vwap) ** 2).sum() / total)
    sigma = math.sqrt(variance) if variance > 0 else None
    z = (inside[-1].ohlc.c - vwap) / sigma if sigma else None
    return VwapState(vwap=vwap, sigma=sigma, z=z, bars=len(inside))


def returns_for(bars: Sequence[Bar]) -> list[float]:
    return log_returns(bars)
