"""Tests for SetupResolver — Phase 2B."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import pytest

from src.schemas.trading_types import (
    MarketRegime,
    SetupType,
    StrategyFamily,
    ThemePosition,
)
from src.services.setup_resolver import SetupResolution, SetupResolver


# ── Stub ───────────────────────────────────────────────────────────────────

@dataclass
class _StubRule:
    strategy_name: str
    system_role: Optional[str] = None
    strategy_family: Optional[str] = None
    applicable_market: Optional[List[str]] = None
    setup_type: Optional[str] = None


def _make_rules() -> List[_StubRule]:
    return [
        _StubRule("bottom_divergence_double_breakout", "entry_core", "reversal", ["balanced", "aggressive"], "bottom_divergence_breakout"),
        _StubRule("bottom_volume", "observation", "reversal", ["balanced", "aggressive", "defensive"]),
        _StubRule("bull_trend", "stock_pool", "trend", ["balanced", "aggressive", "defensive"]),
        _StubRule("dragon_head", "theme_score", "auxiliary", ["balanced", "aggressive"]),
        _StubRule("extreme_strength_combo", "stock_pool", "momentum", ["balanced", "aggressive"]),
        _StubRule("gap_limitup_breakout", "entry_core", "momentum", ["aggressive"], "gap_breakout"),
        _StubRule("ma100_60min_combined", "entry_core", "trend", ["balanced", "aggressive"], "trend_breakout"),
        _StubRule("ma100_low123_combined", "entry_core", "reversal", ["balanced", "aggressive"], "low123_breakout"),
        _StubRule("one_yang_three_yin", "bonus_signal", "auxiliary", ["balanced", "aggressive", "defensive"]),
        _StubRule("shrink_pullback", "entry_core", "trend", ["balanced", "aggressive"], "trend_pullback"),
        _StubRule("trendline_breakout", "confirm", "auxiliary", ["balanced", "aggressive"]),
        _StubRule("volume_breakout", "confirm", "auxiliary", ["balanced", "aggressive", "defensive"]),
    ]


@pytest.fixture()
def resolver() -> SetupResolver:
    return SetupResolver(_make_rules())


# ── No entry_core ──────────────────────────────────────────────────────────

class TestNoEntryCoreReturnsNone:
    def test_no_entry_core_returns_none(self, resolver: SetupResolver):
        result = resolver.resolve(
            allowed_strategies=["bull_trend", "volume_breakout"],
            strategy_scores={"bull_trend": 50.0, "volume_breakout": 40.0},
            market_regime=MarketRegime.BALANCED,
            theme_position=ThemePosition.MAIN_THEME,
        )
        assert result.setup_type == SetupType.NONE
        assert result.primary_strategy is None

    def test_empty_allowed_returns_none(self, resolver: SetupResolver):
        result = resolver.resolve(
            allowed_strategies=[],
            strategy_scores={},
            market_regime=MarketRegime.AGGRESSIVE,
            theme_position=ThemePosition.MAIN_THEME,
        )
        assert result.setup_type == SetupType.NONE
        assert "no_allowed" in result.reason


# ── Single entry_core ──────────────────────────────────────────────────────

class TestSingleEntryCore:
    def test_returns_directly(self, resolver: SetupResolver):
        result = resolver.resolve(
            allowed_strategies=["ma100_60min_combined", "volume_breakout"],
            strategy_scores={"ma100_60min_combined": 60.0, "volume_breakout": 30.0},
            market_regime=MarketRegime.BALANCED,
            theme_position=ThemePosition.MAIN_THEME,
        )
        assert result.setup_type == SetupType.TREND_BREAKOUT
        assert result.strategy_family == StrategyFamily.TREND
        assert result.primary_strategy == "ma100_60min_combined"
        assert "single_entry_core" in result.reason


# ── Environment priority tests ─────────────────────────────────────────────

class TestDefensivePreference:
    def test_defensive_prefers_reversal_over_trend(self, resolver: SetupResolver):
        result = resolver.resolve(
            allowed_strategies=[
                "bottom_divergence_double_breakout",
                "ma100_60min_combined",
            ],
            strategy_scores={
                "bottom_divergence_double_breakout": 50.0,
                "ma100_60min_combined": 50.0,
            },
            market_regime=MarketRegime.DEFENSIVE,
            theme_position=ThemePosition.NON_THEME,
        )
        assert result.setup_type == SetupType.BOTTOM_DIVERGENCE_BREAKOUT
        assert result.strategy_family == StrategyFamily.REVERSAL

    def test_non_theme_prefers_reversal(self, resolver: SetupResolver):
        """Even in aggressive regime, NON_THEME falls back to reversal > trend."""
        result = resolver.resolve(
            allowed_strategies=[
                "bottom_divergence_double_breakout",
                "ma100_60min_combined",
            ],
            strategy_scores={
                "bottom_divergence_double_breakout": 50.0,
                "ma100_60min_combined": 50.0,
            },
            market_regime=MarketRegime.AGGRESSIVE,
            theme_position=ThemePosition.NON_THEME,
        )
        assert result.strategy_family == StrategyFamily.REVERSAL


class TestAggressiveMainTheme:
    def test_prefers_trend_over_reversal(self, resolver: SetupResolver):
        result = resolver.resolve(
            allowed_strategies=[
                "bottom_divergence_double_breakout",
                "ma100_60min_combined",
            ],
            strategy_scores={
                "bottom_divergence_double_breakout": 80.0,
                "ma100_60min_combined": 50.0,
            },
            market_regime=MarketRegime.AGGRESSIVE,
            theme_position=ThemePosition.MAIN_THEME,
        )
        # Trend family has higher priority in aggressive+main_theme
        assert result.strategy_family == StrategyFamily.TREND
        assert result.setup_type == SetupType.TREND_BREAKOUT

    def test_prefers_momentum_over_reversal(self, resolver: SetupResolver):
        result = resolver.resolve(
            allowed_strategies=[
                "gap_limitup_breakout",
                "bottom_divergence_double_breakout",
            ],
            strategy_scores={
                "gap_limitup_breakout": 50.0,
                "bottom_divergence_double_breakout": 50.0,
            },
            market_regime=MarketRegime.AGGRESSIVE,
            theme_position=ThemePosition.MAIN_THEME,
        )
        # aggressive+theme: trend > momentum > reversal
        assert result.strategy_family == StrategyFamily.MOMENTUM


class TestBalancedTheme:
    def test_balanced_main_theme_prefers_trend(self, resolver: SetupResolver):
        result = resolver.resolve(
            allowed_strategies=[
                "bottom_divergence_double_breakout",
                "ma100_60min_combined",
            ],
            strategy_scores={
                "bottom_divergence_double_breakout": 70.0,
                "ma100_60min_combined": 50.0,
            },
            market_regime=MarketRegime.BALANCED,
            theme_position=ThemePosition.MAIN_THEME,
        )
        assert result.strategy_family == StrategyFamily.TREND

    def test_balanced_secondary_theme_prefers_trend(self, resolver: SetupResolver):
        result = resolver.resolve(
            allowed_strategies=[
                "bottom_divergence_double_breakout",
                "shrink_pullback",
            ],
            strategy_scores={
                "bottom_divergence_double_breakout": 60.0,
                "shrink_pullback": 60.0,
            },
            market_regime=MarketRegime.BALANCED,
            theme_position=ThemePosition.SECONDARY_THEME,
        )
        assert result.strategy_family == StrategyFamily.TREND


# ── Tie-break tests ────────────────────────────────────────────────────────

class TestTieBreak:
    def test_same_family_tiebreak_by_score(self, resolver: SetupResolver):
        """Two trend strategies: pick the higher-scoring one."""
        result = resolver.resolve(
            allowed_strategies=[
                "ma100_60min_combined",
                "shrink_pullback",
            ],
            strategy_scores={
                "ma100_60min_combined": 40.0,
                "shrink_pullback": 70.0,
            },
            market_regime=MarketRegime.BALANCED,
            theme_position=ThemePosition.MAIN_THEME,
        )
        assert result.primary_strategy == "shrink_pullback"
        assert result.setup_type == SetupType.TREND_PULLBACK

    def test_same_family_same_score_tiebreak_by_name(self, resolver: SetupResolver):
        """Same family, same score: alphabetical tiebreak."""
        result = resolver.resolve(
            allowed_strategies=[
                "shrink_pullback",
                "ma100_60min_combined",
            ],
            strategy_scores={
                "shrink_pullback": 50.0,
                "ma100_60min_combined": 50.0,
            },
            market_regime=MarketRegime.BALANCED,
            theme_position=ThemePosition.MAIN_THEME,
        )
        # ma100_60min_combined < shrink_pullback alphabetically
        assert result.primary_strategy == "ma100_60min_combined"


# ── Contributing strategies ────────────────────────────────────────────────

class TestContributingStrategies:
    def test_includes_all_allowed_strategies(self, resolver: SetupResolver):
        result = resolver.resolve(
            allowed_strategies=[
                "ma100_60min_combined",
                "volume_breakout",
                "bull_trend",
            ],
            strategy_scores={
                "ma100_60min_combined": 60.0,
                "volume_breakout": 30.0,
                "bull_trend": 40.0,
            },
            market_regime=MarketRegime.BALANCED,
            theme_position=ThemePosition.MAIN_THEME,
        )
        assert set(result.contributing_strategies) == {
            "ma100_60min_combined", "volume_breakout", "bull_trend",
        }


class TestFadingTheme:
    def test_fading_theme_prefers_reversal(self, resolver: SetupResolver):
        """FADING_THEME uses same priority as NON_THEME: reversal > trend."""
        result = resolver.resolve(
            allowed_strategies=[
                "bottom_divergence_double_breakout",
                "ma100_60min_combined",
            ],
            strategy_scores={
                "bottom_divergence_double_breakout": 50.0,
                "ma100_60min_combined": 50.0,
            },
            market_regime=MarketRegime.BALANCED,
            theme_position=ThemePosition.FADING_THEME,
        )
        assert result.strategy_family == StrategyFamily.REVERSAL


# ── Stock-pool role regression ─────────────────────────────────────────────
#
# extreme_strength_combo 的 system_role 是 stock_pool，不应被 SetupResolver
# 当成 entry_core 处理；它永远不能成为 primary_strategy，也不应产出
# 具体 setup_type。这些测试锁住既有行为，防止后续权重/排序调整时
# 误把池子层策略提升为买点策略。

class TestExtremeStrengthComboAsStockPool:
    def test_alone_returns_none_setup(self, resolver: SetupResolver):
        """仅有 extreme_strength_combo 时返回 SetupType.NONE + no_entry_core_in_allowed。"""
        result = resolver.resolve(
            allowed_strategies=["extreme_strength_combo"],
            strategy_scores={"extreme_strength_combo": 95.0},
            market_regime=MarketRegime.AGGRESSIVE,
            theme_position=ThemePosition.MAIN_THEME,
        )
        assert result.setup_type == SetupType.NONE
        assert result.strategy_family is None
        assert result.primary_strategy is None
        assert result.reason == "no_entry_core_in_allowed"
        assert "extreme_strength_combo" in result.contributing_strategies

    def test_with_other_non_entry_core_still_returns_none(self, resolver: SetupResolver):
        """与其他非 entry_core 策略组合，仍然没有 setup_type。"""
        result = resolver.resolve(
            allowed_strategies=[
                "extreme_strength_combo",
                "dragon_head",
                "volume_breakout",
            ],
            strategy_scores={
                "extreme_strength_combo": 90.0,
                "dragon_head": 80.0,
                "volume_breakout": 70.0,
            },
            market_regime=MarketRegime.AGGRESSIVE,
            theme_position=ThemePosition.MAIN_THEME,
        )
        assert result.setup_type == SetupType.NONE
        assert result.primary_strategy is None
        assert result.reason == "no_entry_core_in_allowed"
        assert set(result.contributing_strategies) >= {
            "extreme_strength_combo",
            "dragon_head",
            "volume_breakout",
        }

    def test_with_entry_core_never_wins_primary(self, resolver: SetupResolver):
        """即使 extreme_strength_combo 分数更高，也不能成为 primary_strategy。"""
        result = resolver.resolve(
            allowed_strategies=[
                "extreme_strength_combo",
                "ma100_60min_combined",
            ],
            strategy_scores={
                "extreme_strength_combo": 99.0,
                "ma100_60min_combined": 40.0,
            },
            market_regime=MarketRegime.AGGRESSIVE,
            theme_position=ThemePosition.MAIN_THEME,
        )
        assert result.primary_strategy == "ma100_60min_combined"
        assert result.setup_type == SetupType.TREND_BREAKOUT
        assert result.strategy_family == StrategyFamily.TREND
        assert "single_entry_core" in result.reason
        assert "extreme_strength_combo" in result.contributing_strategies

    @pytest.mark.parametrize(
        "regime,theme_pos",
        [
            (MarketRegime.AGGRESSIVE, ThemePosition.MAIN_THEME),
            (MarketRegime.BALANCED, ThemePosition.MAIN_THEME),
            (MarketRegime.BALANCED, ThemePosition.SECONDARY_THEME),
            (MarketRegime.DEFENSIVE, ThemePosition.NON_THEME),
            (MarketRegime.BALANCED, ThemePosition.FADING_THEME),
        ],
    )
    def test_never_becomes_primary_across_contexts(
        self,
        resolver: SetupResolver,
        regime: MarketRegime,
        theme_pos: ThemePosition,
    ) -> None:
        """不同市场/题材上下文下，extreme_strength_combo 都不应被当成 setup_type 来源。"""
        result = resolver.resolve(
            allowed_strategies=["extreme_strength_combo"],
            strategy_scores={"extreme_strength_combo": 100.0},
            market_regime=regime,
            theme_position=theme_pos,
        )
        assert result.setup_type == SetupType.NONE
        assert result.primary_strategy is None
        assert result.strategy_family is None
        assert result.reason == "no_entry_core_in_allowed"
