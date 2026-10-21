"""Aurion: signal-only trading alert system. No order-execution surface exists."""

__version__ = "0.2.0"

#: Emitted with every /stats response and every validation report, asserted by a test.
BACKTEST_DISCLAIMER = (
    "Backtested and paper results are measurements of past data under modelled costs. "
    "They are not predictions, forecasts, or expected future performance."
)
