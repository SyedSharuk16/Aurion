"""Performance statistics. `docs/04-validation-protocol.md` §6, §7.

`tools/power.py` is the standalone planning twin of the deflated-Sharpe maths here: it is
stdlib-only so it can be run before the package is installed. A test asserts the two agree
to within floating-point tolerance, which is what protects against the two drifting apart.

Two corrections from `docs/07` are baked in:

* The deflated Sharpe is computed on the **concatenated walk-forward out-of-sample series**,
  not a 12-month holdout. On 252 daily observations PSR(0) caps at ~0.84 for an annualised
  Sharpe of 1.0, so the holdout criterion was unattainable regardless of system quality.
* The reported confidence interval is the **wider** of a trade-level stationary block
  bootstrap and a fold-cluster bootstrap. Nominal trade count is an upper bound on
  independent evidence, not a count of it.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from statistics import NormalDist

import numpy as np

# stdlib NormalDist rather than scipy: the runtime target is 1 vCPU / 1-2 GB, and the only
# normal-distribution calls are the CDF and inverse CDF in the deflated Sharpe. This is
# also exactly what the standalone `tools/power.py` uses, which is what lets a test assert
# the two implementations agree.
_N01 = NormalDist()


def norm_cdf(x: float) -> float:
    return _N01.cdf(x)


def norm_ppf(p: float) -> float:
    return _N01.inv_cdf(p)


#: Convention, not a tunable: the annualisation factor used throughout `docs/04` and
#: `tools/power.py`. Both must use the same one or the deflated Sharpe disagrees with the
#: power tables, and a cross-check test asserts they do not.
TRADING_DAYS_PER_YEAR = 252
#: Appears in the expected-maximum-Sharpe approximation g(N) (Bailey & Lopez de Prado).
EULER_MASCHERONI = 0.5772156649015329
#: A **prior**. Below ~30 trades a per-cell expectancy is dominated by sampling noise at
#: the per-trade dispersion these systems show, so a breakout cell below it is reported as
#: insufficient rather than given a number. Cells are descriptive and never gate a
#: decision, so this threshold cannot bias a selection.
MIN_CELL_TRADES = 30


@dataclass(frozen=True, slots=True)
class TradeStats:
    n: int
    expectancy: float
    win_rate: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    max_drawdown_r: float
    sharpe: float
    sortino: float
    total_r: float
    std_r: float

    def as_dict(self) -> dict[str, float]:
        return {
            "n": float(self.n),
            "expectancy_r": self.expectancy,
            "win_rate": self.win_rate,
            "profit_factor": self.profit_factor,
            "avg_win_r": self.avg_win,
            "avg_loss_r": self.avg_loss,
            "max_drawdown_r": self.max_drawdown_r,
            "sharpe": self.sharpe,
            "sortino": self.sortino,
            "total_r": self.total_r,
            "std_r": self.std_r,
        }


def max_drawdown_r(returns: Sequence[float]) -> float:
    """Largest peak-to-trough decline of the cumulative R curve.

    Length-checked rather than truth-checked: this is called with both lists and numpy
    rows, and `if not array` raises on anything longer than one element.
    """
    if len(returns) == 0:
        return 0.0
    equity = np.cumsum(np.asarray(returns, dtype=float))
    peak = np.maximum.accumulate(equity)
    return float(np.max(peak - equity))


def trade_stats(returns: Sequence[float]) -> TradeStats:
    if not returns:
        return TradeStats(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    r = np.asarray(returns, dtype=float)
    wins = r[r > 0]
    losses = r[r < 0]
    gross_win = float(wins.sum())
    gross_loss = float(-losses.sum())
    std = float(r.std(ddof=1)) if r.size > 1 else 0.0
    downside = r[r < 0]
    downside_std = float(downside.std(ddof=1)) if downside.size > 1 else 0.0
    return TradeStats(
        n=int(r.size),
        expectancy=float(r.mean()),
        win_rate=float(wins.size / r.size),
        profit_factor=gross_win / gross_loss if gross_loss > 0 else math.inf,
        avg_win=float(wins.mean()) if wins.size else 0.0,
        avg_loss=float(losses.mean()) if losses.size else 0.0,
        max_drawdown_r=max_drawdown_r(returns),
        sharpe=float(r.mean() / std) if std > 0 else 0.0,
        # Zero downside dispersion with a positive mean is unbounded, not zero: reporting
        # 0.0 would read as "no edge", the opposite of what it means.
        sortino=(
            float(r.mean() / downside_std)
            if downside_std > 0
            else (math.inf if r.mean() > 0 else 0.0)
        ),
        total_r=float(r.sum()),
        std_r=std,
    )


# --------------------------------------------------------------------------
# Bootstraps
# --------------------------------------------------------------------------
def stationary_block_bootstrap(
    returns: Sequence[float],
    paths: int = 10_000,
    expected_block: float = 10.0,
    seed: int = 0,
) -> np.ndarray:
    """Politis-Romano stationary bootstrap. Preserves win/loss clustering.

    i.i.d. resampling systematically understates drawdown for any system with serially
    correlated outcomes, so the block figures are the headline and the i.i.d. ones are the
    optimistic baseline.
    """
    r = np.asarray(returns, dtype=float)
    n = r.size
    if n == 0:
        return np.zeros((paths, 0))
    rng = np.random.default_rng(seed)
    p = 1.0 / max(expected_block, 1.0)
    out = np.empty((paths, n), dtype=float)
    for path in range(paths):
        idx = int(rng.integers(0, n))
        for t in range(n):
            if t > 0:
                idx = int(rng.integers(0, n)) if rng.random() < p else (idx + 1) % n
            out[path, t] = r[idx]
    return out


def iid_bootstrap(
    returns: Sequence[float], paths: int = 10_000, seed: int = 0
) -> np.ndarray:
    r = np.asarray(returns, dtype=float)
    if r.size == 0:
        return np.zeros((paths, 0))
    rng = np.random.default_rng(seed)
    return r[rng.integers(0, r.size, size=(paths, r.size))]


def cluster_bootstrap(
    clusters: Sequence[Sequence[float]], paths: int = 10_000, seed: int = 0
) -> np.ndarray:
    """Resample whole folds rather than trades.

    Required for fit-dependent statistics: with overlapping training windows the folds are
    the independent units, not the trades (`docs/07` §9, Correction 4).
    """
    usable = [np.asarray(c, dtype=float) for c in clusters if len(c)]
    if not usable:
        return np.zeros((paths, 0))
    rng = np.random.default_rng(seed)
    out: list[np.ndarray] = []
    for _ in range(paths):
        picks = rng.integers(0, len(usable), size=len(usable))
        out.append(np.concatenate([usable[i] for i in picks]))
    width = min(a.size for a in out)
    return np.vstack([a[:width] for a in out])


@dataclass(frozen=True, slots=True)
class ConfidenceInterval:
    lower: float
    upper: float
    point: float
    method: str

    @property
    def excludes_zero(self) -> bool:
        return self.lower > 0.0 or self.upper < 0.0


def bootstrap_ci(
    paths: np.ndarray, point: float, level: float = 0.95, method: str = "block"
) -> ConfidenceInterval:
    if paths.size == 0:
        return ConfidenceInterval(float("nan"), float("nan"), point, method)
    means = paths.mean(axis=1)
    alpha = (1.0 - level) / 2.0
    return ConfidenceInterval(
        lower=float(np.quantile(means, alpha)),
        upper=float(np.quantile(means, 1.0 - alpha)),
        point=point,
        method=method,
    )


def widest(*intervals: ConfidenceInterval) -> ConfidenceInterval:
    """The reported CI: the widest of those supplied (`docs/07` §9)."""
    usable = [i for i in intervals if not math.isnan(i.lower)]
    if not usable:
        return ConfidenceInterval(float("nan"), float("nan"), 0.0, "none")
    return max(usable, key=lambda i: i.upper - i.lower)


def drawdown_distribution(paths: np.ndarray) -> dict[str, float]:
    if paths.size == 0:
        return {}
    dds = np.array([max_drawdown_r(path) for path in paths])
    return {
        "median": float(np.quantile(dds, 0.5)),
        "p90": float(np.quantile(dds, 0.9)),
        "p95": float(np.quantile(dds, 0.95)),
        "p99": float(np.quantile(dds, 0.99)),
        "p_gt_10r": float((dds > 10).mean()),
        "p_gt_20r": float((dds > 20).mean()),
    }


# --------------------------------------------------------------------------
# Probabilistic and deflated Sharpe
# --------------------------------------------------------------------------
def expected_max_sharpe_factor(n_trials: int) -> float:
    """``g(N)``: expected maximum of N standard-normal Sharpe estimates, in SD units."""
    if n_trials < 2:
        return float("-inf")
    return (1.0 - EULER_MASCHERONI) * norm_ppf(1.0 - 1.0 / n_trials) + (
        EULER_MASCHERONI * norm_ppf(1.0 - 1.0 / (n_trials * math.e))
    )


def probabilistic_sharpe(
    sharpe_daily: float,
    sr_star_daily: float,
    n_obs: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    if n_obs < 2:
        return float("nan")
    denom = math.sqrt(
        max(1.0 - skew * sharpe_daily + ((kurtosis - 1.0) / 4.0) * sharpe_daily**2, 1e-12)
    )
    return norm_cdf((sharpe_daily - sr_star_daily) * math.sqrt(n_obs - 1) / denom)


@dataclass(frozen=True, slots=True)
class DeflatedSharpe:
    sharpe_daily: float
    sharpe_annual: float
    sr_star_daily: float
    psr: float
    dsr: float
    n_obs: int
    n_trials: int
    n_trials_effective: int
    skew: float
    kurtosis: float
    var_sharpe_across_trials: float

    def as_dict(self) -> dict[str, float]:
        return {
            "sharpe_daily": self.sharpe_daily,
            "sharpe_annual": self.sharpe_annual,
            "sr_star_daily": self.sr_star_daily,
            "psr": self.psr,
            "dsr": self.dsr,
            "n_obs": float(self.n_obs),
            "n_trials": float(self.n_trials),
            "n_trials_effective": float(self.n_trials_effective),
            "skew": self.skew,
            "kurtosis": self.kurtosis,
            "var_sharpe_across_trials": self.var_sharpe_across_trials,
        }


def effective_trials(
    trial_returns: Sequence[Sequence[float]], variance_target: float = 0.95
) -> int:
    """Eigenvalues of the trial-return correlation matrix needed for 95% of its variance.

    Reported alongside the raw count so the degree of conservatism is visible. The headline
    DSR uses the RAW count because it is the more conservative of the two: a larger trial
    count implies a larger expected maximum Sharpe under the null.
    """
    usable = [np.asarray(t, dtype=float) for t in trial_returns if len(t) > 1]
    if len(usable) < 2:
        return max(len(usable), 1)
    width = min(a.size for a in usable)
    matrix = np.vstack([a[:width] for a in usable])
    if np.allclose(matrix.std(axis=1), 0):
        return 1
    corr = np.corrcoef(matrix)
    eigenvalues = np.sort(np.abs(np.linalg.eigvalsh(corr)))[::-1]
    total = eigenvalues.sum()
    if total <= 0:
        return 1
    cumulative = np.cumsum(eigenvalues) / total
    return int(np.searchsorted(cumulative, variance_target) + 1)


def deflated_sharpe(
    daily_returns: Sequence[float],
    n_trials: int,
    trial_sharpes: Sequence[float] | None = None,
    n_trials_effective: int | None = None,
) -> DeflatedSharpe:
    """Bailey & Lopez de Prado deflated Sharpe on daily returns in R units."""
    r = np.asarray(daily_returns, dtype=float)
    n = r.size
    # Relative, not absolute: a constant series has std ~1e-17 rather than exactly 0, so a
    # bare `<= 0` guard let it through and reported a Sharpe of 3.5e15 and DSR = 1.0 on
    # data for which the statistic is undefined. Same bug class as the variance ratio.
    scale = float(np.abs(r).mean()) if n else 0.0
    degenerate = n < 3 or float(r.std(ddof=1)) <= 1e-12 * max(scale, 1.0)
    if degenerate:
        return DeflatedSharpe(
            float("nan"), float("nan"), float("nan"), float("nan"), float("nan"),
            n, n_trials, n_trials_effective or n_trials, float("nan"), float("nan"),
            float("nan"),
        )
    sharpe = float(r.mean() / r.std(ddof=1))
    centred = r - r.mean()
    sd = float(r.std(ddof=0))
    skew = float((centred**3).mean() / sd**3) if sd > 0 else 0.0
    kurtosis = float((centred**4).mean() / sd**4) if sd > 0 else 3.0

    if trial_sharpes is not None and len(trial_sharpes) > 1:
        var_sharpe = float(np.var(np.asarray(trial_sharpes, dtype=float), ddof=1))
    else:
        # Independent trials under the null: the worst case, and the default.
        var_sharpe = 1.0 / n
    sr_star = (
        0.0
        if n_trials < 2
        else math.sqrt(max(var_sharpe, 0.0)) * expected_max_sharpe_factor(n_trials)
    )
    return DeflatedSharpe(
        sharpe_daily=sharpe,
        sharpe_annual=sharpe * math.sqrt(TRADING_DAYS_PER_YEAR),
        sr_star_daily=sr_star,
        psr=probabilistic_sharpe(sharpe, 0.0, n, skew, kurtosis),
        dsr=probabilistic_sharpe(sharpe, sr_star, n, skew, kurtosis),
        n_obs=n,
        n_trials=n_trials,
        n_trials_effective=n_trials_effective or n_trials,
        skew=skew,
        kurtosis=kurtosis,
        var_sharpe_across_trials=var_sharpe,
    )


# --------------------------------------------------------------------------
# Breakouts
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Breakout:
    key: str
    n: int
    stats: TradeStats | None
    note: str = ""


def breakout(
    labelled: Sequence[tuple[str, float]], min_cell: int = MIN_CELL_TRADES
) -> list[Breakout]:
    """Stats per cell, with cells below the minimum shown as counts only.

    A "best session" conclusion drawn from 11 trades is noise, and showing the count rather
    than the metric is what stops it being read as a finding.
    """
    groups: dict[str, list[float]] = {}
    for key, value in labelled:
        groups.setdefault(key, []).append(value)
    out: list[Breakout] = []
    for key in sorted(groups):
        values = groups[key]
        if len(values) < min_cell:
            out.append(
                Breakout(key, len(values), None, f"below the {min_cell}-trade minimum")
            )
        else:
            out.append(Breakout(key, len(values), trade_stats(values)))
    return out


@dataclass(slots=True)
class FunnelReport:
    """Rejections by step and reason. The counts are the point."""

    by_step: dict[str, int] = field(default_factory=dict)
    by_reason: dict[str, int] = field(default_factory=dict)
    emitted: int = 0
    evaluated: int = 0

    def record(self, step: str, reason: str | None) -> None:
        self.evaluated += 1
        if reason is None:
            self.emitted += 1
            return
        self.by_step[step] = self.by_step.get(step, 0) + 1
        self.by_reason[reason] = self.by_reason.get(reason, 0) + 1

    def heaviest_filter(self) -> tuple[str, int] | None:
        return max(self.by_step.items(), key=lambda kv: kv[1]) if self.by_step else None
