"""Walk-forward folds with purging and embargo. `docs/04-validation-protocol.md` §3.

**Revised after the overlap analysis** (`docs/07` §9, Correction 4). The Phase 1 design --
train 24m / test 3m / step 3m -- gave 8 folds sharing 87.5% of their training data, a
variance inflation of 5.38x and an effective N of **51** from a nominal 276. Training
windows are now **non-overlapping**, so the inflation is exactly 1.00, effective N equals
nominal N, and more of the development window becomes out-of-sample: 414 effective trades
instead of 51, and 36 OOS months instead of 24.

Two findings behind that, both contradicting the original design:

* A **smaller step buys nothing** -- it raises nominal N and the overlap inflation in
  near-equal measure.
* A **shorter training window** raises effective N twice over: more folds, so more of the
  development window is out-of-sample, *and* less overlap between them.

Purging and embargo are not optional. Labels span time, so a training event whose label
window reaches into the test block leaks the test outcome.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True, slots=True)
class Fold:
    fold_id: str
    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime

    @property
    def train_days(self) -> int:
        return (self.train_end - self.train_start).days

    @property
    def test_days(self) -> int:
        return (self.test_end - self.test_start).days

    def in_train(self, at: datetime) -> bool:
        return self.train_start <= at < self.train_end

    def in_test(self, at: datetime) -> bool:
        return self.test_start <= at < self.test_end


@dataclass(frozen=True, slots=True)
class WalkForwardPlan:
    folds: tuple[Fold, ...]
    holdout_start: datetime
    holdout_end: datetime
    train_months: int
    test_months: int
    step_months: int
    calibration_tail_frac: float

    @property
    def training_overlap(self) -> float:
        """0.0 under the adopted design. Anything above it inflates the variance."""
        if self.step_months >= self.train_months:
            return 0.0
        return 1.0 - self.step_months / self.train_months

    @property
    def oos_months(self) -> int:
        return len(self.folds) * self.test_months

    def variance_inflation(self) -> float:
        """Inflation for a statistic averaged over overlapping folds (`docs/07` §9).

        ``Var(mean) = (sigma^2 / m) * [1 + 2 * sum_j (1 - j/m) * rho_j]`` with ``rho_j``
        taken as the training-window overlap fraction -- full transmission, the
        conservative end.

        Applies to **fit-dependent** statistics. For a paired-CRN ablation difference both
        arms share the same folds and the overlap largely cancels, so there the residual is
        estimated from the realised difference series instead.
        """
        m = len(self.folds)
        if m < 2 or self.step_months >= self.train_months:
            return 1.0
        j_max = min(m - 1, math.ceil(self.train_months / self.step_months) - 1)
        acc = sum(
            (1.0 - j / m) * max(0.0, 1.0 - j * self.step_months / self.train_months)
            for j in range(1, j_max + 1)
        )
        return 1.0 + 2.0 * acc

    def effective_n(self, nominal_trades: int) -> float:
        return nominal_trades / self.variance_inflation()


def _add_months(at: datetime, months: int) -> datetime:
    """Calendar-month arithmetic without a third-party dependency."""
    total = at.month - 1 + months
    year = at.year + total // 12
    month = total % 12 + 1
    day = min(
        at.day,
        [31, 29 if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0) else 28,
         31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1],
    )
    return at.replace(year=year, month=month, day=day)


def build_plan(
    data_start: datetime,
    data_end: datetime,
    train_months: int = 12,
    test_months: int = 12,
    step_months: int = 12,
    holdout_months: int = 12,
    calibration_tail_frac: float = 0.20,
) -> WalkForwardPlan:
    """Folds over the development window, with the holdout carved off the end.

    The holdout is the only entirely untouched data and is a **degradation check**, not a
    significance gate: at the projected trade rate it holds ~69 trades, which cannot
    establish significance and will not be reported as if it could.
    """
    holdout_start = _add_months(data_end, -holdout_months)
    folds: list[Fold] = []
    cursor = data_start
    index = 0
    while True:
        train_end = _add_months(cursor, train_months)
        test_end = _add_months(train_end, test_months)
        if test_end > holdout_start:
            break
        folds.append(
            Fold(f"f{index:02d}", cursor, train_end, train_end, test_end)
        )
        index += 1
        cursor = _add_months(cursor, step_months)
    return WalkForwardPlan(
        folds=tuple(folds),
        holdout_start=holdout_start,
        holdout_end=data_end,
        train_months=train_months,
        test_months=test_months,
        step_months=step_months,
        calibration_tail_frac=calibration_tail_frac,
    )


@dataclass(frozen=True, slots=True)
class LabelledEvent:
    """A training event with the span its label occupies."""

    decision_at: datetime
    resolved_at: datetime
    label: int
    features: dict[str, float]
    weight: float = 1.0


def purge_and_embargo(
    events: Sequence[LabelledEvent],
    fold: Fold,
    embargo: timedelta,
) -> list[LabelledEvent]:
    """Training events for one fold, purged and embargoed.

    * **purge** -- drop any training event whose label span ``[t, t_resolved]`` overlaps
      the test block.
    * **embargo** -- additionally drop training events within ``embargo`` of the test block
      start.

    Without both, a training event whose label window reaches into the test block leaks the
    test outcome, and the fold's score is not out-of-sample at all.
    """
    cutoff = fold.test_start - embargo
    out: list[LabelledEvent] = []
    for event in events:
        if not fold.in_train(event.decision_at):
            continue
        if event.resolved_at >= cutoff:
            continue
        out.append(event)
    return out


def calibration_split(
    events: Sequence[LabelledEvent], tail_frac: float
) -> tuple[list[LabelledEvent], list[LabelledEvent]]:
    """Split training events into fit and calibration parts.

    The calibration set is the **last** ``tail_frac`` of the training window by time, held
    out from the coefficient fit. There is no separate validate block in the revised
    design, so isotonic calibration must come from somewhere inside the training window --
    and it has to be the tail, because calibrating on data the coefficients saw would make
    the probabilities look better than they are.
    """
    ordered = sorted(events, key=lambda e: e.decision_at)
    if not ordered:
        return [], []
    cut = max(int(len(ordered) * (1.0 - tail_frac)), 1)
    return ordered[:cut], ordered[cut:]


def max_label_horizon(events: Sequence[LabelledEvent]) -> timedelta:
    """Longest observed label span, which sets the embargo (`docs/04` §3.2)."""
    if not events:
        return timedelta(days=1)
    return max(e.resolved_at - e.decision_at for e in events)
