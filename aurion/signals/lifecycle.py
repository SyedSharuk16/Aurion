"""Signal lifecycle state machine. `docs/03-signal-engine.md` §5.

The system **suggests** moving the stop to break-even after TP1. It does not and cannot
move anything: there is no execution surface. Subsequent accounting assumes the suggestion
was followed, and the backtest also reports the no-BE variant, because that assumption
changes expectancy materially and should not be buried in a default.

Intrabar resolution (§4.5): when SL and a TP are both touched inside the same M1 bar, the
SL is assumed first. The **share of trades decided by that rule is a headline metric** --
above 5% the results are substantially a function of the assumption, and a sensitivity run
under the optimistic rule bounds the effect.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from aurion.analysis.base import Direction
from aurion.data.types import M1Candle


class SignalState(StrEnum):
    PENDING = "pending"
    TRIGGERED = "triggered"
    TP1_HIT = "tp1_hit"
    TP2_HIT = "tp2_hit"
    TP3_HIT = "tp3_hit"
    BE_STOP = "be_stop"
    SL_HIT = "sl_hit"
    SL_AFTER_TP = "sl_after_tp"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    FORCE_CLOSED = "force_closed"


TERMINAL_STATES: frozenset[SignalState] = frozenset(
    {
        SignalState.TP3_HIT,
        SignalState.BE_STOP,
        SignalState.SL_HIT,
        SignalState.SL_AFTER_TP,
        SignalState.EXPIRED,
        SignalState.CANCELLED,
        SignalState.FORCE_CLOSED,
    }
)
ACTIVE_STATES: frozenset[SignalState] = frozenset(
    {
        SignalState.PENDING,
        SignalState.TRIGGERED,
        SignalState.TP1_HIT,
        SignalState.TP2_HIT,
    }
)


@dataclass(frozen=True, slots=True)
class Transition:
    state: SignalState
    at: datetime
    price: float
    realised_r: float | None = None
    note: str = ""


@dataclass(slots=True)
class TrackedSignal:
    """One signal and its full transition history."""

    signal_id: str
    instrument: str
    direction: Direction
    entry: float
    stop_loss: float
    targets: tuple[float, ...]
    expires_at: datetime
    created_at: datetime
    allocation: tuple[float, ...] = (0.5, 0.3, 0.2)
    assume_be_after_tp1: bool = True
    state: SignalState = SignalState.PENDING
    history: list[Transition] = field(default_factory=list)
    realised_r: float = 0.0
    filled_fraction: float = 0.0
    tie_break_decided: bool = False
    triggered_at: datetime | None = None

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop_loss)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def effective_stop(self) -> float:
        """Break-even after TP1 when that assumption is enabled."""
        if self.assume_be_after_tp1 and self.state in (
            SignalState.TP1_HIT,
            SignalState.TP2_HIT,
        ):
            return self.entry
        return self.stop_loss

    def _record(self, state: SignalState, at: datetime, price: float, note: str = "") -> None:
        self.state = state
        self.history.append(Transition(state, at, price, self.realised_r, note))

    def r_of(self, price: float) -> float:
        if self.risk <= 0:
            return 0.0
        return (price - self.entry) * int(self.direction) / self.risk

    # -- stepping ----------------------------------------------------------
    def on_candle(self, candle: M1Candle, optimistic_tie_break: bool = False) -> list[Transition]:
        """Advance the state machine over one M1 bar.

        Resolution is always on M1, never on the signal's own timeframe: resolving a
        15-minute trade on 15-minute bars cannot tell whether the stop or the target came
        first, and systematically favours the target.
        """
        if self.is_terminal:
            return []
        before = len(self.history)

        if candle.open_time >= self.expires_at and self.state is SignalState.PENDING:
            self._record(SignalState.EXPIRED, candle.open_time, self.entry, "expiry reached")
            return self.history[before:]

        high, low = candle.mid.h, candle.mid.l
        long = self.direction is Direction.UP

        if self.state is SignalState.PENDING:
            # Entry triggers on the correct side of the book; see docs/02 §0.3. A buy limit
            # fills at or below its price, so the ask low is the test for a long.
            #
            # There is deliberately no "closed beyond the stop before entry" branch. Both
            # entry models are RETRACEMENT limits, so for a long the stop sits below the
            # entry and price cannot reach the stop without first passing through the
            # entry. A bar that does both fills and then stops out, which is what the code
            # below produces. Pre-entry cancellation therefore comes from `invalidate()`
            # -- a mitigated entry array, an opposing MSS, or the window closing -- not
            # from price. A branch on price here would be unreachable, and unreachable code
            # that looks like a safety net is worse than none.
            touched = candle.ask.l <= self.entry if long else candle.bid.h >= self.entry
            if touched:
                self.triggered_at = candle.close_time
                self._record(SignalState.TRIGGERED, candle.close_time, self.entry)

        if self.state in (SignalState.TRIGGERED, SignalState.TP1_HIT, SignalState.TP2_HIT):
            stop = self.effective_stop
            stop_hit = low <= stop if long else high >= stop
            next_index = {
                SignalState.TRIGGERED: 0,
                SignalState.TP1_HIT: 1,
                SignalState.TP2_HIT: 2,
            }[self.state]
            target = self.targets[next_index] if next_index < len(self.targets) else None
            target_hit = (
                target is not None and (high >= target if long else low <= target)
            )

            if stop_hit and target_hit:
                # Both inside one M1 bar: conservative by default, and the share of trades
                # decided here is reported as a headline metric.
                self.tie_break_decided = True
                if not optimistic_tie_break:
                    target_hit = False
                else:
                    stop_hit = False

            if stop_hit:
                self._close_on_stop(candle, stop)
                return self.history[before:]
            if target_hit and target is not None:
                self._advance_target(candle, next_index, target)

        if (
            self.state in (SignalState.TRIGGERED, SignalState.TP1_HIT, SignalState.TP2_HIT)
            and candle.open_time >= self.expires_at
        ):
            self.realised_r += self._remaining_fraction() * self.r_of(candle.mid.c)
            self._record(
                SignalState.FORCE_CLOSED, candle.close_time, candle.mid.c, "expiry while open"
            )
        return self.history[before:]

    def _remaining_fraction(self) -> float:
        return max(1.0 - self.filled_fraction, 0.0)

    def _close_on_stop(self, candle: M1Candle, stop: float) -> None:
        remaining = self._remaining_fraction()
        self.realised_r += remaining * self.r_of(stop)
        self.filled_fraction = 1.0
        if self.state is SignalState.TRIGGERED:
            self._record(SignalState.SL_HIT, candle.close_time, stop)
        elif self.assume_be_after_tp1:
            self._record(SignalState.BE_STOP, candle.close_time, stop, "stop at break-even")
        else:
            self._record(SignalState.SL_AFTER_TP, candle.close_time, stop)

    def _advance_target(self, candle: M1Candle, index: int, target: float) -> None:
        share = self.allocation[index] if index < len(self.allocation) else 0.0
        self.realised_r += share * self.r_of(target)
        self.filled_fraction = min(self.filled_fraction + share, 1.0)
        state = (SignalState.TP1_HIT, SignalState.TP2_HIT, SignalState.TP3_HIT)[index]
        note = "suggest moving stop to break-even" if index == 0 else ""
        self._record(state, candle.close_time, target, note)

    def force_close(self, at: datetime, price: float, note: str = "friday force close") -> None:
        if self.is_terminal:
            return
        if self.state is SignalState.PENDING:
            self._record(SignalState.CANCELLED, at, price, note)
            return
        self.realised_r += self._remaining_fraction() * self.r_of(price)
        self.filled_fraction = 1.0
        self._record(SignalState.FORCE_CLOSED, at, price, note)

    def invalidate(self, at: datetime, price: float, reason: str) -> None:
        if self.state is SignalState.PENDING:
            self._record(SignalState.CANCELLED, at, price, reason)


class LifecycleManager:
    """Tracks every live signal and reports the metrics the validation report needs."""

    __slots__ = ("_signals", "resolved_count", "tie_break_count")

    def __init__(self) -> None:
        self._signals: dict[str, TrackedSignal] = {}
        self.tie_break_count = 0
        self.resolved_count = 0

    def add(self, signal: TrackedSignal) -> None:
        self._signals[signal.signal_id] = signal

    def active_for(self, instrument: str) -> list[TrackedSignal]:
        return [
            s for s in self._signals.values() if s.instrument == instrument and s.is_active
        ]

    def has_active(self, instrument: str) -> bool:
        return bool(self.active_for(instrument))

    @property
    def all_signals(self) -> list[TrackedSignal]:
        return list(self._signals.values())

    def step(
        self, candle: M1Candle, optimistic_tie_break: bool = False
    ) -> list[tuple[TrackedSignal, Transition]]:
        out: list[tuple[TrackedSignal, Transition]] = []
        for signal in self._signals.values():
            if signal.instrument != candle.instrument or signal.is_terminal:
                continue
            transitions = signal.on_candle(candle, optimistic_tie_break)
            out.extend((signal, t) for t in transitions)
            # React to what actually happened rather than re-reading the mutated state:
            # terminal signals were already skipped, so a terminal transition here is
            # always a fresh resolution.
            if transitions and transitions[-1].state in TERMINAL_STATES:
                self.resolved_count += 1
                if signal.tie_break_decided:
                    self.tie_break_count += 1
        return out

    @property
    def tie_break_share(self) -> float:
        """Headline metric: above 5% the backtest is substantially this assumption."""
        return self.tie_break_count / self.resolved_count if self.resolved_count else 0.0
