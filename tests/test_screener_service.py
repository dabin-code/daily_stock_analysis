from unittest.mock import patch

import pandas as pd
import pytest

from src.agent.skills.base import SkillManager
from src.services.screener_service import ScreenerService


def test_screener_filters_ineligible_rows_and_sorts_by_score():
    snapshot_df = pd.DataFrame(
        [
            {
                "code": "600001",
                "name": "趋势龙头",
                "close": 10.4,
                "ma5": 10.2,
                "ma10": 10.0,
                "ma20": 9.8,
                "ma60": 9.4,
                "volume_ratio": 2.6,
                "avg_amount": 180_000_000,
                "breakout_ratio": 1.02,
                "trend_score": 58,
                "liquidity_score": 85,
                "close_strength": 0.8,
                "ma5_distance_pct": 2.0,
                "pct_chg": 4.8,
                "is_st": False,
                "days_since_listed": 500,
            },
            {
                "code": "600002",
                "name": "温和上涨",
                "close": 9.95,
                "ma5": 9.9,
                "ma10": 9.8,
                "ma20": 9.7,
                "ma60": 9.5,
                "volume_ratio": 2.1,
                "avg_amount": 80_000_000,
                "breakout_ratio": 0.995,
                "trend_score": 42,
                "liquidity_score": 65,
                "close_strength": 0.7,
                "ma5_distance_pct": 1.0,
                "pct_chg": 2.1,
                "is_st": False,
                "days_since_listed": 300,
            },
            {
                "code": "600003",
                "name": "低流动性",
                "close": 7.8,
                "ma5": 7.7,
                "ma10": 7.6,
                "ma20": 7.5,
                "ma60": 7.4,
                "volume_ratio": 1.4,
                "avg_amount": 10_000_000,
                "breakout_ratio": 1.01,
                "trend_score": 55,
                "liquidity_score": 20,
                "pct_chg": 3.0,
                "is_st": False,
                "days_since_listed": 800,
            },
            {
                "code": "600004",
                "name": "*ST风险股",
                "close": 5.2,
                "ma5": 5.1,
                "ma10": 5.0,
                "ma20": 4.9,
                "ma60": 4.8,
                "volume_ratio": 2.0,
                "avg_amount": 120_000_000,
                "breakout_ratio": 1.03,
                "trend_score": 60,
                "liquidity_score": 70,
                "pct_chg": 4.2,
                "is_st": True,
                "days_since_listed": 900,
            },
        ]
    )

    skill_manager = SkillManager()
    skill_manager.load_builtin_strategies()
    service = ScreenerService(
        min_list_days=120,
        min_volume_ratio=1.2,
        breakout_lookback_days=20,
        skill_manager=skill_manager,
        strategy_names=["volume_breakout"],
    )

    candidates = service.screen(snapshot_df=snapshot_df, candidate_limit=5)

    assert [candidate.code for candidate in candidates] == ["600001", "600002"]
    assert candidates[0].rule_score > candidates[1].rule_score
    assert "strategy:volume_breakout" in candidates[0].rule_hits
    assert "volume_ratio:>=:2.0" in candidates[0].rule_hits
    assert "breakout_ratio:>=:0.995" in candidates[0].rule_hits


def test_screener_returns_rejection_reasons_for_all_rows():
    snapshot_df = pd.DataFrame(
        [
            {
                "code": "600010",
                "name": "上市太短",
                "close": 11.0,
                "ma5": 10.8,
                "ma10": 10.6,
                "ma20": 10.2,
                "ma60": 9.8,
                "volume_ratio": 1.4,
                "avg_amount": 100_000_000,
                "breakout_ratio": 1.01,
                "pct_chg": 3.2,
                "is_st": False,
                "days_since_listed": 40,
            }
        ]
    )

    service = ScreenerService(min_list_days=120)
    result = service.evaluate(snapshot_df)

    assert result.rejected[0]["code"] == "600010"
    assert "listed_days_below_threshold" in result.rejected[0]["rejection_reasons"]


@patch("src.services.screener_service.get_config")
def test_screener_uses_config_thresholds_by_default(get_config_mock):
    get_config_mock.return_value.screening_min_list_days = 180
    get_config_mock.return_value.screening_min_volume_ratio = 1.6
    get_config_mock.return_value.screening_breakout_lookback_days = 30

    service = ScreenerService()

    assert service.min_list_days == 180
    assert service.min_volume_ratio == 1.6
    assert service.breakout_lookback_days == 30


def test_screener_raises_actionable_error_when_requested_strategies_are_missing():
    snapshot_df = pd.DataFrame(
        [
            {
                "code": "600001",
                "name": "趋势龙头",
                "close": 10.4,
                "ma5": 10.2,
                "ma10": 10.0,
                "ma20": 9.8,
                "volume_ratio": 2.6,
                "avg_amount": 180_000_000,
                "is_st": False,
                "days_since_listed": 500,
            },
        ]
    )

    skill_manager = SkillManager()
    skill_manager.load_builtin_strategies()
    service = ScreenerService(
        skill_manager=skill_manager,
        strategy_names=["missing_strategy"],
    )

    with pytest.raises(RuntimeError, match="missing_strategy"):
        service.evaluate(snapshot_df)


def test_screener_raises_actionable_error_when_strategy_list_is_empty():
    snapshot_df = pd.DataFrame(
        [
            {
                "code": "600001",
                "name": "趋势龙头",
                "close": 10.4,
                "ma5": 10.2,
                "ma10": 10.0,
                "ma20": 9.8,
                "volume_ratio": 2.6,
                "avg_amount": 180_000_000,
                "is_st": False,
                "days_since_listed": 500,
            },
        ]
    )

    skill_manager = SkillManager()
    skill_manager.load_builtin_strategies()
    service = ScreenerService(
        skill_manager=skill_manager,
        strategy_names=[],
    )

    with pytest.raises(RuntimeError, match="empty strategy_names"):
        service.evaluate(snapshot_df)
