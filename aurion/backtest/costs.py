"""Cost model. `docs/04-validation-protocol.md` §2.

These are **assumptions, not settings** (`docs/06` A16). No retail trader can observe the
counterfactual fill, so every number here is declared as an assumption and every headline
statistic is reported under both a conservative and an optimistic variant. If the sign of
expectancy changes between them, the honest conclusion is that no edge has been
demonstrated.

Two modelling errors this avoids:

* **A limit entry does not slip adversely -- it fails to fill.** Treating a limit entry as
  slipping makes a backtest pessimistic on entries and optimistic on exits at the same
  time. Fill realism is a trade-through test instead.
* **Touch-equals-fill overstates fill rates** on exactly the setups this system looks for:
  a wick that reaches the FVG consequent encroachment to the tick and reverses would
  otherwise count as a winning entry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from aurion.analysis.base import Direction
from aurion.core.timeframe import Bucketer, TimeFrame


class SpreadModel(StrEnum):
    FIXED = "fixed"
    HISTORICAL = "historical"
    MODELLED = "modelled"


@dataclass(frozen=True, slots=True)
class CostParams:
    spread_model: SpreadModel = SpreadModel.HISTORICAL
    fixed_spread: float = 0.0
    slippage_atr_m1: float = 0.10
    limit_fill_margin_points: float = 1.0
    commission_per_lot_per_side: float = 0.0
    news_spread_mult: float = 2.0
    news_spread_tau_min: float = 5.0
    point: float = 0.00001

    def with_optimistic_fills(self) -> CostParams:
        """The sensitivity variant: no slippage, touch-equals-fill.

        Used to bound how much of a result rests on the cost assumptions rather than on
        the market.
        """
        return CostParams(
            spread_model=self.spread_model,
            fixed_spread=self.fixed_spread,
            slippage_atr_m1=0.0,
            limit_fill_margin_points=0.0,
            commission_per_lot_per_side=self.commission_per_lot_per_side,
            news_spread_mult=1.0,
            news_spread_tau_min=self.news_spread_tau_min,
            point=self.point,
        )


def news_widening(
    params: CostParams, at: datetime, nearest_event: datetime | None
) -> float:
    """Multiplier on the base spread near a high-impact event.

    The news blackout blocks *new* signals within 30 minutes of an event, so this mostly
    affects **exits** of trades already open -- which is precisely where fixed-spread
    backtests are most wrong.

    The multiplier is the weakest number in the model: it is a guess at a heavy-tailed
    quantity, and replacing it with a measured distribution from stored bid/ask is a
    deliverable, not a footnote.
    """
    if nearest_event is None or params.news_spread_mult <= 1.0:
        return 1.0
    minutes = abs((at - nearest_event).total_seconds()) / 60.0
    decay = math.exp(-minutes / max(params.news_spread_tau_min, 1e-9))
    return 1.0 + (params.news_spread_mult - 1.0) * decay


def effective_spread(
    params: CostParams,
    observed_spread: float | None,
    at: datetime,
    nearest_event: datetime | None = None,
) -> float:
    """Spread to charge at a fill instant."""
    if params.spread_model is SpreadModel.FIXED:
        base = params.fixed_spread
    elif params.spread_model is SpreadModel.HISTORICAL:
        base = observed_spread if observed_spread is not None else params.fixed_spread
    else:
        # MODELLED exists for a mid-only vendor (`data/adapters/twelvedata.py`), where the
        # spread has to be estimated from a fitted distribution rather than observed. That
        # estimator does not exist, and silently returning the historical value would make
        # `spread_model: modelled` a setting that lies about what it does.
        raise NotImplementedError(
            "spread_model=modelled needs a fitted spread distribution, which is not "
            "implemented. Use a bid/ask adapter with spread_model=historical."
        )
    return max(base, 0.0) * news_widening(params, at, nearest_event)


def limit_filled(
    params: CostParams, direction: Direction, limit_price: float, extreme: float
) -> bool:
    """A limit fills only if the correct side of the book trades THROUGH the level.

    ``extreme`` is the bar's ask low for a long entry and bid high for a short, per the
    per-leg basis in `docs/02` §0.3.
    """
    margin = params.limit_fill_margin_points * params.point
    if direction is Direction.UP:
        return extreme <= limit_price - margin
    return extreme >= limit_price + margin


def stop_fill_price(
    params: CostParams,
    direction: Direction,
    stop_price: float,
    atr_m1: float,
    observed_spread: float | None,
    at: datetime,
    nearest_event: datetime | None = None,
) -> float:
    """Stop fills are market orders at a moment of adverse movement.

    Realised slippage depends on the broker's liquidity provider, its internalisation and
    its discretion in fast markets. There is no public record of retail stop fills, so this
    is an assumption: ``slip_atr x ATR(M1) + spread`` in the adverse direction.
    """
    adverse = params.slippage_atr_m1 * max(atr_m1, 0.0) + effective_spread(
        params, observed_spread, at, nearest_event
    )
    return stop_price - adverse if direction is Direction.UP else stop_price + adverse


def commission_cost(params: CostParams, lots: float) -> float:
    """Both sides, in account currency."""
    return 2.0 * params.commission_per_lot_per_side * max(lots, 0.0)


#: The default D1 anchor: 17:00 America/New_York, the broker FX day. That is 21:00 UTC under
#: EDT and 22:00 UTC under EST, so the boundary has to come from the bucketer, which
#: generates boundaries in local time. A fixed UTC hour misclassifies every trade spanning
#: 21:00-22:00 UTC for the ~4.5 months a year the US is on standard time -- in both
#: directions -- and `rollover_share` is an acceptance criterion, not a diagnostic.
_FX_DAY = Bucketer(TimeFrame.D1)


def holds_through_rollover(
    opened: datetime, closed: datetime, fx_day: Bucketer | None = None
) -> bool:
    """Whether a trade spanned the 17:00 NY rollover.

    Defined as the open and close falling in different FX days, using the same bucketer the
    D1 bars use -- so a trade counted as crossing rollover is exactly a trade whose entry
    and exit sit in different D1 bars, with no second definition to drift from it. Pass
    ``fx_day`` built from ``data.anchors.D1`` whenever that anchor is overridden; the
    default stands in only for callers with no config in hand.

    Overnight financing is not modelled, on the grounds that intraday signals with a
    same-session expiry rarely cross rollover. The **share of trades that do** is reported,
    and above 10% financing must be added before the results are trusted. That is the check
    that keeps the omission honest.
    """
    if closed <= opened:
        return False
    day = fx_day if fx_day is not None else _FX_DAY
    return day.open_time(opened) != day.open_time(closed)
