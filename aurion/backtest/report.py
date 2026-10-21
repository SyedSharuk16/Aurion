"""Validation report generation. `docs/04-validation-protocol.md` §8, §10.

Every report opens with the mandatory disclaimer, which is a module-level constant asserted
present by a test so it cannot be removed by editing a template.

The acceptance criteria encoded here are the **revised** ones (`docs/07` §2, §4). Two
Phase 1 criteria were unmeetable as written and are replaced:

* The >=200-trade holdout requirement. The projection puts a 12-month holdout at ~69
  trades, so the primary out-of-sample set is the concatenated walk-forward test blocks and
  the holdout is a degradation check with its power stated.
* DSR >= 0.95 on the 12-month holdout. On 252 daily observations PSR(0) caps at ~0.84 for
  an annualised Sharpe of 1.0 and zero trials are affordable below SR_ann ~1.65, so the
  criterion could never have been met regardless of system quality. DSR now runs on the
  concatenated out-of-sample series.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from aurion import BACKTEST_DISCLAIMER
from aurion.backtest.ablation import ArmResult, BranchDecision
from aurion.backtest.metrics import (
    Breakout,
    ConfidenceInterval,
    DeflatedSharpe,
    FunnelReport,
    TradeStats,
    drawdown_distribution,
)

#: `docs/04` §8 criterion 5, revised: the count at which a paired-CRN arm resolves a
#: 0.20R effect at f = 0.5. The withdrawn criterion (>=200 holdout trades) was unmeetable.
MIN_OOS_TRADES = 341
#: `docs/04` §8 criterion 3, unchanged from Phase 1.
DSR_TARGET = 0.95
#: Both are **priors** and are stated as such: no measurement fixes either. The first is
#: the share of trades decided by the same-M1-bar SL/TP tie-break above which the headline
#: figures are substantially that assumption rather than the strategy (`docs/06` A16); the
#: second is the share of trades held through 17:00 NY above which unmodelled overnight
#: financing can plausibly move expectancy by more than the effects being measured. Each
#: triggers a warning and a sensitivity run, never a silent pass, which is why a wrong
#: value costs a re-run rather than a false conclusion.
TIE_BREAK_WARN = 0.05
ROLLOVER_WARN = 0.10


@dataclass(frozen=True, slots=True)
class Criterion:
    name: str
    passed: bool
    detail: str
    blocking: bool = True


@dataclass(slots=True)
class ValidationReport:
    """Everything the reader needs to recompute the headline numbers by hand."""

    generated_at: datetime
    git_sha: str
    config_hash: str
    data_start: datetime | None
    data_end: datetime | None
    oos_stats: TradeStats
    oos_ci: ConfidenceInterval
    holdout_stats: TradeStats | None
    dsr: DeflatedSharpe
    branch: BranchDecision | None
    arms: list[ArmResult] = field(default_factory=list)
    funnel: FunnelReport | None = None
    breakouts: dict[str, list[Breakout]] = field(default_factory=dict)
    monte_carlo_block: dict[str, float] = field(default_factory=dict)
    monte_carlo_iid: dict[str, float] = field(default_factory=dict)
    tie_break_share: float = 0.0
    rollover_share: float = 0.0
    walkforward_inflation: float = 1.0
    effective_n: float = 0.0
    trials_counted: int = 0
    calendar_point_in_time: bool = False
    seal_breaches: Sequence[str] = ()
    notes: list[str] = field(default_factory=list)

    # -- acceptance ---------------------------------------------------------
    def criteria(self) -> list[Criterion]:
        """`docs/04` §8, as revised. Any blocking failure keeps the `[PAPER]` tag."""
        out: list[Criterion] = [
            Criterion(
                "oos_trades",
                self.oos_stats.n >= MIN_OOS_TRADES,
                f"{self.oos_stats.n} concatenated-OOS trades against a minimum of "
                f"{MIN_OOS_TRADES} (the count at which a paired arm resolves 0.20R at "
                "f=0.5). Below it the result is inconclusive, not a pass.",
            ),
            Criterion(
                "oos_expectancy_positive",
                self.oos_stats.expectancy > 0 and self.oos_ci.lower > 0,
                f"expectancy {self.oos_stats.expectancy:+.3f}R with a 95% "
                f"{self.oos_ci.method} lower bound of {self.oos_ci.lower:+.3f}R",
            ),
            Criterion(
                "deflated_sharpe",
                self.dsr.dsr >= DSR_TARGET,
                f"DSR {self.dsr.dsr:.3f} at {self.dsr.n_trials} counted trials "
                f"(effective {self.dsr.n_trials_effective}), SR_ann "
                f"{self.dsr.sharpe_annual:.2f}, SR* {self.dsr.sr_star_daily:.4f}",
            ),
            Criterion(
                "tie_break_share",
                self.tie_break_share < TIE_BREAK_WARN,
                f"{self.tie_break_share:.1%} of trades were decided by the same-M1-bar "
                "SL/TP tie-break. Above 5% the results are substantially that assumption, "
                "and the optimistic-rule sensitivity run must also pass.",
            ),
            Criterion(
                "financing_not_material",
                self.rollover_share < ROLLOVER_WARN,
                f"{self.rollover_share:.1%} of trades were held through 17:00 NY. Above "
                "10% overnight financing must be modelled before the results are trusted.",
                blocking=False,
            ),
            Criterion(
                "calendar_point_in_time",
                self.calendar_point_in_time,
                "the economic calendar supplied publication timestamps, so the news gate "
                "is point-in-time. Without it the run is marked and the gate is reported "
                "both on and off.",
                blocking=False,
            ),
            Criterion(
                "seal_intact",
                not self.seal_breaches,
                "Tier 2 was not read before Tier 1 was committed"
                if not self.seal_breaches
                else f"SEAL BREACH: {'; '.join(self.seal_breaches)}",
            ),
        ]
        return out

    @property
    def passes(self) -> bool:
        return all(c.passed for c in self.criteria() if c.blocking)

    # -- rendering ----------------------------------------------------------
    def to_markdown(self) -> str:
        lines: list[str] = [
            "# Aurion validation report",
            "",
            f"> {BACKTEST_DISCLAIMER}",
            "",
            f"Generated {self.generated_at.isoformat()} · git `{self.git_sha[:12]}` · "
            f"config `{self.config_hash}`",
            "",
        ]
        if self.branch is not None:
            lines += [
                "## Selection branch",
                "",
                f"**{self.branch.branch}** — {self.branch.rationale}",
                "",
                f"Determined at {self.branch.decided_at.isoformat()} from a measured "
                f"SR_ann of {self.branch.measured_sharpe_annual:.2f} over "
                f"{self.branch.oos_days} OOS days at a trial correlation of "
                f"{self.branch.trial_correlation_ratio:.2f}, **before any arm ran**.",
                "",
            ]
            if self.branch.branch == "A_no_selection":
                lines += [
                    "> No module was selected on evidence. Inclusion rests on prior "
                    "plausibility, and the ablation below is description only.",
                    "",
                ]

        lines += ["## Acceptance criteria", "", "| Criterion | Result | Detail |", "|---|---|---|"]
        for c in self.criteria():
            mark = "pass" if c.passed else ("FAIL" if c.blocking else "warn")
            lines.append(f"| `{c.name}` | {mark} | {c.detail} |")
        lines += [
            "",
            f"**Overall: {'PASS' if self.passes else 'FAIL'}** — "
            f"{'the [PAPER] tag may be removed' if self.passes else 'the [PAPER] tag stays'}.",
            "",
            "## Out-of-sample performance",
            "",
            "Primary set: the concatenation of all walk-forward test blocks. The 12-month "
            "holdout is a degradation check with its power stated, not a significance gate.",
            "",
        ]
        for key, value in self.oos_stats.as_dict().items():
            lines.append(f"- `{key}`: {value:.4f}")
        lines += [
            "",
            f"95% CI ({self.oos_ci.method}): [{self.oos_ci.lower:+.4f}, "
            f"{self.oos_ci.upper:+.4f}]",
            "",
            f"Walk-forward variance inflation {self.walkforward_inflation:.2f}x, so the "
            f"effective N is {self.effective_n:.0f} against a nominal {self.oos_stats.n}. "
            "Nominal trade count is an upper bound on independent evidence.",
            "",
        ]
        if self.holdout_stats is not None:
            lines += [
                "### 12-month holdout (degradation check)",
                "",
                f"{self.holdout_stats.n} trades, expectancy "
                f"{self.holdout_stats.expectancy:+.3f}R. At this count the holdout cannot "
                "establish significance and is not reported as doing so.",
                "",
            ]

        lines += ["## Deflated Sharpe", "", "| Quantity | Value |", "|---|---|"]
        for key, value in self.dsr.as_dict().items():
            lines.append(f"| `{key}` | {value:.6f} |")
        lines += [
            "",
            "Every input is shown so the number can be recomputed by hand. The headline "
            "uses the raw trial count, which is the more conservative of the two.",
            "",
        ]

        if self.arms:
            lines += [
                "## Ablation",
                "",
                "Paired comparison under common random numbers, restricted to the affected "
                "subset. Read the per-affected-trade MDE, not the overall one: pairing "
                "shrinks the detectable overall effect as `f` shrinks, but the per-trade "
                "effect required grows as `1/sqrt(f)`.",
                "",
                "| Arm | delta (R) | f | sigma_d | MDE overall | MDE / affected | Verdict |",
                "|---|---|---|---|---|---|---|",
            ]
            for a in self.arms:
                lines.append(
                    f"| `{a.arm}`{'' if a.selection_eligible else ' *(descriptive)*'} "
                    f"| {a.delta:+.3f} | {a.affected_frac:.2f} | {a.sigma_d:.2f} "
                    f"| {a.mde_overall:.3f} | {a.mde_per_affected:.3f} | {a.verdict} |"
                )
            unresolved = [a.arm for a in self.arms if not a.resolvable]
            if unresolved:
                lines += [
                    "",
                    f"**Unresolved at this sample size: {', '.join(unresolved)}.** These "
                    "are not scored in either direction. Counting an unresolved arm as "
                    "'contributes nothing' would be self-confirming.",
                    "",
                ]

        if self.funnel is not None:
            lines += ["## Candidate funnel", "", "| Step | Rejected | Share |", "|---|---|---|"]
            total = max(self.funnel.evaluated, 1)
            for step, n in sorted(self.funnel.by_step.items(), key=lambda kv: -kv[1]):
                lines.append(f"| `{step}` | {n} | {n / total:.1%} |")
            lines += [
                f"| **emitted** | {self.funnel.emitted} | {self.funnel.emitted / total:.1%} |",
                "",
            ]
            heaviest = self.funnel.heaviest_filter()
            if heaviest is not None:
                lines += [
                    f"Heaviest filter: `{heaviest[0]}` at {heaviest[1]} rejections "
                    f"({heaviest[1] / total:.1%}). A step that kills most candidates is the "
                    "real model whatever the rest of the pipeline does.",
                    "",
                ]

        if self.monte_carlo_block:
            lines += [
                "## Monte Carlo drawdown",
                "",
                "| Statistic | Block bootstrap | i.i.d. |",
                "|---|---|---|",
            ]
            for key in sorted(self.monte_carlo_block):
                lines.append(
                    f"| `{key}` | {self.monte_carlo_block[key]:.4f} "
                    f"| {self.monte_carlo_iid.get(key, float('nan')):.4f} |"
                )
            lines += [
                "",
                "The block figures are the headline: i.i.d. resampling understates drawdown "
                "for any system with serially correlated outcomes.",
                "",
            ]

        for name, cells in self.breakouts.items():
            lines += [f"## By {name}", "", "| Cell | n | Expectancy | Note |", "|---|---|---|---|"]
            for cell in cells:
                expectancy = (
                    f"{cell.stats.expectancy:+.3f}R" if cell.stats is not None else "—"
                )
                lines.append(f"| {cell.key} | {cell.n} | {expectancy} | {cell.note} |")
            lines.append("")

        if self.notes:
            lines += ["## Notes", ""] + [f"- {n}" for n in self.notes] + [""]

        lines += ["---", "", f"_{BACKTEST_DISCLAIMER}_", ""]
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(
            {
                "disclaimer": BACKTEST_DISCLAIMER,
                "generated_at": self.generated_at.isoformat(),
                "git_sha": self.git_sha,
                "config_hash": self.config_hash,
                "passes": self.passes,
                "criteria": [
                    {"name": c.name, "passed": c.passed, "blocking": c.blocking,
                     "detail": c.detail}
                    for c in self.criteria()
                ],
                "oos": self.oos_stats.as_dict(),
                "oos_ci": {
                    "lower": self.oos_ci.lower, "upper": self.oos_ci.upper,
                    "method": self.oos_ci.method,
                },
                "holdout": None if self.holdout_stats is None else self.holdout_stats.as_dict(),
                "dsr": self.dsr.as_dict(),
                "branch": None if self.branch is None else self.branch.as_dict(),
                "arms": [a.as_dict() for a in self.arms],
                "funnel": None
                if self.funnel is None
                else {
                    "evaluated": self.funnel.evaluated,
                    "emitted": self.funnel.emitted,
                    "by_step": self.funnel.by_step,
                    "by_reason": self.funnel.by_reason,
                },
                "monte_carlo_block": self.monte_carlo_block,
                "monte_carlo_iid": self.monte_carlo_iid,
                "tie_break_share": self.tie_break_share,
                "rollover_share": self.rollover_share,
                "walkforward_inflation": self.walkforward_inflation,
                "effective_n": self.effective_n,
                "trials_counted": self.trials_counted,
                "calendar_point_in_time": self.calendar_point_in_time,
                "seal_breaches": list(self.seal_breaches),
                "notes": self.notes,
            },
            indent=2,
            sort_keys=True,
            default=str,
        )

    def write(self, directory: str | Path) -> tuple[Path, Path]:
        base = Path(directory)
        base.mkdir(parents=True, exist_ok=True)
        stem = f"validation_{self.git_sha[:12]}_{self.config_hash}"
        md = base / f"{stem}.md"
        js = base / f"{stem}.json"
        md.write_text(self.to_markdown(), encoding="utf-8")
        js.write_text(self.to_json(), encoding="utf-8")
        return md, js


def monte_carlo_sections(
    returns: Sequence[float], paths: int = 10_000, seed: int = 0
) -> tuple[dict[str, float], dict[str, float]]:
    """Block and i.i.d. drawdown distributions."""
    from aurion.backtest.metrics import iid_bootstrap, stationary_block_bootstrap

    block = stationary_block_bootstrap(returns, paths=paths, seed=seed)
    iid = iid_bootstrap(returns, paths=paths, seed=seed)
    return drawdown_distribution(block), drawdown_distribution(iid)
