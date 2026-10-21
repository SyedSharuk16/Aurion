# Synthetic fixtures

Unit tests run on synthetic, hand-constructed series rather than on vendor data, for two
reasons: vendor market data is not redistributed in this repository
(`docs/06-open-questions.md` Q11), and a hand-constructed series has a *known* answer,
which a real one does not.

## Current state (Phase 2)

Phase 2 needs price action rather than annotated patterns, so series are built
programmatically by `tests/conftest.py::make_m1` and the `ramp_series` /
`pseudo_series` helpers. Both are deterministic, so a failure is reproducible.

The one fully hand-computed fixture is in `tests/unit/test_primitives.py::HAND`: four bars
whose true ranges are 2, 2 and 4, so Wilder's ATR(2) is exactly 2.0 then 3.0. It tests ATR
against arithmetic rather than against a reimplementation of the same formula.

## Phase 3

Each definition in `docs/02-definitions.md` §4-§10 gets a committed series plus a YAML
ground-truth file, with at least one positive case and one **near-miss negative**:

- a 3-candle gap one point *below* the `g x ATR` admission threshold (must not be an FVG)
- a pool penetration that never closes back inside (must not be a sweep)
- an equal-high plateau (must *not* be a swing -- §4.1 uses strict inequalities, and the
  plateau is captured as a liquidity pool instead, which is where it belongs)
- a BOS-sized close with a body below `k x ATR` (must not be displacement)
- an order block beyond `W_ob` bars from its break (must not be admitted)

The near-miss cases are the ones that matter. A test suite that only checks positive cases
proves a detector fires, not that it discriminates.
