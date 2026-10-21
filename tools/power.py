"""Power and capacity calculator for the Aurion validation design.

Answers three questions that the brief's module list and parameter budget depend on:

1. What per-trade effect can an ablation arm detect, under an independent-arm
   comparison and under a paired comparison with common random numbers restricted
   to the affected subset?
2. How many trials (parameter settings + ablation arms taken together) can the
   deflated Sharpe ratio absorb before a plausible observed Sharpe can no longer
   clear the DSR threshold?
3. How many completed trades does the configured selectivity plausibly yield?

Every number this prints is a function of its inputs. Nothing here is measured;
the pilot described in docs/07-power-and-capacity.md supplies the measurements.

Stdlib only (statistics.NormalDist), so it runs anywhere with Python 3.11+.

Usage:
    python3 tools/power.py                # all tables at default assumptions
    python3 tools/power.py --help
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from statistics import NormalDist

N01 = NormalDist()
EULER_MASCHERONI = 0.5772156649015329
TRADING_DAYS_PER_YEAR = 252


# --------------------------------------------------------------------------
# Minimum detectable effect
# --------------------------------------------------------------------------
def _z_pair(alpha: float, power: float, n_tests: int) -> float:
    """Critical + power quantiles under a Bonferroni-equivalent family correction.

    Holm is step-down, so its *smallest* threshold is alpha/n_tests; using that
    throughout is the conservative end of Holm.
    """
    z_crit = N01.inv_cdf(1.0 - alpha / (2.0 * n_tests))
    z_power = N01.inv_cdf(power)
    return z_crit + z_power


def mde_unpaired(
    n_trades: int, sigma_r: float, alpha: float = 0.05, power: float = 0.80, n_tests: int = 1
) -> float:
    """MDE on expectancy for two independent trade populations of n_trades each."""
    se = sigma_r * math.sqrt(2.0 / n_trades)
    return _z_pair(alpha, power, n_tests) * se


def mde_paired_crn(
    n_trades: int,
    affected_frac: float,
    sigma_d_affected: float,
    alpha: float = 0.05,
    power: float = 0.80,
    n_tests: int = 1,
) -> float:
    """MDE on *overall* expectancy for a paired comparison under common random numbers.

    The module changes the outcome on a fraction f of candidates and leaves the rest
    byte-identical. The per-trade difference is zero off the affected subset, so

        Var(mean difference over all N) = f * sigma_d_affected^2 / N
        SE                              = sigma_d_affected * sqrt(f / N)

    Pairing removes all variance the module did not cause, so the MDE on *overall*
    expectancy shrinks as f shrinks. That is not a licence for rare modules: the
    per-affected-trade effect required scales as MDE/f = z*sigma_d/sqrt(f*N), which
    GROWS as f shrinks. A module firing on 5% of candidates needs an implausible
    per-trade effect to be resolvable at all. High-f modules are the resolvable ones.
    """
    se = sigma_d_affected * math.sqrt(affected_frac / n_trades)
    return _z_pair(alpha, power, n_tests) * se


# --------------------------------------------------------------------------
# Deflated Sharpe ratio (Bailey & Lopez de Prado)
# --------------------------------------------------------------------------
def expected_max_sharpe_factor(n_trials: int) -> float:
    """g(N): expected maximum of N standard-normal Sharpe estimates, in SD units.

        g(N) = (1 - gamma) * Phi^-1(1 - 1/N) + gamma * Phi^-1(1 - 1/(N*e))
    """
    if n_trials < 2:
        return float("-inf")  # DSR degenerates to PSR(0) with a single trial
    return (1.0 - EULER_MASCHERONI) * N01.inv_cdf(1.0 - 1.0 / n_trials) + (
        EULER_MASCHERONI * N01.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    )


def psr(
    sharpe_daily: float, sr_star_daily: float, n_obs: int, skew: float = 0.0, kurtosis: float = 3.0
) -> float:
    """Probabilistic Sharpe ratio. kurtosis is non-excess."""
    denom = math.sqrt(
        max(1.0 - skew * sharpe_daily + ((kurtosis - 1.0) / 4.0) * sharpe_daily**2, 1e-12)
    )
    return N01.cdf((sharpe_daily - sr_star_daily) * math.sqrt(n_obs - 1) / denom)


def required_annual_sharpe(
    n_trials: int, n_obs: int, v_ratio: float = 1.0, dsr_target: float = 0.95
) -> float:
    """Observed annualised Sharpe needed for DSR >= dsr_target.

    v_ratio scales the SD of the Sharpe *across trials*:
      1.0  -> trials behave as independent draws under the null (worst case)
      <1.0 -> trials are correlated, so the selection penalty is milder.
              v_ratio must be measured from the realised trial-return matrix.
    """
    sqrt_v = v_ratio / math.sqrt(n_obs)
    g = expected_max_sharpe_factor(n_trials)
    sr_star = 0.0 if g == float("-inf") else sqrt_v * g
    needed_daily = sr_star + N01.inv_cdf(dsr_target) / math.sqrt(n_obs - 1)
    return needed_daily * math.sqrt(TRADING_DAYS_PER_YEAR)


def max_trials(
    n_obs: int,
    attainable_annual_sharpe: float,
    v_ratio: float = 1.0,
    dsr_target: float = 0.95,
    cap: int = 100_000,
) -> int:
    """Largest trial count at which DSR >= target is still attainable."""
    best = 0
    for n in range(1, cap + 1):
        if required_annual_sharpe(n, n_obs, v_ratio, dsr_target) <= attainable_annual_sharpe:
            best = n
        else:
            break
    return best


def n_required_paired(
    mde_target: float,
    affected_frac: float,
    sigma_d_affected: float,
    alpha: float = 0.05,
    power: float = 0.80,
    n_tests: int = 1,
) -> int:
    """Trades needed for a paired-CRN comparison to resolve mde_target.

    Inverts mde_paired_crn:  N = (z * sigma_d)^2 * f / MDE^2
    """
    z = _z_pair(alpha, power, n_tests)
    return math.ceil((z * sigma_d_affected) ** 2 * affected_frac / mde_target**2)


# --------------------------------------------------------------------------
# Trade-count projection
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Funnel:
    """Transparent candidate funnel. Every factor is an assumption, not a measurement."""

    sweeps_per_day_in_window: float = 1.5
    p_mss_given_sweep: float = 0.33
    p_htf_bias_agrees: float = 0.55
    p_htf_array_in_range: float = 0.50
    p_target_at_min_rr: float = 0.50
    p_spread_ok: float = 0.90
    p_not_news_blocked: float = 0.93
    p_survives_dedupe: float = 0.80
    trading_days_per_year: int = 252

    def per_instrument_per_year(self) -> float:
        gate = (
            self.p_htf_bias_agrees
            * self.p_htf_array_in_range
            * self.p_target_at_min_rr
            * self.p_spread_ok
            * self.p_not_news_blocked
            * self.p_survives_dedupe
        )
        per_day = self.sweeps_per_day_in_window * self.p_mss_given_sweep * gate
        return per_day * self.trading_days_per_year

    def total(self, instruments: int, years: float) -> float:
        return self.per_instrument_per_year() * instruments * years


# --------------------------------------------------------------------------
# Walk-forward fold overlap -> effective N
# --------------------------------------------------------------------------
def fold_overlap_inflation(n_folds: int, train_months: int, step_months: int) -> float:
    """Variance inflation for a statistic averaged over OVERLAPPING walk-forward folds.

    Adjacent folds share (train - j*step)/train of their training data, so their
    performance estimates are correlated. For a mean of m correlated observations:

        Var(mean) = (sigma^2 / m) * [ 1 + 2 * sum_j (1 - j/m) * rho_j ]

    with rho_j taken as the training-window overlap fraction -- full transmission of
    overlap into estimate correlation, the conservative end.

    Applies to statistics that depend on the FITTING PROCEDURE (expectancy of the refit
    strategy, fold-level Sharpe). It does NOT apply to a paired-CRN ablation difference,
    where both arms share the same folds and the overlap largely cancels; there the
    inflation is estimated from the realised difference series instead.
    """
    j_max = min(n_folds - 1, math.ceil(train_months / step_months) - 1)
    acc = sum(
        (1.0 - j / n_folds) * max(0.0, 1.0 - j * step_months / train_months)
        for j in range(1, j_max + 1)
    )
    return 1.0 + 2.0 * acc


def n_folds_available(
    dev_months: int, train_months: int, test_months: int, step_months: int
) -> int:
    span = dev_months - train_months - test_months
    return 0 if span < 0 else span // step_months + 1


# --------------------------------------------------------------------------
# Funnel lever elasticity
# --------------------------------------------------------------------------
def p_target_at_rr(rr: float, p_at_2: float = 0.50) -> float:
    """P(a valid target exists at >= rr R) under an exponential tail.

    Calibrated so p(2.0) = p_at_2, i.e. p(rr) = exp(-lam*(rr-1)) with lam = -ln(p_at_2).
    ASSUMED FUNCTIONAL FORM. The pilot measures the achievable-R CDF directly by recording
    the full target ladder per candidate, which replaces this with the exact curve at no
    extra cost and no extra trial.
    """
    lam = -math.log(p_at_2)
    return min(1.0, math.exp(-lam * (rr - 1.0)))


def table_levers(base_n: float, oos_days: int, dsr_target: float, trials: int) -> str:
    base = Funnel()
    rr_mult = p_target_at_rr(1.5) / p_target_at_rr(2.0)
    bias_mult = 1.0 / base.p_htf_bias_agrees
    out = [
        "LEVER ELASTICITY - multiplier on out-of-sample trade count",
        f"  baseline N={base_n:.0f}, T={oos_days} OOS days, {trials} trials"
        f" -> required SR_ann {required_annual_sharpe(trials, oos_days, 1.0, dsr_target):.2f}",
        "",
        "  lever                           | N mult | new N | trial cost | money/time cost",
        "  --------------------------------|--------|-------|------------|-----------------",
    ]
    rows = [
        ("rr_min 2.0 -> 1.5", rr_mult, "+1", "none"),
        ("drop W1/D1 concordance", bias_mult, "+1", "none"),
        ("instruments 6 -> 12", 2.0, "0", "adapter work, worse spreads"),
        ("history 5y -> 10y", 2.25, "0 (raises budget)", "money + calendar"),
    ]
    for name, mult, tcost, cost in rows:
        out.append(
            f"  {name:<31} | {mult:>6.2f} | {base_n * mult:>5.0f} | {tcost:<10} | {cost}"
        )
    combined = rr_mult * bias_mult * 2.0 * 2.25
    out.append(
        f"  {'all four together':<31} | {combined:>6.2f} | {base_n * combined:>5.0f} |"
        f" {'+2':<10} | money + calendar"
    )
    out += [
        "",
        "  Asymmetry that matters: more DATA costs money but ZERO trials, and RAISES the",
        "  affordable trial count by increasing T. Loosening a GATE costs a trial but no",
        "  money. They are not interchangeable levers.",
        "",
        "  required SR_ann at a fixed trial count, by OOS length:",
        "    OOS days  | trials=2 | trials=4 | trials=8 | trials=16",
        "    ----------|----------|----------|----------|----------",
    ]
    for t, lbl in ((1260, "1260 (5y)"), (1764, "1764 (7y)"), (2268, "2268 (9y)")):
        cells = " | ".join(
            f"{required_annual_sharpe(n, t, 1.0, dsr_target):>8.2f}" for n in (2, 4, 8, 16)
        )
        out.append(f"    {lbl:<9} | {cells}")
    out += [
        "",
        "  P(target at >= rr), assumed exponential tail:",
        "    rr  | p      | N mult vs rr=2 | break-even p_hat at R1=rr",
        "    ----|--------|----------------|--------------------------",
    ]
    for rr in (1.5, 2.0, 2.5, 3.0):
        p = p_target_at_rr(rr)
        out.append(f"    {rr:<3} | {p:>6.3f} | {p / 0.5:>14.2f} | {1.0 / (1.0 + rr):>25.3f}")
    out += [
        "",
        "  CAUTION on rr_min, and it cuts against prediction #9. Lowering the floor raises",
        "  N by ~1.41x but lowers R1, while the emission gate is E[R] = p*R1 - (1-p). The",
        "  break-even win rate rises from 0.333 at R1=2.0 to 0.400 at R1=1.5. More",
        "  candidates clear the R:R floor and FEWER clear the expectancy gate. Net sign on",
        "  trade count is positive; net sign on expectancy is NOT predictable a priori.",
    ]
    return "\n".join(out)


def table_effective_n(
    trades_per_oos_month: float, dev_months: int = 48, baseline_train: int = 24
) -> str:
    """Effective N over the walk-forward design grid.

    CORRECTED: nominal N scales with total test months (folds x step, when test = step so
    the test blocks tile contiguously), so it cannot be held fixed while the step varies.
    Overlapping more does not buy effective sample size -- the inflation eats the gain.
    """
    out = [
        "EFFECTIVE N over the walk-forward design grid",
        f"  dev window {dev_months}m; test = step so OOS blocks tile contiguously;",
        f"  {trades_per_oos_month:.1f} trades per OOS month (from the base-case projection)",
        "",
        "  train | step | folds | OOS mo | nominal N | inflation | N_eff | train events",
        "  ------|------|-------|--------|-----------|-----------|-------|-------------",
    ]
    rows = []
    for train in (12, 18, 24, 36):
        for step in (3, 6, 12):
            m = n_folds_available(dev_months, train, step, step)
            if m < 2:
                continue
            oos_months = m * step
            nominal = oos_months * trades_per_oos_month
            infl = fold_overlap_inflation(m, train, step)
            n_eff = nominal / infl
            rel_events = train / baseline_train
            rows.append((train, step, m, oos_months, nominal, infl, n_eff, rel_events))
            out.append(
                f"  {train:>5} | {step:>4} | {m:>5} | {oos_months:>6} | {nominal:>9.0f} |"
                f" {infl:>9.2f} | {n_eff:>5.0f} | {rel_events:>11.2f}x"
            )
    best = max(rows, key=lambda r: r[6])
    baseline = next(r for r in rows if r[0] == 24 and r[1] == 3)
    out += [
        "",
        f"  Highest N_eff: train={best[0]}m, step={best[1]}m -> N_eff {best[6]:.0f}"
        f" (vs {baseline[6]:.0f} at train=24m/step=3m)",
        "",
        "  Two things this shows that the original design got wrong:",
        "   1. A SHORTER training window raises N_eff twice over - more folds (so more OOS",
        "      months) and less overlap between them. It costs training events, which trades",
        "      against the 20-events-per-feature floor.",
        "   2. A SMALLER step does NOT raise N_eff. It raises nominal N and the inflation in",
        "      near-equal measure. Overlapping harder buys nothing.",
        "",
        "  Scope of the adjustment: it applies to FIT-DEPENDENT statistics (expectancy of",
        "  the refit strategy, fold-level Sharpe). For a paired-CRN ablation difference both",
        "  arms share the same folds and the same overlap, so it largely cancels and the",
        "  residual is estimated from the realised difference series. For the DSR, T is a",
        "  count of daily observations and the relevant correction is return autocorrelation,",
        "  not fold overlap.",
        "",
        "  Reported CI = the WIDER of a trade-level stationary block bootstrap and a",
        "  fold-cluster bootstrap. Nominal N is an UPPER BOUND on independent evidence.",
    ]
    return "\n".join(out)


# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------
def table_mde(
    sigma_r: float, sigma_d: float, affected_fracs: tuple[float, ...], n_tests: int
) -> str:
    out = [
        f"MDE on expectancy (R per trade), alpha=0.05 two-sided, power=0.80, "
        f"family correction over {n_tests} tests",
        f"  sigma_R = {sigma_r}  (per-trade R dispersion)",
        f"  sigma_d = {sigma_d}  (paired difference on the affected subset)",
        "",
        "  N trades | unpaired | " + " | ".join(f"paired f={f:<4g}" for f in affected_fracs),
        "  ---------|----------|" + "|".join("-" * 14 for _ in affected_fracs),
    ]
    for n in (250, 500, 1000, 2000):
        cells = [
            f"{mde_paired_crn(n, f, sigma_d, n_tests=n_tests):>13.3f}" for f in affected_fracs
        ]
        out.append(
            f"  {n:>8} | {mde_unpaired(n, sigma_r, n_tests=n_tests):>8.3f} | " + " |".join(cells)
        )
    return "\n".join(out)


def table_trial_budget(v_ratios: tuple[float, ...], dsr_target: float) -> str:
    out = [
        f"Trial budget: largest N for which DSR >= {dsr_target} remains attainable",
        "  rows = out-of-sample length; cols = attainable annualised Sharpe",
        "",
    ]
    sharpes = (0.8, 1.0, 1.2, 1.5, 2.0)
    for v in v_ratios:
        label = "independent trials (worst case)" if v == 1.0 else f"trial-correlation v_ratio={v}"
        out.append(f"  -- {label} --")
        out.append("  OOS days      | " + " | ".join(f"SR={s:<4g}" for s in sharpes))
        out.append("  --------------|" + "|".join("-" * 9 for _ in sharpes))
        for t, name in ((252, "252  (1y)"), (756, "756  (3y)"), (1260, "1260 (5y)")):
            cells = [f"{max_trials(t, s, v, dsr_target):>8}" for s in sharpes]
            out.append(f"  {name:<13} | " + " |".join(cells))
        out.append("")
    return "\n".join(out)


def table_required_sharpe(n_obs: int, v_ratio: float, dsr_target: float) -> str:
    out = [
        f"Observed annualised Sharpe required for DSR >= {dsr_target} "
        f"(T={n_obs} OOS days, v_ratio={v_ratio})",
        "",
        "  trials N | required SR_ann",
        "  ---------|----------------",
    ]
    for n in (1, 2, 4, 8, 13, 16, 32, 64, 128, 256):
        out.append(f"  {n:>8} | {required_annual_sharpe(n, n_obs, v_ratio, dsr_target):>15.2f}")
    return "\n".join(out)


def table_trades(instruments: int, years: float) -> str:
    base = Funnel()
    optimistic = Funnel(
        sweeps_per_day_in_window=3.0, p_mss_given_sweep=0.40, p_target_at_min_rr=0.65
    )
    pessimistic = Funnel(
        sweeps_per_day_in_window=1.0, p_mss_given_sweep=0.25, p_target_at_min_rr=0.40
    )
    out = [
        f"Projected completed trades ({instruments} instruments, {years:g} years of history)",
        "  ASSUMPTION ARITHMETIC, NOT A MEASUREMENT. The pilot replaces every factor.",
        "",
        "  scenario    | /instr/yr | total   | 12m holdout | concat WF OOS (yrs-1)",
        "  ------------|-----------|---------|-------------|----------------------",
    ]
    for name, f in (("pessimistic", pessimistic), ("base", base), ("optimistic", optimistic)):
        per = f.per_instrument_per_year()
        total = f.total(instruments, years)
        holdout = per * instruments
        concat = per * instruments * max(years - 1.0, 0.0)
        out.append(
            f"  {name:<11} | {per:>9.1f} | {total:>7.0f} | {holdout:>11.0f} | {concat:>21.0f}"
        )
    return "\n".join(out)


def table_capacity(
    n_trades: int, sigma_d: float, n_tests: int, oos_days: int, dsr_target: float
) -> str:
    """The verdict: what this sample resolves, and how many trials it can afford."""
    out = [
        f"CAPACITY VERDICT at N={n_trades} completed out-of-sample trades",
        f"  (sigma_d={sigma_d}, family of {n_tests} tests, T={oos_days} OOS days)",
        "",
        "  (a) What a paired-CRN arm can resolve, by affected fraction f:",
        "      f     | MDE (R) | per-trade effect it implies on the affected subset",
        "      ------|---------|---------------------------------------------------",
    ]
    for f in (0.05, 0.10, 0.25, 0.50, 1.00):
        mde = mde_paired_crn(n_trades, f, sigma_d, n_tests=n_tests)
        out.append(f"      {f:<5g} | {mde:>7.3f} | {mde / f:>7.3f} R per affected trade")
    out += [
        "",
        "  (b) Trades needed to resolve a target overall effect (f=0.5):",
        "      MDE target | trades needed",
        "      -----------|--------------",
    ]
    for t in (0.20, 0.15, 0.10, 0.05, 0.03):
        out.append(f"      {t:<10g} | {n_required_paired(t, 0.5, sigma_d, n_tests=n_tests):>13}")
    out += [
        "",
        "  (c) Trial budget -> required Sharpe (independent trials, worst case):",
        "      trials | required SR_ann | e.g. composition",
        "      -------|-----------------|------------------",
    ]
    for n, comp in (
        (4, "1 param setting + 3 arms"),
        (6, "1 param setting + 5 arms"),
        (8, "3 param settings + 5 arms"),
        (12, "3 param settings + 9 arms"),
        (20, "5 param settings + 15 arms"),
    ):
        out.append(
            f"      {n:>6} | {required_annual_sharpe(n, oos_days, 1.0, dsr_target):>15.2f} | {comp}"
        )
    out.append("")
    out.append("  Exchange rate: each doubling of the trial count costs roughly")
    r8 = required_annual_sharpe(8, oos_days, 1.0, dsr_target)
    r16 = required_annual_sharpe(16, oos_days, 1.0, dsr_target)
    out.append(f"  {r16 - r8:.3f} of annualised Sharpe (measured 8 -> 16 trials).")
    return "\n".join(out)


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--sigma-r", type=float, default=1.4, help="per-trade R dispersion")
    p.add_argument(
        "--sigma-d", type=float, default=1.5, help="paired diff sd on the affected subset"
    )
    p.add_argument("--n-tests", type=int, default=6, help="size of the ablation family")
    p.add_argument("--instruments", type=int, default=6)
    p.add_argument("--years", type=float, default=5.0)
    p.add_argument("--dsr-target", type=float, default=0.95)
    p.add_argument("--oos-days", type=int, default=1260)
    p.add_argument("--v-ratio", type=float, default=1.0)
    p.add_argument("--capacity-n", type=int, default=276, help="projected OOS trade count")
    a = p.parse_args()

    print(table_mde(a.sigma_r, a.sigma_d, (0.05, 0.10, 0.25, 0.50), a.n_tests))
    print()
    print(table_trial_budget((1.0, 0.5), a.dsr_target))
    print(table_required_sharpe(a.oos_days, a.v_ratio, a.dsr_target))
    print()
    print(table_trades(a.instruments, a.years))
    print()
    print(table_levers(a.capacity_n, a.oos_days, a.dsr_target, 8))
    print()
    print(table_effective_n(a.capacity_n / 24.0))
    print()
    print(
        table_capacity(
            a.capacity_n, a.sigma_d, a.n_tests, a.oos_days, a.dsr_target
        )
    )


if __name__ == "__main__":
    main()
