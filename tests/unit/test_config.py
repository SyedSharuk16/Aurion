"""Config validation. The process must refuse to start on error, not default."""

from __future__ import annotations

import copy

import pytest
import yaml
from pydantic import ValidationError

from aurion.config.models import AurionConfig, load_config
from aurion.core.timeframe import TimeFrame


@pytest.fixture
def raw() -> dict:
    with open("config.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_shipped_config_is_valid():
    cfg = load_config("config.yaml")
    assert cfg.meta.config_version == 1
    assert set(cfg.enabled_instruments) == {
        "XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD"
    }


def test_pip_factor_is_derived_not_configured():
    cfg = load_config("config.yaml")
    assert cfg.instruments["EURUSD"].pip_factor == 10   # 5-digit
    assert cfg.instruments["USDJPY"].pip_factor == 10   # 3-digit
    assert cfg.instruments["XAUUSD"].pip_factor == 1    # pips are not used for gold


def test_unknown_section_name_is_rejected(raw):
    raw["sessionz"] = {}
    with pytest.raises(ValidationError, match="sessionz"):
        AurionConfig.model_validate(raw)


def test_instrument_referencing_an_undeclared_session_is_rejected(raw):
    raw = copy.deepcopy(raw)
    raw["instruments"]["XAUUSD"]["sessions"].append("not_a_window")
    with pytest.raises(ValidationError, match="undeclared sessions"):
        AurionConfig.model_validate(raw)


def test_timeframe_without_an_anchor_is_rejected(raw):
    raw = copy.deepcopy(raw)
    raw["data"]["anchors"].pop("H4")
    with pytest.raises(ValidationError, match="no anchor declared"):
        AurionConfig.model_validate(raw)


def test_selectivity_must_name_a_declared_level(raw):
    raw = copy.deepcopy(raw)
    raw["analysis"]["selectivity"] = "aggressive"
    with pytest.raises(ValidationError):
        AurionConfig.model_validate(raw)


def test_active_selectivity_resolves_the_collapsed_dimension():
    """docs/07 §6: k, g and eps move together as one ordinal, not three dimensions."""
    cfg = load_config("config.yaml")
    base = cfg.analysis.active_selectivity
    assert base.displacement_k == 1.0
    loose = cfg.analysis.selectivity_levels["loose"]
    tight = cfg.analysis.selectivity_levels["tight"]
    assert loose.displacement_k < base.displacement_k < tight.displacement_k
    assert loose.fvg_min_atr < base.fvg_min_atr < tight.fvg_min_atr
    assert (
        loose.sweep_min_penetration_atr
        < base.sweep_min_penetration_atr
        < tight.sweep_min_penetration_atr
    )


def test_walkforward_ships_with_non_overlapping_training_windows():
    """docs/07 §9 Correction 4. Overlap inflated variance 5.38x and N_eff fell to 51."""
    cfg = load_config("config.yaml")
    assert cfg.validation.wf_step_months >= cfg.validation.wf_train_months
    assert cfg.validation.training_overlap == 0.0


def test_overlapping_walkforward_is_reported_not_silently_accepted(raw):
    raw = copy.deepcopy(raw)
    raw["validation"]["wf_train_months"] = 24
    raw["validation"]["wf_step_months"] = 3
    cfg = AurionConfig.model_validate(raw)
    assert cfg.validation.training_overlap == pytest.approx(0.875)


def test_primary_oos_is_the_concatenated_walkforward_not_the_holdout():
    cfg = load_config("config.yaml")
    assert cfg.validation.oos_primary == "concat_walkforward"
    assert cfg.validation.holdout_role == "degradation_check"


def test_selection_branch_starts_pending_the_pilot():
    """docs/07 §10: the branch is set from measured P3/P5, never assumed."""
    assert load_config("config.yaml").validation.selection_branch == "pending_pilot"


def test_forming_bar_evaluation_is_off_by_default():
    assert load_config("config.yaml").runtime.evaluate_on_forming_bar is False


def test_news_fail_safe_is_block():
    assert load_config("config.yaml").news.fail_safe == "block"


def test_negative_point_is_rejected(raw):
    raw = copy.deepcopy(raw)
    raw["instruments"]["XAUUSD"]["point"] = -0.01
    with pytest.raises(ValidationError):
        AurionConfig.model_validate(raw)


def test_an_unparseable_anchor_fails_at_load(raw):
    """The anchors section is read, not merely validated for presence."""
    raw = copy.deepcopy(raw)
    raw["data"]["anchors"]["D1"] = "17:00 Mars/Olympus"
    with pytest.raises(ValidationError):
        AurionConfig.model_validate(raw)


def test_an_anchor_for_an_unknown_timeframe_fails_at_load(raw):
    raw = copy.deepcopy(raw)
    raw["data"]["anchors"]["M7"] = "00:00 UTC"
    with pytest.raises(ValidationError):
        AurionConfig.model_validate(raw)


def test_declared_anchors_are_exposed_as_parsed_objects():
    anchors = load_config("config.yaml").anchors()
    assert anchors[TimeFrame.D1].tz == "America/New_York"
    assert anchors[TimeFrame.W1].weekday == 6
    assert anchors[TimeFrame.M15].tz == "UTC"
