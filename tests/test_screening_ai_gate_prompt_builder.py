"""Tests for ScreeningAiGatePromptBuilder."""

from __future__ import annotations

import pytest

from src.schemas.screening_ai_gate import ScreeningAiGateConfig
from src.services.screening_ai_gate_evidence_builder import EvidencePackage
from src.services.screening_ai_gate_news_builder import NewsDigest
from src.services.screening_ai_gate_prompt_builder import ScreeningAiGatePromptBuilder


def _make_config() -> ScreeningAiGateConfig:
    return ScreeningAiGateConfig(
        strategy_name="bottom_divergence_double_breakout",
        version="v1",
        ai_priority=1,
        supported=True,
        playbook={
            "goal": "确认底背离双突破结构的有效性",
            "focus": ["验证背离结构", "确认双突破同步性"],
            "output_contract": {
                "verdict": "buy | watch | reject",
                "confidence": "0.0-1.0",
            },
        },
        stage_definitions={
            "confirm_entry_window": {"label": "确认入场窗口", "typical_verdict": "buy"},
            "structure_forming": {"label": "结构形成中", "typical_verdict": "watch"},
        },
        hard_veto_rules=["ST直接reject", "重大利空直接reject"],
        news_focus=["行业政策"],
        payload_fields=["signal_strength", "sync_breakout"],
    )


def _make_evidence() -> EvidencePackage:
    return EvidencePackage(
        candidate_meta={"code": "000001.SZ", "name": "平安银行", "close": 12.5},
        strategy_snapshot={"state": "confirmed", "signal_strength": 0.85},
        strategy_raw_evidence={
            "bottom_divergence_price_low_a": {"idx": 80, "price": 10.5},
            "bottom_divergence_price_low_b": {"idx": 120, "price": 10.8},
        },
        market_filter_snapshot={"volume_ratio": 1.8, "trend_score": 65.0},
        data_quality="sufficient",
    )


def _make_news() -> NewsDigest:
    return NewsDigest(
        structural_positive=["机构调研"],
        structural_negative=[],
        ignored_noise=["日常公告"],
        source_count=3,
    )


class TestPromptBuilderShape:
    """Prompt builder produces well-structured system and user messages."""

    def test_builds_system_and_user_content(self):
        builder = ScreeningAiGatePromptBuilder()
        result = builder.build(
            config=_make_config(),
            evidence=_make_evidence(),
            news_digest=_make_news(),
        )
        assert result["system"] is not None
        assert result["user"] is not None
        assert len(result["system"]) > 0
        assert len(result["user"]) > 0

    def test_prompt_includes_strategy_playbook(self):
        builder = ScreeningAiGatePromptBuilder()
        result = builder.build(
            config=_make_config(),
            evidence=_make_evidence(),
            news_digest=_make_news(),
        )
        assert "确认底背离双突破结构的有效性" in result["user"]

    def test_prompt_includes_stage_definitions(self):
        builder = ScreeningAiGatePromptBuilder()
        result = builder.build(
            config=_make_config(),
            evidence=_make_evidence(),
            news_digest=_make_news(),
        )
        assert "stage_definitions" in result["user"] or "确认入场窗口" in result["user"]

    def test_prompt_omits_dashboard_and_generic_analyzer(self):
        builder = ScreeningAiGatePromptBuilder()
        result = builder.build(
            config=_make_config(),
            evidence=_make_evidence(),
            news_digest=_make_news(),
        )
        assert "dashboard" not in result["user"].lower()
        assert "trend_prediction" not in result["user"]

    def test_prompt_includes_output_contract(self):
        builder = ScreeningAiGatePromptBuilder()
        result = builder.build(
            config=_make_config(),
            evidence=_make_evidence(),
            news_digest=_make_news(),
        )
        # Must mention the verdict options
        assert "buy" in result["user"]
        assert "watch" in result["user"]
        assert "reject" in result["user"]

    def test_prompt_includes_evidence_data(self):
        builder = ScreeningAiGatePromptBuilder()
        result = builder.build(
            config=_make_config(),
            evidence=_make_evidence(),
            news_digest=_make_news(),
        )
        assert "000001.SZ" in result["user"]
        assert "平安银行" in result["user"]

    def test_prompt_includes_news_digest(self):
        builder = ScreeningAiGatePromptBuilder()
        result = builder.build(
            config=_make_config(),
            evidence=_make_evidence(),
            news_digest=_make_news(),
        )
        assert "机构调研" in result["user"]
