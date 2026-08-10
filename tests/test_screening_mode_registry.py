import pytest

from src.config import Config
from src.services.screening_mode_registry import resolve_screening_runtime_config


def _make_config(**kwargs) -> Config:
    defaults = dict(
        stock_list=["600519"],
        screening_default_mode="balanced",
        screening_candidate_limit=30,
        screening_ai_top_k=10,
        screening_min_list_days=120,
        screening_min_volume_ratio=1.2,
        screening_breakout_lookback_days=20,
        screening_factor_lookback_days=80,
    )
    defaults.update(kwargs)
    return Config(**defaults)


def test_resolve_screening_runtime_config_uses_balanced_defaults():
    config = _make_config()

    runtime = resolve_screening_runtime_config(config=config, mode=None, candidate_limit=None, ai_top_k=None)

    assert runtime.mode == "balanced"
    assert runtime.candidate_limit == 30
    assert runtime.ai_top_k == 10
    assert runtime.min_list_days == 120
    assert runtime.min_volume_ratio == 1.2
    assert runtime.breakout_lookback_days == 20
    assert runtime.factor_lookback_days == 80
    assert "screening_min_avg_amount" not in runtime.to_snapshot()


def test_resolve_screening_runtime_config_applies_aggressive_preset():
    config = _make_config()

    runtime = resolve_screening_runtime_config(config=config, mode="aggressive", candidate_limit=None, ai_top_k=None)

    assert runtime.mode == "aggressive"
    assert runtime.candidate_limit == 50
    assert runtime.ai_top_k == 25
    assert runtime.min_list_days == 60
    assert runtime.min_volume_ratio == 1.0
    assert runtime.breakout_lookback_days == 15
    assert runtime.factor_lookback_days == 60


def test_resolve_screening_runtime_config_allows_request_limits_override_preset():
    config = _make_config()

    runtime = resolve_screening_runtime_config(config=config, mode="quality", candidate_limit=15, ai_top_k=4)

    assert runtime.mode == "quality"
    assert runtime.candidate_limit == 15
    assert runtime.ai_top_k == 4
    assert runtime.min_list_days == 180
    assert runtime.min_volume_ratio == 1.5


def test_resolve_screening_runtime_config_keeps_aggressive_direction_with_custom_base():
    config = _make_config(
        screening_min_list_days=30,
        screening_min_volume_ratio=0.8,
        screening_breakout_lookback_days=10,
        screening_factor_lookback_days=40,
    )

    runtime = resolve_screening_runtime_config(config=config, mode="aggressive", candidate_limit=None, ai_top_k=None)

    assert runtime.min_list_days == 30
    assert runtime.min_volume_ratio == 0.8
    assert runtime.breakout_lookback_days == 10
    assert runtime.factor_lookback_days == 40


def test_resolve_screening_runtime_config_keeps_quality_direction_with_custom_base():
    config = _make_config(
        screening_min_list_days=240,
        screening_min_volume_ratio=1.8,
        screening_breakout_lookback_days=40,
        screening_factor_lookback_days=160,
    )

    runtime = resolve_screening_runtime_config(config=config, mode="quality", candidate_limit=None, ai_top_k=None)

    assert runtime.min_list_days == 240
    assert runtime.min_volume_ratio == 1.8
    assert runtime.breakout_lookback_days == 40
    assert runtime.factor_lookback_days == 160


def test_resolve_screening_runtime_config_applies_quality_ai_top_k_preset():
    config = _make_config(screening_ai_top_k=6)

    runtime = resolve_screening_runtime_config(config=config, mode="quality", candidate_limit=None, ai_top_k=None)

    assert runtime.mode == "quality"
    assert runtime.ai_top_k == 10


def test_resolve_screening_runtime_config_clamps_ai_top_k_to_candidate_limit():
    config = _make_config(
        screening_candidate_limit=8,
        screening_ai_top_k=5,
    )

    runtime = resolve_screening_runtime_config(config=config, mode="quality", candidate_limit=None, ai_top_k=None)

    assert runtime.candidate_limit == 8
    assert runtime.ai_top_k == 8


def test_resolve_screening_runtime_config_rejects_invalid_base_config():
    config = _make_config(screening_candidate_limit=10000)

    with pytest.raises(ValueError, match="SCREENING_CANDIDATE_LIMIT"):
        resolve_screening_runtime_config(
            config=config,
            mode=None,
            candidate_limit=None,
            ai_top_k=None,
        )
