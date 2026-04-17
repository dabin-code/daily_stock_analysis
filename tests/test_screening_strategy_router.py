"""TDD tests for market-adaptive strategy selection in screening.

Tests that ScreeningTaskService.resolve_active_strategies can use
StrategyRouter's regime detection to select appropriate strategies.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.agent.strategies.router import StrategyRouter, _REGIME_STRATEGIES


# ── StrategyRouter regime → screening strategy mapping ───────────────────────

class TestStrategyRouterRegimeMapping:
    def test_trending_up_includes_volume_breakout(self):
        strategies = _REGIME_STRATEGIES.get("trending_up", [])
        assert "volume_breakout" in strategies

    def test_trending_up_includes_shrink_pullback(self):
        # shrink_pullback 依赖多头排列（上升趋势），只应出现在 trending_up 下。
        strategies = _REGIME_STRATEGIES.get("trending_up", [])
        assert "shrink_pullback" in strategies

    def test_trending_down_includes_bottom_volume(self):
        strategies = _REGIME_STRATEGIES.get("trending_down", [])
        assert "bottom_volume" in strategies

    def test_trending_down_excludes_shrink_pullback(self):
        # shrink_pullback 需要上升趋势，不应被路由到下跌行情。
        strategies = _REGIME_STRATEGIES.get("trending_down", [])
        assert "shrink_pullback" not in strategies

    def test_sideways_excludes_shrink_pullback(self):
        # 横盘期 MA 不构成多头排列，shrink_pullback detector 会被 no_uptrend 拒绝。
        strategies = _REGIME_STRATEGIES.get("sideways", [])
        assert "shrink_pullback" not in strategies


# ── ScreeningTaskService regime-based selection ──────────────────────────────

class TestScreeningTaskServiceRegimeSelection:

    def test_resolve_active_strategies_with_explicit_strategies(self):
        from src.services.screening_task_service import ScreeningTaskService
        mock_db = MagicMock()
        service = ScreeningTaskService(db_manager=mock_db)

        result = service.resolve_active_strategies(
            mode="balanced",
            strategies=["volume_breakout", "ma_golden_cross"],
        )
        assert result == ["volume_breakout", "ma_golden_cross"]

    def test_resolve_active_strategies_default_returns_none(self):
        from src.services.screening_task_service import ScreeningTaskService
        mock_db = MagicMock()
        service = ScreeningTaskService(db_manager=mock_db)

        result = service.resolve_active_strategies(mode="balanced")
        assert result is None

    def test_resolve_active_strategies_aggressive_mode(self):
        """Aggressive mode could prioritize breakout strategies."""
        from src.services.screening_task_service import ScreeningTaskService
        mock_db = MagicMock()
        service = ScreeningTaskService(db_manager=mock_db)

        result = service.resolve_active_strategies(
            mode="aggressive",
            strategies=["volume_breakout"],
        )
        assert "volume_breakout" in result


# ── Screening-specific regime mapping ────────────────────────────────────────

SCREENING_REGIME_MAPPING = {
    "trending_up": ["volume_breakout", "ma_golden_cross"],
    "trending_down": ["bottom_volume"],
    "sideways": ["shrink_pullback", "one_yang_three_yin"],
}


class TestScreeningRegimeMapping:
    """Test the screening-specific regime-to-strategy mapping."""

    def test_trending_up_returns_breakout_strategies(self):
        strategies = SCREENING_REGIME_MAPPING["trending_up"]
        assert "volume_breakout" in strategies
        assert "ma_golden_cross" in strategies

    def test_trending_down_returns_reversal_strategies(self):
        strategies = SCREENING_REGIME_MAPPING["trending_down"]
        assert "bottom_volume" in strategies

    def test_sideways_returns_consolidation_strategies(self):
        strategies = SCREENING_REGIME_MAPPING["sideways"]
        assert "shrink_pullback" in strategies
        assert "one_yang_three_yin" in strategies
