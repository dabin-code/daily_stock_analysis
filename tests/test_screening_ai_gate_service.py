"""Tests for ScreeningAiGateService – normalization and decision logic."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.schemas.screening_ai_gate import ScreeningAiGateConfig
from src.services.screening_ai_gate_evidence_builder import EvidencePackage
from src.services.screening_ai_gate_news_builder import NewsDigest
from src.services.screening_ai_gate_service import (
    AiGateDecision,
    ScreeningAiGateService,
)


def _make_config() -> ScreeningAiGateConfig:
    return ScreeningAiGateConfig(
        strategy_name="bottom_divergence_double_breakout",
        version="v1",
        ai_priority=1,
        supported=True,
        playbook={"goal": "test"},
        stage_definitions={
            "confirm_entry_window": {"label": "确认入场窗口"},
            "structure_forming": {"label": "结构形成中"},
        },
        hard_veto_rules=["ST直接reject"],
        news_focus=["行业政策"],
        payload_fields=["signal_strength"],
    )


def _make_sufficient_evidence() -> EvidencePackage:
    return EvidencePackage(
        candidate_meta={"code": "000001.SZ", "name": "平安银行", "close": 12.5},
        strategy_snapshot={"state": "confirmed", "signal_strength": 0.85},
        strategy_raw_evidence={},
        market_filter_snapshot={"volume_ratio": 1.8, "is_st": False},
        data_quality="sufficient",
    )


def _make_insufficient_evidence() -> EvidencePackage:
    return EvidencePackage(
        candidate_meta={"code": "000002.SZ", "name": "万科A", "close": 8.0},
        strategy_snapshot={"state": "rejected"},
        strategy_raw_evidence={},
        market_filter_snapshot={"volume_ratio": 1.2, "is_st": False},
        data_quality="insufficient",
        missing_fields=["bottom_divergence_rebound_high"],
    )


def _make_news() -> NewsDigest:
    return NewsDigest()


class TestAiGateDecisionNormalization:
    """Service normalizes model output into stable AiGateDecision."""

    def test_insufficient_evidence_forces_watch(self):
        service = ScreeningAiGateService()
        decision = service.evaluate(
            config=_make_config(),
            evidence=_make_insufficient_evidence(),
            news_digest=_make_news(),
        )
        assert isinstance(decision, AiGateDecision)
        assert decision.verdict == "watch"
        assert decision.ai_status == "insufficient_data"

    def test_st_stock_triggers_hard_veto(self):
        evidence = _make_sufficient_evidence()
        evidence.market_filter_snapshot["is_st"] = True
        service = ScreeningAiGateService()
        decision = service.evaluate(
            config=_make_config(),
            evidence=evidence,
            news_digest=_make_news(),
        )
        assert decision.verdict == "reject"
        assert "hard_veto" in decision.ai_status

    def test_successful_evaluation_returns_decision(self):
        service = ScreeningAiGateService()
        # Mock LLM call
        mock_response = {
            "verdict": "buy",
            "confidence": 0.8,
            "stage": "confirm_entry_window",
            "primary_action": "建议分批入场",
            "reasoning": "结构确认，量价配合良好",
        }
        with patch.object(service, "_call_llm", return_value=mock_response):
            decision = service.evaluate(
                config=_make_config(),
                evidence=_make_sufficient_evidence(),
                news_digest=_make_news(),
            )
        assert decision.verdict == "buy"
        assert decision.confidence == 0.8
        assert decision.ai_status == "completed"

    def test_llm_failure_degrades_to_failed(self):
        service = ScreeningAiGateService()
        with patch.object(service, "_call_llm", side_effect=Exception("timeout")):
            decision = service.evaluate(
                config=_make_config(),
                evidence=_make_sufficient_evidence(),
                news_digest=_make_news(),
            )
        assert decision.verdict == "watch"
        assert decision.ai_status == "failed"

    def test_invalid_llm_output_degrades_to_failed(self):
        service = ScreeningAiGateService()
        with patch.object(service, "_call_llm", return_value=None):
            decision = service.evaluate(
                config=_make_config(),
                evidence=_make_sufficient_evidence(),
                news_digest=_make_news(),
            )
        assert decision.ai_status == "failed"

    def test_decision_has_strategy_name(self):
        service = ScreeningAiGateService()
        decision = service.evaluate(
            config=_make_config(),
            evidence=_make_insufficient_evidence(),
            news_digest=_make_news(),
        )
        assert decision.strategy_name == "bottom_divergence_double_breakout"

    def test_decision_serializable_to_dict(self):
        service = ScreeningAiGateService()
        decision = service.evaluate(
            config=_make_config(),
            evidence=_make_insufficient_evidence(),
            news_digest=_make_news(),
        )
        d = decision.to_dict()
        assert isinstance(d, dict)
        assert "verdict" in d
        assert "ai_status" in d
        assert "strategy_name" in d
