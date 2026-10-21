"""The feature vector: ordered, versioned, immutable. `docs/05-data-contracts.md` §4.

Three rules, each preventing a specific failure:

1. **A released ``FEATURE_SCHEMA_VERSION`` is immutable.** Adding, removing or redefining a
   feature creates a new version and invalidates every model fitted on the old one.
2. **Every model artifact records the version it was fitted on**, and the runtime refuses
   to apply a model to a different version. It does not coerce or pad.
3. **Features are computed in exactly one place** (`features/build.py`). No module computes
   a feature inline for a gate; gates consume this same vector. That is what prevents the
   most insidious backtest/live divergence: the same quantity computed twice with slightly
   different edge handling.

Tags matter operationally. The ablation removes whole tag groups, and ``volume_proxy`` can
be dropped wholesale if the broker's tick aggregation changes (§0.5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

FEATURE_SCHEMA_VERSION = 1


class Tag(StrEnum):
    TIME = "time"
    STRUCTURE = "structure"
    ICT = "ict"
    LIQUIDITY = "liquidity"
    PD = "pd"
    SR = "sr"
    QUARTERLY = "quarterly"
    MMXM = "mmxm"
    REGIME = "regime"
    VOLUME_PROXY = "volume_proxy"
    MACRO = "macro"
    EXPOSURE = "exposure"


#: Tier 1 is selection-eligible; Tier 2 is built and measured but frozen (`docs/07` §7).
TIER1_TAGS: frozenset[Tag] = frozenset(
    {Tag.TIME, Tag.ICT, Tag.LIQUIDITY, Tag.PD, Tag.SR, Tag.REGIME, Tag.STRUCTURE}
)
TIER2_TAGS: frozenset[Tag] = frozenset({Tag.QUARTERLY, Tag.MMXM, Tag.VOLUME_PROXY})


@dataclass(frozen=True, slots=True)
class FeatureSpec:
    name: str
    tag: Tag
    required: bool = True
    description: str = ""


def _spec(name: str, tag: Tag, required: bool = True, description: str = "") -> FeatureSpec:
    return FeatureSpec(name, tag, required, description)


#: Ordered. The index of a feature is part of the schema contract.
FEATURES: tuple[FeatureSpec, ...] = (
    # -- time / session ----------------------------------------------------
    _spec("session_minutes_in", Tag.TIME, True, "minutes into the active signal window"),
    _spec("session_is_london", Tag.TIME, True),
    _spec("session_is_ny_am", Tag.TIME, True),
    _spec("session_is_silver_bullet", Tag.TIME, True),
    _spec("weekday", Tag.TIME, True),
    # -- structure ---------------------------------------------------------
    _spec("trend_d1", Tag.STRUCTURE, True, "+1 bullish, -1 bearish, 0 ranging"),
    _spec("trend_h4", Tag.STRUCTURE, True),
    _spec("trend_h1", Tag.STRUCTURE, True),
    _spec("trend_entry_tf", Tag.STRUCTURE, True),
    _spec("bars_since_break", Tag.STRUCTURE, True),
    _spec("break_is_mss", Tag.STRUCTURE, True),
    _spec("displacement_body_atr", Tag.STRUCTURE, True),
    # -- ICT arrays --------------------------------------------------------
    _spec("dist_nearest_fvg_atr", Tag.ICT, True),
    _spec("nearest_fvg_fill", Tag.ICT, True),
    _spec("nearest_fvg_size_atr", Tag.ICT, True),
    _spec("dist_nearest_ob_atr", Tag.ICT, True),
    _spec("nearest_ob_fill", Tag.ICT, True),
    _spec("in_fvg", Tag.ICT, True),
    _spec("in_ob", Tag.ICT, True),
    _spec("htf_array_dist_atr", Tag.ICT, True),
    _spec("n_available_arrays", Tag.ICT, True),
    _spec("dist_nearest_bpr_atr", Tag.ICT, False),
    # -- liquidity ---------------------------------------------------------
    _spec("sweep_depth_atr", Tag.LIQUIDITY, True),
    _spec("bars_since_sweep", Tag.LIQUIDITY, True),
    _spec("sweep_bars_to_reclaim", Tag.LIQUIDITY, True),
    _spec("nearest_pool_dist_atr", Tag.LIQUIDITY, True),
    _spec("nearest_pool_touches", Tag.LIQUIDITY, True),
    _spec("dist_pdh_atr", Tag.LIQUIDITY, False),
    _spec("dist_pdl_atr", Tag.LIQUIDITY, False),
    _spec("dist_asian_high_atr", Tag.LIQUIDITY, False),
    _spec("dist_asian_low_atr", Tag.LIQUIDITY, False),
    # -- premium / discount ------------------------------------------------
    _spec("range_position", Tag.PD, True),
    _spec("in_ote", Tag.PD, True),
    _spec("dist_equilibrium_atr", Tag.PD, True),
    _spec("range_span_atr", Tag.PD, True),
    # -- support / resistance ----------------------------------------------
    _spec("dist_nearest_level_atr", Tag.SR, True),
    _spec("nearest_level_strength", Tag.SR, True),
    _spec("dist_nearest_pivot_atr", Tag.SR, False),
    _spec("dist_round_tier1_atr", Tag.SR, False),
    # -- quarterly (Tier 2) ------------------------------------------------
    _spec("quarter_index_daily", Tag.QUARTERLY, False),
    _spec("quarter_elapsed_frac", Tag.QUARTERLY, False),
    _spec("amdx_accumulation", Tag.QUARTERLY, False),
    _spec("amdx_manipulation", Tag.QUARTERLY, False),
    _spec("amdx_distribution", Tag.QUARTERLY, False),
    _spec("amdx_continuation", Tag.QUARTERLY, False),
    _spec("amdx_reversal", Tag.QUARTERLY, False),
    _spec("to_rel", Tag.QUARTERLY, False),
    _spec("to_dist_atr", Tag.QUARTERLY, False),
    _spec("qt_align", Tag.QUARTERLY, False),
    # -- MMXM (Tier 2) -----------------------------------------------------
    _spec("mmxm_score", Tag.MMXM, False),
    _spec("mmxm_stage", Tag.MMXM, False),
    # -- regime ------------------------------------------------------------
    _spec("atr_pct", Tag.REGIME, True),
    _spec("rv_m15", Tag.REGIME, False),
    _spec("gk_m15", Tag.REGIME, False),
    _spec("adx", Tag.REGIME, True),
    _spec("hurst", Tag.REGIME, False, "standard error 0.05-0.10; expected to be ablated out"),
    _spec("vr_4_z", Tag.REGIME, False),
    # -- volume proxy (Tier 2) ---------------------------------------------
    _spec("vwap_z", Tag.VOLUME_PROXY, False),
    _spec("nearest_level_rejection", Tag.VOLUME_PROXY, False),
    # -- macro (daily) -----------------------------------------------------
    _spec("us10y_real_d", Tag.MACRO, False),
    _spec("us10y_real_chg20", Tag.MACRO, False),
    _spec("us02y_nominal_d", Tag.MACRO, False),
    _spec("dxy_official_chg1", Tag.MACRO, False),
    _spec("corr_xau_dxy", Tag.MACRO, False),
    # -- exposure ----------------------------------------------------------
    _spec("net_usd_exposure", Tag.EXPOSURE, True),
    _spec("usd_beta", Tag.EXPOSURE, False),
)

FEATURE_NAMES: tuple[str, ...] = tuple(f.name for f in FEATURES)
FEATURE_INDEX: dict[str, int] = {f.name: i for i, f in enumerate(FEATURES)}
REQUIRED_NAMES: tuple[str, ...] = tuple(f.name for f in FEATURES if f.required)


def names_for_tags(tags: frozenset[Tag]) -> tuple[str, ...]:
    return tuple(f.name for f in FEATURES if f.tag in tags)


def names_excluding_tags(tags: frozenset[Tag]) -> tuple[str, ...]:
    """Feature set for one ablation arm: the vector minus whole tag groups."""
    return tuple(f.name for f in FEATURES if f.tag not in tags)


@dataclass(frozen=True, slots=True)
class FeatureVector:
    """Persisted at decision time, never recomputed later.

    The training set and the audit trail are therefore the same object, which is what makes
    a stored candidate replayable against a model fitted months afterwards.
    """

    version: int
    instrument: str
    as_of: datetime
    values: dict[str, float] = field(default_factory=dict)

    @property
    def missing_required(self) -> tuple[str, ...]:
        return tuple(n for n in REQUIRED_NAMES if n not in self.values)

    @property
    def is_complete(self) -> bool:
        """A vector missing any required field cannot produce a signal (§3.4)."""
        return not self.missing_required

    def to_list(self, names: tuple[str, ...] = FEATURE_NAMES) -> list[float]:
        """Dense ordered vector. Absent optional features become 0.0, which is safe only
        because every *required* feature is checked first by `is_complete`."""
        return [self.values.get(n, 0.0) for n in names]

    def rejected_reason(self) -> str | None:
        missing = self.missing_required
        return f"warmup:{missing[0]}" if missing else None
