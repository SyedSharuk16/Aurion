"""Calibrated probability model. `docs/03-signal-engine.md` §3.

**Why not a weighted score.** The brief specified a weighted sum of module outputs gated by
``R:R >= 2``. That has an objective mismatch: weights fitted to predict *whether the trade
wins* give a monotone function of win probability, while the quantity that matters is
**expectancy**, which depends on probability *and* payoff. Two candidates with the same
score and R:R of 2.0 and 4.5 have very different expectancies, and a score threshold is
indifferent between them.

So the model outputs a **calibrated probability** and the engine gates on expectancy
computed from that probability and the candidate's own payoff.

Implementation notes:

* L2 logistic regression fitted by Newton/IRLS, and isotonic calibration by PAVA, both in
  numpy. No scikit-learn: the runtime only needs to *load* a model and evaluate a dot
  product, and a 1 vCPU / 1-2 GB box should not carry a fitting library it never calls.
* Features are standardised using statistics stored **in the artifact**, so live scoring
  cannot drift from training scaling.
* Sample weights carry average uniqueness: overlapping label horizons make events
  non-i.i.d., and without the correction cross-validation scores are inflated.
* The artifact records its feature schema version, training window and config hash. The
  runtime refuses to apply a model to a different schema version -- it does not coerce or
  pad -- and refuses a model whose training window ends after the evaluation time, which
  makes model leakage in the backtest impossible rather than merely unlikely.
"""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from aurion.core.timeframe import UTC
from aurion.features.schema import FEATURE_SCHEMA_VERSION


class ModelError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Isotonic calibration (PAVA)
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class IsotonicCalibrator:
    """Monotone mapping from raw score to calibrated probability.

    Fitted by pool-adjacent-violators. Reported alongside a Brier score and a reliability
    curve: a model whose probabilities are not calibrated makes the expectancy gate
    meaningless, however good its ranking is.
    """

    thresholds: tuple[float, ...]
    values: tuple[float, ...]

    def __call__(self, score: float) -> float:
        if not self.thresholds:
            return score
        idx = int(np.searchsorted(np.asarray(self.thresholds), score, side="right")) - 1
        idx = min(max(idx, 0), len(self.values) - 1)
        return float(self.values[idx])


def fit_isotonic(
    scores: Sequence[float], labels: Sequence[int], weights: Sequence[float] | None = None
) -> IsotonicCalibrator:
    if not scores:
        return IsotonicCalibrator((), ())
    order = np.argsort(np.asarray(scores, dtype=float))
    x = np.asarray(scores, dtype=float)[order]
    y = np.asarray(labels, dtype=float)[order]
    w = (
        np.ones_like(y)
        if weights is None
        else np.asarray(weights, dtype=float)[order]
    )

    # Pool adjacent violators.
    blocks: list[list[float]] = [[xi, yi * wi, wi] for xi, yi, wi in zip(x, y, w, strict=True)]
    i = 0
    while i < len(blocks) - 1:
        left, right = blocks[i], blocks[i + 1]
        if left[1] / left[2] > right[1] / right[2]:
            blocks[i] = [left[0], left[1] + right[1], left[2] + right[2]]
            del blocks[i + 1]
            if i > 0:
                i -= 1
        else:
            i += 1
    return IsotonicCalibrator(
        thresholds=tuple(b[0] for b in blocks),
        values=tuple(min(max(b[1] / b[2], 0.0), 1.0) for b in blocks),
    )


def brier_score(probabilities: Sequence[float], labels: Sequence[int]) -> float:
    p = np.asarray(probabilities, dtype=float)
    y = np.asarray(labels, dtype=float)
    return float(((p - y) ** 2).mean()) if p.size else float("nan")


def reliability_curve(
    probabilities: Sequence[float], labels: Sequence[int], bins: int = 10
) -> list[tuple[float, float, int]]:
    """``(mean predicted, observed frequency, count)`` per bin."""
    p = np.asarray(probabilities, dtype=float)
    y = np.asarray(labels, dtype=float)
    out: list[tuple[float, float, int]] = []
    edges = np.linspace(0.0, 1.0, bins + 1)
    for lo, hi in itertools.pairwise(edges):
        mask = (p >= lo) & (p < hi if hi < 1.0 else p <= hi)
        if mask.sum() == 0:
            continue
        out.append((float(p[mask].mean()), float(y[mask].mean()), int(mask.sum())))
    return out


# --------------------------------------------------------------------------
# Logistic regression
# --------------------------------------------------------------------------
@dataclass(slots=True)
class LogisticModel:
    """L2 logistic regression with stored standardisation and a calibrator."""

    feature_names: tuple[str, ...]
    coefficients: np.ndarray
    intercept: float
    means: np.ndarray
    scales: np.ndarray
    calibrator: IsotonicCalibrator = field(default_factory=lambda: IsotonicCalibrator((), ()))
    schema_version: int = FEATURE_SCHEMA_VERSION
    train_start: datetime | None = None
    train_end: datetime | None = None
    config_hash: str = ""
    fold_id: str = ""
    metrics: dict[str, float] = field(default_factory=dict)

    def _standardise(self, x: np.ndarray) -> np.ndarray:
        standardised: np.ndarray = (x - self.means) / self.scales
        return standardised

    def raw_score(self, values: dict[str, float]) -> float:
        x = np.array([values.get(n, 0.0) for n in self.feature_names], dtype=float)
        z = self._standardise(x)
        return float(self.intercept + z @ self.coefficients)

    def probability(self, values: dict[str, float]) -> float:
        """Calibrated P(TP1 before SL)."""
        raw = 1.0 / (1.0 + math.exp(-max(min(self.raw_score(values), 60.0), -60.0)))
        return self.calibrator(raw)

    def contributions(self, values: dict[str, float]) -> list[tuple[str, float]]:
        """Per-feature log-odds contribution, largest absolute first.

        This is what makes "top 3 reasons" an auditable statement about why *this* model
        raised *this* probability, rather than prose. ``c_j = beta_j * (x_j - mean_j)`` in
        standardised units.
        """
        x = np.array([values.get(n, 0.0) for n in self.feature_names], dtype=float)
        z = self._standardise(x)
        pairs = [
            (name, float(z[i] * self.coefficients[i]))
            for i, name in enumerate(self.feature_names)
        ]
        pairs.sort(key=lambda p: abs(p[1]), reverse=True)
        return pairs

    def check_usable(self, as_of: datetime, schema_version: int) -> None:
        """Refuse a mismatched schema or a model trained past the evaluation instant.

        The second check is what makes model leakage in the backtest impossible rather than
        merely unlikely.
        """
        if schema_version != self.schema_version:
            raise ModelError(
                f"feature schema {schema_version} != model schema {self.schema_version}; "
                "a model is never coerced or padded onto a different schema"
            )
        if self.train_end is not None and self.train_end > as_of:
            raise ModelError(
                f"model trained to {self.train_end.isoformat()} cannot score "
                f"{as_of.isoformat()}: that would leak the training window"
            )

    # -- persistence -------------------------------------------------------
    def to_json(self) -> str:
        return json.dumps(
            {
                "feature_names": list(self.feature_names),
                "coefficients": self.coefficients.tolist(),
                "intercept": self.intercept,
                "means": self.means.tolist(),
                "scales": self.scales.tolist(),
                "calibrator": {
                    "thresholds": list(self.calibrator.thresholds),
                    "values": list(self.calibrator.values),
                },
                "schema_version": self.schema_version,
                "train_start": self.train_start.isoformat() if self.train_start else None,
                "train_end": self.train_end.isoformat() if self.train_end else None,
                "config_hash": self.config_hash,
                "fold_id": self.fold_id,
                "metrics": self.metrics,
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, payload: str) -> LogisticModel:
        d = json.loads(payload)
        return cls(
            feature_names=tuple(d["feature_names"]),
            coefficients=np.array(d["coefficients"], dtype=float),
            intercept=float(d["intercept"]),
            means=np.array(d["means"], dtype=float),
            scales=np.array(d["scales"], dtype=float),
            calibrator=IsotonicCalibrator(
                tuple(d["calibrator"]["thresholds"]), tuple(d["calibrator"]["values"])
            ),
            schema_version=int(d["schema_version"]),
            train_start=(
                datetime.fromisoformat(d["train_start"]).astimezone(UTC)
                if d["train_start"]
                else None
            ),
            train_end=(
                datetime.fromisoformat(d["train_end"]).astimezone(UTC)
                if d["train_end"]
                else None
            ),
            config_hash=d.get("config_hash", ""),
            fold_id=d.get("fold_id", ""),
            metrics=d.get("metrics", {}),
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> LogisticModel:
        return cls.from_json(Path(path).read_text(encoding="utf-8"))


#: `docs/03` §3.2. A **prior**, not a derived quantity: the events-per-variable
#: literature puts the floor for a usable logistic fit at 10 (Peduzzi et al. 1996) and 20
#: is the conservative choice here because a calibration map is fitted on top of the
#: coefficients, consuming degrees of freedom the EPV rule does not account for. It has
#: no backtest justification and cannot have one: it governs whether a fit is attempted
#: at all, so it is not a parameter the backtest can search over.
MIN_EVENTS_PER_FEATURE = 20


def fit_logistic(
    rows: Sequence[dict[str, float]],
    labels: Sequence[int],
    feature_names: Sequence[str],
    weights: Sequence[float] | None = None,
    l2: float = 1.0,
    max_iter: int = 50,
    tol: float = 1e-8,
    enforce_events_per_feature: bool = True,
) -> LogisticModel:
    """Fit by Newton/IRLS with an L2 penalty.

    ``enforce_events_per_feature`` applies the floor of 20 events per feature from
    `docs/03` §3.2. It is a hard error rather than a warning because at the projected
    sample sizes a model with more features than the data supports is not a weaker model,
    it is a fitted-noise model.
    """
    n = len(rows)
    if n != len(labels):
        raise ModelError(f"{n} rows but {len(labels)} labels")
    names = tuple(feature_names)
    if not names:
        raise ModelError("no features supplied")
    if enforce_events_per_feature and n < MIN_EVENTS_PER_FEATURE * len(names):
        raise ModelError(
            f"{n} events for {len(names)} features: the floor is "
            f"{MIN_EVENTS_PER_FEATURE} events per feature "
            f"({MIN_EVENTS_PER_FEATURE * len(names)} needed). Reduce the feature set "
            "by tag group rather than fitting noise."
        )

    x = np.array([[r.get(name, 0.0) for name in names] for r in rows], dtype=float)
    y = np.asarray(labels, dtype=float)
    w = np.ones(n) if weights is None else np.asarray(weights, dtype=float)
    w = w / w.mean() if w.mean() > 0 else np.ones(n)

    means = x.mean(axis=0)
    scales = x.std(axis=0)
    scales[scales <= 1e-12] = 1.0  # a constant feature contributes nothing, not infinity
    z = (x - means) / scales
    design = np.column_stack([np.ones(n), z])

    beta = np.zeros(design.shape[1])
    penalty = np.eye(design.shape[1]) * l2
    penalty[0, 0] = 0.0  # never penalise the intercept

    for _ in range(max_iter):
        eta = np.clip(design @ beta, -60.0, 60.0)
        mu = 1.0 / (1.0 + np.exp(-eta))
        weight = np.clip(mu * (1.0 - mu), 1e-9, None) * w
        gradient = design.T @ (w * (y - mu)) - penalty @ beta
        hessian = design.T @ (design * weight[:, None]) + penalty
        try:
            step = np.linalg.solve(hessian, gradient)
        except np.linalg.LinAlgError as exc:  # pragma: no cover - singular design
            raise ModelError("singular Hessian; reduce the feature set or raise l2") from exc
        beta = beta + step
        if float(np.abs(step).max()) < tol:
            break

    return LogisticModel(
        feature_names=names,
        coefficients=beta[1:],
        intercept=float(beta[0]),
        means=means,
        scales=scales,
    )


def average_uniqueness(
    spans: Sequence[tuple[datetime, datetime]],
) -> list[float]:
    """Average uniqueness per event. `docs/03` §3.2 constraint 3.

    An event's weight is the reciprocal of how many label windows overlap it, averaged over
    its own span. Overlapping horizons make events non-i.i.d., and without this correction
    cross-validation scores are inflated and the model looks far better than it is.
    """
    if not spans:
        return []
    edges = sorted({t for span in spans for t in span})
    overlap: dict[datetime, int] = dict.fromkeys(edges, 0)
    for start, end in spans:
        for edge in edges:
            if start <= edge < end:
                overlap[edge] += 1

    out: list[float] = []
    for start, end in spans:
        inside = [overlap[e] for e in edges if start <= e < end]
        out.append(
            float(np.mean([1.0 / c for c in inside if c > 0])) if inside else 1.0
        )
    return out


@dataclass(slots=True)
class ConstantProbabilityModel:
    """Fixed probability, for the power pilot. `docs/07-power-and-capacity.md` §5.

    The pilot runs the hard gates and the geometry with ``p_hat`` replaced by a constant,
    so it measures the **candidate and trade rate of the geometry** rather than of a fitted
    model. That is the whole point: fitting a model first would make the measured trade
    count a function of the model, and the trade count is what decides whether a model can
    be fitted at all.

    It satisfies the same interface the engine calls, so the pilot and the live path run
    identical code.
    """

    p: float = 0.5
    feature_names: tuple[str, ...] = ()
    schema_version: int = FEATURE_SCHEMA_VERSION
    train_start: datetime | None = None
    train_end: datetime | None = None
    config_hash: str = "pilot"
    fold_id: str = "pilot"
    metrics: dict[str, float] = field(default_factory=dict)

    def probability(self, values: dict[str, float]) -> float:
        _ = values
        return self.p

    def raw_score(self, values: dict[str, float]) -> float:
        _ = values
        return self.p

    def contributions(self, values: dict[str, float]) -> list[tuple[str, float]]:
        """No attribution exists for a constant.

        Returning an empty list rather than fabricated reasons is deliberate: the pilot's
        output must not look like a model's.
        """
        _ = values
        return []

    def check_usable(self, as_of: datetime, schema_version: int) -> None:
        if schema_version != self.schema_version:
            raise ModelError(
                f"feature schema {schema_version} != pilot schema {self.schema_version}"
            )
        _ = as_of
