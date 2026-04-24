"""Tests for ScreeningAiGateResolver – active strategy selection."""

from __future__ import annotations

import pytest

from src.services.screening_ai_gate_registry import ScreeningAiGateRegistry
from src.services.screening_ai_gate_resolver import ScreeningAiGateResolver, ResolvedActiveStrategy


@pytest.fixture
def registry():
    return ScreeningAiGateRegistry.from_builtin_strategies()


@pytest.fixture
def resolver(registry):
    return ScreeningAiGateResolver(registry)


class TestActiveStrategyResolution:
    """Resolver deterministically picks one active strategy per candidate."""

    def test_single_supported_strategy_wins(self, resolver):
        active, alternatives = resolver.resolve(
            matched_strategies=["bottom_divergence_double_breakout"],
            factor_snapshot={"bottom_divergence_signal_strength": 0.85},
        )
        assert active is not None
        assert active.strategy_name == "bottom_divergence_double_breakout"
        assert active.supported is True
        assert alternatives == []

    def test_highest_priority_supported_strategy_wins(self, resolver):
        active, alternatives = resolver.resolve(
            matched_strategies=["ma100_low123_combined", "bottom_divergence_double_breakout"],
            factor_snapshot={"bottom_divergence_signal_strength": 0.92},
        )
        assert active is not None
        assert active.strategy_name == "bottom_divergence_double_breakout"
        assert "ma100_low123_combined" in alternatives

    def test_unsupported_only_returns_skipped(self, resolver):
        active, alternatives = resolver.resolve(
            matched_strategies=["ma100_low123_combined"],
            factor_snapshot={},
        )
        assert active is not None
        assert active.strategy_name == "ma100_low123_combined"
        assert active.supported is False

    def test_empty_strategies_returns_none(self, resolver):
        active, alternatives = resolver.resolve(
            matched_strategies=[],
            factor_snapshot={},
        )
        assert active is None
        assert alternatives == []

    def test_unknown_strategy_returns_none(self, resolver):
        active, alternatives = resolver.resolve(
            matched_strategies=["nonexistent_strategy"],
            factor_snapshot={},
        )
        assert active is None

    def test_resolved_active_strategy_has_config(self, resolver):
        active, _ = resolver.resolve(
            matched_strategies=["bottom_divergence_double_breakout"],
            factor_snapshot={},
        )
        assert active is not None
        assert active.config is not None
        assert active.config.ai_priority == 1

    def test_multiple_unsupported_picks_lowest_priority_number(self, resolver):
        active, alternatives = resolver.resolve(
            matched_strategies=["extreme_strength_combo", "ma100_low123_combined"],
            factor_snapshot={},
        )
        assert active is not None
        assert active.supported is False
        # Both unsupported, both priority 99 — pick first alphabetically or by order
        assert active.strategy_name in {"extreme_strength_combo", "ma100_low123_combined"}
