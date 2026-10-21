"""Pydantic configuration models.

`docs/01-architecture.md` §9: validation happens at load time and the process refuses to
start on error rather than defaulting. Sections belonging to phases not yet built are typed
as raw mappings so that unknown *keys* inside them do not block loading, while unknown
*section names* still fail -- which catches the typo class that matters.
"""

from __future__ import annotations

from datetime import time
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aurion.core.timeframe import Anchor, TimeFrame, parse_anchor

Strict = ConfigDict(extra="forbid")
Frac = Annotated[float, Field(ge=0.0, le=1.0)]


class InstrumentCfg(BaseModel):
    model_config = Strict

    enabled: bool = True
    priority: int = 100
    point: float = Field(gt=0)
    contract_size: float = Field(gt=0)
    quote_ccy: str
    base_ccy: str
    sessions: list[str] = Field(default_factory=list)

    @property
    def pip_factor(self) -> int:
        """Points per pip: 10 for 5-digit FX and 3-digit JPY, 1 where pips are not used."""
        return 10 if self.point in (1e-05, 0.001) else 1


class UsdxProxyCfg(BaseModel):
    model_config = Strict

    enabled: bool = True
    weights: dict[str, float]
    epoch: str = "first_backtest_bar"

    @field_validator("weights")
    @classmethod
    def _non_empty(cls, v: dict[str, float]) -> dict[str, float]:
        if not v or any(w <= 0 for w in v.values()):
            raise ValueError("usdx_proxy.weights must be non-empty and positive")
        return v


class ArchiveCfg(BaseModel):
    model_config = Strict
    enabled: bool = True
    format: Literal["parquet"] = "parquet"
    path: str = "./data/archive"


class DataCfg(BaseModel):
    model_config = Strict

    base_timeframe: Literal["M1"] = "M1"
    timeframes: list[str]
    price_source: Literal["BA"] = "BA"
    m1_retention_days: int = Field(default=180, gt=0)
    min_constituents_frac: Frac = 0.5
    archive: ArchiveCfg = ArchiveCfg()
    anchors: dict[str, str]
    prev_period_day_convention: Literal["ny_midnight", "broker"] = "ny_midnight"


class AdapterCfg(BaseModel):
    model_config = Strict
    primary: str = "oanda"
    stream_idle_timeout_s: float = Field(default=15.0, gt=0)
    reconnect_backoff_s: list[float] = Field(
        default_factory=lambda: [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]
    )
    backfill_overlap_bars: int = Field(default=1, ge=0)


class WindowCfg(BaseModel):
    model_config = Strict
    start: time
    end: time
    emits_signals: bool
    tz: str = "America/New_York"
    #: Monday=0 .. Sunday=6. Empty means every weekday.
    weekdays: list[int] = Field(default_factory=list)

    @field_validator("weekdays")
    @classmethod
    def _valid_weekdays(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("weekdays must be in 0..6 (Monday=0)")
        return v


class SessionsCfg(BaseModel):
    model_config = Strict
    default_tz: str = "America/New_York"
    windows: dict[str, WindowCfg]


class RolloverCfg(BaseModel):
    model_config = Strict
    start: time
    end: time


class BlocksCfg(BaseModel):
    model_config = Strict
    friday_cutoff: time
    friday_force_close: time
    rollover: RolloverCfg
    weekend_gap_block_minutes: int = Field(ge=0)
    weekend_gap_atr_mult: float = Field(ge=0)


class SpreadCfg(BaseModel):
    model_config = Strict
    max_median_mult: float = Field(gt=0)
    bucket_minutes: int = Field(gt=0)
    bucket_window_obs: int = Field(gt=0)
    weekday_classes: list[list[str]]
    fail_closed_on_warmup: bool = True


class NewsCfg(BaseModel):
    model_config = Strict
    provider_env: str = "AURION_CALENDAR_PROVIDER"
    blocked_impacts: list[Literal["low", "medium", "high"]]
    blackout_minutes: int = Field(ge=0)
    event_overrides: dict[str, int] = Field(default_factory=dict)
    fail_safe: Literal["block", "allow"] = "block"
    point_in_time_required: bool = False


class SelectivityLevel(BaseModel):
    model_config = Strict
    displacement_k: float = Field(gt=0)
    fvg_min_atr: float = Field(ge=0)
    sweep_min_penetration_atr: float = Field(ge=0)


class AnalysisCfg(BaseModel):
    """Phase 2 needs only the pieces below; Phase 3 sections ride along untyped."""

    model_config = ConfigDict(extra="allow")

    atr_period: int = Field(default=14, gt=1)
    selectivity: Literal["loose", "base", "tight"] = "base"
    selectivity_levels: dict[str, SelectivityLevel]

    @model_validator(mode="after")
    def _selected_level_exists(self) -> AnalysisCfg:
        if self.selectivity not in self.selectivity_levels:
            raise ValueError(f"analysis.selectivity '{self.selectivity}' has no declared level")
        return self

    @property
    def active_selectivity(self) -> SelectivityLevel:
        return self.selectivity_levels[self.selectivity]


class RuntimeCfg(BaseModel):
    model_config = Strict
    evaluate_on_forming_bar: bool = False
    thread_pool_workers: int = Field(default=1, ge=1)
    log_format: Literal["json", "text"] = "json"
    log_rotate_mb: int = Field(default=50, gt=0)


class HealthCfg(BaseModel):
    model_config = Strict
    max_bar_staleness_s: float = Field(gt=0)
    calendar_outage_realert_min: int = Field(gt=0)
    heartbeat_time: time
    heartbeat_tz: str


class ValidationCfg(BaseModel):
    model_config = ConfigDict(extra="allow")

    max_trials: int = Field(gt=0)
    oos_primary: Literal["concat_walkforward", "holdout"] = "concat_walkforward"
    holdout_role: Literal["degradation_check", "significance_gate"] = "degradation_check"
    holdout_months: int = Field(gt=0)
    wf_train_months: int = Field(gt=0)
    wf_test_months: int = Field(gt=0)
    wf_step_months: int = Field(gt=0)
    selection_branch: Literal["A_no_selection", "B_partial", "C_full", "pending_pilot"]

    @model_validator(mode="after")
    def _no_training_overlap(self) -> ValidationCfg:
        """Guard the Correction-4 design: overlapping training windows inflate variance.

        docs/07 §9: train 24m / step 3m gave 5.38x inflation and an effective N of 51 from a
        nominal 276. A step shorter than the training window is permitted but must be
        deliberate, so it is reported rather than silently accepted.
        """
        if self.wf_step_months < self.wf_train_months:
            overlap = 1.0 - self.wf_step_months / self.wf_train_months
            object.__setattr__(self, "_training_overlap", overlap)
        else:
            object.__setattr__(self, "_training_overlap", 0.0)
        return self

    @property
    def training_overlap(self) -> float:
        return float(getattr(self, "_training_overlap", 0.0))


class MetaCfg(BaseModel):
    model_config = Strict
    config_version: int
    mode: Literal["paper", "live_alerts", "backtest"] = "paper"


class AurionConfig(BaseModel):
    """Root config. Unknown section names fail; unknown keys in future sections do not."""

    model_config = Strict

    meta: MetaCfg
    instruments: dict[str, InstrumentCfg]
    usdx_proxy: UsdxProxyCfg
    data: DataCfg
    adapter: AdapterCfg
    sessions: SessionsCfg
    blocks: BlocksCfg
    spread: SpreadCfg
    news: NewsCfg
    analysis: AnalysisCfg
    runtime: RuntimeCfg
    health: HealthCfg
    validation: ValidationCfg
    # Later phases, carried untyped until their code exists.
    exposure: dict[str, Any] = Field(default_factory=dict)
    bias: dict[str, Any] = Field(default_factory=dict)
    entry: dict[str, Any] = Field(default_factory=dict)
    signal: dict[str, Any] = Field(default_factory=dict)
    lifecycle: dict[str, Any] = Field(default_factory=dict)
    sizing: dict[str, Any] = Field(default_factory=dict)
    model: dict[str, Any] = Field(default_factory=dict)
    notify: dict[str, Any] = Field(default_factory=dict)
    costs: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _cross_checks(self) -> AurionConfig:
        declared = set(self.sessions.windows)
        for sym, inst in self.instruments.items():
            unknown = set(inst.sessions) - declared
            if unknown:
                raise ValueError(
                    f"instrument {sym} references undeclared sessions: {sorted(unknown)}"
                )
        for tf in self.data.timeframes:
            if tf not in self.data.anchors:
                raise ValueError(f"data.timeframes includes {tf} with no anchor declared")
        # Parse at load, not at the first bar. An unparseable anchor is a config error and
        # must stop the process before it has ingested anything.
        self.anchors()
        return self

    def anchors(self) -> dict[TimeFrame, Anchor]:
        """Declared anchors, parsed.

        The resampler, the session gates and the rollover check all take their boundaries
        from here, so changing `data.anchors.D1` changes every one of them together. Nothing
        reads `DEFAULT_ANCHORS` on a configured path -- an anchor that moved in config but
        not in the resampler would silently produce two different definitions of a day.
        """
        out: dict[TimeFrame, Anchor] = {}
        for name, spec in self.data.anchors.items():
            try:
                tf = TimeFrame(name)
            except ValueError as exc:
                raise ValueError(f"data.anchors has unknown timeframe {name!r}") from exc
            out[tf] = parse_anchor(spec)
        return out

    @property
    def enabled_instruments(self) -> dict[str, InstrumentCfg]:
        return {s: i for s, i in self.instruments.items() if i.enabled}


def load_config(path: str | Path = "config.yaml") -> AurionConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return AurionConfig.model_validate(raw)
