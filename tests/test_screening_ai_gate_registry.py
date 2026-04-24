"""Tests for ScreeningAiGateRegistry – YAML ai_gate loading and fallback."""

from __future__ import annotations

import pytest

from src.schemas.screening_ai_gate import ScreeningAiGateConfig
from src.services.screening_ai_gate_registry import ScreeningAiGateRegistry


class TestScreeningAiGateRegistryLoading:
    """Registry can load ai_gate configs from builtin strategy YAML files."""

    def test_registry_loads_bottom_divergence_ai_gate(self):
        registry = ScreeningAiGateRegistry.from_builtin_strategies()
        config = registry.get("bottom_divergence_double_breakout")
        assert config is not None
        assert isinstance(config, ScreeningAiGateConfig)
        assert config.strategy_name == "bottom_divergence_double_breakout"
        assert config.version == "v1"
        assert config.supported is True
        assert config.ai_priority >= 1

    def test_registry_bottom_divergence_has_stage_definitions(self):
        registry = ScreeningAiGateRegistry.from_builtin_strategies()
        config = registry.get("bottom_divergence_double_breakout")
        assert config is not None
        assert "confirm_entry_window" in config.stage_definitions
        assert "structure_forming" in config.stage_definitions

    def test_registry_bottom_divergence_has_hard_veto_rules(self):
        registry = ScreeningAiGateRegistry.from_builtin_strategies()
        config = registry.get("bottom_divergence_double_breakout")
        assert config is not None
        assert len(config.hard_veto_rules) > 0

    def test_registry_bottom_divergence_has_payload_fields(self):
        registry = ScreeningAiGateRegistry.from_builtin_strategies()
        config = registry.get("bottom_divergence_double_breakout")
        assert config is not None
        assert len(config.payload_fields) > 0
        assert "signal_strength" in config.payload_fields

    def test_registry_bottom_divergence_has_news_focus(self):
        registry = ScreeningAiGateRegistry.from_builtin_strategies()
        config = registry.get("bottom_divergence_double_breakout")
        assert config is not None
        assert len(config.news_focus) > 0

    def test_registry_bottom_divergence_has_playbook(self):
        registry = ScreeningAiGateRegistry.from_builtin_strategies()
        config = registry.get("bottom_divergence_double_breakout")
        assert config is not None
        assert config.playbook is not None
        assert len(config.playbook) > 0


class TestScreeningAiGateRegistryUnsupported:
    """Strategies without a complete ai_gate block return unsupported markers."""

    def test_registry_returns_unsupported_for_ma100_low123(self):
        registry = ScreeningAiGateRegistry.from_builtin_strategies()
        config = registry.get("ma100_low123_combined")
        assert config is not None
        assert config.supported is False

    def test_registry_returns_unsupported_for_ma100_60min(self):
        registry = ScreeningAiGateRegistry.from_builtin_strategies()
        config = registry.get("ma100_60min_combined")
        assert config is not None
        assert config.supported is False

    def test_registry_returns_unsupported_for_extreme_strength(self):
        registry = ScreeningAiGateRegistry.from_builtin_strategies()
        config = registry.get("extreme_strength_combo")
        assert config is not None
        assert config.supported is False

    def test_registry_returns_none_for_unknown_strategy(self):
        registry = ScreeningAiGateRegistry.from_builtin_strategies()
        config = registry.get("nonexistent_strategy")
        assert config is None


class TestScreeningAiGateRegistryEnumeration:
    """Registry exposes supported and all strategy lists."""

    def test_registry_lists_all_screening_strategies(self):
        registry = ScreeningAiGateRegistry.from_builtin_strategies()
        all_names = registry.list_all()
        assert "bottom_divergence_double_breakout" in all_names
        assert "ma100_low123_combined" in all_names
        assert "ma100_60min_combined" in all_names
        assert "extreme_strength_combo" in all_names

    def test_registry_lists_supported_strategies(self):
        registry = ScreeningAiGateRegistry.from_builtin_strategies()
        supported = registry.list_supported()
        assert "bottom_divergence_double_breakout" in supported
        # Phase 1: only bottom divergence is fully supported
        for name in supported:
            config = registry.get(name)
            assert config is not None
            assert config.supported is True


class TestScreeningAiGateConfigValidation:
    """ScreeningAiGateConfig Pydantic model validates correctly."""

    def test_minimal_valid_config(self):
        config = ScreeningAiGateConfig(
            strategy_name="test_strategy",
            version="v1",
            ai_priority=1,
            supported=True,
            playbook={"goal": "test"},
            stage_definitions={"stage_a": {"label": "测试"}},
            hard_veto_rules=["rule_a"],
            news_focus=["sector_news"],
            payload_fields=["field_a"],
        )
        assert config.strategy_name == "test_strategy"

    def test_unsupported_config_has_defaults(self):
        config = ScreeningAiGateConfig(
            strategy_name="stub",
            version="v1",
            ai_priority=99,
            supported=False,
            playbook={},
            stage_definitions={},
            hard_veto_rules=[],
            news_focus=[],
            payload_fields=[],
        )
        assert config.supported is False
        assert config.ai_priority == 99
