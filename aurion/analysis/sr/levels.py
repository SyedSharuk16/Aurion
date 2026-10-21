"""Support and resistance by weighted density. §7.1, §7.2, §7.3.

``strength`` is explicitly a **model output** -- a normalised density peak height under
stated weights -- not a measurement of a market property. There is no ground truth for the
strength of a level, so nothing can be validated against; its only validation is predictive.
It is never described as "how strong the level is".

KDE is primary rather than DBSCAN for two reasons: it yields a continuous strength score as
a by-product, and DBSCAN's ``eps`` creates hard cluster boundaries that make the level set
discontinuous in ``eps``, which is bad for sensitivity analysis.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

import numpy as np

from aurion.analysis.structure.swings import Swing
from aurion.core.timeframe import TimeFrame
from aurion.data.types import Bar

_SQRT_2PI = math.sqrt(2.0 * math.pi)


@dataclass(frozen=True, slots=True)
class SRLevel:
    instrument: str
    timeframe: TimeFrame
    price: float
    strength: float
    touches: int
    method: str
    computed_at: datetime

    @property
    def confirmed_at(self) -> datetime:
        return self.computed_at


@dataclass(frozen=True, slots=True)
class SwingWeight:
    swing: Swing
    recency: float
    touch: float
    rejection: float

    @property
    def total(self) -> float:
        return self.recency * self.touch * self.rejection


def weight_swings(
    swings: Sequence[Swing],
    bars: Sequence[Bar],
    halflife_bars: float,
    touch_tolerance_atr: float,
    atr: float,
    touch_min_separation: int,
    rejection_clamp: tuple[float, float] = (0.25, 4.0),
) -> list[SwingWeight]:
    """Recency x touch-count x rejection weight for each swing.

    ``touches`` counts distinct later bars that came within ``tolerance`` of the swing
    price, where distinct means separated by at least ``touch_min_separation`` bars. The
    separation rule is what stops one consolidation being counted as many touches.

    The rejection term uses **tick volume**, normalised against the median. Every caveat in
    §0.5 applies: it is a broker-specific update count, not traded volume, and features
    built on it are tagged ``volume_proxy`` so they can be dropped wholesale if the
    broker's aggregation changes.
    """
    if not bars or atr <= 0:
        return []
    volumes = [b.tick_volume for b in bars if b.tick_volume > 0]
    median_volume = float(np.median(volumes)) if volumes else 1.0
    tol = touch_tolerance_atr * atr
    last_index = len(bars) - 1
    decay = math.log(2.0) / max(halflife_bars, 1.0)
    out: list[SwingWeight] = []

    for swing in swings:
        age = max(last_index - swing.bar_index, 0)
        recency = math.exp(-decay * age)

        touches = 0
        last_touch = -(touch_min_separation + 1)
        for j in range(swing.bar_index + 1, len(bars)):
            reach = bars[j].ohlc.h if swing.is_high else bars[j].ohlc.l
            if abs(reach - swing.price) <= tol and j - last_touch > touch_min_separation:
                touches += 1
                last_touch = j
        touch = 1.0 + math.log1p(touches)

        raw = bars[swing.bar_index].tick_volume
        rejection = min(
            max(raw / median_volume if median_volume > 0 else 1.0, rejection_clamp[0]),
            rejection_clamp[1],
        )
        out.append(SwingWeight(swing, recency, touch, rejection))
    return out


def kde_levels(
    weights: Sequence[SwingWeight],
    bandwidth: float,
    computed_at: datetime,
    grid_divisor: int = 8,
) -> list[SRLevel]:
    """Local maxima of a weighted Gaussian KDE over swing prices."""
    if not weights or bandwidth <= 0:
        return []
    prices = np.array([w.swing.price for w in weights], dtype=float)
    ws = np.array([w.total for w in weights], dtype=float)
    total = ws.sum()
    if total <= 0:
        return []

    step = bandwidth / grid_divisor
    grid = np.arange(prices.min() - bandwidth, prices.max() + bandwidth + step, step)
    if grid.size < 3:
        return []
    z = (grid[:, None] - prices[None, :]) / bandwidth
    density = (np.exp(-0.5 * z * z) / _SQRT_2PI * ws[None, :]).sum(axis=1) / total

    peak_max = float(density.max())
    if peak_max <= 0:
        return []

    out: list[SRLevel] = []
    swing = weights[0].swing
    for i in range(1, len(density) - 1):
        if density[i] > density[i - 1] and density[i] >= density[i + 1]:
            price = float(grid[i])
            near = [
                w
                for w in weights
                if abs(w.swing.price - price) <= bandwidth
            ]
            out.append(
                SRLevel(
                    instrument=swing.instrument,
                    timeframe=swing.timeframe,
                    price=price,
                    strength=float(density[i]) / peak_max,
                    touches=len(near),
                    method="kde",
                    computed_at=computed_at,
                )
            )
    return out


def dbscan_levels(
    weights: Sequence[SwingWeight],
    eps: float,
    computed_at: datetime,
    min_samples: int = 2,
) -> list[SRLevel]:
    """Single-link clustering on 1-D price, weighted by ``SwingWeight.total``.

    Equivalent to DBSCAN on one dimension without the scikit-learn dependency: in 1-D,
    DBSCAN's density-reachability reduces to chaining points within ``eps``.
    """
    if not weights or eps <= 0:
        return []
    ordered = sorted(weights, key=lambda w: w.swing.price)
    clusters: list[list[SwingWeight]] = [[ordered[0]]]
    for w in ordered[1:]:
        if w.swing.price - clusters[-1][-1].swing.price <= eps:
            clusters[-1].append(w)
        else:
            clusters.append([w])

    kept = [c for c in clusters if len(c) >= min_samples]
    if not kept:
        return []
    max_weight = max(sum(w.total for w in c) for c in kept)
    out: list[SRLevel] = []
    for cluster in kept:
        total = sum(w.total for w in cluster)
        price = sum(w.swing.price * w.total for w in cluster) / total
        out.append(
            SRLevel(
                instrument=cluster[0].swing.instrument,
                timeframe=cluster[0].swing.timeframe,
                price=price,
                strength=total / max_weight if max_weight > 0 else 0.0,
                touches=len(cluster),
                method="dbscan",
                computed_at=computed_at,
            )
        )
    return out


def merge_levels(levels: Sequence[SRLevel], max_distance: float) -> list[SRLevel]:
    """Merge the nearest pair while they are within ``max_distance``, repeatedly.

    Merged strength is a probabilistic OR, ``1 - prod(1 - s_i)``, so it saturates at 1
    rather than summing past it.
    """
    if not levels:
        return []
    work = sorted(levels, key=lambda level: level.price)
    while len(work) > 1:
        gaps = [work[i + 1].price - work[i].price for i in range(len(work) - 1)]
        smallest = min(range(len(gaps)), key=lambda i: gaps[i])
        if gaps[smallest] > max_distance:
            break
        a, b = work[smallest], work[smallest + 1]
        total_strength = a.strength + b.strength
        price = (
            (a.price * a.strength + b.price * b.strength) / total_strength
            if total_strength > 0
            else (a.price + b.price) / 2.0
        )
        merged = SRLevel(
            instrument=a.instrument,
            timeframe=a.timeframe,
            price=price,
            strength=1.0 - (1.0 - a.strength) * (1.0 - b.strength),
            touches=a.touches + b.touches,
            method=a.method,
            computed_at=max(a.computed_at, b.computed_at),
        )
        work = [*work[:smallest], merged, *work[smallest + 2:]]
    return work


def nearest_level(levels: Sequence[SRLevel], price: float) -> SRLevel | None:
    return min(levels, key=lambda level: abs(level.price - price)) if levels else None
