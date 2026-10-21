"""Position sizing. `docs/03-signal-engine.md` §6.

**Suggestion only.** No account query, no order. The output is lots to display.

Three rules, each preventing a specific wrong number:

* If the quote-to-account conversion rate is unavailable, sizing returns ``None`` and the
  message says "size unavailable" rather than guessing a rate.
* If the computed size is below the minimum lot, the signal is still emitted with
  ``lots = 0`` and an explicit note. It is never rounded **up**, which would silently
  exceed the risk limit.
* Rounding is always down to the lot step, for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PositionSize:
    lots: float | None
    risk_amount: float
    stop_distance: float
    value_per_price_unit: float | None
    note: str = ""

    @property
    def available(self) -> bool:
        return self.lots is not None

    @property
    def below_minimum(self) -> bool:
        return self.lots == 0.0


def value_per_price_unit(
    contract_size: float,
    base_ccy: str,
    quote_ccy: str,
    account_ccy: str,
    price: float,
    quote_to_account: float | None = None,
) -> float | None:
    """Account currency per one price unit per lot.

    Three cases, and only three:

    * ``quote == account``: ``contract_size``. EURUSD or XAUUSD with a USD account gives
      100,000 and 100 respectively.
    * ``base == account``: ``contract_size / price``. USDJPY at 150 gives 666.67, i.e.
      $6.67 per pip per lot, because the quote currency converts back at the inverse.
    * anything else -- a cross where neither leg is the account currency -- needs an
      explicit ``quote_to_account`` rate and returns ``None`` without one.

    The third case matters: an earlier version applied the inverse fallback whenever the
    quote differed from the account, which silently produced a value 1.4 million times too
    large for a GBP-quoted cross held in USD.
    """
    if quote_ccy == account_ccy:
        return contract_size
    if quote_to_account is not None and quote_to_account > 0:
        return contract_size * quote_to_account
    if base_ccy == account_ccy and price > 0:
        return contract_size / price
    return None


def position_size(
    entry: float,
    stop_loss: float,
    contract_size: float,
    base_ccy: str,
    quote_ccy: str,
    account_ccy: str,
    account_size: float,
    risk_pct: float,
    lot_step: float = 0.01,
    min_lot: float = 0.01,
    quote_to_account: float | None = None,
) -> PositionSize:
    """Risk-based lots, rounded down."""
    risk_amount = account_size * (risk_pct / 100.0)
    stop_distance = abs(entry - stop_loss)
    if stop_distance <= 0:
        return PositionSize(None, risk_amount, stop_distance, None, "zero stop distance")

    value = value_per_price_unit(
        contract_size, base_ccy, quote_ccy, account_ccy, entry, quote_to_account
    )
    if value is None or value <= 0:
        return PositionSize(
            None, risk_amount, stop_distance, None,
            f"size unavailable: no {quote_ccy}->{account_ccy} conversion rate",
        )

    raw = risk_amount / (stop_distance * value)
    lots = (raw // lot_step) * lot_step
    if lots < min_lot:
        return PositionSize(
            0.0, risk_amount, stop_distance, value,
            "below minimum size at the configured risk and this stop distance",
        )
    return PositionSize(round(lots, 8), risk_amount, stop_distance, value)
