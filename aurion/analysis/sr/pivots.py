"""Classic and Camarilla pivots, and round numbers. §7.4, §7.5, §7.6.

The Camarilla constants (``1.1`` and the divisors 2, 4, 6, 12) have **no published
derivation**. They are reproduced exactly as conventionally stated and are never tuned:
fitting undocumented folklore constants is how a four-parameter indicator becomes a
four-parameter overfit. Camarilla enters only as secondary confluence and is expected to be
ablated out -- it is folded into the S&R arm rather than given one of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aurion.data.types import Bar


@dataclass(frozen=True, slots=True)
class PivotSet:
    source_open_time: datetime
    confirmed_at: datetime
    levels: dict[str, float]

    def nearest(self, price: float) -> tuple[str, float]:
        name = min(self.levels, key=lambda k: abs(self.levels[k] - price))
        return name, self.levels[name]

    def distance_atr(self, price: float, atr: float) -> dict[str, float]:
        if atr <= 0:
            return {}
        return {k: (price - v) / atr for k, v in self.levels.items()}


def classic_pivots(previous: Bar) -> PivotSet:
    """``P = (H + L + C) / 3`` and the standard three levels each side."""
    h, low, c = previous.ohlc.h, previous.ohlc.l, previous.ohlc.c
    p = (h + low + c) / 3.0
    rng = h - low
    return PivotSet(
        source_open_time=previous.open_time,
        confirmed_at=previous.close_time,
        levels={
            "P": p,
            "R1": 2 * p - low,
            "S1": 2 * p - h,
            "R2": p + rng,
            "S2": p - rng,
            "R3": h + 2 * (p - low),
            "S3": low - 2 * (h - p),
        },
    )


def camarilla_pivots(previous: Bar) -> PivotSet:
    """Camarilla levels, reproduced verbatim. The constants are not tuned."""
    h, low, c = previous.ohlc.h, previous.ohlc.l, previous.ohlc.c
    rng = h - low
    h5 = (h / low) * c if low > 0 else c
    return PivotSet(
        source_open_time=previous.open_time,
        confirmed_at=previous.close_time,
        levels={
            "H4": c + rng * 1.1 / 2,
            "H3": c + rng * 1.1 / 4,
            "H2": c + rng * 1.1 / 6,
            "H1": c + rng * 1.1 / 12,
            "L1": c - rng * 1.1 / 12,
            "L2": c - rng * 1.1 / 6,
            "L3": c - rng * 1.1 / 4,
            "L4": c - rng * 1.1 / 2,
            "H5": h5,
            "L5": 2 * c - h5,
        },
    )


def round_number_levels(price: float, tiers: list[float], per_tier: int = 2) -> dict[str, float]:
    """Nearest round-number levels above and below, per tier.

    XAUUSD uses multiples of 10/50/100; 5-digit FX uses 0.0050 and 0.0100 (the x.xx50 and
    x.xx00 levels); JPY pairs use 0.50 and 1.00.
    """
    out: dict[str, float] = {}
    for tier in tiers:
        if tier <= 0:
            continue
        base = (price // tier) * tier
        for k in range(per_tier):
            out[f"rn{tier:g}_below_{k}"] = base - k * tier
            out[f"rn{tier:g}_above_{k}"] = base + (k + 1) * tier
    return out


def distance_to_nearest_round(price: float, tier: float) -> float:
    """Signed distance to the nearest multiple of ``tier``."""
    if tier <= 0:
        return 0.0
    nearest = round(price / tier) * tier
    return price - nearest
