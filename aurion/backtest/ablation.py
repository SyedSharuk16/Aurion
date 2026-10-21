"""Ablation design, branch determination and the Tier 2 seal.

`docs/04-validation-protocol.md` §4 and `docs/07-power-and-capacity.md` §10, §12, §13.

Three things here are protocol, not convenience:

**The C3 branch is pre-committed.** `docs/07` Part C predicts the pilot will show this
design cannot afford module selection at all. Building a five-arm selection apparatus that
is pre-registered as unusable, then running it anyway, is how one arm wins on noise and a
prior-plausibility decision gets retroactively dressed as a result. The branch is therefore
determined from the pilot's measured Sharpe and trial correlation **before any arm runs**,
and recorded with a timestamp.

**Tier 2 is sealed.** "Built and measured, not selection-eligible" costs zero trials only
if the measurement is sealed until the Tier 1 result is final. Reading four Tier 2 numbers
early folds four arms into the trial count, roughly doubling it and costing ~0.15 of
annualised Sharpe. The seal is a recorded hash plus a refusal, and a breach is reported
rather than absorbed.

**Selection runs under one declared exit policy.** All-out-at-TP1, matching the training
label. Reporting both policies is description; selecting under both is a second fork that
would double the effective trial count without appearing in the trial ledger.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from statistics import NormalDist

import numpy as np

from aurion.backtest.metrics import (
    ConfidenceInterval,
    bootstrap_ci,
    expected_max_sharpe_factor,
    stationary_block_bootstrap,
    trade_stats,
)
from aurion.core.timeframe import UTC
from aurion.features.schema import TIER1_TAGS, TIER2_TAGS, Tag, names_excluding_tags

_N01 = NormalDist()

#: Tier 1 arms in the order `docs/07` §10 names them for branch B. Fixed in advance so a
#: partial branch cannot pick its arms after seeing results.
TIER1_ARM_ORDER: tuple[tuple[str, frozenset[Tag]], ...] = (
    ("sessions", frozenset({Tag.TIME})),
    ("liquidity", frozenset({Tag.LIQUIDITY})),
    ("ict_arrays", frozenset({Tag.ICT})),
    ("regime", frozenset({Tag.REGIME, Tag.MACRO})),
    ("sr", frozenset({Tag.SR})),
)

TIER2_ARMS: tuple[tuple[str, frozenset[Tag]], ...] = (
    ("quarterly", frozenset({Tag.QUARTERLY})),
    ("mmxm", frozenset({Tag.MMXM})),
    ("volume_proxy", frozenset({Tag.VOLUME_PROXY})),
)

SELECTION_EXIT_POLICY = "all_out_tp1"


class Branch(StrEnum):
    A_NO_SELECTION = "A_no_selection"
    B_PARTIAL = "B_partial"
    C_FULL = "C_full"
    PENDING_PILOT = "pending_pilot"


@dataclass(frozen=True, slots=True)
class BranchDecision:
    branch: Branch
    affordable_trials: int
    measured_sharpe_annual: float
    trial_correlation_ratio: float
    oos_days: int
    param_settings: int
    arms: tuple[str, ...]
    decided_at: datetime
    rationale: str
    #: True when the search hit its cap, meaning the penalty never binds within it. The
    #: trial count is then not a real budget and must not be reported as one -- at a low
    #: measured trial correlation the deflation penalty can stay below a plausible Sharpe
    #: for any count, and that is a statement about the correlation assumption, not about
    #: how many trials are safe.
    budget_unbounded: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "branch": str(self.branch),
            "affordable_trials": self.affordable_trials,
            "measured_sharpe_annual": self.measured_sharpe_annual,
            "trial_correlation_ratio": self.trial_correlation_ratio,
            "oos_days": self.oos_days,
            "param_settings": self.param_settings,
            "arms": list(self.arms),
            "decided_at": self.decided_at.isoformat(),
            "rationale": self.rationale,
            "budget_unbounded": self.budget_unbounded,
            "selection_exit_policy": SELECTION_EXIT_POLICY,
        }


def required_annual_sharpe(
    n_trials: int, n_obs: int, correlation_ratio: float = 1.0, dsr_target: float = 0.95
) -> float:
    """Observed annualised Sharpe needed for DSR >= target at ``n_trials`` trials.

    ``correlation_ratio`` scales the dispersion of the Sharpe across trials: 1.0 treats
    trials as independent draws under the null (the worst case), and below 1.0 reflects
    measured correlation between them, which makes the selection penalty milder.
    """
    if n_obs < 3:
        return float("inf")
    sqrt_v = correlation_ratio / math.sqrt(n_obs)
    g = expected_max_sharpe_factor(n_trials)
    sr_star = 0.0 if g == float("-inf") else sqrt_v * g
    needed_daily = sr_star + _N01.inv_cdf(dsr_target) / math.sqrt(n_obs - 1)
    return needed_daily * math.sqrt(252)


def affordable_trials(
    n_obs: int,
    attainable_annual_sharpe: float,
    correlation_ratio: float = 1.0,
    dsr_target: float = 0.95,
    cap: int = 4096,
) -> int:
    """Largest trial count at which DSR >= target remains attainable."""
    best = 0
    for n in range(1, cap + 1):
        if (
            required_annual_sharpe(n, n_obs, correlation_ratio, dsr_target)
            <= attainable_annual_sharpe
        ):
            best = n
        else:
            break
    return best


def determine_branch(
    measured_sharpe_annual: float,
    oos_days: int,
    trial_correlation_ratio: float = 1.0,
    param_settings: int = 3,
    dsr_target: float = 0.95,
    now: datetime | None = None,
) -> BranchDecision:
    """The pre-committed C3 branch. `docs/07` §10.

    Called **once**, from the pilot's measured P3 and P5, before any arm result exists.
    It cannot be re-evaluated after seeing results: if the Sharpe later improves because
    more data was bought, the whole validation re-runs from a fresh determination.
    """
    cap = 4096
    budget = affordable_trials(
        oos_days, measured_sharpe_annual, trial_correlation_ratio, dsr_target, cap
    )
    unbounded = budget >= cap
    arms_budget = max(budget - param_settings, 0)

    if budget <= 2:
        branch = Branch.A_NO_SELECTION
        arms: tuple[str, ...] = ()
        rationale = (
            f"{budget} trial(s) affordable at SR_ann {measured_sharpe_annual:.2f} over "
            f"{oos_days} OOS days. The ablation is NOT run as selection: the base model "
            "ships with all five Tier 1 groups enabled on prior plausibility, the ablation "
            "is reported descriptively, and the report states that no module was selected "
            "on evidence."
        )
    elif arms_budget >= len(TIER1_ARM_ORDER) + 1:
        branch = Branch.C_FULL
        arms = tuple(name for name, _ in TIER1_ARM_ORDER)
        rationale = (
            (
                f"the deflation penalty never binds below SR_ann "
                f"{measured_sharpe_annual:.2f} within a {cap}-trial search at a measured "
                f"trial correlation of {trial_correlation_ratio:.2f}, so the budget is not "
                "a meaningful number; selection runs on all Tier 1 arms, and the headline "
                "DSR must still be reported at the WORST-CASE correlation of 1.0"
            )
            if unbounded
            else (
                f"{budget} trials affordable: selection runs on all {len(arms)} Tier 1 "
                "arms plus the selectivity dimension."
            )
        )
    else:
        take = max(min(arms_budget - 1, len(TIER1_ARM_ORDER)), 0)
        arms = tuple(name for name, _ in TIER1_ARM_ORDER[:take])
        if not arms:
            # Partial selection of nothing is no selection. Reporting branch B with zero
            # arms would let the report claim a selection branch while selecting nothing.
            branch = Branch.A_NO_SELECTION
            rationale = (
                f"{budget} trial(s) affordable with {param_settings} spent on parameter "
                "settings leaves no arm budget, so this is branch A: the ablation is not "
                "run as selection, the base model ships on prior plausibility, and the "
                "report states that no module was selected on evidence."
            )
        else:
            branch = Branch.B_PARTIAL
            rationale = (
                f"{budget} trials affordable, {param_settings} spent on parameter "
                f"settings, so selection runs on the first {len(arms)} arm(s) in the "
                f"pre-declared order {[n for n, _ in TIER1_ARM_ORDER]}. The remainder are "
                "descriptive."
            )
    return BranchDecision(
        branch=branch,
        affordable_trials=budget,
        measured_sharpe_annual=measured_sharpe_annual,
        trial_correlation_ratio=trial_correlation_ratio,
        oos_days=oos_days,
        param_settings=param_settings,
        arms=arms,
        decided_at=now or datetime.now(tz=UTC),
        rationale=rationale,
        budget_unbounded=unbounded,
    )


# --------------------------------------------------------------------------
# Minimum detectable effect
# --------------------------------------------------------------------------
def holm_critical_z(alpha: float, n_tests: int, power: float = 0.80) -> float:
    """Critical plus power quantiles under a Bonferroni-equivalent family correction.

    Holm is step-down, so its *smallest* threshold is ``alpha / n_tests``; using that
    throughout is the conservative end of Holm.
    """
    return _N01.inv_cdf(1.0 - alpha / (2.0 * max(n_tests, 1))) + _N01.inv_cdf(power)


def mde_paired_crn(
    n_trades: int,
    affected_frac: float,
    sigma_d_affected: float,
    n_tests: int = 1,
    alpha: float = 0.05,
    power: float = 0.80,
) -> float:
    """MDE on **overall** expectancy for a paired comparison under common random numbers.

    The module changes the outcome on a fraction ``f`` of candidates and leaves the rest
    byte-identical, so ``SE = sigma_d * sqrt(f / N)``.

    Read this together with ``mde_per_affected_trade``. Pairing makes the detectable
    *overall* effect shrink as ``f`` shrinks, but the per-trade effect required grows as
    ``1/sqrt(f)``. A module firing on 5% of candidates needs an implausible per-trade
    effect to be resolvable at all; high-``f`` modules are the resolvable ones.
    """
    if n_trades <= 0 or affected_frac <= 0:
        return float("inf")
    se = sigma_d_affected * math.sqrt(affected_frac / n_trades)
    return holm_critical_z(alpha, n_tests, power) * se


def mde_per_affected_trade(
    n_trades: int,
    affected_frac: float,
    sigma_d_affected: float,
    n_tests: int = 1,
    alpha: float = 0.05,
    power: float = 0.80,
) -> float:
    """The number to read: effect per affected trade, which grows as ``f`` shrinks."""
    overall = mde_paired_crn(n_trades, affected_frac, sigma_d_affected, n_tests, alpha, power)
    return overall / affected_frac if affected_frac > 0 else float("inf")


def trades_required(
    target_mde: float,
    affected_frac: float,
    sigma_d_affected: float,
    n_tests: int = 1,
    alpha: float = 0.05,
    power: float = 0.80,
) -> int:
    """Trades needed to resolve ``target_mde``. Inverts `mde_paired_crn`."""
    if target_mde <= 0:
        return 2**31 - 1
    z = holm_critical_z(alpha, n_tests, power)
    return math.ceil((z * sigma_d_affected) ** 2 * affected_frac / target_mde**2)


def estimate_sigma_d(
    paired_differences: Sequence[float],
) -> float:
    """Measured dispersion of the paired difference on the affected subset.

    This is pilot output **P1** (`docs/07` §13), promoted from P4 because it swings the MDE
    by 2.5x and alone determines whether the ablation resolves anything. The early bound
    before any module exists is ``sigma_d ~ sqrt(2) * sigma_R`` restricted to
    near-threshold candidates; this is the realised version.
    """
    affected = [d for d in paired_differences if d != 0.0]
    if len(affected) < 2:
        return float("nan")
    return float(np.std(np.asarray(affected, dtype=float), ddof=1))


# --------------------------------------------------------------------------
# Arm results
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ArmResult:
    arm: str
    tags_removed: tuple[str, ...]
    features_retained: int
    n_trades_full: int
    n_trades_arm: int
    expectancy_full: float
    expectancy_arm: float
    delta: float
    affected_frac: float
    sigma_d: float
    mde_overall: float
    mde_per_affected: float
    resolvable: bool
    ci: ConfidenceInterval | None
    selection_eligible: bool
    verdict: str

    def as_dict(self) -> dict[str, object]:
        return {
            "arm": self.arm,
            "tags_removed": list(self.tags_removed),
            "features_retained": self.features_retained,
            "n_trades_full": self.n_trades_full,
            "n_trades_arm": self.n_trades_arm,
            "expectancy_full_r": self.expectancy_full,
            "expectancy_arm_r": self.expectancy_arm,
            "delta_r": self.delta,
            "affected_frac": self.affected_frac,
            "sigma_d": self.sigma_d,
            "mde_overall_r": self.mde_overall,
            "mde_per_affected_r": self.mde_per_affected,
            "resolvable": self.resolvable,
            "ci_lower": None if self.ci is None else self.ci.lower,
            "ci_upper": None if self.ci is None else self.ci.upper,
            "selection_eligible": self.selection_eligible,
            "verdict": self.verdict,
        }


def evaluate_arm(
    arm: str,
    tags: frozenset[Tag],
    full_returns: Sequence[float],
    arm_returns: Sequence[float],
    paired_differences: Sequence[float],
    n_tests: int,
    selection_eligible: bool,
    bootstrap_paths: int = 2000,
    seed: int = 0,
) -> ArmResult:
    """One ablation arm, with its own resolvability verdict.

    The verdict distinguishes three outcomes, and the third is the one that matters:
    *helps*, *does not help*, and **unresolved** -- the sample cannot tell. Counting an
    unresolved arm as "contributes nothing" is the self-confirming move `docs/07` Part C
    exists to prevent.
    """
    full = trade_stats(full_returns)
    arm_stats = trade_stats(arm_returns)
    delta = arm_stats.expectancy - full.expectancy

    affected = [d for d in paired_differences if d != 0.0]
    affected_frac = len(affected) / len(paired_differences) if paired_differences else 0.0
    sigma_d = estimate_sigma_d(paired_differences)

    if affected_frac > 0 and not math.isnan(sigma_d):
        mde = mde_paired_crn(len(paired_differences), affected_frac, sigma_d, n_tests)
        mde_per = mde / affected_frac
    else:
        mde = float("inf")
        mde_per = float("inf")

    ci = None
    if affected:
        paths = stationary_block_bootstrap(
            list(paired_differences), paths=bootstrap_paths, seed=seed
        )
        ci = bootstrap_ci(paths, delta, method="paired_block")

    resolvable = abs(delta) > mde
    if not resolvable:
        verdict = (
            f"unresolved: |delta| {abs(delta):.3f}R is below the MDE {mde:.3f}R "
            f"(f={affected_frac:.2f}, sigma_d={sigma_d:.2f}). The sample cannot tell."
        )
    elif delta < 0:
        verdict = f"removing the arm costs {abs(delta):.3f}R: it helps"
    else:
        verdict = f"removing the arm gains {delta:.3f}R: it hurts"

    return ArmResult(
        arm=arm,
        tags_removed=tuple(sorted(str(t) for t in tags)),
        features_retained=len(names_excluding_tags(tags)),
        n_trades_full=full.n,
        n_trades_arm=arm_stats.n,
        expectancy_full=full.expectancy,
        expectancy_arm=arm_stats.expectancy,
        delta=delta,
        affected_frac=affected_frac,
        sigma_d=sigma_d,
        mde_overall=mde,
        mde_per_affected=mde_per,
        resolvable=resolvable,
        ci=ci,
        selection_eligible=selection_eligible,
        verdict=verdict,
    )


def holm_adjust(p_values: Sequence[float]) -> list[float]:
    """Holm step-down adjusted p-values, order preserved."""
    n = len(p_values)
    if n == 0:
        return []
    order = sorted(range(n), key=lambda i: p_values[i])
    adjusted = [0.0] * n
    running = 0.0
    for rank, idx in enumerate(order):
        running = max(running, (n - rank) * p_values[idx])
        adjusted[idx] = min(running, 1.0)
    return adjusted


# --------------------------------------------------------------------------
# Tier 2 seal
# --------------------------------------------------------------------------
class SealBreach(RuntimeError):
    """Raised when Tier 2 is read before Tier 1 is committed."""


@dataclass(slots=True)
class Tier2Seal:
    """The sealing protocol. `docs/07` §12.

    Execution order, enforced by the runner and not by discipline:

        1. branch determination        -> written to the ledger, timestamped
        2. Tier 1 runs
        3. Tier 1 result COMMITTED     -> SHA-256 recorded, file becomes the reference
        4. Tier 2 measured             -> written to reports/sealed/, nothing returned
        5. Tier 2 opened and read      -> only now, unseal timestamp recorded

    Reading any Tier 2 number before step 3 folds the four Tier 2 arms into the trial
    count and re-determines the branch with the budget reduced by four. That is recorded as
    a protocol breach rather than quietly absorbed.
    """

    directory: Path
    tier1_digest: str | None = None
    committed_at: datetime | None = None
    unsealed_at: datetime | None = None
    breaches: list[str] = field(default_factory=list)

    def commit_tier1(self, tier1_payload: dict[str, object], now: datetime | None = None) -> str:
        blob = json.dumps(tier1_payload, sort_keys=True, default=str)
        self.tier1_digest = hashlib.sha256(blob.encode()).hexdigest()
        self.committed_at = now or datetime.now(tz=UTC)
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.directory / "tier1_committed.json").write_text(
            json.dumps(
                {
                    "digest": self.tier1_digest,
                    "committed_at": self.committed_at.isoformat(),
                    "payload": tier1_payload,
                },
                indent=2,
                sort_keys=True,
                default=str,
            ),
            encoding="utf-8",
        )
        return self.tier1_digest

    def seal_tier2(self, tier2_payload: dict[str, object]) -> Path:
        """Write Tier 2 results where nothing in the Tier 1 path can read them.

        Returns the path only. The contents are deliberately not returned, logged or
        reported: a function that handed them back would make the seal decorative.
        """
        if self.tier1_digest is None:
            self.breaches.append("tier2 sealed before tier1 was committed")
        self.directory.mkdir(parents=True, exist_ok=True)
        suffix = (self.tier1_digest or "uncommitted")[:12]
        path = self.directory / f"tier2_{suffix}.json"
        path.write_text(
            json.dumps(tier2_payload, indent=2, sort_keys=True, default=str),
            encoding="utf-8",
        )
        return path

    def unseal(
        self, tier1_payload: dict[str, object], now: datetime | None = None
    ) -> dict[str, object]:
        """Open Tier 2, refusing if Tier 1 has changed since it was committed."""
        if self.tier1_digest is None:
            raise SealBreach("tier1 was never committed; nothing to unseal against")
        blob = json.dumps(tier1_payload, sort_keys=True, default=str)
        digest = hashlib.sha256(blob.encode()).hexdigest()
        if digest != self.tier1_digest:
            raise SealBreach(
                "tier1 result changed after it was committed: "
                f"{digest[:12]} != {self.tier1_digest[:12]}. Tier 1 cannot be revised "
                "once Tier 2 is known."
            )
        path = self.directory / f"tier2_{self.tier1_digest[:12]}.json"
        if not path.exists():
            raise SealBreach(f"no sealed tier2 payload at {path}")
        self.unsealed_at = now or datetime.now(tz=UTC)
        loaded: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
        return loaded

    def penalty_trials(self) -> int:
        """Trials to add to the budget if the seal was breached."""
        return len(TIER2_ARMS) + 1 if self.breaches else 0


def tier1_tags() -> frozenset[Tag]:
    return TIER1_TAGS


def tier2_tags() -> frozenset[Tag]:
    return TIER2_TAGS
