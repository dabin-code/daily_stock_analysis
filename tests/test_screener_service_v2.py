"""TDD tests for ScreenerService v2 — strategy-engine backed.

Tests that ScreenerService can operate in two modes:
1. Legacy mode (no SkillManager): backward-compatible hardcoded rules
2. Strategy mode (with SkillManager): delegates to StrategyScreeningEngine
"""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from src.agent.skills.base import Skill, SkillManager, load_skill_from_yaml
from src.services.screener_service import (
    ScreenerService,
    ScreeningCandidateRecord,
    ScreeningEvaluationResult,
)


def _make_snapshot(*rows) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _bullish_row(code="600001", name="趋势龙头", **overrides):
    base = {
        "code": code,
        "name": name,
        "close": 10.4,
        "ma5": 10.2,
        "ma10": 10.0,
        "ma20": 9.8,
        "ma60": 9.4,
        "volume_ratio": 2.5,
        "avg_amount": 200_000_000,
        "breakout_ratio": 1.02,
        "pct_chg": 4.5,
        "is_st": False,
        "days_since_listed": 500,
        "trend_score": 85.0,
        "liquidity_score": 90.0,
        "close_strength": 0.8,
        "ma5_distance_pct": 2.0,
    }
    base.update(overrides)
    return base


# ── Legacy mode tests (backward compat) ─────────────────────────────────────

class TestScreenerServiceLegacyMode:
    """Without SkillManager, ScreenerService should still work with hardcoded rules."""

    def test_legacy_evaluate_returns_evaluation_result(self):
        snapshot = _make_snapshot(
            _bullish_row("600001", breakout_ratio=1.02),
            _bullish_row("600002", is_st=True),
        )
        service = ScreenerService(
            min_list_days=120,
            min_volume_ratio=1.2,
        )
        result = service.evaluate(snapshot)
        assert isinstance(result, ScreeningEvaluationResult)
        assert len(result.selected) >= 1
        assert any(c.code == "600001" for c in result.selected)

    def test_legacy_screen_returns_candidate_records(self):
        snapshot = _make_snapshot(_bullish_row("600001"))
        service = ScreenerService(
            min_list_days=120,
            min_volume_ratio=1.2,
        )
        candidates = service.screen(snapshot, candidate_limit=10)
        assert all(isinstance(c, ScreeningCandidateRecord) for c in candidates)


# ── Strategy mode tests ─────────────────────────────────────────────────────

class TestScreenerServiceStrategyMode:
    """With SkillManager, ScreenerService delegates to StrategyScreeningEngine."""

    @pytest.fixture()
    def skill_manager_with_breakout(self, tmp_path):
        content = """\
name: test_breakout
display_name: 测试突破
description: desc
category: trend
screening:
  filters:
    - field: breakout_ratio
      op: ">="
      value: 0.995
    - field: volume_ratio
      op: ">="
      value: 2.0
  scoring:
    - field: breakout_ratio
      weight: 50
      bonus_above: 1.0
      bonus_multiplier: 500
    - field: volume_ratio
      weight: 30
      cap: 5.0
    - field: trend_score
      weight: 20
instructions: |
  test
"""
        fp = tmp_path / "test_breakout.yaml"
        fp.write_text(content, encoding="utf-8")
        mgr = SkillManager()
        mgr.register(load_skill_from_yaml(fp))
        return mgr

    def test_strategy_evaluate_selects_matching(self, skill_manager_with_breakout):
        snapshot = _make_snapshot(
            _bullish_row("600001", breakout_ratio=1.02, volume_ratio=2.5),
            _bullish_row("600002", breakout_ratio=0.9, volume_ratio=1.0),
        )
        service = ScreenerService(
            skill_manager=skill_manager_with_breakout,
            strategy_names=["test_breakout"],
            min_list_days=120,
        )
        result = service.evaluate(snapshot)

        selected_codes = [c.code for c in result.selected]
        assert "600001" in selected_codes
        assert "600002" not in selected_codes

    def test_strategy_evaluate_returns_matched_strategies(self, skill_manager_with_breakout):
        snapshot = _make_snapshot(
            _bullish_row("600001", breakout_ratio=1.02, volume_ratio=2.5),
        )
        service = ScreenerService(
            skill_manager=skill_manager_with_breakout,
            strategy_names=["test_breakout"],
        )
        result = service.evaluate(snapshot)

        assert hasattr(result.selected[0], "matched_strategies")
        assert "test_breakout" in result.selected[0].matched_strategies

    def test_strategy_evaluate_preserves_candidate_record_interface(self, skill_manager_with_breakout):
        snapshot = _make_snapshot(
            _bullish_row("600001", breakout_ratio=1.02, volume_ratio=2.5),
        )
        service = ScreenerService(
            skill_manager=skill_manager_with_breakout,
            strategy_names=["test_breakout"],
        )
        result = service.evaluate(snapshot)
        c = result.selected[0]

        assert isinstance(c, ScreeningCandidateRecord)
        assert c.code == "600001"
        assert c.rank == 1
        assert c.rule_score > 0
        assert isinstance(c.rule_hits, list)
        assert isinstance(c.factor_snapshot, dict)

    def test_strategy_screen_method_works(self, skill_manager_with_breakout):
        snapshot = _make_snapshot(
            _bullish_row("600001", breakout_ratio=1.02, volume_ratio=2.5),
        )
        service = ScreenerService(
            skill_manager=skill_manager_with_breakout,
            strategy_names=["test_breakout"],
        )
        candidates = service.screen(snapshot, candidate_limit=5)
        assert len(candidates) >= 1
        assert all(isinstance(c, ScreeningCandidateRecord) for c in candidates)


# ── Multi-strategy tests ────────────────────────────────────────────────────

class TestScreenerServiceMultiStrategy:
    @pytest.fixture()
    def multi_strategy_manager(self, tmp_path):
        for name, filters_yaml in [
            ("strat_a", '- {field: volume_ratio, op: ">=", value: 2.0}'),
            ("strat_b", '- {field: trend_score, op: ">=", value: 80}'),
        ]:
            content = f"""\
name: {name}
display_name: {name}
description: desc
screening:
  filters:
    {filters_yaml}
  scoring:
    - field: trend_score
      weight: 100
instructions: |
  test
"""
            fp = tmp_path / f"{name}.yaml"
            fp.write_text(content, encoding="utf-8")
        mgr = SkillManager()
        for fp in tmp_path.glob("*.yaml"):
            mgr.register(load_skill_from_yaml(fp))
        return mgr

    def test_multi_strategy_union(self, multi_strategy_manager):
        snapshot = _make_snapshot(
            _bullish_row("600001", volume_ratio=2.5, trend_score=50),
            _bullish_row("600002", volume_ratio=1.0, trend_score=90),
        )
        service = ScreenerService(
            skill_manager=multi_strategy_manager,
            strategy_names=["strat_a", "strat_b"],
        )
        result = service.evaluate(snapshot)

        selected_codes = {c.code for c in result.selected}
        assert "600001" in selected_codes
        assert "600002" in selected_codes
